const g = id => document.getElementById(id);   // declared first so all wiring below can use it
const chat = g("chat");
const input = g("input");
const send = g("send");
const modelTag = g("model");
const banner = g("banner");
const bannerText = g("banner-text");
const modal = g("modal");
const modalSummary = g("modal-summary");
const micbtn = g("micbtn");
const yolobtn = g("yolobtn");
const yolobadge = g("yolobadge");
const voicehud = g("voicehud");
const vstate = g("vstate");
const vtext = g("vtext");
const composer = g("composer");

let current = null;        // current streaming Helios bubble
let pendingPermId = null;
let busy = false;
let voiceActive = false;   // a voice interaction is on screen (suppresses the text banner)
let voiceShown = "";       // last spoken transcript turned into a user bubble (de-dupe)
let voiceHideTimer = null;
const VSTATE_LABEL = {
  listening: "Listening…", thinking: "Thinking…", speaking: "Speaking",
  dictation: "Dictation — speak now", error: "Trouble hearing you",
};

// Local auth token (injected by the server when it serves this page). Every call to
// the Helios server must carry it; this is what keeps random web pages from driving us.
const TOKEN = window.__HELIOS_TOKEN__ || "";
function jfetch(path, opts = {}) {
  const o = Object.assign({}, opts);
  o.headers = Object.assign({ "X-Auth-Token": TOKEN }, opts.headers || {});
  return fetch(path, o);
}

const REDUCED_MOTION = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);

