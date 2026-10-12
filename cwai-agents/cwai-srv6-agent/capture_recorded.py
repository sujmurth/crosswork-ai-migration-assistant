#!/usr/bin/env python3
"""Capture real SRv6 fabric outputs from CNC (.221) into recorded-fabric.json.

Read-only: runs `show` commands via the CNC->NSO live-status proxy. Used to
ground the Crosswork AI demo agent when the CWAI platform cannot reach CNC.
"""
import json, os, ssl, subprocess, sys, urllib.request, urllib.error

HOST = os.environ.get("CNC_HOST", "198.18.134.221")
PORT = os.environ.get("CNC_PORT", "30603")
USER = os.environ.get("CNC_USER", "admin")
PASS = os.environ.get("CNC_PASSWORD", "${CNC_PASSWORD}")
BASE = f"https://{HOST}:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_HELPER = os.path.join(os.path.dirname(HERE), "get_cnc_token.py")
OUT = os.path.join(HERE, "recorded-fabric.json")

# (phase, node, command, ned)
XR = "tailf-ned-cisco-ios-xr-stats"
COMMANDS = [
    ("Prepare",       "node-2", "show isis neighbors", XR),
    ("Prepare",       "node-2", "show mpls ldp neighbor brief", XR),
    ("Prepare",       "node-2", "show bgp vpnv4 unicast summary", XR),
    ("SRv6 locators", "node-2", "show segment-routing srv6 locator", XR),
    ("IS-IS SRv6",    "node-2", "show isis segment-routing srv6 locators", XR),
    ("BGP RRv6",      "node-2", "show bgp vpnv6 unicast summary", XR),
    ("L3VPN -> SRv6", "node-1", "show cef vrf vpn-101 30.1.4.0/24", XR),
    ("L3VPN -> SRv6", "node-2", "show cef vrf vpn-101 30.1.4.0/24", XR),
    ("L3VPN -> SRv6", "node-4", "show cef vrf vpn-101 30.1.1.0/24", XR),
    ("L3VPN -> SRv6", "node-2", "show segment-routing srv6 sid", XR),
    ("L3VPN -> SRv6", "node-4", "show segment-routing srv6 sid", XR),
    ("L2VPN -> SRv6", "node-2", "show l2vpn xconnect group vpn-129", XR),
    ("L2VPN -> SRv6", "node-4", "show l2vpn xconnect group vpn-129", XR),
    ("L2VPN -> SRv6", "node-2", "show running-config evpn", XR),
    ("Cutover",       "node-2", "show bgp vpnv4 unicast vrf vpn-101", XR),
    ("Verify",        "node-1", "show cef vrf vpn-101 30.1.4.0/24", XR),
    ("Remove MPLS",   "node-1", "show mpls ldp neighbor brief", XR),
    ("Remove MPLS",   "node-2", "show mpls forwarding", XR),
]


def ctx():
    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    return c


def token():
    env = dict(os.environ, CNC_PASSWORD=PASS)
    return subprocess.check_output(
        [sys.executable, TOKEN_HELPER, "--host", HOST, "--user", USER],
        env=env, text=True).strip()


def exec_cmd(jwt, node, cmd, ned):
    p = (f"/crosswork/proxy/nso/restconf/operations/devices/device={node}"
         f"/live-status/{ned}:exec/any")
    r = urllib.request.Request(BASE + p, data=json.dumps({"input": {"args": cmd}}).encode(),
                               headers={"Authorization": "Bearer " + jwt,
                                        "Accept": "application/yang-data+json",
                                        "Content-Type": "application/yang-data+json"},
                               method="POST")
    try:
        with urllib.request.urlopen(r, context=ctx(), timeout=60) as x:
            return next(iter(json.loads(x.read().decode()).values())).get("result", "")
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code}] {e.read().decode()[:200]}"
    except Exception as e:
        return f"[ERR] {e}"


def main():
    jwt = token()
    records = {}
    for phase, node, cmd, ned in COMMANDS:
        key = f"{node}|{cmd}"
        out = exec_cmd(jwt, node, cmd, ned)
        records.setdefault(phase, {})[key] = out
        print(f"[{phase}] {node}: {cmd}  -> {len(out)} bytes")
    payload = {"source": f"{HOST}:{PORT}", "captured_by": "capture_recorded.py",
               "phases": records}
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nSaved {OUT} ({sum(len(v) for v in records.values())} commands across {len(records)} phases)")


if __name__ == "__main__":
    main()
