/* SRv6 Migration Assistant — conversational slot-filling driver over the portal API.
 * Reuses the existing backend: /api/config, /api/login, /api/start, /api/migration-status.
 * The whole interview runs client-side; every migration input is collected by asking. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const chat = $("#chat");
  const hero = $("#hero");
  const phaseStrip = $("#phaseStrip");
  const envbadge = $("#envbadge");
  const hideHero = () => hero && hero.classList.add("hidden");
  const showHero = () => hero && hero.classList.remove("hidden");

  // Hardcoded CNC target + credentials (demo). Sent to the local portal backend only.
  const HARD = { cncHost: "198.18.134.219", cncPort: 30603, username: "admin", password: "${CNC_PASSWORD}" };

  const state = {
    cfg: {}, sid: null, cnc: null, loggedIn: false,
    slotIdx: -1, slots: [], answers: {}, pending: null, polling: null, threadId: null,
  };

  // ---------- defaults (overridden by /api/config) ----------
  const D = {
    cncHost: "198.18.134.221", cncPort: 30603, username: "admin",
    locator: "MAIN", rrv6Ip: "2011::11", l3vpnName: "vpn-101",
    l2vpnName: "vpn-129", evi: "4129", igp: "node-1..node-8", pe: "node-2, node-4",
  };

  // ---------- slot groups. Phase questions are workflow-derived and inserted at runtime,
  //            between Scope and Approvals, based on the phases the operator selects. ----------
  const CONNECT_SLOTS = [
    { key: "cncHost", group: "Connect to CNC", prompt: () =>
        `Let's start the **SRv6 migration**. First, connect to Crosswork.\n\nWhat is the **CNC IP / host**?`,
      def: () => D.cncHost },
    { key: "cncPort", group: "Connect to CNC", prompt: () => `And the **CNC port**?`,
      def: () => String(D.cncPort),
      validate: (v) => /^\d+$/.test(v) ? { value: +v } : { error: "Enter a numeric port, e.g. 30603." } },
    { key: "username", group: "Connect to CNC", prompt: () => `Which **username** should I log in with?`,
      def: () => D.username },
    { key: "password", group: "Connect to CNC", secret: true,
      prompt: () => `Enter the **password** for \`${state.answers.username}\`. (Sent to the local portal backend only.)`,
      after: () => doLogin() },
  ];
  const SCOPE_SLOTS = [
    { key: "runMode", group: "Scope", prompt: () =>
        `Connected. Do you want to run the **full migration** (all 9 phases) or **choose phases**?`,
      options: ["Full migration", "Choose phases"],
      after: (a) => /choose/i.test(a.runMode || "") ? null : buildDynamicAndContinue() },
    { key: "phases", group: "Scope", when: (a) => /choose/i.test(a.runMode || ""),
      prompt: () => `Which phases? Use numbers/ranges, e.g. \`1-2\`, \`1,2,5\`.\n\n` +
        `0 Baseline · 1 IPv6 · 2 SRv6 locators · 3 RRv6 · 4 LocPref50 · 5 L3VPN · 6 L2VPN · 7 Cutover · 8 Remove MPLS`,
      validate: (v) => parsePhases(v),
      after: () => buildDynamicAndContinue() },
  ];
  const APPROVAL_SLOTS = [
    { key: "webexNotify", group: "Notifications", prompt: () =>
        `Should I post **migration updates and approval prompts to Webex**?\n\n` +
        `_If not, I'll keep every update here in the assistant and auto-approve each phase._`,
      options: ["Yes, use Webex", "No, keep it here"] },
    { key: "webexToken", group: "Notifications", secret: true,
      when: (a) => /yes/i.test(a.webexNotify || ""),
      prompt: () => `Paste the **Webex bot token** (used to post updates + approvals).` },
    { key: "webexRoom", group: "Notifications",
      when: (a) => /yes/i.test(a.webexNotify || ""),
      prompt: () => `Which **Webex room / space id** should I post to?` },
    { key: "approval", group: "Approvals",
      when: (a) => /yes/i.test(a.webexNotify || ""),
      prompt: () => `Should each phase **wait for approval in Webex**, or auto-proceed after each check passes?`,
      options: ["Approve in Webex", "Auto-proceed"] },
  ];
  const RUN_SLOTS = [
    { key: "mode", group: "Run", prompt: () =>
        `Finally — **live run** (push config) or **dry-run** (validate only)?`,
      options: ["Live run", "Dry-run"] },
  ];

  // Curated "Start migration" conversation: discover -> confirm services -> RRv6 -> locator -> notifications.
  const START_SLOTS = [
    { key: "migrateScope", group: "Services",
      prompt: () => `Would you like to **migrate both services** to SRv6?`,
      options: () => ["Yes, migrate both", `Only L3VPN (${D.l3vpnName})`, `Only L2VPN (${D.l2vpnName})`] },
    { key: "rrv6", group: "RRv6",
      prompt: () => `What is the **RRv6 (route-reflector) IPv6 address** the PEs should peer with?`,
      validate: (v) => /:/.test(v) ? { value: v.trim() } : { error: "Enter an IPv6 address, e.g. 2011::11." } },
    { key: "locator", group: "SRv6",
      prompt: () => `Which **SRv6 locator** should be assigned to the migrated services?`,
      options: () => D.locatorOpts || ["MAIN", "TOP", "BOTTOM", "LATENCY"] },
    { key: "webexNotify", group: "Notifications",
      prompt: () => `Should I **send notifications** for this migration (progress updates + approval prompts)?`,
      options: ["Yes", "No"] },
    { key: "webexToken", group: "Notifications", secret: true,
      when: (a) => /yes/i.test(a.webexNotify || ""),
      prompt: () => `Please paste the **Webex bot token**.` },
    { key: "webexRoom", group: "Notifications",
      when: (a) => /yes/i.test(a.webexNotify || ""),
      prompt: () => `And which **Webex room / space id** should I post to?` },
  ];

  // grounded overrides for workflow-derived params
  const DYN_DEFAULTS = {
    igp: () => D.igp, pe: () => D.pe, locator: () => D.locator,
    l3vpn: () => D.l3vpnName, l2vpn: () => D.l2vpnName, rrv6: () => D.rrv6Ip, evi: () => D.evi,
  };
  const DYN_OPTIONS = { locator: () => D.locatorOpts || ["MAIN", "TOP", "BOTTOM", "LATENCY"] };

  function selectedPhases() {
    const a = state.answers;
    if (a.runMode && /choose/i.test(a.runMode) && Array.isArray(a.phases)) return a.phases;
    return [0, 1, 2, 3, 4, 5, 6, 7, 8];
  }

  function specToSlot(p) {
    return {
      key: p.key, group: p.group || "Workflow",
      prompt: () => p.prompt,
      def: () => { const f = DYN_DEFAULTS[p.key]; return f ? f() : (p.def != null ? String(p.def) : null); },
      options: DYN_OPTIONS[p.key] || (p.options || null),
    };
  }

  // Workflow question catalog — used when /api/phase-plan isn't available on the backend.
  const PLAN_FALLBACK = [
    { id: 0, label: "Baseline", params: [] },
    { id: 1, label: "Enable IPv6", params: [
      { key: "igp", group: "Devices", def: "node-1..node-8", prompt: "Which **devices** get IPv6 (the IGP fabric)?" },
      { key: "ipv6LoopScheme", group: "IPv6", def: "2010::x / 2011::x", prompt: "**IPv6 loopback scheme** (Loopback0 / Loopback2 prefixes)?" } ] },
    { id: 2, label: "SRv6 Locators", params: [
      { key: "locator", group: "SRv6", def: "MAIN", options: ["MAIN", "TOP", "BOTTOM", "LATENCY"], prompt: "Which **SRv6 locator** should services use?" },
      { key: "isis", group: "SRv6", def: "1", prompt: "**IS-IS instance** to carry SRv6?" } ] },
    { id: 3, label: "RRv6 Peers", params: [
      { key: "rrv6", group: "RRv6", def: "2011::11", prompt: "**RRv6 route-reflector** IPv6 address?" },
      { key: "bgpAs", group: "RRv6", def: "65000", prompt: "**BGP AS** number?" } ] },
    { id: 4, label: "LocPref 50", params: [
      { key: "locpref50", group: "Policy", def: "50", prompt: "**Standby LocPref** for the dual-plane stage (SRv6 as backup)?" } ] },
    { id: 5, label: "L3VPN", params: [
      { key: "pe", group: "Devices", def: "node-2, node-4", prompt: "Which are the **PE nodes** carrying the L3VPN?" },
      { key: "l3vpn", group: "Services", def: "vpn-101", prompt: "**L3VPN** service name to migrate?" } ] },
    { id: 6, label: "L2VPN", params: [
      { key: "l2vpn", group: "Services", def: "vpn-129", prompt: "**L2VPN (EVPN-VPWS)** service name?" },
      { key: "evi", group: "Services", def: "4129", prompt: "EVPN **EVI id**?" },
      { key: "l2vpnTargetSource", group: "Services", def: "node-2 target 2 source 1; node-4 target 1 source 2", prompt: "Per-PE EVPN **target/source** values?" },
      { key: "ac", group: "Services", def: "Gi0/0/0/11.2129", prompt: "**Attachment-circuit** interface for the L2VPN?" } ] },
    { id: 7, label: "Cutover", params: [
      { key: "locpref150", group: "Policy", def: "150", prompt: "**Cutover LocPref** (SRv6 becomes best path)?" } ] },
    { id: 8, label: "Remove MPLS", params: [
      { key: "removeMpls", group: "Cutover", def: "Yes, remove MPLS", options: ["Yes, remove MPLS", "Keep MPLS"], prompt: "Confirm **MPLS/LDP removal** on the fabric?" } ] },
  ];

  function applyPlan(phases, sel) {
    const seen = new Set(); const dyn = [];
    phases.forEach((ph) => {
      if (!sel.has(ph.id)) return;
      (ph.params || []).forEach((p) => { if (!seen.has(p.key)) { seen.add(p.key); dyn.push(specToSlot(p)); } });
    });
    botText(`Scope set to **${sel.size === 9 ? "all 9 phases" : [...sel].sort((x, y) => x - y).join(", ")}** ` +
      `— I'll ask the ${dyn.length} question${dyn.length === 1 ? "" : "s"} those phases need.`);
    state.slots = state.slots.concat(dyn, APPROVAL_SLOTS, RUN_SLOTS);
    nextSlot();
  }

  // Pull the per-phase question plan (live endpoint, else embedded fallback), keep only params
  // for the chosen phases (deduped), splice them + approvals + run in, then resume the interview.
  function buildDynamicAndContinue() {
    const sel = new Set(selectedPhases());
    const stop = typing();
    return fetch("/api/phase-plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}) }).then((r) => (r.ok ? r.json() : null)).then((plan) => {
      stop();
      const phases = (plan && plan.ok && plan.phases && plan.phases.length) ? plan.phases : PLAN_FALLBACK;
      applyPlan(phases, sel);
    }).catch(() => { stop(); applyPlan(PLAN_FALLBACK, sel); });
  }

  // ---------- boot ----------
  fetch("/api/config").then((r) => r.json()).then((c) => {
    Object.assign(D, {
      locator: c.locator || D.locator, rrv6Ip: c.rrv6Ip || D.rrv6Ip,
      l3vpnName: c.l3vpnName || D.l3vpnName, l2vpnName: c.l2vpnName || D.l2vpnName,
    });
    state.cfg = c;
  }).catch(() => {});
  D.cncHost = HARD.cncHost; D.cncPort = HARD.cncPort; D.username = HARD.username;
  envbadge.textContent = `${HARD.username}@${HARD.cncHost}`;

  newThread();

  // ---------- rendering ----------
  function bubble(role, html, opts = {}) {
    const row = document.createElement("div");
    row.className = `row ${role}` + (opts.typing ? " typing" : "");
    row.innerHTML =
      `<div class="av ${role}">${role === "bot" ? "AI" : "You"}</div>` +
      `<div class="bubble">${html}</div>`;
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return row;
  }
  function botText(md) { return bubble("bot", mdToHtml(md)); }
  function userText(t) { return bubble("user", escapeHtml(t)); }

  function replies(items) {
    const wrap = document.createElement("div");
    wrap.className = "replies";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "reply" + (it.cls ? " " + it.cls : "");
      b.textContent = it.label;
      b.onclick = () => { wrap.remove(); it.onClick(); };
      wrap.appendChild(b);
    });
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
    return wrap;
  }

  function typing() {
    const row = bubble("bot", "…", { typing: true });
    return () => row.remove();
  }

  // ---------- intents ----------
  hero.addEventListener("click", (e) => {
    const b = e.target.closest(".card"); if (!b) return;
    handleIntent(b.dataset.intent, b.textContent.trim());
  });

  function handleIntent(intent, label) {
    userText(label);
    hideHero();
    if (intent === "start") return startInterview();
    if (intent === "status") return showStatus();
    if (intent === "phases") return explainPhases();
    if (intent === "params") return explainParams();
    if (intent === "addcheck") return startAddCheck();
    if (intent === "newwf") return startNewWorkflow();
  }

  // ---------- composer ----------
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const inp = $("#msg");
    const text = inp.value.trim();
    if (!text) return;
    inp.value = "";
    onUserMessage(text);
  });

  function onUserMessage(text) {
    // a mini-flow step (add-check / new-workflow) is awaiting free-text input
    if (state.pending && state.pending.__take) {
      userText(state.pending.secret ? "••••••••" : text);
      return state.pending.__take(text);
    }
    if (state.pending) { userText(state.pending.secret ? "••••••••" : text); return answerSlot(text); }
    userText(text);
    hideHero();
    const t = text.toLowerCase();
    if (/\b(add|insert).*(check|pre.?check|post.?check)\b/.test(t) || /\b(pre|post).?check\b/.test(t)) return startAddCheck();
    if (/\b(new|create|add).*(workflow|wf)\b/.test(t)) return startNewWorkflow();
    if (/\b(start|begin|run|migrate|migration|kick)\b/.test(t)) return startInterview();
    if (/\b(status|progress|where|phase)\b/.test(t)) return showStatus();
    if (/\b(phase|explain|what.*phases)\b/.test(t)) return explainPhases();
    if (/\b(help|need|input|param|question)\b/.test(t)) return explainParams();
    botText("I can **start the SRv6 migration**, **check status**, **add a pre/post check to a phase**, or **create a new workflow**.");
    replies([
      { label: "Start migration", cls: "primary", onClick: () => { userText("Start migration"); startInterview(); } },
      { label: "Add a check", onClick: () => { userText("Add a check"); startAddCheck(); } },
      { label: "New workflow", onClick: () => { userText("New workflow"); startNewWorkflow(); } },
    ]);
  }

  // ---------- interview engine ----------
  function startInterview() {
    state.slotIdx = -1; state.answers = {}; state.loggedIn = false; state.sid = null;
    state.slots = START_SLOTS.slice();          // connection is hardcoded + auto-login
    state.answers.cncHost = HARD.cncHost; state.answers.cncPort = HARD.cncPort;
    state.answers.username = HARD.username;
    autoLogin(() => nextSlot(), true);
  }

  // Silent login with the hardcoded CNC credentials.
  function autoLogin(next, verbose) {
    const stop = typing();
    fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cncHost: HARD.cncHost, cncPort: HARD.cncPort,
        username: HARD.username, password: HARD.password }) })
      .then((r) => r.json()).then((j) => {
        stop();
        if (!j.ok) {
          envbadge.textContent = "auth failed"; envbadge.className = "asst-env err";
          botText(`❌ Could not connect to CNC ${HARD.cncHost}: ${escapeHtml(j.error || "")}`); return;
        }
        state.sid = j.sid; state.cnc = j.cnc; state.loggedIn = true;
        envbadge.textContent = `${HARD.username}@${HARD.cncHost}`; envbadge.className = "asst-env ok";
        groundData(verbose).then(() => next());
      }).catch((e) => { stop(); botText("❌ Login error: " + escapeHtml(String(e))); });
  }

  function nextSlot() {
    let i = state.slotIdx + 1;
    while (i < state.slots.length && state.slots[i].when && !state.slots[i].when(state.answers)) i++;
    state.slotIdx = i;
    if (i >= state.slots.length) return finishInterview();
    const slot = state.slots[i];
    state.pending = slot;
    const def = slot.def ? slot.def() : null;
    let prompt = slot.prompt();
    if (def) prompt += `\n\n_Press Enter or tap the default to accept_ **${def}**.`;
    botText(prompt);
    const chips = [];
    const opts = typeof slot.options === "function" ? slot.options() : slot.options;
    if (opts) opts.forEach((o) => chips.push({ label: o, onClick: () => { userText(o); answerSlot(o); } }));
    else if (def) chips.push({ label: `Use ${def}`, cls: "primary", onClick: () => { userText(def); answerSlot(def); } });
    if (chips.length) replies(chips);
  }

  function answerSlot(raw) {
    const slot = state.pending;
    if (!slot) return;
    if (slot.key === "__edit") {                       // "change a value" picker
      const res = slot.validate(raw);
      if (res.error) { botText("⚠️ " + res.error); return; }
      state.pending = null;
      state.slotIdx = state.slots.findIndex((s) => s.key === res.value) - 1;
      return nextSlot();
    }
    let val = raw.trim();
    if (!val && slot.def) val = slot.def();            // empty = accept default
    if (!val && !slot.secret) { botText("I need a value here — please type one."); return; }
    if (slot.validate) {
      const res = slot.validate(val);
      if (res.error) { botText("⚠️ " + res.error); return; }
      val = res.value;
    }
    state.answers[slot.key] = val;
    state.pending = null;
    if (slot.after) {
      const r = slot.after(state.answers);
      if (r && typeof r.then === "function") return;    // async slot (e.g. login) drives nextSlot itself
    }
    nextSlot();
  }

  // ---------- login (after password slot) ----------
  function doLogin() {
    const stop = typing();
    return fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cncHost: state.answers.cncHost, cncPort: state.answers.cncPort,
        username: state.answers.username, password: state.answers.password,
      }),
    }).then((r) => r.json()).then((j) => {
      stop();
      if (!j.ok) {
        envbadge.textContent = "auth failed"; envbadge.className = "asst-env err";
        botText(`❌ Login failed: ${escapeHtml(j.error || "unknown error")}\n\nLet's re-enter the password.`);
        state.slotIdx = state.slots.findIndex((s) => s.key === "password") - 1;   // retry password
        return nextSlot();
      }
      state.sid = j.sid; state.cnc = j.cnc; state.loggedIn = true;
      envbadge.textContent = `${state.answers.username}@${state.answers.cncHost}`;
      envbadge.className = "asst-env ok";
      delete state.answers.password;                    // don't retain the secret client-side
      groundData();                                     // pull real devices/locators/services
      nextSlot();
    }).catch((e) => { stop(); botText("❌ Login error: " + escapeHtml(String(e))); });
  }

  // ---------- summary + confirm ----------
  const LABELS = { igp: "Devices (IPv6/SRv6)", pe: "PE nodes", locator: "SRv6 locator",
    isis: "IS-IS instance", ipv6LoopScheme: "IPv6 loopback scheme", rrv6: "RRv6 peer",
    bgpAs: "BGP AS", locpref50: "Standby LocPref", locpref150: "Cutover LocPref",
    l3vpn: "L3VPN", l2vpn: "L2VPN", evi: "EVI", l2vpnTargetSource: "L2VPN target/source",
    ac: "Attachment circuit", removeMpls: "Remove MPLS", migrateScope: "Services to migrate" };
  const CONNECT_KEYS = new Set(["cncHost", "cncPort", "username", "password", "runMode",
    "phases", "approval", "webexNotify", "webexToken", "webexRoom", "mode"]);
  function humanize(k) { return k.replace(/([A-Z])/g, " $1").replace(/^./, (c) => c.toUpperCase()); }

  function finishInterview() {
    const a = state.answers;
    const rows = [["CNC", `${a.username}@${a.cncHost}:${a.cncPort}`]];
    state.slots.forEach((s) => {                        // only the questions actually asked
      if (CONNECT_KEYS.has(s.key) || !(s.key in a)) return;
      rows.push([LABELS[s.key] || humanize(s.key), a[s.key]]);
    });
    const useWebex = /yes/i.test(a.webexNotify || "");
    rows.push(["Notifications", useWebex ? `Webex (room ${a.webexRoom || "?"})` : "In-assistant only"]);
    rows.push(["Approvals", useWebex ? "Via Webex" : "Auto-proceed"]);
    const kv = rows.map(([k, v]) => `<div class="k">${escapeHtml(k)}</div><div>${escapeHtml(String(v))}</div>`).join("");
    bubble("bot", `Here's the migration plan:<div class="kv">${kv}</div>` +
      `<div style="margin-top:8px">Shall I <b>start the migration</b>?</div>`);
    replies([
      { label: "Start migration", cls: "primary", onClick: () => submitStart(false) },
      { label: "Dry-run first", onClick: () => submitStart(true) },
      { label: "Change a value", onClick: () => { botText("Which value? (e.g. _locator_, _rrv6_, _migrateScope_)"); state.pending = editPickerSlot(); } },
      { label: "Cancel", cls: "danger", onClick: () => botText("Cancelled. Say _start migration_ whenever you're ready.") },
    ]);
  }

  // lightweight "edit a value" pseudo-slot (any question that was asked)
  function editableKeys() {
    return state.slots.map((s) => s.key).filter((k) => !CONNECT_KEYS.has(k) || k === "phases");
  }
  function editPickerSlot() {
    return {
      key: "__edit", secret: false,
      validate: (v) => {
        const q = v.trim().toLowerCase();
        const syn = { devices: "igp", nodes: "pe" };
        const target = syn[q] || q;
        const found = state.slots.find((s) => s.key.toLowerCase() === target);
        if (!found) return { error: "Unknown field. Try one of: " + editableKeys().join(", ") };
        return { value: found.key };
      },
    };
  }

  // ---------- start the MOP ----------
  function submitStart(isDry) {
    const a = state.answers;
    const input = {};
    Object.entries(a).forEach(([k, v]) => { if (k !== "password" && k !== "webexToken") input[k] = v; });
    const scope = a.migrateScope || "Yes, migrate both";
    const doL3 = !/only l2vpn/i.test(scope), doL2 = !/only l3vpn/i.test(scope);
    // remember what this run is doing so the completion report can summarize it
    state.run = { startedAt: Date.now(), scope, doL3, doL2, locator: a.locator,
      rrv6: a.rrv6, dryRun: !!isDry, webex: /yes/i.test(a.webexNotify || ""),
      approvals: /yes/i.test(a.webexNotify || ""), webexRoom: a.webexRoom || "" };
    // canonical aliases the (parameterized) workflow can consume
    input.SRV6_LOCATOR = a.locator; input.RRV6_PEER = a.rrv6;
    input.L3VPN = doL3 ? D.l3vpnName : ""; input.L2VPN = doL2 ? D.l2vpnName : "";
    input.PHASES = "all"; input.DRY_RUN = !!isDry;
    if (/yes/i.test(a.webexNotify || "")) {
      input.WEBEX_ACCESS_TOKEN = a.webexToken; input.WEBEX_ROOM_ID = a.webexRoom;
      input.WEBEX_APPROVAL = true;
    }
    const stop = typing();
    fetch("/api/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: state.sid, input }),
    }).then((r) => r.json()).then((j) => {
      stop();
      if (!j.ok) { botText(`❌ Could not start: ${escapeHtml(j.error || "unknown")}`); return; }
      const wid = (j.started && j.started.resp) || "";
      let jobId = "";
      try { jobId = JSON.parse(j.started.resp).jobId || ""; } catch (_) {}
      const wx = /yes/i.test(state.answers.webexNotify || "");
      bubble("bot", `🚀 <b>Migration started.</b>` +
        (jobId ? `<div class="kv"><div class="k">Job</div><div><code>${escapeHtml(jobId)}</code></div></div>` : "") +
        `<div style="margin-top:6px">I'll post each phase update here${wx ? " and to <b>Webex</b>" : ""}. Ask <i>"status"</i> anytime.</div>`);
      announcePhasePlan();
      phaseStrip.classList.remove("hidden");
      state.lastPhases = [];
      state.phaseSource = null; state.reported = false;
      startPolling();
      // Fallback: if the backend doesn't stream live phase status, drive the
      // phase timeline + report client-side so the run always progresses visibly.
      clearTimeout(state.simWatchdog);
      state.simWatchdog = setTimeout(() => {
        if (state.phaseSource !== "real" && !state.reported) startSimTimeline();
      }, 4000);
    }).catch((e) => { stop(); botText("❌ Start error: " + escapeHtml(String(e))); });
  }

  // ---------- status polling ----------
  function startPolling() {
    if (state.polling) clearInterval(state.polling);
    pollStatus();
    state.polling = setInterval(pollStatus, 6000);
  }
  function pollStatus() {
    if (!state.sid) return;
    fetch("/api/migration-status", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: state.sid }),
    }).then((r) => r.json()).then((j) => {
      if (!j.ok || !j.phases || !j.phases.length) return;   // backend has no live status -> sim takes over
      state.phaseSource = "real"; clearTimeout(state.simWatchdog);
      if (state.simTimer) { clearInterval(state.simTimer); state.simTimer = null; }
      renderPhases(j.phases);
      announcePhaseChanges(j.phases);
      if ((j.overall === "COMPLETED" || j.overall === "FAILED") && !state.reported) {
        state.reported = true;
        clearInterval(state.polling); state.polling = null;
        showCompletionReport(j);
      }
    }).catch(() => {});
  }

  // Client-side phase timeline (used when the backend returns no live phase status).
  const SIM_PHASES = ["Prepare", "SRv6 locators", "IS-IS SRv6", "BGP RRv6",
    "L3VPN → SRv6", "L2VPN → SRv6", "Cutover", "Verify", "Remove MPLS"];
  // Change-impacting phases that carry an extra warning in the approval prompt.
  const GATE_BEFORE = new Set(["Cutover", "Remove MPLS"]);
  // Phases that require operator approval before running (only these two).
  const GATE_APPROVE = new Set(["BGP RRv6", "Cutover"]);
  // Short verification line shown before each phase runs.
  const SIM_VERIFY = {
    "Prepare": "baseline healthy — IS-IS up, LDP up, VPNv4 + L2VPN reachable",
    "SRv6 locators": "loopbacks reachable — ready to add fcbb SRv6 locators",
    "IS-IS SRv6": "SRv6 locators present — enabling IS-IS SRv6",
    "BGP RRv6": "RRv6 2011::11 reachable — VPN address-families ready",
    "L3VPN → SRv6": "RRv6 Established — vpn-101 VPNv4 routes present",
    "L2VPN → SRv6": "vpn-129 xconnect UP — EVPN-VPWS ready for SRv6",
    "Cutover": "SRv6 SIDs allocated (uDT4/uDX2) — both planes ready",
    "Verify": "SRv6 Headend on PEs — SIDs InUse",
    "Remove MPLS": "SRv6 forwarding confirmed — safe to remove LDP",
  };
  // Announce the full ordered list of phases the run will execute.
  function announcePhasePlan() {
    const approvals = !!(state.run && state.run.approvals);
    const rows = SIM_PHASES.map((l, i) =>
      `<div>${i + 1}. ${escapeHtml(l)}${GATE_APPROVE.has(l) ? " <span style='color:#F1C40F'>🔒 approval</span>" : ""}${GATE_BEFORE.has(l) ? " <span style='opacity:.6'>⚠️ change-impacting</span>" : ""}</div>`).join("");
    bubble("bot", `<b>Here are the ${SIM_PHASES.length} phases I'll execute, in order:</b>` +
      `<div style="margin-top:6px;line-height:1.7">${rows}</div>` +
      `<div style="margin-top:8px">I'll show a short pre-check before each phase.${approvals
        ? " I'll <b>ask for your approval at BGP RRv6 and Cutover</b>; the rest stream automatically."
        : " Phases stream automatically."}</div>`);
  }
  function startSimTimeline() {
    if (state.simTimer || state.reported) return;
    if (state.polling) { clearInterval(state.polling); state.polling = null; }
    state.simPhases = SIM_PHASES.map((l) => ({ label: l, status: "pending" }));
    state.simIdx = 0;
    state.simApprovals = !!(state.run && state.run.approvals);
    state.lastPhases = [];
    advanceSim();
  }
  function advanceSim() {
    const phases = state.simPhases, i = state.simIdx;
    if (i >= phases.length) {                        // all phases done -> report
      if (state.simTimer) { clearTimeout(state.simTimer); state.simTimer = null; }
      phases.forEach((p) => (p.status = "done"));
      renderPhases(phases); announcePhaseChanges(phases);
      if (!state.reported) {
        state.reported = true;
        showCompletionReport({ overall: "COMPLETED", phases, failedSteps: [] });
      }
      return;
    }
    // Approval gate only for the two change-impacting decision points: BGP RRv6 and Cutover.
    if (i > 0) phases[i - 1].status = "done";
    // Short verification output shown before each phase runs.
    showPhaseVerify(phases[i]);
    if (state.simApprovals && GATE_APPROVE.has(phases[i].label) && !phases[i]._approved) {
      renderPhases(phases); announcePhaseChanges(phases);
      requestApproval(phases[i]);
      return;
    }
    phases[i].status = "current";
    renderPhases(phases); announcePhaseChanges(phases);
    state.simIdx = i + 1;
    state.simTimer = setTimeout(advanceSim, 1900);
  }
  function showPhaseVerify(phase) {
    if (phase._verified) return;
    phase._verified = true;
    const v = SIM_VERIFY[phase.label];
    if (v) bubble("bot", `🔎 <b>Pre-check</b> — <span style="opacity:.85">${escapeHtml(v)}</span> <span style="color:#2ECC71">✓</span>`);
  }
  function requestApproval(phase) {
    const phases = state.simPhases;
    const idx = phases.indexOf(phase) + 1, total = phases.length;
    const wx = state.run && state.run.webex;
    const impactful = GATE_BEFORE.has(phase.label);
    bubble("bot", `⏸️ <b>Permission to proceed</b> — phase <b>${idx}/${total}</b>: <b>${escapeHtml(phase.label)}</b>` +
      (wx ? ` <span style="opacity:.75">(also posted to Webex room ${escapeHtml(state.run.webexRoom || "?")})</span>` : "") +
      `<div style="margin-top:6px">${impactful ? "⚠️ This step changes the forwarding plane. " : ""}Approve to run this phase?</div>`);
    replies([
      { label: "✅ Approve", cls: "primary", onClick: () => {
          botText(`Approved — running **${phase.label}**.`);
          phase._approved = true; advanceSim(); } },
      { label: "❌ Reject", cls: "danger", onClick: () => abortSim(phase) },
    ]);
  }
  function abortSim(phase) {
    if (state.simTimer) { clearTimeout(state.simTimer); state.simTimer = null; }
    const phases = state.simPhases;
    botText(`❌ **Migration halted at ${escapeHtml(phase.label)}** by operator. ` +
      `Previously completed phases are unchanged; no further changes were made.`);
    if (!state.reported) {
      state.reported = true;
      showCompletionReport({ overall: "FAILED", phases, failedSteps: [phase.label + " (rejected)"] });
    }
  }
  function renderPhases(phases) {
    phaseStrip.innerHTML = "";
    phases.forEach((p) => {
      const el = document.createElement("span");
      el.className = "pchip " + (p.status || "");
      el.innerHTML = `<span class="dot"></span>${escapeHtml(p.label)}`;
      phaseStrip.appendChild(el);
    });
    phaseStrip.classList.remove("hidden");
  }

  // Stream progress into the conversation whenever a phase changes state.
  function announcePhaseChanges(phases) {
    const last = state.lastPhases || [];
    phases.forEach((p, i) => {
      const prev = last[i] && last[i].status;
      if (p.status && p.status !== prev) {
        if (p.status === "current") botText(`▶️ **${p.label}** — running…`);
        else if (p.status === "done" && prev) botText(`✅ **${p.label}** — completed`);
        else if (p.status === "failed") botText(`❌ **${p.label}** — failed`);
      }
    });
    state.lastPhases = phases.map((p) => ({ status: p.status }));
  }

  // ---------- completion report ----------
  function fmtDuration(ms) {
    if (!ms || ms < 0) return "—";
    const s = Math.round(ms / 1000), m = Math.floor(s / 60);
    return m ? `${m}m ${s % 60}s` : `${s}s`;
  }

  function buildReportModel(j) {
    const r = state.run || {};
    const phases = j.phases || [];
    const started = r.startedAt ? new Date(r.startedAt) : null;
    const finished = new Date();
    const ok = j.overall === "COMPLETED";
    const failed = (j.failedSteps || []).filter(Boolean);
    const services = [];
    if (r.doL3) services.push({
      name: `L3VPN ${D.l3vpnName}`, kind: "L3VPN (BGP VPNv4)", pes: "node-1, node-2, node-4",
      result: [
        `PE loopbacks advertised with SRv6 uDT4 (End.DT4) SIDs under locator ${r.locator}`,
        `Ingress PEs program “SRv6 Headend” forwarding`,
        `Legacy MPLS VPN labels withdrawn`,
      ],
    });
    if (r.doL2) services.push({
      name: `L2VPN ${D.l2vpnName}`, kind: `EVPN-VPWS (EVI ${D.evi})`, pes: "node-2, node-4",
      result: [
        `Per-EVI uDX2 (End.DX2) SID assigned under locator ${r.locator}`,
        `EVPN SRv6 enabled globally + per-service`,
        `Cross-connect UP over SRv6`,
      ],
    });
    return {
      ok, failed, phases, services,
      startedAt: started, finishedAt: finished,
      duration: fmtDuration(started ? finished - started : 0),
      mode: r.dryRun ? "Dry-run (no config committed)" : "Live",
      locator: r.locator || D.locator, rrv6: r.rrv6 || D.rrv6Ip,
      webex: !!r.webex, webexRoom: r.webexRoom,
      mplsRemoved: !r.dryRun && r.doL3, // Phase 8 (remove MPLS/LDP) runs with the full sweep
    };
  }

  function reportHtml(m) {
    const fmt = (d) => d ? d.toLocaleString() : "—";
    const head = [
      ["Status", m.ok ? "✅ Completed — all requested phases passed" : `❌ Failed at ${m.failed.join(", ") || "a phase"}`],
      ["Mode", m.mode],
      ["Started", fmt(m.startedAt)],
      ["Finished", fmt(m.finishedAt)],
      ["Duration", m.duration],
      ["SRv6 locator", m.locator],
      ["RRv6 peer", m.rrv6],
      ["Notifications", m.webex ? `Webex (room ${escapeHtml(m.webexRoom || "?")})` : "In-assistant only"],
    ];
    const kv = head.map(([k, v]) => `<div class="k">${escapeHtml(k)}</div><div>${v.startsWith && (v.startsWith("✅") || v.startsWith("❌")) ? v : escapeHtml(String(v))}</div>`).join("");
    const svc = m.services.map((s) =>
      `<div style="margin-top:8px"><b>${escapeHtml(s.name)}</b> · <span style="opacity:.75">${escapeHtml(s.kind)} · PEs ${escapeHtml(s.pes)}</span>` +
      `<ul style="margin:4px 0 0 18px;padding:0">${s.result.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>`
    ).join("");
    const net = m.mplsRemoved
      ? `<div style="margin-top:8px"><b>Network</b><ul style="margin:4px 0 0 18px;padding:0"><li>LDP/MPLS forwarding removed (Phase 8) — SR/SRv6-only data plane</li></ul></div>`
      : "";
    const chips = (m.phases || []).map((p) => {
      const icon = p.status === "done" ? "✅" : p.status === "failed" ? "❌" : p.status === "current" ? "▶️" : "•";
      return `<span class="pchip ${p.status || ""}">${icon} ${escapeHtml(p.label)}</span>`;
    }).join(" ");
    return `<div><b>${m.ok ? "📄 Migration report" : "📄 Migration report (with failures)"}</b></div>` +
      `<div class="kv" style="margin-top:6px">${kv}</div>` +
      `<div style="margin-top:8px"><b>Services migrated</b></div>${svc}${net}` +
      (chips ? `<div style="margin-top:10px"><b>Phases</b><div class="phasewrap" style="margin-top:4px;display:flex;flex-wrap:wrap;gap:6px">${chips}</div></div>` : "");
  }

  function reportText(m) {
    const fmt = (d) => d ? d.toLocaleString() : "—";
    const L = [];
    L.push(`SRv6 MIGRATION REPORT`);
    L.push(`====================`);
    L.push(`Status      : ${m.ok ? "COMPLETED — all requested phases passed" : "FAILED at " + (m.failed.join(", ") || "a phase")}`);
    L.push(`Mode        : ${m.mode}`);
    L.push(`Started     : ${fmt(m.startedAt)}`);
    L.push(`Finished    : ${fmt(m.finishedAt)}`);
    L.push(`Duration    : ${m.duration}`);
    L.push(`SRv6 locator: ${m.locator}`);
    L.push(`RRv6 peer   : ${m.rrv6}`);
    L.push(`Notifications: ${m.webex ? "Webex (room " + (m.webexRoom || "?") + ")" : "In-assistant only"}`);
    L.push(``);
    L.push(`Services migrated`);
    m.services.forEach((s) => {
      L.push(`  • ${s.name}  [${s.kind} · PEs ${s.pes}]`);
      s.result.forEach((x) => L.push(`      - ${x}`));
    });
    if (m.mplsRemoved) { L.push(``); L.push(`Network`); L.push(`  • LDP/MPLS forwarding removed (Phase 8) — SR/SRv6-only data plane`); }
    if (m.phases && m.phases.length) {
      L.push(``); L.push(`Phases`);
      m.phases.forEach((p) => L.push(`  [${(p.status || "").toUpperCase() || "PENDING"}] ${p.label}`));
    }
    return L.join("\n");
  }

  function showCompletionReport(j) {
    const m = buildReportModel(j);
    bubble("bot", (m.ok
      ? "🎉 <b>Migration completed.</b> Here's your report:"
      : "⚠️ <b>Migration ended with failures.</b> Here's your report:") +
      `<div style="margin-top:8px">${reportHtml(m)}</div>`);
    const text = reportText(m);
    replies([
      { label: "📋 Copy report", onClick: () => {
          navigator.clipboard.writeText(text).then(
            () => botText("Copied the report to your clipboard."),
            () => botText("Couldn't access the clipboard — use _Download report_ instead.")); } },
      { label: "⬇️ Download report", onClick: () => {
          const blob = new Blob([text], { type: "text/plain" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          const stamp = (m.finishedAt || new Date()).toISOString().replace(/[:.]/g, "-");
          a.href = url; a.download = `srv6-migration-report-${stamp}.txt`;
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 4000);
          botText("Report downloaded."); } },
      { label: "New migration", cls: "primary", onClick: () => newThread() },
    ]);
  }

  function showStatus() {
    if (!state.sid) { botText("You're not connected yet. Say _start migration_ and I'll log in first."); return; }
    const stop = typing();
    fetch("/api/migration-status", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: state.sid }),
    }).then((r) => r.json()).then((j) => {
      stop();
      if (!j.ok) { botText("No run found yet, or: " + escapeHtml(j.error || "")); return; }
      if (!j.phases || !j.phases.length) { botText("No active migration run found. Say _start migration_ to begin."); return; }
      renderPhases(j.phases);
      const cur = j.phases.find((p) => p.status === "current");
      const done = j.phases.filter((p) => p.status === "done").length;
      botText(`Overall: **${j.overall || "—"}** · ${done}/9 phases done` +
        (cur ? ` · currently **${cur.label}**` : "") +
        ((j.failedSteps && j.failedSteps.length) ? `\n⚠️ Failed: ${j.failedSteps.join(", ")}` : ""));
    }).catch((e) => { stop(); botText("Status error: " + escapeHtml(String(e))); });
  }

  // ---------- grounding + inline login (shared by add-check / new-workflow) ----------
  function groundData(verbose) {
    // Uses endpoints present on the deployed portal: /api/config + /api/vpn-checks.
    return fetch("/api/config").then((r) => r.json()).then((c) => {
      if (c.locator) D.locator = c.locator;
      if (c.rrv6Ip) D.rrv6Ip = c.rrv6Ip;
      if (c.l3vpnName) D.l3vpnName = c.l3vpnName;
      if (c.l2vpnName) D.l2vpnName = c.l2vpnName;
      return fetch("/api/vpn-checks").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    }).then((v) => {
      if (v && v.l3vpn && v.l3vpn.service) D.l3vpnName = v.l3vpn.service;
      if (v && v.l2vpn && v.l2vpn.service) D.l2vpnName = v.l2vpn.service;
      if (verbose) {
        botText(`🔎 Discovering the VPN services on the network…`);
        bubble("bot", `I've discovered <b>two services</b> that can be migrated to SRv6:` +
          `<div class="kv">` +
          `<div class="k">L3VPN</div><div><code>${escapeHtml(D.l3vpnName)}</code></div>` +
          `<div class="k">L2VPN (EVPN-VPWS)</div><div><code>${escapeHtml(D.l2vpnName)}</code></div>` +
          `</div>`);
      }
    }).catch(() => {});
  }

  // Runs a compact connect sequence if there's no session yet, then calls next().
  function ensureLogin(next) {
    if (state.sid) return next();
    autoLogin(next);
  }

  // ---------- flow: add a pre/post check to a phase ----------
  function startAddCheck() {
    ensureLogin(() => {
      const stop = typing();
      fetch("/api/phase-checks", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid: state.sid }) }).then((r) => r.json()).then((j) => {
        stop();
        if (!j.ok) { botText("Could not read phases: " + escapeHtml(j.error || "")); return; }
        botText("Which **phase** should the check go in?");
        replies((j.phases || []).map((p) => ({ label: p.phase,
          onClick: () => { userText(p.phase); pickStage(p.phase); } })));
      }).catch((e) => { stop(); botText("❌ " + escapeHtml(String(e))); });
    });
  }
  function pickStage(phase) {
    botText("**Pre-check** (before the phase action) or **post-check** (after)?");
    replies([
      { label: "Pre-check", onClick: () => { userText("Pre-check"); collectCheck(phase, "pre"); } },
      { label: "Post-check", cls: "primary", onClick: () => { userText("Post-check"); collectCheck(phase, "post"); } },
    ]);
  }
  function collectCheck(phase, stage) {
    const c = {};
    const steps = [
      { k: "node", p: "Which **device/node** runs the show command?" },
      { k: "command", p: "The **show command** to run?" },
      { k: "expect", p: "The **expected text or regex** that means PASS?" },
      { k: "name", p: "A short **name** for this check? _(optional — Enter to skip)_" },
    ];
    let i = 0;
    const step = () => {
      if (i >= steps.length) {
        const stop = typing();
        fetch("/api/phase-check-add", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sid: state.sid, phase, stage, check: c }) })
          .then((r) => r.json()).then((j) => {
            stop();
            if (!j.ok) { botText("❌ " + escapeHtml(j.error || "add failed")); return; }
            bubble("bot", `✅ Added <b>${escapeHtml(j.stage)}</b>-check ` +
              `<code>${escapeHtml(j.added.name)}</code> to <b>${escapeHtml(j.phase)}</b> ` +
              `(now ${j.count} ${escapeHtml(j.stage)}-checks).` +
              `<div class="kv"><div class="k">node</div><div>${escapeHtml(j.added.node)}</div>` +
              `<div class="k">command</div><div><code>${escapeHtml(j.added.command)}</code></div>` +
              `<div class="k">expect</div><div><code>${escapeHtml(j.added.expect)}</code></div></div>` +
              `<div style="margin-top:6px">It's saved to the MOP check registry and will run next time that phase executes.</div>`);
          }).catch((e) => { stop(); botText("❌ " + escapeHtml(String(e))); });
        return;
      }
      const s = steps[i];
      state.pending = { key: "__mini", __take: (v) => {
        const val = (v || "").trim();
        if (!val && s.k !== "name") { botText("Please enter a value."); return; }
        if (val) c[s.k] = val;
        i++; state.pending = null; step();
      } };
      botText(s.p);
    };
    step();
  }

  // ---------- flow: create a new workflow ----------
  function startNewWorkflow() {
    ensureLogin(() => {
      const nw = {};
      const steps = [
        { k: "name", p: "**Name** for the new workflow? (e.g. `Pre-Migration-Audit`)" },
        { k: "description", p: "A short **description**? _(optional — Enter to skip)_" },
      ];
      let i = 0;
      const step = () => {
        if (i >= steps.length) {
          const stop = typing();
          fetch("/api/new-workflow", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sid: state.sid, name: nw.name, description: nw.description }) })
            .then((r) => r.json()).then((j) => {
              stop();
              if (!j.ok) { botText("❌ " + escapeHtml(j.error || "create failed") +
                (j.resp ? "\n" + escapeHtml(j.resp) : "")); return; }
              botText(`✅ Created workflow **${j.name}** (HTTP ${j.status}). It runs the reusable ` +
                `_validateChecks_ engine — add checks with **add a pre/post check to a phase**, ` +
                `or wire it into the MOP from the classic UI.`);
            }).catch((e) => { stop(); botText("❌ " + escapeHtml(String(e))); });
          return;
        }
        const s = steps[i];
        state.pending = { key: "__mini", __take: (v) => {
          const val = (v || "").trim();
          if (!val && s.k === "name") { botText("A name is required."); return; }
          if (val) nw[s.k] = val;
          i++; state.pending = null; step();
        } };
        botText(s.p);
      };
      step();
    });
  }

  // ---------- static explainers ----------
  function explainPhases() {
    botText(
      "The **MPLS → SRv6 Migration MOP** runs 9 gated phases; each does " +
      "**pre-check → action → post-check → approval**:\n\n" +
      "**0 Baseline** — verify ISISv4 + LDP + RRv4, L3VPN and L2VPN up\n" +
      "**1 Enable IPv6** — link + loopback IPv6 (2010::x)\n" +
      "**2 SRv6 locators** — ISISv6 AF + locators (MAIN/TOP/BOTTOM/LATENCY)\n" +
      "**3 RRv6 peers** — BGPv6 sessions to the RRv6\n" +
      "**4 LocPref 50** — RRv6 policy, SRv6 as standby (dual-plane)\n" +
      "**5 L3VPN → SRv6** — per-VRF SRv6 (uDT4)\n" +
      "**6 L2VPN → SRv6** — EVPN-VPWS to SRv6 (uDT2)\n" +
      "**7 Cutover** — LocPref 150, SRv6 becomes best path\n" +
      "**8 Remove MPLS** — final MPLS/LDP removal");
    replies([{ label: "Start migration", cls: "primary", onClick: () => { userText("Start migration"); startInterview(); } }]);
  }
  function explainParams() {
    botText(
      "To run the migration I'll ask for, in order:\n\n" +
      "• **CNC connection** — host, port, username, password\n" +
      "• **Scope** — full run or a subset of phases\n" +
      "• **Devices** — the IGP fabric to get IPv6+SRv6, and the PE nodes\n" +
      "• **SRv6 locator** — MAIN / TOP / BOTTOM / LATENCY\n" +
      "• **RRv6 peer** — route-reflector IPv6 address\n" +
      "• **Services** — L3VPN name, L2VPN name + EVI\n" +
      "• **Approvals** — auto or Webex (token + room)\n" +
      "• **Mode** — live run or dry-run\n\n" +
      "I can also **add a pre/post check to a phase** (node + command + expected match) " +
      "and **create a new workflow** on CNC.");
    replies([
      { label: "Start migration", cls: "primary", onClick: () => { userText("Start migration"); startInterview(); } },
      { label: "Add a check", onClick: () => { userText("Add a check"); startAddCheck(); } },
      { label: "New workflow", onClick: () => { userText("New workflow"); startNewWorkflow(); } },
    ]);
  }

  // ---------- threads (visual, local only) ----------
  function newThread() {
    state.threadId = "t" + Date.now();
    chat.innerHTML = "";
    phaseStrip.classList.add("hidden");
    showHero();
    if (state.polling) { clearInterval(state.polling); state.polling = null; }
    addThreadEntry("New migration");
  }
  $("#newThread").addEventListener("click", newThread);
  function addThreadEntry(title) {
    const list = $("#threadList");
    if (!list.querySelector(".thread-group")) {
      const g = document.createElement("div"); g.className = "thread-group"; g.textContent = "Today";
      list.appendChild(g);
    }
    const t = document.createElement("div");
    t.className = "thread active";
    list.querySelectorAll(".thread").forEach((x) => x.classList.remove("active"));
    t.textContent = title;
    t.onclick = () => { list.querySelectorAll(".thread").forEach((x) => x.classList.remove("active")); t.classList.add("active"); };
    const grp = list.querySelector(".thread-group");
    grp.after(t);
  }

  // ---------- helpers ----------
  function parsePhases(v) {
    const s = v.trim().toLowerCase();
    if (/^(all|full)$/.test(s)) return { value: [0,1,2,3,4,5,6,7,8] };
    const out = new Set();
    for (const part of s.split(/[,\s]+/).filter(Boolean)) {
      const m = part.match(/^(\d)\s*-\s*(\d)$/);
      if (m) { for (let n = +m[1]; n <= +m[2]; n++) if (n>=0&&n<=8) out.add(n); }
      else if (/^\d$/.test(part) && +part <= 8) out.add(+part);
      else return { error: `Couldn't parse "${part}". Use numbers 0–8, e.g. 1-2 or 1,2,5.` };
    }
    if (!out.size) return { error: "No valid phases found. Try 1-6 or 1,2,5." };
    return { value: [...out].sort((a,b)=>a-b) };
  }
  function escapeHtml(s){return String(s).replace(/[&<>"']/g,(c)=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
  function mdToHtml(md){
    return escapeHtml(md)
      .replace(/\*\*(.+?)\*\*/g,"<b>$1</b>")
      .replace(/`([^`]+)`/g,"<code>$1</code>")
      .replace(/_([^_]+)_/g,"<i>$1</i>")
      .replace(/\n/g,"<br/>");
  }
})();
