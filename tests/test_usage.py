import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from raycast_ai_usage import cli, history, quotas
from raycast_ai_usage.history import roots, scan
from raycast_ai_usage.model import PROVIDERS, WINDOWS, Event, Limit, Quota, timestamp
from raycast_ai_usage.parsers import parse_claude, parse_claude_stats, parse_codex, parse_gemini
from raycast_ai_usage.pricing import aggregate, aggregate_months, canonical_model, cost

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC).timestamp()


def write_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def codex_record(total, last, ts="2026-09-13T10:00:00Z"):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": total, "last_token_usage": last},
        },
    }


def usage(input=100, cached=60, output=20):
    return {
        "input_tokens": input,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": 10,
        "total_tokens": input + output,
    }


def test_codex_dedup_cache_reasoning_and_model_switch(tmp_path):
    first = usage()
    second = usage(input=300, cached=160, output=50)
    row = codex_record(first, first)
    path = write_lines(
        tmp_path / "log.jsonl",
        [
            {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
            row,
            row,
            {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            codex_record(second, usage(200, 100, 30), "2026-09-13T11:00:00Z"),
        ],
    )
    warnings = []
    events = parse_codex(path, warnings)
    assert warnings == []
    assert len(events) == 2
    assert [e.model for e in events] == ["gpt-6-astra", "gpt-5.6-sol"]
    assert sum(e.total for e in events) == 350
    assert events[0].input == 40
    assert events[0].output == 20  # Reasoning already included.


def test_codex_cumulative_fallback_and_counter_restart(tmp_path):
    path = write_lines(
        tmp_path / "log.jsonl",
        [
            codex_record(usage(), None),
            codex_record(usage(200, 100, 40), None, "2026-09-13T11:00:00Z"),
            codex_record(usage(50, 10, 15), usage(50, 10, 15), "2026-09-13T11:30:00Z"),
        ],
    )
    events = parse_codex(path, [])
    assert [e.total for e in events] == [120, 120, 65]


def test_codex_invalid_record_is_partial_and_survives_truncated_line(tmp_path):
    path = write_lines(tmp_path / "log.jsonl", [codex_record(usage(10, 20), usage(10, 20))])
    with path.open("a") as stream:
        stream.write('{"type":"event_msg",broken')
    warnings = []
    assert parse_codex(path, warnings) == []
    assert len(warnings) == 2


def claude_row(output, ts="2026-09-13T10:00:00Z"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": "request-1",
        "message": {
            "id": "message-1",
            "model": "claude-opus-5",
            "content": "PRIVATE PROMPT CANARY",
            "usage": {
                "input_tokens": 100,
                "output_tokens": output,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 80,
                "cache_creation": {"ephemeral_1h_input_tokens": 30},
                "output_tokens_details": {"thinking_tokens": 5},
            },
        },
    }


def test_claude_stream_updates_and_cache_ttl(tmp_path):
    path = write_lines(tmp_path / "log.jsonl", [claude_row(10), claude_row(30), claude_row(15)])
    events = parse_claude(path, [])
    assert len(events) == 1
    event = events[0]
    assert (event.input, event.cache_read, event.cache_write, event.cache_write_1h) == (
        100,
        200,
        50,
        30,
    )
    assert event.output == 30
    assert event.total == 410
    assert cost(event) == pytest.approx((100 * 5 + 200 * 0.5 + 50 * 6.25 + 30 * 10 + 30 * 25) / 1e6)


def test_claude_separate_request_counts(tmp_path):
    a, b = claude_row(10), claude_row(10)
    b["requestId"] = "request-2"
    assert len(parse_claude(write_lines(tmp_path / "log.jsonl", [a, b]), [])) == 2


def test_claude_stats_cache_preserves_deleted_history(tmp_path):
    path = tmp_path / "stats-cache.json"
    path.write_text(
        json.dumps(
            {
                "firstSessionDate": "2026-01-24T20:26:41Z",
                "lastComputedDate": "2026-08-24",
                "modelUsage": {
                    "claude-opus-4-6": {
                        "inputTokens": 10,
                        "outputTokens": 20,
                        "cacheReadInputTokens": 300,
                        "cacheCreationInputTokens": 40,
                    }
                },
            }
        )
    )
    events, cutoff = parse_claude_stats(path, [])
    assert cutoff == datetime(2026, 8, 25, tzinfo=UTC).timestamp()
    assert len(events) == 1
    assert events[0].total == 370
    assert events[0].model == "claude-opus-4-6"


def test_claude_stats_cache_replaces_overlapping_transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(history.time, "time", lambda: NOW)
    projects = tmp_path / "claude/projects"
    old = claude_row(10, "2026-08-20T10:00:00Z")
    recent = claude_row(30, "2026-08-26T10:00:00Z")
    recent["requestId"] = "request-2"
    recent["message"]["id"] = "message-2"
    write_lines(projects / "session.jsonl", [old, recent])
    (projects.parent / "stats-cache.json").write_text(
        json.dumps(
            {
                "firstSessionDate": "2026-01-24T20:26:41Z",
                "lastComputedDate": "2026-08-24",
                "modelUsage": {
                    "claude-opus-4-6": {
                        "inputTokens": 1000,
                        "outputTokens": 2000,
                        "cacheReadInputTokens": 3000,
                        "cacheCreationInputTokens": 4000,
                    }
                },
            }
        )
    )
    data = {p: [tmp_path / p] for p in PROVIDERS}
    data["claude"] = [projects]
    events, coverage = scan(data, tmp_path / "cache")
    claude = [event for event in events if event.provider == "claude"]
    assert len(claude) == 3
    usage = aggregate(claude, NOW)["claude"]
    assert usage["30d"]["total"] == 800
    assert usage["365d"]["total"] == 10410
    assert usage["total"]["total"] == 10410
    assert any("historisch aggregaat" in warning for warning in coverage["claude"]["warnings"])


def test_gemini_disjoint_usage(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "sessionId": "s",
                "messages": [
                    {
                        "type": "gemini",
                        "id": "m",
                        "timestamp": "2026-09-13T10:00:00Z",
                        "model": "gemini-3-flash-preview",
                        "tokens": {
                            "input": 100,
                            "cached": 40,
                            "output": 20,
                            "thoughts": 30,
                            "total": 150,
                        },
                    }
                ],
            }
        )
    )
    event = parse_gemini(path, [])[0]
    assert event.total == 150
    assert event.input == 60
    assert event.output == 50
    assert cost(event) == pytest.approx((60 * 0.5 + 40 * 0.05 + 50 * 3) / 1e6)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("eu.anthropic.claude-opus-5-v1:0", "claude-opus-5"),
        ("gpt-6-astra-2026-09-01", "gpt-6-astra"),
        ("gpt-6-astra-unknown", "gpt-6-astra-unknown"),
    ],
)
def test_model_normalization(model, expected):
    assert canonical_model(model) == expected


