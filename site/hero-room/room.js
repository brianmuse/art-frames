// The 3D bedroom in the website hero. A Frame TV and a wall clock hang on the wall of a calm bedroom.
// Scrolling moves the camera in toward the TV and runs a morning: the TV is off at 6:45, turns on at
// 7:00 with the Morning layout, and fades to just the art at 9:00. The wall clock shows the time, and
// sun from a window moves across the wall.
//
// Everything is built here in code: no model, texture or HDRI files. Units are meters. The back wall
// is the plane z = 0, the room is on the +z side, and the floor is y = 0.
//
// Build: npm install && npm run build (writes ../vendor/hero-room.js).

import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

const ROOM = { left: -3.1, right: 3.1, depth: 6.5, height: 2.7 };
const TV = { x: 0, y: 1.43, width: 1.21, depth: 0.026 }; // a 55" Frame: screen width in meters
// The window on the left wall, behind the bed: z and y ranges of the opening.
const WINDOW = { z0: 0.95, z1: 2.05, y0: 1.3, y1: 2.38 };
// The wall clock, to the right of the TV.
const CLOCK = { x: TV.x + 0.95, y: 1.56, radius: 0.165 };
// Times of day, in minutes after midnight.
// A small room sensor on the credenza, left of the books.
const SENSOR = { x: TV.x + 0.2, y: 0, z: 0 }; // y and z are set when the credenza is built
// The sensor's status light, so the last shot can blink it.
const ledColor = new THREE.Color(0x5fe08a);
const ledMaterial = new THREE.MeshBasicMaterial({ color: 0x5fe08a });
const TV_ON = 7 * 60, ART_FROM = 9 * 60, ART_TO = 9 * 60 + 10;

// ---------- Small helpers ----------

function random(seed) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Value noise that tiles with the given period (in cells).
function noise2(seed) {
  const r = random(seed), n = 256, grid = new Float32Array(n * n);
  for (let i = 0; i < grid.length; i++) grid[i] = r();
  return (x, y, px = n, py = n) => {
    px = Math.max(1, Math.round(px)); py = Math.max(1, Math.round(py));
    const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    const x0 = ((xi % px) + px) % px, x1 = (x0 + 1) % px, y0 = ((yi % py) + py) % py, y1 = (y0 + 1) % py;
    const a = grid[y0 * n + x0], b = grid[y0 * n + x1], c = grid[y1 * n + x0], d = grid[y1 * n + x1];
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  };
}

function canvasTexture(w, h, paint, { srgb = true, repeat = null } = {}) {
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d');
  const img = ctx.createImageData(w, h);
  paint(img.data, w, h);
  ctx.putImageData(img, 0, 0);
  const tex = new THREE.CanvasTexture(canvas);
  if (srgb) tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  if (repeat) { tex.wrapS = tex.wrapT = THREE.RepeatWrapping; tex.repeat.set(repeat[0], repeat[1]); }
  return tex;
}

const clamp01 = v => Math.min(1, Math.max(0, v));
const smooth = t => t * t * (3 - 2 * t);
const easeInOut = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

// ---------- Textures ----------

// Light oak planks that run toward the viewer. One tile is 1.2 m square.
function oakFloor() {
  const n = noise2(11), r = random(5), size = 1024, planks = 6;
  const plankTone = [], ends = [];
  for (let i = 0; i < planks; i++) {
    const cut = r(), tones = [0.95 + r() * 0.08, 0.95 + r() * 0.08];
    plankTone.push(tones); ends.push(cut);
  }
  return canvasTexture(size, size, (d, w, h) => {
    const pw = w / planks;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = Math.floor(x / pw), u = x - i * pw, v = y / h;
      const end = ends[i], seg = (v + 1 - end) % 1 < 0.5 ? 0 : 1;
      const tone = plankTone[i][seg];
      const across = (x / w) * 64, along = v * 6 + i * 7.3 + seg * 3.1;
      const streak = n(across, along, 64, 256) * 0.6 + n(across * 3, along * 2.5, 192, 256) * 0.4;
      const ring = Math.sin(across * 2.4 + streak * 9 + i * 13) * 0.5 + 0.5;
      let k = tone * (0.93 + streak * 0.08 + ring * 0.035);
      // Gaps between planks and at plank ends.
      const edge = Math.min(u, pw - u), endDist = Math.abs(((v + 1 - end) % 0.5)) * h;
      if (edge < 1.4) k *= 0.72 + 0.2 * edge / 1.4;
      if (endDist < 1.2 || 0.5 * h - endDist < 1.2) k *= 0.8;
      const o = (y * w + x) * 4;
      d[o] = Math.min(255, 214 * k); d[o + 1] = Math.min(255, 184 * k); d[o + 2] = Math.min(255, 148 * k); d[o + 3] = 255;
    }
  }, { repeat: [(ROOM.right - ROOM.left) / 1.2, ROOM.depth / 3.6] });
}

// A gray linen weave, used as a map (multiplied by the material color) and as a bump map.
function linen(seed, size = 512) {
  const n = noise2(seed), r = random(seed + 1);
  const rows = Array.from({ length: size }, () => r()), cols = Array.from({ length: size }, () => r());
  return canvasTexture(size, size, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const slub = n(x / 64, y / 3, w / 64, h / 3) * 0.5 + n(x / 3, y / 48, w / 3, h / 48) * 0.5;
      const weave = (x + y) % 2 ? rows[y] : cols[x];
      const k = 0.86 + weave * 0.08 + slub * 0.08;
      const o = (y * w + x) * 4;
      d[o] = d[o + 1] = d[o + 2] = 255 * k; d[o + 3] = 255;
    }
  }, { srgb: false, repeat: [1, 1] });
}

// Soft creases for bedding: long, thin ridges at a few angles.
function wrinkles() {
  const n = noise2(53);
  return canvasTexture(512, 512, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const warp = n(x / 64, y / 64, 8, 8) * 6;
      const a = Math.sin((x * 0.8 + y * 0.35) / 22 + warp), b = Math.sin((x * -0.3 + y * 0.9) / 31 + warp * 0.7);
      const ridge = Math.pow(1 - Math.abs(a), 6) * 0.6 + Math.pow(1 - Math.abs(b), 8) * 0.4;
      const k = 0.5 + ridge * 0.25 * n(x / 40, y / 40, 13, 13) + n(x / 2, y / 2, 256, 256) * 0.08;
      const o = (y * w + x) * 4;
      d[o] = d[o + 1] = d[o + 2] = 255 * k; d[o + 3] = 255;
    }
  }, { srgb: false, repeat: [1, 1] });
}

// Walnut veneer, grain along u.
function walnut() {
  const n = noise2(23);
  return canvasTexture(512, 512, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const a = n(x / 90, y / 6, w / 90, h / 6), b = n(x / 30, y / 2.2, w / 30, h / 2.2);
      const ring = Math.sin(y / 9 + a * 10) * 0.5 + 0.5;
      const k = 0.82 + a * 0.16 + b * 0.08 + ring * 0.08;
      const o = (y * w + x) * 4;
      d[o] = 112 * k; d[o + 1] = 78 * k; d[o + 2] = 54 * k; d[o + 3] = 255;
    }
  }, { repeat: [1, 1] });
}

// A tangent-space normal map from a tiling height field. height(x, y) gives 0 to 1 per pixel.
function normalTexture(size, height, strength, repeat = [1, 1]) {
  const hs = new Float32Array(size * size);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) hs[y * size + x] = height(x, y, size);
  return canvasTexture(size, size, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const l = hs[y * w + (x + w - 1) % w], r = hs[y * w + (x + 1) % w];
      const up = hs[((y + h - 1) % h) * w + x], dn = hs[((y + 1) % h) * w + x];
      // Canvas rows run down and v runs up, so the y slope flips.
      let nx = (l - r) * strength, ny = (dn - up) * strength;
      const k = 1 / Math.hypot(nx, ny, 1), o = (y * w + x) * 4;
      d[o] = (nx * k * 0.5 + 0.5) * 255; d[o + 1] = (ny * k * 0.5 + 0.5) * 255; d[o + 2] = (k * 0.5 + 0.5) * 255; d[o + 3] = 255;
    }
  }, { srgb: false, repeat });
}

