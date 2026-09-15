# Raycast AI Usage

A small local Script Command for **Gemini, Codex and Claude**: remaining account
limits, tokens and estimated API token costs over rolling **1d, 7d, 30d and 365d**,
plus the complete locally available history.
Python 3.11+, no runtime dependencies. The Raycast interface is Dutch.

```text
AI-GEBRUIK
Bijgewerkt: 2026-09-13 14:30

GEMINI
24h      0 tokens  ($0.00)
7d       0 tokens  ($0.00)
30d      0 tokens  ($0.00)
365d     150.0k tokens  ($0.23)
Totaal   150.0k tokens  ($0.23)

CLAUDE
24h      800.0k tokens  ($3.10)
7d       5.6M tokens  ($21.70)
30d      24.0M tokens  ($93.00)
365d     80.0M tokens  ($310.00)
Totaal   80.0M tokens  ($310.00)

CODEX
24h      1.2M tokens  ($4.20)
7d       8.4M tokens  ($29.40)
30d      36.0M tokens  ($126.00)
365d     120.0M tokens  ($420.00)
Totaal   120.0M tokens  ($420.00)

TOTALEN
24h      2.0M tokens  ($7.30)
7d       14.0M tokens  ($51.10)
30d      60.0M tokens  ($219.00)
365d     200.2M tokens  ($730.23)
Totaal   200.2M tokens  ($730.23)

MAANDTOTALEN
2026-09  42.0M tokens  ($153.20)
2026-08  58.0M tokens  ($211.40)
2026-07  ≥37.0M* tokens  (≥$136.00*)
2026-06  ≥24.0M* tokens  (≥$89.63*)
2026-05  0 tokens  ($0.00)
2026-04  0 tokens  ($0.00)
2026-03  0 tokens  ($0.00)
2026-02  0 tokens  ($0.00)
2026-01  0 tokens  ($0.00)
2025-12  0 tokens  ($0.00)
2025-11  0 tokens  ($0.00)
2025-10  0 tokens  ($0.00)

LIMIETEN OVER
Gemini   5h ?   7d ?
Claude   5h 88%   7d 65%
Codex    5h —   7d 41%
```

