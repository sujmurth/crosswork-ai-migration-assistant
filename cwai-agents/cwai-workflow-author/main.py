"""Workflow Author Agent — Cisco Crosswork AI ADK.

Turns a natural-language request ("push template T to devices D and verify with
these show-command checks") into a **validated, imported CWM golden-config
workflow**. Reusable beyond SRv6 — it authors any golden-config push+verify
workflow.

Why this is safe for an LLM/operator to drive: the control flow is a FIXED,
proven serverless-workflow skeleton (foreach-apply + optional post-check switch).
The request only fills PARAMETERS (name, devices, CLI body, checks) — never the
state machine — and every generated payload is golden-config **dry-run
validated** against CNC before the workflow is imported. Free-form workflow JSON
is never accepted.

Conversation (pull-based HITL):
  1. operator describes the workflow (NL or key=value)
  2. agent parses params; asks for anything required that's missing
  3. agent builds the skeleton and **dry-runs** the apply payload (GATE)
  4. agent shows the native diff and pauses for approval before import
  5. on `yes`, it imports to CWM and reports the result

CNC reachability: when the runtime can reach CNC (env CNC_HOST/USER/PASSWORD),
the dry-run + import run live. When it cannot, the definition is still built
deterministically and returned as "staged" with the gate marked pending.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from cwai.adk import AssistantMessage, Context, Message, Result, agent

# ---- CNC connection (env-overridable) ---------------------------------------
HOST = os.environ.get("CNC_HOST", "198.18.134.219")
PORT = os.environ.get("CNC_PORT", "30603")
USER = os.environ.get("CNC_USER", "admin")
PASS = os.environ.get("CNC_PASSWORD", "${CNC_PASSWORD}")
BASE = f"https://{HOST}:{PORT}"
NSO = BASE + "/crosswork/proxy/nso/restconf/data"
CWM = BASE + "/crosswork/cwm/v2"
SSO = BASE + "/crosswork/sso/v1/tickets"
YC = "application/yang-data+json"
JC = "application/json"


def _ctx() -> ssl.SSLContext:
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE  # CNC self-signed
    return c


def _post_form(url: str, form: dict, safe: str = "") -> tuple[int, str, str]:
    data = urllib.parse.urlencode(form, safe=safe).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/plain"})
    with urllib.request.urlopen(req, context=_ctx(), timeout=30) as r:
        return r.status, r.getheader("Location", ""), r.read().decode(errors="replace")


def _token() -> str:
    """CAS TGT -> JWT (service = app-dashboard), mirroring get_cnc_token.py."""
    _, loc, body = _post_form(SSO, {"username": USER, "password": PASS}, safe="!")
    m = re.search(r"TGT-[\w.\-]+", loc) or re.search(r"TGT-[\w.\-]+", body)
    if not m:
        raise RuntimeError("no TGT from CAS")
    _, _, jwt = _post_form(f"{SSO}/{m.group(0)}", {"service": f"{BASE}/app-dashboard"})
    jwt = jwt.strip()
    if not jwt.startswith("eyJ"):
        raise RuntimeError("CAS returned no JWT")
    return jwt


def _req(jwt: str, method: str, url: str, body=None, ct: str = YC) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + jwt, "Accept": ct, "Content-Type": ct})
    try:
        with urllib.request.urlopen(r, context=_ctx(), timeout=90) as x:
            return x.status, x.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---- generator primitives (network-free build; HTTP for gate + import) ------
def create_template(jwt: str, template: str, config_cli: str) -> tuple[int, str]:
    body = {"golden-config:template": [{"name": template, "tag": ["generated"],
            "version": [{"id": "1.0", "type": "native", "mode": "merge", "config": config_cli}]}]}
    return _req(jwt, "PUT", f"{NSO}/golden-config:golden-config/template={template}", body)


def dryrun_apply(jwt: str, device: str, template: str, app_prefix: str) -> tuple[int, str, str]:
    app = {"golden-config:golden-config": {"application": [{
        "device": device, "name": f"{app_prefix}-{device}",
        "jinja-template": {"template": template, "version": "1.0"}}]}}
    st, b = _req(jwt, "PATCH", f"{NSO}/golden-config:golden-config?dry-run=native", app)
    diff = ""
    if st == 200:
        try:
            devs = (json.loads(b).get("dry-run-result") or {}).get("native", {}).get("device", [])
            diff = "\n".join(d.get("data", "").strip() for d in devs)
        except Exception:
            diff = b[:300]
    return st, diff, b[:300]


def _fn(name: str, op: str) -> dict:
    return {"name": name, "metadata": {"worker": "cwm-solutions-cisco.nso"}, "operation": op}


def _exec_check(idx: int, node: str, ned: str, cmd: str, outkey: str, nxt: str) -> dict:
    return {"name": f"check{idx}", "type": "operation", "transition": nxt, "actions": [{
        "name": f"check{idx}Exec", "retryRef": "Default",
        "functionRef": {"refName": "NSO.RestconfPost", "arguments": {
            "input": {"data": json.dumps({"input": {"args": cmd}}),
                      "path": f"restconf/operations/devices/device={node}/live-status/{ned}:exec/any"},
            "config": {"resourceId": "eNSO"}}},
        "actionDataFilter": {
            "results": '${ if (.data) then (.data."%s:output" // {}).result else "" end }' % ned,
            "toStateData": "${ ." + outkey + " }"}}]}


def build_workflow(name: str, devices: list[str], template: str, app_prefix: str, checks: list[dict]) -> dict:
    apply_data = ('${ {"golden-config:golden-config": {"application": [ '
                  '{ "device": .node.name, "jinja-template": {"template": "%s", "version": "1.0"}, '
                  '"name": ("%s-" + .node.name) } ]}} }' % (template, app_prefix))
    states = [{
        "name": "iterateDevice", "type": "foreach", "mode": "sequential",
        "inputCollection": "${ .devices }", "iterationParam": "node",
        "actions": [{"name": "applyGC", "retryRef": "Default",
            "functionRef": {"refName": "NSO.RestconfPatch", "arguments": {
                "input": {"data": apply_data, "path": "restconf/data/golden-config:golden-config"},
                "config": {"resourceId": "eNSO"}}}}],
        "transition": "check0" if checks else "endOfWF"}]
    conds = []
    for i, chk in enumerate(checks):
        ned = "tailf-ned-cisco-ios-stats" if chk.get("iosxe") else "tailf-ned-cisco-ios-xr-stats"
        nxt = f"check{i + 1}" if i + 1 < len(checks) else "evaluate"
        states.append(_exec_check(i, chk["node"], ned, chk["command"], f"chk{i}", nxt))
        conds.append('((.chk%d // "") | contains(%s))' % (i, json.dumps(chk["expect"])))
    if checks:
        states.append({"name": "evaluate", "type": "switch",
            "dataConditions": [{"condition": "${ " + " and ".join(conds) + " }", "transition": "endOfWF"}],
            "defaultCondition": {"transition": "verificationFailed"}})
        states.append({"name": "verificationFailed", "type": "operation", "end": {"terminate": True},
            "actions": [{"name": "forceFail", "functionRef": {"refName": "NSO.RestconfGet", "arguments": {
                "input": {"path": "restconf/data/tailf-ncs:devices/device=__check_failed__/config"},
                "config": {"resourceId": "eNSO"}}}}]})
    states.append({"name": "endOfWF", "type": "operation", "end": True})
    return {"name": name, "version": "1.0",
            "wfTags": ["generated", name.split(".")[0] if "." in name else "gen"],
            "definition": {"id": f"{name}-1.0", "name": name, "version": "1.0",
                "start": "iterateDevice", "specVersion": "0.8",
                "functions": [_fn("NSO.RestconfPatch", "cisco.nso.@latest.restconf.Patch"),
                              _fn("NSO.RestconfPost", "cisco.nso.@latest.restconf.Post"),
                              _fn("NSO.RestconfGet", "cisco.nso.@latest.restconf.Get")],
                "states": states,
                "retries": [{"name": "Default", "delay": "PT10S", "multiplier": 1.5, "maxAttempts": 2}]}}


def import_workflow(jwt: str, wf: dict) -> tuple[int, str]:
    _, wl = _req(jwt, "GET", CWM + "/workflow", ct=JC)
    try:
        existing = next((w for w in json.loads(wl) if w.get("name") == wf["name"]), None)
    except Exception:
        existing = None
    if existing:
        wf2 = dict(wf)
        wf2["workflowId"] = existing["workflowId"]
        return _req(jwt, "PUT", f"{CWM}/workflow/{existing['workflowId']}", wf2, ct=JC)
    payload = [{"definition": wf["definition"], "wfTags": wf.get("wfTags", [])}]
    return _req(jwt, "POST", CWM + "/workflowImport", payload, ct=JC)


# ---- NL request parsing -----------------------------------------------------
_KEYS = r"(?:name|named|workflow|devices|device|nodes|cli|config|configuration|template|check)"


def _field(text: str, *keys: str) -> str | None:
    """Pull a `key=value` / `key: value` field from text. Unquoted values stop at
    the next ` <known-key>[:=]` so space-separated key=value pairs parse cleanly."""
    for k in keys:
        m = re.search(rf"\b{k}\s*[:=]\s*(\"[^\"]*\"|'[^']*'|.+?)(?=\s+{_KEYS}\s*[:=]|$)",
                      text, re.I | re.S)
        if m:
            return m.group(1).strip().strip("'\"").strip()
    return None


def _parse_checks(text: str) -> list[dict]:
    checks: list[dict] = []
    # explicit:  check="node|command|expect[|iosxe]"  (repeatable)
    for m in re.finditer(r"\bcheck\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\n,;]+)", text, re.I):
        raw = m.group(1).strip().strip("'\"")
        if "|" in raw:
            p = [x.strip() for x in raw.split("|")]
            checks.append({"node": p[0], "command": p[1], "expect": p[2] if len(p) > 2 else "",
                           "iosxe": len(p) > 3 and p[3].lower() == "iosxe"})
    # natural:  "verify|check on node-X with `show ...` contains|expect VALUE"
    if not checks:
        vm = re.search(r"(?:verify|validate|check).*?(show [^,;`\"']+).*?(?:contains|expect|=|show\s+)\s*([^\n,;]+)",
                       text, re.I)
        if vm:
            nm = re.search(r"\b((?:node|cpe|pce)-[\w]+)\b", text, re.I)
            checks.append({"node": nm.group(1) if nm else "node-1",
                           "command": vm.group(1).strip(), "expect": vm.group(2).strip().strip("'\"`. "),
                           "iosxe": "iosxe" in text.lower() or "ios-xe" in text.lower()})
    return checks


def parse_request(text: str) -> dict:
    """Extract {name, devices, cli, template, checks} from an NL/key=value request."""
    name = _field(text, "name", "named", "workflow") or ""
    if not name:
        nm = re.search(r"\b(?:named|called)\s+([\w.\-]+)", text, re.I)
        name = nm.group(1) if nm else ""
    devices_s = _field(text, "devices", "device", "nodes")
    if devices_s:
        devices = [d.strip() for d in re.split(r"[,\s]+", devices_s) if d.strip()]
    else:
        devices = re.findall(r"\b(?:node|cpe|pce)-[\w]+\b", text, re.I)
    devices = list(dict.fromkeys(devices))  # dedupe, keep order
    cli = _field(text, "cli", "config", "configuration")
    if not cli:
        cm = re.search(r"(?:push|set|apply|configure)\s+(.+?)(?:\s+(?:to|on)\s+(?:node|cpe|pce)-|\bcheck\b|$)",
                       text, re.I)
        cli = cm.group(1).strip().strip("'\"`") if cm else None
    template = _field(text, "template") or (f"gc-{name.replace('.', '-')}" if name else "")
    return {"name": name, "devices": devices, "cli": cli, "template": template,
            "checks": _parse_checks(text)}


def _missing(req: dict) -> list[str]:
    miss = []
    if not req.get("name"):
        miss.append("a workflow **name** (e.g. `name=99.setSnmp`)")
    if not req.get("devices"):
        miss.append("one or more **devices** (e.g. `devices=node-1,node-2`)")
    if not req.get("cli"):
        miss.append("the **config CLI** to push (e.g. `cli=\"snmp-server contact Demo\"`)")
    return miss


async def _say(ctx: Context, text: str) -> None:
    try:
        await ctx.send_partial_result(message=AssistantMessage(content=text))
    except Exception:
        ctx.log.info(text)


def _approved(text: str) -> bool:
    return str(text).strip().lower().startswith(("y", "approve", "go", "proceed", "ok", "import"))


def _summ(req: dict) -> str:
    chk = req.get("checks", [])
    return (f"**{req['name']}** → push `{req['cli']}` to {', '.join(req['devices'])} "
            f"(template `{req['template']}`)"
            + (f"; {len(chk)} post-check(s): "
               + "; ".join(f"`{c['command']}`@{c['node']}→`{c['expect']}`" for c in chk) if chk else ""))


async def _build_and_gate(ctx: Context, req: dict) -> Result | None:
    """Build the skeleton, run the dry-run gate, then pause for import approval."""
    st = ctx.state
    app_prefix = f"gc-{req['name'].replace('.', '-')}"
    wf = build_workflow(req["name"], [{"name": d} for d in req["devices"]],
                        req["template"], app_prefix, req["checks"])
    n_states, n_checks = len(wf["definition"]["states"]), len(req["checks"])
    await _say(ctx, f"🛠️ Built workflow from the fixed skeleton: **{n_states} states** "
                    f"(foreach-apply + {n_checks} check(s){' + verify switch' if n_checks else ''}). "
                    "Control flow is fixed; only parameters were filled.")

    # dry-run GATE
    staged = False
    try:
        jwt = _token()
        ts, _ = create_template(jwt, req["template"], req["cli"])
        await _say(ctx, f"📝 golden-config template `{req['template']}` → HTTP {ts}")
        ds, diff, raw = dryrun_apply(jwt, req["devices"][0], req["template"], app_prefix)
        if ds != 200:
            st.clear()
            return Result(message=AssistantMessage(content=(
                f"🛑 **Dry-run gate FAILED** (HTTP {ds}) — not importing. The CLI does not "
                f"render cleanly as golden-config:\n```\n{raw}\n```")),
                data={"status": "GATE_FAILED", "http": ds})
        await _say(ctx, "✅ **Dry-run gate passed.** native diff:\n```\n"
                        + (diff.strip() or "(no-op / empty)")[:600] + "\n```")
    except Exception as e:  # runtime cannot reach CNC — stage the built definition
        staged = True
        await _say(ctx, f"⚠️ CNC not reachable from the runtime ({type(e).__name__}) — "
                        "dry-run gate **staged**. The definition is built and ready to import "
                        "once reachability is enabled.")

    st["built_wf"] = wf
    st["req"] = req
    st["staged"] = staged
    st["awaiting_import"] = True
    await ctx.user.ask_nowait(AssistantMessage(
        content=f"⏸️ Ready to import **{req['name']}** to CWM"
                + (" (staged — gate pending reachability)" if staged else " (dry-run validated)")
                + ". Reply `yes` to import, `no` to cancel, or add a check "
                  "(e.g. `check=\"node-1|show running-config snmp-server|Demo\"`).",
        follow_ups=["yes", "no"]))
    return None  # pull-based HITL — operator reply resumes the task


def _merge(base: dict, extra: dict) -> dict:
    """Merge newly-supplied params into a partial request (follow-up answers)."""
    out = dict(base)
    for k in ("name", "cli", "template"):
        if not out.get(k) and extra.get(k):
            out[k] = extra[k]
    if not out.get("devices") and extra.get("devices"):
        out["devices"] = extra["devices"]
    if extra.get("checks"):
        out.setdefault("checks", []).extend(extra["checks"])
    if out.get("name") and not out.get("template"):
        out["template"] = f"gc-{out['name'].replace('.', '-')}"
    return out


@agent.on_message("workflow-author-agent")
async def handle(ctx: Context, msg: Message) -> Result | None:
    text = (msg.query.get("prompt") or getattr(msg, "user_message", "") or "").strip()
    st = ctx.state

    # Resume while collecting missing params.
    if st.get("awaiting_params"):
        req = _merge(st.get("req", {}), parse_request(text))
        miss = _missing(req)
        if miss:
            st["req"] = req
            await ctx.user.ask_nowait(AssistantMessage(content=(
                "Still need " + "; ".join(miss) + ".")))
            return None
        st["awaiting_params"] = False
        await _say(ctx, "🧩 Parsed request: " + _summ(req))
        return await _build_and_gate(ctx, req)

    # Resume at the import gate.
    if st.get("awaiting_import"):
        req = st.get("req", {})
        # add-a-check interrupt before import -> rebuild + re-gate
        add = _parse_checks(text)
        if add and not _approved(text) and not text.strip().lower().startswith("n"):
            req.setdefault("checks", []).extend(add)
            st["awaiting_import"] = False
            await _say(ctx, f"➕ Added {len(add)} check(s). Rebuilding and re-validating…")
            return await _build_and_gate(ctx, req)
        st["awaiting_import"] = False
        if not _approved(text):
            st.clear()
            return Result(message=AssistantMessage(content="❌ Cancelled — nothing imported."),
                          data={"status": "CANCELLED"})
        wf = st.get("built_wf")
        if st.get("staged"):
            st.clear()
            return Result(message=AssistantMessage(content=(
                "📦 Definition is **staged** (CNC unreachable from runtime). "
                "Import it from a host with CNC access via "
                "`POST /crosswork/cwm/v2/workflowImport`.")),
                data={"status": "STAGED", "definition": wf["definition"]})
        try:
            jwt = _token()
            ist, ib = import_workflow(jwt, wf)
        except Exception as e:
            st.clear()
            return Result(message=AssistantMessage(content=f"🛑 Import error: {e}"),
                          data={"status": "IMPORT_ERROR"})
        ok = ist in (200, 201)
        name = req.get("name", wf["name"])
        st.clear()
        return Result(message=AssistantMessage(content=(
            f"{'✅' if ok else '🛑'} Import of **{name}** → HTTP {ist}. "
            + ("It's now runnable in CWM." if ok else f"Detail: {ib[:200]}"))),
            data={"status": "IMPORTED" if ok else "IMPORT_FAILED", "http": ist, "workflow": name})

    # New authoring request.
    ctx.log.info("workflow-author agent invoked", extra={"prompt": text})
    st.clear()
    req = parse_request(text)
    miss = _missing(req)
    if miss:
        st["req"] = req
        st["awaiting_params"] = True
        await ctx.user.ask_nowait(AssistantMessage(content=(
            "I author a **dry-run-validated** CWM golden-config workflow. I still need "
            + "; ".join(miss) + ".\n\nExample: `name=99.setSnmp devices=node-1,node-2 "
            "cli=\"snmp-server contact Demo\" check=\"node-1|show running-config snmp-server|Demo\"`")))
        return None
    await _say(ctx, "🧩 Parsed request: " + _summ(req))
    return await _build_and_gate(ctx, req)


if __name__ == "__main__":
    agent.start()
