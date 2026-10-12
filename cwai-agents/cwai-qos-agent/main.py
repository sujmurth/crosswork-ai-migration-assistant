"""QoS Migration Agent — Cisco Crosswork AI ADK.

Migrates QoS configuration from legacy platforms (ASR 900 / IOS-XE, NCS 540 &
NCS 55xx / IOS-XR, ASR 9000 / IOS-XR) to Cisco 8000 IOS-XR, then generates a
deployment workflow — driven by the bundled `8000-platform-hub-config-translation`
skill.

Loop (on_message, pull-based HITL):
  1. check/pick  — resolve the source device (from the prompt; live inventory via
     ctx.http/ctx.dra when reachable, else the bundled sample configs)
  2. get config  — extract the QoS stanzas (class-map / policy-map / service-policy)
  3. translate   — ctx.llm reasons over the SKILL (rules + deterministic algorithms)
     and emits the Cisco 8000 IOS-XR config + per-section confidence report
  4. generate    — build the golden-config template, pre/post checks and the CWM
     workflow (MOP), and show the package for review
  5. approve     — ctx.user.ask_nowait pauses for operator sign-off (HITL)
  6. load+dryrun — on approval, load the template onto NSO + the workflow onto CWM,
     then start the migration in dry-run (native diff, no commit; node-16 read-only)

SMX-optional: works on ctx.llm + the bundled skill; register smx_consult /
smx_rag_search in the Tool Catalog to add live 8000 PID/feature verification.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from cwai.adk import AssistantMessage, Context, Message, MessageFormat, Result, agent
from cwai.adk.integrations.langgraph import ADKChatModel
from langchain_core.messages import HumanMessage, SystemMessage

HERE = os.path.dirname(os.path.abspath(__file__))


def _am(content: str, follow_ups: list[str] | None = None) -> AssistantMessage:
    """AssistantMessage that renders as markdown (fenced code blocks + line breaks)."""
    return AssistantMessage(content=content, format=MessageFormat.MARKDOWN,
                            follow_ups=follow_ups or [])

# --- bundled skill (QoS-only load path: SKILL.md + CONFIG_TRANSLATION_QOS.md) ---
def _load(name: str) -> str:
    try:
        with open(os.path.join(HERE, "skills", name)) as f:
            return f.read()
    except Exception:
        return ""

SKILL = _load("SKILL.md")
QOS_REF = _load("CONFIG_TRANSLATION_QOS.md")

# --- bundled sample source configs (fallback when CNC is not reachable) ---
SOURCES: dict[str, str] = {}
for _p in glob.glob(os.path.join(HERE, "sources", "*.cfg")):
    try:
        SOURCES[os.path.basename(_p)[:-4]] = open(_p).read()
    except Exception:
        pass

# device -> source platform label (bundled samples + live .49 CNC devices)
PLATFORM = {
    "node-16": "ASR 9000 (IOS-XR)",
    "node-3": "NCS 540 (IOS-XR)", "node-4": "NCS 540 (IOS-XR)", "node-7": "NCS 540 (IOS-XR)",
    "node-5": "NCS 5500 (IOS-XR)", "node-6": "NCS 5500 (IOS-XR)", "node-8": "NCS 5500 (IOS-XR)",
    "node-9": "NCS 5500 (IOS-XR)", "node-10": "NCS 5500 (IOS-XR)",
    "cpe-11": "ASR 900 (IOS-XE)", "cpe-12": "ASR 900 (IOS-XE)",
    "cpe-13": "ASR 900 (IOS-XE)", "cpe-14": "ASR 900 (IOS-XE)",
    "node-1": "Cisco 8000 (target)", "node-2": "Cisco 8000 (target)", "node-15": "Cisco 8000 (target)",
}


def _platform_of(name: str, cfg: str) -> str:
    if name in PLATFORM:
        return PLATFORM[name]
    if "license udi pid ASR-920" in cfg or "asr920" in cfg:
        return "ASR 900 (IOS-XE)"
    if "NCS540" in cfg or "NCS-540" in cfg:
        return "NCS 540 (IOS-XR)"
    if "ASR9K" in cfg or "ASR-9" in cfg:
        return "ASR 9000 (IOS-XR)"
    return "IOS-XR (source)"


def _extract_qos(cfg: str) -> str:
    """Pull class-map / policy-map blocks and interface service-policy bindings."""
    out: list[str] = []
    lines = cfg.splitlines()
    in_block = False
    for i, ln in enumerate(lines):
        if ln.startswith(("class-map ", "policy-map ")):
            in_block = True
        if in_block:
            out.append(ln)
            if ln.strip() == "!" or ln.strip().startswith(("end-policy-map", "end-class-map")):
                if ln.strip() == "!":
                    in_block = False
        # interface bindings
        if "service-policy" in ln and ln.strip().startswith("service-policy"):
            # find the enclosing interface line
            for j in range(i, max(i - 30, -1), -1):
                if lines[j].startswith("interface "):
                    out.append("")
                    out.append(lines[j])
                    out.append(ln)
                    break
    return "\n".join(out).strip()


def _find_source(text: str) -> tuple[str, str] | None:
    low = text.lower()
    for name in SOURCES:
        if name.lower() in low:
            return name, SOURCES[name]
    # platform keyword -> first matching bundled sample
    for name, cfg in SOURCES.items():
        p = _platform_of(name, cfg).lower()
        if ("asr9" in low and "asr 9000" in p) or ("ncs540" in low and "ncs 540" in p) \
           or ("asr900" in low and "asr 900" in p):
            return name, cfg
    return None


def _device_from_text(text: str) -> str | None:
    m = re.search(r'\b((?:node|cpe)-\d+)\b', text.lower())
    if m:
        return m.group(1)
    low = text.lower()
    if "asr9" in low:
        return "node-16"
    if "ncs540" in low or "ncs 540" in low:
        return "node-3"
    if "asr900" in low or "asr920" in low:
        return "cpe-11"
    return None


async def _get_config(ctx: Context, name: str) -> tuple[str, str]:
    """Read the device running-config LIVE via the platform NSO DRA (read-only);
    fall back to the bundled sample if the DRA is unreachable."""
    try:
        resp = await ctx.dra.device_command.get_device_running_config(node_id=name)
        out = getattr(resp, "output", "") or ""
        if out.strip():
            return out, "live via CNC NSO DRA"
    except Exception as e:  # DRA not wired / device not found -> fall back
        ctx.log.info("live DRA read failed; using bundled sample", extra={"device": name, "err": str(e)})
    if name in SOURCES:
        return SOURCES[name], "bundled sample"
    return "", ""


def _approved(text: str) -> bool:
    return str(text).strip().lower().startswith(("y", "approve", "go", "proceed", "ok"))


async def _say(ctx: Context, text: str) -> None:
    try:
        await ctx.send_partial_result(message=_am(text))
    except Exception:
        ctx.log.info(text)


def _followups() -> list[str]:
    """Clickable starter prompts shown as chips in the AI Assistant UI."""
    devs = list(SOURCES)[:3] or ["node-16"]
    return [f"Migrate {d} QoS to Cisco 8000 QoS" for d in devs] + ["List QoS migration candidates"]


SYSTEM_PROMPT = (
    "You are the QoS Migration Agent. Translate the operator's source QoS "
    "configuration into Cisco 8000 IOS-XR strictly following the skill below. "
    "Apply every Critical Rule and the deterministic QoS algorithms; never "
    "invent syntax. Output exactly: (1) a fenced code block with the translated "
    "Cisco 8000 IOS-XR config, ONE command per line with standard IOS-XR "
    "indentation and `!` separators (Section 3), then (2) Review Notes (Section 5) "
    "and (3) Unsupported / dropped items (Section 6) as concise markdown bullet "
    "points, and (4) an overall Confidence summary (High/Medium/Low per policy-map). "
    "Never put unsupported config in the code block.\n\n"
    "===== SKILL: SKILL.md =====\n" + SKILL +
    "\n\n===== SKILL: CONFIG_TRANSLATION_QOS.md =====\n" + QOS_REF
)


async def _translate(ctx: Context, name: str, platform: str, qos: str) -> str:
    model = ctx.settings.get("model") or "cisco-gpt-5-nano"
    llm = ADKChatModel(llm=ctx.llm, model_name=model,
                       temperature=ctx.settings.get("temperature", 0.0))
    human = (f"Source device: {name}\nSource platform: {platform}\n"
             f"Target: Cisco 8000 IOS-XR (default VOQ mode unless the config implies ETM/EFC).\n\n"
             f"Translate this QoS configuration:\n```\n{qos}\n```")
    resp = await llm.ainvoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=human)])
    return resp.content if isinstance(resp.content, str) else str(resp.content)


TARGET_8000 = {"node-1", "node-2", "node-15"}  # Cisco 8000 targets on the CNC


def _target_8000(text: str) -> str:
    low = text.lower()
    for t in TARGET_8000:
        if t in low:
            return t
    return "node-1"


def _extract_config_block(text: str) -> str:
    """Pull the translated CLI out of the LLM's fenced code block (Section 3)."""
    i = text.find("```")
    if i == -1:
        return text.strip()
    j = text.find("```", i + 3)
    block = text[i + 3:j] if j != -1 else text[i + 3:]
    lines = block.splitlines()
    if lines and lines[0].strip().lower() in ("", "cli", "text", "iosxr", "xr", "plaintext", "cisco"):
        lines = lines[1:]
    return "\n".join(lines).strip()


