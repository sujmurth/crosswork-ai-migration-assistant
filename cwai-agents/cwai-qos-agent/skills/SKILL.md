---
name: 8000-platform-hub-config-translation
description: Translate configurations from ASR 900 series (IOS XE), NCS 540/NCS55xx/NCS57xx
  (IOS XR), or ASR 9000 (IOS XR) into equivalent IOS XR configuration tailored for
  Cisco 8000 platforms. Reports confidence level per section and flags uncertain or
  unsupported areas.
license: TBD
metadata:
  category: config-migration
  version: '3.0'
  author: smx-team
  source_directive: L2_config_translation
---

# Config Translation Skill — Cisco Legacy Platforms → Cisco 8000 IOS XR

## Purpose

Translate a network device configuration (full or snippet) from a supported source
platform into Cisco 8000 IOS XR. Every section includes a **confidence level**;
uncertain and unsupported areas are flagged.

**Default target release:** Latest IOS XR (currently 26.2.1) unless user specifies otherwise.

---

## Supported Source Platforms

| Cat | Source | OS | Reference |
|---|---|---|---|
| 1 | ASR 900 (920/903/907) | IOS XE | `CONFIG_TRANSLATION_ASR900.md` |
| 2 | NCS 540/55xx/57xx | IOS XR | `CONFIG_TRANSLATION_NCS.md` |
| 3 | ASR 9000 | IOS XR | `CONFIG_TRANSLATION_ASR9K.md` |

Common platform constraints, output template, and confidence rules → `CONFIG_TRANSLATION.md`

**QoS-only conversions:** load `CONFIG_TRANSLATION_QOS.md` together with this file; per-OS files are not required when only QoS is in scope. The QoS file is purely additive and references the existing `RULE_QOS_*`, `LINT_*`, and `COV_QOS_*` IDs defined below — no rule numbering or stable IDs change.

---

## Workflow

### Step 1 — Identify Source Platform

Match fingerprints from `CONFIG_TRANSLATION.md § OS Detection`.
If ambiguous, ask user to confirm.

### Step 2 — Segment the Configuration

Split into logical sections (1–20). Full list in `CONFIG_TRANSLATION.md § Sections`.

### Step 3 — Consult MCP Experts

Query per detected section (not all at once):

1. **Feature support** — `smx_rag_search` first (MDRAG), then `smx_consult` if needed:
   ```
   smx_consult(operation="ask",
     prompt="Is <FEATURE> supported on Cisco 8000 <PID> with IOS XR <VERSION>?",
     targets={"8000-customers-hub": "Verify 8000 feature support"})
   ```

2. **Config template** — ask `xrconfig_expert`:
   ```
   smx_consult(operation="ask",
     prompt="Provide IOS XR config for <FEATURE> on Cisco 8000. Source: <SNIPPET>. Version: <VER>.",
     targets={"xrconfig_expert": "IOS XR config template"})
   ```

3. **Fallback** (MCP unavailable): MDRAG → general IOS XR knowledge.

4. **After MCP response** — always apply Critical Rules below. MCP output does not override rules.

### Step 4 — Translate Each Section

- Direct/functional equivalent → Section 3 (translated config)
- Unsupported → Section 6 only (never in Section 3, not even commented out)
- Needs review → Section 3 (best-effort) + Section 5 (note)

### Step 5 — Assign Confidence

| Level | Meaning |
|---|---|
| High (≥90%) | 1:1 or near-1:1 mapping |
| Medium (60–89%) | Equivalent exists, CLI differs or needs verification |
| Low (<60%) | No direct equivalent; manual review needed |
| N/A | Not supported on Cisco 8000 → Section 6 |

### Step 6 — Render Report

Use output template from `CONFIG_TRANSLATION.md § Output Template`.
Section 3 = one fenced code block with `! === <Name> ===` banners.

### Step 7 — MDRAG Cross-check

Query MDRAG for scale limits and caveats relevant to translated config.

---

## Critical Rules

