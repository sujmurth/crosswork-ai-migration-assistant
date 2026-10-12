#!/usr/bin/env python3
"""
nl_check.py -- turn a plain-English sentence into a structured pre/post check for the
SRv6-Migration-MOP, and (optionally) dry-run it against the live device via NSO.

The structured check matches the schema already consumed by the `validateChecks`
workflow and stored in checks-srv6-migration.json:

    { "name", "phase", "type"(pre|post), "node", "command", "expect",
      "match"(contains|regex), "nedExec", "onFail"(halt|rollback|warn) }

Example NL:
    "On node-2 run 'show isis adjacency' and pass if the output contains 'Up'.
     Add it as a pre-check to phase 2."

CLI:
    python3 nl_check.py "on node-2 run 'show isis adjacency' pass if contains 'Up' as pre-check to phase 2"
    python3 nl_check.py --dry-run --nso 198.18.134.29 "..."   # runs on device
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

# device-role aliases -> canonical device-name (extend as needed)
ALIASES = {
    "rrv6": "pce-12",
    "rr6": "pce-12",
    "rrv4": "pce-11",
    "the rr": "pce-11",
    "route reflector": "pce-11",
    "pce": "pce-11",
}

# device-name prefixes that use the IOS (not IOS-XR) stats NED
IOS_PREFIXES = ("cpe-", "iosv-")


def _ned_exec(node: str) -> str:
    n = node.lower()
    if n.startswith(IOS_PREFIXES):
        return "cisco-ios-stats"
    return "cisco-ios-xr-stats"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def _first_quoted(text: str, after_kw: str):
    """Return the first quoted ('...', \"...\" or `...`) string following a keyword."""
    m = re.search(
        rf"{after_kw}\s*[:=]?\s*['\"`]([^'\"`]+)['\"`]", text, re.IGNORECASE
    )
    return m.group(1).strip() if m else None


def parse_check(sentence: str) -> dict:
    """Parse an English sentence into a structured check dict. Best-effort + explicit
    warnings for anything it could not confidently extract."""
    text = " ".join(sentence.split())
    warnings = []

    # --- device / node ---
    node = None
    m = re.search(r"\bon\s+([A-Za-z0-9][A-Za-z0-9\-]*)\b", text, re.IGNORECASE)
    if m:
        node = m.group(1)
    else:
        for alias, dev in ALIASES.items():
            if alias in text.lower():
                node = dev
                break
    if node and node.lower() in ALIASES:
        node = ALIASES[node.lower()]
    if not node:
        warnings.append("could not determine device/node")

    # --- command ---
    command = _first_quoted(text, r"(?:run|execute|exec|command|cmd)")
    if not command:
        # try any backtick/quoted string that looks like a show/ping command
        m = re.search(r"['\"`]((?:show|ping|traceroute|bgp|monitor)[^'\"`]*)['\"`]",
                      text, re.IGNORECASE)
        command = m.group(1).strip() if m else None
    if not command:
        warnings.append("could not determine device command")

    # --- expect / verification ---
    expect = None
    for kw in (r"contains", r"matches?", r"says", r"expect(?:s|ing)?",
               r"equals?", r"equal to", r"shows?", r"see", r"pass(?:es)? if",
               r"verify(?:ing)? (?:that )?", r"make sure"):
        expect = _first_quoted(text, kw)
        if expect:
            break
    if not expect:
        warnings.append("could not determine expected output; set 'expect' manually")

    # --- match mode ---
    match_mode = "regex" if re.search(r"\bmatch|regex\b", text, re.IGNORECASE) else "contains"

    # --- phase ---
    phase = None
    m = re.search(r"phase\s*#?\s*(\d+)", text, re.IGNORECASE)
    if m:
        phase = m.group(1)
    else:
        warnings.append("no phase specified")

    # --- pre / post ---
    if re.search(r"\bpost[\s-]?check\b|\bafter\b", text, re.IGNORECASE):
        ctype = "post"
    elif re.search(r"\bpre[\s-]?check\b|\bbefore\b", text, re.IGNORECASE):
        ctype = "pre"
    else:
        ctype = "pre"
        warnings.append("pre/post not stated; defaulting to 'pre'")

    # --- on-fail action ---
    if re.search(r"\brollback\b", text, re.IGNORECASE):
        on_fail = "rollback"
    elif re.search(r"\bwarn|non[- ]?blocking|continue\b", text, re.IGNORECASE):
        on_fail = "warn"
    else:
        on_fail = "halt"

    name_bits = [x for x in [command and _slug(command.split("|")[0]), node] if x]
    name = "-".join(name_bits) or "custom-check"

    check = {
        "name": name,
        "phase": phase,
        "type": ctype,
        "node": node,
        "command": command,
        "expect": expect,
        "match": match_mode,
        "nedExec": _ned_exec(node) if node else "cisco-ios-xr-stats",
        "onFail": on_fail,
        "createdVia": "nl-authoring",
    }
    return {"check": check, "warnings": warnings}


def to_workflow_step(check: dict) -> dict:
    """Convert a structured check into a CWM (Conductor-style) workflow task that runs it
    through the `validateChecks` worker at the check's phase/stage. This is the JSON step
    you can insert into the SRv6-Migration-MOP so the check runs as part of the workflow."""
    name = check.get("name") or "custom-check"
    phase = check.get("phase")
    stage = (check.get("type") or "pre").lower()
    ref = _slug(f"{stage}-{name}") or "check"
    if phase:
        ref = f"phase{phase}-{ref}"
    return {
        "name": "validateChecks",
        "taskReferenceName": ref,
        "type": "SIMPLE",
        "optional": check.get("onFail") == "warn",
        "inputParameters": {
            "stage": stage,
            "phase": phase,
            "onFail": check.get("onFail", "halt"),
            "checks": [check],
        },
    }


# ---------------------------------------------------------------------------
#  CONFIG ACTIONS  ->  golden-config template (enso) + CWM config-push state
# ---------------------------------------------------------------------------
_DEV_RE = r"node-\d+|pce-\d+|cpe-[a-z0-9\-]+|iosv-[a-z0-9\-]+"


def parse_action(sentence: str) -> dict:
    """Parse an English sentence describing a CONFIG ACTION (config that CHANGES the device,
    not a read-only check) into a structured action used to build a golden-config template.

    Put the device CLI in a ```fenced block``` or after 'config:'. Example:
        "On node-2 and node-8, phase 4, merge, name it ipv6-loopback. config:
         interface Loopback0
          ipv6 address 2010::{{id}}/128"
    """
    raw = sentence
    warnings = []

    # --- config payload (preserve newlines) ---
    cfg = None
    mf = re.search(r"```+[a-z]*\s*\n?(.*?)\n?\s*```+", raw, re.S)
    if mf:
        cfg = mf.group(1).rstrip("\n")
        meta = raw[:mf.start()] + " " + raw[mf.end():]
    else:
        mc = re.search(r"\bconfig\s*[:=]\s*", raw, re.IGNORECASE)
        if mc:
            cfg = raw[mc.end():].strip("\n")
            meta = raw[:mc.start()]
        else:
            meta = raw
    if not cfg or not cfg.strip():
        warnings.append("no config block found -- put the device CLI in a ``` fenced block or after 'config:'")
        cfg = cfg or ""
    meta = " ".join(meta.split())

    # --- nodes ---
    nodes = []
    for m in re.finditer(rf"\b({_DEV_RE})\b", meta, re.IGNORECASE):
        n = m.group(1)
        if n not in nodes:
            nodes.append(n)
    for alias, dev in ALIASES.items():
        if alias in meta.lower() and dev not in nodes:
            nodes.append(dev)
    if not nodes:
        warnings.append("no target device found; add e.g. 'on node-2'")

    # --- template name ---
    m = re.search(r"(?:name(?:d| it)?|call(?:ed| it)?|template(?:d| named)?)\s+"
                  r"['\"`]?([A-Za-z0-9][A-Za-z0-9\-_]*)['\"`]?", meta, re.IGNORECASE)
    if m:
        name = _slug(m.group(1))
    else:
        first = (cfg.strip().splitlines() or [""])[0].strip()
        name = _slug(first)[:30] or "config-action"

    # --- phase ---
    phase = None
    m = re.search(r"phase\s*#?\s*(\d+)", meta, re.IGNORECASE)
    if m:
        phase = m.group(1)
    else:
        warnings.append("no phase specified")

    # --- merge / replace ---
    mode = "replace" if re.search(r"\breplace\b", meta, re.IGNORECASE) else "merge"

    action = {
        "name": name,
        "phase": phase,
        "nodes": nodes,
        "mode": mode,
        "tag": "SRv6-Migration",
        "config": cfg,
        "resourceId": "nso-dCloud",
        "createdVia": "nl-authoring",
    }
    return {"action": action, "warnings": warnings}


def _tmpl_name(action: dict) -> str:
    n = action.get("name") or "config-action"
    return n if n.endswith("-template") else f"{n}-template"


def to_golden_template(action: dict) -> str:
    """Build the enso golden-config *template* XML (repo golden-config:golden-config format)."""
    return (
        '<golden-config xmlns="http://example.com/golden-config">\n'
        "  <template>\n"
        f"    <name>{_tmpl_name(action)}</name>\n"
        f"    <tag>{action.get('tag', 'SRv6-Migration')}</tag>\n"
        "    <version>\n"
        "      <id>1</id>\n"
        "      <type>native</type>\n"
        f"      <mode>{action.get('mode', 'merge')}</mode>\n"
        "      <config>\n"
        f"{action.get('config', '')}\n"
        "</config>\n"
        "    </version>\n"
        "  </template>\n"
        "</golden-config>\n"
    )


def _applications(action: dict):
    tmpl = _tmpl_name(action)
    return [{
        "device": node,
        "template-type": "jinja-template",
        "jinja-template": {"template": tmpl, "version": "1"},
        "name": f"cwm-gc-{action['name']}-{node}",
    } for node in (action.get("nodes") or ["<device>"])]


def to_golden_application(action: dict) -> str:
    """Build the per-device golden-config *application* binding XML (reference / manual apply)."""
    tmpl = _tmpl_name(action)
    apps = "\n".join(
        "  <application>\n"
        f"    <name>cwm-gc-{action['name']}-{node}</name>\n"
        f"    <device>{node}</device>\n"
        "    <jinja-template>\n"
        f"      <template>{tmpl}</template>\n"
        "      <version>1</version>\n"
        "    </jinja-template>\n"
        "  </application>"
        for node in (action.get("nodes") or ["<device>"]))
    return f'<golden-config xmlns="http://example.com/golden-config">\n{apps}\n</golden-config>\n'


def to_config_push_state(action: dict) -> dict:
    """Build the CWM (Serverless Workflow) operation state that pushes the golden-config
    applications via NSO.RestconfPost -- same pattern as the existing MOP config steps."""
    resource = action.get("resourceId") or "nso-dCloud"
    payload = json.dumps({"application": _applications(action)})
    return {
        "name": f"apply-{action['name']}",
        "type": "operation",
        "actions": [{
            "name": f"{action['name']}Apply",
            "retryRef": "Default",
            "functionRef": {
                "refName": "NSO.RestconfPost",
                "arguments": {
                    "input": {
                        "data": payload,
                        "path": "restconf/data/golden-config:golden-config",
                        "header": {
                            "Accept": "application/yang-data+json",
                            "Content-Type": "application/yang-data+json",
                        },
                    },
                    "config": {"resourceId": resource},
                },
            },
        }],
    }


def to_golden(action: dict) -> dict:
    """Bundle everything needed to turn a config action into a golden-config template +
    per-device binding + a CWM config-push workflow state."""
    return {
        "templateName": _tmpl_name(action),
        "templateXml": to_golden_template(action),
        "applicationXml": to_golden_application(action),
        "pushState": to_config_push_state(action),
        "nodes": action.get("nodes") or [],
    }


def create_golden_local(template_xml: str, ncs_user="admin", timeout=90) -> dict:
    """Create the golden-config template in the local NSO (portal runs on the NSO host) via
    ncs_cli `load merge` + commit. Returns {ok, output, error}. Requires the golden-config
    NSO package to be loaded on that NSO."""
    fd, path = tempfile.mkstemp(prefix="gc-tmpl-", suffix=".xml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(template_xml)
        script = (f"source /opt/ncs/current/ncsrc; ncs_cli -C -u {ncs_user} <<'NCSEOF'\n"
                  f"configure\nload merge {path}\ncommit\nexit\nNCSEOF")
        p = subprocess.run(["bash", "-lc", script], capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        ok = ("Commit complete" in out) or ("No modifications to commit" in out)
        res = {"ok": ok, "output": out.strip()[:1500]}
        if not ok:
            res["error"] = "NSO did not confirm commit (is the golden-config package loaded on this NSO?)"
        return res
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": "", "error": str(e)}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _exec_arg(command: str):
    """Split a device command into the NSO live-status exec verb + argument."""
    c = command.strip()
    low = c.lower()
    if low.startswith("show "):
        return "show", c[5:]
    return "any", c


def dry_run_check(check: dict, nso_host: str, nso_user="cisco", nso_pass="cisco",
                  ncs_user="admin", timeout=45, local=False) -> dict:
    """Run the check's command on the device via NSO live-status and evaluate the match.
    If local=True (or nso_host in {local,127.0.0.1,localhost}) run ncs_cli directly on this
    host instead of tunnelling over ssh. Returns {ok, matched, output, error}."""
    node = check.get("node")
    command = check.get("command")
    expect = check.get("expect")
    ned = check.get("nedExec", "cisco-ios-xr-stats")
    if not (node and command):
        return {"ok": False, "error": "check missing node/command", "output": "", "matched": None}

    verb, arg = _exec_arg(command)
    arg = arg.replace('"', '\\"')
    cli = f'devices device {node} live-status {ned}:exec {verb} "{arg}"'
    remote = f"source /opt/ncs/current/ncsrc; ncs_cli -C -u {ncs_user} <<'NCSEOF'\n{cli}\nNCSEOF"

    is_local = local or str(nso_host).lower() in ("local", "127.0.0.1", "localhost", "")
    if is_local:
        cmd = ["bash", "-lc", remote]
    else:
        cmd = [
            "sshpass", "-p", nso_pass, "ssh",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={timeout}", f"{nso_user}@{nso_host}", remote,
        ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        out = r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "output": "", "matched": None}

    matched = None
    if expect:
        if check.get("match") == "regex":
            matched = re.search(expect, out) is not None
        else:
            matched = expect in out
    return {"ok": True, "output": out, "matched": matched, "cli": cli, "error": None}


def main():
    ap = argparse.ArgumentParser(description="NL -> structured pre/post check (+ optional dry-run).")
    ap.add_argument("sentence", help="the natural-language check description")
    ap.add_argument("--dry-run", action="store_true", help="run the check on the device via NSO")
    ap.add_argument("--nso", default="198.18.134.29", help="NSO host for dry-run")
    ap.add_argument("--nso-user", default="cisco")
    ap.add_argument("--nso-pass", default="cisco")
    args = ap.parse_args()

    parsed = parse_check(args.sentence)
    if args.dry_run:
        parsed["dryRun"] = dry_run_check(parsed["check"], args.nso, args.nso_user, args.nso_pass)
    print(json.dumps(parsed, indent=2))


if __name__ == "__main__":
    main()
