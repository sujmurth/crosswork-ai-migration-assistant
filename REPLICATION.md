# Full Replication Backup — Conversational AI Assistant for Migration (Crosswork AI)

Backup date: **2026-10-12**. This package contains everything needed to rebuild the
SRv6 + QoS migration demo (CW AI agents, CWM workflows, NSO golden-config, the custom
AI assistant portal, and the doc-RAG MCP) on a **different setup**.

> Secrets are NOT stored here. Replace host IPs and credentials for the target environment.
> The CWM workflows and golden-config templates are hard-wired to the source topology
> (node-1..8, vpn-101/vpn-129, RRv6 2011::11/::12, locator fcbb:0:X::/48) — **re-target**
> them to the new fabric before use.

## Source environment (roles)
| Host | Role | Access (source) |
| --- | --- | --- |
| `172.20.163.87` | **CW AI platform** (agent runtimes, MCP gateway, LLM proxy) | via cwaictl (admin) |
| `172.20.163.20` | **Jump host** — Docker + `cwaictl`; hosts the doc-RAG MCP | admin / ${LAB_SSH_PASSWORD} |
| `198.18.134.221:30603` | **CNC** — CWM engine + NSO golden-config (SRv6 MOP) | admin / ${CNC_PASSWORD} |
| `172.20.163.49:30603` | **CNC** — Cisco 8000 fabric (QoS target) | admin / ${CNC_PASSWORD} |
| `198.18.134.29:8077` | **NSO KVM host** — custom AI Assistant portal | cisco / cisco |

## Contents of this backup
```
cwai-agents/                 CW AI agent definitions (exported) + full source
  agents-list.txt            all agents on .87
  agent-*.json               exported metadata for the 3 migration agents
  mcp-resources.txt          registered MCP resources (incl. cisco_docs_rag)
  mcp-tools.txt              full MCP tool catalog (incl. rag_search_cisco_docs)
  cwai-srv6-agent/           source: MPLS->SRv6 HITL agent (main.py, metadata.json, Dockerfile*, Makefile, recorded-fabric.json)
  cwai-qos-agent/            source: QoS->Cisco 8000 agent (+ skills/, sources/)
  cwai-workflow-author/      source: CWM workflow author agent
cwai-mcp-rag/
  cwai-docs-rag/             doc-RAG MCP (corpus.jsonl, retriever.py, server.py, remote-mcp.json, run.sh)
cwm-workflows-221/           102 CWM workflow definitions (+ _all-workflows-list.json)
nso-golden-config-221/
  _all-templates.json        full golden-config template subtree
  _all-applications.json     golden-config device bindings
  templates/                 22 templates as .json + .j2/.cfg bodies
assistant-portal-29/
  srv6-portal/               the full custom assistant (server.py, static/, nl_check.py, run.sh, config.json)
```

## Replication steps (target setup)

### 0. Prereqs on the target
- A Crosswork AI platform reachable by `cwaictl` (set `CWAI_BASE_URL`).
- A CNC with CWM + NSO (golden-config enabled) for the workflows.
- A Linux host with **Docker** + `cwaictl` (to build/upload agents) — the "jump host".
- Python 3.10+ on the portal host and the RAG host.

### 1. Deploy the 3 CW AI agents
Each agent folder has a `Makefile`. On the jump host (Docker + cwaictl authenticated):
```bash
cd cwai-srv6-agent         # repeat for cwai-qos-agent, cwai-workflow-author
#   edit metadata.json env (CNC_HOST/USER/PASSWORD) for the target CNC
make build                 # builds image, saves image.tar, packs <name>.tar
cwaictl agent register --from-file <name>.tar -u https://<CWAI> -k --trust-ca
#   on later updates:
cwaictl agent update --name <name> --from-file <name>.tar -u https://<CWAI> -k --trust-ca
cwaictl runtime delete-instances <name> -u https://<CWAI> -k --trust-ca   # IMPORTANT: load new image
```
Offline rebuild (no internet): use `Dockerfile.offline` (reuses the base image, swaps main.py).
Verify: `cwaictl agent get --name <name>` → status Active + suggestion chip.

### 2. Import CWM workflows to the target CNC
Child subflows must exist before parents (import leaves first). For each definition in
`cwm-workflows-221/` (skip `_all-workflows-list.json`):
```bash
# body = [{"definition": <def>, "wfTags": <tags>}]
curl -sk -X POST "https://<CNC>:30603/crosswork/cwm/v2/workflowImport" \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d @<wrapped-definition>.json
```
(Get `$JWT` via the 2-step SSO: POST `/crosswork/sso/v1/tickets` → TGT → POST `/tickets/{TGT}` with `service=https://<CNC>:30603/app-dashboard`.)

### 3. Load NSO golden-config templates to the target CNC
For each `nso-golden-config-221/templates/*.json`:
```bash
curl -sk -X PUT "https://<CNC>:30603/crosswork/proxy/nso/restconf/data/golden-config:golden-config/template=<name>" \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/yang-data+json" \
  -d @<template>.json
```
The `.j2` files are the human-readable Jinja bodies for review/editing.

### 4. Deploy the custom AI Assistant portal
On the target NSO/portal host:
```bash
scp -r assistant-portal-29/srv6-portal  <host>:~/
ssh <host> 'cd ~/srv6-portal && bash run.sh'   # serves http://0.0.0.0:8077
#   edit srv6-migration-portal/config.json for the target CNC/workflow
```
Access `http://<host>:8077/assistant.html`.

### 5. Deploy + register the doc-RAG MCP
On a host the CW AI cluster can reach:
```bash
scp -r cwai-mcp-rag/cwai-docs-rag <host>:~/
ssh <host> 'cd ~/cwai-docs-rag && bash run.sh'     # pip installs mcp>=1.9,<2, serves :8085/mcp
#   set remote-mcp.json "url" to http://<host>:8085/mcp
cwaictl mcp remote add --from-file cwai-docs-rag/remote-mcp.json -u https://<CWAI> -k --trust-ca
cwaictl mcp list-tools -u https://<CWAI> -k --trust-ca | grep rag_search_cisco_docs
```

## Re-targeting checklist (source -> target)
- Agent `metadata.json`: `CNC_HOST/PORT/USER/PASSWORD`, `suggestion` chip.
- Workflows/golden-config: device names (node-1..8), RRv6 (2011::11/::12), locator (fcbb:0:X::/48),
  VPN names (vpn-101/vpn-129), prefixes (30.1.1.0/24), interfaces.
- Portal `config.json`: default CNC + workflow name.
- RAG `remote-mcp.json`: `url` to the RAG host.

## Notes
- Agent image `.tar` bundles are **not** included (rebuildable from source via `make build`).
- Long-running helpers (portal, RAG server) are bare background processes — add systemd/@reboot
  supervision on the target for durability.
