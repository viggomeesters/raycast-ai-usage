"""Normalized usage: input, cache reads/writes and output are disjoint."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

PROVIDERS = ("gemini", "codex", "claude")
WINDOWS: dict[str, int | None] = {
    "1d": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "365d": 365 * 86400,
    "total": None,
}


def timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.timestamp()
    except (ValueError, OverflowError):
        return None


def integer(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def identity(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def clean(value: object, length: int = 70) -> str:
    return " ".join(str(value).split())[:length].replace("\x1b", "")


@dataclass
class Event:
    key: str
    provider: str
    ts: float
    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0  # A subset of output; never add again to total/cost.
    windows: tuple[str, ...] | list[str] | None = None
    monthly: bool = True
    aggregate_until: float | None = None

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write + self.cache_write_1h


@dataclass
class Limit:
    label: str
    remaining: float
    minutes: int | None = None
    resets_at: float | None = None


@dataclass
class Quota:
    provider: str
    status: str  # ok, unavailable, unsupported, auth_required, offline
    source: str
    note: str = ""
    limits: list[Limit] = field(default_factory=list)
    extras: list[Limit] = field(default_factory=list)


def utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def to_dict(value: Event | Quota) -> dict:
    return asdict(value)
