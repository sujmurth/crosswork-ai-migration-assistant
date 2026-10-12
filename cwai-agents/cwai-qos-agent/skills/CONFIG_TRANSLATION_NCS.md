# NCS 540/55xx/57xx (IOS XR) → Cisco 8000 (IOS XR)

> **Load order:** Use with `SKILL.md` + `CONFIG_TRANSLATION.md`. For QoS conversion, also load `CONFIG_TRANSLATION_QOS.md`. This file uses **stable rule IDs** (`RULE_*`) defined in `SKILL.md § Stable Rule IDs`.

## Key Differences

Both run IOS XR — most config is directly portable. Differences are in hw-module,
interface port assignments, and NPU capabilities.

| Aspect | NCS (Source) | Cisco 8000 (Target) |
|---|---|---|
| NPU | Broadcom Jericho/J+/J2 | Cisco Silicon One |
| hw-module | NCS-specific TCAM/NPU profiles | 8000-specific (exclude per `RULE_HW_MODULE`) |
| SRv6 | Supported | Preferred / native |
| Form factor | NCS 540 fixed; 55xx/57xx modular | Fixed (820x) or modular (86xx/88xx) |

---

## Interface Mapping

NCS 540 (fixed): `0/0/0/N` → same on 8000 fixed. Verify port count per PID.
NCS 55xx/57xx (modular): `0/<LC>/0/N` → `0/<LC>/0/N` on 8000 modular. Renumber if LC plan changes.

| NCS Interface | 8000 Equivalent | Notes |
|---|---|---|
| `TwentyFiveGigE0/0/0/N` | `TwentyFiveGigE0/0/0/N` | Same if target has 25G |
| `HundredGigE0/0/0/N` | `HundredGigE0/0/0/N` | Same |
| `TenGigE0/0/0/N` | `TenGigE0/0/0/N` | May need breakout on 100G-only LC |
| `Bundle-Ether N` | `Bundle-Ether N` | Same |
| `MgmtEth0/RP0/CPU0/0` | `MgmtEth0/RP0/CPU0/0` | Same |

---

## Features Not Available / Changed

| NCS Feature | Status on 8000 | Action |
|---|---|---|
| `hw-module profile tcam` | Different NPU namespace | Remove (`RULE_HW_MODULE`) |
| `hw-module profile segment-routing srv6` | Not needed — relocate sub-commands | `RULE_TC_PROPAGATE_RELOC` (move `traffic-class propagate` to `segment-routing / srv6 / encapsulation`; drop other srv6 hw-module sub-commands) |
| `hw-module profile bgp-pic edge` | Default on 8000 | Remove |
| VPLS on NCS 540 | Not on 8201/8202 | Use EVPN-ELAN |
| NCS 540 built-in GNSS | Not all 8000 PIDs have GNSS | Verify HW |
| `bandwidth percent` | See `RULE_QOS_BW_PERCENT` | Convert per NPU/release |

---

## QoS Specifics

NCS source QoS converges on the same Cisco 8000 target rules as XE/9K sources. NCS-specific notes:

- **`hw-module profile qos-*` knobs** — drop and document under `RULE_HW_MODULE`; QoS profile selection on 8000 is implicit (VOQ model).
- **`traffic-class propagate`** under SRv6 hw-module → relocate to `segment-routing / srv6 / encapsulation / traffic-class propagate` per `RULE_TC_PROPAGATE_RELOC`.
- **NPU gating** — Jericho/J+/J2 is the *source* NPU; the *target* 8000 NPU drives `bandwidth remaining percent` vs `ratio` and ingress H-QoS availability — see `CONFIG_TRANSLATION_QOS.md` §3.
- All translation procedures: Algorithms Q1 (TC numbering), Q2 (egress dual-policy split), Q3 (ingress sanitization), Q4 (WRED / egress-police substitution) in `CONFIG_TRANSLATION_QOS.md`.
