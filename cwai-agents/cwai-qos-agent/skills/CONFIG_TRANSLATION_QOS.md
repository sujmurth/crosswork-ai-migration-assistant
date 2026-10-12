# QoS Translation — Critical-Path Reference (Cisco 8000 IOS XR)

> **Purpose:** Single, target-rule-converged reference for converting QoS
> (class-maps / policy-maps / service-policies) from any supported source
> platform — ASR 900 (IOS XE), ASR 9000 (IOS XR-classic), NCS 540/55xx/57xx
> (IOS XR-classic) — into Cisco 8000 IOS XR ≥26.2.1.
>
> This file is **purely additive**. Authoritative rule/lint/coverage IDs live
> in `SKILL.md`; every section here references those IDs and never invents
> a new RULE_* number or renumbers an existing one. New IDs introduced here
> are only **lint** (`LINT_*_ING`) and **coverage** (`COV_QOS_ING`) entries
> that fill genuine gaps (ingress sanitization).

---

## 1. Scope & Invocation

### When to load this file

| Request shape | Files to load |
|---|---|
| End-to-end config translation (any OS) | `SKILL.md` + `CONFIG_TRANSLATION.md` + per-OS file (+ this file) |
| **QoS-only** stanza (one or more `policy-map` / `class-map` / `service-policy` blocks) | `SKILL.md` + **this file only** |
| End-to-end where QoS is one of many sections | per-OS file is primary; consult this file for any QoS-specific decision |

### Authority chain

1. **Stable IDs** (`RULE_*`, `LINT_*`, `COV_*`) are defined in `SKILL.md` and are immutable.
2. This file **expands** those IDs with examples, source-OS mapping tables, and deterministic algorithms.
3. If this file appears to conflict with `SKILL.md`, `SKILL.md` wins.

### Cross-references (existing IDs from `SKILL.md`, do not redefine)

`RULE_QOS_BW_PERCENT` (rule 6), `RULE_NO_WRED` (7), `RULE_QOS_SPLIT_QR` (9),
`RULE_QOSGRP_EXP_EXCL` (17), `RULE_MATCHALL_DC_BAN` (19),
`RULE_TC_PROPAGATE_RELOC` (20), `RULE_INGRESS_HQOS_NPU` (21),
`RULE_INGRESS_NO_SCHED` (22). Lints: `LINT_QGRP_EGR`, `LINT_WRED`,
`LINT_BW_PCT`, `LINT_EXP_EGR`, `LINT_POLICE_EGR`, `LINT_SHAPE_PCT_OK`.
Coverage: `COV_QOS_TC`, `COV_QOS_EGR`.

### New IDs introduced by this file (additive only)

| New ID | Type | Scope | Defined in |
|---|---|---|---|
| `LINT_BW_ING` | Forbidden token | Ingress policy-map | §7 Algorithm Q3 |
| `LINT_PRIO_ING` | Forbidden token | Ingress policy-map | §7 Algorithm Q3 |
| `LINT_POLICE_ING_SCHED` | Forbidden token | Ingress policy-map (police as scheduler) | §7 Algorithm Q3 |
| `LINT_SHAPE_ING` | Forbidden token | Ingress policy-map | §7 Algorithm Q3 |
| `COV_QOS_ING` | Coverage | Ingress sanitization completeness | §9 Pre-Publish Gates |

These extend (do not replace) `RULE_INGRESS_NO_SCHED` (rule 22) for machine-checkable enforcement.

---

## 2. 8000 QoS Model Summary

Cisco 8000 platforms operate in one of two QoS modes depending on platform
and configuration:

### 2.1 VOQ Mode (Default — all platforms unless EFC configured)

- **Ingress: marking + TC assignment.** Policy-maps applied with
  `service-policy input` set `traffic-class` (from MPLS EXP / DSCP / ACL)
  so egress queuing can classify. Also supports `set dscp`, `set cos`,
  `set mpls experimental imposition`, `set discard-class`. **No** `priority`,
  `bandwidth`, `police` (as scheduler), or `shape`. Police as a *marker*
  (`police … conform-action transmit exceed-action set-…-transmit`) is
  retained only when conform/exceed produce marking, not scheduling.
- **Egress queuing (`-Q` policy): `match traffic-class` only.** Egress
  queuing policies must not match on DSCP/EXP/CoS/ACL. TC is set by the
  **ingress** policy.
