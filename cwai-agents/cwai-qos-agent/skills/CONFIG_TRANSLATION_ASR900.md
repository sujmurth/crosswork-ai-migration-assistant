# ASR 900 (IOS XE) → Cisco 8000 (IOS XR)

> **Load order:** Use with `SKILL.md` + `CONFIG_TRANSLATION.md`. For QoS conversion, also load `CONFIG_TRANSLATION_QOS.md`. This file uses **stable rule IDs** (`RULE_*`) defined in `SKILL.md § Stable Rule IDs` — never numeric rule numbers (which may shift).

## Key Differences

| Aspect | ASR 900 | Cisco 8000 |
|---|---|---|
| CLI model | Monolithic | Hierarchical (modular) |
| Interface model | Physical + `service instance` (EVC) | Sub-interfaces with `l2transport` |
| L2VPN | Inline `xconnect`; `bridge-domain` with `member` | `l2vpn xconnect group` / `l2vpn bridge group` |
| IP keyword | `ip` | `ipv4` / `ipv6` |
| VRF | `vrf definition` | `vrf` |
| Route policy | `route-map` | `route-policy` (RPL) |
| BGP AF | `neighbor activate` under AF | Explicit AF per neighbor |
| MPLS | `mpls ip` per interface | `mpls ldp` block + `interface` sub-block |

---

## Interface Mapping

| ASR 900 | Cisco 8000 IOS XR | Notes |
|---|---|---|
| `GigabitEthernet0/0/N` | `GigE0/0/0/N` or `TwentyFiveGigE0/0/0/N` | Depends on target PID port type |
| `TenGigabitEthernet0/0/N` | `TenGigE0/0/0/N` or `TwentyFiveGigE0/0/0/N` | SFP28 ports run at 10G |
| `Port-channel N` | `Bundle-Ether N` | |
| `Loopback N` | `Loopback N` | |
| `GigabitEthernet0` / `Management0` | `MgmtEth0/RP0/CPU0/0` | |

> Verify target 8000 PID port layout before finalizing mapping.

---

## EVC → L2transport Conversion (`RULE_EVC_TO_L2T`)

| IOS XE | IOS XR |
|---|---|
| `service instance N ethernet` | `interface <intf>.N l2transport` |
| `encapsulation dot1q N` | `encapsulation dot1q N` |
| `rewrite ingress tag pop 1 symmetric` | `rewrite ingress tag pop 1 symmetric` |
| `xconnect A.B.C.D PWID encapsulation mpls` | `l2vpn / xconnect group / p2p / neighbor ipv4 A.B.C.D pw-id PWID` |
| `bridge-domain N / member <intf> service-instance M` | `l2vpn / bridge group / bridge-domain / interface <intf>.M` |

---

## VFI-backed Bridge-Domain Conversion (`RULE_VFI_PW_PLACEMENT`)

When an IOS XE bridge-domain has **both** an AC member (`member <intf> service-instance M`)
and a VFI member (`member vfi VFIN`), the VFI's PW neighbors **must** be wrapped in a
`vfi` block inside the IOS XR `bridge-domain` — never as bare `neighbor` statements.

A bare `neighbor` under `bridge-domain` (without `vfi`) creates a **point-to-point
pseudowire**, not VPLS. The `vfi` wrapper is required to preserve the VPLS/E-LAN
flooding and MAC-learning semantics from the source.

### When to use `vfi` vs bare `neighbor` vs `xconnect group`

| IOS XE Source Pattern | IOS XR Translation | Why |
|---|---|---|
| SI with inline `xconnect` (no bridge-domain) | `l2vpn / xconnect group / p2p` — **no** `vfi` | Point-to-point E-Line; no bridge-domain involved |
| `bridge-domain N / member <intf> SI / member vfi VFIN` | `l2vpn / bridge group / bridge-domain / vfi / neighbor` — **with** `vfi` | E-LAN / VPLS; VFI provides flooding domain |
| `bridge-domain N / member <intf> SI` (local only, no VFI) | `l2vpn / bridge group / bridge-domain / interface` — no PW at all | Local switching; no remote peer |

### IOS XE Source (two VFI styles)

**New-style (`l2vpn vfi context`):**
```
l2vpn vfi context VFI1490
 vpn id 1490
 member 100.111.93.2 encapsulation mpls
!
bridge-domain 1490
 member GigabitEthernet0/0/3 service-instance 1490
 member vfi VFI1490
!
```

**Legacy-style (`l2 vfi … manual`):**
```
l2 vfi VFI1426 manual
 vpn id 1426
 bridge-domain 1426
 neighbor 100.111.93.1 encapsulation mpls
!
```
(Legacy style binds bridge-domain inside the VFI definition; the AC is the
service-instance on the physical interface that references the same bridge-domain
number.)

### Correct IOS XR Translation

```
l2vpn
 bridge group <GROUP>
  bridge-domain BD-1490
   interface TwentyFiveGigE0/0/0/3.1490
   !
   vfi VFI1490
    vpn id 1490
    neighbor 100.111.93.2 pw-id 1490
    !
   !
  !
```

### Incorrect (do NOT produce this)

```
l2vpn
 bridge group <GROUP>
  bridge-domain BD-1490
   interface TwentyFiveGigE0/0/0/3.1490
   !
   neighbor 100.111.93.2 pw-id 1490     ← WRONG: bare neighbor = P2P PW, not VPLS
   !
  !
```

### Detection & Translation Steps

1. **Detect VFI association**: scan for `member vfi <NAME>` in `bridge-domain`
   blocks, or `bridge-domain N` inside `l2 vfi … manual` blocks.
