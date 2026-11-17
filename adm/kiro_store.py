"""Kiro IDE (AWS) session store — exploratory reader + editor.

Kiro is a VS Code fork, so per-workspace data follows the VS Code layout:

    Windows:  %APPDATA%\\Kiro\\User\\workspaceStorage\\<md5>\\
    macOS:    ~/Library/Application Support/Kiro/User/workspaceStorage/<md5>/
    Linux:    ~/.config/Kiro/User/workspaceStorage/<md5>/

Each workspace folder carries a workspace.json ({"folder": "file:///d%3A/..."})
that resolves back to the project directory. Agent chat history lives as JSON
files somewhere under that folder; Kiro's exact filename/layout has changed
across releases, so instead of hardcoding one path this store PROBES: any
*.json under a workspace whose parsed doc contains a list of role-bearing
records under a "messages"-like key is treated as a session transcript.
Records map loosely (role == "user" -> user, anything else -> assistant),
and editing goes through RecordEditor against the same probed container,
so envelope fields survive untouched.

If Kiro changes its storage again, scan() simply returns fewer/zero
sessions instead of breaking the app.
"""
from __future__ import annotations

import json
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import util
from .models import LoadedSession, Message, Part, SessionMeta
from .record_io import RecordEditor

_MAX_JSON = 64 * 1024 * 1024  # skip monster files (not chat transcripts)
_LIST_KEYS = ("messages", "chatMessages", "turns", "conversation", "history")


def _kiro_user_dir() -> Path:
    import os
    import sys
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA")
                    or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Kiro" / "User"


# ---------------------------------------------------------------- scan

def scan(progress=None, ctx=None) -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    for root in (_kiro_user_dir() / "workspaceStorage",
                 _kiro_user_dir() / "globalStorage"):
        if not root.is_dir():
            continue
        workspaces = sorted(d for d in root.iterdir() if d.is_dir())
        for n, ws in enumerate(workspaces):
            if progress:
                progress(f"kiro {n + 1}/{len(workspaces)}")
            cwd = _workspace_cwd(ws / "workspace.json")
            for f in sorted(ws.rglob("*.json")):
                if f.name == "workspace.json" or f.stat().st_size > _MAX_JSON:
                    continue
                m = _scan_file(f, cwd)
                if m:
                    metas.append(m)
        if workspaces:
            break  # workspaceStorage found sessions; skip globalStorage
    return metas


def _workspace_cwd(wj: Path) -> str:
    """VS Code workspace.json -> folder path (file:// URI, percent-encoded)."""
    try:
        uri = json.loads(wj.read_text(encoding="utf-8")).get("folder", "")
    except (OSError, ValueError):
        return ""
    if not uri:
        return ""
    try:
        p = urlparse(uri)
        if p.scheme == "file":
            path = unquote(p.path)
            if p.netloc and p.netloc not in ("", "localhost"):
                path = f"//{p.netloc}{path}"
            return path
    except ValueError:
        pass
    return ""


def _probe_records(doc) -> list | None:
    """Find the list of chat records inside a parsed JSON doc: either at the
    top level under a messages-like key, or one nesting level deeper."""
    if not isinstance(doc, dict):
        return None
    for key in _LIST_KEYS:
        v = doc.get(key)
        if _looks_like_records(v):
            return v
    for v in doc.values():
        if isinstance(v, dict):
            for key in _LIST_KEYS:
                inner = v.get(key)
                if _looks_like_records(inner):
                    return inner
    return None