- **Egress policy split — conditional:** Required ONLY when the source
  egress policy mixes queuing + remarking actions (e.g., `set dscp`
  alongside `bandwidth`/`priority`). In that case, split into:
  - `-REMARKING`: remarking actions only (`set dscp`, `set cos`,
    `set mpls experimental topmost`). Does **NOT** need `set traffic-class`
    (ingress already handles TC assignment).
  - `-Q`: `match traffic-class`, scheduling actions only.
  - Apply both `service-policy output` on same interface.
- **If source egress has queuing only (no remarking):** Single `-Q` policy
  is sufficient. No egress `-REMARKING` needed.
- **No WRED** (`RULE_NO_WRED`): drop `random-detect` everywhere; substitute
  `queue-limit` or document Dual-Queue-Limit if specified.
- **No egress policing as scheduler**: convert to `shape average …`
  (`LINT_POLICE_EGR`).
- **`set qos-group`**: Supported on **ingress only** (values 0–7).
  Cannot coexist with `set dscp`, `set precedence`, or
  `set mpls experimental` in the same class (`RULE_QOSGRP_COEXIST_BAN`).
  In VOQ mode, egress policies **cannot** `match qos-group` — egress
  queuing matches `traffic-class` only. Therefore ingress **must** also
  `set traffic-class` for egress scheduling to work. `set qos-group` on
  ingress in VOQ mode is retained only if the source used it and another
  consumer on the node needs it.
- **`set qos-group` forbidden in egress** (`LINT_QGRP_EGR`): No egress
  policy-map (neither `-REMARKING` nor `-Q`) may contain `set qos-group`.

### 2.2 ETM/EFC Mode (Explicit configuration required)

Applies to:
- **All K100 Line Cards**
- **Select P100 Line Cards** (EFC-capable)
- **K100 Fixed Platforms** (configurable to ETM/EFC mode)
- **K100 Centralized Platforms** (configurable to ETM/EFC mode)

- **Egress `-REMARKING` policy is MANDATORY** and MUST include
  `set traffic-class N` for each class. This is what feeds the egress
  queuing policy (NOT the ingress policy).
- If the source egress policy also had other remarking actions
  (`set dscp`, `set cos`, `set mpls experimental topmost`), those go
  into the `-REMARKING` policy **alongside** `set traffic-class N`.
- **Egress `-Q` policy:** Matches `traffic-class` (set by the egress
  `-REMARKING` policy). Scheduling actions only.
- **Both** `-REMARKING` and `-Q` applied `service-policy output` on same
  interface.
- Ingress policy is optional for egress TC assignment (egress
  `-REMARKING` handles TC). However, ingress `set qos-group` (0–7) IS
  supported and can be used as a **classification criterion** in the egress
  `-REMARKING` policy via `match qos-group N`. This enables the flow:
  ingress `set qos-group N` → egress `-REMARKING` `match qos-group N` +
  `set traffic-class N` → egress `-Q` `match traffic-class N`.
  Same mutual-exclusion rule applies: `set qos-group` cannot coexist with
  `set dscp` / `set precedence` / `set mpls experimental` in the same class.
- **No WRED**, **no egress policing as scheduler** — same as VOQ mode.
- **`set qos-group` forbidden in egress** (`LINT_QGRP_EGR`): No egress
  policy-map may contain `set qos-group`. It may only appear in ingress.

---

## 3. NPU × Release Capability Matrix (MANDATORY pre-translation gate)

Identify the **target NPU family** from PID before emitting any QoS block.
This drives action selection and is a hard gate. ASIC→PID mapping is in
`expert/docs/8000_internal_code_names.md` (load via `rag_search` if unknown).

