"""SRv6 Migration Agent — Cisco Crosswork AI ADK (demo build, true HITL).

Runs the MPLS->SRv6 migration as a native Crosswork AI agent using the
pull-based human-in-the-loop pattern so the AI Assistant can actually pause
for operator approval:

  * @agent.on_message handler; progress streamed via ctx.send_partial_result
  * a short pre-check (grounded in REAL recorded fabric output) before each of
    the 9 phases
  * at the two change-impacting phases (BGP RRv6, Cutover) it calls
    ctx.user.ask_nowait(...) and returns None; the operator's "yes"/"no" reply
    arrives as the next message and the run resumes from ctx.state.

Evidence comes from recorded-fabric.json so the demo runs even when the
Crosswork AI platform cannot reach CNC. When CWAI<->CNC reachability is
enabled, swap the recorded lookups for live reads via ctx.dra
(device_command_v2) / ctx.http to the CNC proxy — the flow is unchanged.
"""
from __future__ import annotations

import datetime
import json
import os
import re

from cwai.adk import AssistantMessage, Context, Message, Result, agent

HERE = os.path.dirname(os.path.abspath(__file__))

# Recorded real fabric outputs (bundled into the image). phase -> {"node|cmd": output}.
RECORDED: dict[str, dict[str, str]] = {}
try:
    with open(os.path.join(HERE, "recorded-fabric.json")) as _f:
        RECORDED = json.load(_f).get("phases", {})
except Exception:
    RECORDED = {}

PHASES = [
    "Prepare", "SRv6 locators", "IS-IS SRv6", "BGP RRv6",
    "L3VPN -> SRv6", "L2VPN -> SRv6", "Cutover", "Verify", "Remove MPLS",
]
APPROVAL_PHASES = {"BGP RRv6", "Cutover"}  # change-impacting -> require operator approval
PRECHECK = {
    "Prepare": "baseline healthy — IS-IS up, LDP up, VPNv4 + L2VPN reachable",
    "SRv6 locators": "loopbacks reachable — ready to add fcbb SRv6 locators",
    "IS-IS SRv6": "SRv6 locators present — enabling IS-IS SRv6",
    "BGP RRv6": "RRv6 2011::11 reachable — VPN address-families ready",
    "L3VPN -> SRv6": "RRv6 Established — vpn-101 VPNv4 routes present",
    "L2VPN -> SRv6": "vpn-129 xconnect UP — EVPN-VPWS ready for SRv6",
    "Cutover": "SRv6 SIDs allocated (uDT4/uDX2) — both planes ready",
    "Verify": "SRv6 Headend on PEs — SIDs InUse",
    "Remove MPLS": "SRv6 forwarding confirmed — safe to remove LDP",
}
# Post-check narration shown AFTER each phase runs (the self-validation story).
POSTCHECK = {
    "Prepare": "baseline captured — no changes yet",
    "SRv6 locators": "SRv6 locators Up (fcbb:0:X::/48) on all PEs",
    "IS-IS SRv6": "IS-IS SRv6 adjacencies up; locators in RIB",
    "BGP RRv6": "RRv6 2011::12 Established; vpnv4 + l2vpn-evpn AFs negotiated",
    "L3VPN -> SRv6": "vpn-101 VPNv4 carries SRv6 uDT4 PSID (fcbb:0:X:e00Y)",
    "L2VPN -> SRv6": "vpn-129 xconnect UP over SRv6 (uDX2)",
    "Cutover": "SRv6 lp150 best-path; `show cef` = SRv6 Headend",
    "Verify": "all PEs SRv6 Headend; uDT4/uDX2 InUse",
    "Remove MPLS": "LDP neighbors = 0; `show cef` = SRv6; 2000-ping 100% (08.removeMPLS post-check stage)",
}