1. **Never guess syntax** — mark Medium/Low and add review note if uncertain.
2. **Interface naming**: IOS XR long-form only (`TenGigE 0/0/0/0`, `HundredGigE 0/0/0/0`, `Bundle-Ether N`). See category reference file for interface mapping.
3. **ASR 900 EVC → IOS XR L2**: `service instance` → dot1q sub-interfaces with `l2transport`. Details in `CONFIG_TRANSLATION_ASR900.md`.
4. **IOS XE route-maps → IOS XR route-policy**: `route-map` → RPL `route-policy`. Show full RPL block.
5. **`ip` → `ipv4`/`ipv6`**: Replace all `ip address`, `ip route`, `ip access-group` with `ipv4` equivalents.
6. **QoS `bandwidth percent` unsupported**: Use `bandwidth remaining percent N` (≥26.3.1 on a100/k100/p100/q200 NPU — default assumption). On older releases/other NPUs: `bandwidth remaining ratio K` (max 63; `K = round(percent / 100 * 63)`).
7. **No WRED**: Skip all `random-detect`. Document in Section 6. Alt: `queue-limit` or Dual Queue Limit.
8. **No egress policing — replace with shaper**: `police` in egress `service-policy` → `shape average <rate>`. Absolute → `shape average N bps`. Percent → `shape average percent N`. Never remove silently. Document each conversion in Section 6.
9. **Egress queuing/remarking — mode-dependent split:**
   - **VOQ mode (default, all platforms):**
     - If source egress policy mixes queuing + remarking actions → split into
       `-REMARKING` (remarking only: `set dscp`/`set cos`/`set mpls exp topmost`,
       **NO** `set traffic-class`) + `-Q` (`match traffic-class`, scheduling only).
       Apply both `service-policy output` on same interface.
     - If source egress policy has queuing only (no remarking) → single `-Q`
       policy is sufficient. No egress `-REMARKING` needed.
     - In both cases, an **ingress** policy that sets `traffic-class` from
       EXP/DSCP/ACL is required. If source had no ingress policy on that
       interface, create one and flag as RECOMMENDATION (net-new policy).
   - **ETM/EFC mode (all K100 LCs / K100 Fixed / K100 Centralized / select P100 LCs):**
     - Egress `-REMARKING` policy is **MANDATORY** and **MUST** include
       `set traffic-class N` for every class. If source also had other remarking
       actions (`set dscp`, `set cos`, `set mpls exp topmost`), include them
       in `-REMARKING` alongside `set traffic-class N`.
     - Egress `-Q` policy: `match traffic-class` only, scheduling actions only.
     - Apply both `service-policy output` on same interface.
     - Ingress policy is optional (egress `-REMARKING` handles TC in this mode).
       However, ingress `set qos-group` (0–7) can feed egress `-REMARKING` via
       `match qos-group N` → `set traffic-class N`. Useful when source used
       qos-group to distinguish flows indistinguishable by header fields alone.
   - `set qos-group` is supported on **ingress only** (values 0–7). Forbidden
     in any egress policy-map (`LINT_QGRP_EGR`). Cannot coexist with `set dscp`,
     `set precedence`, or `set mpls experimental` in the same ingress class
     (`RULE_QOSGRP_COEXIST_BAN`). In VOQ mode, ingress must **also** set
     `traffic-class` (qos-group alone does not feed egress queuing).
9a. **QoS Mode Detection:** Before translating QoS, determine target mode:
   - Default: **VOQ mode** (all platforms unless explicitly stated otherwise).
   - **ETM/EFC mode** if: (a) user explicitly states ETM/EFC, OR (b) target is
     any K100 Line Card, K100 Fixed Platform, or K100 Centralized Platform with
     EFC configured, OR (c) target is a select P100 Line Card with EFC enabled.
   - If ambiguous, ask user. Never assume ETM/EFC — default to VOQ.
