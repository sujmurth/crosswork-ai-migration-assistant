#!/usr/bin/env python3
"""Local functional test of the QoS agent's deterministic pipeline.

Stubs the platform-only deps (cwai.adk, langchain) so we can exercise the pure
logic — device pick, QoS extraction, platform detection, workflow generation —
against the real node-16 config, without the Crosswork AI platform.
The LLM translation step runs on .87 at runtime and is not exercised here.
"""
import sys, types, json

# --- stub platform-only imports so main.py loads locally ---
adk = types.ModuleType("cwai.adk")
class AssistantMessage:
    def __init__(self, content="", follow_ups=None): self.content = content; self.follow_ups = follow_ups or []
class Context: ...
class Message: ...
class Result:
    def __init__(self, message=None, data=None): self.message = message; self.data = data
class _Agent:
    def on_message(self, name):
        def deco(fn): return fn
        return deco
    def start(self): ...
adk.AssistantMessage = AssistantMessage; adk.Context = Context; adk.Message = Message
adk.Result = Result; adk.agent = _Agent()
cwai = types.ModuleType("cwai"); cwai.adk = adk
lg = types.ModuleType("cwai.adk.integrations.langgraph")
class ADKChatModel:
    def __init__(self, **k): ...
lg.ADKChatModel = ADKChatModel
lcm = types.ModuleType("langchain_core.messages")
class HumanMessage:
    def __init__(self, content=""): self.content = content
class SystemMessage:
    def __init__(self, content=""): self.content = content
lcm.HumanMessage = HumanMessage; lcm.SystemMessage = SystemMessage
lc = types.ModuleType("langchain_core")
for m, mod in [("cwai", cwai), ("cwai.adk", adk),
               ("cwai.adk.integrations", types.ModuleType("cwai.adk.integrations")),
               ("cwai.adk.integrations.langgraph", lg),
               ("langchain_core", lc), ("langchain_core.messages", lcm)]:
    sys.modules[m] = mod

import main  # noqa: E402

print("skill loaded:", bool(main.SKILL), "| qos-ref loaded:", bool(main.QOS_REF))
print("bundled sources:", list(main.SOURCES))
print()

for prompt in ["list QoS migration candidates",
               "migrate node-16 QoS to Cisco 8000",
               "translate the ASR9K qos to 8000"]:
    src = main._find_source(prompt)
    print(f"prompt: {prompt!r}")
    if not src:
        print("  -> would list candidates / ask for device\n")
        continue
    name, cfg = src
    plat = main._platform_of(name, cfg)
    qos = main._extract_qos(cfg)
    print(f"  picked device: {name}  ({plat})")
    print(f"  extracted QoS: {qos.count('policy-map ')} policy-maps, {qos.count('class-map ')} class-maps, {len(qos)} chars")
    print()

# simulate the approval-stage generation with a small fake translated block
fake = ("Here is the translated config:\n```\npolicy-map PM-INGRESS\n class CM-VOICE\n"
        "  set traffic-class 7\n!\npolicy-map EGRESS-Q\n class TC7\n  priority level 1\n!\n"
        "interface TenGigE0/0/0/0\n service-policy input PM-INGRESS\n```\nSection 5 notes...")
cfg = main._extract_config_block(fake)
gc, tname = main._golden_config_template("node-16", cfg)
app = main._golden_config_application("node-16", tname, "node-1")
checks = main._gen_checks("node-16", cfg, "node-1")
wf = main._gen_workflow("node-16", tname, "node-1", checks)
print("=== golden-config template (name + body) ===")
print("template:", gc["golden-config:template"][0]["name"])
print("config body:\n" + gc["golden-config:template"][0]["version"][0]["config"])
print("=== golden-config application ===")
print(json.dumps(app))
print(f"=== checks: {len(checks['pre'])} pre, {len(checks['post'])} post ===")
for c in checks["pre"] + checks["post"]:
    print("  ", c["name"], "->", c.get("command"), "| expect:", c.get("expect", c.get("expect_absent", "")))
print("=== workflow states ===")
for s in wf["states"]:
    print("  -", s["name"])
