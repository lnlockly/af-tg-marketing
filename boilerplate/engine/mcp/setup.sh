#!/usr/bin/env bash
# setup.sh — one-time prep for the leads-ops MCP: install the MCP SDK. Idempotent +
# latched; node_modules survives pod restarts via the DATA-PVC overlay.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f ".setup-done" ] && [ -d node_modules/@modelcontextprotocol ]; then
  echo "[leads-ops] already installed"; exit 0
fi
echo "[leads-ops] npm install @modelcontextprotocol/sdk…"
npm install --no-audit --no-fund --loglevel=error
touch ".setup-done"
echo "[leads-ops] ready — register: hermes mcp add leads-ops --command node --args \$PWD/server.mjs --env TGENGINE_DIR=/app/data/tg-engine"