def _looks_like_records(v) -> bool:
    if not isinstance(v, list) or not v:
        return False
    hits = sum(1 for r in v[:20]
               if isinstance(r, dict) and "role" in r
               and ("content" in r or "text" in r or "message" in r))
    return hits >= max(1, min(3, len(v[:20]) // 2))


def _scan_file(f: Path, cwd: str) -> SessionMeta | None:
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    records = _probe_records(doc)
    if not records:
        return None
    n_user = sum(1 for r in records if isinstance(r, dict)
                 and r.get("role") == "user")
    first_user = next((_record_text(r) for r in records
                       if isinstance(r, dict) and r.get("role") == "user"
                       and _record_text(r).strip()), "")
    tss = [_record_ts(r) for r in records if isinstance(r, dict)]
    tss = [t for t in tss if t]
    return SessionMeta(
        source="kiro", id=f.parent.name + "-" + f.stem, path=str(f),
        project=util.path_to_display(cwd) if cwd else "kiro",
        cwd=cwd, title=util.one_line(first_user, 60),
        n_messages=len(records), n_user=n_user, n_assistant=len(records) - n_user,
        size=f.stat().st_size,
        created=tss[0] if tss else None,
        updated=tss[-1] if tss else None,
        preview=util.one_line(first_user, 100),
    )


# ---------------------------------------------------------------- load

def load(meta: SessionMeta) -> LoadedSession:
    doc = json.loads(Path(meta.path).read_text(encoding="utf-8"))
    records = _probe_records(doc) or []
    msgs: list[Message] = []
    for i, rec in enumerate(records):
        m = _to_message(i, rec)
        if m:
            msgs.append(m)
    return LoadedSession(meta=meta, messages=msgs, raw_lines=[],
                         total_lines=len(records))


def _to_message(i: int, rec: dict) -> Message | None:
    text = _record_text(rec)
    parts = [Part(kind="text", text=text)] if text.strip() else []
    # tool-call-ish records (Kiro nests tool use in various shapes)
    for key in ("toolUse", "tool_use", "functionCall"):
        t = rec.get(key)
        if isinstance(t, dict):
            parts.append(Part(kind="tool_call", name=t.get("name") or key,
                              text=json.dumps(t, ensure_ascii=False, indent=2),
                              raw=t))
    if not parts:
        return None
    return Message(line=i,
                   role="user" if rec.get("role") == "user" else "assistant",
                   ts=_record_ts(rec), parts=parts, raw_record=rec)


def _record_text(rec: dict) -> str:
    c = rec.get("content", rec.get("text", rec.get("message")))
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for p in c:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                t = p.get("text") or p.get("content") or ""
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    if isinstance(c, dict):
        t = c.get("text") or c.get("content") or ""
        return t if isinstance(t, str) else ""
    return ""


def _record_ts(rec: dict):
    ts = rec.get("timestamp") or rec.get("created") or rec.get("createdAt") \
        or rec.get("time")
    if isinstance(ts, (int, float)):
        return util.ts_from_ms(ts if ts > 1e11 else ts * 1000)
    if isinstance(ts, str):
        return util.parse_iso(ts)
    return None


# ---------------------------------------------------------------- search

def record_text(rec: dict) -> tuple[str, str]:
    """(role, text) for the global searcher."""
    return ("user" if rec.get("role") == "user" else "assistant",
            _record_text(rec))


# ---------------------------------------------------------------- edits

def _records_container(doc):
    """(holder_dict, list_key) so edits write back into the probed place."""
    for key in _LIST_KEYS:
        v = doc.get(key)
        if _looks_like_records(v):
            return doc, key
    for v in doc.values():
        if isinstance(v, dict):
            for key in _LIST_KEYS:
                inner = v.get(key)
                if _looks_like_records(inner):
                    return v, key
    return None, None


def get_records(doc) -> list:
    """Records of a parsed transcript doc (used by the global searcher)."""
    return _probe_records(doc) or []


def make_editor(session: LoadedSession) -> RecordEditor:
    doc = json.loads(Path(session.meta.path).read_text(encoding="utf-8"))

    def get_records(d):
        holder, key = _records_container(d)
        return (holder or {}).get(key) or [] if key else []

    def set_records(d, records):
        holder, key = _records_container(d)
        if key:
            holder[key] = records

    return RecordEditor(session.meta.path, get_records, set_records,
                        "kiro", session.meta.id)


def linked_lines(session: LoadedSession, target: Message) -> set[int]:
    return {target.line}


def apply_text_edits(ed: RecordEditor, msg: Message,
                     new_texts: dict[int, str]) -> None:
    rec = json.loads(json.dumps(msg.raw_record))  # deep copy
    if 0 in new_texts:
        c = rec.get("content", rec.get("text", rec.get("message")))
        if isinstance(c, str) or c is None:
            for k in ("text", "message"):
                if k in rec:
                    rec[k] = new_texts[0]
                    break
            else:
                rec["content"] = new_texts[0]
        else:
            # structured content: overwrite the first text-bearing entry
            done = False
            if isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and isinstance(p.get("text"), str) \
                            and not done:
                        p["text"] = new_texts[0]
                        done = True
                    elif isinstance(p, str) and not done:
                        pass
            if not done:
                rec["content"] = new_texts[0]
    ed.replace_record(msg.line, rec)


def apply_raw_edit(ed: RecordEditor, msg: Message, raw_json: str) -> None:
    ed.replace_record(msg.line, json.loads(raw_json))


def append_user_message(ed: RecordEditor, meta: SessionMeta, text: str) -> None:
    ed.append_record({"role": "user", "content": text,
                      "timestamp": int(datetime.now(timezone.utc).timestamp()
                                       * 1000)})