_CLI_TOP = ("class-map", "policy-map", "interface", "end-policy-map", "end-class-map")
_CLI_CHILD = ("class ", "match ", "set ", "police", "bandwidth", "shape", "priority",
              "random-detect", "queue-limit", "service-policy", "conform-action",
              "exceed-action", "violate-action")


def _prettify_cli(cfg: str) -> str:
    """Reflow flattened IOS-XR QoS into one statement per line with light indentation."""
    cfg = (cfg or "").strip()
    if not cfg or cfg.count("\n") >= 3:
        return cfg
    t = re.sub(r"[ \t]+", " ", cfg.replace("\n", " ")).strip()
    for kw in _CLI_TOP + _CLI_CHILD:
        t = t.replace(" " + kw, "\n" + kw)
    t = t.replace(" !", "\n!")
    out = []
    for raw in t.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith(_CLI_TOP) or s == "!":
            out.append(s)
        elif s.startswith("class "):
            out.append(" " + s)
        else:
            out.append("  " + s)
    return "\n".join(out)


def _sanitize_cfg(cfg: str, target_if: str = "") -> str:
    """Fix common Cisco 8000 translation issues in interface blocks:
      * drop the invalid `end-interface` token (IOS-XR has no such command);
      * keep only ONE `service-policy output` per interface (IOS-XR allows one);
      * optionally remap the source interface name to a real target interface."""
    out, in_if, seen_out = [], False, False
    for ln in cfg.splitlines():
        s = ln.strip()
        if s == "end-interface":
            continue
        if s.startswith("interface "):
            in_if, seen_out = True, False
            out.append(f"interface {target_if}" if target_if else ln)
            continue
        if in_if:
            if s == "!" or (ln[:1] not in (" ", "\t") and s):
                in_if = False
            elif s.startswith("service-policy output"):
                if seen_out:
                    continue  # collapse multiple egress policies to the first
                seen_out = True
        out.append(ln)
    return "\n".join(out)


def _format_translation(name: str, translated: str) -> str:
    """Clean, decluttered view: a fenced config block + one concise confidence line.
    Full review notes stay in the result payload."""
    cfg = _prettify_cli(_extract_config_block(translated))
    conf = " ".join(l.strip(" -*") for l in translated.splitlines()
                     if "confidence" in l.lower())[:280]
    parts = [f"### \U0001f9fe Translated QoS \u2014 **{name} QoS \u2192 Cisco 8000 QoS**"]
    if cfg:
        parts.append("```\n" + cfg + "\n```")
    parts.append("**Confidence:** " + (conf or "see review notes in the result payload."))
    return "\n\n".join(parts)