def test_long_context_applies_to_all_prompt_categories():
    event = Event(
        "e",
        "codex",
        NOW,
        "gpt-6-astra",
        input=200_000,
        output=1000,
        cache_read=50_000,
        cache_write=22_001,
    )
    assert cost(event) == pytest.approx(
        (2 * (200_000 * 10 + 50_000 + 22_001 * 12.5) + 1000 * 75) / 1e6
    )


def test_rolling_windows_boundaries_and_unknown_prices():
    sample = Event("e", "codex", NOW, "gpt-6-astra", input=100, output=20)
    events = [
        replace(sample, ts=NOW - age)
        for age in (0, 86400, 86401, 7 * 86400, 7 * 86400 + 1, 30 * 86400, 30 * 86400 + 1, -1)
    ]
    report = aggregate(events, NOW)["codex"]
    assert [report[w]["events"] for w in ("1d", "7d", "30d", "365d", "total")] == [
        2,
        4,
        6,
        7,
        7,
    ]
    report = aggregate([sample, replace(sample, model="future-model")], NOW)["codex"]["1d"]
    assert report["total"] == 240
    assert report["unpriced_tokens"] == 120
    assert report["unpriced_models"] == ["future-model"]


def test_calendar_month_totals_and_unallocated_history():
    now = datetime(2026, 3, 15, 12, tzinfo=UTC).timestamp()
    events = [
        Event(
            "known",
            "codex",
            datetime(2026, 2, 10, tzinfo=UTC).timestamp(),
            "gpt-6-astra",
            input=200,
        ),
        Event(
            "historical",
            "claude",
            datetime(2026, 1, 10, tzinfo=UTC).timestamp(),
            "claude-opus-5",
            input=1000,
            monthly=False,
            aggregate_until=datetime(2026, 3, 1, tzinfo=UTC).timestamp(),
        ),
    ]
    months = aggregate_months(events, now, count=3)
    assert [month["month"] for month in months] == ["2026-03", "2026-02", "2026-01"]
    assert months[0]["partial"] is False
    assert months[1]["total"] == 200
    assert months[1]["partial"] is True
    assert months[2]["total"] == 0
    assert months[2]["partial"] is True


