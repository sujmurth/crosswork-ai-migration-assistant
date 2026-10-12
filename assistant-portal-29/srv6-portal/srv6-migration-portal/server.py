#!/usr/bin/env python3
"""
server.py -- SRv6 Migration Portal backend (Python stdlib only).

The operator supplies the CNC IP + username/password in the UI; the backend performs
the CAS login (TGT), mints a JWT per API call, and drives the CWM API. Credentials are
used server-side only and are never written to disk or logged.

Endpoints:
  GET  /                 -> UI
  GET  /api/config       -> non-secret defaults (cnc host, workflow, nso host)
  POST /api/login        -> {cncHost,cncPort,username,password} -> {ok,sid,user}
  POST /api/logout       -> {sid}
  POST /api/nl-parse     -> {sentence}          -> structured check
  POST /api/dry-run      -> {check}             -> run on device via NSO, pass/fail
  GET  /api/checks       -> list saved checks
  POST /api/checks       -> {check}             -> append to portal registry
  POST /api/workflows    -> {sid}               -> list CWM workflows (auth check)
  POST /api/start        -> {sid, input?}       -> start SRv6-Migration-MOP in CWM

Run: python3 server.py   (config.json overrides DEFAULTS)
"""
import json
import os
import re
import sys
import ssl
import time
import secrets
import threading
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PARENT)
import get_cnc_token as cas  # noqa: E402
import nl_check  # noqa: E402

DEFAULTS = {
    "cncHost": "198.18.134.221",
    "cncPort": 30603,
    "casService": None,                 # default: https://<host>:<port>/app-dashboard
    "workflowName": "SRv6-Migration-MOP",
    "workflowVersion": "1.0",
    "l3vpnName": "vpn-101",
    "l2vpnName": "vpn-129",
    "locator": "MAIN",
    "rrv6Ip": "2011::12",
    "nsoHost": "198.18.134.29",
    "nsoUser": "cisco",
    "nsoPass": "cisco",
    "ncsUser": "admin",
    "nsoLocal": False,
    "bindHost": "127.0.0.1",
    "port": 8077,
    "sessionTtl": 28800,                # 8h
}


def load_config():
    cfg = dict(DEFAULTS)
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        try:
            cfg.update(json.load(open(p)))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] bad config.json: {e}")
    return cfg


CFG = load_config()
REGISTRY = os.path.join(HERE, "portal-checks.json")
SESSION = {}   # sid -> {tgt, base, user, ts}


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def base_url(host=None, port=None):
    return f"https://{host or CFG['cncHost']}:{port or CFG['cncPort']}"


def mint_jwt(tgt, base):
    service = CFG.get("casService") or f"{base}/app-dashboard"
    return cas.get_jwt(base, tgt, service)


def cwm_request(method, path, jwt, base, body=None):
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=45) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


# ---------- NSO live-status via the CNC proxy ----------
# CNC proxies NSO RESTCONF at /crosswork/proxy/nso/restconf/... (Bearer JWT).
def _ned_restconf(ned):
    n = (ned or "tailf-ned-cisco-ios-xr-stats").strip()
    return n if n.startswith("tailf-ned-") else "tailf-ned-" + n


def _match_output(output, expect, mode):
    if not expect:
        return None
    out = output or ""
    if (mode or "contains") == "regex":
        try:
            return re.search(expect, out) is not None
        except re.error:
            return expect in out
    return expect in out


def nso_proxy_post(path, jwt, base, body):
    url = f"{base}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/yang-data+json",
        "Content-Type": "application/yang-data+json",
    })
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=45) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def nso_proxy_get(path, jwt, base):
    req = urllib.request.Request(f"{base}{path}", headers={
        "Authorization": f"Bearer {jwt}", "Accept": "application/yang-data+json"})
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=30) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def list_gc_apps(jwt, base):
    """Golden-config application instances on CNC's embedded NSO -> [{name, device}].
    Each entry is one config push applied to a device by the migration workflow."""
    p = "/crosswork/proxy/nso/restconf/data/golden-config:golden-config/application"
    for q in ("?fields=name;device", ""):
        st, txt = nso_proxy_get(p + q, jwt, base)
        if st == 200:
            try:
                return [{"name": a.get("name"), "device": a.get("device")}
                        for a in json.loads(txt).get("golden-config:application", [])]
            except Exception:  # noqa: BLE001
                pass
    return []


def cnc_live_status(check, jwt, base):
    """Run a check's show command on the device via CNC -> NSO live-status exec, and match."""
    node = check.get("node")
    command = check.get("command")
    if not (node and command):
        return {"ok": False, "matched": None, "output": "", "error": "check missing node/command"}
    ned = _ned_restconf(check.get("nedExec"))
    dev = urllib.parse.quote(node, safe="")
    path = (f"/crosswork/proxy/nso/restconf/operations/devices/device={dev}"
            f"/live-status/{ned}:exec/any")
    st, txt = nso_proxy_post(path, jwt, base, {"input": {"args": command}})
    if st not in (200, 201):
        return {"ok": False, "matched": None, "output": txt[:400], "error": f"HTTP {st}"}
    output = txt
    try:
        d = json.loads(txt)
        if isinstance(d, dict) and d:
            val = next(iter(d.values()))
            output = val.get("result") if isinstance(val, dict) else str(val)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "matched": _match_output(output, check.get("expect"),
            check.get("match", "contains")), "output": output or ""}


def run_check_authed(check, b):
    """Resolve the CNC session, mint a JWT, and run one check via CNC -> NSO live-status."""
    tgt, base, err = resolve(b)
    if err:
        return {"ok": False, "error": err, "matched": None, "output": ""}
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"auth failed: {e}", "matched": None, "output": ""}
    if not jwt or not jwt.startswith("eyJ"):
        return {"ok": False, "error": "could not mint JWT (session expired?)", "matched": None, "output": ""}
    return cnc_live_status(check, jwt, base)


# ---------- auth ----------
def do_login(b):
    host = (b.get("cncHost") or CFG["cncHost"]).strip()
    port = int(b.get("cncPort") or CFG["cncPort"])
    user = (b.get("username") or "admin").strip()
    pw = b.get("password") or ""
    if not pw:
        return {"ok": False, "error": "password required"}
    base = base_url(host, port)
    try:
        tgt = cas.get_tgt(base, user, pw)      # backend CAS login (URL-encoded, no shell)
    except SystemExit as e:                     # get_tgt exits with a message on failure
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"login error: {e}"}
    if not tgt:
        return {"ok": False, "error": "no TGT returned (check host/credentials)"}
    # sanity: confirm the TGT can actually mint a JWT
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"TGT obtained but JWT failed: {e}"}
    if not jwt or not jwt.startswith("eyJ"):
        return {"ok": False, "error": "TGT obtained but service ticket invalid"}
    sid = secrets.token_hex(16)
    SESSION[sid] = {"tgt": tgt, "base": base, "user": user, "ts": time.time()}
    return {"ok": True, "sid": sid, "user": user, "cnc": base}


def resolve(b):
    """Return (tgt, base, error) from a session id (or an inline tgt for compatibility)."""
    sid = b.get("sid")
    if sid and sid in SESSION:
        s = SESSION[sid]
        if time.time() - s["ts"] > CFG["sessionTtl"]:
            SESSION.pop(sid, None)
            return None, None, "session expired, please log in again"
        return s["tgt"], s["base"], None
    tgt = (b.get("tgt") or "").strip()
    if tgt:
        return tgt, base_url(b.get("cncHost"), b.get("cncPort")), None
    return None, None, "not authenticated -- log in first"