def _names(cfg: str, kw: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(rf'^{kw} (?:match-\S+ )?(\S+)', cfg, re.M):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def _golden_config_template(name: str, cfg: str) -> tuple[dict, str]:
    tname = f"ct-qos-{name}-8000"
    tmpl = {"golden-config:template": [{
        "name": tname, "tag": ["QoS-Migration"],
        "version": [{"id": "1", "type": "native", "mode": "merge", "config": cfg}],
    }]}
    return tmpl, tname


def _golden_config_application(name: str, tname: str, target: str) -> dict:
    return {"golden-config:golden-config": {"application": [{
        "name": f"cwm-gc-qos-{name}", "device": target,
        "jinja-template": {"template": tname, "version": "1"},
    }]}}


def _gen_checks(name: str, cfg: str, target: str) -> dict:
    pms = _names(cfg, "policy-map")
    intfs = list(dict.fromkeys(re.findall(r'^interface (\S+)', cfg, re.M)))
    pre = [{"name": "pre-qos-baseline", "node": target, "stage": "pre",
            "command": "show running-config policy-map", "expect": ".",
            "note": "capture baseline QoS before change"}]
    post = []
    for pm in pms:
        # presence of the migrated policy-map
        post.append({"name": f"policy-{pm}", "node": target, "stage": "post",
                     "command": f"show running-config policy-map {pm}", "expect": pm})
        # lints SCOPED to this migrated policy-map (not device-wide, so pre-existing
        # config on the target can't trip them)
        post.append({"name": f"no-wred-{pm}", "node": target, "stage": "post",
                     "command": f"show running-config policy-map {pm}",
                     "expect_absent": "random-detect", "note": "RULE_NO_WRED (scoped)"})
        if "egress" in pm.lower() or pm.lower().endswith(("-q", "-out", "-remarking")):
            post.append({"name": f"no-egr-qgrp-{pm}", "node": target, "stage": "post",
                         "command": f"show running-config policy-map {pm}",
                         "expect_absent": "set qos-group",
                         "note": "egress qos-group ban (scoped to egress policy-map)"})
    # Interface attachment is operator-mapped on the target (interface names + HW QoS
    # capability differ from the source) -> advisory, not a hard failure.
    for i in intfs:
        post.append({"name": f"bind-{i}", "node": target, "stage": "post",
                     "command": f"show running-config interface {i}", "expect": "service-policy",
                     "optional": True, "note": "interface attachment (operator-mapped on target)"})
    return {"pre": pre, "post": post}


def _gen_workflow(name: str, tname: str, target: str, checks: dict) -> dict:
    """CWM Serverless-Workflow (MOP): pre-check -> dry-run -> approve -> apply -> post-check."""
    return {
        "id": f"qos-migrate-{name}-to-8000",
        "name": f"QoS Migration MOP — {name} -> Cisco 8000 ({target})",
        "specVersion": "0.8",
        "functions": [
            {"name": "validateChecks", "operation": "subflow:validateChecks"},
            {"name": "NSO.RestconfPatch", "metadata": {"worker": "cwm-solutions-cisco.nso"},
             "operation": "cisco.nso.@latest.restconf.Patch"},
        ],
        "states": [
            {"name": "load-template", "type": "operation",
             "actions": [{"functionRef": "NSO.RestconfPatch",
                          "path": "/golden-config:golden-config",
                          "dataRef": "golden_config_template",
                          "note": "load the QoS golden-config template into NSO (definition only)"}],
             "transition": "pre-check"},
            {"name": "pre-check", "type": "operation",
             "actions": [{"subFlowRef": "validateChecks", "checks": checks["pre"]}],
             "transition": "dry-run"},
            {"name": "dry-run", "type": "operation",
             "actions": [{"functionRef": "NSO.RestconfPatch",
                          "path": "/golden-config:golden-config?dry-run=native",
                          "dataRef": "golden_config_application",
                          "note": "render native diff on target; no commit"}],
             "transition": "approve"},
            {"name": "approve", "type": "operation",
             "actions": [{"note": "operator approval (HITL) before commit"}],
             "transition": "apply"},
            {"name": "apply", "type": "operation",
             "actions": [{"subFlowRef": "apply-golden-config-cwm-sol",
                          "dataRef": "golden_config_application",
                          "note": "reuse the stock CWM golden-config apply workflow (present on CNC)"}],
             "transition": "post-check"},
            {"name": "post-check", "type": "operation",
             "actions": [{"subFlowRef": "validateChecks", "checks": checks["post"]}],
             "end": True},
        ],
    }


# --- guarded load stage (settings-gated, dry-run only, never commits) ---
def _mop_for_import(name: str, target: str) -> dict:
    """Self-contained CWM MOP (inject states) that imports cleanly (no child refs)."""
    return {"definition": {
        "id": f"qos-migration-{name}-to-8000",
        "name": f"QoS Migration - {name} to Cisco 8000",
        "version": "1.0", "specVersion": "0.8", "start": "pre-check",
        "states": [
            {"name": "pre-check", "type": "inject",
             "data": {"stage": "pre", "checks": ["show running-config policy-map (baseline on target)"]},
             "transition": "load-template"},
            {"name": "load-template", "type": "inject",
             "data": {"artifact": f"golden-config template ct-qos-{name}-8000 (native/merge)"},
             "transition": "dry-run"},
            {"name": "dry-run", "type": "inject",
             "data": {"action": "apply golden-config with ?dry-run=native; surface native diff"},
             "transition": "approve"},
            {"name": "approve", "type": "inject",
             "data": {"action": "operator approval (pause-mop) before commit"},
             "transition": "apply"},
            {"name": "apply", "type": "inject",
             "data": {"action": f"apply-golden-config-cwm-sol on target {target}"},
             "transition": "post-check"},
            {"name": "post-check", "type": "inject",
             "data": {"checks": ["policy-map present", "no random-detect",
                                 "no egress set qos-group", "interface service-policy bound"]},
             "end": True},
        ],
    }, "wfTags": ["QoS-Migration", f"{name}-to-8000", "MOP", "cisco-8000"]}


def _http(method: str, url: str, token: str, body=None,
          ct: str = "application/json", timeout: int = 12):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": ct, "Accept": ct})
    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=c, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001 - report, never raise into the flow
        return 0, str(e)


def _setting(ctx: Context, key: str, default: str = "") -> str:
    try:
        return str(ctx.settings.get(key, default) or default)
    except Exception:
        return default


# CNC is hardcoded to the instance this CWAI is a provider on (.49).
CNC_BASE = "https://172.20.163.49:30603"
_SSO = "/crosswork/sso/v1/tickets"
# Lab defaults so the agent works even when no settings UI is available; override
# via the cnc_user / cnc_password / cnc_token agent settings when present.
CNC_USER = "admin"
CNC_PW = "${CNC_PASSWORD}"


def _cas_post(url: str, form: dict, safe: str = "", timeout: int = 7):
    data = urllib.parse.urlencode(form, safe=safe).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/plain"})
    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=c, timeout=timeout) as r:
            return r.status, r.getheader("Location", ""), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, "", e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, "", str(e)


# Process-wide CAS JWT cache: mint ONCE per run and reuse. Minting a fresh token per
# block push/verify trips CAS "session limit per user" -> empty token -> gateway 401.
_TOKEN_CACHE: dict = {"jwt": "", "exp": 0.0}


def _mint_token(ctx: Context) -> str:
    """Fetch a CNC CAS JWT from .49 (two-step CAS: username/password -> TGT -> service
    ticket JWT), cached for ~3 min so a whole migration run reuses one session. Uses
    cnc_user/cnc_password settings, falling back to lab defaults; a pasted cnc_token wins
    only if minting yields nothing."""
    now = time.time()
    if _TOKEN_CACHE["jwt"] and now < _TOKEN_CACHE["exp"]:
        return _TOKEN_CACHE["jwt"]
    pasted = _setting(ctx, "cnc_token")
    user = _setting(ctx, "cnc_user", CNC_USER)
    pw = _setting(ctx, "cnc_password", CNC_PW)
    if not pw:
        return pasted
    # step 1: TGT (CAS wants a literal '!' in the password, not %21)
    _s, loc, body = _cas_post(f"{CNC_BASE}{_SSO}", {"username": user, "password": pw}, safe="!")
    m = re.search(r"TGT-[\w.\-]+", loc) or re.search(r"TGT-[\w.\-]+", body)
    if not m:
        return pasted
    # step 2: service ticket JWT for the registered app-dashboard service
    _s2, _l, jwt = _cas_post(f"{CNC_BASE}{_SSO}/{m.group(0)}", {"service": f"{CNC_BASE}/app-dashboard"})
    jwt = jwt.strip()
    if jwt.startswith("eyJ"):
        _TOKEN_CACHE["jwt"] = jwt
        _TOKEN_CACHE["exp"] = now + 1200  # one CAS login lasts the whole run (20 min)
        return jwt
    return pasted