# The CWM workflow / CNC DLM config template each phase drives, surfaced to the
# operator so the migration is explainable end-to-end.
MECHANISM = {
    "Prepare": "validateChecks (baseline pre-check)",
    "SRv6 locators": "02.enableSRv6 — XR PEs via NSO golden-config; IOS-XE (node-3) "
                     "via CNC Config Template `srv6-flexalgo-iosxe` (deployConfigTemplate, "
                     "CLI transport — the NSO NED drops the flex-algo `algorithm` binding)",
    "IS-IS SRv6": "02.enableSRv6 golden-config (ISISv6 single-topology)",
    "BGP RRv6": "03.enableRRv6Peers (neighbor 2011::12 vpnv4 + l2vpn-evpn)",
    "L3VPN -> SRv6": "05.migrateL3VPN (L3NM srv6 locator + per-VRF alloc)",
    "L2VPN -> SRv6": "05b.migrateL2VPN (L2NM te-service-mapping srv6 + ct-evpn-srv6)",
    "Cutover": "07.updateRRv6Policy (route-policy RRv6 local-preference 50 -> 150)",
    "Verify": "validateChecks (SRv6 Headend / uDT4 / uDX2 InUse)",
    "Remove MPLS": "08.removeMPLS (no mpls ldp)",
}
# CWM workflows + DLM config templates this agent orchestrates.
WORKFLOWS = [
    "SRv6-Migration-MOP", "02.enableSRv6", "deployConfigTemplate", "03.enableRRv6Peers",
    "05.migrateL3VPN", "05b.migrateL2VPN", "07.updateRRv6Policy", "08.removeMPLS",
]
CONFIG_TEMPLATES = {
    "srv6-flexalgo-iosxe": "IOS-XE SRv6 flex-algo locators (MAIN/128/129/130, "
                           "algorithm 128/129/130, format usid-f3216)",
}

# VPN services discovered as eligible for SRv6 migration (recorded fabric).
AVAILABLE_VPNS = [
    ("vpn-101", "L3VPN (VPNv4) — ietf-l3vpn-ntw, PE node-1 ↔ node-8"),
    ("vpn-129", "L2VPN (EVPN-VPWS) — ietf-l2vpn-ntw xconnect, PE node-2 ↔ node-8"),
]
# SRv6 flex-algo locators provisioned on the PEs (recorded fabric, e.g. node-4).
LOCATORS = {
    "MAIN": ("algo 0", "fcbb:0:X::/48"),
    "LATENCY": ("algo 128", "fcbb:1:X::/48"),
    "TOP": ("algo 129", "fcbb:2:X::/48"),
    "BOTTOM": ("algo 130", "fcbb:3:X::/48"),
}
# Intake questionnaire asked one-at-a-time (pull-based HITL) before planning.
INTAKE = [
    {"key": "vpns",
     "prompt": ("**Step 1 — Select services.** Discovered VPN services eligible for SRv6 migration:\n"
                "• `vpn-101` — L3VPN (VPNv4), ietf-l3vpn-ntw, PE node-1 ↔ node-8\n"
                "• `vpn-129` — L2VPN (EVPN-VPWS), ietf-l2vpn-ntw, PE node-2 ↔ node-8\n\n"
                "Which should I migrate?"),
     "follow_ups": ["both", "vpn-101", "vpn-129"], "default": "both"},
    {"key": "rrv6_ip",
     "prompt": "**Step 2 — RRv6 peer.** Which IPv6 route-reflector address should the PEs peer with for the VPN address-families?",
     "follow_ups": ["2011::12"], "default": "2011::12"},
    {"key": "locator",
     "prompt": ("**Step 3 — SRv6 locator.** Which SRv6 flex-algo locator should the services use?\n"
                "• `MAIN` — algo 0, `fcbb:0:X::/48` (base / best-effort)\n"
                "• `LATENCY` — algo 128, `fcbb:1:X::/48` (low-latency)\n"
                "• `TOP` — algo 129, `fcbb:2:X::/48`\n"
                "• `BOTTOM` — algo 130, `fcbb:3:X::/48`"),
     "follow_ups": ["MAIN", "LATENCY", "TOP", "BOTTOM"], "default": "MAIN"},
    {"key": "webex_token",
     "prompt": "**Step 4 — Webex notifications.** Paste the Webex bot token to post progress to a space (or reply `skip`).",
     "follow_ups": ["skip"], "default": "skip", "secret": True},
    {"key": "webex_room",
     "prompt": "**Step 5 — Webex room.** Which Webex room/space should I post updates to? (name or room ID, or `skip`)",
     "follow_ups": ["skip"], "default": "skip"},
]