# ---------- CWM ----------
def find_workflow(jwt, name, base):
    status, txt = cwm_request("GET", "/crosswork/cwm/v2/workflow", jwt, base)
    if status != 200:
        return None, (status, txt)
    try:
        arr = json.loads(txt)
    except Exception:
        return None, (status, txt[:300])
    for wf in arr:
        if wf.get("name") == name:
            return wf, None
    return None, (status, f"workflow '{name}' not found")


def list_workflows(tgt, base):
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"auth failed: {e}"}
    if not jwt or not jwt.startswith("eyJ"):
        return {"ok": False, "error": "could not mint JWT (session expired?)"}
    status, txt = cwm_request("GET", "/crosswork/cwm/v2/workflow", jwt, base)
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}", "resp": txt[:300]}
    try:
        arr = json.loads(txt)
    except Exception:
        return {"ok": False, "error": "bad JSON"}
    ours = [{"name": w.get("name"), "version": w.get("version")}
            for w in arr if "SRv6" in (w.get("name") or "") or w.get("name") in (
                "validateChecks", "webexApprovalGate", "webexNotifyMigration")]
    return {"ok": True, "count": len(arr),
            "migration": sorted(ours, key=lambda x: x["name"] or "")}


def start_migration(tgt, base, wf_input):
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"auth failed: {e}"}
    if not jwt or not jwt.startswith("eyJ"):
        return {"ok": False, "error": "could not mint JWT (session expired?)"}
    wf, err = find_workflow(jwt, CFG["workflowName"], base)
    if not wf:
        return {"ok": False, "error": f"workflow lookup failed: {err}"}
    wid = wf.get("workflowId")
    name = wf.get("name")
    ver = wf.get("version", CFG["workflowVersion"])
    wf_input = wf_input or {}
    # Auto-inject a fresh CNC JWT so the Phase-2 node-3 DLM config-template deploy can
    # call the CNC config API without manual token passing. CWM uses the `data` field
    # for top-level workflow data (not `input`).
    wf_input = {**wf_input, "CNC_JWT": jwt}
    override = CFG.get("startPath")
    candidates = []
    if override:
        candidates.append(("POST", override, {"workflowName": name,
                          "workflowVersion": ver, "data": wf_input, "input": wf_input}))
    candidates += [
        ("POST", "/crosswork/cwm/v2/job",
         {"workflowName": name, "workflowVersion": ver, "data": wf_input, "input": wf_input}),
        ("POST", f"/crosswork/cwm/v2/workflow/{wid}/execute", {"data": wf_input, "input": wf_input}),
        ("POST", f"/crosswork/cwm/v2/workflow/{wid}/run", {"data": wf_input, "input": wf_input}),
        ("POST", "/crosswork/cwm/v2/execute", {"workflowId": wid, "data": wf_input, "input": wf_input}),
    ]
    attempts = []
    for method, path, body in candidates:
        status, txt = cwm_request(method, path, jwt, base, body)
        attempts.append({"path": path, "status": status, "resp": txt[:400]})
        if status in (200, 201, 202):
            return {"ok": True, "workflowId": wid, "version": ver,
                    "started": {"path": path, "status": status, "resp": txt[:600]},
                    "attempts": attempts}
    return {"ok": False, "error": "no start endpoint accepted the request; set "
            "'startPath' in config.json", "workflowId": wid, "attempts": attempts}


def apply_steps_to_workflow(tgt, base, wf_name, steps):
    """Best-effort: fetch the target CWM workflow, append the generated check tasks, and PUT
    it back. Guarded — if the workflow has no 'tasks' array we do NOT modify anything."""
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "applyError": f"auth failed: {e}"}
    if not jwt or not jwt.startswith("eyJ"):
        return {"applied": False, "applyError": "could not mint JWT (session expired?)"}
    wf, err = find_workflow(jwt, wf_name, base)
    if not wf:
        return {"applied": False, "applyError": f"workflow lookup failed: {err}"}
    tasks = wf.get("tasks")
    if not isinstance(tasks, list):
        return {"applied": False,
                "applyError": "workflow definition has no 'tasks' array; add the generated "
                              "steps manually after review", "workflowKeys": list(wf.keys())}
    existing = {t.get("taskReferenceName") for t in tasks}
    added = [s for s in steps if s.get("taskReferenceName") not in existing]
    if not added:
        return {"applied": False, "applyError": "all generated steps already present in the workflow"}
    wf["tasks"] = tasks + added
    wid = wf.get("workflowId")
    status, txt = cwm_request("PUT", f"/crosswork/cwm/v2/workflow/{wid}", jwt, base, wf)
    return {"applied": status in (200, 201, 202), "applyStatus": status,
            "applyResp": txt[:400], "added": [s["taskReferenceName"] for s in added]}


def to_workflow(b):
    """Convert authored check(s) into CWM workflow step(s). Returns the generated JSON for
    review; only mutates the live workflow when apply=True and a session is present."""
    wf_name = (b.get("workflow") or CFG["workflowName"]).strip()
    checks = b.get("checks")
    if not checks:
        one = b.get("check")
        checks = [one] if one else read_registry()
    checks = [c for c in checks if c and c.get("node") and c.get("command")]
    if not checks:
        return {"ok": False, "error": "no valid check to convert (need at least node + command)"}
    steps = [nl_check.to_workflow_step(c) for c in checks]
    res = {"ok": True, "workflow": wf_name, "stepCount": len(steps), "steps": steps}
    if b.get("apply"):
        tgt, base, err = resolve(b)
        if err:
            res["applied"] = False
            res["applyError"] = err
        else:
            res.update(apply_steps_to_workflow(tgt, base, wf_name, steps))
    return res


# ---------- config actions -> golden-config template ----------
GENERATED = os.path.join(HERE, "generated")


def action_generate(b):
    """Turn an authored config action into a golden-config template + per-device binding +
    a CWM config-push state. Writes the artifacts to generated/ and returns them for review."""
    action = b.get("action") or {}
    if not action.get("config", "").strip():
        return {"ok": False, "error": "action has no config body"}
    if b.get("resourceId"):
        action["resourceId"] = b["resourceId"]
    bundle = nl_check.to_golden(action)
    try:
        os.makedirs(GENERATED, exist_ok=True)
        base = bundle["templateName"]
        open(os.path.join(GENERATED, f"{base}.xml"), "w").write(bundle["templateXml"])
        open(os.path.join(GENERATED, f"{base}-application.xml"), "w").write(bundle["applicationXml"])
        json.dump(bundle["pushState"], open(os.path.join(GENERATED, f"{base}-pushstate.json"), "w"), indent=2)
        bundle["savedTo"] = f"generated/{base}.xml (+ -application.xml, -pushstate.json)"
    except Exception as e:  # noqa: BLE001
        bundle["saveError"] = str(e)
    bundle["ok"] = True
    return bundle


