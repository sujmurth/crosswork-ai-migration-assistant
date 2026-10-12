# ASR 9K (IOS XR) → Cisco 8000 (IOS XR)

> **Load order:** Use with `SKILL.md` + `CONFIG_TRANSLATION.md`. For QoS conversion, also load `CONFIG_TRANSLATION_QOS.md`. This file uses **stable rule IDs** (`RULE_*`) defined in `SKILL.md § Stable Rule IDs`.

## Key Differences

Both run IOS XR — most config is directly portable. Key differences:
chassis architecture (RSP→RP), hw-module, interface slots, NPU.

| Aspect | ASR 9K | Cisco 8000 |
|---|---|---|
| Route Processor | RSP (`RP/0/RSP0/CPU0:`) | RP0 (`RP/0/RP0/CPU0:`) |
| NPU | Lightspeed/Tomahawk | Cisco Silicon One |
| Location syntax | `0/RSP0/CPU0` | `0/RP0/CPU0` |
| Fabric | Crossbar (modular) | Centralized NPU (no crossbar) |
| MPLS TE | RSVP-TE (mature) | SR-TE preferred; RSVP-TE supported |

---

## Interface Mapping

ASR9K: `<type> 0/<SLOT>/<SUBSLOT>/<PORT>` (SUBSLOT usually 0).
8000 fixed: `<type> 0/0/0/<PORT>`. 8000 modular: `<type> 0/<LC>/0/<PORT>`.

| ASR 9K | 8000 Modular | 8000 Fixed | Notes |
|---|---|---|---|
| `TenGigE 0/SLOT/0/N` | `TenGigE 0/LC/0/N` | `TenGigE 0/0/0/N` | Renumber slot |
| `HundredGigE 0/SLOT/0/N` | `HundredGigE 0/LC/0/N` | `HundredGigE 0/0/0/N` | |
| `FortyGigE 0/SLOT/0/N` | `HundredGigE 0/LC/0/N` | `HundredGigE 0/0/0/N` | 40G→100G |
| `Bundle-Ether N` | `Bundle-Ether N` | `Bundle-Ether N` | |
| `MgmtEth0/RSP0/CPU0/0` | `MgmtEth0/RP0/CPU0/0` | `MgmtEth0/RP0/CPU0/0` | RSP→RP0 |

> Always confirm target 8000 PID and LC slot plan before finalizing.

---

## Features Not Available / Changed

| ASR 9K Feature | Status on 8000 | Action |
|---|---|---|
| `controller Fabric` | Not applicable (no crossbar) | Remove |
| `hw-module profile npu` | Different NPU | Remove (`RULE_HW_MODULE`) |
| RSP commands (`admin hw-module location 0/RSP0`) | RP-based | Adapt to `0/RP0/CPU0` |
| `FortyGigE` | No 40G on 8000 | Migrate to 100G or 10G breakout |
| VPLS on 8201/8202 | Not supported | Use EVPN-ELAN |
| `bandwidth percent` | See `RULE_QOS_BW_PERCENT` | Convert per NPU/release |
| RSVP-TE (sole TE) | Supported but legacy | Recommend SR-TE (`RULE_LDP_TO_SR`) |
| OC-3/12/48 (TDM/SONET) | Not supported | Out of scope |

---

## QoS Specifics

ASR 9K source QoS converges on the same Cisco 8000 target rules as XE/NCS sources. ASR9K-specific notes:

- **Bundle main-interface vs member-link policies** — apply policy on the `Bundle-Ether N` main; do not carry forward per-member policies (8000 supports main-interface attachment only for bundles).
- **NPU gating** — see `CONFIG_TRANSLATION_QOS.md` §3 (NPU × Release Capability Matrix) for `bandwidth remaining percent` vs `ratio` selection and ingress H-QoS availability based on the **target** 8000 NPU.
- All translation procedures: Algorithms Q1 (TC numbering), Q2 (egress dual-policy split), Q3 (ingress sanitization), Q4 (WRED / egress-police substitution) in `CONFIG_TRANSLATION_QOS.md`.
