// Helios orb — a 3D NEURAL NETWORK rendered with three.js inside a React component (htm for
// no-build JSX). Runs in a transparent, frameless, always-on-top overlay window
// (helios/orb_webview.py — the "webview" orb engine).
//
// The picture: ~110 nodes on a slowly-turning sphere, linked by synapses, with signal pulses
// travelling along the links — rare drifting sparks when idle, cascades while thinking,
// voice-level-synced while speaking — around a small glowing core.
//
// Two render styles, switchable from Settings ([orb].style):
//   "bloom" (default) — real UnrealBloom post-processing, made transparency-safe via a
//                       selective-bloom composite that preserves the canvas alpha.
//   "glow"            — layered additive fresnel shells (cheap, no post-processing).
//
// Reactivity: subscribes to the app's SSE /events and reflects Helios's state through colour +
// motion + pulse cadence, and swells with the live mic/TTS level. Click toggles the dashboard;
// dragging moves the orb (pywebview drag region; click-vs-drag disambiguated by distance).

import * as THREE from "three";
import htm from "/static/vendor/htm.module.js";

const { useEffect, useRef } = React;
const html = htm.bind(React.createElement);
const TOKEN = window.__HELIOS_TOKEN__ || "";
const STYLE = new URLSearchParams(location.search).get("style") || window.__ORB_STYLE__ || "bloom";
// Test-only: ?bg=dark gives an opaque dark backdrop so the orb can be evaluated in a screenshot
// (the real overlay is always transparent). Harmless in production (the setting never sets bg).
if (new URLSearchParams(location.search).get("bg") === "dark")
  document.documentElement.style.background = document.body.style.background = "#0a0c10";

// State -> colour, spin (rad/s), pulse (hz), spark rate (signals/s along the synapses).
// Mirrors the dashboard's accent vocabulary (synapse dot / emblem / voice HUD).
const STATES = {
  idle:      { color: 0x4dd0e1, spin: 0.25, pulse: 0.7, rate: 0.6 },
  listening: { color: 0x7ee0a0, spin: 0.5,  pulse: 1.5, rate: 2.5 },
  thinking:  { color: 0x66e0ff, spin: 0.95, pulse: 2.0, rate: 12 },
  speaking:  { color: 0x66e0ff, spin: 0.7,  pulse: 1.9, rate: 4 },
  acting:    { color: 0xd6a263, spin: 0.6,  pulse: 1.6, rate: 7 },
  dictation: { color: 0xbdaaf0, spin: 0.55, pulse: 1.6, rate: 3 },
  error:     { color: 0xe0556b, spin: 1.1,  pulse: 3.0, rate: 8 },
};

const N_NODES = 110;        // neurons on the sphere
const EDGE_D2 = 0.145;      // link nodes closer than sqrt(this) (unit-sphere chord²)
const MAX_PULSES = 48;      // travelling-signal pool size

