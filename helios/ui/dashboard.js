/* ============================================================================
   Helios dashboard (docs/HELIOS_DASHBOARD_UI.md). Loaded after app.js and reuses
   its helpers (g, jfetch, esc, fmtTok, sendMessage, openDrawer, …).

   Data honesty: every value here comes from /dashboard/* (helios/dashboard.py) or
   live SSE events. Nothing is hard-coded or estimated; a missing source renders
   as an empty state or "not exposed", never as a made-up number.
   ============================================================================ */
(() => {
  "use strict";
  const e = v => esc(String(v ?? ""));
  const $ = sel => document.querySelector(sel);

  let summary = null, limitsData = null, biz = [], selId = null;
  const robot = { voice: null, followup: false, busy: false, conn: true, wake: null, wakeReason: "", level: 0, levelAt: 0 };
  let lastModel = "", refreshTimer = 0;

  // ------------------------------------------------------------------ helpers
  async function getJSON(path) {
    const r = await jfetch(path);
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  async function postJSON(path, body) {
    const r = await jfetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let data = {}; try { data = await r.json(); } catch {}
    if (!r.ok) throw new Error(data.error || data.message || "HTTP " + r.status);
    return data;
  }
  function openUrl(u) {
    if (!/^https?:\/\//i.test(u || "")) return;
    jfetch("/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: u }) })
      .catch(() => window.open(u, "_blank"));
  }
  function toast(text, cls) {
    const t = document.createElement("div");
    t.className = "toast " + (cls || ""); t.textContent = text;
    g("toasts").appendChild(t);
    setTimeout(() => t.remove(), 6500);
  }
  function parseIso(iso) { const d = iso ? new Date(String(iso).replace(" ", "T")) : null; return d && !isNaN(d) ? d : null; }
  function ago(iso) {
    const d = parseIso(iso); if (!d) return "";
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 0) return "in " + until(d);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    if (s < 86400 * 30) return Math.round(s / 86400) + "d ago";
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }
  function until(d) { const m = Math.round((d - Date.now()) / 60000); return m < 60 ? m + "m" : m < 1440 ? Math.round(m / 60) + "h" : Math.round(m / 1440) + "d"; }
  function when(iso) {
    const d = parseIso(iso); if (!d) return "";
    const t = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    const today = new Date(); const tm = new Date(today.getTime() + 86400000);
    if (d.toDateString() === today.toDateString()) return t;
    if (d.toDateString() === tm.toDateString()) return "Tmrw " + t;
    return d.toLocaleDateString(undefined, { weekday: "short" }) + " " + t;
  }
  function hm(iso) { const d = parseIso(iso); return d ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "—"; }
  const narrow = () => window.matchMedia("(max-width: 1279px)").matches;
  const tiny = () => window.matchMedia("(max-width: 1023px)").matches;
  const STATUS_LABEL = { DRAFTED: "To approve", QUALIFIED: "Qualified", APPROVED: "Approved", CONTACTED: "Contacted",
    REPLIED: "Replied", INTERESTED: "Interested", NOT_INTERESTED: "Not interested", CLOSED: "Closed",
    REJECTED: "Rejected", ARCHIVED: "Archived", NEW: "New", RESEARCHED: "Researched" };
  const CONTACTED = ["CONTACTED", "REPLIED", "INTERESTED", "NOT_INTERESTED", "CLOSED"];

  // ------------------------------------------------------------------ views + drawers
  function showView(v) {
    document.body.dataset.view = v;
    g("vw_home").classList.toggle("on", v === "home"); g("vw_home").setAttribute("aria-selected", v === "home");
    g("vw_chat").classList.toggle("on", v === "chat"); g("vw_chat").setAttribute("aria-selected", v === "chat");
    if (v === "chat") setTimeout(() => { chat.scrollTop = chat.scrollHeight; }, 0);
  }
  function updCount() {
    const n = chat.querySelectorAll(".msg.user").length;
    const c = g("chatcount"); c.textContent = n; c.classList.toggle("hidden", !n);
  }
  function setNav(id) { document.querySelectorAll(".navi").forEach(b => b.classList.toggle("on", b.dataset.nav === id)); }
  function closeOverlays() {
    document.body.classList.remove("side-open", "rp-open");
    g("sidescrim").classList.add("hidden");
  }
  function openRp() { if (narrow()) { document.body.classList.add("rp-open"); g("sidescrim").classList.remove("hidden"); } }
  g("vw_home").addEventListener("click", () => { showView("home"); setNav("home"); });
  g("vw_chat").addEventListener("click", () => showView("chat"));
  g("sidetoggle").addEventListener("click", () => { document.body.classList.add("side-open"); g("sidescrim").classList.remove("hidden"); });
  g("rptoggle").addEventListener("click", () => { document.body.classList.add("rp-open"); g("sidescrim").classList.remove("hidden"); });
  g("rpclose").addEventListener("click", closeOverlays);
  g("sidescrim").addEventListener("click", closeOverlays);
  document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeOverlays(); });
  g("cmdmic").addEventListener("click", () => micbtn.click());
  // When the drawer closes, the nav highlight returns to the workspace.
  new MutationObserver(() => { if (!drawer.classList.contains("open")) setNav("home"); })
    .observe(drawer, { attributes: true, attributeFilter: ["class"] });

  function infoPane(title, html) {
    openDrawer("infoview", title);
    g("infobody").innerHTML = `<div class="infopane">${html}</div>`;
    return g("infobody");
  }

  // ------------------------------------------------------------------ command bar status
  function setCmdStatus(s, text) { const el = g("cmdstatus"); el.dataset.s = s; el.textContent = text; }
  let errTimer = 0;

  // ------------------------------------------------------------------ robot (real state)
  function wakeName(w) {
    if (!w) return "";
    const base = String(w).split(/[\\/]/).pop().replace(/\.onnx$/i, "");
    return base.replace(/_/g, " ");
  }
  function robotView() {
    const v = robot.voice, eng = summary && summary.engine;
    if (!robot.conn) return ["error", "Helios server unreachable"];
    if (v === "listening") return robot.followup ? ["followup", "Waiting for follow-up..."] : ["listening", "Listening..."];
    if (v === "dictation") return ["listening", "Dictation — speak now"];
    if (v === "speaking") return ["speaking", "Speaking..."];
    if (v === "permission") return ["processing", "Waiting for your approval…"];
    if (v === "thinking" || robot.busy) return ["processing", "Processing..."];
    if (eng && eng.engine === "antigravity" && eng.ok === false) return ["error", "Antigravity unavailable"];
    if (v === "off") return ["muted", "Microphone muted"];
    if (v === "disabled") return ["muted", "No microphone found"];
    return ["idle", "Online & Ready"];
  }
  function renderRobot() {
    const card = g("robotcard");
    const [st, text] = robotView();
    card.dataset.state = st;
    g("rstatus").textContent = text;
    const eng = summary && summary.engine;
    card.title = eng && eng.engine === "antigravity" && eng.ok === false ? "Antigravity check: " + (eng.detail || "failed") : "";
    const s = summary && summary.voice, v = robot.voice;
    let mic = "Mic: checking…";
    if (s) {
      if (!s.enabled) mic = "Mic: voice is off in settings";
      else if (!s.daemon && !v) mic = "Mic: voice daemon not running";
      else if (v === "off") mic = "Mic: muted — the mic button or a double-clap turns it on";
      else if (v === "disabled") mic = "Mic: no microphone found";
      else if (["listening", "speaking", "dictation", "permission"].includes(v)) mic = "Mic: live";
      else mic = "Mic: on";
    }
    g("rmic").textContent = mic; g("rmic").title = mic;
    const name = wakeName(robot.wake != null ? robot.wake : s && s.wake_word);
    const ready = robot.wake != null ? !robot.wakeReason : (s ? s.wake_ready : null);
    const reason = robot.wake != null ? robot.wakeReason : (s && s.wake_reason) || "";
    let wake = "Wake word: checking…";
    if (s) {
      if (!name) wake = "Wake word: none set (double-clap still works)";
      else if (ready === true) wake = `Wake word: “${name}” · ready`;
      else if (ready === false) wake = `Wake word unavailable — ${reason || "not loaded"}`;
      else wake = `Wake word: “${name}” · status shown once voice starts`;
    }
    g("rwake").textContent = wake; g("rwake").title = wake;
    // Speaking mouth follows the real TTS level when the daemon sends one.
    const live = st === "speaking" && Date.now() - robot.levelAt < 600;
    card.classList.toggle("lvl", live);
    card.style.setProperty("--lvl", live ? robot.level.toFixed(3) : "0");
  }

  // ------------------------------------------------------------------ hooks from app.js
  window.Dash = {
    onBubble() { showView("chat"); setTimeout(updCount, 0); },   // after app.js appends the bubble
    onClear() { showView("home"); updCount(); },
    onBusy(on, label) {
      robot.busy = on;
      if (on) { clearTimeout(errTimer); setCmdStatus("busy", label || "Working…"); }
      else if (g("cmdstatus").dataset.s !== "error") setCmdStatus("ready", "Ready");
      if (!on) scheduleRefresh(1500);   // a finished turn may have changed business data
      renderRobot();
    },
    onMic(on, live) { const b = g("cmdmic"); b.classList.toggle("on", !!on); b.classList.toggle("live", !!live); b.title = micbtn.title; },
    onVoice(d) {
      const st = d.state || "idle";
      if ("wake" in d) { robot.wake = d.wake; robot.wakeReason = d.wake_reason || ""; }
      // Level-only frames (12 Hz) carry no followup flag: keep the last one while still listening.
      if (st !== "listening") robot.followup = false;
      else if ("followup" in d) robot.followup = !!d.followup;
      robot.voice = st;
      if (typeof d.level === "number") { robot.level = d.level; robot.levelAt = Date.now(); }
      renderRobot();
    },
    onConn(ok) {
      robot.conn = ok;
      if (!ok) setCmdStatus("offline", "Reconnecting…");
      else if (!robot.busy) setCmdStatus("ready", "Ready");
      renderRobot();
    },
    onModel(m) { lastModel = m; renderLimits(); },
    onError(msg) {
      setCmdStatus("error", "Error — see conversation");
      g("cmdstatus").title = String(msg || "").slice(0, 300);
      clearTimeout(errTimer);
      errTimer = setTimeout(() => { if (!robot.busy) { setCmdStatus("ready", "Ready"); g("cmdstatus").title = "Helios status"; } }, 12000);
    },
    onEvent(d) {
      if (d && d.event === "action_done") { toast(d.text || "Done", /didn't run/.test(d.text || "") ? "err" : "ok"); scheduleRefresh(300); }
    },
  };

  // ------------------------------------------------------------------ summary: nav, system, hero, metrics
  async function loadSummary() {
    try { summary = await getJSON("/dashboard/summary"); }
    catch (err) { g("sysbody").innerHTML = `<div class="empty-note">Couldn't load status (${e(err.message)})</div>`; return; }
    if (robot.voice == null && summary.voice && summary.voice.state) robot.voice = summary.voice.state;
    if (summary.voice && summary.voice.state === "listening" && robot.voice === "listening") robot.followup = !!summary.voice.followup;
    renderNav(); renderSystem(); renderHero(); renderMetrics(); renderRobot();
  }
  function renderNav() {
    const mods = Object.fromEntries((summary.modules || []).map(m => [m.id, m]));
    document.querySelectorAll(".navi").forEach(b => {
      const m = mods[b.dataset.nav], ind = b.querySelector(".ind");
      if (m) { ind.dataset.s = m.state; b.title = `${m.label}: ${m.detail}`; }
      else { ind.removeAttribute("data-s"); b.title = b.textContent.trim(); }
    });
  }
  function meter(pct, hot) { return `<div class="meter${hot ? " hot" : ""}"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></div>`; }
  function renderSystem() {
    const s = summary.system || {}, v = summary.voice || {};
    const rows = [];
    if (s.cpu != null) rows.push(`<div class="kv"><span>CPU</span><b>${Math.round(s.cpu)}%</b></div>${meter(s.cpu, s.cpu > 85)}`);
    else rows.push(`<div class="kv"><span>CPU</span><b class="na">measuring…</b></div>`);
    if (s.ram) rows.push(`<div class="kv"><span>Memory</span><b>${s.ram.used_gb} / ${s.ram.total_gb} GB</b></div>${meter(s.ram.pct, s.ram.pct > 88)}`);
    (s.disks || []).forEach(d => rows.push(`<div class="kv"><span>Disk ${e(d.drive)}</span><b class="${d.free_gb < 5 ? "bad" : ""}">${d.free_gb} GB free</b></div>`));
    if (s.battery) rows.push(`<div class="kv"><span>Battery</span><b>${s.battery.pct}%${s.battery.plugged ? " · charging" : ""}</b></div>`);
    rows.push(`<div class="kv"><span>Voice daemon</span><b class="${v.daemon ? "ok" : (v.enabled ? "bad" : "na")}">${v.daemon ? "running" : (v.enabled ? "not running" : "off")}</b></div>`);
    rows.push(`<div class="kv"><span>Helios</span><b>v${e(s.version || "?")} · ${robot.conn ? "connected" : "reconnecting"}</b></div>`);
    g("sysbody").innerHTML = rows.join("");
  }
  function renderHero() {
    const d = new Date();
    g("herodate").textContent = d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    const m = summary.metrics || {}, bits = [];
    if (m.to_approve) bits.push(`${m.to_approve} outreach draft${m.to_approve === 1 ? " is" : "s are"} waiting for your approval`);
    const proj = (summary.modules || []).find(x => x.id === "projects");
    if (proj && proj.state === "warn") bits.push(`projects: ${proj.detail}`);
    const night = (summary.modules || []).find(x => x.id === "night");
    if (night && night.state !== "off") bits.push(`Night Mode: ${night.detail}`);
    if (m.research_week) bits.push(`${m.research_week} new research finding${m.research_week === 1 ? "" : "s"} this week`);
    g("herosub").textContent = bits.length ? bits.join(" · ") + "."
      : "All quiet. Ask me anything above, or say the wake word.";
  }
  const MICONS = {
    biz: '<svg viewBox="0 0 24 24"><path d="M4 10v10h16V10M3 10l2-6h14l2 6"/><path d="M10 20v-5h4v5"/></svg>',
    qual: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.3 2.3 4.7-5"/></svg>',
    appr: '<svg viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/></svg>',
    cont: '<svg viewBox="0 0 24 24"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>',
    res: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg>',
    tok: '<svg viewBox="0 0 24 24"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg>',
  };
  function renderMetrics() {
    const m = summary.metrics || {};
    const card = (ic, color, label, val, sub, tip) =>
      `<div class="metric" style="--mc:${color}" title="${e(tip)}"><div class="ml">${MICONS[ic]}${e(label)}</div>` +
      `<div class="mv">${e(val)}</div><div class="ms">${e(sub)}</div></div>`;
    g("metrics").innerHTML = [
      card("biz", "var(--neon)", "Businesses found", m.businesses ?? 0, m.businesses_week ? `+${m.businesses_week} this week` : "from OpenStreetMap",
           "Local businesses in the prospect database (rejected/archived excluded)"),
      card("qual", "var(--green)", "Qualified", m.qualified ?? 0, "clear gap + evidence", "Qualified or drafted prospects"),
      card("appr", "var(--orange)", "To approve", m.to_approve ?? 0, "never sent automatically", "Drafted outreach waiting for your approval"),
      card("cont", "var(--purple)", "Contacted", m.contacted ?? 0, `${m.approved ?? 0} approved, not sent yet`,
           "Marked contacted only with evidence of your send"),
      card("res", "#5ee7c4", "Research · 7 days", m.research_week ?? 0, `${m.leads_week ?? 0} freelance leads`, "Findings in your research library"),
      card("tok", "var(--amber)", "AI tokens today", fmtTok(m.tokens_today || 0), "measured by Helios", "From data/usage.jsonl — not your provider quota"),
    ].join("");
  }

  // ------------------------------------------------------------------ model limits
  const NA = (why) => `<b class="na">Not exposed</b><i class="tip" title="${e(why)}">i</i>`;
  async function loadLimits(force) {
    const btn = g("limrefresh"); if (force) btn.classList.add("spin");
    try { limitsData = await getJSON("/dashboard/limits" + (force ? "?refresh=1" : "")); }
    catch (err) { g("limbody").innerHTML = `<div class="empty-note">Couldn't load (${e(err.message)})</div>`; btn.classList.remove("spin"); return; }
    renderLimits();
    // The engine check runs in the background — pick its result up a little later.
    if (limitsData.health && limitsData.health.checking) setTimeout(() => loadLimits(false), 9000);
    else btn.classList.remove("spin");
    if (force) loadSummary();
  }
  function renderLimits() {
    const L = limitsData; if (!L) return;
    const h = L.health || {}, p = L.provider || {}, loc = L.local || {}, why = p.why || "Not exposed by the current interface.";
    const chatModel = (L.models || [])[0];
    const health = h.ok === true ? `<b class="ok">reachable</b>` : h.ok === false ? `<b class="bad">unavailable</b>`
      : h.checking ? `<b class="na">checking…</b>` : `<b class="na">${e(h.detail || "not checked")}</b>`;
    const pv = (v) => v == null ? NA(why) : `<b>${e(v)}</b>`;
    const top = (loc.by_purpose || []).slice(0, 3).map(x => `${x.purpose} ${fmtTok(x.tokens)}`).join(" · ");
    g("limbody").innerHTML =
      `<div class="kv"><span>Backend</span><b title="${e(L.backend)}">${e(L.backend)}</b></div>` +
      `<div class="kv"><span>Engine</span>${health}</div>` +
      `<div class="kv"><span>Model</span><b title="Configured live-chat model">${e(chatModel ? chatModel.model : "—")}</b></div>` +
      (lastModel ? `<div class="kv"><span>Last used</span><b>${e(lastModel)}</b></div>` : "") +
      `<div class="limgroup"><div class="gl">Provider quota</div>` +
      `<div class="kv"><span>Session</span>${pv(p.session)}</div>` +
      `<div class="kv"><span>Weekly</span>${pv(p.weekly)}</div>` +
      `<div class="kv"><span>Resets</span>${pv(p.reset)}</div>` +
      `<div class="kv"><span title="Model context window">Context</span>${pv(p.context_window)}</div></div>` +
      `<div class="limgroup"><div class="gl" title="${e(loc.source || "")}">Measured by Helios</div>` +
      `<div class="kv"><span>Today</span><b>${fmtTok((loc.today || {}).in + (loc.today || {}).out || 0)} tok · ${(loc.today || {}).calls || 0} calls</b></div>` +
      `<div class="kv"><span>7 days</span><b>${fmtTok((loc.week || {}).in + (loc.week || {}).out || 0)} tok · ${(loc.week || {}).calls || 0} calls</b></div>` +
      (top ? `<div class="kv"><span>Top today</span><b title="${e(top)}">${e(top)}</b></div>` : "") + `</div>` +
      ((L.models || []).some(m => m.offered === false)
        ? `<div class="limfoot" style="color:var(--orange)">A configured model isn't offered by your account — see AI Insights.</div>` : "") +
      `<div class="limfoot">Updated ${hm(L.updated)}${h.at ? ` · engine checked ${hm(h.at)}` : ""}` +
      (h.ok === false ? `<br>${e(h.detail || "")}` : "") + `</div>`;
  }
  g("limrefresh").addEventListener("click", () => loadLimits(true));
  g("limsettings").addEventListener("click", () => g("setbtn").click());

  // ------------------------------------------------------------------ top leads table
  const T = { q: "", status: "active", prio: "", sortK: "", asc: false, page: 0, per: 8 };
  function filtered() {
    const q = T.q.toLowerCase();
    let rows = biz.filter(r => {
      if (T.status === "active" && ["ARCHIVED", "REJECTED"].includes(r.status)) return false;
      if (T.status === "contacted" && !CONTACTED.includes(r.status)) return false;
      if (T.status === "closed" && !["ARCHIVED", "REJECTED"].includes(r.status)) return false;
      if (!["active", "contacted", "closed", "all"].includes(T.status) && r.status !== T.status) return false;
      if (T.prio && r.priority !== T.prio) return false;
      return !q || `${r.name} ${r.area} ${r.category}`.toLowerCase().includes(q);
    });
    if (T.sortK) {
      const k = T.sortK, dir = T.asc ? 1 : -1;
      rows = rows.slice().sort((a, b) => {
        let x = a[k], y = b[k];
        if (k === "priority") { x = x || "Z"; y = y || "Z"; return (x < y ? -1 : x > y ? 1 : 0) * -dir; }
        if (x == null) x = -1; if (y == null) y = -1;
        return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * dir;
      });
    }
    return rows;
  }
  function scoreCell(v) { return v == null ? `<span class="score sx">—</span>` : `<span class="score s${v}">${v}/5</span>`; }
  function renderTable() {
    const rows = filtered(), pages = Math.max(1, Math.ceil(rows.length / T.per));
    T.page = Math.min(T.page, pages - 1);
    const slice = rows.slice(T.page * T.per, T.page * T.per + T.per);
    const body = g("lbody");
    if (!biz.length) {
      body.innerHTML = `<tr><td colspan="8"><div class="empty-note"><b>No businesses yet.</b><br>Turn on <code>[prospecting]</code> with your areas, or use <b>Scan area</b> below. Only real OpenStreetMap listings appear here.</div></td></tr>`;
    } else if (!rows.length) {
      body.innerHTML = `<tr><td colspan="8"><div class="empty-note">Nothing matches these filters.</div></td></tr>`;
    } else {
      body.innerHTML = slice.map(r => `<tr data-id="${r.id}" class="${r.id === selId ? "sel" : ""}">` +
        `<td><div class="bname" title="${e(r.name)}">${e(r.name)}</div><div class="bcat">${e(r.category)}</div></td>` +
        `<td class="c-area">${e(r.area)}</td>` +
        `<td class="c-web"><span class="wst" title="${e(r.website || "")}">${e(r.website_status || "—")}</span></td>` +
        `<td class="num">${scoreCell(r.website_score)}</td><td class="num">${scoreCell(r.ai_score)}</td>` +
        `<td><span class="pill p${e(r.priority)}">${e(r.priority || "—")}</span></td>` +
        `<td><span class="pill st-${e(r.status)}">${e(STATUS_LABEL[r.status] || r.status)}</span></td>` +
        `<td class="c-src">${r.source_url ? `<a class="srclink" data-url="${e(r.source_url)}" title="${e(r.source_name)}">${/openstreetmap/i.test(r.source_url) ? "OSM" : "Source"} ↗</a>` : "—"}</td></tr>`).join("");
    }
    g("lcount").textContent = biz.length ? `${rows.length} of ${biz.length} businesses` : "";
    g("lpage").textContent = rows.length ? `${T.page + 1} / ${pages}` : "";
    g("lprev").disabled = T.page <= 0; g("lnext").disabled = T.page >= pages - 1;
    document.querySelectorAll("#ltable th[data-k]").forEach(th => {
      th.classList.toggle("sorted", th.dataset.k === T.sortK); th.classList.toggle("asc", th.dataset.k === T.sortK && T.asc);
    });
    document.querySelectorAll(".qa[data-qa=quote]").forEach(b => { b.disabled = !selId; });
  }
  g("lbody").addEventListener("click", ev => {
    const a = ev.target.closest(".srclink");
    if (a) { ev.stopPropagation(); openUrl(a.dataset.url); return; }
    const tr = ev.target.closest("tr[data-id]");
    if (tr) { selectProspect(+tr.dataset.id); openRp(); }
  });
  g("lq").addEventListener("input", () => { T.q = g("lq").value.trim(); T.page = 0; renderTable(); });
  g("lstatus").addEventListener("change", () => { T.status = g("lstatus").value; T.page = 0; renderTable(); });
  g("lprio").addEventListener("change", () => { T.prio = g("lprio").value; T.page = 0; renderTable(); });
  g("lprev").addEventListener("click", () => { T.page--; renderTable(); });
  g("lnext").addEventListener("click", () => { T.page++; renderTable(); });
  document.querySelectorAll("#ltable th[data-k]").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (T.sortK === k) { if (T.asc) { T.sortK = ""; T.asc = false; } else T.asc = true; } else { T.sortK = k; T.asc = false; }
    renderTable();
  }));

  // ------------------------------------------------------------------ business data (table, coverage, map)
  async function loadBusiness() {
    let data;
    try { data = await getJSON("/dashboard/business"); } catch (err) {
      g("lbody").innerHTML = `<tr><td colspan="8"><div class="empty-note">Couldn't load businesses (${e(err.message)})</div></td></tr>`; return;
    }
    biz = data.items || [];
    renderTable(); renderCoverage(data.coverage || []);
    if (selId == null || !biz.some(r => r.id === selId)) {
      const f = summary && summary.featured;
      if (f != null) selectProspect(f, true); else if (!biz.length) renderProspect(null);
    }
    loadMap();
  }
  function renderCoverage(rows) {
    if (!rows.length) {
      g("covbody").innerHTML = `<div class="empty-note">No areas scanned yet.<br>Coverage appears here as OpenStreetMap cells are searched.</div>`; return;
    }
    g("covbody").innerHTML = rows.map(r => {
      const pct = r.cells ? Math.round(100 * r.done / r.cells) : 0;
      return `<div class="covrow"><div class="ct"><b title="${e(r.area)}">${e(r.area.split(",")[0])}</b><span>${r.done}/${r.cells} cells</span></div>` +
        meter(pct) + `<div class="cs">${r.candidates} businesses · ${r.qualified} qualified${r.failed ? ` · ${r.failed} cell(s) failed` : ""}` +
        ` · last scanned ${r.last ? ago(r.last) : "never"}</div><div class="cs" title="${e(r.categories)}">${e(r.categories)}</div></div>`;
    }).join("") + `<div class="covnote">Searched OpenStreetMap only — it doesn't list every business, so this is never "everything in the area".</div>`;
  }
  async function loadMap() {
    let m; try { m = await getJSON("/dashboard/map"); } catch { return; }
    renderMap(m);
  }
  function renderMap(m) {
    const box = g("mapbox");
    const pts = m.points || [], cells = m.cells || [];
    g("mapattr").textContent = (pts.length || cells.length) ? "Data " + (m.attribution || "") : "";
    if (!pts.length && !cells.length) {
      box.innerHTML = `<div class="empty-note" style="padding-top:70px">Nothing plotted yet.<br>Scanned businesses and the cells searched will appear here.</div>`; return;
    }
    let s = 90, n = -90, w = 180, ea = -180;
    const take = (la, lo) => { s = Math.min(s, la); n = Math.max(n, la); w = Math.min(w, lo); ea = Math.max(ea, lo); };
    cells.forEach(c => { take(c.s, c.w); take(c.n, c.e); }); pts.forEach(p => take(p.lat, p.lon));
    const W = 600, H = 230, pad = 18, k = Math.cos(((s + n) / 2) * Math.PI / 180);
    const spanX = Math.max(1e-4, (ea - w) * k), spanY = Math.max(1e-4, n - s);
    const sc = Math.min((W - 2 * pad) / spanX, (H - 2 * pad) / spanY);
    const ox = (W - spanX * sc) / 2, oy = (H - spanY * sc) / 2;
    const X = lo => ox + (lo - w) * k * sc, Y = la => oy + (n - la) * sc;
    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Plot of scanned businesses">`;
    for (let x = 0; x <= W; x += 50) svg += `<line class="gridl" x1="${x}" y1="0" x2="${x}" y2="${H}"/>`;
    for (let y = 0; y <= H; y += 46) svg += `<line class="gridl" x1="0" y1="${y}" x2="${W}" y2="${y}"/>`;
    cells.forEach(c => {
      svg += `<rect class="mcell ${c.status === "done" ? "" : e(c.status)}" x="${X(c.w).toFixed(1)}" y="${Y(c.n).toFixed(1)}" width="${Math.max(2, (X(c.e) - X(c.w))).toFixed(1)}" height="${Math.max(2, (Y(c.s) - Y(c.n))).toFixed(1)}">` +
        `<title>${e(c.area)} · ${e(c.status)} · ${c.candidates} businesses</title></rect>`;
    });
    pts.forEach(p => {
      svg += `<circle class="mpt ${e(p.priority || "X")}${p.id === selId ? " sel" : ""}" data-id="${p.id}" cx="${X(p.lon).toFixed(1)}" cy="${Y(p.lat).toFixed(1)}" r="${p.id === selId ? 7 : 5}">` +
        `<title>${e(p.name)} — ${e(p.category)} · priority ${e(p.priority || "?")}</title></circle>`;
    });
    svg += `</svg><div class="mapnote">${e(m.note || "")}</div>`;
    box.innerHTML = svg;
  }
  g("mapbox").addEventListener("click", ev => {
    const c = ev.target.closest(".mpt"); if (c) { selectProspect(+c.dataset.id); openRp(); }
  });

  // ------------------------------------------------------------------ research + activity + upcoming
  async function loadActivity() {
    let d; try { d = await getJSON("/dashboard/activity"); } catch { return; }
    const res = d.research || [];
    g("resbody").innerHTML = res.length ? res.map(r =>
      `<div class="fitem"><span class="fdot research"></span><div class="fmain">` +
      `<div class="ftitle">${r.source_url ? `<a data-url="${e(r.source_url)}" title="${e(r.summary)}">${e(r.title)}</a>` : e(r.title)}</div>` +
      `<div class="fmeta"><span class="tag2">${e(r.topic)}</span> · ${e(r.source_name || "")} · ${ago(r.found)}</div></div></div>`).join("")
      : `<div class="empty-note">No research findings yet.<br>Night Mode research adds sourced findings here.</div>`;
    const act = d.items || [];
    g("actbody").innerHTML = act.length ? act.map(a =>
      `<div class="fitem"><span class="fdot ${e(a.kind)}"></span><div class="fmain"><div class="ftitle">${e(a.text)}</div>` +
      `<div class="fmeta">${ago(a.at)}</div></div></div>`).join("")
      : `<div class="empty-note">Nothing recorded yet.</div>`;
  }
  g("resbody").addEventListener("click", ev => { const a = ev.target.closest("a[data-url]"); if (a) openUrl(a.dataset.url); });
  async function loadUpcoming() {
    let d; try { d = await getJSON("/dashboard/upcoming"); } catch { return; }
    const it = d.items || [];
    g("upbody").innerHTML = it.length ? it.map(u =>
      `<div class="upitem ${e(u.kind)}"><span class="uptime">${u.at ? e(when(u.at)) : "Now"}</span>` +
      `<div><div class="uptitle">${e(u.title)}</div><div class="updet">${e(u.detail || "")}</div></div></div>`).join("")
      : `<div class="empty-note">Nothing scheduled.</div>`;
  }

  // ------------------------------------------------------------------ right panel: one prospect
  let cur = null;
  async function selectProspect(id, quiet) {
    selId = id;
    document.querySelectorAll("#lbody tr").forEach(tr => tr.classList.toggle("sel", +tr.dataset.id === id));
    document.querySelectorAll(".mpt").forEach(c => { const on = +c.dataset.id === id; c.classList.toggle("sel", on); c.setAttribute("r", on ? 7 : 5); });
    document.querySelectorAll(".qa[data-qa=quote]").forEach(b => { b.disabled = false; });
    try { cur = await getJSON("/dashboard/prospect?id=" + encodeURIComponent(id)); }
    catch (err) { if (!quiet) toast("Couldn't load that business (" + err.message + ")", "err"); return; }
    renderProspect(cur);
  }
  function auditHtml(p) {
    const a = p.audit_view || {};
    if (!a.audited) {
      return `<div class="arow2"><span>Website</span><b>${e(p.website_status || "not checked")}</b></div>` +
        (a.note ? `<div class="safe">${e(a.note)}</div>` : "") +
        (p.website ? `<div class="arow2"><span>Listed site</span><b><a class="srclink" data-url="${e(p.website)}">${e(p.website.replace(/^https?:\/\//, "").slice(0, 32))} ↗</a></b></div>` : "") +
        (a.checked ? `<div class="safe">Checked ${ago(a.checked)}</div>` : "");
    }
    return (a.rows || []).map(r => `<div class="arow2"><span>${e(r.k)}</span><b class="${r.ok === true ? "ok" : r.ok === false ? "bad" : ""}">${e(r.v)}</b></div>`).join("") +
      `<div class="safe"><a class="srclink" data-url="${e(a.url)}">${e(String(a.url).replace(/^https?:\/\//, "").slice(0, 40))} ↗</a> · audited ${ago(a.checked)}</div>`;
  }
  function renderProspect(p) {
    const body = g("rpbody");
    if (!p) {
      body.innerHTML = `<div class="rpsec"><div class="empty-note"><b>No business opportunities yet.</b><br>When the prospecting engine finds and audits local businesses, the best one appears here with its audit, package and draft.</div></div>`;
      return;
    }
    const q = p.price || "", priced = q && q !== "NEEDS USER PRICING";
    const d = p.draft || {}, drafted = p.status === "DRAFTED", contacted = CONTACTED.includes(p.status);
    const reasons = (p.reasons || []).slice(0, 5).map(r => `<li class="${r.kind === "ai" ? "ai" : ""}" title="${e(r.detail || "")}">${e(r.factor)}</li>`).join("");
    body.innerHTML =
      `<section class="rpsec"><div class="bizname">${e(p.name)}</div>` +
      `<div class="bizmeta">${e(p.category)} · ${e(p.area)} · <span class="pill st-${e(p.status)}">${e(STATUS_LABEL[p.status] || p.status)}</span> ` +
      (p.priority ? `<span class="pill p${e(p.priority)}" title="${e(p.priority_reason || "")}">Priority ${e(p.priority)}</span>` : "") + `</div>` +
      `<div class="scorepair"><div class="scorebox"><div class="sl">Website</div><div class="sv">${p.website_score == null ? "—" : p.website_score + "/5"}</div></div>` +
      `<div class="scorebox"><div class="sl">AI opportunity</div><div class="sv">${p.ai_score}/5</div></div></div>` +
      (reasons ? `<ul class="reasons">${reasons}</ul>` : `<div class="safe">No specific gaps recorded.</div>`) +
      (p.source_url ? `<div class="safe">Source: <a class="srclink" data-url="${e(p.source_url)}">${e(p.source_name || "source")} ↗</a>${p.verification ? " · " + e(p.verification) : ""}</div>` : "") +
      `</section>` +
      `<section class="rpsec"><h4>Website audit</h4>${auditHtml(p)}</section>` +
      `<section class="rpsec" id="pkgsec"><h4>Recommended package</h4>` +
      (p.package ? `<div class="pkgname">${e(p.package)}</div>` +
        (priced ? `<div class="price">${e(q)}</div>` : `<div class="price needs">Needs your pricing — add it to [prospecting.pricing]. Helios never invents prices.</div>`) +
        (p.timeline ? `<div class="safe">Timeline: about ${e(p.timeline)}</div>` : "") +
        ((p.features || []).length ? `<div class="feats">${p.features.slice(0, 8).map(f => `<span>${e(f)}</span>`).join("")}</div>` : "")
        : `<div class="safe">No package yet — it's chosen once the business is researched.</div>`) +
      `</section>` +
      `<section class="rpsec"><h4>Outreach</h4>` +
      (d.body ? `<div class="arow2"><span>${d.channel === "phone" ? "Call notes" : "Email"}</span><b title="${e(d.to_source || "")}">${e(d.to || "no public contact")}</b></div>` +
        `<div class="safe" style="margin-top:4px">${e(d.subject || "")}</div>`
        : `<div class="safe">No draft yet.</div>`) +
      `<div class="dactions2">` +
      `<button data-act="view" ${d.body ? "" : "disabled"}>View</button>` +
      `<button data-act="edit" ${d.body && drafted ? "" : "disabled"} title="${drafted ? "Edit the text before approving" : "Only drafts waiting for approval can be edited"}">Edit</button>` +
      `<button data-act="reject" ${contacted || ["REJECTED", "ARCHIVED"].includes(p.status) ? "disabled" : ""}>Reject</button>` +
      `<button data-act="archive" ${p.status === "ARCHIVED" || contacted ? "disabled" : ""}>Archive</button>` +
      `<button data-act="approve" class="approve" ${drafted && d.to ? "" : "disabled"} title="${drafted ? (d.to ? "Approve — sends nothing" : "No public business contact") : "Only drafts can be approved"}">Approve</button></div>` +
      `<div class="safe">Approving sends nothing. You send it yourself (Gmail drafts via <code>helios prospects gmail ${p.id}</code>); it's marked contacted only with evidence.</div>` +
      ((p.history || []).length ? `<div class="safe">History: ${(p.history || []).slice(-3).map(h => `${e(h.event)} ${ago(h.at)}`).join(" · ")}</div>` : "") +
      `</section>`;
  }
  g("rpbody").addEventListener("click", async ev => {
    const a = ev.target.closest(".srclink"); if (a) { openUrl(a.dataset.url); return; }
    const b = ev.target.closest("button[data-act]"); if (!b || b.disabled || !cur) return;
    const act = b.dataset.act;
    if (act === "view" || act === "edit") { openDraft(cur, act === "edit"); return; }
    const msgs = { approve: `Approve the outreach for ${cur.name}?\n\nNothing is sent — you send it yourself afterwards.`,
                   reject: `Reject ${cur.name}? It leaves the queue.`, archive: `Archive ${cur.name}?` };
    if (!window.confirm(msgs[act])) return;
    b.disabled = true;
    try {
      await postJSON("/prospects/action", { id: cur.id, action: act });
      toast(act === "approve" ? `Approved — nothing was sent. Send it yourself when ready.` : `${cur.name}: ${act}ed`, "ok");
      await selectProspect(cur.id, true); loadBusiness(); loadSummary(); loadUpcoming();
    } catch (err) { toast(`Couldn't ${act}: ${err.message}`, "err"); b.disabled = false; }
  });
  function step(dir) {
    const rows = filtered(); if (!rows.length) return;
    let i = rows.findIndex(r => r.id === selId);
    i = i < 0 ? 0 : (i + dir + rows.length) % rows.length;
    selectProspect(rows[i].id);
  }
  g("rpprev").addEventListener("click", () => step(-1));
  g("rpnext").addEventListener("click", () => step(1));

  // ------------------------------------------------------------------ draft modal
  function openDraft(p, edit) {
    const d = p.draft || {};
    g("dm_title").textContent = d.channel === "phone" ? `Call notes — ${p.name}` : `Outreach email — ${p.name}`;
    g("dm_meta").textContent = d.channel === "phone" ? `For you to use on a call or visit · ${d.to || ""}`
      : `To ${d.to || "?"} (${d.to_source || "source unknown"}) · ${STATUS_LABEL[p.status] || p.status}`;
    g("dm_subject").value = d.subject || ""; g("dm_body").value = d.body || "";
    const claims = d.claims || [];
    g("dm_claimlist").innerHTML = claims.length ? `<ul>${claims.map(c => `<li><b>${e(c.text)}</b> ← ${e(c.evidence)}</li>`).join("")}</ul>` : `<div class="safe">No factual claims in this draft.</div>`;
    setDraftEdit(!!edit && p.status === "DRAFTED");
    g("dm_edit").classList.toggle("hidden", p.status !== "DRAFTED");
    g("draftmodal").classList.remove("hidden");
  }
  function setDraftEdit(on) {
    g("dm_subject").readOnly = !on; g("dm_body").readOnly = !on;
    g("dm_save").classList.toggle("hidden", !on); g("dm_edit").textContent = on ? "Cancel edit" : "Edit";
    if (on) g("dm_body").focus();
  }
  g("dm_edit").addEventListener("click", () => {
    const on = g("dm_body").readOnly;
    if (!on && cur) { g("dm_subject").value = (cur.draft || {}).subject || ""; g("dm_body").value = (cur.draft || {}).body || ""; }
    setDraftEdit(on);
  });
  g("dm_close").addEventListener("click", () => g("draftmodal").classList.add("hidden"));
  g("dm_save").addEventListener("click", async () => {
    if (!cur) return;
    try {
      await postJSON("/prospects/action", { id: cur.id, action: "edit", subject: g("dm_subject").value, body: g("dm_body").value });
      toast("Draft saved", "ok"); g("draftmodal").classList.add("hidden"); selectProspect(cur.id, true);
    } catch (err) { toast("Couldn't save: " + err.message, "err"); }
  });

  // ------------------------------------------------------------------ drawers: modules, projects, report, insights
  async function openModule(id, label) {
    setNav(id);
    const el = infoPane(label, `<div class="skel"></div><div class="skel"></div>`);
    let m; try { m = await getJSON("/dashboard/module?id=" + encodeURIComponent(id)); }
    catch (err) { el.innerHTML = `<div class="infopane"><div class="empty-note">Couldn't load (${e(err.message)})</div></div>`; return; }
    const stateTxt = { ok: "Ready", warn: "Needs attention", off: "Off", err: "Problem" }[m.state] || m.state;
    let actions = "";
    if (id === "voice") actions = `<button class="primary" data-x="mic">${micbtn.classList.contains("on") ? "Turn voice off" : "Turn voice on"}</button><button class="ghost" data-x="settings">Voice settings</button>`;
    if (id === "night") actions = `<button class="primary" data-x="night">Run Night Mode now</button><button class="ghost" data-x="report">Latest report</button>`;
    el.innerHTML = `<div class="infopane"><div class="kv"><span>Status</span><b class="${m.state === "ok" ? "ok" : m.state === "off" ? "na" : "bad"}">${e(stateTxt)}</b></div>` +
      `<div class="lines">${(m.lines || []).map(l => `<div>${e(l)}</div>`).join("")}</div>` +
      (m.help ? `<div class="help">${e(m.help)}</div>` : "") + (actions ? `<div class="row">${actions}</div>` : "") + `</div>`;
    el.querySelectorAll("[data-x]").forEach(b => b.addEventListener("click", () => {
      const x = b.dataset.x;
      if (x === "mic") { micbtn.click(); setTimeout(() => openModule(id, label), 1200); }
      else if (x === "settings") g("setbtn").click();
      else if (x === "night") runAction("night_mode");
      else if (x === "report") openReport();
    }));
  }
  async function openProjects() {
    setNav("projects");
    const el = infoPane("Projects", `<div class="skel"></div><div class="skel"></div>`);
    let d; try { d = await getJSON("/dashboard/projects"); } catch (err) { el.innerHTML = `<div class="infopane"><div class="empty-note">Couldn't load (${e(err.message)})</div></div>`; return; }
    const it = d.items || [];
    el.innerHTML = `<div class="infopane">` + (it.length ? it.map(p =>
      `<div class="prcard"><div class="prhead"><span class="prname">${e(p.project)}</span><span class="vbadge ${e(p.verdict)}">${e(p.verdict)}</span></div>` +
      ((p.issues || []).length ? `<div class="prmore"><ul>${p.issues.map(i => `<li>${e(i)}</li>`).join("")}</ul></div>` : `<div class="safe">No issues recorded.</div>`) +
      `<div class="safe">Checked ${ago(p.at)}</div></div>`).join("")
      : `<div class="empty-note">No active projects registered. Add one with <code>helios projects add &lt;path&gt;</code>.</div>`) +
      `<div class="row"><button class="primary" id="prun">Run checks now</button></div></div>`;
    const b = el.querySelector("#prun"); if (b) b.addEventListener("click", () => runAction("project_health"));
  }
  async function openReport() {
    const el = infoPane("Night Mode report", `<div class="skel"></div>`);
    let d; try { d = await getJSON("/dashboard/report"); } catch (err) { el.innerHTML = `<div class="infopane"><div class="empty-note">Couldn't load (${e(err.message)})</div></div>`; return; }
    el.innerHTML = `<div class="infopane"><pre>${e(d.text || "No night report yet.")}</pre></div>`;
  }
  async function openInsights() {
    const el = infoPane("AI insights", `<div class="skel"></div>`);
    if (!limitsData) await loadLimits(false);
    const L = limitsData || {}, loc = L.local || {}, h = L.health || {};
    el.innerHTML = `<div class="infopane">` +
      `<h5>Brain</h5><div class="kv"><span>Backend</span><b>${e(L.backend || "—")}</b></div>` +
      (L.models || []).map(m => `<div class="kv"><span>${e(m.role)}</span><b class="${m.offered === false ? "bad" : ""}"` +
        `${m.offered === false ? ` title="Not in your account's agy models list — runs using it will fail"` : ""}>${e(m.model)}` +
        `${m.offered === false ? " · not offered" : ""}</b></div>`).join("") +
      `<div class="kv"><span>Engine check</span><b class="${h.ok ? "ok" : h.ok === false ? "bad" : "na"}">${e(h.detail || "not checked")}</b></div>` +
      ((h.models || []).length ? `<h5>Models your account offers (agy models)</h5>` + h.models.map(m => `<div class="kv"><span>${e(m.id)}</span><b>${e(m.name)}</b></div>`).join("") : "") +
      `<h5>Helios usage today by purpose</h5>` +
      ((loc.by_purpose || []).length ? loc.by_purpose.map(x => `<div class="kv"><span>${e(x.purpose)}</span><b>${fmtTok(x.tokens)} tok · ${x.calls} calls</b></div>`).join("")
        : `<div class="empty-note">No AI calls recorded today.</div>`) +
      `<div class="help">These are tokens Helios measured itself (${e(loc.source || "usage log")}). Your provider's quota is separate: ${e((L.provider || {}).why || "")}</div></div>`;
  }

  // ------------------------------------------------------------------ nav + shortcuts + quick actions
  document.querySelectorAll(".navi").forEach(b => b.addEventListener("click", () => {
    const id = b.dataset.nav, label = b.querySelector("span").textContent;
    if (tiny()) closeOverlays();
    if (id === "home") { setNav("home"); showView("home"); g("home").scrollTo({ top: 0, behavior: REDUCED_MOTION ? "auto" : "smooth" }); }
    else if (id === "research") { setNav("research"); showView("home"); g("rescard").scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth", block: "start" }); }
    else if (id === "leads") { setNav("leads"); g("prospectbtn").click(); }
    else if (id === "projects") openProjects();
    else if (id === "settings") { setNav("settings"); g("setbtn").click(); }
    else openModule(id, label);
  }));
  const scrollTo = (id) => { showView("home"); g(id).scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth", block: "start" }); };
  document.querySelectorAll(".shortcuts button").forEach(b => b.addEventListener("click", () => {
    const sc = b.dataset.sc;
    if (sc === "today") { scrollTo("actcard"); if (narrow()) openRp(); g("upsec").scrollIntoView({ block: "nearest" }); }
    else if (sc === "leads") scrollTo("leadscard");
    else if (sc === "research") scrollTo("rescard");
    else if (sc === "projects") openProjects();
    else if (sc === "insights") openInsights();
  }));

  async function runAction(action) {
    const ask = action === "night_mode"
      ? "Start Night Mode now?\n\nIt researches and drafts in the background (uses AI quota) and never sends anything."
      : "Run your projects' health checks now?\n\nThey run exactly the commands in your project manifests.";
    if (!window.confirm(ask)) return;
    try { const r = await postJSON("/dashboard/run", { action }); toast(r.message + " — you'll see the result here when it finishes.", "ok"); }
    catch (err) { toast("Not started: " + err.message, "err"); }
  }
  let qaKind = "";
  function askInput(kind) {
    qaKind = kind;
    const scan = kind === "scan";
    g("qa_title").textContent = scan ? "Scan an area" : "Audit a website";
    g("qa_help").textContent = scan
      ? "Helios searches OpenStreetMap for local businesses there, checks their websites and drafts outreach for the best ones. Nothing is sent."
      : "Helios opens the site in its hidden browser and checks HTTPS, speed, phone layout, contact routes, booking, WhatsApp and SEO basics.";
    g("qa_input").placeholder = scan ? "e.g. Banjara Hills, Hyderabad" : "https://example.com";
    g("qa_input").value = "";
    g("qamodal").classList.remove("hidden"); g("qa_input").focus();
  }
  function ask(text) { input.value = text; sendMessage(); }
  g("qa_cancel").addEventListener("click", () => g("qamodal").classList.add("hidden"));
  g("qa_input").addEventListener("keydown", ev => { if (ev.key === "Enter") g("qa_run").click(); });
  g("qa_run").addEventListener("click", () => {
    let v = g("qa_input").value.trim(); if (!v) { g("qa_input").focus(); return; }
    if (qaKind === "audit") {
      if (!/^https?:\/\//i.test(v)) v = "https://" + v;
      if (!/^https?:\/\/[^\s/]+\.[^\s]+/i.test(v)) { toast("That doesn't look like a website address.", "err"); return; }
      ask(`Audit the website ${v} with the prospecting website audit and tell me what you found.`);
    } else {
      ask(`Scan ${v} for local business prospects (OpenStreetMap) and research the most promising ones.`);
    }
    g("qamodal").classList.add("hidden");
  });
  document.querySelectorAll(".qa").forEach(b => b.addEventListener("click", () => {
    const k = b.dataset.qa;
    if (k === "scan" || k === "audit") askInput(k);
    else if (k === "leads") ask("Find new freelance leads for my services.");
    else if (k === "quote") {
      if (!selId) { toast("Pick a business first (Top leads or the map).", "err"); return; }
      openRp(); const s = g("pkgsec"); if (s) { s.scrollIntoView({ block: "nearest" }); s.animate?.([{ boxShadow: "0 0 0 2px var(--neon)" }, { boxShadow: "0 0 0 0 transparent" }], 1200); }
    }
    else if (k === "reports") openReport();
    else if (k === "night") runAction("night_mode");
    else if (k === "health") runAction("project_health");
  }));

  // ------------------------------------------------------------------ refresh loop
  function scheduleRefresh(ms) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => { loadSummary().then(loadBusiness); loadActivity(); loadUpcoming(); }, ms);
  }
  function boot() {
    showView("home"); updCount(); renderRobot();
    loadSummary().then(loadBusiness);
    loadLimits(false); loadActivity(); loadUpcoming();
    setInterval(() => { if (!document.hidden) loadSummary(); }, 60000);
    setInterval(() => { if (!document.hidden) { loadBusiness(); loadActivity(); loadUpcoming(); } }, 180000);
    setInterval(() => { if (!document.hidden) loadLimits(false); }, 300000);
  }
  boot();
})();
