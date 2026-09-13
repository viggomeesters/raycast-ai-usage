#!/bin/bash

# @raycast.schemaVersion 1
# @raycast.title AI Usage
# @raycast.mode fullOutput
# @raycast.packageName AI Usage
# @raycast.icon 📊
# @raycast.description Gemini, Codex en Claude: limieten, tokens en geschatte kosten
# @raycast.author Viggo Meesters
# @raycast.authorURL https://github.com/viggomeesters/raycast-ai-usage

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
if [[ -x "$REPO_DIR/.venv/bin/ai-usage" ]]; then
  exec "$REPO_DIR/.venv/bin/ai-usage" "$@"
fi
if command -v ai-usage >/dev/null 2>&1; then
  exec ai-usage "$@"
fi
echo "AI Usage is nog niet geïnstalleerd."
echo "Open deze repo in Terminal en voer uit: uv sync --no-dev"
exit 1