Illustrative numbers only. The vertically grouped plain-text result stays readable
in Raycast and can be copied directly into Obsidian or another notes app. Remaining
limits are last because Raycast opens a completed Script Command at the bottom.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Raycast](https://www.raycast.com/), then:

```sh
git clone https://github.com/viggomeesters/raycast-ai-usage.git
cd raycast-ai-usage
uv sync --no-dev
chmod +x raycast/ai-usage.sh
uv run ai-usage
```

In Raycast, open **Extensions → + → Add Script Directory** and select this
repository's `raycast` directory. Search for **AI Usage** and press Enter.
You can assign a hotkey or alias in Raycast. No background service is installed.
Run once in Terminal to build the cache and handle any macOS Keychain prompt.

```sh
uv run ai-usage --json           # Machine-readable report
uv run ai-usage --offline        # Logs only, no credentials/network/CLI probes
uv run ai-usage --output-tokens  # Generated output incl. reasoning; cost still all tokens
uv run ai-usage --snapshot-db /path/to/snapshots.sqlite3
make test check build           # Local checks, no GitHub Actions
```

The first scan may take longer on large histories. Unchanged files use a local
SQLite metadata cache; changed files are rescanned. One provider's missing
credentials or network error does not hide the others.

## Snapshots

Every successful CLI or Raycast run appends one point-in-time snapshot to
`~/.local/share/raycast-ai-usage/snapshots.sqlite3` (or `$XDG_DATA_HOME`). The
database has private file permissions and stores normalized totals, coverage and
month buckets only. Quotas, credentials, prompts, responses and source paths are
not stored.

Snapshots are explicitly marked `additive = 0`: their totals must never be summed.
The ledger compares only consecutive cumulative totals under the same measurement
contract. It records a delta only when coverage is stable, totals never decrease,
and a provider's latest event advances when its total grows. File-count changes,
counter decreases and historical backfills start a new comparison baseline instead
of becoming usage. USD deltas are omitted when the checked-in pricing snapshot
changes. JSON output includes the snapshot id, comparison status and any safe delta.

## What is measured

| Provider | Local history | Account limits |
| --- | --- | --- |
| Codex | `~/.codex/sessions` and `archived_sessions` | Signed-in `codex app-server`, `account/rateLimits/read` |
| Claude | `~/.claude/projects`, `stats-cache.json`, custom profiles and embedded Desktop Code session stores | Claude Code OAuth usage; Bedrock/API/Vertex have different quota systems |
| Gemini | `~/.gemini/tmp/**/chats/session-*.json` | Gemini CLI OAuth quota buckets, per model |

`CODEX_HOME`, `CLAUDE_CONFIG_DIR` and `GEMINI_HOME` override data roots. Custom
Claude profiles never fall back to the global Keychain. Set `AI_USAGE_CODEX_BIN`
or `AI_USAGE_CLAUDE_BIN` to an absolute executable path when needed. Codex bundled
in `/Applications/ChatGPT.app` or `/Applications/Codex.app` is detected too.

Raycast may not inherit your shell's Bedrock/Vertex environment. In that case,
create `~/.config/raycast-ai-usage/config.json` (or under `$XDG_CONFIG_HOME`):

```json
{"claude_provider": "bedrock"}
```

Allowed values: `auto` (default), `bedrock`, `vertex`, `foundry`, `api`. This only
selects the active quota system; historical costs remain standard API estimates.

- Quota percentages are **remaining**, not used. Window durations come from the
  provider. Primary does not necessarily mean 5h: it may be weekly.
- Model-specific Codex buckets (for example Spark) remain separate from the main
  allowance. Gemini's model buckets are not relabeled as 5h/7d windows.
- `—` means a window was not reported or is not applicable; `?` means unavailable.
  Neither means unlimited. No quota is inferred from tokens or dollar amounts.
- **Tokens totaal** = uncached input + cached input + cache creation + output.
  Reasoning is included in output once. Use JSON for each category.
- Claude's `stats-cache.json` preserves lifetime model totals after old transcripts
  disappear. Those totals supplement `365d` only when the entire recorded history
  fits inside that window, and always supplement `Totaal`. Newer transcripts remain
  the source for short rolling windows. The cache does not retain cache-write TTL,
  so supplemented cost estimates are marked partial with `*`.
- Claude streaming records are deduplicated by message/request; Codex repeated
  counters and replayed fork histories are deduplicated. Gemini thought tokens
  are added to its separately reported visible output.
- Windows use actual timestamps: the last 24 hours, 7 days, 30 days and 365 days,
  inclusive, ending at the report time. `Totaal` covers every available local log.
  These are not calendar-day buckets.
- `MAANDTOTALEN` shows the current calendar month and the previous eleven months,
  newest first. A `≥…*` month contains a known subtotal plus history that cannot be
  assigned precisely to that month, such as Claude's retained lifetime aggregate.
- History covers **local CLI/Code logs across local accounts**, not web chats,
  all machines, cloud tasks, general API usage, or an organization billing export.
  Old logs with no recent activity can yield zero; absent/unreadable history yields
  `?`. Read errors and unknown model prices mark partial results with `*`.

## Costs and prices

Amounts are **estimated standard API token value in USD**, using the checked-in
price snapshot dated **2026-09-13**. They are not subscription payments or billed
spend. Current prices are applied to all events in the requested period; this is
not a historical tariff reconstruction. Fast/priority and regional processing,
discounts, taxes, tools, audio-specific pricing and cache storage fees are excluded.
Long-context multipliers are applied per observed request when the model has a
known threshold; these remain estimates for sessions that switch context tiers.

For Bedrock, these are the same base Anthropic list-price equivalents, **not AWS
invoices**. A regional profile or other AWS-specific pricing can differ.
Unknown models keep their tokens but their cost is unknown; a partial subtotal
is shown as `≥$…*`. Prices are never borrowed from a similarly named model.

Primary pricing references:

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5)
- [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4)
- [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)

## Gemini and authentication limitations

Google [retired consumer Google login for Gemini CLI on June 18, 2026](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals).
Standard/Enterprise accounts still use Gemini CLI. Consumer accounts now use
Antigravity. This version reads Gemini CLI history and eligible live Gemini CLI
quotas; **Antigravity and Gemini web-chat usage are not yet supported**.

Expired logins remain visible as unavailable. This tool does not refresh or write
OAuth credentials; authenticate through the provider's own application.
Claude OAuth requires a subscription login with profile scope. API/Bedrock login
does not provide the Claude subscription's 5h/7d limits.

Quota endpoint mappings were checked against
[CodexBar's provider documentation](https://github.com/steipete/CodexBar/blob/main/docs/providers.md)
and [Gemini quota documentation](https://github.com/steipete/CodexBar/blob/main/docs/gemini.md).
Private provider endpoints may change. Codex uses its own installed RPC client;
Claude/Gemini use fixed HTTPS hosts and refuse redirects.

## Privacy and development

No telemetry or inference requests. Existing session logs are read only; only
normalized usage metadata enters `~/.cache/raycast-ai-usage/history.sqlite3`
(`$XDG_CACHE_HOME` is respected). Cache directories/files are created with private
permissions. Raw prompts, responses and credentials are not cached or printed.
The cache may contain local file paths and model/token metadata. Remove that
directory to rebuild it. JSON output contains usage data: treat it as private.

Credentials are used only for the provider's quota endpoint, never copied into the
repository. Public test records are synthetic. Tests do not call live providers.

```sh
uv sync
make test check build
```

MIT licensed. Unaffiliated with the providers or Raycast.