10. **TC ordering (contiguous from TC7)**: P1→TC7, P2→TC6, P3→TC5… no gaps. TC0 = class-default. Rebuild TC map from scratch when translating.
11. **Interface slot renaming**: ASR9K/NCS slot numbers may change. Note dependency on target 8000 PID.
12. **`hw-module profile` — exclude**: Remove all from translated output; document in Section 6.
13. **MPLS LDP → SR**: LDP supported but legacy. Flag and suggest SR migration.
14. **Preserve descriptions/comments** from source where possible.
15. **Never include unsupported config in translated block** — not even as IOS XR comments. Section 6 only.
16. **Comment style**: Place on separate `!! ` line before the config it explains. Never inline after config. Never reference rule numbers in comments.
17. **`set qos-group` mutual exclusion (`RULE_QOSGRP_COEXIST_BAN`)**: `set qos-group` cannot coexist with `set dscp`, `set precedence`, or `set mpls experimental` (imposition or topmost) in the same ingress class. If source has both, keep `set qos-group` (values 0–7), remove the conflicting `set` action, and move that marking to the egress `-REMARKING` policy (Rule 9).
18. **`set mpls exp imposition` invalid in egress**: Egress marking must use `set mpls experimental topmost N`. `imposition` is ingress-only on 8000.
19. **`match-all` qos-group + discard-class unsupported in egress**: Collapse CONFORM/EXCEED class-maps into single `match qos-group`; use CONFORM marking value.
20. **`traffic-class propagate` relocation (NCS→8000)**: NCS `hw-module profile segment-routing srv6 … / encapsulation / traffic-class propagate` → 8000 `segment-routing / srv6 / encapsulation / traffic-class propagate`. Other hw-module srv6 sub-commands dropped.
21. **Ingress H-QoS**: Supported **only** on A100, K100, P100 NPUs with IOS XR ≥26.2.1. Not on Q100/Q200 or older releases — flatten or flag. Egress H-QoS not restricted.
22. **Ingress `service-policy input` — NO priority/bandwidth/police/shape**: Cisco 8000 ingress policy-maps support **marking actions only** (`set` statements). Any `priority level`, `bandwidth`, `police`, or `shape` under a class in an **ingress** policy-map is **NOT supported** and must be removed. The correct translation is:
   - Keep the class-map structure and match criteria intact.
   - Replace the unsupported scheduling action with `set traffic-class N` so the downstream egress queuing policy (Queuing `-Q` policy) can classify correctly. Use the same TC mapping as egress: P1→TC7, P2→TC6, P3→TC5, remaining classes in descending order, class-default→TC0.
   - Document the removal in Section 6 with a note: "Ingress `priority level` is invalid on Cisco 8000; replaced with `set traffic-class` for correct egress scheduling."
   - Example: ASR9K ingress policy with `priority level 1` on QOS-VOICE class → 8000 ingress policy with `set traffic-class 7` on same class.
23. **G.8032 ERP placement**: Ring definition (`ethernet ring g8032 <name>` with port0/port1, instances, aps-channel) MUST be **inside `l2vpn` block**. Profile (`ethernet ring g8032 profile <id>`) stays at **global config level**. `aps-channel` must specify: `level <0-7>`, `port0 interface <intf>.<subif>`, `port1 interface <intf>.<subif>`. Source IOS XE `port0 service instance N` → IOS XR `port0 interface <intf>.N`. These sub-interfaces must also exist as `l2transport` and be in a `bridge-domain` within the same `l2vpn` block.
24. **VFI-backed bridge-domain PW placement**: When an IOS XE `bridge-domain` has `member vfi <NAME>`, the VFI's PW neighbors MUST be placed inside a `vfi <NAME>` block within the IOS XR `bridge-domain` — never as bare `neighbor` statements. Bare `neighbor` under `bridge-domain` creates a P2P pseudowire, not VPLS. Detect both new-style (`l2vpn vfi context` with `member` entries) and legacy (`l2 vfi … manual` with `neighbor` entries). Details in `CONFIG_TRANSLATION_ASR900.md`.

---

## QoS Algorithms Cross-Reference

Rules 6–10 and 17–22 above are operationalized as deterministic algorithms in `CONFIG_TRANSLATION_QOS.md`. Translators MUST follow those algorithms; this file states the rules, the QoS file states the procedure.