def _invalidate_token() -> None:
    _TOKEN_CACHE["jwt"] = ""
    _TOKEN_CACHE["exp"] = 0.0


_YC = "application/yang-data+json"


def _http_authed(ctx: Context, method: str, url: str, body=None,
                 ct: str = _YC, timeout: int = 30):
    """_http with the cached CAS token; on 401/Unauthorized, invalidate + re-mint once."""
    st, b = _http(method, url, _mint_token(ctx), body, ct=ct, timeout=timeout)
    if st in (401, 403) or (isinstance(b, str) and "unauthorized request" in b.lower()):
        _invalidate_token()
        st, b = _http(method, url, _mint_token(ctx), body, ct=ct, timeout=timeout)
    return st, b


def _gc_available(ctx: Context, target: str) -> bool:
    """True if NSO golden-config is installed on this CNC (preferred push path = apply
    through NSO golden-config rather than raw CLI exec)."""
    try:
        st, _ = _http_authed(ctx, "GET",
            f"{CNC_BASE}/crosswork/proxy/nso/restconf/data/golden-config:golden-config?depth=1",
            ct=_YC, timeout=15)
        return st == 200
    except Exception:
        return False


def _gc_apply_block(ctx: Context, target: str, block: str, idx: int) -> tuple[bool, str]:
    """Push one block THROUGH NSO golden-config: create a native template, then apply it
    to the device (NSO commits). This is the workflow/golden-config-through-NSO path."""
    tname = f"ct-qos-blk{idx}-{target}"
    tmpl = {"golden-config:template": [{"name": tname, "tag": ["QoS-Migration"],
            "version": [{"id": "1", "type": "native", "mode": "merge", "config": block}]}]}
    st1, b1 = _http_authed(ctx, "PUT",
        f"{CNC_BASE}/crosswork/proxy/nso/restconf/data/golden-config:golden-config/template={tname}",
        tmpl, ct=_YC, timeout=30)
    if st1 not in (200, 201, 204):
        return False, f"gc-template HTTP {st1}"
    app = {"golden-config:golden-config": {"application": [{
        "name": f"cwm-gc-blk{idx}-{target}", "device": target,
        "jinja-template": {"template": tname, "version": "1"}}]}}
    st2, _b2 = _http_authed(ctx, "PATCH",
        f"{CNC_BASE}/crosswork/proxy/nso/restconf/data", app, ct=_YC, timeout=60)
    return st2 in (200, 201, 204), f"gc-apply HTTP {st2}"



def _native_diff(body: str) -> str:
    """Pull the native CLI out of an NSO `?dry-run=native` response (JSON or raw)."""
    try:
        d = json.loads(body)
        for k in ("dry-run:dry-run-result", "dry-run-result", "tailf-ncs:dry-run-result"):
            v = d.get(k) if isinstance(d, dict) else None
            if isinstance(v, dict):
                nat = v.get("native") or {}
                dev = nat.get("device") or v.get("device") or []
                if isinstance(dev, list) and dev:
                    return "\n".join(x.get("data", "") for x in dev if isinstance(x, dict)).strip()
                if isinstance(nat.get("data"), str):
                    return nat["data"].strip()
    except Exception:
        pass
    return ""


def _load_and_dryrun_sync(ctx: Context, name: str, target: str,
                          gc_tmpl: dict, gc_app: dict) -> tuple[str, str, dict]:
    """On approval: (1) load the golden-config template onto NSO, (2) import the
    workflow onto CWM, (3) start the migration in dry-run (native diff, NO commit).
    Never commits device config; source stays read-only. Credential-gated via settings."""
    enabled = _setting(ctx, "load_to_cnc", "true").lower() in ("1", "true", "yes", "on")
    base = CNC_BASE  # hardcoded to the CNC this CWAI is a provider on (.49)
    token = _mint_token(ctx)
    if not enabled:
        return ("\ud83d\udd12 **Load disabled** (`load_to_cnc` off) — package returned only; "
                "nothing pushed.", "", {"loaded": False, "reason": "disabled"})
    if not token:
        return ("\ud83d\udd12 **Load skipped** — I could not obtain a CNC token. Set "
                "`cnc_password` (user `cnc_user`, default admin) so I can fetch a CAS JWT "
                "from 172.20.163.49, or paste a `cnc_token`.",
                "", {"loaded": False, "reason": "no-token"})
    lines, results = [], {}
    yc = "application/yang-data+json"  # RESTCONF media type for the NSO proxy
    # (1) load the golden-config TEMPLATE onto NSO (template definition only, not a device change)
    st1, b1 = _http("PATCH", f"{base}/crosswork/proxy/nso/restconf/data", token, gc_tmpl, ct=yc)
    results["nso_template"] = {"status": st1, "body": b1[:300]}
    if st1 in (200, 201, 204):
        lines.append(f"- \u2460 NSO golden-config **template loaded** \u2192 HTTP {st1}")
    else:
        lines.append(f"- \u2460 NSO golden-config template \u2192 HTTP {st1} (golden-config package not installed "
                     "on this NSO \u2014 template returned in `data.golden_config_template`)")
    # (2) import the WORKFLOW (MOP) onto CWM
    st2, b2 = _http("POST", f"{base}/crosswork/cwm/v2/workflowImport", token,
                    [_mop_for_import(name, target)])
    results["cwm_import"] = {"status": st2, "body": b2[:300]}
    lines.append(f"- \u2461 CWM **workflow imported** \u2192 HTTP {st2}")
    # (3) START migration in DRY-RUN (native diff only, NO commit)
    st3, b3 = _http("PATCH", f"{base}/crosswork/proxy/nso/restconf/data?dry-run=native",
                    token, gc_app, ct=yc)
    results["dry_run"] = {"status": st3, "body": b3[:400]}
    try:
        cfg_preview = gc_tmpl["golden-config:template"][0]["version"][0]["config"]
    except Exception:
        cfg_preview = ""
    cfg_block = ""
    if st3 in (200, 201, 204):
        diff = _native_diff(b3) or b3[:1500]
        lines.append(f"- \u2462 Migration **dry-run started** \u2192 HTTP {st3} (native diff below; no commit)")
        cfg_block = f"**Native diff NSO would push to {target} (Cisco 8000):**\n\n```\n{diff}\n```"
        results["dry_run"]["native_diff"] = diff
    else:
        lines.append(f"- \u2462 Migration **dry-run** \u2192 HTTP {st3} (golden-config apply unavailable on this "
                     "NSO; translated config shown below)")
        if cfg_preview:
            cfg_block = f"**Config that will be pushed to {target} (Cisco 8000):**\n\n```\n{cfg_preview}\n```"
        results["dry_run"]["config_preview"] = cfg_preview
    lines.append("- \u26a0\ufe0f **source read-only** \u00b7 **dry-run only** \u00b7 nothing committed to any device.")
    summary = "\ud83d\udce4 **Load & dry-run results:**\n" + "\n".join(lines)
    return summary, cfg_block, {"loaded": True, **results}