def action_create_in_nso(b):
    """Create the generated golden-config template in the NSO the portal runs on (the provider
    NSO used for golden-config). Uses local ncs_cli -- no CNC login required, but the NSO must
    have the golden-config package loaded."""
    action = b.get("action") or {}
    if not action.get("config", "").strip():
        return {"ok": False, "error": "action has no config body"}
    if not CFG.get("nsoLocal"):
        return {"ok": False, "error": "create-in-NSO only runs on the NSO host (nsoLocal); "
                "use the generated template XML to import it on the CNC embedded NSO instead"}
    xml = nl_check.to_golden_template(action)
    res = nl_check.create_golden_local(xml, ncs_user=CFG.get("ncsUser", "admin"))
    res["templateName"] = nl_check._tmpl_name(action)
    return res


def to_workflow_action(b):
    """Convert a config action into the CWM (Serverless Workflow) config-push state, optionally
    appending it to the target workflow's states when apply=True and logged in."""
    action = b.get("action") or {}
    if b.get("resourceId"):
        action["resourceId"] = b["resourceId"]
    if not action.get("config", "").strip():
        return {"ok": False, "error": "action has no config body"}
    state = nl_check.to_config_push_state(action)
    wf_name = (b.get("workflow") or CFG["workflowName"]).strip()
    res = {"ok": True, "workflow": wf_name, "state": state}
    if b.get("apply"):
        tgt, base, err = resolve(b)
        if err:
            res["applied"] = False
            res["applyError"] = err
        else:
            res.update(apply_state_to_workflow(tgt, base, wf_name, state))
    return res


def apply_state_to_workflow(tgt, base, wf_name, state):
    """Append a Serverless-Workflow state to the target workflow's definition.states, guarded."""
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "applyError": f"auth failed: {e}"}
    if not jwt or not jwt.startswith("eyJ"):
        return {"applied": False, "applyError": "could not mint JWT (session expired?)"}
    wf, err = find_workflow(jwt, wf_name, base)
    if not wf:
        return {"applied": False, "applyError": f"workflow lookup failed: {err}"}
    defn = wf.get("definition") if isinstance(wf.get("definition"), dict) else wf
    states = defn.get("states")
    if not isinstance(states, list):
        return {"applied": False, "applyError": "workflow has no 'definition.states' array; "
                "add the generated state manually after review", "workflowKeys": list(wf.keys())}
    if any(s.get("name") == state["name"] for s in states):
        return {"applied": False, "applyError": f"state '{state['name']}' already present"}
    states.append(state)
    wid = wf.get("workflowId")
    status, txt = cwm_request("PUT", f"/crosswork/cwm/v2/workflow/{wid}", jwt, base, wf)
    return {"applied": status in (200, 201, 202), "applyStatus": status,
            "applyResp": txt[:400], "added": state["name"]}


# ---------- webex ----------
WEBEX = "https://webexapis.com"


def _webex(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{WEBEX}{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=20) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def webex_check(b):
    """Validate a Webex token; then either validate a given roomId (and send a test
    message) or resolve a roomId from a space/title. Returns a UX-friendly result."""
    token = (b.get("token") or "").strip()
    room_id = (b.get("roomId") or "").strip()
    space = (b.get("spaceName") or "").strip()
    send_test = b.get("test", True)
    if not token:
        return {"ok": False, "error": "Webex token required"}

    st, txt = _webex("GET", "/v1/people/me", token)
    if st == 401:
        return {"ok": False, "error": "token invalid or expired (401)"}
    if st != 200:
        return {"ok": False, "error": f"token check failed (HTTP {st})", "resp": txt[:200]}
    me = json.loads(txt)
    ident = me.get("displayName") or (me.get("emails") or [None])[0]
    me_id = me.get("id")
    is_bot = me.get("type") == "bot"
    out = {"ok": True, "identity": ident, "isBot": is_bot}

    if not room_id and space:
        st, txt = _webex("GET", "/v1/rooms?max=200", token)
        if st != 200:
            return {"ok": False, "error": f"could not list rooms (HTTP {st})", "identity": ident}
        rooms = json.loads(txt).get("items", [])
        match = next((r for r in rooms if (r.get("title") or "").strip() == space), None)
        if not match:
            near = [r.get("title") for r in rooms if space.lower() in (r.get("title") or "").lower()][:5]
            return {"ok": False, "error": f"space '{space}' not found among the "
                    f"{len(rooms)} rooms the token can see", "identity": ident, "similar": near}
        out["resolvedRoomId"] = match.get("id")
        out["room"] = match.get("title")
        room_id = match.get("id")

    if room_id:
        st, txt = _webex("GET", f"/v1/rooms/{urllib.parse.quote(room_id, safe='')}", token)
        if st == 404:
            return {"ok": False, "error": "room not found, or the bot is not a member of it",
                    "identity": ident, "resolvedRoomId": out.get("resolvedRoomId")}
        if st != 200:
            return {"ok": False, "error": f"room check failed (HTTP {st})", "identity": ident}
        room = json.loads(txt)
        out["room"] = room.get("title")
        out["roomType"] = room.get("type")
        # membership check: is the authenticated bot/user actually in the room?
        mst, mtxt = _webex("GET",
                           f"/v1/memberships?roomId={urllib.parse.quote(room_id, safe='')}&max=200",
                           token)
        if mst == 200:
            members = json.loads(mtxt).get("items", [])
            out["members"] = len(members)
            out["botIsMember"] = any(m.get("personId") == me_id for m in members)
        if send_test:
            st, txt = _webex("POST", "/v1/messages", token, {
                "roomId": room_id,
                "markdown": "\u2705 **SRv6 Migration Portal** \u2014 Webex integration test OK. "
                            "Notifications and approval prompts will post here.",
            })
            out["testPosted"] = st in (200, 201)
            out["testStatus"] = st
            if st not in (200, 201):
                out["testError"] = txt[:200]
    return out


# ---------- webex approval relay ----------
# Bridges human replies in the Webex room to CWM's HTTP event ingress as
# 'webex-approval' CloudEvents, unblocking the workflow's interactive approval
# gates (webexApprovalGate). The relay polls the room for new messages and
# forwards decision-like replies (proceed/stop/...) as structured CloudEvents.
RELAY = {
    "running": False, "thread": None, "roomId": None, "token": None,
    "sid": None, "base": None, "seen": [], "posted": [], "polls": 0,
    "lastError": None, "startedAt": None, "interval": 5, "meId": None,
}
_RELAY_LOCK = threading.Lock()
DECISION_RE = re.compile(
    r"^\s*(proceed|approve[d]?|continue|resume|yes|go|ok|okay|"
    r"stop|hold|halt|pause|no|reject|deny|cancel|abort)\b", re.I)
EVENT_INGRESS = "/crosswork/cwm/v2/event/http"


def _relay_jwt():
    s = SESSION.get(RELAY["sid"])
    if not s:
        return None, "CNC session gone -- log in and restart the relay"
    if time.time() - s["ts"] > CFG["sessionTtl"]:
        return None, "CNC session expired -- log in and restart the relay"
    try:
        jwt = mint_jwt(s["tgt"], s["base"])
    except Exception as e:  # noqa: BLE001
        return None, f"JWT mint failed: {e}"
    if not jwt or not jwt.startswith("eyJ"):
        return None, "JWT mint failed"
    return jwt, None


def post_approval_event(text, person_email, base):
    """POST a webex-approval CloudEvent (structured mode) to the CWM HTTP event ingress."""
    jwt, err = _relay_jwt()
    if err:
        return 0, err
    ce = {
        "specversion": "1.0", "id": secrets.token_hex(16),
        "source": "webex-bot", "type": "cisco.cwm.webex.approval",
        "datacontenttype": "application/json",
        "data": {"text": text, "personEmail": person_email or "webex-user"},
    }
    req = urllib.request.Request(f"{base}{EVENT_INGRESS}", data=json.dumps(ce).encode(),
                                 method="POST", headers={
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/cloudevents+json",
    })
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=20) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200]