| Capability | Q100 (Pacific) | Q200 (Gibraltar) | A100 (Argon) | K100 (Krypton) | P100 (Palladium) | G100 (Graphene) |
|---|---|---|---|---|---|---|
| Direct `bandwidth percent N` support | **no** | **no** | **no** | **no** | **no** | **no** |
| Direct `bandwidth N` support in QoS scheduling classes | **no** | **no** | **no** | **no** | **no** | **no** |
| `bandwidth remaining percent` (≥26.2.1) | ratio only | ratio only | yes | yes | yes | yes |
| `bandwidth remaining ratio K` (K≤63) | yes | yes | yes (fallback) | yes (fallback) | yes (fallback) | yes (fallback) |
| **Ingress H-QoS** | **no** | **no** | **yes** | **yes** | **yes** | yes |
| Egress H-QoS | yes | yes | yes | yes | yes | yes |
| `match traffic-class` (egress queuing) | yes | yes | yes | yes | yes | yes |
| `set traffic-class` (ingress + egress remark) | yes | yes | yes | yes | yes | yes |
| `set qos-group` (ingress only, 0–7) | **yes** | **yes** | **yes** | **yes** | **yes** | **yes** |
| WRED / `random-detect` | **no** | **no** | **no** | **no** | **no** | **no** |
| Egress `police` as scheduler | **no** | **no** | **no** | **no** | **no** | **no** |
| Dual-Queue-Limit | yes | yes | yes | yes | yes | yes |
| `traffic-class propagate` (SRv6) | n/a | n/a | yes | yes | yes | yes |
| **Default mode** | VOQ | VOQ | VOQ | VOQ | VOQ | VOQ |
| **ETM/EFC mode available** | **no** | **no** | **no** | **yes** (all LCs, fixed, centralized) | **yes** (select LCs) | **no** |
| Egress `-REMARKING` mandatory (VOQ) | no* | no* | no* | no* | no* | no* |
| Egress `-REMARKING` mandatory (ETM/EFC) | n/a | n/a | n/a | **yes** | **yes** | n/a |
| `set traffic-class` required in egress `-REMARKING` (ETM/EFC) | n/a | n/a | n/a | **yes** | **yes** | n/a |

\* In VOQ mode, egress `-REMARKING` is required only when source policy mixes
queuing + remarking actions. When required, it carries remarking actions only
(`set dscp`/`set cos`/`set mpls exp topmost`) — NOT `set traffic-class`.

**Decision points:**

- If target NPU is **Q100/Q200** → emit `bandwidth remaining ratio K`
  (`K = round(percent / 100 * 63)`, max 63). Reject `bandwidth remaining percent`.
- If target NPU is **A100/K100/P100/G100** AND release ≥26.2.1 → emit
  `bandwidth remaining percent` (preferred).
- Direct source actions `bandwidth percent N` and absolute `bandwidth N` are
  **not valid direct Cisco 8000 scheduler syntax**. Always convert them to
  `bandwidth remaining percent` or `bandwidth remaining ratio` per the gate
  above.
- Ingress H-QoS requested on **Q100/Q200** → flatten to a single ingress
  policy and document the loss in Section 6 (`RULE_INGRESS_HQOS_NPU`).
- Release < 26.2.1 → fall back to `bandwidth remaining ratio` regardless of NPU.

If NPU cannot be determined from PID, **stop and ask** — do not guess.

---

## 4. Source → Target Mapping Tables

Each row carries the originating stable ID. Target column is **the same** for
all source OS; only the input differs. This is why a single QoS file beats
per-OS QoS sections.

### 4.1 Match criteria

| Source token (XE 900) | Source token (XR-classic 9K) | Source token (XR-classic NCS) | 8000 IOS XR | Stable ID |
|---|---|---|---|---|
| `match cos N` | `match cos N` | `match cos N` | `match cos N` (ingress only) | — |
| `match dscp NAME\|N` | `match dscp NAME\|N` | `match dscp NAME\|N` | `match dscp NAME\|N` | — |
| `match precedence N` | `match precedence N` | `match precedence N` | `match precedence N` | — |
| `match mpls experimental topmost N` | `match mpls experimental topmost N` | `match mpls experimental topmost N` | `match mpls experimental topmost N` | — |
| `match qos-group N` | `match qos-group N` | `match qos-group N` | `match traffic-class N` (egress queuing) | `COV_QOS_TC` |
| `match access-group name ACL` | `match access-group name ACL` | `match access-group ipv4 ACL` | `match access-group ipv4 ACL` | — |
| `match-all` (qos-group + discard-class) | `match-all` (qos-group + discard-class) | `match-all` (qos-group + discard-class) | **collapse to `match qos-group N`** (CONFORM value) | `RULE_MATCHALL_DC_BAN` |
| `match vlan N` | n/a | n/a | move to sub-interface `encapsulation dot1q N` | — |

### 4.2 Marking actions