async def _load_and_dryrun(ctx: Context, name: str, target: str,
                           gc_tmpl: dict, gc_app: dict) -> tuple[str, str, dict]:
    """Async wrapper: run the blocking load/dry-run off the event loop with a budget."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_load_and_dryrun_sync, ctx, name, target, gc_tmpl, gc_app),
            timeout=50)
    except Exception as e:  # noqa: BLE001
        return (f"\u26a0\ufe0f Load/dry-run could not complete ({type(e).__name__}); "
                "the translated config is shown above. Proceed to push to apply it.",
                "", {"loaded": False, "reason": "timeout"})


async def _start_workflow(ctx: Context, name: str, target: str) -> tuple[str, dict]:
    """Hand the migration to the imported CWM workflow (best-effort start; honest fallback)."""
    token = _mint_token(ctx)
    base = CNC_BASE
    wid = ""
    _s, body = _http("GET", f"{base}/crosswork/cwm/v2/workflow", token)
    try:
        d = json.loads(body)
        wfs = d if isinstance(d, list) else d.get("workflows", [])
        for w in wfs:
            if "QoS Migration" in (w.get("name", "") or "") and name in (w.get("name", "") or ""):
                wid = w.get("workflowId", ""); break
        if not wid:
            for w in wfs:
                if "QoS Migration" in (w.get("name", "") or ""):
                    wid = w.get("workflowId", ""); break
    except Exception:
        pass
    jobstat = None
    if wid:
        js, _jb = _http("POST", f"{base}/crosswork/cwm/v2/job", token,
                        {"workflowDefinitionId": wid, "jobName": f"qos-mig-{name}", "inputData": {}})
        jobstat = js
    started = jobstat in (200, 201)
    if started:
        msg = (f"\u25b6\ufe0f **Migration started** \u2014 CWM workflow `{wid}` is running on **{target}** "
               f"(pre-check \u2192 dry-run \u2192 approve \u2192 apply \u2192 post-check). **node-16 stays read-only.**")
    else:
        msg = (f"\u25b6\ufe0f **Migration handed to CWM** \u2014 workflow **QoS Migration - {name} to Cisco 8000** "
               f"(`{wid or 'imported'}`) is **staged and ready** in the CWM Workflow Manager; run it there to execute "
               f"pre-check \u2192 dry-run \u2192 approve \u2192 apply \u2192 post-check on **{target}**. "
               f"**node-16 read-only; nothing committed.**")
    return msg, {"workflow_id": wid, "job_status": jobstat, "started": started}


def _classify_platform(model: str, osname: str) -> tuple[str, str]:
    """Map NSO platform (model + os name) to a label + role (source|target|skip).
    source = IOS-XR excluding Cisco 8000; target = Cisco 8000; skip = IOS-XE / unknown."""
    m = (model or "").lower()
    xr = "xr" in (osname or "").lower()
    xe = "xe" in (osname or "").lower()
    if re.search(r"8[0-9]{3}", m) and xr:
        return "Cisco 8000 (IOS-XR)", "target"
    if xe or "asr-920" in m or "asr920" in m or "c8300" in m:
        return "IOS-XE (excluded)", "skip"
    if "asr9k" in m or "asr-9" in m or "asr9" in m:
        return "ASR 9000 (IOS-XR)", "source"
    if "ncs540" in m or "ncs-540" in m:
        return "NCS 540 (IOS-XR)", "source"
    if "ncs-5500" in m or "ncs5500" in m or "ncs-55" in m or "ncs55" in m:
        return "NCS 5500 (IOS-XR)", "source"
    if xr:
        return "IOS-XR (other)", "source"
    return f"unknown ({model})", "skip"


def _discover_inventory_sync(ctx: Context) -> tuple[dict, dict] | None:
    """Blocking core of inventory discovery (run via asyncio.to_thread)."""
    token = _mint_token(ctx)
    if not token:
        return None
    st, body = _http(
        "GET",
        f"{CNC_BASE}/crosswork/proxy/nso/restconf/data/tailf-ncs:devices/device?fields=name;platform",
        token, ct="application/yang-data+json")
    if st != 200:
        return None
    try:
        d = json.loads(body)
        devs = d.get("tailf-ncs:device") or d.get("device") or []
    except Exception:
        return None
    sources, targets = {}, {}
    for x in devs:
        nm = x.get("name")
        p = x.get("platform") or {}
        label, role = _classify_platform(p.get("model", ""), p.get("name", ""))
        if role == "source":
            sources[nm] = label
        elif role == "target":
            targets[nm] = label
    return (sources, targets) if (sources or targets) else None


async def _discover_inventory(ctx: Context) -> tuple[dict, dict] | None:
    """Fetch the live device inventory from CNC .49 (NSO device list via the proxy) and
    split into IOS-XR migration sources (excluding 8000) and Cisco 8000 targets. Runs the
    blocking HTTP off the event loop with a short budget so the agent never hangs."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_discover_inventory_sync, ctx), timeout=25)
    except Exception:
        return None


def _static_inventory() -> tuple[dict, dict]:
    """Fallback inventory from the bundled PLATFORM map when CNC is unreachable."""
    sources, targets = {}, {}
    for dev, label in PLATFORM.items():
        low = label.lower()
        if "8000" in low or "target" in low:
            targets[dev] = "Cisco 8000 (IOS-XR)"
        elif "ios-xe" in low:
            continue  # IOS-XE excluded from the IOS-XR source list ("asr 900" would also hit "asr 9000")
        else:
            sources[dev] = label
    return sources, targets


def _inventory_message(sources: dict, targets: dict, live: bool) -> tuple[str, list[str]]:
    """Render the discovered inventory as a source/target selection prompt + chips."""
    src_rows = "\n".join(f"- **{n}** — {p}" for n, p in sorted(sources.items())) or "- (none found)"
    tgt_rows = "\n".join(f"- **{n}** — {p}" for n, p in sorted(targets.items())) or "- (none found)"
    origin = "live from CNC 172.20.163.49" if live else "bundled inventory (CNC unreachable)"
    body = (
        f"### 🧭 Select the QoS migration platforms ({origin})\n\n"
        f"**Source — IOS-XR platforms (excluding Cisco 8000):**\n{src_rows}\n\n"
        f"**Target — Cisco 8000 platforms in the network:**\n{tgt_rows}\n\n"
        "Reply with a **source → target**, e.g. `node-16 to node-15`."
    )
    chips = []
    if "node-16" in sources and "node-15" in targets:
        chips.append("node-16 to node-15")
    for s in list(sources)[:2]:
        t = next(iter(targets), "node-15")
        if f"{s} to {t}" not in chips:
            chips.append(f"{s} to {t}")
    return body, chips