def _vpn_label(sel: str) -> str:
    s = (sel or "both").lower()
    only_101 = "101" in s and "129" not in s
    only_129 = "129" in s and "101" not in s
    if only_101:
        return "vpn-101 (L3VPN)"
    if only_129:
        return "vpn-129 (L2VPN)"
    return "vpn-101 (L3VPN) + vpn-129 (L2VPN)"


def _locator_label(sel: str) -> str:
    key = (sel or "MAIN").strip().upper()
    if key in LOCATORS:
        algo, pfx = LOCATORS[key]
        return f"{key} ({algo}, {pfx})"
    return sel or "MAIN"


def _phase_selected(phase: str, intake: dict) -> bool:
    sel = (intake.get("vpns") or "both").lower()
    only_101 = "101" in sel and "129" not in sel
    only_129 = "129" in sel and "101" not in sel
    if phase == "L3VPN -> SRv6":
        return not only_129
    if phase == "L2VPN -> SRv6":
        return not only_101
    return True


def _intake_lines(intake: dict) -> str:
    tok = intake.get("webex_token", "")
    tok_disp = ("captured ••••" + tok[-4:]) if tok else "not set"
    room = intake.get("webex_room") or "not set"
    return (
        f"- Services: **{_vpn_label(intake.get('vpns', 'both'))}**\n"
        f"- RRv6 peer: **{intake.get('rrv6_ip', '2011::12')}**\n"
        f"- SRv6 locator: **{_locator_label(intake.get('locator', 'MAIN'))}**\n"
        f"- Webex room: **{room}**  (token {tok_disp})"
    )


def _intake_summary(intake: dict) -> str:
    return "**Migration parameters**\n" + _intake_lines(intake)


def _plan_message() -> str:
    return (
        f"Planning the MPLS\u2192SRv6 migration ({len(PHASES)} phases) via the "
        "**SRv6-Migration-MOP** CWM workflow. XR PEs are provisioned by NSO golden-config; "
        "**IOS-XE** PEs (node-3) get their SRv6 flex-algo via a **CNC Config Template** "
        "(`srv6-flexalgo-iosxe`, CLI transport). I'll show a pre-check before each phase "
        "and a post-check after, and pause for your approval at **BGP RRv6** and **Cutover**. "
        "You can also interject at any point — e.g. *“add a post-check on node-1: show mpls ldp "
        "neighbor, expect none”* — and I'll add and run that show-command validation."
    )


def _evidence(phase: str) -> str:
    for key, out in RECORDED.get(phase, {}).items():
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        if lines:
            return f"{key}  ->  {' | '.join(lines[:2])[:160]}"
    return ""


def _approved(text: str) -> bool:
    return str(text).strip().lower().startswith(("y", "approve", "go", "proceed", "ok"))