function esc(s) {
  // Escape quotes too, not just &<> — model/tool output flows into href="..." in render(),
  // so unescaped quotes would let injected content break out and add event handlers (XSS).
  return s.replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function render(text) {
  // minimal markdown: fenced code, inline code, bold, line breaks
  let html = esc(text);
  html = html.replace(/```([\s\S]*?)```/g, (_, c) => `<pre>${c.replace(/^\n/, "")}</pre>`);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  // markdown links [label](url) -> clickable label (URL kept clean, no stray paren)
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" class="lnk">$1</a>');
  // bare urls (preceded by start/space/paren so we don't touch ones already in an anchor);
  // trim trailing punctuation that isn't part of the URL
  html = html.replace(/(^|[\s(])(https?:\/\/[^\s<>"'`]+)/g, (m, pre, url) => {
    const u = url.replace(/[).,;:!?\]]+$/, "");
    return `${pre}<a href="${u}" class="lnk">${u}</a>`;
  });
  return html.replace(/\n/g, "<br>");
}
function atBottom() { return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80; }
function scroll() { chat.scrollTop = chat.scrollHeight; }

const emptyEl = g("empty");
function clearChat() {
  // Remove conversation content but keep the empty-state element, then reveal it.
  [...chat.querySelectorAll(".msg, .chip, .memnote, .usage, .turnact")].forEach(n => n.remove());
  if (emptyEl) emptyEl.classList.remove("hidden");
  current = null; act = null;
}
function bubble(kind, who) {
  if (emptyEl) emptyEl.classList.add("hidden");   // first message hides the empty state
  const el = document.createElement("div");
  el.className = "msg " + kind;
  if (kind === "helios" || kind === "error") {
    // small neural mark floating beside the reply — Helios's avatar
    const mk = document.createElement("span");
    mk.className = "jmark";
    mk.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none"/>' +
      '<circle cx="5.5" cy="8" r="1.4" fill="currentColor" stroke="none"/>' +
      '<circle cx="18.5" cy="7" r="1.4" fill="currentColor" stroke="none"/>' +
      '<circle cx="7" cy="18" r="1.4" fill="currentColor" stroke="none"/>' +
      '<circle cx="17.5" cy="17.5" r="1.4" fill="currentColor" stroke="none"/>' +
      '<path d="M10.2 10.8 6.6 8.8M13.8 10.6l3.4-2.8M10.4 13.5 7.9 17M13.6 13.4l2.9 3.2" opacity=".55"/></svg>';
    el.appendChild(mk);
  }
  if (who) { const w = document.createElement("div"); w.className = "who"; w.textContent = who; el.appendChild(w); }
  const body = document.createElement("div"); body.className = "body"; el.appendChild(body);
  chat.appendChild(el); scroll();
  return body;
}
function chip(text) {
  const el = document.createElement("div"); el.className = "chip"; el.textContent = text;
  chat.appendChild(el); if (atBottom()) scroll();
}

function setBusy(on, label) {
  busy = on;
  send.disabled = on;
  document.body.classList.toggle("working", on);   // drives the title-bar hairline drift
  // While the voice HUD is up it already shows the live state — don't double up with the banner.
  banner.classList.toggle("hidden", !on || voiceActive);
  if (label) bannerText.textContent = label;
}

/* ==========================================================================
   Living canvases — the micro synapse-cluster status dot (title bar) and the
   neural emblem (empty state). Same visual language as the desktop orb: nodes
   on a slowly-turning sphere, depth-dimmed links, colour = state. Idle barely
   breathes; thinking/acting quicken it. Reduced-motion renders one still frame.
   ========================================================================== */
const VIZ_STATES = {   // [r,g,b], spin rad/s, pulse hz — mirrors the orb's vocabulary
  idle:      [[77, 208, 225], 0.22, 0.9],
  thinking:  [[102, 224, 255], 0.85, 2.0],
  acting:    [[214, 162, 99], 0.6, 1.6],
  error:     [[224, 85, 107], 0.95, 3.0],
  listening: [[126, 224, 160], 0.45, 1.5],
  speaking:  [[102, 224, 255], 0.6, 1.9],
  dictation: [[189, 170, 240], 0.5, 1.6],
};
function sphereNodes(n) {
  const pts = [];
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const phi = i * Math.PI * (3 - Math.sqrt(5));
    pts.push([Math.cos(phi) * r, y, Math.sin(phi) * r]);
  }
  return pts;
}
function sphereEdges(nodes, maxD2) {
  const e = [];
  for (let i = 0; i < nodes.length; i++)
    for (let j = i + 1; j < nodes.length; j++) {
      const dx = nodes[i][0] - nodes[j][0], dy = nodes[i][1] - nodes[j][1], dz = nodes[i][2] - nodes[j][2];
      if (dx * dx + dy * dy + dz * dz < maxD2) e.push([i, j]);
    }
  return e;
}
class NeuralViz {
  constructor(canvas, opts) {
    this.cv = canvas; this.ctx = canvas.getContext("2d");
    this.o = Object.assign({ n: 26, edgeD2: 0.55, lineW: 1, fps: 30, pulses: true }, opts || {});
    this.nodes = sphereNodes(this.o.n);
    this.edges = sphereEdges(this.nodes, this.o.edgeD2);
    this.state = "idle"; this.cur = [77, 208, 225]; this.spin = 0.22; this.pulseHz = 0.9;
    this.level = 0; this.levelSmooth = 0;
    this.ang = Math.random() * 6; this.t = 0; this.last = 0;
    this.sparks = [];        // traveling signal pulses: {e, t, v}
    this.running = false; this._raf = 0;
  }
  setState(s) { this.state = VIZ_STATES[s] ? s : "idle"; if (REDUCED_MOTION) this.drawFrame(0); }
  setLevel(l) { this.level = Math.max(0, Math.min(1, l || 0)); }
  visible() { return this.cv.offsetParent !== null && !document.hidden; }
  start() {
    if (REDUCED_MOTION) { this.drawFrame(0); return; }   // one calm still frame
    if (this.running) return;
    this.running = true; this.last = performance.now();
    const loop = (now) => {
      if (!this.running) return;
      this._raf = requestAnimationFrame(loop);
      const dt = Math.min(0.1, (now - this.last) / 1000);
      // frame cap: idle canvases don't need 60fps
      if (dt < 1 / this.o.fps) return;
      this.last = now;
      if (this.visible()) this.drawFrame(dt);
    };
    this._raf = requestAnimationFrame(loop);
  }
  stop() { this.running = false; cancelAnimationFrame(this._raf); }
  drawFrame(dt) {
    const [tc, tspin, tpulse] = VIZ_STATES[this.state] || VIZ_STATES.idle;
    for (let i = 0; i < 3; i++) this.cur[i] += (tc[i] - this.cur[i]) * 0.12;
    this.spin += (tspin - this.spin) * 0.08;
    this.pulseHz += (tpulse - this.pulseHz) * 0.08;
    this.t += dt; this.ang += this.spin * dt;
    this.levelSmooth += (this.level - this.levelSmooth) * 0.3; this.level *= 0.92;

    const ctx = this.ctx, W = this.cv.width, H = this.cv.height, cx = W / 2, cy = H / 2;
    const pulse = 0.93 + 0.07 * Math.sin(this.t * this.pulseHz * Math.PI);
    const R = (Math.min(W, H) * 0.36) * pulse * (1 + 0.18 * this.levelSmooth);
    const ca = Math.cos(this.ang), sa = Math.sin(this.ang);
    const tilt = 0.42, ct = Math.cos(tilt), st = Math.sin(tilt);
    const pts = this.nodes.map(p => {
      const x = p[0] * ca - p[2] * sa, z0 = p[0] * sa + p[2] * ca, y = p[1];
      const y2 = y * ct - z0 * st, z2 = y * st + z0 * ct;
      const persp = 1 / (1.9 - z2);
      return [cx + x * R * persp, cy + y2 * R * persp, (z2 + 1) / 2];   // depth 0..1
    });
    const [r, gg, b] = this.cur.map(Math.round);
    ctx.clearRect(0, 0, W, H);
    // links, depth-dimmed
    ctx.lineWidth = this.o.lineW;
    for (const [i, j] of this.edges) {
      const d = (pts[i][2] + pts[j][2]) / 2;
      ctx.strokeStyle = `rgba(${r},${gg},${b},${(0.06 + 0.2 * d).toFixed(3)})`;
      ctx.beginPath(); ctx.moveTo(pts[i][0], pts[i][1]); ctx.lineTo(pts[j][0], pts[j][1]); ctx.stroke();
    }
    // signal sparks travel a link then die; spawn rate scales with activity
    if (this.o.pulses && !REDUCED_MOTION) {
      const live = this.state !== "idle";
      const rate = live ? 3.2 : 0.35;                     // sparks/sec
      if (Math.random() < rate * dt && this.edges.length)
        this.sparks.push({ e: this.edges[(Math.random() * this.edges.length) | 0], t: 0, v: 0.9 + Math.random() * 1.3 });
      this.sparks = this.sparks.filter(s => (s.t += dt * s.v) < 1);
      for (const s of this.sparks) {
        const [i, j] = s.e, x = pts[i][0] + (pts[j][0] - pts[i][0]) * s.t, y = pts[i][1] + (pts[j][1] - pts[i][1]) * s.t;
        const a = Math.sin(s.t * Math.PI);
        ctx.fillStyle = `rgba(${Math.min(255, r + 70)},${Math.min(255, gg + 60)},${Math.min(255, b + 60)},${(0.75 * a).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(x, y, this.o.lineW * 1.6, 0, 7); ctx.fill();
      }
    }
    // nodes, front bigger + brighter, soft glow via shadowBlur
    for (const p of pts) {
      const d = p[2], rad = (0.55 + 1.5 * d) * (Math.min(W, H) / 90);
      ctx.shadowColor = `rgba(${r},${gg},${b},.8)`; ctx.shadowBlur = 4 * d * (W / 90);
      ctx.fillStyle = `rgba(${Math.min(255, r + 55 * d)},${Math.min(255, gg + 35 * d)},${Math.min(255, b + 35 * d)},${(0.35 + 0.6 * d).toFixed(3)})`;
      ctx.beginPath(); ctx.arc(p[0], p[1], rad, 0, 7); ctx.fill();
    }
    ctx.shadowBlur = 0;
  }
}
const dotViz = new NeuralViz(g("dot"), { n: 9, edgeD2: 1.1, lineW: 1, fps: 24, pulses: false });
const emblemViz = new NeuralViz(g("emblem"), { n: 34, edgeD2: 0.42, lineW: 1.1, fps: 30, pulses: true });
dotViz.start(); emblemViz.start();
// One shared "presence" state feeding both canvases (voice owns it while active, like the orb).
function setUiState(s) { dotViz.setState(s); emblemViz.setState(s); }

// --- voice HUD + mic button ---------------------------------------------------------------
function setMic(on, live) {
  micbtn.classList.toggle("on", !!on);
  micbtn.classList.toggle("live", !!live);
  micbtn.setAttribute("aria-pressed", on ? "true" : "false");
  micbtn.title = on ? "Voice on — listening for “hey Helios” (click to turn off)"
                    : "Voice off (click to turn on)";
}

// --- YOLO (all-permissions) toggle --------------------------------------------------------
function setYolo(on) {
  yolobtn.classList.toggle("on", !!on);
  yolobtn.setAttribute("aria-pressed", on ? "true" : "false");
  if (yolobadge) yolobadge.classList.toggle("hidden", !on);
  yolobtn.title = on ? "YOLO mode ON — all permissions auto-approved this chat (click to turn off)"
                     : "YOLO mode — auto-approve all permissions for this chat";
}
function handleVoice(data) {
  const state = (data && data.state) || "idle";
  if (state === "off" || state === "disabled") {
    setMic(false, false); hideVoiceHud(0); voiceActive = false; setUiState(busy ? "thinking" : "idle"); return;
  }
  setMic(true, state === "listening" || state === "speaking" || state === "dictation");
  // The recognized command becomes a user bubble so the conversation reads naturally.
  if (data && data.transcript && data.transcript !== voiceShown) {
    voiceShown = data.transcript;
    bubble("user", null).textContent = data.transcript;
    current = null;
  }
  if (typeof (data && data.level) === "number") { dotViz.setLevel(data.level); emblemViz.setLevel(data.level); }
  if (state === "idle") { hideVoiceHud(900); setUiState(busy ? "thinking" : "idle"); return; }
  // A live/active state: show the HUD + let the presence canvases mirror it.
  clearTimeout(voiceHideTimer);
  voiceActive = true;
  if (VIZ_STATES[state]) setUiState(state);
  const prev = voicehud.dataset.state;
  voicehud.dataset.state = state;
  vstate.textContent = VSTATE_LABEL[state] || state;
  if (state === "listening" && prev !== "listening") { vtext.textContent = ""; vtext.classList.remove("live"); }
  // Live transcript: only touch the text when we actually have some, so a level-only VU event
  // (12Hz) doesn't wipe the partial. `partial` updates in real time; `transcript` is the final.
  if (data && (data.partial != null || data.transcript != null)) {
    const live = data.partial || "", said = data.transcript || "";
    vtext.textContent = live ? (live.length > 70 ? "…" + live.slice(-70) : live) : said;
    vtext.classList.toggle("live", !!live);
  }
  // VU meter: drive the equalizer bars from the live audio level; otherwise let them animate.
  if (typeof (data && data.level) === "number") setVU(data.level);
  else if (state === "thinking") clearVU();
  voicehud.classList.remove("hidden");
}
const EQW = [0.55, 0.85, 1.0, 0.8, 0.6];   // per-bar weighting (center taller)
function setVU(level) {
  voicehud.classList.add("reactive");       // CSS pauses the keyframe animation
  const bars = voicehud.querySelectorAll(".eq i");
  bars.forEach((b, i) => {
    const jit = 0.82 + Math.random() * 0.36;
    const h = Math.max(0.16, Math.min(1, level * 2.6 * (EQW[i] || 0.7) * jit));
    b.style.transform = "scaleY(" + h.toFixed(3) + ")";
  });
}
function clearVU() {
  voicehud.classList.remove("reactive");
  voicehud.querySelectorAll(".eq i").forEach(b => { b.style.transform = ""; });
}
function hideVoiceHud(delay) {
  clearTimeout(voiceHideTimer);
  voiceHideTimer = setTimeout(() => {
    voicehud.classList.add("hidden");
    voiceActive = false;
    clearVU();
    if (!busy) banner.classList.add("hidden");
  }, delay);
}

function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  // Sending while a turn is still running = STEER it: Helios acknowledges, preempts the live turn,
  // and folds the addition into a fresh resumed turn (see brain.run_turn(steer=True)).
  const steer = busy;
  bubble("user", null).textContent = text;
  input.value = ""; input.style.height = "auto";
  // one-shot capsule ripple — the composer "hands the thought over"
  composer.classList.remove("sent"); void composer.offsetWidth; composer.classList.add("sent");
  if (steer) {
    chip("✚ added — folding that in…");   // the acknowledgment; the new merged turn streams next
  } else {
    current = null;
    setBusy(true, "Thinking…");
    if (!voiceActive) setUiState("thinking");
  }
  jfetch("/message", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, steer }) })
    .then(r => { if (!r.ok) throw new Error("server said " + r.status); })
    .catch(err => {
      if (!steer) { setBusy(false); setUiState("idle"); }
      bubble("error", "Helios").textContent =
        "Couldn't reach Helios (" + ((err && err.message) || "send failed") +
        "). Reload the dashboard and try again.";
    });
}

/* ==========================================================================
   Live tool timeline — visible thought. Every `tool` SSE event lands as a
   friendly row under the streaming reply ("Reading a file…"); when the turn
   finishes the strip collapses to a quiet "N steps · Xs" chip (click to
   re-expand). Replaces the old per-tool chat chips + "Acting…" banner text.
   ========================================================================== */
let act = null;   // { el, rows, sum, t0, count }
const ACT_ICONS = {
  file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  shell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l6-5-6-5"/><path d="M12 19h8"/></svg>',
  web: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  computer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4l7.5 18 2.6-7.9L22 11.5z"/></svg>',
  helios: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3.4"/><circle cx="12" cy="12" r="8.5" opacity=".45"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/></svg>',
};
const ACT_LABELS = {
  read: ["file", "Reading a file"], write: ["file", "Writing a file"], edit: ["file", "Editing a file"],
  glob: ["file", "Finding files"], grep: ["file", "Searching code"], notebookedit: ["file", "Editing a notebook"],
  bash: ["shell", "Running a command"], run_shell: ["shell", "Running a command"],
  powershell: ["shell", "Running a command"],
  webfetch: ["web", "Fetching a page"], web_fetch: ["web", "Fetching a page"],
  websearch: ["web", "Searching the web"], web_search: ["web", "Searching the web"],
  get_window_state: ["computer", "Reading the screen"], screenshot: ["computer", "Taking a screenshot"],
  click_element: ["computer", "Clicking"], type_text_in: ["computer", "Typing"],
  press_key: ["computer", "Pressing keys"], hotkey: ["computer", "Pressing keys"],
  scroll: ["computer", "Scrolling"], open_app: ["computer", "Opening an app"],
  task: ["helios", "Delegating to an agent"], todowrite: ["helios", "Planning"],
  toolsearch: ["helios", "Looking up a tool"],
};
function friendlyTool(name) {
  const raw = String(name || "a tool");
  const mcp = raw.match(/^mcp__([a-z0-9_-]+)__(.+)$/i);
  const server = mcp ? mcp[1] : "", short = (mcp ? mcp[2] : raw).toLowerCase();
  if (ACT_LABELS[short]) return ACT_LABELS[short];
  if (server === "computer") return ["computer", short.replace(/_/g, " ")];
  if (server === "helios") return ["helios", short.replace(/_/g, " ")];
  return ["spark", short.replace(/_/g, " ")];
}
function ensureAct() {
  if (act) return act;
  const el = document.createElement("div");
  el.className = "turnact";
  const rows = document.createElement("div"); rows.className = "arows"; el.appendChild(rows);
  const sum = document.createElement("button"); sum.className = "asum"; el.appendChild(sum);
  sum.addEventListener("click", () => el.classList.toggle("open"));
  // Ride inside the streaming reply bubble when there is one; float standalone otherwise.
  if (current && current.parentElement) current.parentElement.appendChild(el);
  else { el.classList.add("bare"); if (emptyEl) emptyEl.classList.add("hidden"); chat.appendChild(el); }
  act = { el, rows, sum, t0: performance.now(), count: 0 };
  return act;
}
function actRow(name) {
  const a = ensureAct();
  const prev = a.rows.lastElementChild; if (prev) prev.classList.remove("live");
  const [ico, label] = friendlyTool(name);
  const secs = (performance.now() - a.t0) / 1000;
  const row = document.createElement("div");
  row.className = "arow live";
  row.innerHTML = `<span class="aico">${ACT_ICONS[ico] || ACT_ICONS.spark}</span>` +
    `<span class="alab">${esc(label)}</span>` +
    `<span class="atime">${secs < 1 ? "" : secs.toFixed(0) + "s"}</span>`;
  a.rows.appendChild(row);
  a.count++;
  if (atBottom()) scroll();
}
function finishAct() {
  if (!act) return;
  const a = act; act = null;
  const last = a.rows.lastElementChild; if (last) last.classList.remove("live");
  if (!a.count) { a.el.remove(); return; }
  const secs = ((performance.now() - a.t0) / 1000).toFixed(secsFmt(a.t0));
  a.sum.innerHTML = `<span class="aico">${ACT_ICONS.spark}</span>` +
    `${a.count} step${a.count === 1 ? "" : "s"} · ${secs}s`;
  a.el.classList.add("done");
}
function secsFmt(t0) { return (performance.now() - t0) / 1000 >= 10 ? 0 : 1; }

// --- events from server ---
const ev = new EventSource("/events?token=" + encodeURIComponent(TOKEN));
// If the stream drops mid-turn, EventSource auto-reconnects — but don't leave the composer
// stuck disabled on "Thinking…". If it's still not OPEN after a grace period, re-enable.
ev.onerror = () => {
  clearTimeout(ev._recover);
  ev._recover = setTimeout(() => {
    if (busy && ev.readyState !== 1) { setBusy(false); setUiState("idle"); }
  }, 6000);
};
ev.onopen = () => clearTimeout(ev._recover);
ev.onmessage = (e) => {
  let msg; try { msg = JSON.parse(e.data); } catch { return; }
  const { kind, data } = msg;
  if (kind === "model") {
    modelTag.textContent = data.model || "—";
    finishAct();                       // a fresh turn: close out any straggler strip
    current = bubble("helios", "Helios"); current.dataset.raw = "";
    setBusy(true, "Thinking…");
    if (!voiceActive) setUiState("thinking");
  } else if (kind === "token") {
    if (!current) { current = bubble("helios", "Helios"); current.dataset.raw = ""; }
    current.dataset.raw += data;
    current.innerHTML = render(current.dataset.raw);
    if (atBottom()) scroll();
  } else if (kind === "tool") {
    actRow(data && data.name);
    setBusy(true, "Working…");
    if (!voiceActive) setUiState("acting");
  } else if (kind === "status") {
    chip(typeof data === "string" ? data : JSON.stringify(data));
  } else if (kind === "memory") {
    const el = document.createElement("div"); el.className = "memnote"; el.textContent = "🜔 " + data;
    chat.appendChild(el); if (atBottom()) scroll();
  } else if (kind === "done") {
    if (data.text) {
      if (!current) current = bubble("helios", "Helios");
      current.innerHTML = render(data.text);
    }
    finishAct();
    current = null; setBusy(false);
    if (!voiceActive) setUiState("idle");
    scroll();
  } else if (kind === "error") {
    bubble("error", "Helios").innerHTML = render(typeof data === "string" ? data : JSON.stringify(data));
    finishAct();
    current = null; setBusy(false);
    if (!voiceActive) setUiState("error");
    clearTimeout(ev._errCalm); ev._errCalm = setTimeout(() => { if (!busy) setUiState("idle"); }, 1800);
  } else if (kind === "permission") {
    pendingPermId = data.id;
    modalSummary.textContent = data.summary || data.tool;
    modal.classList.remove("hidden");
  } else if (kind === "mission") {
    if (typeof onMissionEvent === "function") onMissionEvent(data);
  } else if (kind === "workflow") {
    if (typeof onWorkflowEvent === "function") onWorkflowEvent(data);
  } else if (kind === "voice") {
    handleVoice(data);
  } else if (kind === "usage") {
    handleUsage(data);
  } else if (kind === "yolo") {
    setYolo(data && data.on);   // server-driven (toggle elsewhere, or reset on new-chat/panic)
  } else if (kind === "model3d") {
    model3dCard(data);          // tool `show` -> chat card with render PNG + viewer button
  }
};

/* ------------------------------------------------------------------------
   Session stats — running total in the title bar; click for the instrument
   readout: totals, est. cost, uptime + a per-turn mini-log.
   ------------------------------------------------------------------------ */
let sessionTokens = 0, sessionCost = 0, sessionIn = 0, sessionOut = 0;
const turnLog = [];                    // {model, tin, tout, ms}
const SESSION_T0 = Date.now();
const statsEl = g("stats"), statspop = g("statspop");
function fmtTok(n) { return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : "" + (n || 0); }
function handleUsage(d) {
  if (!d) return;
  const tin = d.in || 0, tout = d.out || 0, ms = d.ms || 0;
  sessionTokens += tin + tout; sessionIn += tin; sessionOut += tout;
  if (typeof d.cost === "number") sessionCost += d.cost;
  turnLog.push({ model: d.model || "?", tin, tout, ms });
  // Per-turn footer attached to the just-finished reply bubble (the tool detail lives in the
  // timeline strip above it now).
  const host = (current && current.parentElement) ? current.parentElement : chat;
  const f = document.createElement("div");
  f.className = "usage";
  let line = (d.model || "") + (tin || tout ? ` · ${fmtTok(tin)}→${fmtTok(tout)} tok` : "");
  if (ms) line += ` · ${(ms / 1000).toFixed(1)}s`;
  const meta = document.createElement("div"); meta.className = "umeta"; meta.textContent = line;
  f.appendChild(meta);
  host.appendChild(f);
  if (atBottom()) scroll();
  // Session running total in the title bar.
  statsEl.textContent = "Σ " + fmtTok(sessionTokens) + " tok" + (sessionCost ? ` · ~$${sessionCost.toFixed(3)}` : "");
  if (!statspop.classList.contains("hidden")) renderStatsPop();
}
function renderStatsPop() {
  const up = Math.max(1, Math.round((Date.now() - SESSION_T0) / 60000));
  const rows = turnLog.slice(-10).reverse().map(t =>
    `<div class="sturn"><span class="sm">${esc(t.model)}</span>` +
    `<span>${fmtTok(t.tin)}→${fmtTok(t.tout)}</span>` +
    `<span class="st">${t.ms ? (t.ms / 1000).toFixed(1) + "s" : ""}</span></div>`).join("");
  statspop.innerHTML =
    `<h4>This session</h4>` +
    `<div class="srow"><span>Tokens</span><b>${sessionTokens.toLocaleString()} (${fmtTok(sessionIn)} in · ${fmtTok(sessionOut)} out)</b></div>` +
    (sessionCost ? `<div class="srow"><span>Est. cost</span><b>~$${sessionCost.toFixed(4)} <span class="muted small">(plan covers it)</span></b></div>` : "") +
    `<div class="srow"><span>Turns</span><b>${turnLog.length}</b></div>` +
    `<div class="srow"><span>Model</span><b>${esc(modelTag.textContent || "—")}</b></div>` +
    `<div class="srow"><span>Dashboard up</span><b>${up < 60 ? up + "m" : (up / 60).toFixed(1) + "h"}</b></div>` +
    (rows ? `<div class="sturns">${rows}</div>` : "");
}
statsEl.addEventListener("click", (e) => {
  e.stopPropagation();
  const show = statspop.classList.contains("hidden");
  statspop.classList.toggle("hidden", !show);
  statsEl.setAttribute("aria-expanded", show ? "true" : "false");
  if (show) renderStatsPop();
});
statspop.addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => { statspop.classList.add("hidden"); statsEl.setAttribute("aria-expanded", "false"); });

function respond(decision) {
  if (!pendingPermId) return;
  jfetch("/permission/respond", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: pendingPermId, decision }) });
  pendingPermId = null;
  modal.classList.add("hidden");
}

// --- wiring ---
send.addEventListener("click", sendMessage);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
input.addEventListener("input", () => {
  input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px";
});
g("approve").addEventListener("click", () => respond("allow"));
g("deny").addEventListener("click", () => respond("deny"));
g("panicbtn").addEventListener("click", () => {
  jfetch("/panic", { method: "POST" }); setBusy(false); setUiState("idle"); finishAct();
});
// Mic button = master voice on/off. Optimistically flip, then trust the server's answer.
micbtn.addEventListener("click", async () => {
  const turningOn = !micbtn.classList.contains("on");
  setMic(turningOn, false);
  try {
    const r = await (await jfetch("/voice/toggle", { method: "POST" })).json();
    setMic(!!r.running, false);
    if (!r.running) hideVoiceHud(0);
  } catch { setMic(!turningOn, false); }
});
// YOLO button = all-permissions-for-this-chat. Optimistic flip, then trust the server's answer.
yolobtn.addEventListener("click", async () => {
  const turningOn = !yolobtn.classList.contains("on");
  setYolo(turningOn);
  try {
    const r = await (await jfetch("/yolo", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: turningOn ? "on" : "off" }) })).json();
    setYolo(!!r.on);
  } catch { setYolo(!turningOn); }
});
g("newbtn").addEventListener("click", () => {
  jfetch("/new", { method: "POST" }); clearChat(); modelTag.textContent = "—"; setUiState("idle");
});

// --- resize the frameless window from any edge/corner -> /window/resize ---
// Each .rz zone resizes along its edge(s); the OPPOSITE edge stays put (fixx/fixy -> pywebview
// FixPoint, server-side). All math is in CSS px (= logical px), which pywebview scales to physical.
(() => {
  const zones = document.querySelectorAll("#resizeedges .rz");
  if (!zones.length) return;
  const MINW = 420, MINH = 520;
  let edges = "", active = false, sx = 0, sy = 0, sw = 0, sh = 0;
  let raf = 0, pendW = 0, pendH = 0, fixx = "left", fixy = "top";
  const flush = () => {
    raf = 0;
    jfetch("/window/resize", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ width: pendW, height: pendH, fixx, fixy }) }).catch(() => {});
  };
  zones.forEach(z => z.addEventListener("mousedown", (e) => {
    e.preventDefault();
    active = true; edges = z.dataset.edges || "se";
    sx = e.screenX; sy = e.screenY;
    sw = window.innerWidth; sh = window.innerHeight;   // frameless: content size ≈ window size
    document.body.style.userSelect = "none";
  }));
  window.addEventListener("mousemove", (e) => {
    if (!active) return;
    const dx = e.screenX - sx, dy = e.screenY - sy;
    let w = sw, h = sh; fixx = "left"; fixy = "top";   // default: anchor top-left (grow E/S)
    if (edges.includes("e")) { w = sw + dx; fixx = "left"; }
    if (edges.includes("w")) { w = sw - dx; fixx = "right"; }   // dragging W -> keep E edge fixed
    if (edges.includes("s")) { h = sh + dy; fixy = "top"; }
    if (edges.includes("n")) { h = sh - dy; fixy = "bottom"; }  // dragging N -> keep S edge fixed
    pendW = Math.max(MINW, Math.min(screen.availWidth, Math.round(w)));
    pendH = Math.max(MINH, Math.min(screen.availHeight, Math.round(h)));
    if (!raf) raf = requestAnimationFrame(flush);   // throttle to one resize per frame
  });
  window.addEventListener("mouseup", () => {
    if (!active) return;
    active = false; document.body.style.userSelect = "";
  });
})();
// Example prompts in the empty state: fill the composer (don't auto-send) and focus.
document.querySelectorAll("#empty .ex").forEach(b => b.addEventListener("click", () => {
  input.value = b.textContent; input.focus();
  input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px";
}));
// Time-aware greeting.
(() => {
  const h = new Date().getHours();
  const part = h < 5 ? "Good evening" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  const el = g("greet");
  if (el) el.textContent = `${part}, sir. How can I help?`;
})();
// Custom window controls (frameless window has no native title bar): minimize -> taskbar,
// close -> hide back to the orb. The native app drives the window via these endpoints.
g("minbtn").addEventListener("click", () => jfetch("/window/minimize", { method: "POST" }));
g("closebtn").addEventListener("click", () => jfetch("/window/close", { method: "POST" }));

// --- power menu (top-left): Sleep / Shut down ---
const powerbtn = g("powerbtn"), powermenu = g("powermenu");
let quitArmed = false, quitTimer = null;
function disarmQuit() {
  quitArmed = false; clearTimeout(quitTimer);
  g("pm_quit").classList.remove("armed");
  g("pm_quit_label").textContent = "Shut down";
}
function togglePower(show) {
  const open = show === undefined ? powermenu.classList.contains("hidden") : show;
  powermenu.classList.toggle("hidden", !open);
  powerbtn.setAttribute("aria-expanded", open ? "true" : "false");
  if (!open) disarmQuit();
}
powerbtn.addEventListener("click", (e) => { e.stopPropagation(); togglePower(); });
powermenu.addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => togglePower(false));
g("pm_sleep").addEventListener("click", () => {
  togglePower(false);
  jfetch("/sleep", { method: "POST" });   // hide dashboard + orb, go dormant (clap to wake)
});
g("pm_quit").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!quitArmed) {   // two-step: a full shutdown means manually relaunching, so confirm first
    quitArmed = true;
    g("pm_quit").classList.add("armed");
    g("pm_quit_label").textContent = "Click again to shut down";
    clearTimeout(quitTimer); quitTimer = setTimeout(disarmQuit, 3000);
    return;
  }
  jfetch("/quit", { method: "POST" });
  togglePower(false);
});

/* ==========================================================================
   The drawer — one right-hand slide-over hosting History / Missions /
   Workflows / Settings. openDrawer() swaps the .dview panes; #dback is a
   contextual back button each view can claim (wf detail→list, mission
   detail→list); closing stops any live polls.
   ========================================================================== */
const drawer = g("drawer"), scrim = g("scrim"), dtitle = g("dtitle"), dback = g("dback");
let drawerView = null;      // "histview" | "missionview" | "wfview" | "setview" | null
let drawerBackFn = null;
function setDrawerBack(fn) { drawerBackFn = fn || null; dback.classList.toggle("hidden", !fn); }
function setDrawerTitle(t) { dtitle.textContent = t; }
function openDrawer(view, title) {
  drawerView = view;
  ["histview", "missionview", "wfview", "setview", "modelview"].forEach(v =>
    g(v).classList.toggle("hidden", v !== view));
  setDrawerTitle(title);
  setDrawerBack(null);
  g("wfnew").classList.add("hidden");        // wf list view re-shows it via showWf('list')
  scrim.classList.remove("hidden");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
}
function closeDrawer() {
  if (!drawerView) return;
  drawerView = null;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  scrim.classList.add("hidden");
  stopMissionPoll(); stopWfPoll();
  if (window.HeliosViewer) HeliosViewer.pause();   // free the 3D model's GPU memory
  input.focus();
}
g("dclose").addEventListener("click", closeDrawer);
scrim.addEventListener("click", closeDrawer);
dback.addEventListener("click", () => { if (drawerBackFn) drawerBackFn(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawerView) closeDrawer(); });

// --- settings (drawer view) ---
g("setbtn").addEventListener("click", async () => {
  let s = {}; try { s = await (await jfetch("/settings")).json(); } catch {}
  const R = s.router || {}, P = s.proactive || {}, hk = s.hotkeys || {};
  g("s_router_auto").checked = !!R.auto;
  g("s_light").value = R.light || ""; g("s_medium").value = R.medium || ""; g("s_heavy").value = R.heavy || "";
  g("s_pro_enabled").checked = !!P.enabled;
  g("s_battery").value = P.battery_pct; g("s_disk").value = P.disk_gb; g("s_downloads").value = P.downloads_count;
  g("s_qs").value = P.quiet_start; g("s_qe").value = P.quiet_end;
  g("s_tone").value = s.tone || "";
  const V = s.voice || {};
  g("s_voice_enabled").checked = !!V.enabled;
  if (V.tts_voice) g("s_voice_name").value = V.tts_voice;
  g("s_voice_speed").value = V.tts_speed != null ? V.tts_speed : 1.0;
  g("s_wake_thresh").value = V.wake_threshold != null ? V.wake_threshold : 0.5;
  if (V.stt_model) g("s_stt_model").value = V.stt_model;
  g("s_followup").value = V.followup_sec != null ? V.followup_sec : 6;
  g("s_speak_all").checked = !!V.speak_all;
  g("s_barge_in").checked = !!V.barge_in;
  g("s_live_transcript").checked = V.live_transcript !== false;
  const O = s.orb || {};
  g("s_orb_engine").value = O.engine || "webview";
  g("s_orb_style").value = O.style || "bloom";
  g("s_orb_size").value = O.size != null ? O.size : 190;
  const ST = s.startup || {};
  g("s_start_hidden").checked = !!ST.hidden;
  g("s_clap_wake").checked = ST.wake_gesture === "double_clap";
  g("s_clap_sens").value = ST.clap_sensitivity != null ? ST.clap_sensitivity : 0.1;
  g("s_info").textContent = `v${s.version} · ${(s.allowlist || []).length} auto-approved tools · ` +
    `summon ${hk.summon || ""} · panic ${hk.panic || ""} · dictation ${V.dictation_hotkey || ""}` +
    `  (hotkey changes need a restart)`;
  openDrawer("setview", "Settings");
});
g("setclose").addEventListener("click", closeDrawer);
g("setsave").addEventListener("click", async () => {
  const num = id => { const v = parseFloat(g(id).value); return isNaN(v) ? undefined : v; };
  const changes = {
    "router.auto": g("s_router_auto").checked,
    "router.light": g("s_light").value.trim() || "haiku",
    "router.medium": g("s_medium").value.trim() || "sonnet",
    "router.heavy": g("s_heavy").value.trim() || "opus",
    "proactive.enabled": g("s_pro_enabled").checked,
  };
  for (const [id, key] of [["s_battery", "proactive.battery_pct"], ["s_disk", "proactive.disk_gb"],
    ["s_downloads", "proactive.downloads_count"], ["s_qs", "proactive.quiet_start"], ["s_qe", "proactive.quiet_end"]]) {
    const v = num(id); if (v !== undefined) changes[key] = v;
  }
  // Voice settings.
  changes["voice.enabled"] = g("s_voice_enabled").checked;
  changes["voice.tts_voice"] = g("s_voice_name").value;
  changes["voice.stt_model"] = g("s_stt_model").value;
  changes["voice.speak_all"] = g("s_speak_all").checked;
  changes["voice.barge_in"] = g("s_barge_in").checked;
  changes["voice.live_transcript"] = g("s_live_transcript").checked;
  for (const [id, key] of [["s_voice_speed", "voice.tts_speed"],
    ["s_wake_thresh", "voice.wake_threshold"], ["s_followup", "voice.followup_sec"]]) {
    const v = num(id); if (v !== undefined) changes[key] = v;
  }
  // Orb (engine/size restart the orb process server-side; style reloads its page).
  changes["orb.engine"] = g("s_orb_engine").value;
  changes["orb.style"] = g("s_orb_style").value;
  { const v = num("s_orb_size"); if (v !== undefined) changes["orb.size"] = Math.max(120, Math.min(500, Math.round(v))); }
  // Startup & wake.
  changes["startup.hidden"] = g("s_start_hidden").checked;
  changes["startup.wake_gesture"] = g("s_clap_wake").checked ? "double_clap" : "none";
  { const v = num("s_clap_sens"); if (v !== undefined) changes["startup.clap_sensitivity"] = v; }
  await jfetch("/settings", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ changes, tone: g("s_tone").value.trim() }) });
  setMic(g("s_voice_enabled").checked, false);
  closeDrawer();
});

// --- conversation history (drawer view) ---
const histlist = g("histlist");
g("histbtn").addEventListener("click", async () => {
  let items = [];
  try { items = (await (await jfetch("/conversations")).json()).items || []; } catch {}
  histlist.innerHTML = items.length ? "" : '<div class="muted">No saved conversations yet.</div>';
  for (const c of items) {
    const el = document.createElement("div");
    el.className = "histitem";
    el.innerHTML = `<div class="ht">${esc(c.title || "(untitled)")}</div>` +
      `<div class="hw">${(c.updated_at || "").replace("T", " ").slice(0, 16)}</div>`;
    el.addEventListener("click", () => loadConversation(c.id));
    histlist.appendChild(el);
  }
  openDrawer("histview", "Past conversations");
});
async function loadConversation(id) {
  let msgs = [];
  try { msgs = (await (await jfetch("/conversation?id=" + encodeURIComponent(id))).json()).messages || []; } catch {}
  clearChat();
  for (const m of msgs) {
    const b = bubble(m.role === "user" ? "user" : "helios", m.role === "user" ? null : "Helios");
    if (m.role === "user") b.textContent = m.content; else b.innerHTML = render(m.content || "");
  }
  jfetch("/conversation/load", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }) });
  closeDrawer(); scroll();
}

// --- agent missions (drawer view, live multi-agent) ---
const missionlist = g("missionlist");
const missiondetail = g("missiondetail");
let openMission = null, missionTimer = null;

g("missionbtn").addEventListener("click", () => { openDrawer("missionview", "Agent missions"); openMissionList(); });

function stopMissionPoll() { if (missionTimer) { clearInterval(missionTimer); missionTimer = null; } }
function startMissionPoll() { stopMissionPoll(); missionTimer = setInterval(refreshMissionDetail, 3000); }

// SSE "mission" events refresh whatever's on screen if the panel is open
function onMissionEvent() {
  if (drawerView !== "missionview") return;
  if (openMission) refreshMissionDetail(); else openMissionList();
}

async function openMissionList() {
  openMission = null; stopMissionPoll(); missiondetail.innerHTML = "";
  setDrawerBack(null); setDrawerTitle("Agent missions");
  let items = [];
  try { items = (await (await jfetch("/missions")).json()).items || []; } catch {}
  missionlist.style.display = "";
  missionlist.innerHTML = items.length ? ""
    : '<div class="muted">No missions yet. Give Helios a big multi-part task and it can launch a team.</div>';
  items.forEach(m => {
    const el = document.createElement("div");
    el.className = "histitem";
    el.innerHTML = `<b>#${m.id}</b> <span class="mstatus ${esc(m.status)}">${esc(m.status)}</span> <span>${esc((m.goal || "").slice(0, 90))}</span>`;
    el.addEventListener("click", () => openMissionDetail(m.id));
    missionlist.appendChild(el);
  });
}

async function openMissionDetail(id) {
  openMission = id;
  setDrawerBack(() => { stopMissionPoll(); openMissionList(); });
  await refreshMissionDetail(); startMissionPoll();
}

async function refreshMissionDetail() {
  if (!openMission) return;
  let d = null;
  try { d = await (await jfetch("/mission?id=" + openMission)).json(); } catch {}
  if (!d || !d.mission) return;
  const m = d.mission;
  missionlist.style.display = "none";
  setDrawerTitle("Mission #" + m.id);
  const agentsHtml = (d.agents || []).map(a =>
    `<div class="magent"><span class="mstatus ${esc(a.status)}">${esc(a.status)}</span> <b>${esc(a.role)}</b> #${a.id}` +
    `${a.depth ? ` <span class="muted small">depth ${a.depth}</span>` : ""} — ${esc((a.task || "").slice(0, 90))}</div>`).join("");
  const logHtml = (d.log || []).map(e =>
    `<div class="mlog"><span class="muted small">${esc(e.sender_role || "?")}#${e.sender_id == null ? "-" : e.sender_id} · ${esc(e.kind)}</span><br>${esc((e.content || "").slice(0, 500))}</div>`).join("");
  missiondetail.innerHTML =
    `<div style="display:flex;align-items:center;gap:9px"><span class="mstatus ${esc(m.status)}">${esc(m.status)}</span>` +
    `<span class="muted small">${esc(m.goal || "")}</span></div>` +
    (m.result ? `<div class="mresult"><b>Result</b><div>${render(m.result)}</div></div>` : "") +
    `<div class="msection">Team (${(d.agents || []).length})</div>${agentsHtml || '<div class="muted small">no agents yet</div>'}` +
    `<div class="msection">Blackboard</div>${logHtml || '<div class="muted small">empty</div>'}`;
}

// --- 3D models (drawer view: three.js viewer, ui/viewer.js; ported from Helios-main) ---
const modellist = g("modellist"), modelstage = g("modelstage");
function fmtSize(n) {
  if (!n) return "";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}
function showModel(item) {
  modelstage.classList.remove("hidden");
  g("modelname").textContent = item.name || "";
  g("modelstats").textContent = "";
  if (window.HeliosViewer) HeliosViewer.open(item.url, item.name);
  else g("modelstats").textContent = "viewer failed to load";
}
async function openModels(autoItem) {
  openDrawer("modelview", "3D Models");
  if (!autoItem) modelstage.classList.add("hidden");
  let items = [];
  try { items = (await (await jfetch("/models")).json()).items || []; } catch {}
  modellist.innerHTML = items.length ? "" :
    '<div class="muted">No models yet — ask Helios to design one.</div>';
  for (const m of items) {
    const el = document.createElement("div");
    el.className = "histitem";
    el.innerHTML = `<div class="ht">${esc(m.name || "model")}</div>` +
      `<div class="hw">${fmtSize(m.size)}${m.mtime ? " · " + new Date(m.mtime * 1000).toLocaleDateString() : ""}</div>`;
    el.addEventListener("click", () => showModel(m));
    modellist.appendChild(el);
  }
  if (autoItem) showModel(autoItem);
}
g("modelsbtn").addEventListener("click", () => openModels());
g("wireframebtn").addEventListener("click", () => { if (window.HeliosViewer) HeliosViewer.wireframe(); });
// Chat card for a `model3d` SSE frame (tool `show`): render PNG (data URI) + stats + an
// Open-in-viewer button. Never summons the window.
function model3dCard(d) {
  if (emptyEl) emptyEl.classList.add("hidden");
  const el = document.createElement("div");
  el.className = "msg card m3dcard";
  const t = document.createElement("div"); t.className = "ctitle";
  t.textContent = "3D MODEL — " + (d.name || "model");
  el.appendChild(t);
  if (typeof d.png === "string" && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(d.png)) {
    const img = document.createElement("img");
    img.className = "m3d"; img.alt = d.name || "model preview"; img.src = d.png;
    el.appendChild(img);
  }
  const meta = document.createElement("div"); meta.className = "muted small";
  meta.textContent = `${fmtTok(d.verts || 0)} verts · ${fmtTok(d.faces || 0)} faces` +
    (fmtSize(d.size) ? ` · ${fmtSize(d.size)}` : "");
  el.appendChild(meta);
  const btn = document.createElement("button");
  btn.className = "primary m3dopen"; btn.textContent = "Open in 3D viewer";
  btn.addEventListener("click", () => openModels({ url: d.url, name: d.name }));
  el.appendChild(btn);
  chat.appendChild(el); if (atBottom()) scroll();
}

// --- workflows (drawer view: automations = a trigger + ordered steps) ---
const wflistview = g("wflistview"), wfdetail = g("wfdetail"), wfeditor = g("wfeditor");
let wfPollTimer = null, wfOpenId = null;             // id of the workflow whose detail is open
let wfSteps = [], wfStepSeq = 0, wfEditId = null;    // editor working state
const WF_TYPES = ["agent", "brain", "notify", "http", "delay", "condition"];
const WF_AGENTS = ["auto", "researcher", "operator", "coder", "organizer", "writer"];

g("workflowbtn").addEventListener("click", () => { openDrawer("wfview", "Workflows"); openWfList(); });
g("wfnew").addEventListener("click", () => openWfEditor(null, null));

function stopWfPoll() { if (wfPollTimer) { clearInterval(wfPollTimer); wfPollTimer = null; } }
function startWfPoll() { stopWfPoll(); wfPollTimer = setInterval(refreshWfDetail, 3000); }
function showWf(view) {   // 'list' | 'detail' | 'editor'
  wflistview.classList.toggle("hidden", view !== "list");
  wfdetail.classList.toggle("hidden", view !== "detail");
  wfeditor.classList.toggle("hidden", view !== "editor");
  g("wfnew").classList.toggle("hidden", view !== "list");
  setDrawerTitle(view === "editor" ? (wfEditId != null ? "Edit workflow" : "New workflow")
    : view === "detail" ? "Workflow" : "Workflows");
  if (view === "list") setDrawerBack(null);
  else if (view === "detail") setDrawerBack(() => { stopWfPoll(); openWfList(); });
  else setDrawerBack(() => (wfEditId != null ? openWfDetail(wfEditId) : openWfList()));
}
// SSE "workflow" events refresh whatever workflow view is on screen.
function onWorkflowEvent() {
  if (drawerView !== "wfview") return;
  if (!wfdetail.classList.contains("hidden") && wfOpenId != null) refreshWfDetail();
  else if (!wflistview.classList.contains("hidden")) openWfList();
}

// ---- list ----
async function openWfList() {
  wfOpenId = null; stopWfPoll(); showWf("list");
  let items = [];
  try { items = (await (await jfetch("/workflows")).json()).items || []; } catch {}
  const list = g("wflist");
  if (!items.length) {
    list.innerHTML = '<div class="muted small" style="padding:12px 2px">No workflows yet. ' +
      'Click <b>New workflow</b> above — or just say <b>“Helios, make a workflow that…”</b>.</div>';
    return;
  }
  list.innerHTML = "";
  items.forEach(w => {
    const el = document.createElement("div");
    el.className = "histitem wfitem";
    const trig = w.schedule ? `<span class="wftag">${esc(w.schedule)}</span>` : '<span class="wftag manual">manual</span>';
    const next = (w.schedule && w.enabled && w.next_run)
      ? `<span class="dotsep">·</span><span>next ${esc(w.next_run.replace("T", " ").slice(0, 16))}</span>` : "";
    el.innerHTML =
      `<div class="wfmain"><div class="wfname">${esc(w.name)}</div><div class="wfmeta">${trig}${next}</div></div>` +
      `<button class="wfsw ${w.enabled ? "on" : ""}" title="${w.enabled ? "Enabled" : "Disabled"}" aria-label="Toggle enabled" aria-pressed="${w.enabled ? "true" : "false"}"></button>` +
      `<button class="iconbtn wficon" data-act="run" title="Run now" aria-label="Run now"><svg viewBox="0 0 24 24" width="16" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg></button>`;
    el.querySelector(".wfmain").addEventListener("click", () => openWfDetail(w.id));
    el.querySelector(".wfsw").addEventListener("click", async (e) => {
      e.stopPropagation();
      const btn = e.currentTarget, on = !btn.classList.contains("on");
      btn.classList.toggle("on", on); btn.setAttribute("aria-pressed", on ? "true" : "false");
      try {
        await jfetch("/workflow/enabled", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: w.id, enabled: on }) });
      } catch { btn.classList.toggle("on", !on); }
    });
    el.querySelector('[data-act=run]').addEventListener("click", (e) => { e.stopPropagation(); runWf(w.id, true); });
    list.appendChild(el);
  });
}