def _relay_poll_once():
    token = RELAY["token"]; room = RELAY["roomId"]; base = RELAY["base"]
    st, txt = _webex("GET", f"/v1/messages?roomId={urllib.parse.quote(room, safe='')}&max=50", token)
    if st != 200:
        RELAY["lastError"] = f"webex messages HTTP {st}"
        return
    items = json.loads(txt).get("items", [])
    for m in reversed(items):                    # oldest-first so replies forward in order
        mid = m.get("id")
        if not mid or mid in RELAY["seen"]:
            continue
        RELAY["seen"].append(mid)
        text = (m.get("text") or "").strip()
        if not DECISION_RE.match(text):
            continue                             # not a decision reply -> ignore (skips notifications)
        code, e = post_approval_event(text, m.get("personEmail"), base)
        RELAY["posted"].insert(0, {
            "ts": time.strftime("%H:%M:%S"), "person": m.get("personEmail") or "?",
            "text": text[:80], "status": code, "error": e or None})
        RELAY["posted"] = RELAY["posted"][:25]
        RELAY["lastError"] = None if code in (200, 201, 202) else f"event POST HTTP {code}: {e}"
    if len(RELAY["seen"]) > 500:
        RELAY["seen"] = RELAY["seen"][-500:]


def _relay_loop():
    while RELAY["running"]:
        try:
            _relay_poll_once()
            RELAY["polls"] += 1
        except Exception as e:  # noqa: BLE001
            RELAY["lastError"] = f"loop: {e}"
        for _ in range(max(1, int(RELAY["interval"] * 2))):   # responsive stop
            if not RELAY["running"]:
                break
            time.sleep(0.5)


def relay_start(b):
    token = (b.get("token") or "").strip()
    room = (b.get("roomId") or "").strip()
    sid = b.get("sid")
    if not token or not room:
        return {"ok": False, "error": "Webex token and roomId are required"}
    if not sid or sid not in SESSION:
        return {"ok": False, "error": "log in to CNC first (need a session to post events)"}
    st, txt = _webex("GET", "/v1/people/me", token)
    if st != 200:
        return {"ok": False, "error": f"Webex token invalid (HTTP {st})"}
    me = json.loads(txt)
    st, txt = _webex("GET", f"/v1/rooms/{urllib.parse.quote(room, safe='')}", token)
    if st != 200:
        return {"ok": False, "error": f"room not accessible, or identity not a member (HTTP {st})"}
    room_title = json.loads(txt).get("title")
    with _RELAY_LOCK:
        if RELAY["running"]:
            return {"ok": False, "error": "relay already running -- stop it first"}
        RELAY.update({"running": True, "roomId": room, "token": token, "sid": sid,
                      "base": SESSION[sid]["base"], "seen": [], "posted": [],
                      "polls": 0, "lastError": None, "meId": me.get("id"),
                      "interval": int(b.get("interval") or 5),
                      "startedAt": time.strftime("%Y-%m-%d %H:%M:%S")})
        s2, t2 = _webex("GET", f"/v1/messages?roomId={urllib.parse.quote(room, safe='')}&max=50", token)
        if s2 == 200:                            # seed 'seen' so only NEW replies forward
            RELAY["seen"] = [m.get("id") for m in json.loads(t2).get("items", []) if m.get("id")]
        th = threading.Thread(target=_relay_loop, daemon=True)
        RELAY["thread"] = th
        th.start()
    return {"ok": True, "running": True, "room": room_title or room, "seeded": len(RELAY["seen"]),
            "interval": RELAY["interval"], "ingress": EVENT_INGRESS}


def relay_stop():
    with _RELAY_LOCK:
        RELAY["running"] = False
        RELAY["token"] = None                    # drop the secret from memory
    return {"ok": True, "running": False}


def relay_status():
    return {"ok": True, "running": RELAY["running"], "room": RELAY["roomId"],
            "startedAt": RELAY["startedAt"], "polls": RELAY["polls"],
            "interval": RELAY["interval"], "seen": len(RELAY["seen"]),
            "posted": RELAY["posted"], "lastError": RELAY["lastError"],
            "ingress": EVENT_INGRESS}


# ---------- registry ----------
def read_registry():
    if os.path.exists(REGISTRY):
        try:
            return json.load(open(REGISTRY))
        except Exception:
            return []
    return []


def append_registry(check):
    checks = read_registry()
    checks.append(check)
    json.dump(checks, open(REGISTRY, "w"), indent=2)
    return checks


# ---------- L3VPN / L2VPN migration status ----------
VPN_CHECKS_FILE = os.path.join(HERE, "vpn-checks.json")


def read_vpn_checks():
    try:
        return json.load(open(VPN_CHECKS_FILE))
    except Exception:
        return {}


def _rollup(results):
    pre = [r for r in results if r["stage"] == "pre"]
    post = [r for r in results if r["stage"] == "post"]
    pre_pass = bool(pre) and all(r["matched"] is True for r in pre)
    post_pass = bool(post) and all(r["matched"] is True for r in post)
    # the first pre check is the baseline service-health indicator (service is up on MPLS)
    baseline_ok = bool(pre) and pre[0]["matched"] is True
    if post_pass:
        status = "migrated"          # SRv6 dataplane/control-plane in place
    elif baseline_ok:
        status = "baseline"          # MPLS service healthy, not yet migrated
    else:
        status = "attention"         # baseline service itself is down
    return pre_pass, post_pass, status


def vpn_status(b):
    """Run each L3VPN/L2VPN check on the device via CNC -> NSO live-status (read-only show) and
    roll up a per-service migration status: migrated / baseline / attention. Requires a CNC login."""
    tgt, base, err = resolve(b)
    if err:
        return {"ok": False, "error": err}
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"auth failed: {e}"}
    if not jwt or not jwt.startswith("eyJ"):
        return {"ok": False, "error": "could not mint JWT (session expired?)"}
    data = read_vpn_checks()
    out = {}
    for key in [k for k in data if not k.startswith("_")]:
        svc = data.get(key)
        if not isinstance(svc, dict) or "checks" not in svc:
            continue
        results = []
        for c in svc.get("checks", []):
            r = cnc_live_status(c, jwt, base)
            results.append({"name": c["name"], "stage": c["stage"], "command": c["command"],
                            "expect": c.get("expect"), "matched": r.get("matched"),
                            "error": r.get("error")})
        pre_pass, post_pass, status = _rollup(results)
        out[key] = {"service": svc.get("service"), "type": svc.get("type"), "status": status,
                    "prePass": pre_pass, "postPass": post_pass, "results": results}
    return {"ok": True, "vpns": out}