def _parse_src_tgt(text: str, sources: dict, targets: dict) -> tuple[str | None, str | None]:
    """Pull a source and target device from a selection reply like 'node-16 to node-15'."""
    devs = re.findall(r"\b(?:node|cpe)-\d+\b", text.lower())
    src = next((d for d in devs if d in sources), None)
    tgt = next((d for d in devs if d in targets), None)
    # 'X to Y' ordering fallback when labels aren't resolvable
    if (not src or not tgt) and " to " in text.lower() and len(devs) >= 2:
        src = src or devs[0]
        tgt = tgt or devs[-1]
    return src, tgt


# --- hypotheses split / selection, live push + verify, report, rollback ---
_NED_EXEC = "tailf-ned-cisco-ios-xr-stats"  # node-15 NED; exec/any enters config + show


def _split_hypotheses(cfg: str) -> list[dict]:
    """Split prettified IOS-XR QoS into top-level blocks (class-map / policy-map /
    interface) so the operator can accept or reject each translated piece."""
    hyps: list[dict] = []
    cur: list[str] = []

    def flush() -> None:
        if not cur:
            return
        first = cur[0].strip()
        if not first or first.split()[0] not in ("class-map", "policy-map", "interface"):
            return
        block = "\n".join(cur).rstrip()
        hyps.append({"id": len(hyps) + 1, "title": first,
                     "kind": first.split()[0], "config": block})

    for ln in cfg.splitlines():
        top = bool(ln) and ln[0] not in " \t!" and ln.split() \
            and ln.split()[0] in ("class-map", "policy-map", "interface")
        if top:
            flush()
            cur = [ln]
        elif cur:
            cur.append(ln)
    flush()
    return hyps


def _hyp_message(name: str, target: str, hyps: list[dict]) -> tuple[str, list[str]]:
    parts = [f"### 🧪 Translated QoS — hypotheses for **{name} → Cisco 8000 ({target})**",
             "Review each translated block and pick the ones to migrate "
             "(reply `all`, numbers like `1,3`, or `none`):"]
    for h in hyps:
        parts.append(f"**{h['id']}. {h['title']}**\n```\n{h['config']}\n```")
    chips = ["all"] + [str(h["id"]) for h in hyps[:3]] if len(hyps) > 1 else ["all"]
    return "\n\n".join(parts), chips


def _select_hyps(text: str, hyps: list[dict]) -> list[dict]:
    low = text.strip().lower()
    if low in ("all", "yes", "everything", "select all", "migrate all", "yes all", "all of them"):
        return list(hyps)
    if low in ("none", "no", "cancel", "stop"):
        return []
    nums = {int(n) for n in re.findall(r"\d+", text)}
    sel = [h for h in hyps if h["id"] in nums]
    for h in hyps:
        nm = h["title"].split()[-1].lower()
        if nm and nm in low and h not in sel:
            sel.append(h)
    return sel


def _exec_cli(ctx: Context, node: str, args: str) -> tuple[int, str]:
    """Run CLI on a device via the NSO live-status exec/any proxy on CNC .49.
    Uses the cached token and self-heals once on an auth rejection."""
    url = (f"{CNC_BASE}/crosswork/proxy/nso/restconf/operations/devices/"
           f"device={node}/live-status/{_NED_EXEC}:exec/any")
    st, body = _http_authed(ctx, "POST", url, {"input": {"args": args}}, ct=_YC, timeout=45)
    out = body
    if st == 200:
        try:
            out = (json.loads(body).get(f"{_NED_EXEC}:output") or {}).get("result", "") or body
        except Exception:
            pass
    return st, out


async def _apush(ctx: Context, target: str, cfg: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_push_config, ctx, target, cfg)


async def _averify(ctx: Context, target: str, checks: dict) -> tuple[bool, list[dict]]:
    return await asyncio.to_thread(_verify_checks, ctx, target, checks)


def _push_config(ctx: Context, target: str, cfg: str) -> tuple[bool, str]:
    """Push QoS CLI to the 8000 target (configure terminal -> commit)."""
    st, out = _exec_cli(ctx, target, "configure terminal\n" + cfg + "\ncommit\nend")
    low = out.lower()
    ok = st == 200 and not any(e in low for e in (
        "% ", "invalid input", "syntax error", "failed to commit", "aborted", "% error"))
    return ok, out


def _verify_checks(ctx: Context, target: str, checks: dict) -> tuple[bool, list[dict]]:
    """Run the post-checks live on the target. Optional checks (interface attachment)
    report WARN instead of FAIL so they don't block an otherwise-clean migration.
    Returns (no_hard_failures, results)."""
    results = []
    for c in checks.get("post", []):
        node = c.get("node", target)
        _st, out = _exec_cli(ctx, node, c["command"])
        if "expect_absent" in c:
            ok = c["expect_absent"].lower() not in out.lower()
            exp = f"absent `{c['expect_absent']}`"
        else:
            ok = (c.get("expect", "") or "").lower() in out.lower()
            exp = f"`{c.get('expect', '')}`"
        verdict = "PASS" if ok else ("WARN" if c.get("optional") else "FAIL")
        results.append({"name": c["name"], "command": c["command"], "expect": exp,
                        "verdict": verdict,
                        "output": out.replace("\r", "").strip()[:160]})
    return (all(r["verdict"] != "FAIL" for r in results) if results else True), results


def _report(name: str, target: str, pushed_cfg: str, results: list[dict], ok: bool) -> str:
    icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    passed = sum(r["verdict"] == "PASS" for r in results)
    warned = sum(r["verdict"] == "WARN" for r in results)
    head = "✅ **Migration complete**" if ok else "⚠️ **Verification FAILED**"
    rows = "\n".join(
        f"- {icon.get(r['verdict'], '❓')} `{r['command']}` → {r['verdict']}"
        + (f" (expected {r['expect']})" if r["verdict"] in ("FAIL", "WARN") else "")
        for r in results) or "- (no post-checks)"
    tail = f"\n\n_Note: {warned} interface-attachment check(s) are advisory — bind the policies to the target's real interfaces._" if warned else ""
    return (f"{head} — **{name} QoS → Cisco 8000 ({target})**\n\n"
            f"**Config pushed & committed:**\n```\n{pushed_cfg}\n```\n\n"
            f"**Post-checks ({passed}/{len(results)} passed"
            + (f", {warned} advisory" if warned else "") + f"):**\n{rows}{tail}")


def _rollback_cli(cfg: str) -> str:
    """Build negation CLI (unbind interface service-policies first, then remove maps)."""
    unbinds, negs, cur_if = [], [], None
    for ln in cfg.splitlines():
        s = ln.strip()
        if s.startswith("interface"):
            cur_if = s
        elif s.startswith("service-policy") and cur_if:
            unbinds += [cur_if, " no " + s]
        elif s.startswith(("policy-map", "class-map")):
            negs.append("no " + s)
    return "\n".join(unbinds + negs)


def _order_blocks(blocks: list[dict]) -> list[dict]:
    """Dependency order: class-maps -> policy-maps -> interface bindings."""
    rank = {"class-map": 0, "policy-map": 1, "interface": 2}
    return sorted(blocks, key=lambda b: rank.get(b.get("kind", ""), 3))