2. **Collect VFI PW neighbors**: from `member A.B.C.D encapsulation mpls`
   (new-style) or `neighbor A.B.C.D encapsulation mpls` (legacy-style).
3. **In IOS XR output**: place those PW neighbors **inside a `vfi <NAME>` block**
   within the `bridge-domain` stanza under `l2vpn / bridge group`.
4. **Preserve `vpn id`**: if the source VFI had a `vpn id`, include it as
   `vpn id` inside the `vfi` block.
5. **Preserve VFI name**: use the source VFI name for traceability.
6. **Multiple members**: if a VFI has multiple `member`/`neighbor` entries,
   all go inside the same `vfi` block.

---

## Unsupported Features

| ASR 900 Feature | Alternative on 8000 |
|---|---|
| `spanning-tree` | Remove — design L2 without STP (G.8032, EVPN) |
| `switchport` | `l2transport` sub-interfaces |
| `ip dhcp pool` | External DHCP server |
| `interface Vlan N` (SVI) | `interface BVI N` |
| `cdp enable` | `lldp` (IOS XR doesn't support CDP) |
| `vlan database` | Remove |
| `ip dhcp snooping` / `device-tracking` | Remove |
| `no ip proxy-arp` | Default on IOS XR (no action) |

---

## Deterministic Conversion Algorithms (MANDATORY)

Generators MUST implement these algorithms stanza-by-stanza on a parsed model — NOT via free-form regex rewriting of full text. Free-form rewrites cannot enforce structural rules (G.8032 placement, VFI wrapping, BGP AF activation, QoS split) and have produced repeated regressions.

### A. Interface Name Normalization (RULE_INTF_LONGFORM)

Apply this map globally as the LAST step before emitting Section 3 (so any internally generated string is also normalized). Slot/port suffix is preserved unchanged.

| Source token | Target token |
|---|---|
| `Port-channel` | `Bundle-Ether` |
| `TenGigabitEthernet` | `TenGigE` |
| `HundredGigabitEthernet` | `HundredGigE` |
| `FortyGigabitEthernet` | `FortyGigE` |
| `TwentyFiveGigabitEthernet` / `TwentyFiveGigEthernet` | `TwentyFiveGigE` |
| `TwoHundredGigabitEthernet` | `TwoHundredGigE` |
| `FourHundredGigabitEthernet` | `FourHundredGigE` |
| `GigabitEthernet0` (mgmt port only) | `MgmtEth0/RP0/CPU0/0` |

Post-condition: re-run `LINT_INTF` and `LINT_LAG` on Section 3. Any residual long-form token = hard fail.

### B. G.8032 Conversion Algorithm (RULE_G8032_PLACEMENT)

Inputs: source stanzas `ethernet ring g8032 profile <id>` and `ethernet ring g8032 <name>`.

Steps:
1. Emit profile globally; preserve timers and inner knobs verbatim.
2. Emit ring under `l2vpn` (NOT global). Include `open-ring`, `exclusion-list vlan-ids ...`, `port0 interface <xr-if>` (apply Algorithm A), `port1 interface <xr-if>` or `port1 none`.
3. For each `instance N`:
   - Preserve `description`, `profile <id>`, `rpl ...`, `inclusion-list vlan-ids ...`.
   - Emit `aps-channel` with `level <0-7>` (default `level 7` if source did not specify).
   - Convert `port0 service instance M` → `port0 interface <xr-if>.M`. Same for `port1`.
   - If source has `port1 none` or no port1 service instance, emit `port1 interface none`.
4. Ensure each APS sub-interface (`<xr-if>.M`) ALSO exists as `interface <xr-if>.M l2transport` and is a member of the bridge-domain whose number matches the source `bridge-domain` of that SI; if missing, fail `COV_G8032_RING`.

### C. Route-Map → Route-Policy Completion (RULE_RMAP_TO_RPL)

For every IOS XE `route-map NAME [permit|deny] SEQ` block:
1. Emit `route-policy NAME` ... `end-policy` with a non-empty body.
2. Translate clauses; supported mappings:
   - `match ip address prefix-list X` → `if destination in X then`
   - `match community X` → `if community matches-any X then`
   - `set local-preference N` → `set local-preference N`
   - `set as-path prepend X` → `prepend as-path X`
   - `set community X additive` → `set community X additive`
   - Multiple sequences → `if ... then ... elseif ... then ... else pass endif`.
3. Translate referenced `ip prefix-list` / `ip community-list` to IOS XR equivalents in the same Section 3 (under Route Policy).
4. Reject `pass`-only or empty bodies if the source route-map had any non-trivial clause (`NOPH_RPL_BODY`).

### D. Stanza-Aware Translation (REQUIRED METHODOLOGY)

Translation MUST be stanza-aware: parse each source stanza into an intermediate model (interfaces, sub-interfaces, BGP AFs, QoS class-maps/policy-maps, G.8032 rings, VFI bridge-domains), then emit the IOS XR stanza from the model. Free-form text rewrites are not permitted as the primary translation method. Final post-processing may apply only Algorithm A (interface name normalization) and redaction; structural decisions must come from the model.

---

## QoS Specifics

EVC-attached service-policies (`service-policy {input|output}` under a `service instance`) translate to `service-policy {input|output}` under the IOS XR `l2transport` sub-interface created by Algorithm A — never under the main interface. All other QoS handling (egress dual-policy split, ingress sanitization, TC numbering, WRED/police substitution, NPU gating) is in `CONFIG_TRANSLATION_QOS.md` Algorithms Q1–Q4. The structural rules in this file (sub-interface creation, `l2transport`, encapsulation/rewrite preservation) take precedence over QoS body content.