def vpn_seed_registry():
    """Add the L3VPN + L2VPN pre/post checks into the workflow check registry (portal-checks.json)."""
    data = read_vpn_checks()
    checks = read_registry()
    existing = {(c.get("name"), c.get("type")) for c in checks}
    added = 0
    for key in [k for k in data if not k.startswith("_")]:
        svc = data.get(key) or {}
        if not isinstance(svc, dict):
            continue
        for c in svc.get("checks", []):
            entry = {"name": c["name"], "phase": "5", "type": c["stage"], "node": c["node"],
                     "command": c["command"], "expect": c.get("expect"),
                     "match": c.get("match", "contains"), "nedExec": c.get("nedExec"),
                     "onFail": "halt", "service": svc.get("service"), "createdVia": "vpn-seed"}
            if (entry["name"], entry["type"]) not in existing:
                checks.append(entry)
                existing.add((entry["name"], entry["type"]))
                added += 1
    json.dump(checks, open(REGISTRY, "w"), indent=2)
    return {"ok": True, "added": added, "checks": checks}


# ---------- live migration progress + Webex notifications ----------
import base64  # noqa: E402

MOP_WF_ID = "94353bd6-360f-4974-87cb-f83c0e0a12f7"
PHASE_LABELS = ["Baseline", "Enable IPv6", "SRv6 Locators", "RRv6 Peers", "LocPref 50",
                "L3VPN -> SRv6", "L2VPN -> SRv6", "Cutover 150", "Remove MPLS"]

# Webex notify creds + per-run state; populated at /api/start, reused for start/error/complete posts.
NOTIFY = {"token": None, "roomId": None, "sid": None, "lastPhase": -1,
          "sent": set(), "gcSeen": set(), "gcLast": set(), "gcPrompted": set(), "watching": False}


def _phase_of(name):
    """Map a CWM sub-workflow name to a migration phase index (0-8), or None if phase-agnostic."""
    n = name or ""
    if "removeMPLS" in n or "08." in n:
        return 8
    if "updateRRv6" in n or "07." in n:
        return 7
    if "l2vpn" in n.lower():
        return 6
    if "migrateL3VPN" in n or "05." in n or "L3VPN" in n:
        return 5
    if "createRRv6" in n or "04a" in n:
        return 4
    if "enableRRv6" in n or "03." in n:
        return 3
    if "enableSRv6" in n or "02." in n:
        return 2
    if "enableIPV6" in n or "enableIPv6" in n or "01." in n:
        return 1
    return None


def _memo_field(j, name):
    try:
        return json.loads(base64.b64decode(j["memo"]["fields"][name]["data"]).decode())
    except Exception:
        return None


def mop_status(jwt, base):
    """Read the latest SRv6-Migration-MOP run from the CWM job API and roll it up into
    per-phase status (pending/current/done/failed) for the UI phase strip + notifications."""
    st, txt = cwm_request("GET", "/crosswork/cwm/v2/workflow", jwt, base)
    id2name = {}
    if st == 200:
        try:
            id2name = {w.get("workflowId"): w.get("name") for w in json.loads(txt)}
        except Exception:
            pass
    st, txt = cwm_request("GET", "/crosswork/cwm/v2/job", jwt, base)
    if st != 200:
        return {"ok": False, "error": f"job list HTTP {st}"}
    try:
        jobs = json.loads(txt)
    except Exception:
        return {"ok": False, "error": "bad job JSON"}
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs") or []
    parents = []
    for j in jobs:
        ex = j.get("execution") or {}
        if _memo_field(j, "WorkflowDefinitionID") == MOP_WF_ID and "_" not in ex.get("workflowId", ""):
            parents.append(j)
    if not parents:
        return {"ok": True, "jobId": None, "overall": None, "phases": [], "currentPhase": 0}
    parents.sort(key=lambda j: j.get("startTime", ""), reverse=True)
    parent = parents[0]
    job_id = (parent.get("execution") or {}).get("workflowId")
    overall = parent.get("status", "").replace("WORKFLOW_EXECUTION_STATUS_", "")
    per = {i: "pending" for i in range(9)}
    failed_steps = []
    for j in jobs:
        if job_id not in json.dumps(j):          # rootExecution / parentExecution references this run
            continue
        nm = id2name.get(_memo_field(j, "WorkflowDefinitionID"))
        s = j.get("status", "").replace("WORKFLOW_EXECUTION_STATUS_", "")
        if s == "FAILED" and nm:
            failed_steps.append(nm)
        ph = _phase_of(nm)
        if ph is None:
            continue
        if s == "FAILED":
            per[ph] = "failed"
        elif s == "RUNNING" and per[ph] != "failed":
            per[ph] = "current"
        elif s == "COMPLETED" and per[ph] == "pending":
            per[ph] = "done"
    reached = [i for i in range(9) if per[i] != "pending"]
    maxr = max(reached) if reached else 0
    per[0] = "done" if maxr >= 1 else ("current" if overall == "RUNNING" else "done")
    if overall == "COMPLETED":
        for i in range(9):
            if per[i] != "failed" and i <= maxr:
                per[i] = "done"
    phases = [{"index": i, "label": PHASE_LABELS[i], "status": per[i]} for i in range(9)]
    return {"ok": True, "jobId": job_id, "overall": overall, "currentPhase": maxr,
            "phases": phases, "failedSteps": sorted(set(failed_steps)),
            "startTime": parent.get("startTime"), "closeTime": parent.get("closeTime")}


def migration_status(b):
    tgt, base, err = resolve(b)
    if err:
        return {"ok": False, "error": err}
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"auth failed: {e}"}
    if not jwt or not jwt.startswith("eyJ"):
        return {"ok": False, "error": "could not mint JWT (session expired?)"}
    return mop_status(jwt, base)


def webex_notify(text):
    """Post a markdown message to the notify room (falls back to the relay's creds)."""
    token = NOTIFY.get("token") or RELAY.get("token")
    room = NOTIFY.get("roomId") or RELAY.get("roomId")
    if not token or not room:
        return False
    st, _ = _webex("POST", "/v1/messages", token, {"roomId": room, "markdown": text})
    return st in (200, 201)


def _notify_watch():
    """Background watcher: polls the MOP run and posts Webex updates on phase change / error / done."""
    NOTIFY["watching"] = True
    end = time.time() + 3600
    try:
        while time.time() < end:
            s = SESSION.get(NOTIFY["sid"])
            if not s:
                break
            try:
                jwt = mint_jwt(s["tgt"], s["base"])
            except Exception:  # noqa: BLE001
                time.sleep(8)
                continue
            # per-node golden-config apply notifications + approval prompt when a phase settles
            try:
                apps = list_gc_apps(jwt, s["base"])
                names_now = {a.get("name") for a in apps if a.get("name")}
                for a in apps:
                    nm = a.get("name")
                    if nm and nm not in NOTIFY["gcSeen"]:
                        NOTIFY["gcSeen"].add(nm)
                        dev = a.get("device") or "?"
                        action = (nm or "").replace("cwm-gc-", "")
                        if dev and action.endswith("-" + dev):
                            action = action[:-(len(dev) + 1)]
                        webex_notify("\U0001F527 **Config applied** to **%s** \u2014 %s (golden-config)."
                                     % (dev, action or nm))
                # config for a phase has settled (no new apps since last poll) -> the gate is waiting
                if names_now and names_now == NOTIFY["gcLast"]:
                    acts = {}
                    for nm in names_now:
                        act = re.sub(r"-node-\d+$", "", nm.replace("cwm-gc-", ""))
                        acts[act] = acts.get(act, 0) + 1
                    for act in acts:
                        if act not in NOTIFY["gcPrompted"]:
                            NOTIFY["gcPrompted"].add(act)
                            webex_notify("\u23f8 **Approval needed** \u2014 '%s' applied to %d node(s). "
                                         "Reply **proceed** (mention the bot) to continue, or **stop** to hold."
                                         % (act, acts[act]))
                NOTIFY["gcLast"] = names_now
            except Exception:  # noqa: BLE001
                pass
            stt = mop_status(jwt, s["base"])
            if stt.get("ok") and stt.get("jobId"):
                overall = stt.get("overall")
                cur = stt.get("currentPhase", 0)
                if cur > NOTIFY["lastPhase"] and ("phase-%d" % cur) not in NOTIFY["sent"]:
                    NOTIFY["sent"].add("phase-%d" % cur)
                    NOTIFY["lastPhase"] = cur
                    lbl = stt["phases"][cur]["label"] if stt.get("phases") else str(cur)
                    webex_notify("\u25b6 **SRv6 Migration** \u2014 Phase %d **%s** in progress." % (cur, lbl))
                if overall == "FAILED" and "failed" not in NOTIFY["sent"]:
                    NOTIFY["sent"].add("failed")
                    fs = ", ".join(stt.get("failedSteps") or []) or "unknown step"
                    lbl = stt["phases"][cur]["label"] if stt.get("phases") else str(cur)
                    webex_notify("\u274c **SRv6 Migration FAILED** at **%s** \u2014 failing step(s): %s. "
                                 "Check the portal + CWM job." % (lbl, fs))
                    break
                if overall == "COMPLETED" and "done" not in NOTIFY["sent"]:
                    NOTIFY["sent"].add("done")
                    webex_notify("\u2705 **SRv6 Migration COMPLETED** \u2014 all phases passed.")
                    break
            time.sleep(8)
    finally:
        NOTIFY["watching"] = False