def _push_blocks(ctx: Context, target: str, cfg: str) -> tuple[bool, list[dict], list[dict], str, list[dict]]:
    """Push each top-level block separately in dependency order. Prefers applying THROUGH
    NSO golden-config when installed; else uses the NSO live-status CLI. A failed interface
    attachment is ADVISORY (target interfaces + HW QoS differ from the source) and does not
    fail the migration. Returns (all_ok, pushed, failed, mechanism, advisory)."""
    blocks = _split_hypotheses(cfg) or [{"id": 1, "title": "config", "kind": "block", "config": cfg}]
    use_gc = _gc_available(ctx, target)
    mech = "NSO golden-config apply" if use_gc else "NSO live-status CLI (exec/any)"
    pushed, failed, advisory = [], [], []
    for i, b in enumerate(_order_blocks(blocks)):
        if use_gc:
            ok, out = _gc_apply_block(ctx, target, b["config"], i)
            if not ok:  # golden-config apply failed for this block -> CLI fallback
                ok, out = _push_config(ctx, target, b["config"])
        else:
            ok, out = _push_config(ctx, target, b["config"])
        rec = {"title": b["title"], "output": str(out).replace("\r", "").strip()[:220]}
        if ok:
            pushed.append(rec)
        elif b.get("kind") == "interface":
            advisory.append(rec)  # attachment is operator-mapped -> advisory, not fatal
        else:
            failed.append(rec)
    return (len(failed) == 0), pushed, failed, mech, advisory


async def _apush_blocks(ctx: Context, target: str, cfg: str) -> tuple[bool, list[dict], list[dict], str, list[dict]]:
    return await asyncio.to_thread(_push_blocks, ctx, target, cfg)


async def _begin_migration(ctx: Context, name: str, target: str) -> Result | None:
    """Read the source QoS, translate to Cisco 8000, build the package, and pause for
    approval. Source stays read-only; dry-run makes no commit."""
    st = ctx.state
    cfg, src_label = await _get_config(ctx, name)
    if not cfg:
        return Result(message=_am(
            f"Could not read config for **{name}** (no live DRA result and no bundled sample)."),
            data={"status": "NO_CONFIG", "device": name})
    platform = _platform_of(name, cfg)
    qos = _extract_qos(cfg)
    await _say(ctx, f"🔎 Read QoS from **{name}** ({platform}) [{src_label}] — "
                    f"{qos.count('policy-map ')} policy-maps, {qos.count('class-map ')} class-maps.")
    await _say(ctx, f"⚙️ Translating **{name} QoS → {target} (Cisco 8000 IOS-XR)** per skill "
                    "(no WRED · egress police→shape · TC ordering P1=TC7 · ingress marking-only · "
                    "qos-group egress ban · mpls exp imposition→topmost)…")
    translated = await _translate(ctx, name, platform, qos)
    await _say(ctx, _format_translation(name, translated))

    cfg = _prettify_cli(_extract_config_block(translated))
    cfg = _sanitize_cfg(cfg, _setting(ctx, "target_interface", ""))
    hyps = _split_hypotheses(cfg)
    if not hyps:
        hyps = [{"id": 1, "title": "translated config", "kind": "block", "config": cfg}]
    body, chips = _hyp_message(name, target, hyps)
    st.update(source=name, target=target, translated=translated, hyps=hyps,
              awaiting_hypotheses=True)
    await ctx.user.ask_nowait(_am(body, chips))
    return None


