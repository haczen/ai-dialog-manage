"""Gemini CLI / Qwen Code session store.

Both tools record chats as single JSON documents:

    ~/.gemini/tmp/<sha256(cwd)>/chats/session-<stamp>-<uuid>.json
    ~/.qwen/tmp/<sha256(cwd)>/chats/session-<stamp>-<uuid>.json

    {"sessionId", "projectHash": sha256(cwd), "startTime", "lastUpdated",
     "messages": [{"id", "timestamp" (ISO), "type": "user"|"gemini",
                   "content": <string>, "model", "tokens"}]}

The project hash is one-way, so scan() takes an optional cwd_hints map
(sha256 -> cwd) built by sources.scan_all() from the other stores' cwds;
unresolved hashes display as the short hash.

Writing side (transplant) always emits type "gemini" for assistant records;
real Qwen recordings may use their own tag, so reading treats anything that
is not "user" as an assistant turn.
"""
from __future__ import annotations

import hashlib
import json
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

from . import config, meta_cache, util
from .models import LoadedSession, Message, Part, SessionMeta
from .record_io import RecordEditor

_TOOLS = ("gemini", "qwen")


def _home(tool: str) -> Path:
    return config.GEMINI_DIR if tool == "gemini" else config.QWEN_DIR


def project_hash(cwd: str) -> str:
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- scan

def scan(progress=None, ctx=None, *, tool: str = "gemini") -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    root = _home(tool)
    if not root.is_dir():
        return metas
    hints = getattr(ctx, "cwd_hints", None) or {}
    files = sorted(root.glob("tmp/*/chats/session-*.json"))
    for n, f in enumerate(files):
        if progress:
            progress(f"{tool} {n + 1}/{len(files)}")
        m = meta_cache.cached_meta(str(f), lambda f=f: _scan_file(f, tool, hints))
        if m and not m.cwd:
            # hints may have improved since this entry was cached
            ph = f.parent.parent.name
            if ph in hints:
                m.cwd = hints[ph]
                m.project = util.path_to_display(m.cwd)
        if m:
            metas.append(m)
    return metas


def _scan_file(f: Path, tool: str, hints: dict[str, str]) -> SessionMeta | None:
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        msgs = doc.get("messages") or []
    except (OSError, ValueError):
        return None
    n_user = sum(1 for r in msgs if r.get("type") == "user")
    n_asst = len(msgs) - n_user
    first_user = next((r.get("content") for r in msgs
                       if r.get("type") == "user"
                       and isinstance(r.get("content"), str)
                       and r.get("content").strip()), "")
    tss = [util.parse_iso(r.get("timestamp")) for r in msgs]
    tss = [t for t in tss if t]
    model = next((r.get("model") for r in msgs
                  if isinstance(r.get("model"), str) and r.get("model")), "")
    ph = doc.get("projectHash") or ""
    cwd = hints.get(ph, "")
    try:
        size = f.stat().st_size
    except OSError:
        size = 0
    return SessionMeta(
        source=tool, id=doc.get("sessionId") or f.stem, path=str(f),
        project=util.path_to_display(cwd) if cwd else (ph[:8] or tool),
        cwd=cwd, title=util.one_line(first_user, 60),
        n_messages=len(msgs), n_user=n_user, n_assistant=n_asst, size=size,
        created=tss[0] if tss else util.parse_iso(doc.get("startTime")),
        updated=tss[-1] if tss else util.parse_iso(doc.get("lastUpdated")),
        model=model, preview=util.one_line(first_user, 100),
    )


# ---------------------------------------------------------------- load

def load(meta: SessionMeta) -> LoadedSession:
    doc = json.loads(Path(meta.path).read_text(encoding="utf-8"))
    records = doc.get("messages") or []
    msgs: list[Message] = []
    for i, rec in enumerate(records):
        m = _to_message(i, rec)
        if m:
            msgs.append(m)
    return LoadedSession(meta=meta, messages=msgs, raw_lines=[],
                         total_lines=len(records))


def _to_message(i: int, rec: dict) -> Message | None:
    text = _content_text(rec.get("content"))
    if not text.strip():
        return None
    role = "user" if rec.get("type") == "user" else "assistant"
    return Message(line=i, role=role, ts=util.parse_iso(rec.get("timestamp")),
                   parts=[Part(kind="text", text=text)],
                   model=rec.get("model"), raw_record=rec)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for k in ("text", "content", "output", "result"):
            v = content.get(k)
            if isinstance(v, str):
                return v
        return json.dumps(content, ensure_ascii=False, indent=2)
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
        return "\n".join(out)
    return ""


def record_text(rec: dict) -> tuple[str, str]:
    """(role, text) for the global searcher."""
    return ("user" if rec.get("type") == "user" else "assistant",
            _content_text(rec.get("content")))


# ---------------------------------------------------------------- edits

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


def get_records(doc: dict) -> list:
    return doc.get("messages") or []


def set_records(doc: dict, records: list) -> None:
    doc["messages"] = records
    doc["lastUpdated"] = _now_iso()


def make_editor(session: LoadedSession) -> RecordEditor:
    return RecordEditor(session.meta.path, get_records, set_records,
                        session.meta.source, session.meta.id)


def linked_lines(session: LoadedSession, target: Message) -> set[int]:
    return {target.line}


def apply_text_edits(ed: RecordEditor, msg: Message,
                     new_texts: dict[int, str]) -> None:
    rec = json.loads(json.dumps(msg.raw_record))  # deep copy
    if 0 in new_texts:
        rec["content"] = new_texts[0]
    ed.replace_record(msg.line, rec)


def apply_raw_edit(ed: RecordEditor, msg: Message, raw_json: str) -> None:
    ed.replace_record(msg.line, json.loads(raw_json))


def append_user_message(ed: RecordEditor, meta: SessionMeta, text: str) -> None:
    rec = {"id": str(uuidlib.uuid4()), "timestamp": _now_iso(),
           "type": "user", "content": text}
    ed.append_record(rec)


def duplicate_session(meta: SessionMeta) -> str | None:
    try:
        src = Path(meta.path)
        doc = json.loads(src.read_text(encoding="utf-8"))
        new_id = str(uuidlib.uuid4())
        doc["sessionId"] = new_id
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        dest = src.parent / f"session-{stamp}-{new_id}.json"
        dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
        return new_id
    except (OSError, ValueError):
        return None
