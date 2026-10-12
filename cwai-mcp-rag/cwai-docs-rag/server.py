"""Cisco Docs RAG — MCP server exposing a retrieval tool over corpus.jsonl.

Transport: streamable-http (what `cwaictl mcp remote add` consumes).
Tool: rag_search_cisco_docs(query, platform="", k=5) -> list of ranked snippets.

Run:  python3 server.py           # serves http://0.0.0.0:8085/mcp
Register (from a host the CW AI cluster can reach):
      cwaictl mcp remote add --from-file remote-mcp.json -u https://<cwai> -k --trust-ca
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from retriever import search

HOST = os.environ.get("RAG_HOST", "0.0.0.0")
PORT = int(os.environ.get("RAG_PORT", "8085"))

mcp = FastMCP("cisco-docs-rag", host=HOST, port=PORT)


@mcp.tool()
def rag_search_cisco_docs(query: str, platform: str = "", k: int = 5) -> list[dict]:
    """Retrieve Cisco IOS-XR / IOS-XE / Cisco 8000 command-reference snippets for a query.

    Args:
        query: natural-language or keyword query (e.g. "Cisco 8000 egress priority queue").
        platform: optional filter — "IOS-XR", "IOS-XE", "Cisco 8000", or "ASR 9000".
        k: number of snippets to return (default 5).

    Returns a ranked list of {id, platform, feature, title, text, source, score}.
    Use the snippets to ground config translation; always dry-run validate before commit.
    """
    return search(query, platform or None, k)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
