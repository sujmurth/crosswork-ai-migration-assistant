# Config Translation — Reference File

Referenced by `SKILL.md`. Contains OS detection, section list, platform constraints
quick-reference, and the output report template.

---

## OS Detection

### Fingerprint Patterns

| Source OS | Fingerprint Patterns |
|---|---|
| **IOS XE (ASR 900)** | `version 15./16./17.`; `service instance N ethernet`; `bridge-domain N` with `member`; `vrf definition`; `ASR-920/903/907` in inventory |
| **IOS XR — NCS** | `NCS-54/55/57/56` in hostname/version; `hw-module profile tcam`; `interface preconfigure` |
| **IOS XR — ASR9K** | `RP/0/RSP` in prompt; `ASR-9`/`asr9k` in version; `hw-module profile npu`; `controller Fabric` |
| **IOS XR (generic)** | `RP/0/RP0/CPU0:` prompt; `router isis` with nested AF; `l2vpn` with `xconnect group`; RPL syntax |
| **Junos** | Lines start with `set `; `set interfaces`/`set protocols`; `ge-`/`xe-`/`et-`/`ae` |
| **Nokia SR-OS** | `configure router`/`configure service`; `card 1`/`port 1/1/c1`; `epipe`/`vpls`/`vprn` |
| **Huawei VRP** | `sysname`; 3-part slash interfaces; `ip vpn-instance`; `route-policy X permit node Y` |

---

## Sections

| # | Section | IOS XE Markers | IOS XR Markers |
|---|---|---|---|
| 1 | Hostname & Management | `hostname`, NTP, SNMP, logging | `hostname`, `ntp`, `snmp-server` |
| 2 | Physical Interfaces | `interface <type>` | `interface <type>` |
| 3 | Bundle / LAG | `interface Port-channel` | `interface Bundle-Ether` |
| 4 | Sub-interfaces / EVC | `service instance`, dot1q | `.N`, `l2transport` |
| 5 | IS-IS | `router isis` | `router isis` |
| 6 | OSPF | `router ospf` | `router ospf` |
| 7 | BGP | `router bgp` | `router bgp` |
| 8 | MPLS / SR | `mpls ip`, `segment-routing` | `mpls ldp`, `segment-routing` |
| 9 | VRF / L3VPN | `vrf definition`/`ip vrf` | `vrf` |
| 10 | L2VPN — E-Line | `xconnect`, `pseudowire-class` | `l2vpn xconnect group` |
| 11 | L2VPN — E-LAN | `bridge-domain` with `member` | `l2vpn bridge group` |
| 12 | EVPN | `evpn` | `evpn` |
| 13 | Route Policy | `route-map` | `route-policy` |
| 14 | ACL | `ip access-list` | `ipv4 access-list` |
| 15 | QoS | `policy-map`, `class-map` | `policy-map`, `class-map` |
| 16 | Timing / PTP / SyncE | `ptp`, `network-clock` | `ptp`, `frequency synchronization` |
| 17 | BFD | `bfd` | `bfd` |
| 18 | Multicast / mVPN | `ip pim`, `ip igmp` | `router pim`, `router igmp`, `multicast-routing` |
| 19 | VRRP / HSRP | `standby`, `vrrp` | `router vrrp` |
| 20 | Static Routes | `ip route` | `router static` |

---

## Platform Constraints (Quick-Reference)

All rules below are fully defined in `SKILL.md § Critical Rules`. This table is a lookup aid keyed on **stable rule IDs** (immune to renumbering); rule numbers themselves live in `SKILL.md § Stable Rule IDs`.

