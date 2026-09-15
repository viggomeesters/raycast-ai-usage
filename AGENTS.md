# Project contract

Build a small native Raycast Extension, backed by the local Python CLI, showing
Gemini, Codex and Claude account limits and rolling 24h, 7d and 30d token usage
and estimated token costs.
This repository uses the explicit user request as its bounded task contract.

- Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run ty check src/`, `uv build`, and `cd extension && npm run check` before
  shipping.
- No GitHub Actions. All checks run locally.
- Read provider sessions and credentials only for this report. Never commit real
  logs, account identifiers, credentials, private settings or usage snapshots.
- Never write to provider credential stores or sessions, or invoke a model to
  measure usage. Never treat missing data as zero or infer a quota from tokens.
- Keep token categories disjoint. Unknown model prices remain explicitly unpriced.
- Keep the final Raycast summary compact; diagnostics go before it.
- Tests use synthetic records and mocked quota endpoints only.
