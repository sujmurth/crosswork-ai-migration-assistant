#!/usr/bin/env python3
"""Load the QoS Migration MOP into CNC .49 CWM (proves the agent->CWM load path).

Read/verify only for NSO golden-config (package not present on .49). Imports a
self-contained MOP workflow into CWM via /crosswork/cwm/v2/workflowImport.
Reversible: delete via DELETE /crosswork/cwm/v2/workflow/{id}.
"""
import json, os, ssl, subprocess, sys, urllib.request, urllib.error

HOST="172.20.163.49"; PORT="30603"; USER="admin"; PASS=os.environ.get("CNC_PASSWORD","${CNC_PASSWORD}")
BASE=f"https://{HOST}:{PORT}"; HERE=os.path.dirname(os.path.abspath(__file__))
TOKEN=os.path.join(os.path.dirname(HERE),"get_cnc_token.py")

def ctx():
    c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE; return c
def tok():
    return subprocess.check_output([sys.executable,TOKEN,"--host",HOST,"--user",USER],
        env=dict(os.environ,CNC_PASSWORD=PASS),text=True).strip()
def req(method,path,jwt,body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(BASE+path,data=data,method=method,
        headers={"Authorization":"Bearer "+jwt,"Content-Type":"application/json","Accept":"application/json"})
    try:
        with urllib.request.urlopen(r,context=ctx(),timeout=40) as x:
            return x.status, x.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

MOP={
 "definition":{
   "id":"qos-migration-asr9k-to-8000",
   "name":"QoS Migration - ASR9000 to Cisco 8000",
   "version":"1.0","specVersion":"0.8","start":"pre-check",
   "states":[
     {"name":"pre-check","type":"inject",
      "data":{"stage":"pre","checks":["show running-config policy-map (baseline on target Cisco 8000)"]},
      "transition":"load-template"},
     {"name":"load-template","type":"inject",
      "data":{"artifact":"golden-config template ct-qos-<src>-8000 (native/merge), translated by the QoS Migration Agent"},
      "transition":"dry-run"},
     {"name":"dry-run","type":"inject",
      "data":{"action":"apply golden-config with ?dry-run=native; surface native diff on target"},
      "transition":"approve"},
     {"name":"approve","type":"inject",
      "data":{"action":"operator approval (pause-mop) before commit"},
      "transition":"apply"},
     {"name":"apply","type":"inject",
      "data":{"action":"apply-golden-config-cwm-sol on target Cisco 8000 (node-1)"},
      "transition":"post-check"},
     {"name":"post-check","type":"inject",
      "data":{"checks":["policy-map present","no random-detect","no egress set qos-group","interface service-policy bound"]},
      "end":True}
   ]
 },
 "wfTags":["QoS-Migration","ASR9000-to-8000","MOP","cisco-8000"]
}

def main():
    jwt=tok()
    st,body=req("POST","/crosswork/cwm/v2/workflowImport",jwt,[MOP])
    print(f"workflowImport -> HTTP {st}\n{body[:400]}")
    # verify
    st2,lst=req("GET","/crosswork/cwm/v2/workflow",jwt)
    try:
        wfs=json.loads(lst); wfs=wfs if isinstance(wfs,list) else wfs.get("workflows",[])
        match=[w for w in wfs if "QoS Migration" in (w.get("name","") or "")]
        print("verify: found", len(match), "QoS Migration workflow(s):",
              [(w.get("name"),w.get("workflowId")) for w in match])
    except Exception as e:
        print("verify parse err", e)

if __name__=="__main__": main()