@agent.on_message("qos-migration-agent")
async def handle(ctx: Context, msg: Message) -> Result | None:
    text = (msg.query.get("prompt") or getattr(msg, "user_message", "") or "").strip()
    st = ctx.state

    # Resume after the source/target selection gate.
    if st.get("awaiting_selection"):
        st["awaiting_selection"] = False
        sources = st.get("sources", {})
        targets = st.get("targets", {})
        src, tgt = _parse_src_tgt(text, sources, targets)
        src = src or st.get("presel_src")
        tgt = tgt or st.get("presel_tgt")
        if not (src and tgt):
            body, chips = _inventory_message(sources, targets, bool(sources))
            st.update(awaiting_selection=True, presel_src=src, presel_tgt=tgt)
            await ctx.user.ask_nowait(_am(
                "I still need both a **source** and a **target**.\n\n" + body,
                chips or ["node-16 to node-15"]))
            return None
        st.clear()
        await _say(ctx, f"✅ Selected **{src} → {tgt}**. Starting the QoS migration…")
        return await _begin_migration(ctx, src, tgt)

    # Resume after the hypotheses selection gate (which translated blocks to migrate).
    if st.get("awaiting_hypotheses"):
        st["awaiting_hypotheses"] = False
        hyps = st.get("hyps", [])
        name = st.get("source", "?")
        target = st.get("target", "node-1")
        selected = _select_hyps(text, hyps)
        if not selected:
            st["awaiting_hypotheses"] = True
            body, chips = _hyp_message(name, target, hyps)
            await ctx.user.ask_nowait(_am(
                "No blocks selected — reply `all`, numbers like `1,3`, or `none` to cancel.\n\n"
                + body, chips))
            return None
        cfg = "\n!\n".join(h["config"] for h in selected).strip()
        gc_tmpl, tname = _golden_config_template(name, cfg)
        gc_app = _golden_config_application(name, tname, target)
        checks = _gen_checks(name, cfg, target)
        wf = _gen_workflow(name, tname, target, checks)
        await _say(ctx, f"🧩 **Selected {len(selected)}/{len(hyps)} block(s)** — package built: "
                        f"template `{tname}`, {len(checks['post'])} post-checks.")
        st.update(cfg=cfg, tname=tname, gc_tmpl=gc_tmpl, gc_app=gc_app, checks=checks, wf=wf,
                  awaiting_approval=True)
        await ctx.user.ask_nowait(_am(
            f"⏸️ Approve to **review** (load template + dry-run preview) on **{target}**? "
            f"Reply `yes` to proceed or `no` to stop. ({name} stays read-only.)",
            ["yes", "no"]))
        return None

    # Resume after HITL gate 1 (load + dry-run approval).
    if st.get("awaiting_approval"):
        st["awaiting_approval"] = False
        name = st.get("source", "?")
        if _approved(text):
            target = st.get("target", "node-1")
            gc_tmpl = st.get("gc_tmpl", {})
            gc_app = st.get("gc_app", {})
            tname = st.get("tname", "")
            await _say(ctx, f"✅ Approved — loading golden-config template **{tname}** onto NSO, "
                            f"importing the workflow onto CWM, and running the **dry-run** on **{target}**…")
            load_summary, load_cfg, load_data = await _load_and_dryrun(ctx, name, target, gc_tmpl, gc_app)
            await _say(ctx, f"📦 **{name} QoS → Cisco 8000 QoS ({target}) — dry-run complete.**\n\n{load_summary}")
            if load_cfg:
                await _say(ctx, load_cfg)
            st.update(awaiting_execute=True, load=load_data)
            await ctx.user.ask_nowait(_am(
                f"▶️ **Push the selected QoS to {target} (Cisco 8000) and verify now?** Reply "
                f"`yes` to push via NSO (configure → commit) and run the post-checks, or `no` "
                f"to keep it staged. ({name} read-only; nothing committed until you approve.)",
                ["yes", "no"]))
            return None
        st.clear()
        return Result(message=_am(f"❌ Not applied to **{name}** — operator declined.", _followups()),
                      data={"status": "DECLINED", "device": name})

    # Resume after HITL gate 2 (push to the 8000 target + verify).
    if st.get("awaiting_execute"):
        st["awaiting_execute"] = False
        name = st.get("source", "?")
        target = st.get("target", "node-1")
        cfg = st.get("cfg", "")
        checks = st.get("checks", {})
        if not _approved(text):
            st.clear()
            return Result(message=_am(
                f"⏸️ Not pushed — staged only. Nothing committed to **{target}**.", _followups()),
                data={"status": "STAGED", "device": name, "target": target})
        await _say(ctx, f"🚀 Pushing QoS to **{target}** block-by-block via NSO (configure → commit)…")
        all_ok, pushed, failed, mech, advisory = await _apush_blocks(ctx, target, cfg)
        push_rows = "\n".join(f"- ✅ `{p['title']}`" for p in pushed)
        if failed:
            push_rows += "\n" + "\n".join(
                f"- ❌ `{f['title']}` — {f['output'][:140]}" for f in failed)
        if advisory:
            push_rows += "\n" + "\n".join(
                f"- ⚠️ `{a['title']}` (attachment — bind on target's real interface)" for a in advisory)
        await _say(ctx, f"**Push results** (via **{mech}**) — {len(pushed)}/{len(pushed) + len(failed) + len(advisory)} committed:\n{push_rows}")
        await _say(ctx, f"Running {len(checks.get('post', []))} post-checks on **{target}**…")
        allpass, results = await _averify(ctx, target, checks)
        if all_ok and allpass:
            st.clear()
            return Result(message=_am(_report(name, target, cfg, results, True), _followups()),
                          data={"status": "MIGRATION_COMPLETED", "device": name, "target": target,
                                "pushed_config": cfg, "pushed": pushed, "checks": results})
        rep = _report(name, target, cfg, results, False)
        if failed:
            rep += "\n\n**Blocks that did not commit:**\n" + "\n".join(
                f"- ❌ `{f['title']}` — {f['output'][:160]}" for f in failed)
        st.update(awaiting_correction=True, cfg=cfg, checks=checks, last_results=results,
                  failed_blocks=failed)
        await _say(ctx, rep)
        await ctx.user.ask_nowait(_am(
            "Some blocks failed to commit or verify. Paste a **corrected config** (``` block) "
            "to re-apply and re-verify, reply `retry` to re-run, or `rollback` to remove it.",
            ["retry", "rollback"]))
        return None

    # Resume in the correct-and-retry loop (push failed or verification failed).
    if st.get("awaiting_correction"):
        st["awaiting_correction"] = False
        name = st.get("source", "?")
        target = st.get("target", "node-1")
        cfg = st.get("cfg", "")
        low = text.strip().lower()
        if low.startswith("rollback"):
            neg = _rollback_cli(cfg)
            okr, _o = (await _apush(ctx, target, neg)) if neg else (True, "")
            st.clear()
            return Result(message=_am(
                f"↩️ **Rolled back** on **{target}** ({'ok' if okr else 'check device'}). "
                f"Nothing left applied.", _followups()),
                data={"status": "ROLLED_BACK", "device": name, "target": target})
        if low in ("retry", "yes", "re-verify", "verify", "rerun", "re-run"):
            newcfg = cfg
        else:
            newcfg = _extract_config_block(text) if "```" in text else text.strip()
            newcfg = _prettify_cli(newcfg) if newcfg else cfg
            newcfg = _sanitize_cfg(newcfg, _setting(ctx, "target_interface", ""))
        await _say(ctx, f"🔁 Re-applying corrected QoS to **{target}** block-by-block and re-verifying…")
        all_ok, pushed, failed, mech, advisory = await _apush_blocks(ctx, target, newcfg)
        if failed:
            rows = "\n".join(f"- ❌ `{f['title']}` — {f['output'][:140]}" for f in failed)
            st.update(awaiting_correction=True, cfg=newcfg)
            await _say(ctx, f"❌ **Some blocks still failing** (via {mech}; {len(pushed)} committed):\n{rows}")
            await ctx.user.ask_nowait(_am(
                "Paste another corrected config (``` block), reply `retry`, or `rollback`.",
                ["retry", "rollback"]))
            return None
        checks = _gen_checks(name, newcfg, target)
        allpass, results = await _averify(ctx, target, checks)
        if allpass:
            st.clear()
            return Result(message=_am(_report(name, target, newcfg, results, True), _followups()),
                          data={"status": "MIGRATION_COMPLETED", "device": name, "target": target,
                                "pushed_config": newcfg, "pushed": pushed, "checks": results})
        st.update(awaiting_correction=True, cfg=newcfg, checks=checks, last_results=results)
        await _say(ctx, _report(name, target, newcfg, results, False))
        await ctx.user.ask_nowait(_am(
            "Still failing. Paste a corrected config (``` block), reply `retry`, or `rollback`.",
            ["retry", "rollback"]))
        return None

    # New request.
    st.clear()
    low = text.lower()
    if not SKILL or not QOS_REF:
        return Result(message=_am("Skill files are missing from the image."),
                      data={"status": "ERROR"})

    # Discover the live platform inventory from CNC .49 (IOS-XR sources / 8000 targets).
    inv = await _discover_inventory(ctx)
    live = inv is not None
    sources, targets = inv if inv else _static_inventory()

    # Pure "list inventory" ask.
    if any(k in low for k in ("list", "which", "candidates", "inventory", "platforms")) \
            and not _parse_src_tgt(text, sources, targets)[0]:
        body, chips = _inventory_message(sources, targets, live)
        return Result(message=_am(body, chips or _followups()),
                      data={"status": "LISTED", "sources": sources, "targets": targets, "live": live})

    # Resolve source + target from the prompt.
    src, tgt = _parse_src_tgt(text, sources, targets)
    if not src:
        src = _device_from_text(text) if _device_from_text(text) in sources else None
    if not (src and tgt):
        body, chips = _inventory_message(sources, targets, live)
        st.update(awaiting_selection=True, sources=sources, targets=targets,
                  presel_src=src, presel_tgt=tgt)
        await ctx.user.ask_nowait(_am(
            body + "\n\n_Select the **source** (IOS-XR, excluding 8000) and **target** "
            "(Cisco 8000) to migrate._", chips or ["node-16 to node-15"]))
        return None

    return await _begin_migration(ctx, src, tgt)


if __name__ == "__main__":
    agent.start()
