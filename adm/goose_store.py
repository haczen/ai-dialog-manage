r"""Goose session store.

    Windows: %APPDATA%\Block\goose\data\sessions\<id>.json
    Linux/macOS: ~/.local/share/goose/sessions/<id>.json

    {"id", "description" (title), "working_dir",
     "conversation": {"messages": [{"role": "user"|"assistant",
        "created": <unix seconds>, "content": [{"type": "text", "text"},
        ...tool blocks]}]},
     "message_count", "created_at", "updated_at"}

Timestamps: per-message `created` is unix SECONDS; envelope dates are ISO.
Content entries beyond plain "text" (toolRequest / toolResponse / image …)
are shown as pretty JSON and stay editable as JSON.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config, meta_cache, util
from .models import LoadedSession, Message, Part, SessionMeta
from .record_io import RecordEditor

# content types that represent a tool call (vs a result / attachment)
_CALL_TYPES = ("toolRequest", "toolRequestStatus", "commandRequest",
               "inputControl")


# ---------------------------------------------------------------- scan

def scan(progress=None, ctx=None) -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    root = config.goose_sessions_dir()
    if not root.is_dir():
        return metas
    files = sorted(root.glob("*.json"))
    for n, f in enumerate(files):
        if progress:
            progress(f"goose {n + 1}/{len(files)}")
        m = meta_cache.cached_meta(str(f), lambda f=f: _scan_file(f))
        if m:
            metas.append(m)
    return metas


def _scan_file(f: Path) -> SessionMeta | None:
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        msgs = (doc.get("conversation") or {}).get("messages") or []
    except (OSError, ValueError):
        return None
    n_user = sum(1 for r in msgs if r.get("role") == "user")
    n_asst = len(msgs) - n_user
    first_user = next((r.get("content") and _first_text(r.get("content"))
                       for r in msgs if r.get("role") == "user"), "") or ""
    cwd = doc.get("working_dir") or ""
    try:
        size = f.stat().st_size
    except OSError:
        size = 0
    tss = [util.ts_from_ms((r.get("created") or 0) * 1000) for r in msgs]
    tss = [t for t in tss if t]
    return SessionMeta(
        source="goose", id=doc.get("id") or f.stem, path=str(f),
        project=util.path_to_display(cwd) if cwd else "goose",
        cwd=cwd, title=util.one_line(
            doc.get("description") or first_user, 60),
        n_messages=len(msgs), n_user=n_user, n_assistant=n_asst, size=size,
        created=tss[0] if tss else util.parse_iso(doc.get("created_at")),
        updated=tss[-1] if tss else util.parse_iso(doc.get("updated_at")),
        preview=util.one_line(first_user, 100),
    )


def _first_text(content) -> str:
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text" \
                    and isinstance(p.get("text"), str):
                return p["text"]
    return ""


# ---------------------------------------------------------------- load

def load(meta: SessionMeta) -> LoadedSession:
    doc = json.loads(Path(meta.path).read_text(encoding="utf-8"))
    records = (doc.get("conversation") or {}).get("messages") or []
    msgs: list[Message] = []
    for i, rec in enumerate(records):
        m = _to_message(i, rec)
        if m:
            msgs.append(m)
    return LoadedSession(meta=meta, messages=msgs, raw_lines=[],
                         total_lines=len(records))


def _to_message(i: int, rec: dict) -> Message | None:
    content = rec.get("content")
    if not isinstance(content, list):
        content = [{"type": "text", "text": str(content or "")}]
    parts: list[Part] = []
    for j, p in enumerate(content):
        if not isinstance(p, dict):
            parts.append(Part(kind="text", text=str(p)))
            continue
        pt = p.get("type") or ""
        if pt == "text":
            parts.append(Part(kind="text", text=p.get("text") or "", raw=p))
            continue
        # tool traffic / attachments -> readable JSON
        kind = "tool_call" if any(t in pt for t in _CALL_TYPES) else "tool_result"
        parts.append(Part(kind=kind, name=pt, text=_pretty(p), raw=p))
    if not parts:
        return None
    role = rec.get("role") or "user"
    ts = util.ts_from_ms((rec.get("created") or 0) * 1000)
    return Message(line=i, role=role, ts=ts, parts=parts, raw_record=rec)


def _pretty(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(obj)


def record_text(rec: dict) -> tuple[str, str]:
    """(role, text) for the global searcher."""
    chunks = []
    for p in rec.get("content") or []:
        if isinstance(p, dict):
            chunks.append(p.get("text") if p.get("type") == "text"
                          else _pretty(p))
        else:
            chunks.append(str(p))
    return (rec.get("role") or "user", "\n".join(chunks))


# ---------------------------------------------------------------- edits

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


def get_records(doc: dict) -> list:
    return (doc.get("conversation") or {}).get("messages") or []


def set_records(doc: dict, records: list) -> None:
    doc.setdefault("conversation", {})["messages"] = records
    doc["message_count"] = len(records)
    doc["updated_at"] = _now_iso()


def make_editor(session: LoadedSession) -> RecordEditor:
    return RecordEditor(session.meta.path, get_records, set_records,
                        "goose", session.meta.id)


def linked_lines(session: LoadedSession, target: Message) -> set[int]:
    return {target.line}


def apply_text_edits(ed: RecordEditor, msg: Message,
                     new_texts: dict[int, str]) -> None:
    rec = json.loads(json.dumps(msg.raw_record))  # deep copy
    content = rec.get("content")
    if isinstance(content, list):
        for i, p in enumerate(content):
            if i not in new_texts or not isinstance(p, dict):
                continue
            v = new_texts[i]
            if p.get("type") == "text":
                p["text"] = v
            else:
                # tool blocks edit as JSON (mirrors claude tool_use handling)
                try:
                    content[i] = json.loads(v)
                except ValueError:
                    p["text"] = v
    ed.replace_record(msg.line, rec)


def apply_raw_edit(ed: RecordEditor, msg: Message, raw_json: str) -> None:
    ed.replace_record(msg.line, json.loads(raw_json))


def append_user_message(ed: RecordEditor, meta: SessionMeta, text: str) -> None:
    rec = {"role": "user", "created": int(datetime.now(timezone.utc).timestamp()),
           "content": [{"type": "text", "text": text}]}
    ed.append_record(rec)


def duplicate_session(meta: SessionMeta) -> str | None:
    try:
        src = Path(meta.path)
        doc = json.loads(src.read_text(encoding="utf-8"))
        new_id = datetime.now().strftime("%Y%m%d_%H%M%S") \
            + f"-{meta.id[-6:]}"
        doc["id"] = new_id
        dest = src.parent / f"{new_id}.json"
        dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
        return new_id
    except (OSError, ValueError):
        return None