| Constraint | Stable ID |
|---|---|
| SRv6 preferred over LDP | `RULE_LDP_TO_SR` |
| Interface naming (long-form) | `RULE_INTF_LONGFORM` |
| `ip` → `ipv4`/`ipv6` | `RULE_IP_TO_AFI` |
| route-map → route-policy | `RULE_RMAP_TO_RPL` |
| hw-module excluded | `RULE_HW_MODULE` |
| `bandwidth percent` unsupported | `RULE_QOS_BW_PERCENT` |
| No WRED | `RULE_NO_WRED` |
| No egress policing → shape | `RULE_NO_EGRESS_POLICE` |
| Egress queuing/remarking split (two policies) | `RULE_QOS_SPLIT_QR` |
| TC ordering (P1=TC7 contiguous) | `RULE_TC_ORDER` |
| `set qos-group` / `set mpls exp imposition` exclusion | `RULE_QOSGRP_EXP_EXCL` |
| `set mpls exp imposition` → `topmost` in egress | `RULE_EXP_EGRESS_TOPMOST` |
| `match-all` qos-group+discard-class unsupported | `RULE_MATCHALL_DC_BAN` |
| `traffic-class propagate` relocation | `RULE_TC_PROPAGATE_RELOC` |
| Ingress H-QoS (A100/K100/P100 only, ≥26.2.1) | `RULE_INGRESS_HQOS_NPU` |
| Ingress: marking only (no priority/bw/police/shape) | `RULE_INGRESS_NO_SCHED` |
| G.8032 ring inside `l2vpn`, profile global, aps-channel complete | `RULE_G8032_PLACEMENT` |
| VFI-backed bridge-domain PW placement (vfi wrapper required) | `RULE_VFI_PW_PLACEMENT` |
| No VPLS on 8201/8202 — use EVPN-ELAN | — |
| BFD min 3.3ms (HW-assisted) | — |
| ECMP 64-way (128 on some PIDs) | — |
| G.8032 NPU: A100/K100/P100/Fury(≥24.2.1)/Everest(≥24.4.1); not Q100/Q200 | `RULE_G8032_PLACEMENT` |
| QoS-only conversions: load `CONFIG_TRANSLATION_QOS.md` | — |

---

## Output Template

```
# Config Translation Report
**Source OS:** <detected>
**Target:** Cisco 8000 IOS XR <version>
**Date:** <date>

---

## 1. Source Detection

| Parameter | Value |
|---|---|
| Detected OS | |
| Detection Confidence | High / Medium / Low |
| Fingerprints Matched | |
| Source Platform | |

---

## 2. Sections Detected

| # | Section | Detected | Lines/Stanzas |
|---|---|---|---|
(list only detected sections)

---

## 3. Translated Configuration

(One fenced code block, sections delimited by `! === <Name> ===` banners.
Canonical order: Hostname → VRF → ACL → QoS → Interfaces → Sub-intf →
Bundle → Route-policy → Static → ISIS → OSPF → BGP → MPLS →
Multicast → L2VPN → VRRP → end.
Only detected sections included. No unsupported config.)

---

## 4. Confidence Summary

| Section | Confidence | Notes |
|---|---|---|
(per detected section)

---

## 5. Uncertain / Review-Required Areas

| Source Statement | Issue | Recommendation |
|---|---|---|

---

## 6. Unsupported / No Equivalent Features

| Source Feature | Source Config | Reason | Alternative |
|---|---|---|---|
```

---

## Confidence Level Rules

| Condition | Confidence |
|---|---|
| Exact CLI match, same semantics | High (95%) |
| Functional equivalent, moderate syntax change | High (85%) |
| Equivalent exists, behaviour may differ | Medium (70%) |
| Significant restructuring needed | Medium (60%) |
| No direct equivalent; design decision needed | Low (40%) |
| Not supported on Cisco 8000 | N/A |

## Scale Handling
- Do NOT research scale limits before translating — just generate the config.
- Flag scale concerns as `!! REVIEW:` comments inline in the translated output.
- Only perform scale mapping if the user explicitly asks for it.

## Interface Mapping (XR-to-XR)
- When translating from IOS XR to IOS XR (e.g., NCS 5500 → 8000, ASR 9000 → 8000), do NOT convert interface names unless explicitly asked.
- Preserve original interface names as-is; the user will handle physical mapping separately.
- Interface mapping is only performed for cross-OS translations (e.g., IOS XE → IOS XR, Junos → IOS XR).

## Feature Template Generation
- After a translation is complete, a follow-up user query should be able to produce a **feature template** for the Cisco 8000 target platform.
- The feature template contains one instance of each **unique feature/stanza type** from the translated output (deduplicated), serving as a reusable reference config snippet.
- Template rules:
  - Extract one representative example of each distinct feature (e.g., one VRF, one BD, one xconnect p2p, one ISIS instance, one QoS policy, one G.8032 ring, one BVI with VRRP, etc.)
  - Use placeholder values (e.g., `<VRF_NAME>`, `<VLAN_ID>`, `<NEIGHBOR_IP>`) where instance-specific data would go.
  - Preserve the full structural hierarchy and all required sub-stanzas.
  - Group by section with `! === <Section> ===` banners.
  - Include inline comments explaining mandatory vs optional knobs.

---