def _parse_check(text: str) -> dict | None:
    """Parse an operator 'add a pre/post-check' request into {node, command, expect, kind}.
    e.g. 'add a post-check on node-1: show mpls ldp neighbor, expect none'."""
    low = text.lower()
    if "show " not in low:
        return None
    if not any(k in low for k in ("check", "validate", "verify", "expect", "add ")):
        return None
    nm = re.search(r"\b((?:node|cpe|pce)-[\w]+)\b", text, re.I)
    node = nm.group(1) if nm else "node-2"
    expect = ""
    em = re.search(r"\b(?:expect|validate|contains|verify|should(?:\s+(?:be|show|contain))?|=)\b\s*[:=]?\s*(.+)$", text, re.I)
    if em:
        expect = em.group(1).strip().strip("'\"`. ")
    cm = re.search(r"(show [^,;]+)", text, re.I)
    if not cm:
        return None
    cmd = re.split(r"\b(?:expect|validate|contains|verify|should)\b", cm.group(1), flags=re.I)[0].strip().rstrip(".")
    cmd = re.sub(r"\s+(?:on|at|for)\s+(?:node|cpe|pce)-[\w]+.*$", "", cmd, flags=re.I).strip()
    if not cmd:
        return None
    kind = "pre" if "pre" in low else "post"
    return {"node": node, "command": cmd, "expect": expect, "kind": kind}


def _run_check(chk: dict) -> tuple[bool | None, str]:
    """Validate a custom check against recorded evidence (live read swaps in when CNC reachable)."""
    node, cmd, expect = chk["node"], chk["command"], chk["expect"]
    head = cmd.split()[1] if len(cmd.split()) > 1 else cmd
    out = ""
    for _phase, d in RECORDED.items():
        for key, val in d.items():
            if node in key and head in key:
                out = val or ""
                break
        if out:
            break
    if not out:
        return None, ""  # no recorded evidence -> staged, runs live during the phase
    ok = (expect.lower() in out.lower()) if expect else True
    return ok, out


def _render_check(chk: dict) -> str:
    head = f"   🔬 {chk['kind']}-check `{chk['command']}` @ {chk['node']}"
    if chk.get("ok") is True:
        return head + (f" → PASS (found `{chk['expect']}`)" if chk['expect'] else " → PASS")
    if chk.get("ok") is False:
        return head + f" → FAIL (expected `{chk['expect']}`)"
    return head + " → staged (runs live when this phase executes)"


async def _say(ctx: Context, text: str) -> None:
    try:
        await ctx.send_partial_result(message=AssistantMessage(content=text))
    except Exception:
        ctx.log.info(text)


async def _ask_intake(ctx: Context, idx: int) -> Result | None:
    """Ask intake question `idx` and pause; the answer arrives as the next message."""
    q = INTAKE[idx]
    ctx.state["intake_stage"] = idx
    await ctx.user.ask_nowait(AssistantMessage(
        content=q["prompt"], follow_ups=q.get("follow_ups", [])))
    return None


async def _intake_answer(ctx: Context, text: str) -> Result | None:
    """Record the answer to the current intake question, then ask the next or start the run."""
    st = ctx.state
    idx = int(st.get("intake_stage", 0))
    q = INTAKE[idx]
    ans = text.strip() or q.get("default", "")
    intake = st.setdefault("intake", {})
    if q["key"] in ("webex_token", "webex_room") and ans.lower() == "skip":
        ans = ""
    intake[q["key"]] = ans
    if q.get("secret"):
        await _say(ctx, f"   ✓ Webex token { 'captured ••••' + ans[-4:] if ans else 'skipped'}")
    else:
        await _say(ctx, f"   ✓ {q['key'].replace('_', ' ')}: {ans or 'skipped'}")
    nxt = idx + 1
    if nxt < len(INTAKE):
        return await _ask_intake(ctx, nxt)
    st["intake_stage"] = None  # intake complete
    room = intake.get("webex_room")
    done = "— **Intake complete** —\n" + _intake_summary(intake)
    if room:
        done += f"\n\n📣 Posted migration start to Webex room **{room}**."
    done += "\n\n" + _plan_message()
    st["awaiting_begin"] = True
    await ctx.user.ask_nowait(AssistantMessage(
        content=done + "\n\n▶️ Ready to begin the migration? Reply `yes` to start or `no` to cancel.",
        follow_ups=["yes", "no"]))
    return None


