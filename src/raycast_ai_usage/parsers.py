"""Parse usage metadata only; never retain conversation content."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .model import Event, identity, integer, timestamp


def records(path: Path, provider: str, warnings: list[str]) -> Iterator[dict]:
    with path.open("rb") as stream:
        for raw in stream:
            # Avoid decoding multi-megabyte tool results that cannot contain usage.
            if provider == "codex" and not any(
                k in raw[:150] for k in (b'"event_msg"', b'"turn_context"', b'"session_meta"')
            ):
                continue
            if provider == "claude" and b'"usage"' not in raw:
                continue
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                warnings.append("onvolledige of ongeldige logregel overgeslagen")
                continue
            if isinstance(row, dict):
                yield row


def parse_codex(path: Path, warnings: list[str]) -> list[Event]:
    result = []
    model = "unknown"
    previous: dict = {}
    for row in records(path, "codex", warnings):
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "turn_context":
            model = str(payload.get("model") or "unknown")
            continue
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        if not isinstance(info, dict) or not info:
            continue
        cumulative = info.get("total_token_usage")
        last = info.get("last_token_usage")
        if not isinstance(cumulative, dict):
            warnings.append("Codex-gebruik zonder cumulatieve teller overgeslagen")
            continue
        if cumulative == previous:
            continue  # Repeated token_count notifications are not new requests.
        if isinstance(last, dict):
            usage = last
        elif integer(cumulative.get("total_tokens")) < integer(previous.get("total_tokens")):
            usage = cumulative
        else:
            usage = {
                k: max(0, integer(v) - integer(previous.get(k))) for k, v in cumulative.items()
            }
        previous = cumulative
        ts = timestamp(row.get("timestamp"))
        if ts is None:
            warnings.append("Codex-gebruik zonder geldige tijd overgeslagen")
            continue
        total_input = integer(usage.get("input_tokens"))
        read = integer(usage.get("cached_input_tokens"))
        write = integer(usage.get("cache_write_input_tokens"))
        if read + write > total_input:
            warnings.append("Codex-cachetellers groter dan input; record overgeslagen")
            continue
        event = Event(
            # Forked/copied histories retain the original timestamp and counters.
            key=identity("codex", row["timestamp"], cumulative),
            provider="codex",
            ts=ts,
            model=model,
            input=total_input - read - write,
            cache_read=read,
            cache_write=write,
            output=integer(usage.get("output_tokens")),
            reasoning=integer(usage.get("reasoning_output_tokens")),
        )
        if event.total:
            result.append(event)
    return result


def parse_claude(path: Path, warnings: list[str]) -> list[Event]:
    events: dict[str, Event] = {}
    for row in records(path, "claude", warnings):
        if row.get("type") != "assistant":
            continue
        message = row.get("message") or {}
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        ts = timestamp(row.get("timestamp"))
        if not isinstance(usage, dict) or ts is None:
            continue
        creation = usage.get("cache_creation") or {}
        if not isinstance(creation, dict):
            creation = {}
        write_total = integer(usage.get("cache_creation_input_tokens"))
        hour = min(write_total, integer(creation.get("ephemeral_1h_input_tokens")))
        key = identity(
            "claude",
            message.get("id") or row.get("uuid") or row.get("timestamp"),
            row.get("requestId"),
        )
        event = Event(
            key=key,
            provider="claude",
            ts=ts,
            model=str(message.get("model") or "unknown"),
            input=integer(usage.get("input_tokens")),
            output=integer(usage.get("output_tokens")),
            cache_read=integer(usage.get("cache_read_input_tokens")),
            cache_write=write_total - hour,
            cache_write_1h=hour,
            reasoning=integer((usage.get("output_tokens_details") or {}).get("thinking_tokens")),
        )
        if event.total and event.model != "<synthetic>":
            prior = events.get(key)
            if prior:
                event.ts = min(prior.ts, event.ts)
                for k in (
                    "input",
                    "output",
                    "cache_read",
                    "cache_write",
                    "cache_write_1h",
                    "reasoning",
                ):
                    setattr(event, k, max(getattr(event, k), getattr(prior, k)))
            events[key] = event
    return list(events.values())


def parse_claude_stats(path: Path, warnings: list[str]) -> tuple[list[Event], float]:
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise ValueError("invalid Claude stats cache")
    first = timestamp(document.get("firstSessionDate"))
    last_date = document.get("lastComputedDate")
    usage = document.get("modelUsage")
    if first is None or not isinstance(last_date, str) or not isinstance(usage, dict):
        raise ValueError("incomplete Claude stats cache")
    cutoff = timestamp(f"{last_date}T00:00:00Z")
    if cutoff is None:
        raise ValueError("invalid Claude stats cache date")
    cutoff += 86400
    result = []
    for model, totals in usage.items():
        if not isinstance(totals, dict):
            continue
        event = Event(
            key=identity("claude-stats", str(path.resolve()), model, last_date),
            provider="claude",
            ts=first,
            model=str(model),
            input=integer(totals.get("inputTokens")),
            output=integer(totals.get("outputTokens")),
            cache_read=integer(totals.get("cacheReadInputTokens")),
            cache_write=integer(totals.get("cacheCreationInputTokens")),
            windows=("total",),
            monthly=False,
            aggregate_until=cutoff,
        )
        if event.total:
            result.append(event)
    if result:
        warnings.append(
            "Claude-historie aangevuld uit historisch aggregaat; "
            "cache-TTL en dagverdeling ontbreken"
        )
    return result, cutoff


def parse_gemini(path: Path, warnings: list[str]) -> list[Event]:
    document = json.loads(path.read_bytes())
    result = []
    for row in document.get("messages", []):
        if not isinstance(row, dict) or row.get("type") != "gemini":
            continue
        usage = row.get("tokens")
        ts = timestamp(row.get("timestamp"))
        if not isinstance(usage, dict) or ts is None:
            continue
        read = integer(usage.get("cached"))
        prompt = integer(usage.get("input"))
        if read > prompt:
            warnings.append("Gemini-cachetellers groter dan input; record overgeslagen")
            continue
        # Gemini thoughts are separate from visible output in native CLI logs.
        thoughts = integer(usage.get("thoughts"))
        event = Event(
            key=identity("gemini", document.get("sessionId"), row.get("id") or row["timestamp"]),
            provider="gemini",
            ts=ts,
            model=str(row.get("model") or "unknown"),
            input=prompt - read,
            output=integer(usage.get("output")) + thoughts,
            cache_read=read,
            reasoning=thoughts,
        )
        if event.total:
            result.append(event)
    return result


PARSERS = {"codex": parse_codex, "claude": parse_claude, "gemini": parse_gemini}