| Source action | 8000 IOS XR (ingress) | 8000 IOS XR (egress remarking) | Stable ID |
|---|---|---|---|
| `set qos-group N` | Keep `set qos-group N` (0–7) if no `set dscp`/`set precedence`/`set mpls exp` coexists; **also** add `set traffic-class N` in VOQ mode (required for egress queuing). In EFC mode, `set qos-group` alone is sufficient if egress `-REMARKING` will `match qos-group`. | **forbidden** — `set qos-group` is ingress-only (`LINT_QGRP_EGR`) | `LINT_QGRP_EGR` (egress only) |
| `set dscp NAME\|N` | `set dscp NAME\|N` | `set dscp NAME\|N` | — |
| `set cos N` | `set cos N` | `set cos N` (egress L2 only) | — |
| `set mpls experimental imposition N` | `set mpls experimental imposition N` (ingress) | **drop**; use `set mpls experimental topmost N` | `LINT_EXP_EGR` |
| `set mpls experimental topmost N` | n/a (cannot mark imposed label at ingress this way) | `set mpls experimental topmost N` | — |
| `set discard-class N` | `set discard-class N` (ingress only) | **drop** | — |
| `police … exceed-action set-dscp-transmit N` | retain as marker (no rate scheduling intent) | retain as marker | — |

**Mutual exclusion (`RULE_QOSGRP_COEXIST_BAN`):** `set qos-group` cannot
coexist with **any** of the following in the same class:
- `set dscp`
- `set precedence`
- `set mpls experimental imposition`
- `set mpls experimental topmost`

If the source class has both `set qos-group` and one of the above, keep
`set qos-group` (mapped to `set traffic-class` in VOQ; retained as
`set qos-group` in EFC if egress `-REMARKING` will `match qos-group`).
Document removal of the conflicting `set` action and move it to the
egress `-REMARKING` policy where applicable.

### 4.3 Scheduling / queuing actions