const FRESNEL_VERT = `
  varying vec3 vN; varying vec3 vView;
  uniform float uTime; uniform float uWarp;
  void main() {
    vec3 p = position;
    float n = sin(p.x*5.0 + uTime*1.3) * cos(p.y*5.0 + uTime) * sin(p.z*5.0 + uTime*0.7);
    p += normal * n * uWarp;
    vN = normalize(normalMatrix * normal);
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vView = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const CORE_FRAG = `
  varying vec3 vN; varying vec3 vView;
  uniform vec3 uColor; uniform float uIntensity;
  void main() {
    float f = pow(1.0 - max(dot(vN, vView), 0.0), 2.2);
    vec3 col = mix(uColor * 0.35, uColor, f) * uIntensity;
    gl_FragColor = vec4(col, 0.30 + 0.65 * f);
  }`;
const GLOW_FRAG = `
  varying vec3 vN; varying vec3 vView;
  uniform vec3 uColor; uniform float uOpacity; uniform float uPower;
  void main() {
    float f = pow(1.0 - max(dot(vN, vView), 0.0), uPower);
    gl_FragColor = vec4(uColor, f * uOpacity);
  }`;
// Selective-bloom composite that PRESERVES alpha: add bloom light, raise alpha by bloom luminance.
const MIX_FRAG = `
  varying vec2 vUv;
  uniform sampler2D baseTexture;
  uniform sampler2D bloomTexture;
  void main() {
    vec4 base = texture2D(baseTexture, vUv);
    vec4 bloom = texture2D(bloomTexture, vUv);
    float bl = max(bloom.r, max(bloom.g, bloom.b));
    gl_FragColor = vec4(base.rgb + bloom.rgb, clamp(base.a + bl, 0.0, 1.0));
  }`;

function makeShader(frag, uniforms, opts = {}) {
  return new THREE.ShaderMaterial({
    vertexShader: FRESNEL_VERT, fragmentShader: frag, uniforms,
    transparent: true, depthWrite: false,
    blending: opts.additive ? THREE.AdditiveBlending : THREE.NormalBlending,
    side: opts.side || THREE.FrontSide,
  });
}

// ---- neural-net geometry helpers (fibonacci sphere, distance-threshold synapses) ----
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
// Soft round sprite for nodes + pulses (canvas radial gradient -> texture).
function makeSprite() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const ctx = c.getContext("2d");
  const grd = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.35, "rgba(255,255,255,.7)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = grd;
  ctx.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

function Orb() {
  const mount = useRef(null);

  useEffect(() => {
    const el = mount.current;
    let disposed = false, raf = 0, es = null, renderer = null;
    const W = window.innerWidth, H = window.innerHeight;

    renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(W, H);
    renderer.setClearColor(0x000000, 0);
    el.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, W / H, 0.1, 100);
    camera.position.z = 3.1;
    const group = new THREE.Group();
    group.rotation.x = 0.35;
    scene.add(group);

    const col = new THREE.Color(STATES.idle.color);
    const uTime = { value: 0 };
    const bloom = STYLE !== "glow";
    const sprite = makeSprite();

    // ---- the network ----
    const nodes = sphereNodes(N_NODES);
    const edges = sphereEdges(nodes, EDGE_D2);
    // neurons
    const nodePos = new Float32Array(nodes.length * 3);
    nodes.forEach((p, i) => nodePos.set(p, i * 3));
    const nodeGeo = new THREE.BufferGeometry();
    nodeGeo.setAttribute("position", new THREE.BufferAttribute(nodePos, 3));
    const nodeMat = new THREE.PointsMaterial({
      map: sprite, color: col.clone(), size: 0.085, sizeAttenuation: true,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    });
    group.add(new THREE.Points(nodeGeo, nodeMat));
    // synapses
    const edgePos = new Float32Array(edges.length * 6);
    edges.forEach(([i, j], k) => { edgePos.set(nodes[i], k * 6); edgePos.set(nodes[j], k * 6 + 3); });
    const edgeGeo = new THREE.BufferGeometry();
    edgeGeo.setAttribute("position", new THREE.BufferAttribute(edgePos, 3));
    const edgeMat = new THREE.LineBasicMaterial({
      color: col.clone(), transparent: true, opacity: bloom ? 0.22 : 0.3,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    group.add(new THREE.LineSegments(edgeGeo, edgeMat));
    // travelling signal pulses: a pooled Points geometry; dead slots are coloured BLACK,
    // which is invisible under additive blending (a cheap per-point alpha).
    const pulsePos = new Float32Array(MAX_PULSES * 3);
    const pulseCol = new Float32Array(MAX_PULSES * 3);
    const pulseGeo = new THREE.BufferGeometry();
    pulseGeo.setAttribute("position", new THREE.BufferAttribute(pulsePos, 3));
    pulseGeo.setAttribute("color", new THREE.BufferAttribute(pulseCol, 3));
    const pulseMat = new THREE.PointsMaterial({
      map: sprite, vertexColors: true, size: 0.14, sizeAttenuation: true,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    });
    group.add(new THREE.Points(pulseGeo, pulseMat));
    const pulses = [];          // { e: edge index, t: 0..1, v: speed }
    let spawnAcc = 0;

    // ---- the core (small energy sphere the net wraps around) ----
    const coreU = { uTime, uWarp: { value: 0.02 }, uColor: { value: col.clone() },
      uIntensity: { value: bloom ? 1.2 : 1.0 } };
    const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.34, 6), makeShader(CORE_FRAG, coreU));
    group.add(core);
    const sparkU = { uTime, uWarp: { value: 0 }, uColor: { value: new THREE.Color(0xffffff) },
      uIntensity: { value: bloom ? 1.9 : 1.4 } };
    const spark = new THREE.Mesh(new THREE.IcosahedronGeometry(0.15, 4),
      makeShader(CORE_FRAG, sparkU, { additive: true }));
    group.add(spark);

    // glow-style only: additive halo shells (bloom style gets its glow from post-processing)
    const shells = [];
    if (!bloom) {
      [{ r: 1.08, op: 0.28, pw: 3.2 }, { r: 1.24, op: 0.12, pw: 2.4 }].forEach(s => {
        const u = { uTime, uWarp: { value: 0 }, uColor: { value: col.clone() },
          uOpacity: { value: s.op }, uPower: { value: s.pw } };
        group.add(new THREE.Mesh(new THREE.IcosahedronGeometry(s.r, 5),
          makeShader(GLOW_FRAG, u, { additive: true, side: THREE.BackSide })));
        shells.push(u);
      });
    }

    const st = { target: new THREE.Color(STATES.idle.color), spin: STATES.idle.spin,
      pulse: STATES.idle.pulse, rate: STATES.idle.rate,
      level: 0, levelSmooth: 0, errUntil: 0, voiceActive: false };
    function setState(name) {
      const s = STATES[name] || STATES.idle;
      st.target.setHex(s.color); st.spin = s.spin; st.pulse = s.pulse; st.rate = s.rate;
      if (name === "error") st.errUntil = performance.now() + 1600;
    }

    es = new EventSource("/events?token=" + encodeURIComponent(TOKEN));
    es.onmessage = (e) => {
      let m; try { m = JSON.parse(e.data); } catch { return; }
      const { kind, data } = m;
      if (kind === "voice") {
        const vs = data && data.state;
        if (vs === "idle" || vs === "off" || vs === "disabled") { st.voiceActive = false; setState("idle"); }
        else if (["listening", "speaking", "thinking", "dictation"].includes(vs)) { st.voiceActive = true; setState(vs); }
        else if (vs === "error") { st.voiceActive = true; setState("error"); }
        if (data && typeof data.level === "number") st.level = Math.max(0, Math.min(1, data.level));
      } else if (kind === "model") { if (!st.voiceActive) setState("thinking"); }
      else if (kind === "tool") { setState("acting"); }
      else if (kind === "done") { if (!st.voiceActive) { setState("idle"); st.level = 0; } }
      else if (kind === "error") { setState("error"); }
      else if (kind === "control") {
        if (data && data.action === "sleep") { st.voiceActive = false; setState("idle"); }
        else if (data && data.action === "orb_reload") { location.reload(); }
      }
    };
    es.onerror = () => {};

    // ---- render path ----
    let renderFn = () => renderer.render(scene, camera);
    let bloomPass = null, bloomComposer = null, finalComposer = null;
    if (bloom) {
      Promise.all([
        import("/static/vendor/jsm/postprocessing/EffectComposer.js"),
        import("/static/vendor/jsm/postprocessing/RenderPass.js"),
        import("/static/vendor/jsm/postprocessing/ShaderPass.js"),
        import("/static/vendor/jsm/postprocessing/UnrealBloomPass.js"),
      ]).then(([{ EffectComposer }, { RenderPass }, { ShaderPass }, { UnrealBloomPass }]) => {
        if (disposed) return;
        const renderScene = new RenderPass(scene, camera);
        bloomPass = new UnrealBloomPass(new THREE.Vector2(W, H), 0.9, 0.55, 0.1);
        bloomComposer = new EffectComposer(renderer);
        bloomComposer.renderToScreen = false;
        bloomComposer.addPass(renderScene);
        bloomComposer.addPass(bloomPass);
        const mixPass = new ShaderPass(new THREE.ShaderMaterial({
          uniforms: { baseTexture: { value: null }, bloomTexture: { value: bloomComposer.renderTarget2.texture } },
          vertexShader: "varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }",
          fragmentShader: MIX_FRAG, transparent: true, blending: THREE.NoBlending,
        }), "baseTexture");
        finalComposer = new EffectComposer(renderer);
        finalComposer.addPass(renderScene);
        finalComposer.addPass(mixPass);
        renderFn = () => { renderer.setClearColor(0x000000, 0); bloomComposer.render(); finalComposer.render(); };
      });
    }

    const bright = new THREE.Color();
    let last = performance.now();
    function frame(now) {
      raf = requestAnimationFrame(frame);
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      uTime.value += dt;
      if (st.errUntil && now > st.errUntil) { st.errUntil = 0; if (!st.voiceActive) setState("idle"); }
      st.levelSmooth += (st.level - st.levelSmooth) * 0.25;
      st.level *= 0.94;
      const lvl = st.levelSmooth;

      // colours chase the state
      coreU.uColor.value.lerp(st.target, 0.12);
      nodeMat.color.copy(coreU.uColor.value);
      edgeMat.color.copy(coreU.uColor.value);
      shells.forEach(u => u.uColor.value.copy(coreU.uColor.value));

      // motion
      group.rotation.y += st.spin * dt;
      group.rotation.x = 0.35 + Math.sin(uTime.value * 0.3) * 0.1;
      const breathe = 1 + 0.035 * Math.sin(uTime.value * st.pulse * Math.PI);
      group.scale.setScalar(breathe * (1 + 0.16 * lvl));

      // travelling signals: spawn by rate accumulator (+voice), advance, expire
      spawnAcc += (st.rate + 8 * lvl) * dt;
      while (spawnAcc >= 1 && pulses.length < MAX_PULSES) {
        spawnAcc -= 1;
        pulses.push({ e: (Math.random() * edges.length) | 0, t: 0, v: 0.9 + Math.random() * 1.4 });
      }
      if (spawnAcc > 2) spawnAcc = 2;
      for (let k = pulses.length - 1; k >= 0; k--) {
        pulses[k].t += pulses[k].v * dt;
        if (pulses[k].t >= 1) pulses.splice(k, 1);
      }
      bright.copy(coreU.uColor.value).lerp(new THREE.Color(0xffffff), 0.55);
      for (let s = 0; s < MAX_PULSES; s++) {
        const p = pulses[s];
        if (p) {
          const [i, j] = edges[p.e];
          const a = nodes[i], b = nodes[j];
          pulsePos[s * 3] = a[0] + (b[0] - a[0]) * p.t;
          pulsePos[s * 3 + 1] = a[1] + (b[1] - a[1]) * p.t;
          pulsePos[s * 3 + 2] = a[2] + (b[2] - a[2]) * p.t;
          const glow = Math.sin(p.t * Math.PI);
          pulseCol[s * 3] = bright.r * glow; pulseCol[s * 3 + 1] = bright.g * glow; pulseCol[s * 3 + 2] = bright.b * glow;
        } else {
          pulseCol[s * 3] = pulseCol[s * 3 + 1] = pulseCol[s * 3 + 2] = 0;   // black = invisible (additive)
        }
      }
      pulseGeo.attributes.position.needsUpdate = true;
      pulseGeo.attributes.color.needsUpdate = true;

      // energy follows the voice
      coreU.uIntensity.value = (bloom ? 1.1 : 0.9) + 0.8 * lvl;
      coreU.uWarp.value = 0.02 + 0.06 * lvl;
      sparkU.uIntensity.value = (bloom ? 1.7 : 1.1) + 2.2 * lvl;
      spark.scale.setScalar(0.8 + 0.9 * lvl);
      if (shells.length) shells[0].uOpacity.value = 0.3 + 0.4 * lvl;
      if (bloomPass) bloomPass.strength = 0.8 + 0.8 * lvl;

      renderFn();
    }
    raf = requestAnimationFrame(frame);

    const onResize = () => {
      const w = window.innerWidth, h = window.innerHeight;
      renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
      bloomComposer && bloomComposer.setSize(w, h);
      finalComposer && finalComposer.setSize(w, h);
    };
    window.addEventListener("resize", onResize);

    return () => {
      disposed = true; cancelAnimationFrame(raf); if (es) es.close();
      window.removeEventListener("resize", onResize);
      try { renderer.dispose(); el.removeChild(renderer.domElement); } catch {}
    };
  }, []);

  return html`<div id="orb-mount" ref=${mount}></div>`;
}

// ---- click (toggle dashboard) vs drag (move window) ----
let down = null;
document.addEventListener("pointerdown", (e) => { down = { x: e.screenX, y: e.screenY }; });
document.addEventListener("pointerup", (e) => {
  if (!down) return;
  const moved = Math.abs(e.screenX - down.x) + Math.abs(e.screenY - down.y);
  down = null;
  if (moved <= 5) {
    fetch("/orb/toggle", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Auth-Token": TOKEN }, body: "{}" }).catch(() => {});
  }
});

ReactDOM.createRoot(document.getElementById("root")).render(html`<${Orb} />`);
