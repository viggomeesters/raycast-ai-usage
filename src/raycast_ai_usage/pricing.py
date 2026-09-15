"""Versioned standard API list-price equivalents, USD per million tokens.

This deliberately estimates token value, not a bill. Discounts, regional/fast
processing, subscriptions, tax, tools and cache storage are outside this measure.
See README for primary sources and the snapshot date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .model import PROVIDERS, WINDOWS, Event

PRICE_DATE = "2026-09-13"


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    read: float
    write: float = 0
    write_1h: float = 0
    long_threshold: int | None = None
    long_input_factor: float = 2
    long_output_factor: float = 1.5


PRICES = {
    "gpt-6-astra": Price(10, 50, 1, 12.5, long_threshold=272000),
    "gpt-5.6-sol": Price(4, 20, 0.4, 5, long_threshold=272000),
    "gpt-5.6-terra": Price(2, 12, 0.2, 2.5, long_threshold=272000),
    "gpt-5.6-luna": Price(0.2, 1.2, 0.02, 0.25, long_threshold=272000),
    "gpt-5.5": Price(5, 30, 0.5, long_threshold=272000),
    "gpt-5.4": Price(2.5, 15, 0.25, long_threshold=272000),
    "claude-opus-5": Price(5, 25, 0.5, 6.25, 10),
    "claude-sonnet-5": Price(2, 10, 0.2, 2.5, 4),
    "claude-haiku-4-5": Price(1, 5, 0.1, 1.25, 2),
    "claude-fable-5": Price(10, 50, 1, 12.5, 20),
    "claude-mythos-5": Price(10, 50, 1, 12.5, 20),
    "claude-fable-5-1": Price(10, 50, 0.25, 12.5, 20),
    "claude-mythos-5-1": Price(10, 50, 0.25, 12.5, 20),
    "gemini-3-flash-preview": Price(0.5, 3, 0.05),
    "gemini-3.1-flash-lite": Price(0.25, 1.5, 0.025),
    "gemini-3.5-flash-lite": Price(0.3, 2.5, 0.03),
}
for version in ("4-5", "4-6", "4-7", "4-8"):
    PRICES[f"claude-opus-{version}"] = Price(5, 25, 0.5, 6.25, 10)
for version in ("4-5", "4-6"):
    PRICES[f"claude-sonnet-{version}"] = Price(3, 15, 0.3, 3.75, 6)


def canonical_model(model: str) -> str:
    # Exact known names and documented snapshot suffixes; never guess a family price.
    model = re.sub(r"^(?:(?:eu|us|apac|global)\.)?anthropic\.", "", model)
    model = re.sub(r"-v\d+:\d+$", "", model)
    model = re.sub(r"-\d{4}-?\d{2}-?\d{2}$", "", model)
    return model


def cost(event: Event) -> float | None:
    price = PRICES.get(canonical_model(event.model))
    if price is None or (event.cache_write and not price.write):
        return None
    prompt = event.input + event.cache_read + event.cache_write + event.cache_write_1h
    long = price.long_threshold is not None and prompt > price.long_threshold
    input_factor = price.long_input_factor if long else 1
    output_factor = price.long_output_factor if long else 1
    return (
        input_factor
        * (
            event.input * price.input
            + event.cache_read * price.read
            + event.cache_write * price.write
            + event.cache_write_1h * price.write_1h
        )
        + output_factor * event.output * price.output
    ) / 1e6


def aggregate(events: list[Event], now: float) -> dict:
    result: dict[str, dict[str, dict]] = {
        p: {
            w: {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cache_write_1h": 0,
                "reasoning": 0,
                "total": 0,
                "events": 0,
                "estimated_usd": 0.0,
                "unpriced_tokens": 0,
                "unpriced_models": [],
            }
            for w in WINDOWS
        }
        for p in PROVIDERS
    }
    for event in events:
        estimate = cost(event)
        for window, seconds in WINDOWS.items():
            if event.windows is not None and window not in event.windows:
                continue
            if event.ts > now or (seconds is not None and event.ts < now - seconds):
                continue
            bucket = result[event.provider][window]
            for field in (
                "input",
                "output",
                "cache_read",
                "cache_write",
                "cache_write_1h",
                "reasoning",
                "total",
            ):
                bucket[field] += getattr(event, field)
            bucket["events"] += 1
            if estimate is None:
                bucket["unpriced_tokens"] += event.total
                if event.model not in bucket["unpriced_models"]:
                    bucket["unpriced_models"].append(event.model)
            else:
                bucket["estimated_usd"] += estimate
    return result


def aggregate_months(events: list[Event], now: float, count: int = 12) -> list[dict]:
    current = datetime.fromtimestamp(now, UTC)
    month_ids = []
    month_bounds = {}
    absolute_month = current.year * 12 + current.month - 1
    for offset in range(count):
        value = absolute_month - offset
        year, zero_based_month = divmod(value, 12)
        month = zero_based_month + 1
        key = f"{year:04d}-{month:02d}"
        next_value = value + 1
        next_year, next_zero_based_month = divmod(next_value, 12)
        start = datetime(year, month, 1, tzinfo=UTC).timestamp()
        end = datetime(next_year, next_zero_based_month + 1, 1, tzinfo=UTC).timestamp()
        month_ids.append(key)
        month_bounds[key] = (start, end)

    result: dict[str, dict] = {
        month: {
            "month": month,
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cache_write_1h": 0,
            "reasoning": 0,
            "total": 0,
            "events": 0,
            "estimated_usd": 0.0,
            "unpriced_tokens": 0,
            "unpriced_models": [],
            "partial": False,
        }
        for month in month_ids
    }
    for event in events:
        if not event.monthly:
            if event.aggregate_until is not None:
                for month, (start, end) in month_bounds.items():
                    if event.ts < end and event.aggregate_until > start:
                        result[month]["partial"] = True
            continue
        if event.ts > now:
            continue
        month = datetime.fromtimestamp(event.ts, UTC).strftime("%Y-%m")
        if month not in result:
            continue
        bucket = result[month]
        for field in (
            "input",
            "output",
            "cache_read",
            "cache_write",
            "cache_write_1h",
            "reasoning",
            "total",
        ):
            bucket[field] += getattr(event, field)
        bucket["events"] += 1
        estimate = cost(event)
        if estimate is None:
            bucket["unpriced_tokens"] += event.total
            if event.model not in bucket["unpriced_models"]:
                bucket["unpriced_models"].append(event.model)
        else:
            bucket["estimated_usd"] += estimate
    return [result[month] for month in month_ids]
