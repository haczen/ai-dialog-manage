"""Registry of every session source — the single dispatch point.

Stores are registered with their scan/load callables and UI metadata
(badge label, color, editability, resume command). Anything that used to
branch on `meta.source == "claude"/"codex"` should ask this module instead,
so adding a source touches one file.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable

from . import (
    claude_store,
    cline_store,
    codex_store,
    gemini_store,
    goose_store,
    kiro_store,
    opencode_store,
)
from .models import LoadedSession, SessionMeta


@dataclass
class ScanContext:
    """Cross-source hints handed to scan() (currently: the project-hash ->
    cwd map the gemini/qwen stores need, built from every other source)."""
    cwd_hints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSpec:
    key: str
    scan: Callable[..., list]    # scan(progress, ctx) -> list[SessionMeta]
    load: Callable[[SessionMeta], LoadedSession]
    editable: bool               # False => UI blocks edit/append/delete
    resume_cmd: str | None       # "{id}" placeholder; None => no CLI


SOURCES: dict[str, SourceSpec] = {
    "claude": SourceSpec(
        key="claude",
        scan=lambda p, c: claude_store.scan_claude(p),
        load=claude_store.load_claude, editable=True,
        resume_cmd="claude --resume {id}"),
    "codex": SourceSpec(
        key="codex",
        scan=lambda p, c: codex_store.scan_codex(p),
        load=codex_store.load_codex, editable=True,
        resume_cmd="codex resume {id}"),
    "opencode": SourceSpec(
        key="opencode", scan=opencode_store.scan,
        load=opencode_store.load, editable=True,
        resume_cmd="opencode --session {id}"),
    "gemini": SourceSpec(
        key="gemini",
        scan=lambda p, c: gemini_store.scan(p, c, tool="gemini"),
        load=gemini_store.load, editable=True,
        resume_cmd="gemini --resume {id}"),
    "qwen": SourceSpec(
        key="qwen",
        scan=lambda p, c: gemini_store.scan(p, c, tool="qwen"),
        load=gemini_store.load, editable=True,
        resume_cmd="qwen --resume {id}"),
    "goose": SourceSpec(
        key="goose", scan=goose_store.scan,
        load=goose_store.load, editable=True,
        resume_cmd="goose session --resume --name {id}"),
    "cline": SourceSpec(
        key="cline",
        scan=lambda p, c: cline_store.scan(p, c, tool="cline"),
        load=cline_store.load, editable=True,
        resume_cmd=None),
    "roo": SourceSpec(
        key="roo",
        scan=lambda p, c: cline_store.scan(p, c, tool="roo"),
        load=cline_store.load, editable=True,
        resume_cmd=None),
    "kiro": SourceSpec(
        key="kiro", scan=kiro_store.scan,
        load=kiro_store.load, editable=True,
        resume_cmd=None),
}

# sources whose sessions live under a one-way hash dir; they scan last so
# they can resolve their hash back to a cwd using everyone else's cwds
_HASH_DIR_SOURCES = ("gemini", "qwen")

_STORES = {
    "claude": claude_store, "codex": codex_store,
    "opencode": opencode_store, "gemini": gemini_store,
    "qwen": gemini_store, "goose": goose_store,
    "cline": cline_store, "roo": cline_store, "kiro": kiro_store,
}


def store_for(source: str):
    """The store module for a source (edit helpers like make_editor)."""
    return _STORES.get(source)


def editable(source: str) -> bool:
    spec = SOURCES.get(source)
    return bool(spec and spec.editable)


def resume_command(meta: SessionMeta) -> str | None:
    spec = SOURCES.get(meta.source)
    if not spec or not spec.resume_cmd:
        return None
    return spec.resume_cmd.format(id=meta.id)


def load(meta: SessionMeta) -> LoadedSession:
    spec = SOURCES.get(meta.source)
    if not spec:
        raise ValueError(f"unsupported source: {meta.source}")
    return spec.load(meta)


def _hash_hints(cwds) -> dict[str, str]:
    """sha256(path-variant) -> cwd, so hashed chat dirs can be named.
    The exact path form the CLI hashed is not documented, so register the
    common variants (raw / slashes / lowercase)."""
    out: dict[str, str] = {}
    for cwd in cwds:
        if not cwd:
            continue
        for c in {cwd, cwd.replace("\\", "/"), cwd.replace("/", "\\"),
                  cwd.replace("\\", "/").lower()}:
            out.setdefault(hashlib.sha256(c.encode("utf-8")).hexdigest(), cwd)
    return out


def _scan_one(spec: SourceSpec, progress) -> list[SessionMeta]:
    """Run one source's scan; a failing source degrades to an empty list."""
    try:
        return spec.scan(progress, ScanContext())
    except Exception as e:  # noqa: BLE001 — scanner never dies
        if progress:
            progress(f"{spec.key} scan error: {e}")
        return []


def scan_all(progress=None) -> list[SessionMeta]:
    """Scan every source (order: everything, then the hash-dir sources).

    File-backed stores run through the persistent meta cache, so a warm
    scan stat()s each file instead of re-reading hundreds of MB. Sources
    run sequentially: claude is ~96% of the cost, the GIL serialises the
    decode/parse (thread pools measured slower), and everything already
    runs in the background ScanThread so the UI never blocks."""
    from . import meta_cache
    metas: list[SessionMeta] = []
    cwds: set[str] = set()
    for key, spec in SOURCES.items():
        if key in _HASH_DIR_SOURCES:
            continue
        ms = _scan_one(spec, progress)
        metas.extend(ms)
        cwds.update(m.cwd for m in ms if m.cwd)
    hints = _hash_hints(cwds)
    ctx = ScanContext(cwd_hints=hints)
    for key in _HASH_DIR_SOURCES:
        metas.extend(_scan_one_ctx(SOURCES[key], ctx, progress))
    meta_cache.save()
    return metas


def _scan_one_ctx(spec: SourceSpec, ctx: ScanContext, progress):
    try:
        return spec.scan(progress, ctx)
    except Exception as e:  # noqa: BLE001
        if progress:
            progress(f"{spec.key} scan error: {e}")
        return []


def combo_items() -> list[tuple[str, str]]:
    """(source_key, badge_label) in display order, for the filter combo."""
    from .models import SOURCE_BADGES
    return [(k, SOURCE_BADGES.get(k, k.capitalize())) for k in SOURCES]
