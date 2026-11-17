"""Snapshot-based editing of pure-JSON session documents.

Used by the gemini / qwen / goose / cline stores, whose transcripts are
single JSON files rather than per-line jsonl. Mirrors the guarantees of
jsonlio.SessionEditor: timestamped backup -> verify the on-disk file still
matches the snapshot taken at load (external-change guard) -> keep records
the CLI appended since load -> atomic replace.

The document itself is opaque to this module: each store supplies
get_records(doc) / set_records(doc, records) accessors, so envelope fields
(sessionId, projectHash, working_dir, message_count, …) are preserved and
store-specific bookkeeping (e.g. bumping updated_at) can happen in
set_records.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .jsonlio import FileChangedError, make_backup


class RecordEditor:
    """Accumulates record edits against a snapshot; saves atomically."""

    def __init__(self, path: str | Path,
                 get_records: Callable[[Any], list],
                 set_records: Callable[[Any, list], None],
                 source: str, sid: str):
        self.path = Path(path)
        self._get_records = get_records
        self._set_records = set_records
        self.source = source
        self.sid = sid
        self._doc = self._load_doc()
        self._snap: list = list(get_records(self._doc) or [])
        self._replaced: dict[int, Any] = {}
        self._deleted: set[int] = set()
        self._appended: list[Any] = []
        self.dirty = False
        self._refresh_stat()

    # -- read ------------------------------------------------------------
    def _load_doc(self) -> Any:
        return json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def snapshot(self) -> list:
        """Records as of load time (before our edits)."""
        return self._snap

    # -- mutate ----------------------------------------------------------
    def replace_record(self, idx: int, obj: Any) -> None:
        self._replaced[idx] = obj
        self.dirty = True

    def delete_record(self, idx: int) -> None:
        self._deleted.add(idx)
        self.dirty = True

    def delete_records(self, idxs) -> None:
        for i in idxs:
            self.delete_record(i)

    def append_record(self, obj: Any) -> None:
        self._appended.append(obj)
        self.dirty = True

    # -- save ------------------------------------------------------------
    def _current_tail(self, cur_records: list) -> list:
        """Records the CLI appended since our snapshot (prefix guard)."""
        if len(cur_records) < len(self._snap):
            raise FileChangedError("session shrank on disk")
        if cur_records[: len(self._snap)] != self._snap:
            raise FileChangedError("existing records changed on disk")
        return cur_records[len(self._snap):]

    def save(self) -> int:
        """Apply edits and write. Returns number of records written."""
        if not self.dirty:
            return len(self._snap)
        ext = self.path.suffix or ".json"
        make_backup(self.path, self.source, self.sid, ext=ext)
        base_doc = self._doc
        if self._changed_on_disk():
            base_doc = self._load_doc()
            tail = self._current_tail(self._get_records(base_doc) or [])
        else:
            tail = []
        out: list = []
        for i, rec in enumerate(self._snap):
            if i in self._deleted:
                continue
            out.append(self._replaced.get(i, rec))
        out.extend(tail)
        out.extend(self._appended)
        self._set_records(base_doc, out)
        tmp = self.path.with_suffix(ext + ".admtmp")
        tmp.write_text(json.dumps(base_doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)
        self._doc = base_doc
        self._snap = out
        self._replaced.clear()
        self._deleted.clear()
        self._appended.clear()
        self.dirty = False
        self._refresh_stat()
        return len(out)

    # -- external-change guard -------------------------------------------
    _snap_size = 0
    _snap_mtime = 0

    def _refresh_stat(self) -> None:
        st = self.path.stat()
        self._snap_size = st.st_size
        self._snap_mtime = st.st_mtime_ns

    def _changed_on_disk(self) -> bool:
        try:
            st = self.path.stat()
        except OSError:
            return True
        return st.st_size != self._snap_size or st.st_mtime_ns != self._snap_mtime