// Walnut grain relief that matches walnut(): soft ridges along the grain and fine open pores.
function walnutNormal() {
  const n = noise2(23), pores = noise2(29);
  return normalTexture(512, (x, y, w) => {
    const a = n(x / 90, y / 6, w / 90, w / 6), b = n(x / 30, y / 2.2, w / 30, w / 2.2);
    const ring = Math.sin(y / 9 + a * 10) * 0.5 + 0.5;
    return ring * 0.35 + b * 0.4 + pores(x / 1.5, y / 0.8, w / 1.5, w / 0.8) * 0.25;
  }, 2.2);
}

// Matte wall paint: a faint roller texture.
function plasterNormal(repeat) {
  const n = noise2(53);
  return normalTexture(256, (x, y, w) => n(x / 2, y / 2, w / 2, w / 2) * 0.5 + n(x / 9, y / 9, w / 9, w / 9) * 0.5, 0.9, repeat);
}

// A wool rug: a quiet field with a slim border.
function rug() {
  const n = noise2(31);
  return canvasTexture(512, 512, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const e = Math.min(x, y, w - 1 - x, h - 1 - y);
      const band = e > 18 && e < 24 ? 1 : 0;
      const fiber = n(x / 2, y / 2, w / 2, h / 2) * 0.6 + n(x / 14, y / 14, w / 14, h / 14) * 0.4;
      const k = (0.9 + fiber * 0.1) * (band ? 0.93 : 1);
      const o = (y * w + x) * 4;
      d[o] = 234 * k; d[o + 1] = 226 * k; d[o + 2] = 212 * k; d[o + 3] = 255;
    }
  });
}

// A soft dark blob for contact shadows: a rectangle (half size hx, hy in 0..0.5) with soft edges.
function softShadow(hx, hy, soft) {
  return canvasTexture(128, 128, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const dx = Math.max(Math.abs(x / w - 0.5) - hx, 0), dy = Math.max(Math.abs(y / h - 0.5) - hy, 0);
      const a = Math.exp(-((dx * dx + dy * dy) / (soft * soft)));
      const o = (y * w + x) * 4; // alpha maps read the green channel
      d[o] = d[o + 1] = d[o + 2] = 255 * a; d[o + 3] = 255;
    }
  }, { srgb: false });
}

// A vertical fade from dark to clear, for the soft corners where walls meet the floor.
function cornerFade() {
  return canvasTexture(4, 128, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      // Dark at the top row of the canvas, which is v = 1 on a plane.
      const t = y / (h - 1), o = (y * w + x) * 4;
      d[o] = d[o + 1] = d[o + 2] = 255 * Math.pow(1 - t, 2.2); d[o + 3] = 255;
    }
  }, { srgb: false });
}

// Snake plant leaf: dark green with lighter bands and a yellow edge.
function leafTexture() {
  const n = noise2(41);
  return canvasTexture(128, 512, (d, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const u = x / (w - 1), edge = Math.min(u, 1 - u);
      const band = Math.sin(y / 9 + n(x / 10, y / 20) * 6) * 0.5 + 0.5;
      let rC = 46 + band * 22, gC = 78 + band * 30, bC = 50 + band * 14;
      if (edge < 0.07) { rC = 196; gC = 186; bC = 96; }
      const o = (y * w + x) * 4;
      d[o] = rC; d[o + 1] = gC; d[o + 2] = bC; d[o + 3] = 255;
    }
  });
}

// ---------- Geometry helpers ----------

function box(w, h, d, radius, material, segments = 4) {
  const geo = radius > 0 ? new RoundedBoxGeometry(w, h, d, segments, radius) : new THREE.BoxGeometry(w, h, d);
  const mesh = new THREE.Mesh(geo, material);
  mesh.castShadow = mesh.receiveShadow = true;
  return mesh;
}

// Rounded boxes get UVs per face; scale them so a texture tiles at a real size (meters per tile).
function worldUV(geo, metersPerTile) {
  const pos = geo.attributes.position, nor = geo.attributes.normal, uv = geo.attributes.uv;
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i);
    const ax = Math.abs(nor.getX(i)), ay = Math.abs(nor.getY(i)), az = Math.abs(nor.getZ(i));
    let u, v;
    if (ay >= ax && ay >= az) { u = x; v = z; } else if (ax >= az) { u = z; v = y; } else { u = x; v = y; }
    uv.setXY(i, u / metersPerTile, v / metersPerTile);
  }
  uv.needsUpdate = true;
  return geo;
}

function displace(geo, fn) {
  const pos = geo.attributes.position, v = new THREE.Vector3();
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i);
    fn(v);
    pos.setXYZ(i, v.x, v.y, v.z);
  }
  geo.computeVertexNormals();
  return geo;
}

function shadowDecal(w, d, tex, opacity, y = 0.003) {
  const mesh = new THREE.Mesh(
    new THREE.PlaneGeometry(w, d),
    new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity, depthWrite: false, color: 0x000000 }),
  );
  mesh.material.alphaMap = tex; mesh.material.map = null;
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = y;
  mesh.renderOrder = 1;
  return mesh;
}

// ---------- The window ----------

// Sky outside the window: a soft vertical gradient, tinted by the time of day.
const skyMaterial = new THREE.MeshBasicMaterial({ color: 0xffffff });

function buildWindow(scene) {
  const { z0, z1, y0, y1 } = WINDOW, x = ROOM.left, zc = (z0 + z1) / 2, yc = (y0 + y1) / 2;
  const white = new THREE.MeshStandardMaterial({ color: 0xf4f1eb, roughness: 0.6 });
  const part = (w, h, d, px, py, pz) => {
    const m = box(d, h, w, 0, white); m.position.set(px, py, pz); scene.add(m); return m;
  };
  // Frame inside the opening, two mullions, and a sill.
  const f = 0.045;
  part(z1 - z0, f, 0.1, x - 0.06, y1 - f / 2, zc);
  part(z1 - z0, f, 0.1, x - 0.06, y0 + f / 2, zc);
  part(f, y1 - y0, 0.1, x - 0.06, yc, z0 + f / 2);
  part(f, y1 - y0, 0.1, x - 0.06, yc, z1 - f / 2);
  part(0.028, y1 - y0, 0.05, x - 0.07, yc, zc);
  part(z1 - z0, 0.028, 0.05, x - 0.07, y0 + (y1 - y0) * 0.58, zc);
  part(z1 - z0 + 0.12, 0.03, 0.2, x - 0.04, y0 - 0.015, zc);
  // Casing on the wall around the opening.
  const c = 0.06;
  part(z1 - z0 + 2 * c, c, 0.02, x + 0.01, y1 + c / 2, zc);
  part(c, y1 - y0 + c, 0.02, x + 0.01, yc + c / 2, z0 - c / 2);
  part(c, y1 - y0 + c, 0.02, x + 0.01, yc + c / 2, z1 + c / 2);

  // The sky, a little way outside.
  const sky = new THREE.Mesh(new THREE.PlaneGeometry(12, 8), skyMaterial);
  sky.rotation.y = Math.PI / 2; sky.position.set(x - 3, 2.2, zc);
  skyMaterial.map = canvasTexture(4, 256, (d, w, h) => {
    for (let yy = 0; yy < h; yy++) for (let xx = 0; xx < w; xx++) {
      const t = yy / (h - 1), k = 0.72 + 0.28 * Math.pow(t, 0.7), o = (yy * w + xx) * 4; // lighter toward the horizon
      d[o] = d[o + 1] = d[o + 2] = 255 * k; d[o + 3] = 255;
    }
  });
  scene.add(sky);
  // A far-off hedge line so the window does not read as a blank light box.
  const hedge = new THREE.Mesh(new THREE.PlaneGeometry(12, 1.6, 240, 1), new THREE.MeshBasicMaterial({ color: 0xaab39d }));
  hedge.rotation.y = Math.PI / 2; hedge.position.set(x - 2.9, 0.75, zc);
  displace(hedge.geometry, v => { if (v.y > 0) v.y += Math.sin(v.x * 2.3) * 0.05 + Math.abs(Math.sin(v.x * 9.7)) * 0.07 + Math.abs(Math.sin(v.x * 23 + 1)) * 0.03; });
  hedge.geometry.attributes.position.needsUpdate = true;
  scene.add(hedge);

  // Sheer curtains, pulled to the sides.
  const sheer = new THREE.MeshStandardMaterial({ color: 0xfbf8f2, roughness: 1, transparent: true, opacity: 0.82, side: THREE.DoubleSide });
  for (const [za, zb] of [[z0 - 0.12, z0 + 0.2], [z1 - 0.2, z1 + 0.12]]) {
    const g = new THREE.PlaneGeometry(zb - za, y1 + 0.16 - (y0 - 0.12), 24, 2);
    displace(g, v => { v.z = Math.sin(v.x * 42) * 0.018; });
    const m = new THREE.Mesh(g, sheer);
    m.rotation.y = Math.PI / 2;
    m.position.set(x + 0.07, (y1 + 0.16 + y0 - 0.12) / 2, (za + zb) / 2);
    scene.add(m);
  }
  const rod = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, z1 - z0 + 0.5, 12), new THREE.MeshStandardMaterial({ color: 0x8a7a66, roughness: 0.5, metalness: 0.4 }));
  rod.rotation.x = Math.PI / 2; rod.position.set(x + 0.07, y1 + 0.17, (z0 + z1) / 2);
  scene.add(rod);
}

