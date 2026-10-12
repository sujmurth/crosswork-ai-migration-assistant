# cisco-docs-rag — a RAG MCP for CW AI (IOS-XR / IOS-XE / Cisco 8000)

A lab-deployable Retrieval-Augmented-Generation tool for Crosswork AI. It retrieves
Cisco **IOS-XR / IOS-XE / Cisco 8000** QoS + SRv6 command-reference snippets and exposes
them to CW AI agents as an **MCP tool** (`rag_search_cisco_docs`). This is the "custom doc
RAG" alternative to Cisco SMX when SMX is not deployed.

## Contents
- `corpus.jsonl` — curated Cisco doc snippets (`{id, platform, feature, title, text, source}`). Add more lines to grow coverage.
- `retriever.py` — pure-stdlib **BM25** retriever (no third-party deps). Test: `python3 retriever.py cisco 8000 egress priority`.
- `server.py` — **MCP server** exposing `rag_search_cisco_docs(query, platform="", k=5)` over `streamable-http`.
- `remote-mcp.json` — registration config for `cwaictl mcp remote add`.
- `run.sh` / `requirements.txt` — start the server (creates a venv, installs `mcp`).

## Deploy
1. Copy this folder to a host the **CW AI cluster (.87) can reach** (e.g. the jump host `172.20.163.20`).
2. Start it: `bash run.sh`  → serves `http://0.0.0.0:8085/mcp`.
3. Point `remote-mcp.json` `url` at that host (default `http://172.20.163.20:8085/mcp`).
4. Register with CW AI:
   ```bash
   cwaictl mcp remote add --from-file remote-mcp.json -u https://172.20.163.87 -k --trust-ca
   cwaictl mcp list-tools -u https://172.20.163.87 -k --trust-ca | grep rag_search_cisco_docs
   ```
5. The QoS / SRv6 / workflow-author agents can now call `rag_search_cisco_docs` to ground
   their output in Cisco command-reference facts.

## Notes
- **Offline-capable**: retrieval is BM25 (no embedding model/API). Swap in an embedding
  vector store later if desired — the tool contract stays the same.
- **RAG grounds, dry-run proves**: keep golden-config `?dry-run=native` validation — the
  live device is the final source of truth.
- Grow the corpus by appending JSONL lines (ingest real IOS-XR/XE config-guide sections,
  tagged by `platform`/`feature`/release).
