# Conversational AI Assistant for Migration using Crosswork AI

AI‑assisted network migrations on **Cisco Crosswork AI (CW AI)**, driven conversationally and executed as standards‑based **CNCF Serverless Workflows** via **CWM** + **NSO golden‑config**.

## 🎥 Demo recording
[Watch the demo on Vidcast](https://app.vidcast.io/share/cbb2ddf9-8823-4b66-97ae-92942f5838e3?playerMode=vidcast)

Two use cases:
1. **Brownfield MPLS/LDP → SRv6** migration with L2/L3VPN services (dual‑plane cutover, hitless rollback).
2. **QoS migration ASR 9000 → Cisco 8000** (extract → translate → push → verify → correct).

> **Security note:** this repository is a scrubbed snapshot. All lab credentials/tokens have been replaced with placeholders (`${CNC_PASSWORD}`, `${LAB_SSH_PASSWORD}`, `${NSO_PASSWORD}`, `<JWT_REDACTED>`). Set real values via environment variables / secrets before running. Internal lab IPs/topology remain as examples — re‑target for your environment.

## What's here
```
cwai-agents/            CW AI ADK agents (source) + exported definitions
  cwai-srv6-agent/      MPLS→SRv6 HITL agent (narrated, approval-gated, dry-run)
  cwai-qos-agent/       QoS→Cisco 8000 agent (extract/translate/push/verify, + skills/, sources/)
  cwai-workflow-author/ NL → dry-run-validated CWM workflow author
  agent-*.json          exported agent metadata;  mcp-tools.txt / mcp-resources.txt
cwai-mcp-rag/
  cwai-docs-rag/        custom RAG MCP: BM25 retriever over IOS-XR/XE/Cisco 8000 command refs
cwm-workflows-221/      CWM Serverless Workflow definitions (SRv6-Migration-MOP + subflows + discovery)
nso-golden-config-221/  NSO golden-config (Jinja) templates (.json + .j2) + applications
assistant-portal-29/    custom web AI Assistant portal (server.py, static/, nl_check.py)
portable-discovery/*    00.discoverServices — reads IETF L2NM/L3NM models → service list (portable)
REPLICATION.md          full host inventory + step-by-step rebuild on another CNC
```

## Architecture
```
Operator ── chat ──> AI Assistant portal ──> CW AI platform (ADK agents + MCP gateway)
                                                   │ CAS token
                                                   ▼
                                     CNC: CWM (Serverless Workflow) ──> NSO golden-config / live-status ──> Routers
```
Safety model: **fixed skeleton (AI fills parameters) · dry‑run validation · human approvals · pre/post checks (HALT) · hitless rollback · CNCF Serverless Workflow.**

## Key pieces
- **SRv6 migration workflow** (`SRv6-Migration-MOP`): 8 phases, each `pre-check → gate → action subflow → golden-config template → post-check → Webex approval`, with auto‑rollback on cutover failure.
- **Portability**: `00.discoverServices` reads the IETF **L3NM/L2NM** service models (`ietf-l3vpn-ntw` / `ietf-l2vpn-ntw`) and returns the full `l3_services[]` / `l2_services[]` list — no hard‑coded VPNs/PEs/locators.
- **RAG grounding** (`cwai-docs-rag`): an MCP tool (`rag_search_cisco_docs`) registered to CW AI to ground config translation in Cisco IOS‑XR/XE/8000 command references.

## Deploy / replicate
See **[REPLICATION.md](REPLICATION.md)** for the full step‑by‑step (deploy agents, import workflows, load golden‑config, start the portal, register the RAG MCP) and the re‑targeting checklist. Short version:
```bash
# Agents (host with Docker + cwaictl)
cd cwai-agents/cwai-srv6-agent && make build && cwaictl agent register --from-file *.tar -u https://<CWAI> -k --trust-ca

# Workflows + golden-config -> target CNC (via RESTCONF, see REPLICATION.md)
# Portal
cd assistant-portal-29/srv6-portal && bash run.sh        # http://<host>:8077/assistant.html
# RAG MCP
cd cwai-mcp-rag/cwai-docs-rag && bash run.sh && cwaictl mcp remote add --from-file remote-mcp.json -u https://<CWAI> -k --trust-ca
```

## Status
- SRv6 migration agent + QoS agent + workflow‑author agent: built and deployable.
- `SRv6-Migration-MOP` + `00.discoverServices`: validated on a live CNC (no‑commit dry‑run).
- RAG MCP (`rag_search_cisco_docs`): deployable; registered via `cwaictl mcp remote add`.

---
*Snapshot generated 2026‑10‑12. Credentials scrubbed for publication.*