def test_history_cache_dedup_updates_deletions_and_no_content(tmp_path):
    data = {p: [tmp_path / p] for p in PROVIDERS}
    row = claude_row(10)
    a = write_lines(data["claude"][0] / "a.jsonl", [row])
    b = write_lines(data["claude"][0] / "nested/b.jsonl", [row])
    cache = tmp_path / "cache"
    first, coverage = scan(data, cache)
    second, _ = scan(data, cache)
    assert len(first) == len(second) == 1
    assert first[0].total == second[0].total == 390
    assert coverage["gemini"]["last_event"] is None
    write_lines(a, [row, claude_row(30)])
    changed, _ = scan(data, cache)
    assert changed[0].total == 410
    a.unlink()
    assert scan(data, cache)[0][0].total == 390
    b.unlink()
    assert scan(data, cache)[0] == []
    with sqlite3.connect(cache / "history.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert b"PRIVATE PROMPT CANARY" not in (cache / "history.sqlite3").read_bytes()
    assert (cache / "history.sqlite3").stat().st_mode & 0o777 == 0o600


def test_codex_replayed_fork_is_deduped_across_files(tmp_path):
    data = {p: [tmp_path / p] for p in PROVIDERS}
    row = codex_record(usage(), usage())
    write_lines(data["codex"][0] / "a.jsonl", [row])
    write_lines(data["codex"][0] / "b.jsonl", [row])
    assert len(scan(data, tmp_path / "cache")[0]) == 1


def test_quota_primary_can_be_weekly_and_spark_is_separate():
    data = {
        "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 67, "windowDurationMins": 10080}},
            "spark": {"primary": {"usedPercent": 0, "windowDurationMins": 300}},
        }
    }
    quota = quotas.parse_codex_quota(data)
    assert cli.quota_cell(quota, 10080) == "33%"
    assert cli.quota_cell(quota, 300) == "—"
    assert len(quota.extras) == 1
    assert cli.quota_cell(Quota("codex", "unavailable", "x"), 300) == "?"


def test_legacy_limits_and_invalid_percentage():
    quota = quotas.parse_codex_quota(
        {"rateLimits": {"primary": {"usedPercent": 100, "windowDurationMins": 300}}}
    )
    assert cli.quota_cell(quota, 300) == "0%"
    for value in (None, "5", float("nan"), float("inf"), -1, 101, True):
        assert quotas.percent(value) is None


def test_claude_windows_missing_values_and_scopes():
    quota = quotas.parse_claude_quota(
        {
            "five_hour": {"utilization": 20},
            "seven_day": {"utilization": None},
            "seven_day_sonnet": {"utilization": 5},
        }
    )
    assert cli.quota_cell(quota, 300) == "80%"
    assert cli.quota_cell(quota, 10080) == "—"
    assert quota.extras[0].remaining == 95