def start_and_notify(tgt, base, b):
    """Start the MOP, then post a 'started' Webex notice and launch the progress watcher."""
    res = start_migration(tgt, base, b.get("input"))
    if res.get("ok"):
        inp = b.get("input") or {}
        NOTIFY.update({
            "token": inp.get("WEBEX_ACCESS_TOKEN") or RELAY.get("token"),
            "roomId": inp.get("WEBEX_ROOM_ID") or RELAY.get("roomId"),
            "sid": b.get("sid"), "lastPhase": -1, "sent": set(),
        })
        try:
            cur = list_gc_apps(mint_jwt(tgt, base), base)
            NOTIFY["gcSeen"] = {a["name"] for a in cur if a.get("name")}
            NOTIFY["gcLast"] = set()
            NOTIFY["gcPrompted"] = {re.sub(r"-node-\d+$", "", a["name"].replace("cwm-gc-", ""))
                                    for a in cur if a.get("name")}
        except Exception:  # noqa: BLE001
            NOTIFY["gcSeen"] = set(); NOTIFY["gcLast"] = set(); NOTIFY["gcPrompted"] = set()
        webex_notify("\U0001F680 **SRv6 Migration STARTED** \u2014 baseline validation underway. "
                     "Phase updates and any errors will post here.")
        if not NOTIFY["watching"]:
            threading.Thread(target=_notify_watch, daemon=True).start()
    return res


# ---------- grounding: devices / SRv6 locators / VPN services ----------
CHECKS_MOP = os.path.join(PARENT, "CWM-MOP-Workflows", "checks-srv6-migration.json")


def _authed_jwt(b):
    """Resolve the CNC session and mint a JWT, or return an error string."""
    tgt, base, err = resolve(b)
    if err:
        return None, None, err
    try:
        jwt = mint_jwt(tgt, base)
    except Exception as e:  # noqa: BLE001
        return None, None, f"auth failed: {e}"
    if not jwt or not jwt.startswith("eyJ"):
        return None, None, "could not mint JWT (session expired?)"
    return jwt, base, None


def list_devices(b):
    """Live NSO-managed device names (via the CNC proxy) so the assistant can offer real nodes."""
    jwt, base, err = _authed_jwt(b)
    if err:
        return {"ok": False, "error": err, "devices": []}
    st, txt = nso_proxy_get(
        "/crosswork/proxy/nso/restconf/data/tailf-ncs:devices/device?fields=name", jwt, base)
    if st != 200:
        return {"ok": False, "error": f"HTTP {st}", "devices": []}
    try:
        devs = [d.get("name") for d in json.loads(txt).get("tailf-ncs:device", [])]
    except Exception:  # noqa: BLE001
        devs = []
    return {"ok": True, "devices": [d for d in devs if d]}


def list_srv6_locators(b):
    """SRv6 locator names, read live from a PE device when possible, else the known set."""
    jwt, base, err = _authed_jwt(b)
    if err:
        return {"ok": False, "error": err, "locators": ["MAIN", "TOP", "BOTTOM", "LATENCY"]}
    node = (b.get("node") or CFG.get("pe") or "node-2").split(",")[0].strip()
    res = cnc_live_status(
        {"node": node, "command": "show segment-routing srv6 locator"}, jwt, base)
    found = []
    if res.get("ok"):
        for line in (res.get("output") or "").splitlines():
            m = re.match(r"\s*([A-Za-z][\w-]+)\s+\d+\s+\d+\s+fcbb", line)
            if m:
                found.append(m.group(1))
    locs, seen = [], set()
    for l in (found or ["MAIN", "TOP", "BOTTOM", "LATENCY"]):
        if l not in seen:
            seen.add(l)
            locs.append(l)
    return {"ok": True, "locators": locs, "source": "device" if found else "default"}


# ---------- CNC DLM Configuration Templates (CLI transport, base /crosswork/config/v1) ----------
# Used to push config the NSO NED can't model (e.g. IOS-XE SRv6 locator `algorithm N`).
def _device_uuid(name, jwt, base):
    """Resolve a device's CNC inventory UUID by host name (handles .nso-topology suffix)."""
    st, txt = cwm_request("POST", "/crosswork/inventory/v1/nodes/query", jwt, base, {})
    if st != 200:
        return None
    try:
        for n in json.loads(txt).get("data", []):
            h = n.get("host_name", "") or ""
            if h == name or h.split(".")[0] == name:
                return n.get("uuid")
    except Exception:  # noqa: BLE001
        pass
    return None


def list_templates(b):
    """CNC DLM config templates (CLI transport) -> [names]."""
    jwt, base, err = _authed_jwt(b)
    if err:
        return {"ok": False, "error": err, "templates": []}
    st, txt = cwm_request("POST", "/crosswork/config/v1/templates/query?page=0&size=100",
                          jwt, base, {"filter": {}})
    names = []
    if st == 200:
        try:
            names = [t.get("name") for t in json.loads(txt).get("template", [])]
        except Exception:  # noqa: BLE001
            pass
    return {"ok": st == 200, "templates": [n for n in names if n]}


def template_deploy(b):
    """Deploy a DLM config template to a device (CLI transport). Returns {ok, jobId, device}."""
    jwt, base, err = _authed_jwt(b)
    if err:
        return {"ok": False, "error": err}
    device = b.get("device") or "node-3"
    uuid = _device_uuid(device, jwt, base)
    if not uuid:
        return {"ok": False, "error": f"no inventory UUID for {device}"}
    variables = [{"name": k, "value": str(v)}
                 for k, v in (b.get("variables") or {"node_id": "3"}).items()]
    body = {
        "template_name": b.get("template") or "srv6-flexalgo-iosxe",
        "version": "1",
        "device_uuids": {"device_uuids": [uuid]},
        "details": [{"version": 1, "device_uuid": "GLOBAL", "variables": variables}],
        "additional_params": {"backup_before_deploy": False,
                              "rollback_on_failure": True, "nso_sync": False},
    }
    st, txt = cwm_request("POST", "/crosswork/config/v1/templates/deploy-template", jwt, base, body)
    if st not in (200, 202):
        return {"ok": False, "error": f"HTTP {st}", "resp": txt[:300]}
    try:
        return {"ok": True, "jobId": json.loads(txt).get("job_id"), "device": device}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "no job_id", "resp": txt[:200]}