| Source action | 8000 IOS XR (egress -Q policy) | Stable ID |
|---|---|---|
| `priority` (no level) | `priority level 1` | — |
| `priority level N` | `priority level N` (1–7 supported) | — |
| `bandwidth percent N` | direct syntax unsupported; convert to `bandwidth remaining percent N` (A100/K100/P100/G100, ≥26.2.1) **or** `bandwidth remaining ratio K` (Q100/Q200 or older release) | `RULE_QOS_BW_PERCENT`, `LINT_BW_PCT` |
| `bandwidth N` (kbps absolute) | direct syntax unsupported; convert to `bandwidth remaining percent` (compute % of parent) **or** `bandwidth remaining ratio K` | `RULE_QOS_BW_PERCENT` |
| `bandwidth remaining percent N` | passthrough on capable NPUs; else convert to `ratio` | `RULE_QOS_BW_PERCENT` |
| `bandwidth remaining ratio K` | passthrough (cap K=63) | — |
| `shape average <rate>` | `shape average <rate>` | — |
| `shape average percent N` | `shape average percent N` | — |
| `police cir <rate>` (egress, scheduling intent) | `shape average <rate>` | `LINT_POLICE_EGR` |
| `random-detect …` | **drop**; substitute `queue-limit <N>` (or Dual-Queue-Limit if spec'd) | `RULE_NO_WRED`, `LINT_WRED` |
| `queue-limit N {bytes\|us\|packets}` | `queue-limit N {bytes\|us\|packets}` | — |

### 4.4 Service-policy attachment

| Source attachment | 8000 IOS XR attachment | Notes |
|---|---|---|
| `service-policy input <NAME>` (XE/XR-classic) | `service-policy input <NAME>` | Ingress: marking only — see Algorithm Q3 |
| `service-policy output <NAME>` (XE/XR-classic single egress policy) | **two lines** on same interface: `service-policy output <NAME>-Q` + `service-policy output <NAME>-REMARKING` | `RULE_QOS_SPLIT_QR` |
| EVC sub-interface attachment (XE 900) | apply on IOS XR `l2transport` sub-interface (under `interface … l2transport`) | per-OS file (ASR900) |
| Bundle main-interface attachment (XR-classic 9K) | apply on `interface Bundle-Ether N`; member-link policies are **not** carried over | per-OS file (ASR9K) |

---

## 5. Algorithm Q1 — Traffic-Class Numbering (deterministic)

Operationalizes the TC ordering referenced in `SKILL.md` rules 9 and 22.
Two runs over the same source MUST produce identical TC numbers.

**Inputs:** the set of egress classes in the source policy-map plus `class-default`.

**Step 1 — Mark priority classes:**
- `priority level 1` (or bare `priority`) → `TC 7`.
- `priority level 2` → `TC 6`.
- `priority level 3` → `TC 5`.
- Higher `priority level N` → `TC (8 - N)` clamped to `[5, 7]`; if collision with a lower N already assigned, use the next free TC ≤4.

**Step 2 — Order the remaining (non-priority, non-default) classes by:**

1. Descending `bandwidth percent` (or ratio K, normalized to percent).
2. Tie-break: descending count of `match` lines in the class-map (more specific first).
3. Tie-break: ascending lexicographic class-map name (stable across runs).

Assign TC values **descending from 4 down to 1** in that order.

**Step 3 — `class-default` → `TC 0`.**

**Step 4 — Validation:**
- All classes in the policy-map must end up with exactly one TC value.
- TC values 0 and 7 must be unique (default and top-priority).
- If more than 4 non-priority + non-default classes exist, fail with
  `COV_QOS_TC` and ask user to consolidate (8000 has 8 TCs; one is reserved
  for default).

**Output side-effects:**
- The TC number selected here is what the egress remarking policy emits as
  `set traffic-class N` (Algorithm Q2) and what the egress queuing policy
  matches with `match traffic-class N` (Algorithm Q2).
- The same TC number is used by Algorithm Q3 for ingress sanitization.

---

## 6. Algorithm Q2 — Egress Policy Split (`-Q` + `-REMARKING`)

Operationalizes `RULE_QOS_SPLIT_QR`.

**MODE GATE (execute FIRST):**

1. Determine target platform mode:
   - **ETM/EFC mode** if: (a) user explicitly states ETM/EFC, OR (b) target is
     any K100 LC / K100 Fixed Platform / K100 Centralized Platform with EFC
     configured, OR (c) target is a select P100 LC with EFC enabled.
     If ambiguous, ask user.
   - **VOQ mode** (default): all other cases.

2. Determine if source egress policy mixes queuing + remarking:
   - **Queuing actions:** `priority`, `bandwidth`, `shape`, `queue-limit`,
     `police` (scheduler)
   - **Remarking actions:** `set dscp`, `set cos`,
     `set mpls experimental topmost`, `set discard-class`
   - If a single source egress policy contains BOTH → split is required
     in both modes.
   - If source egress policy has queuing only (no remarking) → see
     mode-specific handling below.

**VOQ mode path:**
- If source has queuing + remarking mixed → split into:
  - `-REMARKING`: remarking actions only (`set dscp`, `set cos`,
    `set mpls exp topmost`). **NO** `set traffic-class`.
  - `-Q`: `match traffic-class`, scheduling only.
  - Apply both `service-policy output` on same interface.
- If source has queuing only → single `-Q` policy. No `-REMARKING` needed.
- In BOTH cases: ensure an **ingress** policy exists that sets
  `traffic-class` from EXP/DSCP/ACL. If source had no ingress policy on
  that interface, create one and flag as RECOMMENDATION.

**ETM/EFC mode path:**
- `-REMARKING` policy is **MANDATORY** regardless of source content.
- `-REMARKING` MUST include `set traffic-class N` for every class.
- **Classification in `-REMARKING`:** Can match on packet headers
  (EXP/DSCP/ACL) OR on `qos-group` if the ingress policy sets it.
  Using `match qos-group N` (from ingress `set qos-group N`) is a valid
  and useful classification method when the source config used qos-group
  to distinguish flows that are otherwise indistinguishable by header
  fields alone (e.g., TERADATA vs ARTIFACTORY both marked DSCP af21).
- If source also had remarking actions (`set dscp`, `set cos`, etc.),
  include them in `-REMARKING` alongside `set traffic-class N`.
- `-Q` policy: `match traffic-class`, scheduling only.
- Apply both `service-policy output` on same interface.
- Ingress policy is optional for egress TC (egress `-REMARKING` handles
  TC). However, an ingress policy with `set qos-group` may be needed to
  feed the egress `-REMARKING` `match qos-group` classification.

---

**Input:** a single source egress policy-map `<NAME>` containing classes
`C1..Cn` plus `class-default`, each with a mix of `set …` and scheduling
actions.

**Step 1 — Derive TC numbers** for every class via Algorithm Q1.

**Step 2 — Build `<NAME>-REMARKING`** (marking only, classifies on the
source criteria):

```
class-map match-any <NAME>-CK-Ci
 <copy original match lines, translated per §4.1>
!
policy-map <NAME>-REMARKING
 class <NAME>-CK-Ci
  set traffic-class <TCi>
  <pass-through marking actions: set dscp / set cos / set mpls experimental topmost>
 !
 class class-default
  set traffic-class 0
 !
end-policy-map
```

Forbidden in `-REMARKING`: `priority`, `bandwidth*`, `police` (as scheduler),
`shape`, `random-detect`, `queue-limit`. Lints `LINT_BW_PCT`,
`LINT_POLICE_EGR`, `LINT_WRED` apply.

**Step 3 — Build `<NAME>-Q`** (queuing only, classifies on TC):

```
class-map match-any <NAME>-CQ-Ci
 match traffic-class <TCi>
!
policy-map <NAME>-Q
 class <NAME>-CQ-Ci
  <scheduling actions only: priority / bandwidth remaining {percent|ratio} / shape / queue-limit>
 !
 class class-default
  <scheduling action(s) for default; default `bandwidth remaining percent 1` if absent>
 !
end-policy-map
```

Forbidden in `-Q`: any `set …` action, any `match` other than
`match traffic-class`, `random-detect`, egress `police`. Lint `LINT_QGRP_EGR`
applies.

**Step 4 — Interface attachment** (replace the single source line):

```
interface <xr-if>
 service-policy output <NAME>-REMARKING
 service-policy output <NAME>-Q
```

Order is fixed: remarking first (sets TC), queuing second (matches TC).

**Step 5 — Document** the split in Section 6 of the report with one line
per source policy-map: `Egress policy <NAME> split into -REMARKING (marking)
+ -Q (queuing) per RULE_QOS_SPLIT_QR; TC mapping: C1→TC7, C2→TC4, …`.

---

## 7. Algorithm Q3 — Ingress Sanitization

Operationalizes `RULE_INGRESS_NO_SCHED` (rule 22). Ingress policies on Cisco
8000 may carry **marking actions only**.

**Input:** a source ingress policy-map (any OS) that contains scheduling
actions (`priority`, `bandwidth*`, `police` as scheduler, `shape`).

**Step 1 — For each class with a forbidden scheduling action:**

| Source action (ingress) | Replacement | New lint ID |
|---|---|---|
| `priority [level N]` | `set traffic-class <Q1-TC for this class>` | `LINT_PRIO_ING` |
| `bandwidth …` (any form) | `set traffic-class <Q1-TC>` | `LINT_BW_ING` |
| `police cir …` (used as rate scheduler, no marking conform/exceed) | `set traffic-class <Q1-TC>`; if a strict ingress rate cap is required, document that ingress shaping is unsupported | `LINT_POLICE_ING_SCHED` |
| `shape average …` | `set traffic-class <Q1-TC>` | `LINT_SHAPE_ING` |

**Step 2 — Retain** all genuine marking actions (`set dscp`, `set cos`,
`set mpls experimental imposition`, `set discard-class`, `police … conform/exceed
set-…-transmit` as marker).

**Step 3 — Class-map structure and match criteria are preserved** — only the
forbidden action lines are replaced.

**Step 4 — Document** each removal in Section 6 with the format:
`Ingress class <C>: '<action>' is invalid on Cisco 8000 ingress; replaced with
'set traffic-class <N>' for downstream egress queuing.` (Verbatim from rule
22 example.)

**Step 5 — Coverage:** every removed scheduling line must produce exactly
one `set traffic-class` line (or be explicitly retained as marker).
Counter-check is `COV_QOS_ING` — see §9.

**Note on H-QoS gating:** if the source uses a parent-child ingress
hierarchy and the target NPU is Q100/Q200, flatten to a single level and
document under `RULE_INGRESS_HQOS_NPU`.

---

## 8. Algorithm Q4 — WRED & Egress-Police Substitution

Operationalizes `RULE_NO_WRED` (rule 7) and the egress-police rule
(`LINT_POLICE_EGR`, rule 8 in `SKILL.md`). Substitutions are **never silent**.

**Input:** any source egress policy line containing `random-detect …` or
`police …` used as a scheduler.

| Source line | 8000 IOS XR | Documentation entry (Section 6) |
|---|---|---|
| `random-detect dscp-based` | drop; emit `queue-limit <derived> bytes` if any threshold present, else nothing | `WRED dropped per RULE_NO_WRED — 8000 has no random-detect; consider Dual-Queue-Limit if AQM intent must be preserved.` |
| `random-detect dscp <X> <min> <max>` | drop; if both thresholds map to a single `queue-limit`, emit it | same |
| `random-detect exp …` | drop | same |
| `police cir <rate>` (egress, scheduler) | `shape average <rate>` | `Egress police converted to shape per LINT_POLICE_EGR — 8000 has no egress policing as scheduler.` |
| `police cir percent N` (egress) | `shape average percent N` | same |
| `police … pir …` (egress, two-rate) | `shape average <cir>`; document loss of pir | `Two-rate egress police: cir converted to shape; pir dropped (lossy translation).` |

**Forbidden silent removals:** `LINT_WRED` and `LINT_POLICE_EGR` fire on any
unconverted match. Pass `LINT_SHAPE_PCT_OK` confirms percent shaping is
allowed where the source was `police … percent N`.

---

## 9. QoS-Only Pre-Publish Gates

When the request is **QoS-only**, run **only** this subset of the pre-publish
checklist (defined in `CONFIG_TRANSLATION.md`). Other coverage IDs
(`COV_EVC`, `COV_BD`, `COV_VFI`, `COV_G8032_*`, `COV_BGP*`, `COV_RPL`,
`COV_LAG`, `COV_INTF`, `COV_VRF`) are **not applicable** and must not block
publishing.

| Gate | IDs (run in order) | Pass condition |
|---|---|---|
| **Coverage** | `COV_QOS_TC`, `COV_QOS_EGR`, `COV_QOS_ING` (new) | every source class with `match qos-group` produces an egress-queuing class with `match traffic-class`; every source egress policy with `set qos-group` produces an `-REMARKING` policy with `set traffic-class`; every removed ingress scheduling line produces a `set traffic-class`. |
| **Forbidden Token Lint** | `LINT_QGRP_EGR`, `LINT_WRED`, `LINT_BW_PCT`, `LINT_EXP_EGR`, `LINT_POLICE_EGR`, `LINT_BW_ING`, `LINT_PRIO_ING`, `LINT_POLICE_ING_SCHED`, `LINT_SHAPE_ING` | zero matches in Section 3 output. |
| **No-Placeholder** | `NOPH_TODO`, `NOPH_ELLIPSIS`, `NOPH_REDACT_BAD` (subset from `SKILL.md`) | zero matches. |
| **Section Layout** | only Sections 1–6 of the standard report; Section 3 contains exclusively QoS blocks | publishable as a QoS-only report variant. |

**`COV_QOS_ING` definition:** for each ingress class in the source that
contained at least one of `priority`, `bandwidth*`, `police` (scheduler),
`shape`, the corresponding 8000 ingress class MUST contain exactly one
`set traffic-class N` line OR be explicitly documented as retained marker
in Section 6. Failure: list the offending class names and source actions.

**Failure handling:** on any gate failure, emit the IDs and offending lines,
re-translate only those classes, and re-run the gate set. Do not publish
partial pass.

---

## 10. Worked Examples

Three short before/after examples. Same logical intent in all three so the
reader sees only the **input differences** and a single converged 8000 output.

### 10.1 Source: ASR 900 (IOS XE)

```
class-map match-any VOICE
 match dscp ef
class-map match-any VIDEO
 match dscp af41
!
policy-map EGRESS-PARENT
 class VOICE
  priority
  set dscp ef
 class VIDEO
  bandwidth percent 30
  set dscp af41
  random-detect dscp-based
 class class-default
  bandwidth percent 10
  shape average 100000000
!
interface GigabitEthernet0/0/0
 service-policy output EGRESS-PARENT
```

### 10.2 Source: ASR 9000 (IOS XR-classic)

```
class-map match-any VOICE
 match dscp ef
 end-class-map
class-map match-any VIDEO
 match dscp af41
 end-class-map
!
policy-map EGRESS-PARENT
 class VOICE
  priority level 1
  set dscp ef
 class VIDEO
  bandwidth remaining percent 30
  set dscp af41
  random-detect dscp af41 100 ms 200 ms
 class class-default
  bandwidth remaining percent 10
  shape average 100 mbps
 end-policy-map
!
interface HundredGigE0/0/0/0
 service-policy output EGRESS-PARENT
```

### 10.3 Source: NCS 540 / 55xx / 57xx (IOS XR-classic)

```
class-map match-any VOICE
 match dscp ef
 end-class-map
class-map match-any VIDEO
 match dscp af41
 end-class-map
!
policy-map EGRESS-PARENT
 class VOICE
  priority level 1
  set dscp ef
 class VIDEO
  bandwidth remaining percent 30
  set dscp af41
  random-detect dscp af41 100 ms 200 ms
 class class-default
  bandwidth remaining percent 10
  shape average percent 50
 end-policy-map
!
interface HundredGigE0/0/0/0
 service-policy output EGRESS-PARENT
```

### 10.4 Converged Cisco 8000 IOS XR target (target NPU A100/K100/P100/G100, ≥26.2.1)

Algorithm Q1 result: VOICE → TC7 (priority), VIDEO → TC4 (highest non-priority bandwidth), class-default → TC0.

```
class-map match-any EGRESS-PARENT-CK-VOICE
 match dscp ef
 end-class-map
class-map match-any EGRESS-PARENT-CK-VIDEO
 match dscp af41
 end-class-map
class-map match-any EGRESS-PARENT-CQ-VOICE
 match traffic-class 7
 end-class-map
class-map match-any EGRESS-PARENT-CQ-VIDEO
 match traffic-class 4
 end-class-map
!
policy-map EGRESS-PARENT-REMARKING
 class EGRESS-PARENT-CK-VOICE
  set traffic-class 7
  set dscp ef
 class EGRESS-PARENT-CK-VIDEO
  set traffic-class 4
  set dscp af41
 class class-default
  set traffic-class 0
 end-policy-map
!
policy-map EGRESS-PARENT-Q
 class EGRESS-PARENT-CQ-VOICE
  priority level 1
 class EGRESS-PARENT-CQ-VIDEO
  bandwidth remaining percent 30
  queue-limit 200 ms
 class class-default
  bandwidth remaining percent 10
  shape average 100 mbps
 end-policy-map
!
interface HundredGigE0/0/0/0
 service-policy output EGRESS-PARENT-REMARKING
 service-policy output EGRESS-PARENT-Q
```

**Section 6 documentation entries (mandatory):**

- `Egress policy EGRESS-PARENT split into -REMARKING + -Q per RULE_QOS_SPLIT_QR; TC mapping: VOICE→TC7, VIDEO→TC4, class-default→TC0.`
- `WRED on class VIDEO dropped per RULE_NO_WRED; substituted queue-limit 200 ms (derived from random-detect max threshold).`
- `bandwidth percent → bandwidth remaining percent retained (target NPU supports it on ≥26.2.1) per RULE_QOS_BW_PERCENT.`
- *(only if source was ASR900 with `bandwidth percent`)* `bandwidth percent N converted to bandwidth remaining percent N per LINT_BW_PCT.`

**Target-NPU swap (Q100/Q200 example):** replace
`bandwidth remaining percent 30` with `bandwidth remaining ratio 19`
(`round(30/100 * 63) = 19`) and `bandwidth remaining percent 10` with
`bandwidth remaining ratio 6`. Document under `RULE_QOS_BW_PERCENT`.

---

## 11. Anti-patterns / Forbidden Outputs (recap)

Do **not** restate target rules — only IDs + one-line rationale, links back
to `SKILL.md`.

| Forbidden in 8000 output | Lint ID | Rationale |
|---|---|---|
| `set qos-group N` in **egress** policy | `LINT_QGRP_EGR` | `set qos-group` is ingress-only (0–7). Forbidden in any egress policy-map. |
| `random-detect …` | `LINT_WRED` | Not supported on 8000. |
| `bandwidth percent N` (queuing) | `LINT_BW_PCT` | Use `bandwidth remaining percent` (or `ratio` on Q100/Q200). |
| `set mpls experimental imposition N` (egress) | `LINT_EXP_EGR` | Use `set mpls experimental topmost N` at egress. |
| `police cir …` in egress as scheduler | `LINT_POLICE_EGR` | Convert to `shape average …`. |
| `priority`/`bandwidth*`/`police`/`shape` in **ingress** policy-map | `LINT_PRIO_ING` / `LINT_BW_ING` / `LINT_POLICE_ING_SCHED` / `LINT_SHAPE_ING` | Ingress is marking-only on 8000; replace with `set traffic-class N` per Algorithm Q3. |
| `match qos-group N` in **egress queuing** policy | (covered by `COV_QOS_TC` failure) | Egress queuing matches `traffic-class`. |
| `match-all` with both `qos-group` and `discard-class` | `RULE_MATCHALL_DC_BAN` | Collapse to single `match qos-group` (CONFORM value). |
| `set qos-group` AND `set mpls experimental imposition` in same class | `RULE_QOSGRP_EXP_EXCL` | Mutually exclusive; keep `set qos-group`. |
| Empty placeholder lines in QoS section | `NOPH_TODO`, `NOPH_ELLIPSIS` | No-Placeholder gate. |

---

## Determinism Guarantee

Two independent runs of any compliant translator over the same source MUST
produce byte-identical output for the QoS Section 3 block, given the same
target NPU + release. This is enforced by:

- Algorithm Q1 stable tie-breakers (lexicographic class-map name).
- Fixed naming convention for split policies (`-Q`, `-REMARKING`, `-CQ-`,
  `-CK-`).
- Fixed attachment order (remarking before queuing).
- Fixed Section 6 documentation phrasing (templates above).

Any deviation is a translator bug, not a content choice.
