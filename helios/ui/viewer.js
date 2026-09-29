/* 3D model viewer for the Models drawer pane — vendored three.js r170 ES modules resolved
   by the import map in index.html (/static/vendor/three/). Design constraints (4 GB GPU,
   always-running desktop app): lazy init (no WebGL context until the first open()),
   render-on-demand (NO requestAnimationFrame loop — zero GPU while idle), and the previous
   model's geometry/materials/textures are disposed on every open()/pause(). app.js drives
   this through window.HeliosViewer = { open, wireframe, pause }. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

let renderer = null, scene = null, camera = null, controls = null, model = null;
let canvas = null;

const stats = () => document.getElementById("modelstats");
function fmtK(n) { return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : "" + n; }

function renderOnce() {
  if (renderer) renderer.render(scene, camera);
}

function fit() {
  if (!renderer) return;
  const host = canvas.parentElement || canvas;
  const w = host.clientWidth || 480, h = canvas.clientHeight || 320;
  renderer.setSize(w, h, false);
  camera.aspect = w / Math.max(1, h);
  camera.updateProjectionMatrix();
}

function init() {
  if (renderer) return;
  canvas = document.getElementById("modelcanvas");
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  // Tone-mapped 3-light rig: dark materials must stay legible from every orbit angle.
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.5;
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x4a5058, 2.2));
  const sun = new THREE.DirectionalLight(0xffffff, 2.2);
  sun.position.set(4, 8, 5);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xbfd0e8, 1.3);   // opposite the sun: no black backsides
  fill.position.set(-4, 4, -6);
  scene.add(fill);
  const grid = new THREE.GridHelper(10, 20, 0x2a3a4a, 0x1a2430);
  grid.material.toneMapped = false;
  scene.add(grid);
  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = false;            // damping needs a rAF loop; we render on demand
  controls.addEventListener("change", renderOnce);
  canvas.addEventListener("webglcontextlost", (e) => {
    e.preventDefault();
    const el = stats();
    if (el) el.textContent = "graphics context lost - close and reopen the drawer";
  });
  new ResizeObserver(() => { fit(); renderOnce(); }).observe(canvas.parentElement || canvas);
  fit();
}

function disposeModel() {
  if (!model) return;
  scene.remove(model);
  model.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
    for (const m of mats) {
      for (const k in m) { const v = m[k]; if (v && v.isTexture) v.dispose(); }
      m.dispose();
    }
  });
  model = null;
}

function countGeo(root) {
  let verts = 0, tris = 0;
  root.traverse((o) => {
    const gm = o.geometry;
    if (!gm || !gm.attributes || !gm.attributes.position) return;
    verts += gm.attributes.position.count;
    tris += (gm.index ? gm.index.count : gm.attributes.position.count) / 3;
  });
  return { verts, tris: Math.round(tris) };
}

function frameModel(root) {
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  root.position.sub(center);                 // recenter at the origin
  const r = Math.max(sphere.radius, 0.001);
  camera.position.set(r * 1.3, r * 0.9, r * 1.6);
  camera.near = r / 100;
  camera.far = r * 100;
  camera.updateProjectionMatrix();
  controls.target.set(0, 0, 0);
  controls.update();
}

window.HeliosViewer = {
  open(url, name) {
    init();
    fit();
    disposeModel();
    renderOnce();
    const el = stats();
    if (el) el.textContent = "loading…";
    const authed = url + (url.includes("?") ? "&" : "?") +
      "token=" + encodeURIComponent(window.__HELIOS_TOKEN__ || "");
    new GLTFLoader().load(authed, (gltf) => {
      model = gltf.scene || (gltf.scenes && gltf.scenes[0]);
      if (!model) { if (el) el.textContent = "empty model"; return; }
      scene.add(model);
      frameModel(model);
      const c = countGeo(model);
      if (el) el.textContent = `${fmtK(c.verts)} verts · ${fmtK(c.tris)} tris`;
      renderOnce();
    }, undefined, (err) => {
      if (el) el.textContent = "could not load model";
      console.warn("[viewer]", name, err);
    });
  },

  wireframe() {
    if (!model) return;
    model.traverse((o) => {
      const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
      for (const m of mats) if ("wireframe" in m) m.wireframe = !m.wireframe;
    });
    renderOnce();
  },

  pause() {
    // Drawer closed: free the model's GPU memory; keep the renderer for an instant reopen.
    disposeModel();
    renderOnce();
  },
};
