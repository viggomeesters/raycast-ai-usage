"""Append-only point-in-time snapshots; snapshot totals are never additive."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .model import PROVIDERS

SCHEMA = 1


@dataclass(frozen=True)
class SnapshotResult:
    run_id: int
    status: str
    deltas: dict[str, dict[str, int | float]]


def snapshot_payload(report: dict) -> dict:
    keys = (
        "schema_version",
        "timestamp",
        "updated_at",
        "scope",
        "currency",
        "cost_basis",
        "pricing_date",
        "usage",
        "months",
        "coverage",
    )
    return {key: report[key] for key in keys}


def total(payload: dict, provider: str, field: str) -> int | float:
    value = payload["usage"][provider]["total"][field]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid snapshot total")
    return value


def has_errors(payload: dict) -> bool:
    return any(payload["coverage"][provider]["errors"] for provider in PROVIDERS)


def contract(payload: dict) -> tuple:
    return (
        payload["schema_version"],
        payload["scope"],
        payload["currency"],
        payload["cost_basis"],
    )


def coverage_shape(payload: dict) -> tuple:
    return tuple(
        (
            provider,
            payload["coverage"][provider]["files"],
            payload["coverage"][provider]["last_event"] is not None,
        )
        for provider in PROVIDERS
    )


def compare(previous: dict | None, current: dict) -> tuple[str, dict]:
    if previous is None:
        return "baseline", {}
    if contract(previous) != contract(current) or has_errors(previous) or has_errors(current):
        return "incomplete_or_contract_changed", {}
    if coverage_shape(previous) != coverage_shape(current):
        return "reset_or_coverage_change", {}
    if any(
        total(current, provider, "total") < total(previous, provider, "total")
        for provider in PROVIDERS
    ):
        return "reset_or_coverage_change", {}
    same_prices = previous["pricing_date"] == current["pricing_date"]
    if same_prices and any(
        total(current, provider, "estimated_usd") < total(previous, provider, "estimated_usd")
        for provider in PROVIDERS
    ):
        return "reset_or_coverage_change", {}
    for provider in PROVIDERS:
        grew = total(current, provider, "total") > total(previous, provider, "total")
        repriced = same_prices and (
            total(current, provider, "estimated_usd") != total(previous, provider, "estimated_usd")
        )
        previous_event = previous["coverage"][provider]["last_event"]
        current_event = current["coverage"][provider]["last_event"]
        if (grew or repriced) and (
            previous_event is None or current_event is None or current_event <= previous_event
        ):
            return "historical_backfill_or_recount", {}

    deltas = {}
    for provider in PROVIDERS:
        values: dict[str, int | float] = {
            "tokens": int(total(current, provider, "total") - total(previous, provider, "total"))
        }
        if same_prices:
            values["estimated_usd"] = round(
                float(
                    total(current, provider, "estimated_usd")
                    - total(previous, provider, "estimated_usd")
                ),
                12,
            )
        deltas[provider] = values
    return "comparable", deltas


def save_snapshot(report: dict, database: Path) -> SnapshotResult:
    payload = snapshot_payload(report)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    measurement = {
        key: value for key, value in payload.items() if key not in ("timestamp", "updated_at")
    }
    measurement_hash = hashlib.sha256(
        json.dumps(measurement, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source = {
        provider: {
            "available": payload["coverage"][provider]["last_event"] is not None,
            "errors": payload["coverage"][provider]["errors"],
        }
        for provider in PROVIDERS
    }
    source_hash = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(database.parent, 0o700)
    with sqlite3.connect(database, timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA):
            raise RuntimeError("unsupported snapshot schema")
        db.execute(f"PRAGMA user_version={SCHEMA}")
        db.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            "id INTEGER PRIMARY KEY, captured_at REAL NOT NULL, report_schema INTEGER NOT NULL, "
            "measurement_hash TEXT NOT NULL, source_hash TEXT NOT NULL, previous_id INTEGER, "
            "comparison TEXT NOT NULL, deltas_json TEXT NOT NULL, report_json TEXT NOT NULL, "
            "additive INTEGER NOT NULL DEFAULT 0 CHECK(additive = 0))"
        )
        previous_row = db.execute(
            "SELECT id, report_json FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = json.loads(previous_row[1]) if previous_row else None
        status, deltas = compare(previous, payload)
        cursor = db.execute(
            "INSERT INTO snapshots (captured_at, report_schema, measurement_hash, source_hash, "
            "previous_id, comparison, deltas_json, report_json, additive) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                payload["timestamp"],
                payload["schema_version"],
                measurement_hash,
                source_hash,
                previous_row[0] if previous_row else None,
                status,
                json.dumps(deltas, sort_keys=True),
                encoded,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("snapshot id unavailable")
        run_id = cursor.lastrowid
    os.chmod(database, 0o600)
    return SnapshotResult(run_id, status, deltas)