def test_gemini_does_not_invent_window_and_takes_minimum():
    quota = quotas.parse_gemini_quota(
        {
            "buckets": [
                {"modelId": "gemini-pro", "remainingFraction": 0.8},
                {"modelId": "gemini-pro", "remainingFraction": 0.2},
                {"modelId": "gemini-flash", "remainingFraction": 0.9},
            ]
        }
    )
    assert cli.quota_cell(quota, 300) == "—"
    assert [e.remaining for e in quota.extras] == [20, 90]


def test_offline_never_probes_or_reads_credentials(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("offline must never call a provider")

    monkeypatch.setattr(quotas, "codex_quota", fail)
    monkeypatch.setattr(quotas, "claude_quota", fail)
    monkeypatch.setattr(quotas, "gemini_quota", fail)
    for p in PROVIDERS:
        assert quotas.fetch(p, tmp_path, offline=True).status == "offline"


def test_probe_failure_is_bounded_no_secret(tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("private-token-canary")

    monkeypatch.setattr(quotas, "codex_quota", fail)
    quota = quotas.fetch("codex", tmp_path)
    assert quota.status == "unavailable"
    assert "private-token" not in quota.note
    with pytest.raises(quotas.ProbeError):
        quotas.request_json("https://example.test/steal", "canary")
    with pytest.raises(quotas.ProbeError):
        quotas.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test")


def test_rpc_handshake_split_response_and_termination(tmp_path):
    program = tmp_path / "fake-codex"
    program.write_text(
        f"#!{sys.executable}\n"
        + """import sys,json,time
first=json.loads(sys.stdin.readline())
assert first['method']=='initialize'
print(json.dumps({'id':1,'result':{}}),flush=True)
assert json.loads(sys.stdin.readline())['method']=='initialized'
assert json.loads(sys.stdin.readline())['method']=='account/rateLimits/read'
sys.stdout.write('{"id":2,"result":');sys.stdout.flush()
time.sleep(.02)
print('{"rateLimits":{"primary":{"usedPercent":30,"windowDurationMins":300}}}}',flush=True)
time.sleep(20)
"""
    )
    program.chmod(0o700)
    result = quotas.codex_rpc(str(program), timeout=2)
    assert quotas.parse_codex_quota(result).limits[0].remaining == 70


def test_rpc_timeout(tmp_path):
    program = tmp_path / "fake-codex"
    program.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    program.chmod(0o700)
    with pytest.raises(quotas.ProbeError):
        quotas.codex_rpc(str(program), timeout=0.05)


def report_fixture():
    events = [Event("e", "codex", NOW, "gpt-6-astra", input=100)]
    coverage = {p: {"files": 1, "errors": 0, "warnings": [], "last_event": NOW} for p in PROVIDERS}
    return {
        "timestamp": NOW,
        "coverage": coverage,
        "usage": aggregate(events, NOW),
        "months": aggregate_months(events, NOW),
        "duration_seconds": 1.2,
    }


def test_vertical_report_is_copyable_and_complete():
    report = report_fixture()
    q = {
        p: Quota(p, "ok", "source", limits=[Limit("a", 70, 300), Limit("b", 30, 10080)])
        for p in PROVIDERS
    }
    text = cli.format_report(report, q)
    lines = text.splitlines()
    assert lines[0] == "AI-GEBRUIK"
    assert "|" not in text
    assert text.index("GEMINI") < text.index("CLAUDE") < text.index("CODEX")
    assert text.index("CODEX") < text.index("TOTALEN") < text.index("MAANDTOTALEN")
    assert text.index("MAANDTOTALEN") < text.index("LIMIETEN OVER")
    assert "2026-09  100 tokens  ($0.00)" in text
    for name in ("GEMINI", "CLAUDE", "CODEX", "TOTALEN"):
        block = text[text.index(name) :]
        assert "24h" in block
        assert "7d" in block
        assert "30d" in block
        assert "365d" in block
        assert "Totaal" in block
    assert [line for line in lines if line][-4:] == [
        "LIMIETEN OVER",
        "Gemini   5h 70%   7d 30%",
        "Claude   5h 70%   7d 30%",
        "Codex    5h 70%   7d 30%",
    ]
    assert not any("ophalen" in line.lower() for line in lines)


def test_empty_and_partial_are_visible():
    report = report_fixture()
    report["coverage"]["gemini"]["last_event"] = None
    report["usage"]["codex"]["1d"]["unpriced_tokens"] = 50
    q = {p: Quota(p, "unavailable", "source") for p in PROVIDERS}
    text = cli.format_report(report, q)
    gemini = text[text.index("GEMINI") : text.index("CLAUDE")]
    assert "24h      ? tokens  (?)" in gemini
    assert "≥$" in text
    assert "gedeeltelijk" in text


def test_total_rows_sum_all_providers():
    report = report_fixture()
    for index, provider in enumerate(PROVIDERS, start=1):
        for window in WINDOWS:
            report["usage"][provider][window]["total"] = index * 1000
            report["usage"][provider][window]["output"] = index * 100
            report["usage"][provider][window]["estimated_usd"] = index * 1.25
    q = {p: Quota(p, "ok", "source") for p in PROVIDERS}
    text = cli.format_report(report, q)
    totals = text[text.index("TOTALEN") : text.index("LIMIETEN OVER")]
    assert "24h      6.0k tokens  ($7.50)" in totals
    assert "Totaal   6.0k tokens  ($7.50)" in totals


def test_total_rows_mark_missing_provider_as_partial():
    report = report_fixture()
    report["coverage"]["gemini"]["last_event"] = None
    q = {p: Quota(p, "ok", "source") for p in PROVIDERS}
    text = cli.format_report(report, q)
    totals = text[text.index("TOTALEN") : text.index("LIMIETEN OVER")]
    assert "24h      ≥100* tokens  (≥$0.00*)" in totals


def test_cli_json_empty_and_cache_failure(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_HOME")
    }
    args = [
        sys.executable,
        "-m",
        "raycast_ai_usage",
        "--offline",
        "--json",
        "--data-home",
        str(tmp_path / "home"),
        "--cache-dir",
        str(tmp_path / "cache"),
    ]
    result = subprocess.run(args, capture_output=True, text=True, env=env, check=True)
    report = json.loads(result.stdout)
    assert report["coverage"]["codex"]["last_event"] is None
    assert report["quotas"]["claude"]["status"] == "offline"
    blocker = tmp_path / "file"
    blocker.write_text("file")
    args[-1] = str(blocker)
    result = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 1
    assert "error" in json.loads(result.stdout)
    assert not result.stderr


def test_custom_roots_do_not_split_literal_claude_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile,one"))
    assert roots(tmp_path)["claude"] == [tmp_path / "profile,one/projects"]


def test_timestamp_requires_timezone():
    assert timestamp("2026-09-13T12:00:00+02:00") == NOW - 7200
    assert timestamp("2026-09-13T12:00:00") is None
    assert timestamp(True) is None


def test_bedrock_config_works_without_shell_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.setattr(quotas, "executable", lambda _: pytest.fail("No CLI needed"))
    quota = quotas.fetch("claude", tmp_path, claude_provider="bedrock")
    assert quota.status == "unsupported"
    assert quota.note.startswith("bedrock:")
    assert cli.quota_cell(quota, 300) == "—"


def test_codex_reset_without_last_usage(tmp_path):
    path = write_lines(
        tmp_path / "log.jsonl",
        [
            codex_record(usage(500, 200, 100), None),
            codex_record(usage(100, 50, 20), None, "2026-09-13T11:00:00Z"),
        ],
    )
    assert [e.total for e in parse_codex(path, [])] == [600, 120]


def test_cli_invalid_configuration_is_sanitized(tmp_path, monkeypatch, capsys):
    config = tmp_path / "raycast-ai-usage/config.json"
    config.parent.mkdir()
    config.write_text('["secret-canary"]')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["ai-usage", "--json"])
    assert cli.main() == 1
    result = capsys.readouterr()
    assert "error" in json.loads(result.out)
    assert "secret-canary" not in result.out
    assert result.err == ""