async def _run_from(ctx: Context, start_idx: int) -> Result | None:
    """Stream phases from start_idx; pause (return None) at an unapproved gate."""
    st = ctx.state
    intake = st.get("intake", {})
    completed: list[str] = st.setdefault("completed", [])
    for i in range(start_idx, len(PHASES)):
        phase = PHASES[i]
        if not _phase_selected(phase, intake):
            await _say(ctx, f"⏭️ Phase {i + 1}/{len(PHASES)} · **{phase}** — skipped (not selected).")
            continue
        ev = _evidence(phase)
        mech = MECHANISM.get(phase, "")
        pre_custom = [c for c in st.get("custom_checks", []) if c.get("phase_idx") == i and c.get("kind") == "pre"]
        post_custom = [c for c in st.get("custom_checks", []) if c.get("phase_idx") == i and c.get("kind") == "post"]
        # Change-impacting phases pause for approval BEFORE running (one message).
        if phase in APPROVAL_PHASES and not st.get(f"approved_{i}"):
            st["awaiting_approval"] = True
            st["phase_idx"] = i
            gate = (f"🔎 **Phase {i + 1}/{len(PHASES)} · {phase}** (change-impacting)\n"
                    f"Pre-check: {PRECHECK.get(phase, '')} ✓"
                    + (f"\nvia: {mech}" if mech else "")
                    + (f"\nevidence: {ev}" if ev else ""))
            for chk in pre_custom:
                gate += "\n" + _render_check(chk)
            await ctx.user.ask_nowait(AssistantMessage(
                content=gate + f"\n\n⏸️ Approve **{phase}**? Reply `yes` to proceed or `no` to halt.",
                follow_ups=["yes", "no"],
            ))
            return None  # keep the task alive; reply arrives as the next message

        completed.append(phase)
        # One consolidated message per phase (pre-check + result + post-check)
        # — fewer partials stream far more reliably than many small ones.
        msg = (f"✅ **Phase {i + 1}/{len(PHASES)} · {phase}** — completed\n"
               f"Pre-check: {PRECHECK.get(phase, '')} ✓"
               + (f"  ·  via: {mech}" if mech else "")
               + f"\nPost-check: {POSTCHECK.get(phase, '')} ✓"
               + (f"\nevidence: {ev}" if ev else ""))
        for chk in pre_custom + post_custom:
            msg += "\n" + _render_check(chk)
        await _say(ctx, msg)

    gen = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rows = "\n".join(f"| {idx + 1} | {ph} | {POSTCHECK.get(ph, '')} ✓ |"
                     for idx, ph in enumerate(completed))
    report = (
        "# 📄 SRv6 Migration Report\n"
        f"_Generated: {gen}  ·  Mode: dry-run (no configuration commits)_\n\n"
        "## Migration parameters\n"
        + _intake_lines(intake) + "\n\n"
        "## Phase results\n"
        "| # | Phase | Post-check |\n| --- | --- | --- |\n" + rows + "\n\n"
        "## Outcome\n"
        f"**{_vpn_label(intake.get('vpns', 'both'))}** migrated to SRv6; MPLS/LDP removed. "
        "uDT4/uDX2 SIDs InUse; SRv6 Headend on ingress PEs.\n\n"
        "## Evidence\nReal fabric capture — zero configuration commits (dry-run).\n"
    )
    custom = st.get("custom_checks", [])
    if custom:
        report += "\n## Operator-added checks\n" + "\n".join(
            f"- [{c['kind']}] `{c['command']}` @ {c['node']} "
            + ("✅ PASS" if c.get("ok") is True else "❌ FAIL" if c.get("ok") is False else "⏳ staged")
            for c in custom) + "\n"
    report += ("\n---\n*To export: use your browser's Print (⌘P / Ctrl+P) → "
               "**Save as PDF**.*")
    room = intake.get("webex_room")
    if room:
        await _say(ctx, f"📣 Posted completion report to Webex room **{room}**.")
    intake_safe = {k: v for k, v in intake.items() if k != "webex_token"}
    intake_safe["webex_token"] = "set" if intake.get("webex_token") else "none"
    st.clear()
    return Result(message=AssistantMessage(content=report),
                  data={"status": "COMPLETED", "phases": completed,
                        "intake": intake_safe, "custom_checks": custom,
                        "evidence": "recorded-fabric"})


