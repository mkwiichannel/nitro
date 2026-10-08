/* Mii part pictures taken straight from the game's own resource file
 * (RFL_Res.dat, an "FFRA" archive of zlib-compressed textures and meshes).
 *
 * Nothing is rendered by WebGL and nothing is pre-generated: the eye / brow /
 * mouth / nose / glasses / mustache pictures are the game's own part textures,
 * hair / face shape / beard are the game's own meshes drawn with a tiny
 * software rasteriser. Colours are applied on the fly, so a colour change shows
 * instantly. Only the entries that are actually shown are ever decompressed.
 *
 * FFRA layout (big endian): 16-byte table entries from 0x40, one per "slot":
 *   u32 dataPos, u32 expandedSize, u32 compressedSize, u8[4] zlib parameters
 * Textures end with a 12-byte footer (u32 mipOffset, u16 w, u16 h, u8 mips,
 * u8 format 0=R8 1=RG8 2=RGBA8); meshes start with a 0x48-byte header.
 */
(function (global) {
  "use strict";

  /* first slot of every part kind in the bundled resource */
  const SLOT = {
    eye: 135, brow: 215, glass: 267 /* + type (1..8) */, mouth: 289, mustache: 341 /* + type */,
    nose: 347, faceline: 243, facemake: 256, face: 636, hair: 697, beard: 368 /* + type */
  };

  let res = null, view = null;
  const inflated = new Map(), textures = new Map(), meshes = new Map(), rasters = new Map();

  function slotEntry(slot) {
    const o = 0x40 + slot * 16;
    if (o + 16 > view.byteLength) return null;
    const pos = view.getUint32(o), csize = view.getUint32(o + 8);
    if (!pos || !csize || pos + csize > res.length) return null;
    return { pos, csize };
  }

  function inflate(slot) {
    let p = inflated.get(slot);
    if (p) return p;
    const e = slotEntry(slot);
    if (!e) { p = Promise.resolve(null); }
    else {
      p = new Response(new Blob([res.subarray(e.pos, e.pos + e.csize)]).stream()
        .pipeThrough(new DecompressionStream("deflate"))).arrayBuffer()
        .then(b => new Uint8Array(b)).catch(() => null);
    }
    inflated.set(slot, p);
    return p;
  }

  async function texture(slot) {
    if (textures.has(slot)) return textures.get(slot);
    const u8 = await inflate(slot);
    let t = null;
    if (u8 && u8.length > 12) {
      const n = u8.length, dv = new DataView(u8.buffer, u8.byteOffset, n);
      const w = dv.getUint16(n - 8), h = dv.getUint16(n - 6), fmt = u8[n - 3];
      const bpp = [1, 2, 4][fmt];
      if (bpp && w >= 16 && h >= 16 && w * h * bpp <= n) t = { w, h, bpp, d: u8.subarray(0, w * h * bpp) };
    }
    textures.set(slot, t);
    return t;
  }

  async function mesh(slot) {
    if (meshes.has(slot)) return meshes.get(slot);
    const u8 = await inflate(slot);
    let m = null;
    if (u8 && u8.length > 0x48) {
      const dv = new DataView(u8.buffer, u8.byteOffset, u8.length);
      const posOff = dv.getUint32(0), idxOff = dv.getUint32(20);
      const nv = dv.getUint32(24) / 16, ni = dv.getUint32(44);
      if (posOff >= 0x48 && posOff < 0x200 && nv > 2 && ni > 2 && idxOff + ni * 2 <= u8.length) {
        const P = new Float32Array(nv * 3);
        let ok = true;
        for (let i = 0; i < nv; i++) {
          for (let k = 0; k < 3; k++) {
            const v = dv.getFloat32(posOff + i * 16 + k * 4);
            if (!isFinite(v)) ok = false;
            P[i * 3 + k] = v;
          }
        }
        const I = new Uint16Array(ni);
        for (let i = 0; i < ni; i++) { I[i] = dv.getUint16(idxOff + i * 2); if (I[i] >= nv) ok = false; }
        if (ok) {
          /* smooth vertex normals from the triangles */
          const N = new Float32Array(nv * 3);
          for (let t = 0; t + 2 < ni; t += 3) {
            const a = I[t] * 3, b = I[t + 1] * 3, c = I[t + 2] * 3;
            const ux = P[b] - P[a], uy = P[b + 1] - P[a + 1], uz = P[b + 2] - P[a + 2];
            const vx = P[c] - P[a], vy = P[c + 1] - P[a + 1], vz = P[c + 2] - P[a + 2];
            const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
            for (const q of [a, b, c]) { N[q] += nx; N[q + 1] += ny; N[q + 2] += nz; }
          }
          for (let i = 0; i < nv; i++) {
            const l = Math.hypot(N[i * 3], N[i * 3 + 1], N[i * 3 + 2]) || 1;
            N[i * 3] /= l; N[i * 3 + 1] /= l; N[i * 3 + 2] /= l;
          }
          m = { nv, P, N, I };
        }
      }
    }
    meshes.set(slot, m);
    return m;
  }

  /* ---------- colour helpers ---------- */
  function rgb(hex) {
    const h = String(hex || "#000000").replace("#", "");
    return [parseInt(h.substr(0, 2), 16) || 0, parseInt(h.substr(2, 2), 16) || 0, parseInt(h.substr(4, 2), 16) || 0];
  }
  const clamp = v => v < 0 ? 0 : v > 255 ? 255 : v;

  /* alpha bounding box of a texture-derived ImageData (cached by caller) */
  function bbox(img, thr) {
    const { width: w, height: h, data } = img;
    let x0 = w, y0 = h, x1 = -1, y1 = -1;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      if (data[(y * w + x) * 4 + 3] > thr) {
        if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
    }
    return x1 < 0 ? null : { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 };
  }

  function toCanvas(img) {
    const c = document.createElement("canvas");
    c.width = img.width; c.height = img.height;
    c.getContext("2d").putImageData(img, 0, 0);
    return c;
  }

  /* draw a layer so its content box is centred in the tile at a fixed scale */
  function place(ctx, layer, box, scale, cw, ch, dy) {
    const dw = box.w * scale, dh = box.h * scale;
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    ctx.drawImage(layer, box.x, box.y, box.w, box.h, (cw - dw) / 2, (ch - dh) / 2 + (dy || 0), dw, dh);
  }

  /* ---------- texture based parts ---------- */
  function layered(t, cols) {
    /* generic: out = sum(channel * colour), alpha from the A channel (RGBA) */
    const out = new ImageData(t.w, t.h), o = out.data, d = t.d;
    for (let p = 0, n = t.w * t.h; p < n; p++) {
      const r = d[p * 4] / 255, g = d[p * 4 + 1] / 255, b = d[p * 4 + 2] / 255;
      o[p * 4] = clamp(r * cols[0][0] + g * cols[1][0] + b * cols[2][0]);
      o[p * 4 + 1] = clamp(r * cols[0][1] + g * cols[1][1] + b * cols[2][1]);
      o[p * 4 + 2] = clamp(r * cols[0][2] + g * cols[1][2] + b * cols[2][2]);
      o[p * 4 + 3] = d[p * 4 + 3];
    }
    return out;
  }
  function mono(t, col, mirror, alphaMul) {
    /* single channel alpha mask in one colour; optionally mirrored to a full width */
    const W = mirror ? t.w * 2 : t.w, out = new ImageData(W, t.h), o = out.data;
    for (let y = 0; y < t.h; y++) for (let x = 0; x < t.w; x++) {
      const a = t.d[(y * t.w + x) * t.bpp] * (alphaMul || 1);
      const q = (y * W + x) * 4;
      o[q] = col[0]; o[q + 1] = col[1]; o[q + 2] = col[2]; o[q + 3] = a;
      if (mirror) { const m = (y * W + (W - 1 - x)) * 4; o[m] = col[0]; o[m + 1] = col[1]; o[m + 2] = col[2]; o[m + 3] = a; }
    }
    return out;
  }

  const boxCache = new Map();
  function cachedBox(key, img, thr) {
    let b = boxCache.get(key);
    if (b === undefined) { b = bbox(img, thr); boxCache.set(key, b); }
    return b;
  }

  const WHITE = [255, 255, 255];
  const OUTLINE = "drop-shadow(1px 0 0 #3a3a3a) drop-shadow(-1px 0 0 #3a3a3a) drop-shadow(0 1px 0 #3a3a3a) drop-shadow(0 -1px 0 #3a3a3a)";

  async function drawTextureTile(cat, i, ctx, cw, ch, o) {
    if (cat === "eyes") {
      const t = await texture(SLOT.eye + i); if (!t) return;
      const img = layered(t, [WHITE, WHITE, rgb(o.eye)]);
      const b = cachedBox("e" + i, img, 8); if (!b) return;
      place(ctx, toCanvas(img), b, cw * 0.86 / 152, cw, ch);
    } else if (cat === "brows") {
      const t = await texture(SLOT.brow + i); if (!t) return;
      const img = mono(t, rgb(o.brow));
      const b = cachedBox("b" + i, img, 8); if (!b) return;
      place(ctx, toCanvas(img), b, cw * 0.9 / 144, cw, ch);
    } else if (cat === "mouth") {
      const t = await texture(SLOT.mouth + i); if (!t) return;
      const lip = rgb(o.lip), dark = lip.map(v => v * 0.8);
      const img = layered(t, [dark, lip, WHITE]);
      const b = cachedBox("m" + i, img, 8); if (!b) return;
      place(ctx, toCanvas(img), b, cw * 0.9 / 176, cw, ch);
    } else if (cat === "nose") {
      const t = await texture(SLOT.nose + i); if (!t) return;
      const img = mono(t, [26, 26, 26]);
      const b = cachedBox("n" + i, img, 8); if (!b) return;
      place(ctx, toCanvas(img), b, cw * 0.9 / 150, cw, ch);
    } else if (cat === "mustache") {
      const t = await texture(SLOT.mustache + i); if (!t) return;
      const img = mono(t, rgb(o.beard), true);
      const b = cachedBox("s" + i, img, 8); if (!b) return;
      place(ctx, toCanvas(img), b, cw * 0.9 / 150, cw, ch);
    } else if (cat === "glasses") {
      const t = await texture(SLOT.glass + i); if (!t) return;
      const col = rgb(o.glass), out = new ImageData(t.w, t.h), q = out.data, d = t.d;
      const sun = i >= 6;
      for (let p = 0, n = t.w * t.h; p < n; p++) {
        const r = d[p * 2] / 255, g = d[p * 2 + 1] / 255;
        let R, G, B, A;
        if (sun) { /* R = frame, G = tinted lens */
          const lens = Math.max(0, g - r);
          A = Math.min(1, r + lens * 0.8);
          const fr = r / Math.max(A, 0.001);
          R = col[0] * fr * 0.55 + 90 * (1 - fr); G = col[1] * fr * 0.55 + 80 * (1 - fr); B = col[2] * fr * 0.55 + 85 * (1 - fr);
        } else { R = col[0]; G = col[1]; B = col[2]; A = r; }
        q[p * 4] = clamp(R); q[p * 4 + 1] = clamp(G); q[p * 4 + 2] = clamp(B); q[p * 4 + 3] = A * 255;
      }
      const b = cachedBox("g" + i, out, 8); if (!b) return;
      place(ctx, toCanvas(out), b, cw * 0.94 / 230, cw, ch);
    }
  }

  /* ---------- mesh based parts (software rasteriser) ---------- */
  const LIGHT = (() => { const v = [0.35, 0.55, 0.76], l = Math.hypot(...v); return v.map(x => x / l); })();

  /* rasterise meshes front-on into owner/shade planes; later layers may hide earlier ones by depth */
  function raster(parts, W, H, frame) {
    const owner = new Uint8Array(W * H), shade = new Uint8Array(W * H), zb = new Float32Array(W * H).fill(-1e9);
    const sx = W / (frame[2] - frame[0]), sy = H / (frame[3] - frame[1]);
    parts.forEach((m, pi) => {
      if (!m) return;
      const px = new Float32Array(m.nv), py = new Float32Array(m.nv), it = new Float32Array(m.nv);
      for (let v = 0; v < m.nv; v++) {
        px[v] = (m.P[v * 3] - frame[0]) * sx;
        py[v] = H - (m.P[v * 3 + 1] - frame[1]) * sy;
        const nd = m.N[v * 3] * LIGHT[0] + m.N[v * 3 + 1] * LIGHT[1] + m.N[v * 3 + 2] * LIGHT[2];
        it[v] = 0.5 + 0.5 * Math.max(0, nd);
      }
      for (let t = 0; t + 2 < m.I.length; t += 3) {
        const a = m.I[t], b = m.I[t + 1], c = m.I[t + 2];
        const x0 = px[a], y0 = py[a], x1 = px[b], y1 = py[b], x2 = px[c], y2 = py[c];
        const den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2);
        if (Math.abs(den) < 1e-6) continue;
        const minX = Math.max(0, Math.floor(Math.min(x0, x1, x2))), maxX = Math.min(W - 1, Math.ceil(Math.max(x0, x1, x2)));
        const minY = Math.max(0, Math.floor(Math.min(y0, y1, y2))), maxY = Math.min(H - 1, Math.ceil(Math.max(y0, y1, y2)));
        const za = m.P[a * 3 + 2], zbv = m.P[b * 3 + 2], zc = m.P[c * 3 + 2];
        for (let y = minY; y <= maxY; y++) for (let x = minX; x <= maxX; x++) {
          const fx = x + 0.5, fy = y + 0.5;
          const l0 = ((y1 - y2) * (fx - x2) + (x2 - x1) * (fy - y2)) / den;
          const l1 = ((y2 - y0) * (fx - x2) + (x0 - x2) * (fy - y2)) / den;
          const l2 = 1 - l0 - l1;
          if (l0 < -0.001 || l1 < -0.001 || l2 < -0.001) continue;
          const z = l0 * za + l1 * zbv + l2 * zc, k = y * W + x;
          if (z > zb[k]) {
            zb[k] = z; owner[k] = pi + 1;
            shade[k] = Math.round(255 * (l0 * it[a] + l1 * it[b] + l2 * it[c]));
          }
        }
      }
    });
    return { owner, shade, W, H };
  }

  async function cachedRaster(key, slots, W, H, frame) {
    let r = rasters.get(key);
    if (!r) {
      const ms = await Promise.all(slots.map(s => s == null ? null : mesh(s)));
      r = raster(ms, W, H, frame);
      rasters.set(key, r);
      if (rasters.size > 420) rasters.delete(rasters.keys().next().value);
    }
    return r;
  }

  function colourise(r, cols) {
    /* cols[ownerIndex-1] = [r,g,b]; returns canvas */
    const img = new ImageData(r.W, r.H), o = img.data;
    for (let k = 0, n = r.W * r.H; k < n; k++) {
      const ow = r.owner[k];
      if (!ow) continue;
      const c = cols[ow - 1], s = r.shade[k] / 255;
      o[k * 4] = c[0]; o[k * 4 + 1] = c[1]; o[k * 4 + 2] = c[2]; o[k * 4 + 3] = 255; /* flat 2D, like the Mii Channel icons */
    }
    return toCanvas(img);
  }

  async function drawMeshTile(cat, i, ctx, cw, ch, o) {
    const SS = 2, W = cw * SS, H = ch * SS;
    let key, slots, frame, cols;
    if (cat === "hair") {
      key = `h${i}:${W}x${H}`; slots = [SLOT.face, SLOT.hair + i]; frame = [-50, -26, 50, 80];
      cols = [rgb(o.skin), rgb(o.hair)];
    } else if (cat === "face") {
      key = `f${i}:${W}x${H}`; slots = [SLOT.face + i]; frame = [-35, -3, 35, 67];
      cols = [rgb(o.skin)];
    } else if (cat === "beard") {
      key = `d${i}:${W}x${H}`; slots = [SLOT.beard + i]; frame = [-27, -12, 27, 33];
      cols = [rgb(o.beard)];
    } else return;
    const r = await cachedRaster(key, slots, W, H, frame);
    const cv = colourise(r, cols);
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    const T = document.createElement("canvas"); T.width = cw; T.height = ch;
    const tx = T.getContext("2d");
    tx.imageSmoothingEnabled = true; tx.imageSmoothingQuality = "high";
    if (cat === "face") { /* the face mesh is open at the top (hair covers it): close it with a rounded crown */
      const sc = cw / 70, c = cols[0];
      tx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`;
      tx.beginPath();
      tx.ellipse(cw / 2, (67 - 49.4) * sc, 26.0 * sc, 15 * sc, 0, Math.PI, 2 * Math.PI);
      tx.fill();
    }
    tx.drawImage(cv, 0, 0, cw, ch);
    ctx.save(); ctx.filter = OUTLINE; /* thin dark outline like the Mii Channel icons */
    ctx.drawImage(T, 0, 0); ctx.restore();
  }

  /* facial features: the game's make-up / wrinkle textures over a face shape */
  const FEATURE = { 0: [0, 0], 1: [0, 1], 2: [0, 3], 3: [0, 9], 4: [5, 0], 5: [2, 0], 6: [1, 0], 7: [6, 0], 8: [8, 0], 9: [0, 10], 10: [9, 0], 11: [11, 0] };
  async function drawFeatureTile(i, ctx, cw, ch, o) {
    await drawMeshTile("face", 0, ctx, cw, ch, o);
    const [wr, mk] = FEATURE[i] || [0, 0];
    const SSC = document.createElement("canvas"); SSC.width = 512; SSC.height = 512;
    const x = SSC.getContext("2d");
    const layers = [];
    if (mk) { const t = await texture(SLOT.facemake + mk); if (t && t.bpp === 4) layers.push({ t }); }
    if (wr) { const t = await texture(SLOT.faceline + wr); if (t) layers.push({ t, line: true }); }
    for (const L of layers) {
      let img;
      if (L.line) img = mono(L.t, [60, 50, 45], true, 1.6);
      else {
        img = new ImageData(L.t.w * 2, L.t.h);
        for (let y = 0; y < L.t.h; y++) for (let xx = 0; xx < L.t.w; xx++) {
          const s = (y * L.t.w + xx) * 4;
          for (const dx of [xx, L.t.w * 2 - 1 - xx]) {
            const q = (y * L.t.w * 2 + dx) * 4;
            img.data[q] = L.t.d[s]; img.data[q + 1] = L.t.d[s + 1]; img.data[q + 2] = L.t.d[s + 2]; img.data[q + 3] = L.t.d[s + 3];
          }
        }
      }
      x.drawImage(toCanvas(img), 0, 0, img.width, img.height, 0, 0, 512, 512);
    }
    /* map the 512x512 face-texture space onto the face shape (x ±26.3, y 0..50 in a 62x62 frame) */
    const fx = (35 - 26.3) / 70 * cw, fw = 52.6 / 70 * cw, fy = (67 - 51) / 70 * ch, fh = 51 / 70 * ch;
    ctx.save();
    ctx.globalCompositeOperation = "source-atop"; /* only on the face itself */
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(SSC, 0, 0, 512, 512, fx, fy, fw, fh);
    ctx.restore();
  }

  /* ---------- public API ---------- */
  const MiiParts = {
    ready: false,
    async init(resBytes) {
      if (this.ready) return true;
      if (!(resBytes instanceof Uint8Array) || resBytes.length < 0x1000 || typeof DecompressionStream !== "function") return false;
      res = resBytes; view = new DataView(res.buffer, res.byteOffset, res.byteLength);
      if (view.getUint32(0) !== 0x46465241) return false; /* "FFRA" */
      /* sanity check against the layout this code was written for */
      const e = await texture(SLOT.eye), m = await texture(SLOT.mouth);
      if (!e || e.w !== 152 || e.h !== 128 || !m || m.w !== 176) return false;
      return (this.ready = true);
    },
    /* paint option `i` of category `cat` into a canvas. o = { skin, hair, brow, eye, lip, glass, beard } hex colours */
    async draw(cat, i, canvas, o) {
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const cw = canvas.width, ch = canvas.height;
      try {
        if (cat === "hair" || cat === "face" || cat === "beard") {
          if (cat === "beard" && i === 0) return;
          if (cat === "hair" && i === 30) { /* bald: just the head */
            return drawMeshTile("face", 0, ctx, cw, ch, o);
          }
          return await drawMeshTile(cat, i, ctx, cw, ch, o);
        }
        if (cat === "feature") return await drawFeatureTile(i, ctx, cw, ch, o);
        if ((cat === "glasses" && i === 0) || (cat === "mustache" && i === 0)) return;
        const idx = cat === "glasses" || cat === "mustache" ? i : i;
        const slotOffset = cat === "glasses" ? -0 : 0;
        return await drawTextureTile(cat, idx + slotOffset, ctx, cw, ch, o);
      } catch (e) { console.warn("part", cat, i, e); }
    }
  };
  /* glasses / mustache type n lives at slot base + n (type 0 = none) */
  global.MiiParts = MiiParts;
})(window);
