import json
import sqlite3

from raycast_ai_usage.snapshots import save_snapshot


def report(tokens: int, cost: float = 1.0, *, pricing_date: str = "2026-09-13") -> dict:
    usage = {}
    coverage = {}
    for provider in ("gemini", "codex", "claude"):
        total = tokens if provider == "codex" else 0
        usage[provider] = {
            "total": {
                "total": total,
                "estimated_usd": cost if provider == "codex" else 0.0,
            }
        }
        coverage[provider] = {
            "files": 1,
            "errors": 0,
            "warnings": [],
            "last_event": 1.0,
        }
    return {
        "schema_version": 1,
        "timestamp": 1000.0 + tokens,
        "updated_at": "2026-09-15T12:00:00+00:00",
        "scope": "local_cli_logs_all_local_accounts",
        "currency": "USD",
        "cost_basis": "current_standard_api_token_list_prices_not_billed",
        "pricing_date": pricing_date,
        "usage": usage,
        "months": [],
        "coverage": coverage,
        "quotas": {"codex": {"note": "must not be persisted"}},
    }


def test_snapshots_are_append_only_non_additive_and_use_safe_deltas(tmp_path):
    database = tmp_path / "snapshots.sqlite3"
    first_report = report(100)
    second_report = report(150, 1.5)
    third_report = report(120, 1.2)
    second_report["coverage"]["codex"]["last_event"] = 2.0
    third_report["coverage"]["codex"]["last_event"] = 3.0
    first = save_snapshot(first_report, database)
    second = save_snapshot(second_report, database)
    third = save_snapshot(third_report, database)

    assert (first.status, first.deltas) == ("baseline", {})
    assert second.status == "comparable"
    assert second.deltas["codex"] == {"tokens": 50, "estimated_usd": 0.5}
    assert (third.status, third.deltas) == ("reset_or_coverage_change", {})

    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT additive, comparison, report_json FROM snapshots ORDER BY id"
        ).fetchall()
    assert len(rows) == 3
    assert [row[0] for row in rows] == [0, 0, 0]
    assert [row[1] for row in rows] == [
        "baseline",
        "comparable",
        "reset_or_coverage_change",
    ]
    assert "quotas" not in json.loads(rows[0][2])
    assert database.stat().st_mode & 0o777 == 0o600


def test_identical_runs_are_both_saved_without_double_counting(tmp_path):
    database = tmp_path / "snapshots.sqlite3"
    save_snapshot(report(100), database)
    repeated = report(100)
    repeated["timestamp"] += 60
    repeated["updated_at"] = "2026-09-15T12:01:00+00:00"
    result = save_snapshot(repeated, database)
    assert result.status == "comparable"
    assert result.deltas["codex"]["tokens"] == 0
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT measurement_hash FROM snapshots ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][0] == rows[1][0]


def test_cost_delta_is_omitted_when_pricing_snapshot_changes(tmp_path):
    database = tmp_path / "snapshots.sqlite3"
    save_snapshot(report(100), database)
    current = report(150, 9.0, pricing_date="2026-10-01")
    current["coverage"]["codex"]["last_event"] = 2.0
    result = save_snapshot(current, database)
    assert result.status == "comparable"
    assert result.deltas["codex"] == {"tokens": 50}


def test_backfill_and_source_changes_are_never_reported_as_new_usage(tmp_path):
    database = tmp_path / "snapshots.sqlite3"
    first = report(100)
    first["coverage"]["codex"]["last_event"] = 10.0
    save_snapshot(first, database)

    backfill = report(1000)
    backfill["coverage"]["codex"]["last_event"] = 10.0
    result = save_snapshot(backfill, database)
    assert (result.status, result.deltas) == ("historical_backfill_or_recount", {})

    changed = report(1100)
    changed["coverage"]["codex"]["files"] = 2
    changed["coverage"]["codex"]["last_event"] = 20.0
    result = save_snapshot(changed, database)
    assert (result.status, result.deltas) == ("reset_or_coverage_change", {})
