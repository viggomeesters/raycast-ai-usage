"""Read-only quota probes. No inference, browser scraping or credential writes."""

from __future__ import annotations

import json
import math
import os
import selectors
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .model import Limit, Quota, clean, timestamp


class ProbeError(Exception):
    """A bounded, safe diagnostic suitable for the user."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError("API-redirect geweigerd")


def request_json(
    url: str, token: str, body: dict | None = None, headers: dict | None = None
) -> dict:
    if url not in {
        "https://api.anthropic.com/api/oauth/usage",
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    }:
        raise ProbeError("Onbekend quota-endpoint")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "raycast-ai-usage/0.1",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
            data = json.loads(response.read(2_000_000))
        if not isinstance(data, dict):
            raise ProbeError("Onverwacht API-antwoord")
        return data
    except urllib.error.HTTPError as exc:
        # Never expose a body, request header or a credential-bearing exception.
        raise ProbeError(f"Quota-API HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProbeError("Quota-API niet bereikbaar") from None
    except (ValueError, UnicodeError):
        raise ProbeError("Onleesbaar quota-antwoord") from None


def executable(name: str) -> str | None:
    override = os.environ.get(f"AI_USAGE_{name.upper()}_BIN")
    if override:
        return override if os.access(override, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return found
    candidates = [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    if name == "codex":
        candidates += [
            f"/Applications/{app}.app/Contents/Resources/codex" for app in ("ChatGPT", "Codex")
        ]
    return next((p for p in candidates if os.access(p, os.X_OK)), None)


def codex_rpc(binary: str, timeout: float = 12) -> dict:
    process = subprocess.Popen(
        [binary, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None

    def send(message: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "raycast_ai_usage", "version": "0.1.0"}},
            }
        )
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            if not selector.select(min(0.25, max(0, deadline - time.monotonic()))):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 2_000_000:
                raise ProbeError("Codex-antwoord te groot")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    response = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(response, dict):
                    continue
                if response.get("id") in (1, 2) and response.get("error"):
                    raise ProbeError("Codex-limieten niet beschikbaar; controleer codex login")
                if response.get("id") == 1:
                    send({"method": "initialized"})
                    send({"id": 2, "method": "account/rateLimits/read"})
                elif response.get("id") == 2:
                    result = response.get("result")
                    if isinstance(result, dict):
                        return result
                    raise ProbeError("Onverwacht Codex-antwoord")
        raise ProbeError("Codex reageert niet binnen 12 seconden")
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def percent(value: object, scale: float = 100) -> float | None:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or not 0 <= value <= scale:
        return None
    return 100 * float(value) / scale


def parse_codex_quota(data: dict) -> Quota:
    buckets = data.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = data.get("rateLimits") or {}
        buckets = {legacy.get("limitId") or "codex": legacy}
    quota = Quota("codex", "ok", "codex app-server")
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used = percent(window.get("usedPercent"))
            if used is None:
                continue
            minutes = window.get("windowDurationMins")
            minutes = minutes if isinstance(minutes, int) and minutes > 0 else None
            label = clean(bucket.get("limitName") or bucket_id, 40)
            limit = Limit(label, 100 - used, minutes, timestamp(window.get("resetsAt")))
            (quota.limits if bucket_id == "codex" else quota.extras).append(limit)
    if not quota.limits and not quota.extras:
        quota.status = "unavailable"
        quota.note = "Account retourneert geen numerieke limieten"
    return quota


def codex_quota(home: Path) -> Quota:
    binary = executable("codex")
    if not binary:
        return Quota("codex", "unavailable", "codex app-server", "Codex CLI niet gevonden")
    return parse_codex_quota(codex_rpc(binary))


def claude_quota(home: Path, provider_override: str = "auto") -> Quota:
    selected_provider = provider_override
    if selected_provider == "auto":
        for flag, name in (
            ("CLAUDE_CODE_USE_BEDROCK", "bedrock"),
            ("CLAUDE_CODE_USE_VERTEX", "vertex"),
            ("CLAUDE_CODE_USE_FOUNDRY", "foundry"),
        ):
            if os.environ.get(flag) == "1":
                selected_provider = name
                break
    if selected_provider in ("bedrock", "vertex", "foundry", "api"):
        return Quota(
            "claude",
            "unsupported",
            "provider configuration",
            f"{selected_provider}: geen 5h/7d-abonnementslimiet",
        )
    binary = executable("claude")
    if binary:
        try:
            response = subprocess.run(
                [binary, "auth", "status"], capture_output=True, timeout=5, check=False
            )
            auth = json.loads(response.stdout)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            auth = {}
        provider = str(auth.get("apiProvider") or "").lower()
        if provider in ("bedrock", "vertex", "foundry") or auth.get("authMethod") == "api_key":
            return Quota(
                "claude",
                "unsupported",
                "claude auth status",
                f"{clean(provider or 'API', 18)}: geen 5h/7d-abonnementslimiet",
            )
    config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    credentials = {}
    path = config_root / ".credentials.json"
    if path.exists():
        credentials = json.loads(path.read_bytes())
    # Never use global credentials for a custom Claude profile.
    if (
        not credentials.get("claudeAiOauth")
        and sys.platform == "darwin"
        and "CLAUDE_CONFIG_DIR" not in os.environ
    ):
        response = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if response.returncode == 0:
            credentials = json.loads(response.stdout)
    oauth = credentials.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        return Quota(
            "claude",
            "auth_required",
            "Claude OAuth",
            "Geen Claude OAuth-login; API/Bedrock heeft andere limieten",
        )
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires < time.time() * 1000:
        return Quota("claude", "auth_required", "Claude OAuth", "Login verlopen; open Claude Code")
    data = request_json(
        "https://api.anthropic.com/api/oauth/usage",
        oauth["accessToken"],
        headers={"anthropic-beta": "oauth-2025-04-20"},
    )
    return parse_claude_quota(data)


def parse_claude_quota(data: dict) -> Quota:
    quota = Quota("claude", "ok", "Claude OAuth")
    for key, value in data.items():
        if key != "five_hour" and not key.startswith("seven_day"):
            continue
        if not isinstance(value, dict):
            continue
        used = percent(value.get("utilization"))
        if used is None:
            continue
        minutes = 300 if key == "five_hour" else 10080
        limit = Limit(clean(key, 40), 100 - used, minutes, timestamp(value.get("resets_at")))
        (quota.limits if key in ("five_hour", "seven_day") else quota.extras).append(limit)
    if not quota.limits:
        quota.status = "unavailable"
        quota.note = "Account retourneert geen numerieke limieten"
    return quota


def gemini_quota(home: Path) -> Quota:
    root = Path(os.environ.get("GEMINI_HOME", home / ".gemini"))
    path = root / "oauth_creds.json"
    if not path.exists():
        return Quota("gemini", "auth_required", "Gemini CLI", "Geen Gemini CLI-login gevonden")
    settings = root / "settings.json"
    if settings.exists():
        selected = (
            json.loads(settings.read_bytes())
            .get("security", {})
            .get("auth", {})
            .get("selectedType")
        )
        if selected in ("api-key", "vertex-ai"):
            return Quota(
                "gemini", "unsupported", "Gemini CLI", "API/Vertex: geen 5h/7d-abonnementslimiet"
            )
    credentials = json.loads(path.read_bytes())
    if not credentials.get("access_token"):
        return Quota("gemini", "auth_required", "Gemini CLI", "Geen bruikbare Gemini-login")
    expiry = credentials.get("expiry_date")
    if not isinstance(expiry, (int, float)) or expiry < time.time() * 1000:
        return Quota(
            "gemini",
            "auth_required",
            "Gemini CLI",
            "Login verlopen; consumenten gebruiken nu Antigravity",
        )
    token = credentials["access_token"]
    context = request_json(
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        token,
        {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
    )
    if not context.get("currentTier") and any(
        isinstance(t, dict) and t.get("reasonCode") == "UNSUPPORTED_CLIENT"
        for t in context.get("ineligibleTiers", [])
    ):
        return Quota(
            "gemini", "unsupported", "Gemini CLI", "Gemini-consumentenaccount: gebruik Antigravity"
        )
    project = context.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id")
    data = request_json(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        token,
        {"project": project} if isinstance(project, str) else {},
    )
    return parse_gemini_quota(data)


def parse_gemini_quota(data: dict) -> Quota:
    quota = Quota("gemini", "ok", "Gemini CLI", "Quota per model; 5h/7d niet gerapporteerd")
    models: dict[str, Limit] = {}
    for bucket in data.get("buckets", []):
        if not isinstance(bucket, dict):
            continue
        remaining = percent(bucket.get("remainingFraction"), 1)
        if remaining is None:
            continue
        model = clean(bucket.get("modelId") or "model", 40)
        if model not in models or remaining < models[model].remaining:
            models[model] = Limit(model, remaining, None, timestamp(bucket.get("resetTime")))
    quota.extras = list(models.values())
    if not models:
        quota.status = "unavailable"
        quota.note = "Geen numerieke Gemini-quota ontvangen"
    return quota


def fetch(provider: str, home: Path, offline: bool = False, claude_provider: str = "auto") -> Quota:
    if offline:
        return Quota(provider, "offline", "offline", "Live limieten niet opgehaald")
    try:
        if provider == "claude":
            return claude_quota(home, claude_provider)
        return {"codex": codex_quota, "claude": claude_quota, "gemini": gemini_quota}[provider](
            home
        )
    except ProbeError as exc:
        return Quota(provider, "unavailable", "quota probe", str(exc))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return Quota(
            provider, "unavailable", "quota probe", "Quota-bron niet beschikbaar of ongeldig"
        )
