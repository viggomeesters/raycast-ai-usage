"""Cache normalized events, keyed by source fingerprint. Original files are read-only."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import TypedDict

from .model import PROVIDERS, Event
from .parsers import PARSERS, parse_claude_stats

SCHEMA = 3


class Coverage(TypedDict):
    files: int
    errors: int
    warnings: list[str]
    last_event: float | None


def roots(home: Path) -> dict[str, list[Path]]:
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    claude_env = os.environ.get("CLAUDE_CONFIG_DIR")
    claude = (
        [Path(claude_env) / "projects"]
        if claude_env
        else [home / ".claude/projects", home / ".config/claude/projects"]
    )
    if not claude_env:
        for name in ("local-agent-mode-sessions", "claude-code-sessions"):
            desktop = home / "Library/Application Support/Claude" / name
            if desktop.is_dir():
                claude.extend(desktop.glob("**/.claude/projects"))
    return {
        "codex": [codex / "sessions", codex / "archived_sessions"],
        "claude": claude,
        "gemini": [Path(os.environ.get("GEMINI_HOME", home / ".gemini")) / "tmp"],
    }


def scan(source_roots: dict[str, list[Path]], cache_dir: Path) -> tuple[list[Event], dict]:
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path = cache_dir / "history.sqlite3"
    with sqlite3.connect(db_path, timeout=30) as db:
        os.chmod(db_path, 0o600)
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA:
            db.execute("DROP TABLE IF EXISTS files")
            db.execute(f"PRAGMA user_version={SCHEMA}")
        db.execute(
            "CREATE TABLE IF NOT EXISTS files "
            "(path TEXT PRIMARY KEY, stamp TEXT, events TEXT, warnings TEXT)"
        )
        stats: dict[str, Coverage] = {}
        unique: dict[str, Event] = {}
        seen_paths: set[str] = set()
        for provider in PROVIDERS:
            stats[provider] = {"files": 0, "errors": 0, "warnings": [], "last_event": None}
            for root in source_roots[provider]:
                if not root.exists():
                    continue
                pattern = "**/chats/session-*.json" if provider == "gemini" else "**/*.jsonl"
                root_events: list[Event] = []
                try:
                    files = list(root.glob(pattern))
                except OSError:
                    stats[provider]["errors"] += 1
                    continue
                for path in files:
                    key = str(path.resolve())
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)
                    stats[provider]["files"] += 1
                    warnings: list[str] = []
                    try:
                        stat = path.stat()
                        stamp = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
                        cached = db.execute(
                            "SELECT stamp, events, warnings FROM files WHERE path=?", (key,)
                        ).fetchone()
                        if cached and cached[0] == stamp:
                            events = [Event(**e) for e in json.loads(cached[1])]
                            warnings = json.loads(cached[2])
                        else:
                            events = PARSERS[provider](path, warnings)
                            warnings = sorted(set(warnings))
                            db.execute(
                                "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                                (
                                    key,
                                    stamp,
                                    json.dumps([asdict(e) for e in events]),
                                    json.dumps(warnings),
                                ),
                            )
                        root_events.extend(events)
                        stats[provider]["warnings"].extend(warnings)
                    except (OSError, ValueError, TypeError, AttributeError):
                        stats[provider]["errors"] += 1
                if provider == "claude":
                    stats_path = root.parent / "stats-cache.json"
                    if stats_path.is_file():
                        warnings = []
                        try:
                            historical, cutoff = parse_claude_stats(stats_path, warnings)
                            if historical:
                                covers_365d = historical[0].ts >= time.time() - 365 * 86400
                                historical_windows = (
                                    ("365d", "total") if covers_365d else ("total",)
                                )
                                transcript_windows = (
                                    ("1d", "7d", "30d")
                                    if covers_365d
                                    else ("1d", "7d", "30d", "365d")
                                )
                                for event in historical:
                                    event.windows = historical_windows
                                for event in root_events:
                                    if event.ts < cutoff:
                                        event.windows = transcript_windows
                                root_events.extend(historical)
                            stats[provider]["warnings"].extend(warnings)
                        except (OSError, ValueError, TypeError, AttributeError):
                            stats[provider]["errors"] += 1
                for event in root_events:
                    prior = unique.get(event.key)
                    if prior is None or event.total > prior.total:
                        unique[event.key] = event
                    previous = stats[provider]["last_event"]
                    stats[provider]["last_event"] = max(event.ts, previous or 0)
            stats[provider]["warnings"] = sorted(set(stats[provider]["warnings"]))
        # Removed/archived source files must not survive as phantom usage.
        for (path,) in db.execute("SELECT path FROM files").fetchall():
            if path not in seen_paths:
                db.execute("DELETE FROM files WHERE path=?", (path,))
        return list(unique.values()), stats