## Source Marker → Output Block Parity Matrix (Pre-Publish, MANDATORY)

This matrix is the single source of truth for whether a translation is publishable. Each detected source marker has a required corresponding output block in Section 3. Run the check after assembling Section 3 and before emitting the report.

| Source Marker (regex/pattern) | Required Output Block in Section 3 | Mandatory Fields | Fail ID |
|---|---|---|---|
| `service instance N ethernet` | `interface <if>.N l2transport` | `encapsulation` (untagged or `dot1q ...`); copy `rewrite ingress tag pop ... symmetric` if present | COV_EVC |
| `bridge-domain N` (referenced as member) | `bridge-domain BD-N` under `l2vpn / bridge group <NAME>` | one or more `interface <if>.<sub>` lines | COV_BD |
| `l2vpn xconnect context NAME` or inline `xconnect A.B.C.D PWID encapsulation mpls` | `xconnect group <NAME> / p2p <NAME>` | `neighbor <ip> pw-id <id>` for each member | COV_XC |
| `bridge-domain` with `member vfi NAME` (or `l2 vfi NAME manual`) | `vfi NAME` inside `bridge-domain BD-N` | `vpn id <N>`, one or more `neighbor <ip> pw-id <id>` | COV_VFI |
| `ethernet ring g8032 profile <id>` | global `ethernet ring g8032 profile <id>` | preserve timers/knobs verbatim | COV_G8032_PROF |
| `ethernet ring g8032 <name>` | `ethernet ring g8032 <name>` under `l2vpn` | `port0 interface <xr-if>`, `port1 ...`, `instance N` with `aps-channel`, `level`, `portN interface <xr-if>.<subif>` | COV_G8032_RING |
| `vrf definition <name>` | `vrf <name>` | `address-family ipv4|ipv6 unicast` and `route-target import/export` (if present in source) | COV_VRF |
| `router bgp <asn>` | `router bgp <asn>` | `bgp router-id`, neighbor-group(s), neighbors with `use neighbor-group`, AF blocks, route-policy refs | COV_BGP |
| `address-family ipv4|ipv6 vrf <name>` (BGP) | `vrf <name> / address-family ... unicast` under `router bgp` | per-neighbor activation/policy | COV_BGP_VRF |
| `route-map <name> ...` | `route-policy <name> ... end-policy` | non-empty body translating each source clause | COV_RPL |
| `class-map ... match qos-group N` | `class-map type traffic ... match traffic-class N` | TC mapped per `RULE_TC_ORDER` | COV_QOS_TC |
| `policy-map ... egress` with `set qos-group` | egress `policy-map` using `set traffic-class` | no `set qos-group` retained | COV_QOS_EGR |
| `Port-channel N` | `Bundle-Ether N` | preserve member interface bindings | COV_LAG |
| `interface <Long-form-Eth>` (TenGigabitEthernet, HundredGigabitEthernet, ...) | XR short-form (`TenGigE`, `HundredGigE`, ...) | per `RULE_INTF_LONGFORM` and Algorithm A in `CONFIG_TRANSLATION_ASR900.md` | COV_INTF |
| ingress `policy-map` with `priority`/`bandwidth*`/`police`/`shape` (any source OS) | ingress policy-map with `set traffic-class N` (marking-only) | every removed scheduling line produces exactly one `set traffic-class` line per `RULE_INGRESS_NO_SCHED` and Algorithm Q3 in `CONFIG_TRANSLATION_QOS.md` | COV_QOS_ING |

Source markers not in this table fall under best-effort coverage; still report any structural mapping uncertainty in Section 5.

---

## Pre-Publish Checklist (MANDATORY, run in order)

Publish ONLY if all four gates pass.

1. **Coverage Gate** — Run the parity matrix above. Any missing required output block is a hard fail (emit COV_* IDs with offending source lines).
2. **Forbidden Token Lint** — Run `SKILL.md § Forbidden Tokens Lint`. Any match in Section 3 is a hard fail (emit LINT_* IDs).
3. **No-Placeholder Gate** — Run `SKILL.md § No-Placeholder Gate`. Any match in Section 3 is a hard fail (emit NOPH_* IDs).
4. **Section Layout Check** — Section 3 is a single fenced block; canonical banner order from Output Template is preserved; every removed feature is documented in Section 6; review-only items are in Section 5.

On any failure: do NOT publish. Output the failing IDs and the offending lines, then loop back to translation for those specific stanzas only — never delete a flagged source feature to make the gate pass.
