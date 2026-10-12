#!/usr/bin/env python3
"""
get_cnc_token.py -- obtain a Crosswork (CNC) CAS JWT for calling the CWM / RESTCONF APIs.

CNC's SSO is CAS. Auth is a two-step flow:
  1) POST /crosswork/sso/v1/tickets           (username+password)     -> TGT (ticket-granting ticket)
  2) POST /crosswork/sso/v1/tickets/<TGT>      (service=<registered>)  -> JWT (service ticket, a JWT)

The service MUST be a registered CAS service. An arbitrary API URL is rejected
("... not found in service registry"), but the registered app  '<host>/app-dashboard'
returns a signed JWT that the Tyk gateway accepts as `Authorization: Bearer <jwt>`
for every /crosswork/cwm/v2/* (and RESTCONF) call.

The password is NEVER taken from the command line. Provide it via the CNC_PASSWORD
environment variable or the interactive prompt (so it is not stored in shell history).

Examples:
  # full login, print JWT
  CNC_PASSWORD='${CNC_PASSWORD}' ./get_cnc_token.py --host 198.18.134.221 --user admin

  # reuse an existing TGT (skip password step), print a fresh JWT
  ./get_cnc_token.py --host 198.18.134.221 --tgt 'TGT-18-...-cas-...'

  # just mint/print a TGT to reuse later
  CNC_PASSWORD='${CNC_PASSWORD}' ./get_cnc_token.py --host 198.18.134.221 --user admin --print tgt

  # use it directly in curl
  JWT=$(./get_cnc_token.py --host 198.18.134.221 --tgt "$TGT")
  curl -sk -H "Authorization: Bearer $JWT" https://198.18.134.221:30603/crosswork/cwm/v2/workflow
"""
import argparse
import getpass
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request

PORT = 30603
SSO = "/crosswork/sso/v1/tickets"


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE  # CNC uses a self-signed cert
    return c


def _post(url, form, accept="text/plain", safe=""):
    # NOTE: CNC's CAS rejects a %21-encoded '!' in the password -- it wants the literal
    # char. Pass safe="!" for the login body so urlencode leaves '!' (and only '!') alone
    # while still encoding &, =, %, spaces, etc. correctly.
    data = urllib.parse.urlencode(form, safe=safe).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": accept},
    )
    with urllib.request.urlopen(req, context=_ctx(), timeout=30) as r:
        return r.status, r.getheader("Location", ""), r.read().decode(errors="replace")


def get_tgt(base, user, password):
    # CNC CAS wants a literal '!' in the password body (not %21).
    status, location, body = _post(f"{base}{SSO}", {"username": user, "password": password},
                                   safe="!")
    # CAS returns 201 Created with the TGT in the Location header (and/or body).
    m = re.search(r"TGT-[\w.\-]+", location) or re.search(r"TGT-[\w.\-]+", body)
    if not m:
        sys.exit(f"ERROR: no TGT in response (HTTP {status}). Body head: {body[:200]!r}")
    return m.group(0)


def get_jwt(base, tgt, service):
    status, _loc, body = _post(f"{base}{SSO}/{tgt}", {"service": service})
    body = body.strip()
    if not body.startswith("eyJ"):  # a JWT starts with base64url of '{"alg"...'
        sys.exit(f"ERROR: CAS did not return a JWT (HTTP {status}). Response: {body[:200]!r}")
    return body


def main():
    ap = argparse.ArgumentParser(description="Get a CNC CAS JWT (or TGT) for API calls.")
    ap.add_argument("--host", required=True, help="CNC host/IP (e.g. 198.18.134.221)")
    ap.add_argument("--port", type=int, default=PORT, help=f"HTTPS port (default {PORT})")
    ap.add_argument("--user", default="admin", help="username (default: admin)")
    ap.add_argument("--tgt", help="reuse an existing TGT instead of logging in")
    ap.add_argument("--service", help="CAS service to mint the ticket for "
                                       "(default: https://<host>:<port>/app-dashboard)")
    ap.add_argument("--print", dest="what", choices=["jwt", "tgt", "both"], default="jwt",
                    help="what to print (default: jwt)")
    args = ap.parse_args()

    base = f"https://{args.host}:{args.port}"
    service = args.service or f"{base}/app-dashboard"

    tgt = args.tgt
    if not tgt:
        password = os.environ.get("CNC_PASSWORD") or getpass.getpass(f"Password for {args.user}@{args.host}: ")
        tgt = get_tgt(base, args.user, password)

    if args.what == "tgt":
        print(tgt)
        return

    jwt = get_jwt(base, tgt, service)
    if args.what == "both":
        sys.stderr.write(f"TGT={tgt}\n")
    print(jwt)


if __name__ == "__main__":
    main()