async function runWf(id, thenOpen) {
  try { await jfetch("/workflow/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) }); }
  catch {}
  if (thenOpen) openWfDetail(id);   // jump to the detail view to watch it run live
}

// ---- detail (steps + run history) ----
async function openWfDetail(id) { wfOpenId = id; showWf("detail"); await refreshWfDetail(); startWfPoll(); }

function stepSummary(s) {
  switch (s.type) {
    case "agent": return `${s.agent || "auto"} — ${(s.prompt || "").slice(0, 90)}`;
    case "brain": return (s.prompt || "").slice(0, 100);
    case "notify": return `notify — ${(s.message || "").slice(0, 90)}`;
    case "http": return `${s.method || "GET"} ${(s.url || "").slice(0, 80)}`;
    case "delay": return `wait ${s.seconds || 0}s`;
    case "condition": return `if ${s.source || ""} ${s.op || "not_empty"} ${s.value || ""}`;
    default: return s.type || "";
  }
}
function wfRunHtml(r) {
  let steps = []; try { steps = JSON.parse(r.log || "[]"); } catch {}
  const chips = steps.map(s => `<span class="rstep ${esc(s.status || "")}">${esc(s.id)}</span>`).join("");
  const t = (r.started_at || "").replace("T", " ").slice(0, 16);
  return `<div class="wfrun"><div class="rhead"><span class="mstatus ${esc(r.status)}">${esc(r.status)}</span>` +
    `<span>${esc(r.trigger || "")}</span><span class="rtime">${esc(t)}</span></div>` +
    (chips ? `<div class="rsteps">${chips}</div>` : "") + `</div>`;
}
async function refreshWfDetail() {
  if (wfOpenId == null) return;
  let d = null;
  try { d = await (await jfetch("/workflow?id=" + wfOpenId)).json(); } catch {}
  if (!d || !d.workflow) return;
  const w = d.workflow, spec = d.spec || {}, runs = d.runs || [], steps = spec.steps || [];
  setDrawerTitle(w.name || "Workflow");
  const trig = w.schedule ? `<span class="wftag">${esc(w.schedule)}</span>` : '<span class="wftag manual">manual</span>';
  const stepList = steps.map((s, i) =>
    `<div class="wfstep ro t-${esc(WF_TYPES.includes(s.type) ? s.type : "brain")}">` +
    `<div class="stephead"><span class="stepbadge">${i + 1}</span>` +
    `<span class="stepid">${esc(s.id)}</span><span class="wftag manual" style="margin-left:auto">${esc(s.type)}</span></div>` +
    `<div class="muted small" style="margin-top:5px">${esc(stepSummary(s))}</div></div>`).join("");
  wfdetail.innerHTML =
    `<div class="wfdetailhead"><span class="wfname">${esc(w.name)}</span>${trig}<span class="wfactions">` +
      `<button class="iconbtn wficon" id="wfdrun" title="Run now" aria-label="Run now"><svg viewBox="0 0 24 24" width="16" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg></button>` +
      `<button class="ghost" id="wfdedit">Edit</button>` +
      `<button class="iconbtn wficon danger" id="wfddel" title="Delete workflow" aria-label="Delete workflow"><svg viewBox="0 0 24 24" width="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V5h6v2M7 7l1 13h8l1-13"/></svg></button>` +
    `</span></div>` +
    `<div class="msection">Steps (${steps.length})</div><div class="wfsteps">${stepList || '<div class="muted small">no steps</div>'}</div>` +
    `<div class="msection">Recent runs</div><div class="wfruns">${runs.length ? runs.map(wfRunHtml).join("") : '<div class="muted small">No runs yet — hit ▶ to run it now.</div>'}</div>`;
  g("wfdrun").addEventListener("click", () => runWf(w.id, false));
  g("wfdedit").addEventListener("click", () => openWfEditor(w.id, spec));
  const del = g("wfddel");
  del.addEventListener("click", async () => {
    if (del.dataset.armed !== "1") {   // two-click confirm (destructive)
      del.dataset.armed = "1"; del.title = "Click again to delete";
      setTimeout(() => { del.dataset.armed = "0"; del.title = "Delete workflow"; }, 3000);
      return;
    }
    try { await jfetch("/workflow/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: w.id }) }); } catch {}
    stopWfPoll(); openWfList();
  });
}

// ---- editor ----
function newStep(type) { wfStepSeq++; return { id: "s" + wfStepSeq, type }; }
function openWfEditor(id, spec) {
  wfEditId = id != null ? id : null;
  stopWfPoll();
  const s = spec || { name: "", trigger: { type: "manual" }, steps: [] };
  wfSteps = (s.steps || []).map(st => Object.assign({}, st));
  wfStepSeq = 0;
  wfSteps.forEach(st => { const n = parseInt(String(st.id || "").replace(/\D/g, ""), 10) || 0; if (n > wfStepSeq) wfStepSeq = n; });
  if (!wfSteps.length) wfSteps.push(newStep("brain"));
  const trig = s.trigger || { type: "manual" }, isSched = trig.type === "schedule";
  showWf("editor");
  wfeditor.innerHTML =
    `<div class="wfform">` +
    `<div class="ffield"><label>Name</label><input id="wfname" class="fbig" placeholder="Morning brief" value="${esc(s.name || "")}"></div>` +
    `<div class="ffield"><label>Trigger</label>` +
      `<div class="seg" id="wftrigseg">` +
        `<button type="button" data-v="manual" class="${isSched ? "" : "on"}">Manual — run on demand</button>` +
        `<button type="button" data-v="schedule" class="${isSched ? "on" : ""}">On a schedule</button></div>` +
      `<input type="hidden" id="wftrigtype" value="${isSched ? "schedule" : "manual"}">` +
      `<input id="wfsched" class="fsched" placeholder="daily 08:00 · every 30m · hourly" value="${esc(isSched ? (trig.schedule || "") : "")}" style="${isSched ? "" : "display:none"}"></div>` +
    `<div class="wfvarhint">Steps run top to bottom. Reference an earlier step's result with <code>{{stepid.output}}</code> — each step shows its id.</div>` +
    `<div id="wfstepbox" class="wfsteps"></div>` +
    `<button class="wfadd" id="wfaddstep"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg> Add step</button>` +
    `<div class="wferr small" id="wferr"></div>` +
    `<div class="row"><button class="ghost" id="wfecancel">Cancel</button><button class="primary" id="wfsave">Save workflow</button></div>` +
    `</div>`;
  // Segmented trigger control: buttons flip the hidden #wftrigtype (saveWf reads it) + the
  // schedule input's visibility.
  g("wftrigseg").querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    g("wftrigseg").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    g("wftrigtype").value = b.dataset.v;
    g("wfsched").style.display = b.dataset.v === "schedule" ? "" : "none";
    if (b.dataset.v === "schedule") g("wfsched").focus();
  }));
  const back = () => (wfEditId != null ? openWfDetail(wfEditId) : openWfList());
  g("wfecancel").addEventListener("click", back);
  g("wfaddstep").addEventListener("click", () => { syncSteps(); wfSteps.push(newStep("brain")); renderSteps(); });
  g("wfsave").addEventListener("click", saveWf);
  renderSteps();
}

function stepFields(st) {
  const ta = (f, label, val) => `<div class="wffield"><label>${label}</label><textarea data-field="${f}" placeholder="${esc(label)}">${esc(val || "")}</textarea></div>`;
  const inp = (f, label, val) => `<div class="wffield"><label>${label}</label><input data-field="${f}" placeholder="${esc(label)}" value="${esc(val || "")}"></div>`;
  const sel = (f, label, opts, cur) => `<div class="wffield two"><label>${label}</label><select data-field="${f}">` +
    opts.map(o => `<option value="${o}"${o === cur ? " selected" : ""}>${o}</option>`).join("") + `</select></div>`;
  switch (st.type) {
    case "agent": return sel("agent", "Specialist", WF_AGENTS, st.agent || "auto") + ta("prompt", "Instruction for the agent", st.prompt);
    case "brain": return ta("prompt", "What should Helios do or answer?", st.prompt);
    case "notify": return inp("title", "Title", st.title) + ta("message", "Message (supports {{step.output}})", st.message);
    case "http": return sel("method", "Method", ["GET", "POST"], st.method || "GET") + inp("url", "https://…", st.url) + ta("body", "Body (optional)", st.body);
    case "delay": return `<div class="wffield two"><label>Seconds</label><input data-field="seconds" type="number" min="0" max="3600" value="${esc(String(st.seconds != null ? st.seconds : 5))}"></div>`;
    case "condition": return inp("source", "Value to check (e.g. {{step.output}})", st.source) +
      `<div class="wffield two"><label>Test</label><select data-field="op">` +
      [["not_empty", "is not empty"], ["contains", "contains"], ["equals", "equals"]].map(([v, t]) =>
        `<option value="${v}"${(st.op || "not_empty") === v ? " selected" : ""}>${t}</option>`).join("") +
      `</select></div>` + inp("value", "Compare value (for contains / equals)", st.value);
    default: return "";
  }
}
function stepCard(st, i) {
  const el = document.createElement("div");
  el.className = "wfstep t-" + (WF_TYPES.includes(st.type) ? st.type : "brain");
  el.innerHTML =
    `<div class="stephead"><span class="stepbadge">${i + 1}</span><span class="stepid">${esc(st.id)}</span>` +
    `<input type="hidden" data-field="id" value="${esc(st.id)}">` +
    `<select class="steptype" data-field="type">${WF_TYPES.map(t => `<option value="${t}"${t === st.type ? " selected" : ""}>${t}</option>`).join("")}</select>` +
    `<button class="iconbtn wficon" data-act="up" title="Move up" aria-label="Move up"><svg viewBox="0 0 24 24" width="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg></button>` +
    `<button class="iconbtn wficon" data-act="down" title="Move down" aria-label="Move down"><svg viewBox="0 0 24 24" width="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>` +
    `<button class="iconbtn wficon danger stepx" data-act="del" title="Remove step" aria-label="Remove step"><svg viewBox="0 0 24 24" width="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>` +
    `</div><div class="stepfields">${stepFields(st)}</div>`;
  el.querySelector(".steptype").addEventListener("change", (e) => { syncSteps(); wfSteps[i].type = e.target.value; renderSteps(); });
  el.querySelector('[data-act=up]').addEventListener("click", () => { if (i > 0) { syncSteps(); [wfSteps[i - 1], wfSteps[i]] = [wfSteps[i], wfSteps[i - 1]]; renderSteps(); } });
  el.querySelector('[data-act=down]').addEventListener("click", () => { if (i < wfSteps.length - 1) { syncSteps(); [wfSteps[i + 1], wfSteps[i]] = [wfSteps[i], wfSteps[i + 1]]; renderSteps(); } });
  el.querySelector('[data-act=del]').addEventListener("click", () => { syncSteps(); wfSteps.splice(i, 1); if (!wfSteps.length) wfSteps.push(newStep("brain")); renderSteps(); });
  return el;
}
function renderSteps() { const box = g("wfstepbox"); box.innerHTML = ""; wfSteps.forEach((st, i) => box.appendChild(stepCard(st, i))); }
function readCard(card) { const st = {}; card.querySelectorAll("[data-field]").forEach(el => { st[el.dataset.field] = el.value; }); return st; }
function syncSteps() { wfSteps = [...g("wfstepbox").children].map(readCard); }

async function saveWf() {
  syncSteps();
  const trigType = g("wftrigtype").value;
  const trigger = trigType === "schedule" ? { type: "schedule", schedule: g("wfsched").value.trim() } : { type: "manual" };
  const spec = { name: g("wfname").value.trim(), trigger, steps: wfSteps };
  const errEl = g("wferr"); errEl.textContent = "";
  try {
    const r = await (await jfetch("/workflow/save", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: wfEditId, spec }) })).json();
    if (!r.ok) { errEl.textContent = r.error || "Could not save this workflow."; return; }
    openWfDetail(r.id);
  } catch { errEl.textContent = "Save failed — is Helios reachable?"; }
}