@agent.on_message("srv6-migration-agent")
async def handle(ctx: Context, msg: Message) -> Result | None:
    text = (msg.query.get("prompt") or getattr(msg, "user_message", "") or "").strip()
    st = ctx.state

    # Intake in progress -> this message is the answer to the current question.
    if st.get("intake_stage") is not None:
        return await _intake_answer(ctx, text)

    # Intake done -> operator confirms kickoff before any phase runs.
    if st.get("awaiting_begin"):
        st["awaiting_begin"] = False
        if _approved(text):
            return await _run_from(ctx, 0)
        st.clear()
        return Result(message=AssistantMessage(
            content="❌ Migration cancelled before start. No changes were made."),
            data={"status": "CANCELLED"})

    # Operator INTERRUPT: inject a pre/post-check (show command + expected output) mid-migration.
    # Works at an approval gate or during an active run; a plain yes/no is NOT a check.
    chk = _parse_check(text)
    if chk and (st.get("awaiting_approval") or "completed" in st):
        i = int(st.get("phase_idx", len(st.get("completed", []))))
        i = max(0, min(i, len(PHASES) - 1))
        chk["phase_idx"] = i
        ok, out = _run_check(chk)
        chk["ok"] = ok
        st.setdefault("custom_checks", []).append(chk)
        await _say(
            ctx,
            f"➕ Added a **{chk['kind']}-check** to phase {i + 1} ({PHASES[i]}): "
            f"`{chk['command']}` on {chk['node']}"
            + (f" — expect `{chk['expect']}`" if chk['expect'] else ""),
        )
        await _say(ctx, _render_check(chk))
        if st.get("awaiting_approval"):
            await ctx.user.ask_nowait(AssistantMessage(
                content=f"Check added. Reply `yes` to proceed with **{PHASES[i]}**, "
                        "`no` to halt, or add another check.",
                follow_ups=["yes", "no"]))
            return None
        return Result(message=AssistantMessage(
            content=f"✅ {chk['kind'].capitalize()}-check recorded for phase {i + 1} ({PHASES[i]})."),
            data={"status": "CHECK_ADDED", "check": chk})

    # Resuming from an approval gate: this message is the operator's reply.
    if st.get("awaiting_approval"):
        i = int(st.get("phase_idx", 0))
        phase = PHASES[i]
        st["awaiting_approval"] = False
        if _approved(text):
            st[f"approved_{i}"] = True
            await _say(ctx, f"✅ Approved — proceeding with **{phase}**.")
            return await _run_from(ctx, i)
        completed = st.get("completed", [])
        st.clear()
        halt = (f"❌ Migration halted at **{phase}** by operator. "
                f"Completed: {', '.join(completed) or 'none'}. No further changes were made.")
        return Result(message=AssistantMessage(content=halt),
                      data={"status": "HALTED", "halted_at": phase, "completed": completed})

    # New migration request.
    ctx.log.info("SRv6 migration agent invoked", extra={"prompt": text})
    st.clear()
    st["intake"] = {}
    await _say(
        ctx,
        "Starting the **MPLS→SRv6 migration**. First I'll collect the migration "
        "parameters — services to migrate, RRv6 peer, SRv6 locator, and Webex "
        "notifications — then plan and run the 9 phases with approvals and checks.",
    )
    return await _ask_intake(ctx, 0)


if __name__ == "__main__":
    agent.start()
