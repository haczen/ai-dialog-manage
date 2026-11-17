"""Global full-text search across all session sources.

Claude/Codex transcripts are raw jsonl — a substring match on the raw line
covers everything visible in the viewer. The newer sources store whole-JSON
documents or sqlite, so search dispatches per source:

  claude / codex : line-by-line jsonl scan (unchanged)
  gemini/qwen/goose/cline/roo : parse the JSON, walk message records
  opencode : sqlite instr() over session_message.data
"""
from __future__ import annotations

import json

from PyQt6.QtCore import QThread, pyqtSignal

from . import cline_store, gemini_store, goose_store, kiro_store, opencode_store
from .models import SessionMeta

MAX_HITS = 3000

_RECORD_STORES = {"gemini": gemini_store, "qwen": gemini_store,
                  "goose": goose_store, "cline": cline_store,
                  "roo": cline_store, "kiro": kiro_store}


class SearchThread(QThread):
    """Searches raw jsonl lines so it also finds tool args / file paths.

    Sessions store text as non-escaped UTF-8, so a plain substring match on
    the raw line covers everything visible in the viewer.
    """
    hit = pyqtSignal(dict)      # {meta, line, role, snippet, text}
    done = pyqtSignal(int)      # total hits
    status = pyqtSignal(str)

    def __init__(self, metas: list[SessionMeta], query: str, parent=None):
        super().__init__(parent)
        self.metas = metas
        self.query = query.strip()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        q = self.query.lower()
        n = 0
        oc_con = None
        for k, m in enumerate(self.metas):
            if self._cancel or n >= MAX_HITS or not q:
                break
            self.status.emit(f"{k + 1}/{len(self.metas)} {m.display_title[:40]}")
            try:
                if m.source == "opencode":
                    if oc_con is None:
                        oc_con = opencode_store.connect_ro()
                    n = self._search_opencode(oc_con, m, q, n)
                elif m.source in _RECORD_STORES:
                    n = self._search_records(m, q, n)
                else:
                    n = self._search_jsonl(m, q, n)
            except OSError:
                continue
            except Exception:  # noqa: BLE001 — one bad session never kills search
                continue
        if oc_con is not None:
            oc_con.close()
        self.done.emit(n)

    def _emit(self, m: SessionMeta, line: int, role: str, text: str,
              q: str, n: int) -> int:
        pos = text.lower().find(q)
        if pos == -1:
            pos = 0
        self.hit.emit({
            "meta": m, "line": line, "role": role,
            "text": text,
            "snippet": _snippet(text, pos),
        })
        return n + 1

    def _search_jsonl(self, m: SessionMeta, q: str, n: int) -> int:
        with open(m.path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if q in line.lower():
                    role, text = _extract(line)
                    if text is None:
                        continue
                    n = self._emit(m, i, role, text, q, n)
                    if n >= MAX_HITS:
                        break
        return n

    def _search_records(self, m: SessionMeta, q: str, n: int) -> int:
        store = _RECORD_STORES[m.source]
        try:
            with open(m.path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except ValueError:
            return n
        records = store.get_records(doc) if hasattr(store, "get_records") \
            else None
        if records is None:
            return n
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            role, text = store.record_text(rec)
            if text and q in text.lower():
                n = self._emit(m, i, role, text, q, n)
                if n >= MAX_HITS:
                    break
        return n

    def _search_opencode(self, con, m: SessionMeta, q: str, n: int) -> int:
        rows = con.execute(
            "SELECT seq, type, data FROM session_message"
            " WHERE session_id=? AND instr(lower(data), ?) > 0"
            " ORDER BY seq", (m.id, q)).fetchall()
        for seq, typ, data in rows:
            role, text = opencode_store.message_text(typ, data)
            if not text or q not in text.lower():
                continue
            n = self._emit(m, seq, role, text, q, n)
            if n >= MAX_HITS:
                break
        return n


def _extract(line: str) -> tuple[str, str | None]:
    try:
        o = json.loads(line)
    except ValueError:
        return "", None
    t = o.get("type")
    if t == "response_item":
        p = o.get("payload") or {}
        pt = p.get("type")
        if pt == "message":
            texts = [x.get("text", "") for x in (p.get("content") or [])
                     if isinstance(x, dict)]
            return p.get("role") or "user", "\n".join(texts)
        if pt in ("function_call", "function_call_output", "reasoning"):
            blob = p.get("arguments") or p.get("output") or ""
            if not isinstance(blob, str):
                blob = json.dumps(blob, ensure_ascii=False)
            return pt, blob
        return "", None
    if t in ("user", "assistant"):
        msg = o.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            return t, c
        if isinstance(c, list):
            texts = []
            for x in c:
                if isinstance(x, dict):
                    if x.get("type") == "text":
                        texts.append(x.get("text", ""))
                    elif x.get("type") == "tool_use":
                        texts.append(json.dumps(x.get("input"), ensure_ascii=False))
                    elif x.get("type") == "tool_result":
                        texts.append(_flat(x.get("content")))
            return t, "\n".join(texts)
    return "", None


def _flat(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def _snippet(text: str, pos: int, before: int = 60, after: int = 220) -> str:
    s = max(0, pos - before)
    frag = " ".join(text[s: pos + after].split())
    return ("…" if s > 0 else "") + frag