def template_status(b):
    """Poll a config-template deploy job: last_run_status (SUCCESS/RUN_FAILED/PARTIAL/IN_PROGRESS)."""
    jwt, base, err = _authed_jwt(b)
    if err:
        return {"ok": False, "error": err}
    job = b.get("jobId")
    st, txt = cwm_request(
        "POST", "/crosswork/config/v1/templates/jobs/deployments/query", jwt, base,
        {"name": job, "last_run_status": ["NOT_STARTED", "IN_PROGRESS", "SUCCESS",
                                          "RUN_FAILED", "PARTIAL"]})
    status = None
    if st == 200:
        try:
            jobs = json.loads(txt).get("jobs", [])
            status = jobs[0].get("last_run_status") if jobs else None
        except Exception:  # noqa: BLE001
            pass
    return {"ok": st == 200, "jobId": job, "status": status}


def list_vpn_services(b):
    """L3VPN / L2VPN service names from the portal's VPN check registry + config defaults."""
    data = read_vpn_checks()
    l3, l2 = [], []
    for k, v in data.items():
        if k.startswith("_") or not isinstance(v, dict) or "checks" not in v:
            continue
        blob = json.dumps(v).lower()
        (l2 if ("l2vpn" in blob or "xconnect" in blob or "evpn" in blob or "l2" in k.lower())
         else l3).append(v.get("service") or k)
    if not l3 and CFG.get("l3vpnName"):
        l3 = [CFG["l3vpnName"]]
    if not l2 and CFG.get("l2vpnName"):
        l2 = [CFG["l2vpnName"]]
    return {"ok": True, "l3vpn": sorted(set(l3)), "l2vpn": sorted(set(l2))}


# ---------- MOP phase check registry (pre/post per phase) ----------
def read_phase_checks():
    try:
        return json.load(open(CHECKS_MOP))
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def phase_checks(_b=None):
    """Return the editable per-phase pre/post check registry the MOP consumes."""
    reg = read_phase_checks()
    if "_error" in reg:
        return {"ok": False, "error": reg["_error"], "phases": []}
    phases = [{"phase": k, "action_workflow": v.get("action_workflow"),
               "pre": v.get("pre", []), "post": v.get("post", [])}
              for k, v in reg.items()
              if not k.startswith("_") and isinstance(v, dict)]
    return {"ok": True, "phases": phases}


def phase_check_add(b):
    """Append a pre/post check to a phase in checks-srv6-migration.json (the MOP's registry)."""
    phase = str(b.get("phase") or "").strip()
    stage = (b.get("stage") or "post").lower()
    check = b.get("check") or {}
    if stage not in ("pre", "post"):
        return {"ok": False, "error": "stage must be 'pre' or 'post'"}
    if not (check.get("node") and check.get("command")):
        return {"ok": False, "error": "check needs at least node + command"}
    reg = read_phase_checks()
    if "_error" in reg:
        return {"ok": False, "error": reg["_error"]}
    keys = [k for k in reg if not k.startswith("_") and isinstance(reg[k], dict)]
    if phase not in reg:
        cands = [k for k in keys if k == phase or k.startswith(phase + "_")
                 or phase in k or k.split("_")[0] == "phase" + phase]
        if len(cands) == 1:
            phase = cands[0]
        else:
            return {"ok": False, "error": f"unknown phase '{phase}'. Known: {', '.join(keys)}"}
    entry = {"name": check.get("name") or f"custom-{stage}-{len(reg[phase].get(stage, [])) + 1}",
             "node": check["node"], "command": check["command"],
             "expect": check.get("expect", ".*"), "phase": str(b.get("phaseNum", ""))}
    if check.get("nedExec"):
        entry["nedExec"] = check["nedExec"]
    reg[phase].setdefault(stage, []).append(entry)
    try:
        json.dump(reg, open(CHECKS_MOP, "w"), indent=2)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"write failed: {e}"}
    return {"ok": True, "phase": phase, "stage": stage, "added": entry,
            "count": len(reg[phase][stage])}


# ---------- workflow-driven question plan (per phase) ----------
# Maps each MOP phase (0-8) to the operator questions it actually needs. Derived from the
# phase actions + golden-config template variables so the assistant asks only what's relevant.
PHASE_PARAM_CATALOG = {
    1: [
        {"key": "igp", "group": "Devices", "def": "node-1..node-8",
         "prompt": "Which **devices** get IPv6 (the IGP fabric)?"},
        {"key": "ipv6LoopScheme", "group": "IPv6", "def": "2010::x / 2011::x",
         "prompt": "**IPv6 loopback scheme** (Loopback0 / Loopback2 prefixes)?"},
    ],
    2: [
        {"key": "locator", "group": "SRv6", "def": "MAIN",
         "options": ["MAIN", "TOP", "BOTTOM", "LATENCY"],
         "prompt": "Which **SRv6 locator** should services use?"},
        {"key": "isis", "group": "SRv6", "def": "1",
         "prompt": "**IS-IS instance** to carry SRv6?"},
    ],
    3: [
        {"key": "rrv6", "group": "RRv6", "def": "2011::11",
         "prompt": "**RRv6 route-reflector** IPv6 address?"},
        {"key": "bgpAs", "group": "RRv6", "def": "65000",
         "prompt": "**BGP AS** number?"},
    ],
    4: [
        {"key": "locpref50", "group": "Policy", "def": "50",
         "prompt": "**Standby LocPref** for the dual-plane stage (SRv6 as backup)?"},
    ],
    5: [
        {"key": "pe", "group": "Devices", "def": "node-2, node-4",
         "prompt": "Which are the **PE nodes** carrying the L3VPN?"},
        {"key": "l3vpn", "group": "Services", "def": "vpn-101",
         "prompt": "**L3VPN** service name to migrate?"},
    ],
    6: [
        {"key": "l2vpn", "group": "Services", "def": "vpn-129",
         "prompt": "**L2VPN (EVPN-VPWS)** service name?"},
        {"key": "evi", "group": "Services", "def": "4129",
         "prompt": "EVPN **EVI id**?"},
        {"key": "l2vpnTargetSource", "group": "Services",
         "def": "node-2 target 2 source 1; node-4 target 1 source 2",
         "prompt": "Per-PE EVPN **target/source** values?"},
        {"key": "ac", "group": "Services", "def": "Gi0/0/0/11.2129",
         "prompt": "**Attachment-circuit** interface for the L2VPN?"},
    ],
    7: [
        {"key": "locpref150", "group": "Policy", "def": "150",
         "prompt": "**Cutover LocPref** (SRv6 becomes best path)?"},
    ],
    8: [
        {"key": "removeMpls", "group": "Cutover", "def": "Yes, remove MPLS",
         "options": ["Yes, remove MPLS", "Keep MPLS"],
         "prompt": "Confirm **MPLS/LDP removal** on the fabric?"},
    ],
}


