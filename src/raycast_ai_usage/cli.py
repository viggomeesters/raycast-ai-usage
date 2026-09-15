"""Raycast-friendly text and machine-readable JSON."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .history import roots, scan
from .model import PROVIDERS, Quota, utc_iso
from .pricing import PRICE_DATE, aggregate, aggregate_months
from .quotas import fetch


def compact(value: int | float) -> str:
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= divisor:
            return f"{value / divisor:.1f}{suffix}"
    return str(int(value))


def money(value: float) -> str:
    return f"${value / 1000:.1f}k" if value >= 1000 else f"${value:.2f}"


def quota_cell(quota: Quota, minutes: int) -> str:
    matches = [q.remaining for q in quota.limits if q.minutes == minutes]
    if matches:
        return f"{min(matches):.0f}%"
    return "—" if quota.status in ("ok", "unsupported") else "?"


def token_cell(data: dict, available: bool, output_only: bool = False) -> str:
    if not available:
        return "?"
    return compact(data["output"] if output_only else data["total"])


def cost_cell(data: dict, available: bool, partial: bool = False, lower_bound: bool = False) -> str:
    if not available:
        return "?"
    value = money(data["estimated_usd"])
    if data["unpriced_tokens"]:
        return f"≥{value}*" if data["unpriced_tokens"] < data["total"] else "?*"
    if lower_bound:
        return f"≥{value}*"
    return f"{value}*" if partial else value


def sum_window(report: dict, window: str) -> dict:
    fields = ("output", "total", "estimated_usd", "unpriced_tokens")
    return {
        field: sum(report["usage"][provider][window][field] for provider in PROVIDERS)
        for field in fields
    }


DISPLAY_PROVIDERS = ("gemini", "claude", "codex")
DISPLAY_WINDOWS = (
    ("1d", "24h"),
    ("7d", "7d"),
    ("30d", "30d"),
    ("365d", "365d"),
    ("total", "Totaal"),
)


def usage_line(label: str, tokens: str, cost: str) -> str:
    return f"{label:<8} {tokens} tokens  ({cost})"


def format_report(report: dict, quotas: dict[str, Quota], output_only: bool = False) -> str:
    updated = datetime.fromtimestamp(report["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [
        "AI-GEBRUIK",
        f"Bijgewerkt: {updated}",
    ]
    any_missing = False
    any_partial = False
    any_errors = False
    for provider in DISPLAY_PROVIDERS:
        stats = report["coverage"][provider]
        available = stats["last_event"] is not None
        partial = bool(stats["errors"] or stats["warnings"])
        any_missing = any_missing or not available
        any_partial = any_partial or partial
        any_errors = any_errors or bool(stats["errors"])
        lines.extend(["", provider.upper()])
        for window, label in DISPLAY_WINDOWS:
            data = report["usage"][provider][window]
            lines.append(
                usage_line(
                    label,
                    token_cell(data, available, output_only),
                    cost_cell(data, available, partial),
                )
            )

    lines.extend(["", "TOTALEN"])
    for window, label in DISPLAY_WINDOWS:
        data = sum_window(report, window)
        total_tokens = (
            ("≥" if any_missing else "")
            + compact(data["output"] if output_only else data["total"])
            + ("*" if any_missing else "")
        )
        lines.append(
            usage_line(
                label,
                total_tokens,
                cost_cell(data, True, any_partial or any_missing, lower_bound=any_missing),
            )
        )
    lines.extend(["", "MAANDTOTALEN"])
    for month in report.get("months", []):
        partial = bool(month["partial"] or any_missing or any_errors)
        value = month["output"] if output_only else month["total"]
        tokens = ("≥" if partial else "") + compact(value) + ("*" if partial else "")
        lines.append(
            usage_line(
                month["month"],
                tokens,
                cost_cell(month, True, partial, lower_bound=partial),
            )
        )
    lines.extend(
        [
            "",
            "Kosten: geschatte standaard API-tokenwaarden in USD, geen facturen.",
            "— = niet van toepassing   ? = onbekend   * = gedeeltelijk",
            "",
            "LIMIETEN OVER",
        ]
    )
    for provider in DISPLAY_PROVIDERS:
        quota = quotas[provider]
        lines.append(
            f"{provider.title():<8} 5h {quota_cell(quota, 300)}   7d {quota_cell(quota, 10080)}"
        )
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Lokale AI-tokens, API-schatting en accountlimieten"
    )
    result.add_argument("--json", action="store_true", help="JSON zonder voortgangstekst")
    result.add_argument(
        "--offline", action="store_true", help="Alleen logs; geen netwerk of credentials"
    )
    result.add_argument(
        "--output-tokens",
        action="store_true",
        help="Toon outputtokens incl. reasoning; kosten blijven alle tokens",
    )
    result.add_argument("--cache-dir", type=Path, help="Map voor de lokale metadata-cache")
    result.add_argument("--data-home", type=Path, help="Alternatieve datamap; vereist --offline")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.data_home and not args.offline:
        parser().error("--data-home vereist --offline")
    started = time.monotonic()
    home = args.data_home or Path.home()
    cache = (
        args.cache_dir
        or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "raycast-ai-usage"
    )
    try:
        config_path = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "raycast-ai-usage/config.json"
        )
        config = (
            json.loads(config_path.read_bytes())
            if config_path.exists() and not args.offline
            else {}
        )
        if not isinstance(config, dict):
            raise ValueError("invalid configuration")
        claude_provider = config.get("claude_provider", "auto")
        if claude_provider not in ("auto", "bedrock", "vertex", "foundry", "api"):
            raise ValueError("invalid claude_provider")
        with ThreadPoolExecutor(max_workers=4) as pool:
            history = pool.submit(scan, roots(home), cache)
            pending_quotas = {
                p: pool.submit(fetch, p, home, args.offline, claude_provider) for p in PROVIDERS
            }
            events, coverage = history.result()
            quotas = {p: job.result() for p, job in pending_quotas.items()}
        now = time.time()
        report = {
            "schema_version": 1,
            "timestamp": now,
            "updated_at": utc_iso(now),
            "scope": "local_cli_logs_all_local_accounts",
            "currency": "USD",
            "cost_basis": "current_standard_api_token_list_prices_not_billed",
            "pricing_date": PRICE_DATE,
            "usage": aggregate(events, now),
            "months": aggregate_months(events, now),
            "coverage": coverage,
            "quotas": {p: asdict(q) for p, q in quotas.items()},
            "duration_seconds": time.monotonic() - started,
        }
        print(
            json.dumps(report, indent=2)
            if args.json
            else format_report(report, quotas, args.output_tokens)
        )
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        # Bound errors to their type; filesystem/credential contents are never echoed.
        error = f"Lokale telling mislukt ({type(exc).__name__}); controleer cache en leesrechten"
        if args.json:
            print(json.dumps({"error": error}))
        else:
            print(f"AI Usage niet bijgewerkt\n{error}\nVoer ai-usage opnieuw uit na herstel.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