// ---------- The room ----------

function buildRoom(scene, dark) {
  const width = ROOM.right - ROOM.left;
  const linenA = linen(3), linenB = linen(7, 256), oak = oakFloor(), wood = walnut();

  // Walls: warm off-white matte paint, close to the page's wall color.
  const wallMat = new THREE.MeshStandardMaterial({ color: dark ? 0xcdc1b0 : 0xf2e5d2, roughness: 0.96, normalMap: plasterNormal([width / 0.9, ROOM.height / 0.9]), normalScale: new THREE.Vector2(0.35, 0.35) });
  const back = new THREE.Mesh(new THREE.PlaneGeometry(width, ROOM.height), wallMat);
  back.position.set((ROOM.left + ROOM.right) / 2, ROOM.height / 2, 0);
  back.receiveShadow = true;
  // The left wall is built from thick blocks around the window opening. The blocks reach past the
  // room so that the sun only gets in through the window.
  const T = 0.14, Z0 = -1, Z1 = ROOM.depth + 2, Y0 = -1, Y1 = 5;
  const wallBlock = (z0, z1, y0, y1) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(T, y1 - y0, z1 - z0), wallMat);
    m.position.set(ROOM.left - T / 2, (y0 + y1) / 2, (z0 + z1) / 2);
    m.castShadow = m.receiveShadow = true;
    scene.add(m);
  };
  wallBlock(Z0, WINDOW.z0, Y0, Y1);
  wallBlock(WINDOW.z1, Z1, Y0, Y1);
  wallBlock(WINDOW.z0, WINDOW.z1, Y0, WINDOW.y0);
  wallBlock(WINDOW.z0, WINDOW.z1, WINDOW.y1, Y1);
  buildWindow(scene);
  const right = new THREE.Mesh(new THREE.PlaneGeometry(ROOM.depth, ROOM.height), wallMat);
  right.rotation.y = -Math.PI / 2; right.position.set(ROOM.right, ROOM.height / 2, ROOM.depth / 2); right.receiveShadow = true;
  const ceiling = new THREE.Mesh(new THREE.PlaneGeometry(width, ROOM.depth), new THREE.MeshStandardMaterial({ color: 0xf4f1ec, roughness: 1, emissive: 0xe8e2d8, emissiveIntensity: dark ? 0.05 : 0.7 }));
  ceiling.rotation.x = Math.PI / 2; ceiling.position.set(0, ROOM.height, ROOM.depth / 2);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(width, ROOM.depth), new THREE.MeshStandardMaterial({ map: oak, roughness: 0.62 }));
  floor.rotation.x = -Math.PI / 2; floor.position.set(0, 0, ROOM.depth / 2); floor.receiveShadow = true;
  scene.add(back, right, ceiling, floor);

  // Baseboards.
  const trim = new THREE.MeshStandardMaterial({ color: 0xf3efe8, roughness: 0.7 });
  const bb = box(width, 0.1, 0.016, 0, trim); bb.position.set(0, 0.05, 0.008);
  const bl = box(0.016, 0.1, ROOM.depth, 0, trim); bl.position.set(ROOM.left + 0.008, 0.05, ROOM.depth / 2);
  const br = box(0.016, 0.1, ROOM.depth, 0, trim); br.position.set(ROOM.right - 0.008, 0.05, ROOM.depth / 2);
  scene.add(bb, bl, br);

  // Soft darkening where walls meet the floor and each other (a cheap stand-in for ambient occlusion).
  const fade = cornerFade();
  const aoMat = o => new THREE.MeshBasicMaterial({ color: 0x000000, alphaMap: fade, transparent: true, opacity: o, depthWrite: false });
  // Each fade is dark at v = 1; remap the UVs so v = 1 is the corner side.
  const ao = (w, h, opacity, remap) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), aoMat(opacity));
    const uv = m.geometry.attributes.uv;
    for (let i = 0; i < uv.count; i++) uv.setXY(i, 0.5, remap(uv.getX(i), uv.getY(i)));
    m.renderOrder = 1;
    return m;
  };
  const aoFloorBack = ao(width, 0.35, 0.16, (u, v) => v); // rotated flat: v = 1 is the wall side
  aoFloorBack.rotation.x = -Math.PI / 2; aoFloorBack.position.set(0, 0.002, 0.175);
  const aoWallBack = ao(width, 0.4, 0.1, (u, v) => 1 - v);
  aoWallBack.position.set(0, 0.2, 0.002);
  const aoCornerL = ao(0.5, ROOM.height, 0.12, u => 1 - u);
  aoCornerL.position.set(ROOM.left + 0.25, ROOM.height / 2, 0.002);
  const aoCornerR = ao(0.5, ROOM.height, 0.12, u => u);
  aoCornerR.position.set(ROOM.right - 0.25, ROOM.height / 2, 0.002);
  const aoFloorLeft = ao(ROOM.depth, 0.35, 0.16, (u, v) => v);
  aoFloorLeft.rotation.set(-Math.PI / 2, 0, Math.PI / 2); aoFloorLeft.position.set(ROOM.left + 0.175, 0.002, ROOM.depth / 2);
  scene.add(aoFloorBack, aoWallBack, aoCornerL, aoCornerR, aoFloorLeft);

  // ----- Bed: headboard against the left wall, the bed reaching into the room. -----
  const upholstery = new THREE.MeshStandardMaterial({ color: 0xc7bba9, roughness: 0.92, map: linenB, bumpMap: linenB, bumpScale: 0.6 });
  const creases = wrinkles();
  const sheet = new THREE.MeshStandardMaterial({ color: 0xf2eee6, roughness: 0.9, map: linenA, bumpMap: creases, bumpScale: 0.9 });
  const oat = new THREE.MeshStandardMaterial({ color: 0xd8c9b0, roughness: 0.95, map: linenB, bumpMap: linenB, bumpScale: 1 });
  const clay = new THREE.MeshStandardMaterial({ color: 0xbfa58a, roughness: 0.92, map: linenA, bumpMap: linenA, bumpScale: 0.8 });
  linenA.repeat.set(3, 3); linenB.repeat.set(2, 2);

  const bed = new THREE.Group();
  const bedZ = 1.5, bedW = 1.62, bedLen = 2.12, x0 = ROOM.left + 0.02;
  const headboard = box(0.1, 1.12, bedW + 0.1, 0.04, upholstery, 6);
  headboard.position.set(x0 + 0.05, 0.56, bedZ);
  worldUV(headboard.geometry, 0.5);
  const base = box(bedLen, 0.28, bedW, 0.035, upholstery, 5);
  base.position.set(x0 + 0.1 + bedLen / 2, 0.06 + 0.14, bedZ);
  worldUV(base.geometry, 0.5);
  const mattress = box(bedLen - 0.08, 0.22, bedW - 0.06, 0.06, sheet, 5);
  mattress.position.set(x0 + 0.1 + bedLen / 2, 0.45, bedZ);
  // Duvet: wider than the mattress, so it drapes over the sides, with soft folds on top.
  const duvetLen = 1.72, duvetX = x0 + 0.1 + bedLen - duvetLen / 2 + 0.03;
  const duvetGeo = new RoundedBoxGeometry(duvetLen, 0.36, bedW + 0.1, 12, 0.1);
  displace(duvetGeo, v => {
    const top = clamp01((v.y + 0.1) / 0.28);
    const fold = Math.sin(v.x * 7.3 + v.z * 2.1) * 0.014 + Math.sin(v.x * 3.1 - v.z * 5.7) * 0.012 + Math.sin(v.z * 11 + v.x * 1.3) * 0.006 + Math.sin(v.x * 17 - v.z * 3) * 0.003;
    v.y += fold * top;
    if (v.y < 0) { v.x += Math.sin(v.z * 9) * 0.008; v.z += Math.sign(v.z) * (-v.y) * 0.05; }
  });
  worldUV(duvetGeo, 0.6);
  const duvet = new THREE.Mesh(duvetGeo, sheet); duvet.castShadow = duvet.receiveShadow = true;
  duvet.position.set(duvetX, 0.52, bedZ);
  // The duvet's top edge folded back toward the pillows.
  const foldGeo = new THREE.CapsuleGeometry(0.06, bedW + 0.02, 6, 16);
  displace(foldGeo, v => { v.x += Math.sin(v.y * 8) * 0.008; v.z *= 0.75; });
  const foldBack = new THREE.Mesh(foldGeo, sheet); foldBack.castShadow = foldBack.receiveShadow = true;
  foldBack.rotation.x = Math.PI / 2; foldBack.position.set(duvetX - duvetLen / 2 + 0.03, 0.7, bedZ);
  // Throw folded across the foot.
  const throwBlanket = new THREE.Group();
  const throwTop = box(0.55, 0.035, bedW + 0.13, 0.016, oat, 3);
  displace(throwTop.geometry, v => { v.y += Math.sin(v.z * 5 + 1) * 0.005; });
  worldUV(throwTop.geometry, 0.4);
  throwTop.position.y = 0.0;
  throwBlanket.add(throwTop);
  for (const side of [-1, 1]) {
    const drape = box(0.55, 0.3, 0.03, 0.014, oat, 3);
    displace(drape.geometry, v => { v.z += Math.sin(v.x * 18) * 0.006 * (0.5 - v.y / 0.3); });
    worldUV(drape.geometry, 0.4);
    drape.position.set(0, -0.14, side * (bedW / 2 + 0.065));
    throwBlanket.add(drape);
  }
  throwBlanket.position.set(x0 + 0.1 + bedLen - 0.36, 0.735, bedZ);
  bed.add(headboard, base, mattress, duvet, foldBack, throwBlanket);

  // Pillows leaning on the headboard: two sleeping pillows and one smaller clay one in front.
  const pillow = (w, h, t, mat) => {
    const g = new RoundedBoxGeometry(w, h, t, 8, Math.min(t, h) * 0.45);
    displace(g, v => {
      const ex = Math.pow(Math.abs(v.x) / (w / 2), 4), ey = Math.pow(Math.abs(v.y) / (h / 2), 4);
      v.z *= 1 - 0.55 * Math.max(ex, ey);
      v.x *= 1 - 0.04 * (1 - ey); v.y *= 1 - 0.05 * (1 - ex);
    });
    worldUV(g, 0.5);
    const m = new THREE.Mesh(g, mat); m.castShadow = m.receiveShadow = true;
    return m;
  };
  const p1 = pillow(0.72, 0.5, 0.2, sheet), p2 = pillow(0.72, 0.5, 0.2, sheet), p3 = pillow(0.46, 0.32, 0.14, clay);
  for (const [p, z, x, y, tilt] of [[p1, bedZ - 0.38, x0 + 0.26, 0.82, 0.34], [p2, bedZ + 0.38, x0 + 0.26, 0.82, 0.3], [p3, bedZ + 0.05, x0 + 0.42, 0.76, 0.42]]) {
    p.rotation.set(0, Math.PI / 2, 0);
    p.rotateX(-tilt);
    p.position.set(x, y, z);
    bed.add(p);
  }
  scene.add(bed);

  // Rug under the bed.
  const rugMesh = new THREE.Mesh(new RoundedBoxGeometry(2.5, 0.012, 2.3, 2, 0.005), new THREE.MeshStandardMaterial({ map: rug(), roughness: 1 }));
  rugMesh.position.set(x0 + 1.45, 0.006, bedZ + 0.05); rugMesh.receiveShadow = true;
  scene.add(rugMesh);

  // ----- Nightstand with a lamp, between the bed and the back wall. -----
  const grainBump = walnutNormal(); grainBump.repeat.copy(wood.repeat); grainBump.wrapS = grainBump.wrapT = THREE.RepeatWrapping;
  // An oiled finish: low specular, so the low sun glancing across the doors does not wash them grey.
  const walnutMat = new THREE.MeshPhysicalMaterial({ map: wood, normalMap: grainBump, normalScale: new THREE.Vector2(0.22, 0.22), roughness: 0.7, specularIntensity: 0.2, envMapIntensity: 0.5 });
  const stand = new THREE.Group();
  const sw = 0.46, sd = 0.42, sh = 0.5;
  const standBody = box(sd, sh - 0.1, sw, 0.012, walnutMat, 3);
  standBody.position.set(0, 0.1 + (sh - 0.1) / 2, 0);
  worldUV(standBody.geometry, 0.6);
  const legMat = new THREE.MeshStandardMaterial({ color: 0x4a3426, roughness: 0.6 });
  for (const [lx, lz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
    const leg = box(0.03, 0.1, 0.03, 0, legMat); leg.position.set(lx * (sd / 2 - 0.03), 0.05, lz * (sw / 2 - 0.03)); stand.add(leg);
  }
  const groove = box(0.004, 0.006, sw - 0.04, 0, new THREE.MeshStandardMaterial({ color: 0x2a1d15 }));
  groove.position.set(sd / 2 + 0.001, 0.36, 0); groove.castShadow = false;
  stand.add(standBody, groove);
  stand.position.set(ROOM.left + 0.02 + sd / 2, 0, bedZ - bedW / 2 - 0.08 - sw / 2);
  scene.add(stand);

  const lampBase = new THREE.Mesh(
    new THREE.LatheGeometry([[0, 0], [0.06, 0.004], [0.095, 0.06], [0.1, 0.12], [0.085, 0.2], [0.04, 0.27], [0.018, 0.3], [0.018, 0.34], [0, 0.34]].map(([x, y]) => new THREE.Vector2(x, y)), 40),
    new THREE.MeshStandardMaterial({ color: 0xa8ad98, roughness: 0.45 }),
  );
  lampBase.castShadow = lampBase.receiveShadow = true;
  lampBase.position.set(stand.position.x + 0.02, sh, stand.position.z - 0.02);
  const shadeMat = new THREE.MeshStandardMaterial({
    color: 0xf6efe2, roughness: 1, side: THREE.DoubleSide, map: linenA,
    emissive: 0xffd9a8, emissiveIntensity: dark ? 0.9 : 0.32,
  });
  const shade = new THREE.Mesh(new THREE.CylinderGeometry(0.14, 0.17, 0.22, 48, 1, true), shadeMat);
  shade.position.set(lampBase.position.x, sh + 0.39, lampBase.position.z);
  shade.castShadow = true;
  const lampLight = new THREE.PointLight(0xffc78a, dark ? 2.2 : 0.3, 0, 2);
  lampLight.position.set(shade.position.x, shade.position.y - 0.02, shade.position.z);
  scene.add(lampBase, shade, lampLight);
  // A book on the nightstand.
  const book = box(0.17, 0.035, 0.24, 0.004, new THREE.MeshStandardMaterial({ color: 0x8d5a43, roughness: 0.8 }), 2);
  book.position.set(stand.position.x + 0.02, sh + 0.018, stand.position.z + 0.13); book.rotation.y = 0.25;
  scene.add(book);

  // ----- Low credenza under the TV. -----
  const cred = new THREE.Group();
  const cw = 1.8, cdp = 0.42, ch = 0.5, legH = 0.14;
  const body = box(cw, ch, cdp, 0.012, walnutMat, 3);
  body.position.set(0, legH + ch / 2, 0);
  worldUV(body.geometry, 0.9);
  cred.add(body);
  for (let i = 1; i < 4; i++) {
    const g = box(0.005, ch - 0.06, 0.004, 0, groove.material); g.position.set(-cw / 2 + (cw / 4) * i, legH + ch / 2, cdp / 2 + 0.001); g.castShadow = false; cred.add(g);
  }
  for (const [lx, lz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
    const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.012, legH, 12), legMat);
    leg.castShadow = true; leg.position.set(lx * (cw / 2 - 0.08), legH / 2, lz * (cdp / 2 - 0.06)); cred.add(leg);
  }
  // A separate top with a soft rounded edge, a little wider than the body.
  const slab = box(cw + 0.024, 0.026, cdp + 0.02, 0.011, walnutMat, 5);
  slab.position.set(0, legH + ch + 0.006 - 0.013, 0.004);
  worldUV(slab.geometry, 0.9);
  slab.castShadow = false; // the low sun would throw its edge down the whole front
  cred.add(slab);
  cred.position.set(TV.x, 0, cdp / 2 + 0.03);
  scene.add(cred);
  const top = legH + ch + 0.006;
  SENSOR.y = top + 0.03; SENSOR.z = cred.position.z - 0.02;
  // Books, a vase with dried stems, and a small bowl.
  const bookColors = [0xd9cfbf, 0x9a6b52, 0x6f7a68];
  let by = top;
  bookColors.forEach((c, i) => {
    const b = box(0.3 - i * 0.03, 0.035 + (i % 2) * 0.01, 0.22 - i * 0.015, 0.004, new THREE.MeshStandardMaterial({ color: c, roughness: 0.85 }), 2);
    b.position.set(TV.x + 0.55, by + b.geometry.parameters.height / 2, cred.position.z - 0.02); b.rotation.y = (i - 1) * 0.08;
    by += b.geometry.parameters.height; scene.add(b);
  });
  // A stoneware bowl with a real wall thickness and a small foot.
  const bowlPts = [[0, 0], [0.03, 0], [0.032, 0.006], [0.05, 0.012], [0.07, 0.028], [0.083, 0.048], [0.085, 0.052], [0.079, 0.052], [0.075, 0.044], [0.062, 0.028], [0.04, 0.017], [0, 0.014]];
  const bowl = new THREE.Mesh(new THREE.LatheGeometry(bowlPts.map(([x, y]) => new THREE.Vector2(x, y)), 64), new THREE.MeshPhysicalMaterial({ color: 0x3c3732, roughness: 0.62, clearcoat: 0.25, clearcoatRoughness: 0.5 }));
  bowl.position.set(TV.x + 0.55, by, cred.position.z - 0.02); bowl.castShadow = bowl.receiveShadow = true;
  scene.add(bowl);
  const vase = new THREE.Mesh(
    new THREE.LatheGeometry([[0, 0], [0.05, 0.003], [0.075, 0.07], [0.07, 0.15], [0.035, 0.22], [0.03, 0.26], [0.036, 0.275], [0, 0.275]].map(([x, y]) => new THREE.Vector2(x, y)), 40),
    new THREE.MeshStandardMaterial({ color: 0xe9e2d6, roughness: 0.75 }),
  );
  vase.scale.setScalar(2.1);
  const vaseTop = 0.275 * 2.1 - 0.04;
  vase.position.set(TV.x - 1.22, 0, 0.3); vase.castShadow = vase.receiveShadow = true;
  scene.add(vase);
  // A small candle on the credenza.
  // A candle in a matte ceramic tumbler: a rounded rim, a wax top a little below it, and a wick.
  const cup = [[0, 0], [0.04, 0], [0.045, 0.004], [0.046, 0.086], [0.044, 0.09], [0.04, 0.088], [0.039, 0.074], [0, 0.074]];
  const candle = new THREE.Mesh(new THREE.LatheGeometry(cup.map(([x, y]) => new THREE.Vector2(x, y)), 64), new THREE.MeshStandardMaterial({ color: 0x6d6258, roughness: 0.72 }));
  candle.position.set(TV.x - 0.6, top, cred.position.z - 0.02); candle.castShadow = candle.receiveShadow = true;
  const wax = new THREE.Mesh(new THREE.CircleGeometry(0.0395, 48), new THREE.MeshStandardMaterial({ color: 0xece3d2, roughness: 0.5 }));
  wax.rotation.x = -Math.PI / 2; wax.position.y = 0.0745; candle.add(wax);
  const wick = new THREE.Mesh(new THREE.CylinderGeometry(0.0012, 0.0012, 0.012, 6), new THREE.MeshStandardMaterial({ color: 0x1c1813 }));
  wick.position.y = 0.08; candle.add(wick);
  scene.add(candle);
  // A room sensor (light and presence), like an ESPHome puck: white, rounded, with a green status light.
  const sensor = box(0.09, 0.05, 0.07, 0.016, new THREE.MeshStandardMaterial({ color: 0xf4f1ec, roughness: 0.45 }), 4);
  sensor.position.set(SENSOR.x, top + 0.025, SENSOR.z); sensor.rotation.y = -0.12; sensor.castShadow = true;
  scene.add(sensor);
  const lens = new THREE.Mesh(new THREE.SphereGeometry(0.014, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2), new THREE.MeshStandardMaterial({ color: 0x2c2a28, roughness: 0.15 }));
  lens.rotation.x = Math.PI / 2; lens.position.set(-0.012, 0.004, 0.035); sensor.add(lens);
  const led = new THREE.Mesh(new THREE.SphereGeometry(0.0045, 12, 8), ledMaterial);
  led.position.set(0.026, 0.008, 0.035); sensor.add(led);
  const stemMat = new THREE.MeshStandardMaterial({ color: 0x9c8466, roughness: 0.9 });
  const rs = random(9);
  for (let i = 0; i < 7; i++) {
    const len = 0.7 + rs() * 0.35, a = -0.2 + (rs() - 0.5) * 0.6, b = (rs() - 0.5) * 0.4;
    const curve = new THREE.QuadraticBezierCurve3(
      new THREE.Vector3(0, 0, 0), new THREE.Vector3(Math.sin(a) * len * 0.3, len * 0.55, Math.sin(b) * len * 0.2),
      new THREE.Vector3(Math.sin(a) * len * 0.75, len, Math.sin(b) * len * 0.5),
    );
    const stem = new THREE.Mesh(new THREE.TubeGeometry(curve, 16, 0.0025, 5), stemMat);
    stem.position.set(vase.position.x, vaseTop, vase.position.z); stem.castShadow = true;
    scene.add(stem);
    // Small seed heads along the top of each stem.
    for (let j = 0; j < 5; j++) {
      const t = 0.62 + j * 0.08, p = curve.getPoint(t);
      const seed = new THREE.Mesh(new THREE.SphereGeometry(0.008 + rs() * 0.006, 8, 6), stemMat);
      seed.scale.y = 1.6; seed.position.set(vase.position.x + p.x + (rs() - 0.5) * 0.02, vaseTop + p.y, vase.position.z + p.z);
      scene.add(seed);
    }
  }

  // ----- Snake plant in the right corner. -----
  const potMat = new THREE.MeshStandardMaterial({ color: 0xd6cbbb, roughness: 0.85 });
  const pot = new THREE.Mesh(
    new THREE.LatheGeometry([[0, 0], [0.17, 0], [0.2, 0.02], [0.215, 0.36], [0.205, 0.38], [0.19, 0.38], [0.19, 0.34], [0, 0.34]].map(([x, y]) => new THREE.Vector2(x, y)), 48),
    potMat,
  );
  const plantX = TV.x + 1.5, plantZ = 0.42;
  pot.position.set(plantX, 0, plantZ); pot.castShadow = pot.receiveShadow = true;
  const soil = new THREE.Mesh(new THREE.CircleGeometry(0.19, 32), new THREE.MeshStandardMaterial({ color: 0x3b2e24, roughness: 1 }));
  soil.rotation.x = -Math.PI / 2; soil.position.set(plantX, 0.345, plantZ);
  scene.add(pot, soil);
  const leafMat = new THREE.MeshStandardMaterial({ map: leafTexture(), roughness: 0.55, side: THREE.DoubleSide });
  const rl = random(17);
  for (let i = 0; i < 15; i++) {
    const hgt = 0.55 + rl() * 0.5, wid = 0.05 + rl() * 0.03, segs = 18;
    const g = new THREE.PlaneGeometry(1, 1, 4, segs);
    const bend = (rl() - 0.5) * 0.25;
    displace(g, v => {
      const t = v.y + 0.5, profile = Math.pow(Math.sin(Math.PI * Math.min(1, t * 0.95 + 0.05)), 0.55) * (1 - t * 0.35);
      const xx = v.x * wid * profile;
      v.set(xx, t * hgt, -Math.abs(v.x) * wid * 0.5 * profile + bend * t * t * hgt);
    });
    const leaf = new THREE.Mesh(g, leafMat); leaf.castShadow = true;
    const ang = rl() * Math.PI * 2, rad = rl() * 0.12;
    leaf.position.set(plantX + Math.cos(ang) * rad, 0.33, plantZ + Math.sin(ang) * rad);
    leaf.rotation.set((rl() - 0.5) * 0.25, rl() * Math.PI, (rl() - 0.5) * 0.25);
    scene.add(leaf);
  }

  // Contact shadows under furniture.
  const blob = softShadow(0.36, 0.36, 0.09);
  const under = (w, d, x, z, o, y) => { const m = shadowDecal(w, d, blob, o, y); m.position.x = x; m.position.z = z; scene.add(m); };
  under(cw + 0.25, cdp + 0.25, cred.position.x, cred.position.z, 0.5);
  under(bedLen + 0.3, bedW + 0.3, x0 + 0.1 + bedLen / 2, bedZ, 0.55, 0.014);
  under(sd + 0.2, sw + 0.2, stand.position.x, stand.position.z, 0.45);
  under(0.6, 0.6, plantX, plantZ, 0.45);
  under(0.4, 0.4, TV.x - 1.22, 0.3, 0.4);
  // Small objects on the credenza.
  const credZ = cred.position.z - 0.02, credY = top + 0.0015;
  under(0.42, 0.32, TV.x + 0.55, credZ, 0.42, credY);
  under(0.17, 0.17, TV.x - 0.6, credZ, 0.5, credY);
  under(0.15, 0.13, SENSOR.x, SENSOR.z, 0.45, credY);
}