// open links in the real system browser (so auth links etc. are one click)
chat.addEventListener("click", (e) => {
  const a = e.target.closest("a.lnk");
  if (!a) return;
  e.preventDefault();
  jfetch("/open", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: a.href }) }).catch(() => window.open(a.href, "_blank"));
});
// --- holographic open animation ---
// Replays the 3D unfold + scan/glow overlay. Called by the native app (evaluate_js) each
// time the dashboard is summoned, and once on first load.
function heliosMaterialize() {
  // Respect reduced-motion: skip the class toggle + forced reflow entirely. (Adding
  // .materializing also establishes a transform containing block that would mis-position a
  // modal opened during the ~0.5s window, so there's no reason to add it when motion is off.)
  if (REDUCED_MOTION) return;
  const root = document.documentElement;
  root.classList.remove("materializing");
  void root.offsetWidth;               // force reflow so the animation restarts
  root.classList.add("materializing");
  clearTimeout(heliosMaterialize._t);
  // Clear shortly after the ~0.45s animation finishes (frees the transform so fixed-position
  // modals position against the viewport again, not the transformed #app).
  heliosMaterialize._t = setTimeout(() => root.classList.remove("materializing"), 550);
}
window.__heliosMaterialize = heliosMaterialize;

window.addEventListener("load", () => {
  heliosMaterialize();
  input.focus();
  jfetch("/version").then(r => r.json()).then(d => {
    g("ver").textContent = "v" + d.version;
  }).catch(() => {});
  // Reflect whether voice is enabled (the daemon also pushes live state via SSE once it's up).
  jfetch("/settings").then(r => r.json()).then(s => {
    setMic(!!(s.voice && s.voice.enabled), false);
    setYolo(!!s.yolo);   // YOLO is a live runtime flag — restore the toggle on (re)load
  }).catch(() => {});
});