def phase_plan(_b=None):
    """Per-phase question plan: labels from the check registry + params from the catalog.
    The assistant filters this by the phases the operator selects, so it asks exactly the
    appropriate questions for the chosen scope."""
    reg = read_phase_checks()
    labels = {i: PHASE_LABELS[i] for i in range(len(PHASE_LABELS))}
    # enrich labels/action from the registry when available
    order = ["phase0_baseline", "phase1_enableIPv6", "phase2_enableSRv6",
             "phase3_enableRRv6Peers", "phase4_rrv6Policy_locpref50",
             "phase5_migrateL3VPN_perVRF", "phase5b_migrateL2VPN_evpnVpws",
             "phase6_cutover_locpref150", "phase7_removeMPLS"]
    actions = {}
    if "_error" not in reg:
        for i, key in enumerate(order):
            v = reg.get(key)
            if isinstance(v, dict):
                actions[i] = v.get("action_workflow")
    phases = []
    for i in range(9):
        phases.append({
            "id": i, "label": labels.get(i, f"Phase {i}"),
            "action": actions.get(i),
            "params": PHASE_PARAM_CATALOG.get(i, []),
        })
    return {"ok": True, "phases": phases}


# ---------- create a new CWM workflow ----------
def new_workflow(b):
    """Create a minimal validateChecks-based CWM workflow from a name (+ optional checks)."""
    jwt, base, err = _authed_jwt(b)
    if err:
        return {"ok": False, "error": err}
    name = (b.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "workflow name required"}
    desc = b.get("description") or f"Custom workflow '{name}' created via the migration portal."
    checks = b.get("checks") or []
    label = b.get("phaseLabel") or name.lower().replace(" ", "-")
    jq_input = "${ . + { phaseLabel: \"%s\", checks: %s } }" % (label, json.dumps(checks))
    definition = {
        "id": f"{name}-1.0", "name": name, "version": "1.0", "specVersion": "0.8",
        "description": desc, "start": "runChecks",
        "states": [{
            "name": "runChecks", "type": "operation", "end": {"terminate": True},
            "actions": [{"subFlowRef": {"invoke": "sync", "version": "1.0",
                                        "workflowId": "validateChecks"}}],
            "stateDataFilter": {"input": jq_input},
        }],
    }
    st, txt = cwm_request("POST", "/crosswork/cwm/v2/workflowImport", jwt, base,
                          [{"definition": definition, "wfTags": ["custom", "portal-created"]}])
    if st in (200, 201, 202):
        return {"ok": True, "name": name, "status": st, "resp": txt[:300]}
    st2, txt2 = cwm_request("POST", "/crosswork/cwm/v2/workflow", jwt, base,
                            {"definition": definition, "wfTags": ["custom", "portal-created"]})
    if st2 in (200, 201, 202):
        return {"ok": True, "name": name, "status": st2, "resp": txt2[:300]}
    return {"ok": False, "error": f"import HTTP {st}", "resp": txt[:400]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _static(self, name, ctype):
        p = os.path.join(HERE, "static", name)
        if not os.path.exists(p):
            return self._send(404, {"error": "not found"})
        self._send(200, open(p, "rb").read(), ctype)

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            return self._send(200, {
                "cncHost": CFG["cncHost"], "cncPort": CFG["cncPort"],
                "workflowName": CFG["workflowName"], "nsoHost": CFG["nsoHost"],
                "l3vpnName": CFG["l3vpnName"], "l2vpnName": CFG["l2vpnName"],
                "locator": CFG["locator"], "rrv6Ip": CFG["rrv6Ip"],
            })
        if path == "/api/checks":
            return self._send(200, {"checks": read_registry()})
        if path == "/api/vpn-checks":
            return self._send(200, read_vpn_checks())
        if path == "/api/relay/status":
            return self._send(200, relay_status())
        if path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        # ---- static files (index + assets under static/, incl. /cnc/*) ----
        rel = (path.lstrip("/") or "index.html")
        safe = os.path.normpath(rel)
        if safe.startswith("..") or os.path.isabs(safe):
            return self._send(404, {"error": "not found"})
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else "html"
        ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css",
                 "json": "application/json", "png": "image/png", "svg": "image/svg+xml",
                 "woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf",
                 "ico": "image/x-icon"}.get(ext, "text/plain")
        return self._static(safe, ctype)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        b = self._body()
        if path == "/api/login":
            return self._send(200, do_login(b))
        if path == "/api/logout":
            SESSION.pop(b.get("sid"), None)
            return self._send(200, {"ok": True})
        if path == "/api/nl-parse":
            return self._send(200, nl_check.parse_check(b.get("sentence", "")))
        if path == "/api/webex-check":
            return self._send(200, webex_check(b))
        if path == "/api/relay/start":
            return self._send(200, relay_start(b))
        if path == "/api/relay/stop":
            return self._send(200, relay_stop())
        if path == "/api/relay/status":
            return self._send(200, relay_status())
        if path == "/api/dry-run":
            return self._send(200, run_check_authed(b.get("check", {}), b))
        if path == "/api/checks":
            return self._send(200, {"checks": append_registry(b.get("check", {}))})
        if path == "/api/to-workflow":
            return self._send(200, to_workflow(b))
        if path == "/api/nl-action":
            return self._send(200, nl_check.parse_action(b.get("sentence", "")))
        if path == "/api/action-generate":
            return self._send(200, action_generate(b))
        if path == "/api/action-to-workflow":
            return self._send(200, to_workflow_action(b))
        if path == "/api/action-create-nso":
            return self._send(200, action_create_in_nso(b))
        if path == "/api/vpn-status":
            return self._send(200, vpn_status(b))
        if path == "/api/migration-status":
            return self._send(200, migration_status(b))
        if path == "/api/devices":
            return self._send(200, list_devices(b))
        if path == "/api/srv6-locators":
            return self._send(200, list_srv6_locators(b))
        if path == "/api/vpn-services":
            return self._send(200, list_vpn_services(b))
        if path == "/api/templates":
            return self._send(200, list_templates(b))
        if path == "/api/template-deploy":
            return self._send(200, template_deploy(b))
        if path == "/api/template-status":
            return self._send(200, template_status(b))
        if path == "/api/phase-checks":
            return self._send(200, phase_checks(b))
        if path == "/api/phase-plan":
            return self._send(200, phase_plan(b))
        if path == "/api/phase-check-add":
            return self._send(200, phase_check_add(b))
        if path == "/api/new-workflow":
            return self._send(200, new_workflow(b))
        if path == "/api/vpn-seed":
            return self._send(200, vpn_seed_registry())
        if path == "/api/workflows":
            tgt, base, err = resolve(b)
            if err:
                return self._send(200, {"ok": False, "error": err})
            return self._send(200, list_workflows(tgt, base))
        if path == "/api/start":
            tgt, base, err = resolve(b)
            if err:
                return self._send(200, {"ok": False, "error": err})
            return self._send(200, start_and_notify(tgt, base, b))
        return self._send(404, {"error": "not found"})


def main():
    port = int(CFG.get("port", 8077))
    host = CFG.get("bindHost", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"SRv6 Migration Portal -> http://{host}:{port}")
    print(f"  default CNC={base_url()}  workflow={CFG['workflowName']}  "
          f"NSO={'local' if CFG.get('nsoLocal') else CFG['nsoHost']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