| Rule(s) | Stable ID(s) | Operationalized as |
|---|---|---|
| 6 | `RULE_QOS_BW_PERCENT` | §3 NPU × Release Capability Matrix (selects `bandwidth remaining percent` vs `ratio`) |
| 7 | `RULE_NO_WRED` | Algorithm Q4 (WRED substitution) |
| 8 | `RULE_NO_EGRESS_POLICE` | Algorithm Q4 (egress police → shape) |
| 9, 10 | `RULE_QOS_SPLIT_QR`, `RULE_TC_ORDER` | Algorithm Q1 (TC numbering) + Algorithm Q2 (egress dual-policy split) |
| 17 | `RULE_QOSGRP_EXP_EXCL` | §4.2 Marking actions table |
| 18 | `RULE_EXP_EGRESS_TOPMOST` | §4.2 + Algorithm Q2 (`-REMARKING` policy) |
| 19 | `RULE_MATCHALL_DC_BAN` | §4.1 Match criteria table |
| 20 | `RULE_TC_PROPAGATE_RELOC` | per-OS NCS file (`CONFIG_TRANSLATION_NCS.md § QoS Specifics`) |
| 21 | `RULE_INGRESS_HQOS_NPU` | §3 NPU × Release Capability Matrix |
| 22 | `RULE_INGRESS_NO_SCHED` | Algorithm Q3 (ingress sanitization) |

---

## Stable Rule IDs

Use these IDs in directives, validators, and review notes (rule numbers may shift over time; IDs are stable).

| Stable ID | Rule # | Topic |
|---|---|---|
| RULE_NEVER_GUESS | 1 | Mark uncertainty; never invent syntax |
| RULE_INTF_LONGFORM | 2 | IOS XR long-form interface names |
| RULE_EVC_TO_L2T | 3 | service instance → `.N l2transport` |
| RULE_RMAP_TO_RPL | 4 | route-map → route-policy (full RPL body) |
| RULE_IP_TO_AFI | 5 | `ip` → `ipv4`/`ipv6` |
| RULE_QOS_BW_PERCENT | 6 | `bandwidth percent` → `bandwidth remaining percent` |
| RULE_NO_WRED | 7 | drop `random-detect` |
| RULE_NO_EGRESS_POLICE | 8 | egress `police` → `shape average` |
| RULE_QOS_SPLIT_QR | 9 | egress queuing/remarking split (two policies) |
| RULE_TC_ORDER | 10 | TC ordering (P1=TC7, contiguous, class-default=TC0) |
| RULE_INTF_SLOT | 11 | slot renaming dependency on target PID |
| RULE_HW_MODULE | 12 | exclude `hw-module` |
| RULE_LDP_TO_SR | 13 | LDP supported but legacy → SR |
| RULE_PRESERVE_DESC | 14 | preserve descriptions/comments |
| RULE_NO_UNSUPPORTED_S3 | 15 | unsupported never in Section 3 |
| RULE_COMMENT_STYLE | 16 | comment placement |
| RULE_QOSGRP_EXP_EXCL | 17 | qos-group / exp-imposition mutual exclusion |
| RULE_EXP_EGRESS_TOPMOST | 18 | imposition invalid in egress; use `topmost` |
| RULE_MATCHALL_DC_BAN | 19 | match-all qos-group + discard-class banned |
| RULE_TC_PROPAGATE_RELOC | 20 | traffic-class propagate relocation |
| RULE_INGRESS_HQOS_NPU | 21 | ingress H-QoS NPU/release gate |
| RULE_INGRESS_NO_SCHED | 22 | ingress: marking only (no priority/bw/police/shape) |
| RULE_G8032_PLACEMENT | 23 | G.8032 ring under `l2vpn`; profile global; aps-channel complete |
| RULE_VFI_PW_PLACEMENT | 24 | VFI PW inside `vfi` block |

---

## Required Coverage Gate (Pre-Publish, MANDATORY)

A translated report MUST NOT be published if any source marker below is present in the input but its translated counterpart is missing in Section 3. The full parity matrix lives in `CONFIG_TRANSLATION.md § Source Marker → Output Block Parity Matrix`. Summary fail IDs:

| Source Marker | Section 3 Must Contain | Fail ID |
|---|---|---|
| `service instance` | `l2transport` subinterface(s) | COV_EVC |
| `bridge-domain N` (with member) | `l2vpn / bridge group / bridge-domain BD-N` | COV_BD |
| `xconnect` (inline) / `l2vpn xconnect context` | `l2vpn / xconnect group / p2p` | COV_XC |
| `member vfi` / `l2 vfi … manual` | `vfi <NAME>` inside bridge-domain | COV_VFI |
| `ethernet ring g8032 profile <id>` | global `ethernet ring g8032 profile <id>` | COV_G8032_PROF |
| `ethernet ring g8032 <name>` | ring under `l2vpn` with `aps-channel`, `level`, `portN interface <if>.<subif>` | COV_G8032_RING |
| `vrf definition` | `vrf <name>` with AFs and RT (if present in source) | COV_VRF |
| `router bgp` with `address-family` | `router bgp` with each AF, neighbor-group/neighbor, route-policy refs | COV_BGP |
| `route-map NAME` | `route-policy NAME … end-policy` (non-empty body) | COV_RPL |
| `class-map ... match qos-group N` | `class-map type traffic ... match traffic-class N` | COV_QOS_TC |
| `Port-channel N` | `Bundle-Ether N` | COV_LAG |

Action on fail: do NOT publish. Output the failing fail-IDs and the offending source lines; loop back to translation for those stanzas only.

---

## Forbidden Tokens Lint (Section 3, MANDATORY)

The translated config MUST NOT contain any of the following tokens. Match is case-sensitive. Each match is a hard failure; fix the upstream stanza, do not regex-strip.

| Forbidden Token | Lint ID | Reason |
|---|---|---|
| `service instance ` | LINT_SI | EVC syntax invalid in IOS XR |
| `Port-channel` | LINT_LAG | Use `Bundle-Ether` |
| `TenGigabitEthernet` / `HundredGigabitEthernet` / `FortyGigabitEthernet` / `TwentyFiveGigabitEthernet` / `TwoHundredGigabitEthernet` / `FourHundredGigabitEthernet` | LINT_INTF | Use IOS XR short-form (`TenGigE`, `HundredGigE`, `FortyGigE`, `TwentyFiveGigE`, `TwoHundredGigE`, `FourHundredGigE`) |
| `route-map ` | LINT_RMAP | Use `route-policy` |
| ` ip address `, ` ip route `, ` ip access-group ` | LINT_IP | Use `ipv4`/`ipv6` |
| `set qos-group ` in **egress** policy | LINT_QGRP_EGR | `set qos-group` is ingress-only (0–7). Forbidden in egress. |
| `set mpls experimental imposition` (egress) | LINT_EXP_EGR | Use `set mpls experimental topmost N` |
| `random-detect` | LINT_WRED | Not supported on 8000 |
| `bandwidth percent ` (queuing) | LINT_BW_PCT | Use `bandwidth remaining percent` |
| `police ` in egress policy | LINT_POLICE_EGR | Use `shape average` |
| `switchport` | LINT_SWPT | Use `l2transport` subinterfaces |
| `spanning-tree` | LINT_STP | Replace with G.8032/EVPN design |
| `cdp ` | LINT_CDP | IOS XR uses LLDP |

---

## No-Placeholder Gate (Section 3, MANDATORY)

Section 3 MUST NOT contain placeholder/incomplete artifacts. If a true placeholder is unavoidable for a stanza, move that item to Section 5 (Review) with an explicit reason; never leave it inside Section 3.

| Forbidden Pattern | Lint ID |
|---|---|
| `# TODO` / `! TODO` / `TODO:` | NOPH_TODO |
| `pass` as the only RPL body when source had real `route-map` clauses | NOPH_RPL_EMPTY |
| `<REDACTED>` for non-secret fields | NOPH_REDACT_BAD |
| `...` ellipsis in any config line | NOPH_ELLIPSIS |
| `route-policy NAME … end-policy` with empty body and source had non-trivial route-map | NOPH_RPL_BODY |

---

## When to Use This Skill

- ASR 900 / NCS 5xx / NCS 55xx / NCS 57xx / ASR 9K → Cisco 8000 migration
- Config validation for manually converted configs
- Quick snippet translation for lab/POC
