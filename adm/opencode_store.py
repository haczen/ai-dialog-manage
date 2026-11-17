"""OpenCode 2.x session store.

Sessions live in `session_v2`, messages in `session_message`:

    session_v2: id (ses_*), project_id, directory (cwd), title,
        model (JSON text), agent, cost, tokens_input/output,
        time_created/time_updated (unix ms), time_archived
    session_message: id, session_id, type in {user, assistant, idle,
        model-switched, synthetic}, seq, time_created (ms), data (JSON)

    user data:      {time: {created}, text, files, agents}
    assistant data: {time, agent, model, content: [{type: "reasoning"|
                       "tool"|"text", …}], finish, cost, tokens}
    tool block:     {type: "tool", id, name, executed,
                     state: {status, input, content: [{type: "text", text,
                     metadata}], metadata}}

The `event` table is unused by OpenCode 2.x (0 rows observed) —
session_message IS the store of truth. `event_sequence.seq` is the
per-session sequence allocator and always >= max(message seq): appends
allocate from it (and advance it) so the running service never collides
on the UNIQUE (session_id, seq) index.

Writes are guarded like every other store: sqlite-backup-API snapshot ->
per-row external-change guard -> short BEGIN IMMEDIATE transaction.
Message.line carries session_message.seq so global-search hits land on
the right message after the jump.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid as uuidlib
from pathlib import Path
from urllib.parse import quote as _uriquote

from . import config, util
from .jsonlio import FileChangedError
from .models import LoadedSession, Message, Part, SessionMeta


def connect_ro(path: Path | None = None) -> sqlite3.Connection:
    db = Path(path or config.OPENCODE_DB)
    uri = f"file:{_uriquote(str(db))}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=4)
    con.row_factory = sqlite3.Row
    return con


def connect_rw(path: Path | None = None) -> sqlite3.Connection:
    db = Path(path or config.OPENCODE_DB)
    uri = f"file:{_uriquote(str(db))}?mode=rw"
    con = sqlite3.connect(uri, uri=True, timeout=8, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=8000")
    return con


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------- scan

def scan(progress=None, ctx=None) -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    db = config.OPENCODE_DB
    if not db.exists():
        return metas
    try:
        con = connect_ro(db)
    except sqlite3.Error:
        return metas
    try:
        if not {"session_v2", "session_message"} <= _tables(con):
            if progress:
                progress("opencode: schema not recognised, skipped")
            return metas
        rows = con.execute(
            "SELECT id, directory, title, model, agent, tokens_input,"
            " tokens_output, time_created, time_updated, time_archived"
            " FROM session_v2 ORDER BY time_updated DESC").fetchall()
        counts: dict[str, int] = {}
        for r in con.execute(
                "SELECT session_id, COUNT(*) FROM session_message"
                " WHERE type IN ('user','assistant')"
                " GROUP BY session_id"):
            counts[r[0]] = r[1]
        for k, r in enumerate(rows):
            if progress and k % 10 == 0:
                progress(f"opencode {k + 1}/{len(rows)}")
            cwd = util.strip_verbatim(r["directory"] or "")
            try:
                model = (json.loads(r["model"] or "{}") or {}).get("id", "")
            except ValueError:
                model = ""
            m = SessionMeta(
                source="opencode", id=r["id"], path=str(db),
                project=util.path_to_display(cwd) if cwd else "opencode",
                cwd=cwd, title=util.collapse_ws(r["title"] or ""),
                n_messages=counts.get(r["id"], 0), size=db.stat().st_size,
                created=util.ts_from_ms(r["time_created"]),
                updated=util.ts_from_ms(r["time_updated"]),
                model=model,
                tokens=(r["tokens_input"] or 0) + (r["tokens_output"] or 0),
                archived=bool(r["time_archived"]),
                running=_db_recently_touched(db),
            )
            if not m.title:
                m.title = m.id[:12]
            metas.append(m)
        # per-session sizes: sum of message data lengths (cheap enough at
        # OpenCode scale, and there is no per-session file to stat)
        sizes: dict[str, int] = {}
        for r in con.execute(
                "SELECT session_id, SUM(LENGTH(data)) FROM session_message"
                " GROUP BY session_id"):
            sizes[r[0]] = r[1] or 0
        for m in metas:
            m.size = sizes.get(m.id, m.size)
            first = con.execute(
                "SELECT data FROM session_message WHERE session_id=? AND"
                " type='user' ORDER BY seq LIMIT 1", (m.id,)).fetchone()
            if first is not None:
                try:
                    text = (json.loads(first[0]) or {}).get("text") or ""
                except ValueError:
                    text = ""
                if text:
                    m.preview = util.one_line(text, 100)
                    if m.title == m.id[:12]:
                        m.title = util.one_line(text, 60)
        return metas
    except sqlite3.Error:
        return metas
    finally:
        con.close()


def _db_recently_touched(db: Path, seconds: float = 180.0) -> bool:
    for suffix in ("", "-wal", "-shm"):
        try:
            if time.time() - db.with_name(db.name + suffix).stat().st_mtime \
                    < seconds:
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- load

def load(meta: SessionMeta) -> LoadedSession:
    con = connect_ro()
    try:
        rows = con.execute(
            "SELECT seq, type, time_created, data FROM session_message"
            " WHERE session_id=? ORDER BY seq", (meta.id,)).fetchall()
    finally:
        con.close()
    msgs: list[Message] = []
    for r in rows:
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        m = _to_message(r["seq"], r["type"], r["time_created"], data)
        if m:
            msgs.append(m)
    return LoadedSession(meta=meta, messages=msgs, raw_lines=[],
                         total_lines=len(rows))


def _to_message(seq: int, typ: str, created_ms, data: dict) -> Message | None:
    t = data.get("time") or {}
    ts = util.ts_from_ms(t.get("created") or t.get("completed") or created_ms)
    if typ == "user":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        if text.lstrip().startswith("<"):  # harness/context injections
            return None
        return Message(line=seq, role="user", ts=ts,
                       parts=[Part(kind="text", text=text)], raw_record=data)
    if typ == "assistant":
        parts, _slots = _content_parts(data)
        if not parts:
            return None
        model = ""
        mdl = data.get("model")
        if isinstance(mdl, dict):
            model = mdl.get("id") or ""
        return Message(line=seq, role="assistant", ts=ts, parts=parts,
                       model=model, raw_record=data)
    return None  # idle / model-switched / synthetic / …


def _content_parts(data: dict) -> tuple[list[Part], list[tuple]]:
    """Build display parts for an assistant record plus, parallel to each
    part, a writeback slot (kind, content-block) used by apply_text_edits
    so part indices map 1:1 both ways."""
    parts: list[Part] = []
    slots: list[tuple] = []
    for p in data.get("content") or []:
        if not isinstance(p, dict):
            parts.append(Part(kind="text", text=str(p)))
            slots.append(("none", None))
            continue
        pt = p.get("type") or ""
        if pt == "reasoning":
            parts.append(Part(kind="thinking", text=p.get("text") or "", raw=p))
            slots.append(("thinking", p))
        elif pt == "text":
            parts.append(Part(kind="text", text=p.get("text") or "", raw=p))
            slots.append(("text", p))
        elif pt == "tool":
            state = p.get("state") or {}
            inp = state.get("input")
            pretty = inp if isinstance(inp, str) else (
                json.dumps(inp, ensure_ascii=False, indent=2)
                if inp is not None else "")
            parts.append(Part(kind="tool_call",
                              name=p.get("name") or "tool",
                              text=pretty, tool_id=p.get("id") or "",
                              raw=p))
            slots.append(("tool_input", p))
            out = []
            for c in state.get("content") or []:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    out.append(c["text"])
            if out:
                parts.append(Part(kind="tool_result", text="\n".join(out),
                                  tool_id=p.get("id") or "", raw=p))
                slots.append(("tool_result", p))
        else:
            parts.append(Part(kind="tool_result", name=pt,
                              text=json.dumps(p, ensure_ascii=False,
                                              indent=2), raw=p))
            slots.append(("json", p))
    return parts, slots


# ---------------------------------------------------------------- search

def message_text(typ: str, data_str: str) -> tuple[str, str]:
    """(role, searchable text) for one session_message row."""
    try:
        data = json.loads(data_str or "{}")
    except ValueError:
        return "", ""
    if typ == "user":
        text = data.get("text")
        return "user", text if isinstance(text, str) else ""
    if typ == "assistant":
        chunks = []
        for p in data.get("content") or []:
            if not isinstance(p, dict):
                continue
            pt = p.get("type") or ""
            if pt in ("reasoning", "text"):
                chunks.append(p.get("text") or "")
            elif pt == "tool":
                state = p.get("state") or {}
                inp = state.get("input")
                chunks.append(inp if isinstance(inp, str) else
                              json.dumps(inp, ensure_ascii=False)
                              if inp is not None else "")
                for c in state.get("content") or []:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        chunks.append(c["text"])
        return "assistant", "\n".join(chunks)
    return "", ""


# ---------------------------------------------------------------- edits

def _backup_db(sid: str) -> None:
    """Consistent snapshot of the db (WAL included) via the sqlite backup
    API — a raw file copy could miss commits still sitting in the -wal."""
    from .jsonlio import _prune
    d = config.BACKUP_DIR / "opencode" / sid
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + \
        f"-{int(time.time() * 1000) % 1000:03d}"
    src = sqlite3.connect(f"file:{_uriquote(str(config.OPENCODE_DB))}?mode=ro",
                          uri=True, timeout=4)
    dest = sqlite3.connect(str(d / f"{stamp}.db"))
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
    _prune(d, ".db")


class OpenCodeEditor:
    """Row-level edits on session_message with the same semantics as
    jsonlio.SessionEditor: backup -> external-change guard (every snapshot
    row must be untouched on disk; externally appended rows are kept) ->
    short BEGIN IMMEDIATE transaction."""

    def __init__(self, meta: SessionMeta):
        self.sid = meta.id
        con = connect_ro()
        try:
            rows = con.execute(
                "SELECT seq, id, type, data FROM session_message"
                " WHERE session_id=? ORDER BY seq", (self.sid,)).fetchall()
        finally:
            con.close()
        # seq -> (id, type, data-json-text)
        self._snap: dict[int, tuple[str, str, str]] = {
            r[0]: (r[1], r[2], r[3] or "") for r in rows}
        self._replaced: dict[int, dict] = {}
        self._deleted: set[int] = set()
        self._appended: list[tuple[str, dict]] = []
        self.dirty = False

    @property
    def snapshot(self) -> dict[int, tuple[str, str, str]]:
        return self._snap

    def replace_record(self, seq: int, data: dict) -> None:
        self._replaced[seq] = data
        self.dirty = True

    def delete_record(self, seq: int) -> None:
        self._deleted.add(seq)
        self.dirty = True

    def delete_records(self, seqs) -> None:
        for s in seqs:
            self.delete_record(s)

    def append_record(self, type_: str, data: dict) -> None:
        self._appended.append((type_, data))
        self.dirty = True

    def save(self) -> int:
        if not self.dirty:
            return len(self._snap)
        _backup_db(self.sid)
        now = int(time.time() * 1000)
        con = connect_rw()
        try:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT seq, id, type, data FROM session_message"
                " WHERE session_id=? ORDER BY seq", (self.sid,)).fetchall()
            current = {r[0]: (r[1], r[2], r[3] or "") for r in rows}
            max_snap = max(self._snap, default=0)
            for seq, snap_row in self._snap.items():
                if current.get(seq) != snap_row:
                    raise FileChangedError(
                        f"message seq {seq} changed on disk since load")
            for seq in self._deleted:
                con.execute("DELETE FROM session_message WHERE id=?",
                            (self._snap[seq][0],))
            for seq, data in self._replaced.items():
                con.execute(
                    "UPDATE session_message SET data=?, time_updated=?"
                    " WHERE id=?",
                    (json.dumps(data, ensure_ascii=False), now,
                     self._snap[seq][0]))
            # allocate seqs strictly above the service's counter AND any
            # externally appended rows, then advance the counter so the
            # running OpenCode never collides on (session_id, seq)
            ev = con.execute(
                "SELECT seq FROM event_sequence WHERE aggregate_id=?",
                (self.sid,)).fetchone()
            next_seq = max((ev[0] if ev else 0), max(current, default=0),
                           max_snap) + 1
            last_seq = next_seq - 1
            for typ, data in self._appended:
                con.execute(
                    "INSERT INTO session_message (id, session_id, type, seq,"
                    " time_created, time_updated, data)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("msg_" + uuidlib.uuid4().hex, self.sid, typ, next_seq,
                     now, now, json.dumps(data, ensure_ascii=False)))
                last_seq = next_seq
                next_seq += 1
            if ev is None:
                con.execute("INSERT INTO event_sequence (aggregate_id, seq)"
                            " VALUES (?, ?)", (self.sid, last_seq))
            else:
                con.execute("UPDATE event_sequence SET seq=max(seq, ?)"
                            " WHERE aggregate_id=?", (last_seq, self.sid))
            con.execute("UPDATE session_v2 SET time_updated=? WHERE id=?",
                        (now, self.sid))
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        kept = len(self._snap) - len(self._deleted) + len(self._appended)
        self._snap = {s: v for s, v in self._snap.items()
                      if s not in self._deleted}
        self._replaced.clear()
        self._deleted.clear()
        self._appended.clear()
        self.dirty = False
        return kept


def make_editor(session: LoadedSession) -> OpenCodeEditor:
    return OpenCodeEditor(session.meta)


def linked_lines(session: LoadedSession, target: Message) -> set[int]:
    # tool calls and their results live in the SAME assistant record —
    # deleting the record removes both, no cross-message pairing needed
    return {target.line}


def apply_text_edits(ed: OpenCodeEditor, msg: Message,
                     new_texts: dict[int, str]) -> None:
    data = json.loads(json.dumps(msg.raw_record))  # deep copy
    if msg.role == "user":
        if 0 in new_texts:
            data["text"] = new_texts[0]
    else:
        _parts, slots = _content_parts(data)
        for i, (_part, (kind, block)) in enumerate(zip(_parts, slots)):
            if i not in new_texts:
                continue
            v = new_texts[i]
            if kind in ("text", "thinking"):
                block["text"] = v
            elif kind == "tool_input":
                st = block.setdefault("state", {})
                try:
                    st["input"] = json.loads(v)
                except ValueError:
                    st["input"] = v
            elif kind == "tool_result":
                st = block.setdefault("state", {})
                cont = st.get("content")
                if isinstance(cont, list) and cont \
                        and isinstance(cont[0], dict):
                    cont[0]["text"] = v
                else:
                    st["content"] = [{"type": "text", "text": v}]
            elif kind == "json":
                try:
                    new_block = json.loads(v)
                    if isinstance(new_block, dict):
                        block.clear()
                        block.update(new_block)
                except ValueError:
                    pass
    ed.replace_record(msg.line, data)


def apply_raw_edit(ed: OpenCodeEditor, msg: Message, raw_json: str) -> None:
    ed.replace_record(msg.line, json.loads(raw_json))


def append_user_message(ed: OpenCodeEditor, meta: SessionMeta, text: str) -> None:
    now = int(time.time() * 1000)
    ed.append_record("user", {"time": {"created": now}, "text": text,
                              "files": [], "agents": []})


# ---------------------------------------------------------------- session ops

def rename_session(sid: str, title: str) -> None:
    con = connect_rw()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE session_v2 SET title=?, time_updated=? WHERE id=?",
                    (title, int(time.time() * 1000), sid))
        con.execute("COMMIT")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


def duplicate_session(meta: SessionMeta) -> str | None:
    try:
        con = connect_rw()
        try:
            row = con.execute("SELECT * FROM session_v2 WHERE id=?",
                              (meta.id,)).fetchone()
            if row is None:
                return None
            con.execute("BEGIN IMMEDIATE")
            now = int(time.time() * 1000)
            new_id = "ses_" + uuidlib.uuid4().hex[:24]
            con.execute(
                "INSERT INTO session_v2 (id, project_id, workspace_id,"
                " parent_id, fork_session_id, directory, path, title, slug,"
                " version, cost, tokens_input, tokens_output, agent, model,"
                " time_created, time_updated, time_idle, time_viewed)"
                " SELECT ?, project_id, workspace_id, parent_id,"
                " fork_session_id, directory, path, title, slug, version,"
                " cost, tokens_input, tokens_output, agent, model, ?, ?, ?, ?"
                " FROM session_v2 WHERE id=?",
                (new_id, now, now, now, now, meta.id))
            con.execute(
                "INSERT INTO session_message (id, session_id, type, seq,"
                " time_created, time_updated, data)"
                " SELECT 'msg_' || lower(hex(randomblob(16))), ?, type, seq,"
                " time_created, time_updated, data"
                " FROM session_message WHERE session_id=?",
                (new_id, meta.id))
            # carry the seq allocator so appends to the copy can't collide
            con.execute(
                "INSERT INTO event_sequence (aggregate_id, seq, owner_id)"
                " SELECT ?, seq, owner_id FROM event_sequence"
                " WHERE aggregate_id=?", (new_id, meta.id))
            con.execute("COMMIT")
            return new_id
        finally:
            con.close()
    except sqlite3.Error:
        return None


def remove_session(meta: SessionMeta) -> bool:
    """Backup + delete one session's rows."""
    try:
        _backup_db(meta.id)
        con = connect_rw()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM session_message WHERE session_id=?",
                        (meta.id,))
            con.execute("DELETE FROM event_sequence WHERE aggregate_id=?",
                        (meta.id,))
            con.execute("DELETE FROM session_v2 WHERE id=?", (meta.id,))
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        return True
    except (sqlite3.Error, OSError):
        return False