// The Frame TV: a thin black frame flush to the wall. The screen mixes three looks in one shader:
// off (dark glass with a faint sheen), the Morning layout, and just the art.
function buildTV(scene, busy, art, maxAniso) {
  const group = new THREE.Group();
  // Screen size, the thin dark line around it, and the walnut frame's width and depth.
  const w = TV.width, h = (w * 9) / 16, line = 0.004, d = 0.03;
  const iw = w + 2 * line, ih = h + 2 * line;
  const black = new THREE.MeshStandardMaterial({ color: 0x0d0b09, roughness: 0.5 });
  const backPanel = new THREE.Mesh(new THREE.BoxGeometry(iw + 0.01, ih + 0.01, d - 0.006), black);
  backPanel.position.z = (d - 0.006) / 2; backPanel.castShadow = true; group.add(backPanel);
  // A gallery moulding swept around the screen. The profile runs from the sight edge (u = 0, next to
  // the screen) out to the wall side (u = F), with z toward the room: a stepped sight edge, a bead
  // with a soft gilt tone, a cove, and a rounded outer lip. Each side is its own strip, so the
  // corners are true miters that meet on the same points (no gaps).
  const P = [];
  const add = (u, z, gilt = 0, hard = false) => P.push({ u, z, gilt, hard });
  const arc = (cu, cz, r, a0, a1, steps, gilt = 0) => {
    for (let i = 0; i <= steps; i++) { const t = a0 + (a1 - a0) * (i / steps); add(cu + r * Math.cos(t), cz + r * Math.sin(t), gilt); }
  };
  const F = 0.062;
  add(0, 0.022, 0, true); add(0, 0.033, 0, true); // the sight edge faces in
  add(0.005, 0.033, 0, true); add(0.005, 0.036, 1, true); // a small flat step
  arc(0.0095, 0.036, 0.0045, Math.PI, 0, 10, 1); // the bead
  P[P.length - 1].hard = true;
  add(0.016, 0.034, 0, true); // a fillet
  arc(0.016, 0.051, 0.017, -Math.PI / 2, -0.06, 10); // the cove: hollow, curving up and out
  add(0.04, 0.0505); // a short flat
  arc(0.0515, 0.04, 0.0105, Math.PI / 2, 0, 12); // the rounded outer lip
  P[P.length - 1].hard = true;
  add(F, 0.002, 0, true); // the outer edge, down to the wall
  add(F - 0.004, 0, 0, true);
  // The profile's length so far, for the v texture coordinate.
  let run = 0;
  P.forEach((p, i) => { if (i) run += Math.hypot(p.u - P[i - 1].u, p.z - P[i - 1].z); p.v = run; });
  const grain = walnut(), bump = walnutNormal();
  for (const t of [grain, bump]) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(1 / 0.7, 1 / 0.7); }
  const wood = new THREE.MeshPhysicalMaterial({
    map: grain, normalMap: bump, normalScale: new THREE.Vector2(0.45, 0.45), vertexColors: true,
    roughness: 0.45, clearcoat: 0.25, clearcoatRoughness: 0.4, color: 0xfff0e0,
  });
  const gilt = new THREE.Color(1.55, 1.28, 0.86), plain = new THREE.Color(1, 1, 1);
  // Sides: inner corner A to B, and the outward direction of that side.
  const hw = iw / 2, hh = ih / 2;
  const sides = [[[-hw, hh], [hw, hh], [0, 1]], [[hw, -hh], [-hw, -hh], [0, -1]], [[hw, hh], [hw, -hh], [1, 0]], [[-hw, -hh], [-hw, hh], [-1, 0]]];
  for (const [[ax, ay], [bx, by], [ox, oy]] of sides) {
    const pos = [], uv = [], col = [], idx = [];
    const len = Math.hypot(bx - ax, by - ay), dx = (bx - ax) / len, dy = (by - ay) / len;
    // Duplicate points at hard edges so the normals stay crisp there.
    const rows = [];
    P.forEach((p, i) => { rows.push(p); if (p.hard && i > 0 && i < P.length - 1) rows.push(p); });
    rows.forEach(p => {
      for (const end of [0, 1]) {
        // Miter: the corner moves out along both the side and the outward direction.
        const sx = end ? bx + dx * p.u : ax - dx * p.u, sy = end ? by + dy * p.u : ay - dy * p.u;
        pos.push(sx + ox * p.u, sy + oy * p.u, p.z);
        uv.push((end ? len + p.u : -p.u) / 0.7, p.v / 0.7 + (ox ? 0.37 : 0));
        const c = p.gilt ? gilt : plain; col.push(c.r, c.g, c.b);
      }
    });
    for (let r = 0; r < rows.length - 1; r++) {
      if (rows[r] === rows[r + 1]) continue; // the crease between duplicated points
      const i = r * 2;
      idx.push(i, i + 2, i + 1, i + 1, i + 2, i + 3);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
    g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    // Face the room: if the lip's top points away from the viewer, flip the winding.
    const lip = rows.findIndex(p => p.u > 0.05 && p.z > 0.05) * 2;
    if (g.attributes.normal.getZ(Math.max(0, lip)) < 0) { g.setIndex(idx.map((_, k) => idx[k - (k % 3) + 2 - (k % 3)])); g.computeVertexNormals(); }
    g.computeTangents();
    const piece = new THREE.Mesh(g, wood);
    piece.castShadow = piece.receiveShadow = true;
    group.add(piece);
  }
  const texture = image => {
    const t = new THREE.Texture(image);
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = maxAniso;
    t.minFilter = THREE.LinearMipmapLinearFilter;
    t.generateMipmaps = true;
    t.needsUpdate = true;
    return t;
  };
  const uniforms = {
    busy: { value: texture(busy) }, art: { value: texture(art) },
    mixArt: { value: 0 }, power: { value: 0 }, bright: { value: 1 },
  };
  const material = new THREE.ShaderMaterial({
    uniforms, toneMapped: false,
    vertexShader: 'varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: `
      uniform sampler2D busy; uniform sampler2D art; uniform float mixArt; uniform float power; uniform float bright;
      varying vec2 vUv;
      void main() {
        vec3 content = mix(texture2D(busy, vUv).rgb, texture2D(art, vUv).rgb, mixArt) * bright;
        // Off: dark glass with a soft diagonal sheen, like the room reflected in a matte screen.
        float band = 1.0 - clamp(abs(vUv.x * 0.7 + vUv.y * 0.7 - 1.0) * 2.2, 0.0, 1.0);
        vec3 off = vec3(0.006, 0.006, 0.007) + vec3(0.016, 0.015, 0.014) * band * band + 0.005 * vUv.y;
        // On: the picture fades up from black with a short, gentle bloom.
        float rise = smoothstep(0.0, 1.0, power);
        float bloom = sin(3.14159 * clamp(power, 0.0, 1.0)) * 0.22;
        vec3 color = mix(off, content * (1.0 + bloom) + bloom * 0.04, rise * rise);
        gl_FragColor = vec4(color, 1.0);
        #include <colorspace_fragment>
      }`,
  });
  const screen = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material);
  screen.position.z = d - 0.004;
  group.add(screen);
  // A soft shadow on the wall around the TV.
  const halo = new THREE.Mesh(new THREE.PlaneGeometry((iw + 0.128) * 1.25, (ih + 0.128) * 1.3), new THREE.MeshBasicMaterial({ color: 0x000000, alphaMap: softShadow(0.34, 0.3, 0.07), transparent: true, opacity: 0.28, depthWrite: false }));
  halo.position.set(0.02, -0.03, 0.001); halo.renderOrder = 1;
  group.add(halo);
  group.position.set(TV.x, TV.y, 0);
  scene.add(group);
  return uniforms;
}

// A simple modern wall clock: walnut rim, off-white face, thin black hands and marks.
function buildClock(scene) {
  const { radius: R } = CLOCK, depth = 0.035;
  const group = new THREE.Group();
  const rim = new THREE.Mesh(new THREE.CylinderGeometry(R, R, depth, 64), new THREE.MeshStandardMaterial({ color: 0x5b3f2c, roughness: 0.5 }));
  rim.rotation.x = Math.PI / 2; rim.position.z = depth / 2; rim.castShadow = rim.receiveShadow = true;
  const face = new THREE.Mesh(new THREE.CircleGeometry(R - 0.011, 64), new THREE.MeshStandardMaterial({ color: 0xf5f1ea, roughness: 0.85 }));
  face.position.z = depth + 0.0005; face.receiveShadow = true;
  group.add(rim, face);
  const ink = new THREE.MeshStandardMaterial({ color: 0x23211e, roughness: 0.5 });
  for (let i = 0; i < 12; i++) {
    const major = i % 3 === 0;
    const mark = new THREE.Mesh(new THREE.BoxGeometry(major ? 0.006 : 0.003, major ? 0.026 : 0.016, 0.001), ink);
    const a = (i / 12) * Math.PI * 2, r = R - 0.03 - (major ? 0.005 : 0);
    mark.position.set(Math.sin(a) * r, Math.cos(a) * r, depth + 0.001);
    mark.rotation.z = -a;
    group.add(mark);
  }
  const hand = (len, width, z) => {
    const g = new THREE.BoxGeometry(width, len, 0.002);
    g.translate(0, len / 2 - 0.018, 0);
    const m = new THREE.Mesh(g, ink); m.position.z = z; m.castShadow = true; group.add(m);
    return m;
  };
  const hourHand = hand(R * 0.52, 0.008, depth + 0.004), minuteHand = hand(R * 0.78, 0.005, depth + 0.007);
  const cap = new THREE.Mesh(new THREE.CylinderGeometry(0.008, 0.008, 0.006, 20), ink);
  cap.rotation.x = Math.PI / 2; cap.position.z = depth + 0.009;
  group.add(cap);
  group.position.set(CLOCK.x, CLOCK.y, 0);
  scene.add(group);
  const shadow = new THREE.Mesh(new THREE.PlaneGeometry(R * 2.7, R * 2.7), new THREE.MeshBasicMaterial({ color: 0x000000, alphaMap: softShadow(0.2, 0.2, 0.09), transparent: true, opacity: 0.22, depthWrite: false }));
  shadow.position.set(CLOCK.x + 0.006, CLOCK.y - 0.01, 0.001); shadow.renderOrder = 1;
  scene.add(shadow);
  return minutes => {
    hourHand.rotation.z = -((minutes / 60) % 12) / 12 * Math.PI * 2;
    minuteHand.rotation.z = -(minutes % 60) / 60 * Math.PI * 2;
  };
}

export function webglAvailable() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch { return false; }
}

// stage: the hero element the canvas fills. busy, art: the loaded <img> elements for the Morning
// layout and for just the art. compact: a phone, so draw fewer pixels and a smaller shadow map.
// Returns null when WebGL fails.
export function createHeroRoom({ stage, busy, art, dark = false, compact = false }) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
  } catch { return null; }
  renderer.setPixelRatio(Math.min(compact ? 1.5 : 2, window.devicePixelRatio || 1));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  // Neutral tone mapping keeps the walnut brown in bright sun (ACES turned it grey). The screen opts
  // out (toneMapped: false), so the picture keeps its true colors.
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = dark ? 0.42 : 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.shadowMap.autoUpdate = false;
  renderer.shadowMap.needsUpdate = true;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(dark ? 0x221f1b : 0xebe5db);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = dark ? 0.22 : 0.5;
  pmrem.dispose();

  // Morning sun through the window on the left wall. Its patch of light lands on the back wall near
  // the TV and moves with the time: low and warm at 6:45, higher and whiter by 9:40.
  const sun = new THREE.DirectionalLight(0xffffff, 1);
  sun.castShadow = true;
  sun.shadow.mapSize.setScalar(compact ? 1024 : 4096);
  Object.assign(sun.shadow.camera, { left: -5, right: 5, top: 5, bottom: -5, near: 0.5, far: 24 });
  sun.shadow.radius = 5;
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.02;
  sun.target.position.set(-1, 1, 1.5);
  scene.add(sun, sun.target);
  // Soft daylight that fills the room (no shadows; stands in for bounce light).
  // A hemisphere light, not a directional one, so it adds no shiny highlight across the walnut.
  const fill = new THREE.HemisphereLight(0xfff2e4, 0xcfb79a, dark ? 0.12 : 0.75);
  scene.add(fill);
  const warm = new THREE.Color(0xffa066), white = new THREE.Color(0xffead4);
  const dawnSky = new THREE.Color(0xf3d2bc), daySky = new THREE.Color(0xdbe8f4);
  let sunKey = -1;
  const setSun = minutes => {
    const s = smooth(clamp01((minutes - 405) / 175)); // 6:45 → 9:40 AM
    if (s === sunKey) return;
    sunKey = s;
    const elevation = THREE.MathUtils.degToRad(4 + 12 * s), azimuth = THREE.MathUtils.degToRad(20 + 11 * s);
    const dir = new THREE.Vector3(Math.cos(azimuth) * Math.cos(elevation), -Math.sin(elevation), -Math.sin(azimuth) * Math.cos(elevation));
    sun.position.copy(sun.target.position).addScaledVector(dir, -12);
    sun.color.copy(warm).lerp(white, s);
    sun.intensity = (dark ? 0.4 : 1.9) + s * (dark ? 0.4 : 1.5);
    skyMaterial.color.copy(dawnSky).lerp(daySky, s).multiplyScalar(dark ? 0.6 : 1);
    scene.environmentIntensity = (dark ? 0.18 : 0.4) + s * (dark ? 0.06 : 0.14);
    renderer.shadowMap.needsUpdate = true;
  };

  buildRoom(scene, dark);
  const screen = buildTV(scene, busy, art, renderer.capabilities.getMaxAnisotropy());
  const setClock = buildClock(scene);

  // The camera starts back in the room and ends looking straight at the wall, with the TV large and
  // the clock beside it in frame. A tall, narrow stage (a phone) starts with a wider lens, so the room
  // still reads, and ends closer, with just the TV and the clock across the screen.
  const FOV_END = 36;
  let fovStart = 46;
  const camera = new THREE.PerspectiveCamera(fovStart, 1, 0.05, 30);
  const startPos = new THREE.Vector3(1.6, 1.38, 5.6), startTarget = new THREE.Vector3(-0.85, 0.78, 0.5);
  const endTarget = new THREE.Vector3(TV.x + 0.22, TV.y - 0.15, 0), endPos = new THREE.Vector3();
  const pos = new THREE.Vector3(), target = new THREE.Vector3();

  const canvas = renderer.domElement;
  canvas.setAttribute('aria-hidden', 'true');
  let size = '', last = '', move = 0, minutes = 405, focus = 0;
  const sensorPos = new THREE.Vector3(), sensorTarget = new THREE.Vector3();

  // The TV turns on and off over a short time, not with the scroll, so it reads as one event.
  let power = 0, animating = false, prev = 0;
  const step = now => {
    const goal = minutes >= TV_ON ? 1 : 0, dt = Math.min(0.1, (now - prev) / 1000);
    prev = now;
    power = goal > power ? Math.min(1, power + dt / 1.2) : Math.max(0, power - dt / 0.35);
    draw(true);
    if (power !== goal) requestAnimationFrame(step); else animating = false;
  };

  const draw = (force = false) => {
    const W = stage.clientWidth, H = stage.clientHeight;
    if (!W || !H) return;
    const sizeKey = `${W}x${H}`;
    if (sizeKey !== size) {
      size = sizeKey; renderer.setSize(W, H, false); camera.aspect = W / H; force = true;
      // At least 58 degrees across at the start.
      fovStart = Math.max(46, THREE.MathUtils.radToDeg(2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(29)) / camera.aspect)));
    }
    const key = `${move}|${minutes}|${focus}`;
    if (!force && key === last) return;
    last = key;
    setSun(minutes);
    setClock(minutes);
    screen.power.value = power;
    screen.mixArt.value = smooth(clamp01((minutes - ART_FROM) / (ART_TO - ART_FROM)));
    // End pose: far enough back to show 2.5 m of wall across (2.2 m on a tall stage: the TV and the
    // clock), and at least 1.5 m top to bottom.
    const half = Math.tan(THREE.MathUtils.degToRad(FOV_END / 2));
    const dist = Math.max((camera.aspect < 1 ? 1.1 : 1.25) / (half * camera.aspect), 0.75 / half);
    endPos.set(endTarget.x, endTarget.y, dist);
    const k = easeInOut(move), ka = smooth(move);
    pos.lerpVectors(startPos, endPos, k);
    pos.y += Math.sin(Math.PI * k) * 0.05; // a slight rise, like a person walking up
    target.lerpVectors(startTarget, endTarget, Math.max(k, ka));
    camera.fov = fovStart + (FOV_END - fovStart) * k;
    // The last shot: down and over to the room sensor, with the TV's lower edge still in view.
    if (focus > 0) {
      const f = easeInOut(focus), back = camera.aspect < 1 ? 1.1 : 1.1;
      sensorTarget.set(SENSOR.x - 0.08, SENSOR.y + 0.2, SENSOR.z);
      sensorPos.set(SENSOR.x + 0.2 * back, SENSOR.y + 0.36, SENSOR.z + back);
      pos.lerp(sensorPos, f); target.lerp(sensorTarget, f);
      // The status light blinks as it reads the room, and the screen brightens a little with the sun.
      ledMaterial.color.copy(ledColor).multiplyScalar(0.45 + 0.55 * (0.5 + 0.5 * Math.cos(focus * Math.PI * 8)));
      screen.bright.value = 1 + 0.12 * f;
    } else { ledMaterial.color.copy(ledColor); screen.bright.value = 1; }
    camera.position.copy(pos);
    camera.lookAt(target);
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);
    // Where the TV's frame is on the canvas, so the page can place text beside it.
    const fw = TV.width / 2 + 0.068, fh = (TV.width * 9) / 32 + 0.068;
    const a = corner.set(TV.x - fw, TV.y + fh, 0.03).project(camera), ax = a.x, ay = a.y;
    const b = corner.set(TV.x + fw, TV.y - fh, 0.03).project(camera);
    tvRect = { left: (ax + 1) / 2 * W, top: (1 - ay) / 2 * H, right: (b.x + 1) / 2 * W, bottom: (1 - b.y) / 2 * H };
    // Points the page's callouts attach to. (The vector is reused, so read these after the corners.)
    const at = (x, y, z) => { const c = corner.set(x, y, z).project(camera); return { x: (c.x + 1) / 2 * W, y: (1 - c.y) / 2 * H }; };
    const sw = TV.width, sh = (TV.width * 9) / 16, onScreen = (u, v) => at(TV.x + (u - 0.5) * sw, TV.y + (0.5 - v) * sh, 0.027);
    points = { calendar: onScreen(0.69, 0.71), art: onScreen(0.4, 0.26), sensor: at(SENSOR.x, SENSOR.y + 0.005, SENSOR.z + 0.01) };
  };
  const corner = new THREE.Vector3();
  let tvRect = null, points = null;

  return {
    // The TV frame's box on the canvas in CSS pixels, from the last render.
    tvRect: () => tvRect,
    // Callout points on the canvas in CSS pixels, from the last render: calendar, art and sensor.
    points: () => points,
    canvas,
    // t: how far the camera has moved in (0 to 1). time: minutes after midnight. toSensor: how far
    // the last shot has moved to the room sensor (0 to 1).
    render(t, time, toSensor = 0) {
      move = clamp01(t); minutes = time; focus = clamp01(toSensor);
      const goal = minutes >= TV_ON ? 1 : 0;
      // The first frame starts in the right state (a reload can land mid-morning); later changes animate.
      if (!last) power = goal;
      if (power !== goal && !animating) { animating = true; prev = performance.now(); requestAnimationFrame(step); }
      draw();
    },
    dispose() { renderer.dispose(); renderer.forceContextLoss(); canvas.remove(); },
  };
}
