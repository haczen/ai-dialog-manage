"""Cline / Roo Code task store (VS Code extensions).

    globalStorage/saoudrizwan.claude-dev/tasks/<id>/     (cline)
    globalStorage/rooveterinaryinc.roo-cline/tasks/<id>/ (roo)

    api_conversation_history.json  — the API-level truth: a JSON ARRAY of
        {"role": "user"|"assistant", "ts": <unix ms>,
         "content": [{"type": "text", "thinking", "tool_use", "tool_result",
                      "image", …}]}
    ui_messages.json               — UI-only rendering log (kept untouched)
    task_metadata.json             — misc bookkeeping (kept untouched)

Editing targets api_conversation_history.json only; ui_messages.json keeps
stale conversationHistoryIndex entries, which the extension tolerates on
replay. Cline embeds the working directory in the first user record's
environment block ("Working directory:"), parsed best-effort for cwd.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, meta_cache, util
from .models import LoadedSession, Message, Part, SessionMeta
from .record_io import RecordEditor

_EXTS = {"cline": "saoudrizwan.claude-dev", "roo": "rooveterinaryinc.roo-cline"}
_HISTORY_NAME = "api_conversation_history.json"

_WORKDIR_RE = re.compile(r"Working directory:\s*[`\"]?([^\r\n`\"<]+)", re.I)


def _tasks_dir(tool: str) -> Path:
    return config.CLINE_TASKS if tool == "cline" else config.ROO_TASKS


# ---------------------------------------------------------------- scan

def scan(progress=None, ctx=None, *, tool: str = "cline") -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    root = _tasks_dir(tool)
    if not root.is_dir():
        return metas
    files = sorted(root.glob(f"*/{_HISTORY_NAME}"))
    for n, f in enumerate(files):
        if progress:
            progress(f"{tool} {n + 1}/{len(files)}")
        m = meta_cache.cached_meta(str(f), lambda f=f: _scan_file(f, tool))
        if m:
            metas.append(m)
    return metas


def _scan_file(f: Path, tool: str) -> SessionMeta | None:
    try:
        records = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            return None
    except (OSError, ValueError):
        return None
    n_user = sum(1 for r in records if isinstance(r, dict) and r.get("role") == "user")
    n_asst = len([r for r in records if isinstance(r, dict)]) - n_user
    cwd = _extract_cwd(records)
    first_user = _first_user_text(records)
    tss = [util.ts_from_ms(r.get("ts")) for r in records if isinstance(r, dict)]
    tss = [t for t in tss if t]
    try:
        size = f.stat().st_size
    except OSError:
        size = 0
    return SessionMeta(
        source=tool, id=f.parent.name, path=str(f),
        project=util.path_to_display(cwd) if cwd else f"{tool} task",
        cwd=cwd, title=util.one_line(first_user, 60),
        n_messages=len(records), n_user=n_user, n_assistant=n_asst, size=size,
        created=tss[0] if tss else None,
        updated=tss[-1] if tss else None,
        preview=util.one_line(first_user, 100),
    )


def _extract_cwd(records: list) -> str:
    """Best-effort: the environment block in the first user records embeds
    'Working directory: <path>'."""
    for r in records[:4]:
        if not isinstance(r, dict) or r.get("role") != "user":
            continue
        for p in r.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                m = _WORKDIR_RE.search(p.get("text") or "")
                if m:
                    return m.group(1).strip()
    return ""


def _first_user_text(records: list) -> str:
    for r in records:
        if not isinstance(r, dict) or r.get("role") != "user":
            continue
        for p in r.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                t = (p.get("text") or "").strip()
                if t and not t.startswith("<"):
                    return t
    # everything was harness noise — fall back to the first text at all
    for r in records:
        if not isinstance(r, dict):
            continue
        for p in r.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                t = (p.get("text") or "").strip()
                if t:
                    return t
    return ""


# ---------------------------------------------------------------- load

def load(meta: SessionMeta) -> LoadedSession:
    records = json.loads(Path(meta.path).read_text(encoding="utf-8"))
    msgs: list[Message] = []
    for i, rec in enumerate(records):
        m = _to_message(i, rec)
        if m:
            msgs.append(m)
    return LoadedSession(meta=meta, messages=msgs, raw_lines=[],
                         total_lines=len(records))


def _to_message(i: int, rec: dict) -> Message | None:
    if not isinstance(rec, dict):
        return None
    parts: list[Part] = []
    for p in rec.get("content") or []:
        if not isinstance(p, dict):
            parts.append(Part(kind="text", text=str(p)))
            continue
        pt = p.get("type") or ""
        if pt == "text":
            parts.append(Part(kind="text", text=p.get("text") or "", raw=p))
        elif pt == "thinking":
            parts.append(Part(kind="thinking",
                              text=p.get("thinking") or p.get("text") or "", raw=p))
        elif pt == "tool_use":
            parts.append(Part(kind="tool_call", name=p.get("name") or "?",
                              text=_pretty(p.get("input")),
                              tool_id=p.get("id") or "", raw=p))
        elif pt == "tool_result":
            parts.append(Part(kind="tool_result", text=_result_text(p.get("content")),
                              tool_id=p.get("tool_use_id") or "", raw=p))
        elif pt == "image":
            parts.append(Part(kind="tool_result", text="[image]", raw=p))
        else:
            parts.append(Part(kind="tool_result", name=pt, text=_pretty(p), raw=p))
    if not parts:
        return None
    return Message(line=i, role=rec.get("role") or "user",
                   ts=util.ts_from_ms(rec.get("ts")), parts=parts,
                   raw_record=rec)


def _pretty(obj) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(obj)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    out.append(p.get("text") or "")
                elif p.get("type") == "image":
                    out.append("[image]")
        return "\n".join(out)
    return str(content or "")


def record_text(rec: dict) -> tuple[str, str]:
    """(role, text) for the global searcher."""
    chunks = []
    for p in rec.get("content") or []:
        if isinstance(p, dict):
            pt = p.get("type") or ""
            if pt in ("text", "thinking"):
                chunks.append(p.get("text") or p.get("thinking") or "")
            elif pt == "tool_use":
                chunks.append(_pretty(p.get("input")))
            elif pt == "tool_result":
                chunks.append(_result_text(p.get("content")))
            else:
                chunks.append(_pretty(p))
        else:
            chunks.append(str(p))
    return (rec.get("role") or "user", "\n".join(chunks))


# ---------------------------------------------------------------- edits

def get_records(doc: list) -> list:
    return doc


def set_records(doc: list, records: list) -> None:
    doc[:] = records


def make_editor(session: LoadedSession) -> RecordEditor:
    return RecordEditor(session.meta.path, get_records, set_records,
                        session.meta.source, session.meta.id)


def linked_lines(session: LoadedSession, target: Message) -> set[int]:
    """tool_use in an assistant record pairs with tool_result in a user
    record — delete them together (mirrors the claude store)."""
    ids = target.tool_ids()
    out = {target.line}
    if not ids:
        return out
    for m in session.messages:
        if m.line != target.line and (m.tool_ids() & ids):
            out.add(m.line)
    return out


def apply_text_edits(ed: RecordEditor, msg: Message,
                     new_texts: dict[int, str]) -> None:
    rec = json.loads(json.dumps(msg.raw_record))  # deep copy
    content = rec.get("content")
    if isinstance(content, list):
        for i, p in enumerate(content):
            if i not in new_texts or not isinstance(p, dict):
                continue
            v = new_texts[i]
            pt = p.get("type")
            if pt == "text":
                p["text"] = v
            elif pt == "thinking":
                p["thinking"] = v
            elif pt == "tool_use":
                try:
                    p["input"] = json.loads(v)
                except ValueError:
                    p["input"] = v
            elif pt == "tool_result":
                p["content"] = v
            else:
                try:
                    content[i] = json.loads(v)
                except ValueError:
                    p["text"] = v
    ed.replace_record(msg.line, rec)


def apply_raw_edit(ed: RecordEditor, msg: Message, raw_json: str) -> None:
    ed.replace_record(msg.line, json.loads(raw_json))


def append_user_message(ed: RecordEditor, meta: SessionMeta, text: str) -> None:
    rec = {"role": "user", "ts": int(time.time() * 1000),
           "content": [{"type": "text", "text": text}]}
    ed.append_record(rec)


def duplicate_session(meta: SessionMeta) -> str | None:
    try:
        src_dir = Path(meta.path).parent
        new_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        dest = src_dir.parent / new_id
        shutil.copytree(src_dir, dest)
        return new_id
    except (OSError, ValueError):
        return None
