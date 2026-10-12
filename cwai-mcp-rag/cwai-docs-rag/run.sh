#!/bin/bash
# Start the Cisco Docs RAG MCP server (streamable-http on :8085).
# Prereq: python3 with the `mcp` package (pip install -r requirements.txt, ideally in a venv).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip -q install -r requirements.txt
fi
pkill -f "server.py" 2>/dev/null || true
sleep 1
setsid ./.venv/bin/python -u server.py > rag.log 2>&1 < /dev/null &
sleep 2
echo "cisco-docs-rag MCP started on http://0.0.0.0:8085/mcp (log: $DIR/rag.log)"
