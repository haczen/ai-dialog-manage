"""Persistent scan cache: path -> (mtime_ns, size, parsed meta fields).

A warm scan stat()s each file and reuses the parsed meta when nothing
changed, so startup and watcher refreshes skip re-reading hundreds of MB
of transcripts. Entries for vanished paths are pruned on save.

Why not parallel scans instead: claude is ~96% of the cost and the GIL
serialises the decode/parse, so thread pools measured *slower*; running
the other sources concurrently would save ~50ms at best. The cache makes
the recurring cost ~100x cheaper instead.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from . import config
from .models import SessionMeta

CACHE_VERSION = 1

_lock = threading.Lock()
_cache: dict[str, dict] | None = None
_dirty = False


def _cache_file() -> Path:
    return config.APP_DIR / "scan_cache.json"


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            raw = json.loads(_cache_file().read_text(encoding="utf-8"))
            _cache = raw.get("entries") if raw.get("version") == CACHE_VERSION \
                else {}
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, ValueError, AttributeError):
            _cache = {}
    return _cache


def cached_meta(path: str, produce):
    """Reuse the meta for `path` when (mtime_ns, size) match, else call
    produce() and remember its result. Returns whatever produce() would
    (including None). Transient flags (running/starred/…) are NOT cached —
    callers recompute or re-apply them every scan."""
    global _dirty
    try:
        st = Path(path).stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return produce()
    cache = _load()
    ent = cache.get(path)
    if ent and (ent.get("mtime_ns"), ent.get("size")) == stamp:
        return _from_dict(ent.get("meta") or {})
    m = produce()
    with _lock:
        if m is None:
            if path in cache:
                cache.pop(path, None)
                _dirty = True
        else:
            cache[path] = {"mtime_ns": stamp[0], "size": stamp[1],
                           "meta": _to_dict(m)}
            _dirty = True
    return m


def save() -> None:
    """Persist the cache (atomic); prune entries whose file vanished."""
    global _dirty
    if not _dirty or _cache is None:
        return
    with _lock:
        for p in [p for p in _cache if not Path(p).exists()]:
            _cache.pop(p, None)
        try:
            config.ensure_app_dirs()
            tmp = _cache_file().with_suffix(".json.admtmp")
            tmp.write_text(json.dumps(
                {"version": CACHE_VERSION, "entries": _cache},
                ensure_ascii=False), encoding="utf-8")
            tmp.replace(_cache_file())
        except OSError:
            pass
        _dirty = False


def _to_dict(m: SessionMeta) -> dict:
    return {
        "source": m.source, "id": m.id, "path": m.path, "project": m.project,
        "cwd": m.cwd, "title": m.title, "n_messages": m.n_messages,
        "n_user": m.n_user, "n_assistant": m.n_assistant, "size": m.size,
        "created": m.created.isoformat() if m.created else None,
        "updated": m.updated.isoformat() if m.updated else None,
        "model": m.model, "tokens": m.tokens, "archived": m.archived,
        "subagent": m.subagent, "preview": m.preview,
    }


def _from_dict(d: dict) -> SessionMeta:
    def _dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    return SessionMeta(
        source=d.get("source", "?"), id=d.get("id", ""), path=d.get("path", ""),
        project=d.get("project", ""), cwd=d.get("cwd", ""),
        title=d.get("title", ""), n_messages=d.get("n_messages", 0),
        n_user=d.get("n_user", 0), n_assistant=d.get("n_assistant", 0),
        size=d.get("size", 0), created=_dt(d.get("created")),
        updated=_dt(d.get("updated")), model=d.get("model", ""),
        tokens=d.get("tokens"), archived=bool(d.get("archived")),
        subagent=bool(d.get("subagent")), preview=d.get("preview", ""),
    )
