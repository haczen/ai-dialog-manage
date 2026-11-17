"""Filesystem locations for all session sources + app config."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HOME = Path.home()

CLAUDE_DIR = HOME / ".claude"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
CLAUDE_SESSIONS = CLAUDE_DIR / "sessions"  # <pid>.json live-session markers

CODEX_DIR = HOME / ".codex"
CODEX_SESSIONS = CODEX_DIR / "sessions"  # YYYY/MM/DD/rollout-*.jsonl
CODEX_STATE_DB = CODEX_DIR / "state_5.sqlite"
CODEX_THREAD_HISTORY_DB = CODEX_DIR / "thread_history_1.sqlite"
CODEX_LOCKS = CODEX_DIR / "thread-writer-locks"

XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share")

# OpenCode 2.x — sqlite owned by the running service; read-only for us.
OPENCODE_DATA_DIR = XDG_DATA_HOME / "opencode"
OPENCODE_DB = OPENCODE_DATA_DIR / "opencode.db"

# Gemini CLI / Qwen Code — chat recordings live under
# <dir>/tmp/<sha256(cwd)>/chats/session-*.json
GEMINI_DIR = Path(os.environ.get("GEMINI_CLI_HOME") or HOME / ".gemini")
QWEN_DIR = Path(os.environ.get("QWEN_HOME") or HOME / ".qwen")


def goose_sessions_dir() -> Path:
    env = os.environ.get("GOOSE_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA")
                       or HOME / "AppData" / "Roaming")
        cands = [appdata / "Block" / "goose" / "data" / "sessions",
                 appdata / "goose" / "data" / "sessions",
                 appdata / "Block" / "goose" / "sessions",
                 appdata / "goose" / "sessions"]
        for c in cands:
            if c.exists() or c.parent.exists():
                return c
        return cands[0]
    return XDG_DATA_HOME / "goose" / "sessions"


def _vscode_global_storage() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = HOME / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
    stable = base / "Code" / "User" / "globalStorage"
    insiders = base / "Code - Insiders" / "User" / "globalStorage"
    return insiders if not stable.exists() and insiders.exists() else stable


VSCODE_GLOBAL_STORAGE = _vscode_global_storage()
CLINE_TASKS = VSCODE_GLOBAL_STORAGE / "saoudrizwan.claude-dev" / "tasks"
ROO_TASKS = VSCODE_GLOBAL_STORAGE / "rooveterinaryinc.roo-cline" / "tasks"

APP_DIR = Path(os.environ.get("APPDATA", str(HOME))) / "ai-dialog-manager"
BACKUP_DIR = APP_DIR / "backups"
META_FILE = APP_DIR / "meta.json"

BACKUPS_KEEP = 8  # per session


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
