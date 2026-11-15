// Living wallpaper renderer (Stonic port, Phase 5).
//
// Recovered from Stonic's renderer bundle and kept faithful to it: a slow fbm
// nebula drawn into a quarter-resolution texture (redrawn ~8×/s), blitted with
// a light dither so dark gradients don't band, then 3800 drifting stars and
// 2200 motes orbiting the character's spot. Frame rate is throttled hard —
// 20 fps when calm, 30 when the character is active, 12/24 on battery — and the
// whole thing stops when the desktop is covered.

import { nightLevel } from './presence';

const STARS = 3800;
const MOTES = 2200;
const FIELD_SCALE = 0.25;
const FIELD_EVERY_MS = 125;
const MAX_WIDTH = 3400;
const FRAME_CALM = 1000 / 20;
const FRAME_ACTIVE = 1000 / 30;
const FRAME_CALM_BATTERY = 1000 / 12;
const FRAME_ACTIVE_BATTERY = 1000 / 24;
export const DEFAULT_ACCENT: [number, number, number] = [0.2, 0.52, 1];

export interface WallpaperInputs {
  focusX: number;
  focusY: number;
  /** 0.25 resting, 0.6 thinking, 1 in conversation. */
  wake: number;
  /** The character is speaking (0..1). */
  ai: number;
  /** The user is speaking (0..1). */
  mic: number;
  battery: boolean;
}

const FIELD_FS = `
precision mediump float;
uniform vec2 uRes; uniform float uTime; uniform vec2 uFocus; uniform vec3 uAccent;
uniform float uEnergy; uniform float uWake; uniform float uNight; uniform float uFlare;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p){ vec2 i = floor(p); vec2 f = fract(p); vec2 u = f*f*(3.0-2.0*f);
  return mix(mix(hash(i), hash(i+vec2(1.0,0.0)), u.x), mix(hash(i+vec2(0.0,1.0)), hash(i+vec2(1.0,1.0)), u.x), u.y); }
float fbm(vec2 p){ float v = 0.0; float a = 0.5; for (int i = 0; i < 4; i++) { v += a*noise(p); p = p*2.03 + vec2(17.0, 9.0); a *= 0.5; } return v; }
void main(){
  vec2 uv = gl_FragCoord.xy / uRes;
  float aspect = uRes.x / uRes.y;
  vec2 p = vec2(uv.x * aspect, uv.y);
  vec2 f = vec2(uFocus.x * aspect, 1.0 - uFocus.y);
  float t = uTime * 0.018;
  vec2 q = vec2(fbm(p*1.5 + t), fbm(p*1.5 - t + 4.3));
  float n = fbm(p*1.15 + q*1.3 + vec2(t*0.7, -t*0.45));
  float d = distance(p, f);
  float core = exp(-d*d*5.0);
  float halo = exp(-d*d*38.0);
  vec3 base = mix(vec3(0.014, 0.020, 0.036), vec3(0.028, 0.044, 0.080), uv.y*0.75 + 0.1);
  vec3 col = base;
  col += uAccent * smoothstep(0.30, 0.95, n) * (0.15 + 0.12*uWake) * (0.45 + core*1.4);
  col += vec3(0.060, 0.090, 0.180) * smoothstep(0.45, 1.0, fbm(p*0.85 - q + 7.0)) * 0.55;
  col += uAccent * core * (0.085 + 0.06*uWake + 0.10*uEnergy);
  col += mix(uAccent, vec3(1.0), 0.45) * halo * (0.05 + 0.06*uEnergy + 0.22*uFlare);
  col *= mix(1.0, 0.7, uNight);
  gl_FragColor = vec4(col, 1.0);
}`;

const QUAD_UV_VS = `
attribute vec2 aPos;
varying vec2 vUv;
void main(){ vUv = aPos*0.5 + 0.5; gl_Position = vec4(aPos, 0.0, 1.0); }`;

const QUAD_VS = `
attribute vec2 aPos;
void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }`;

const BLIT_FS = `
precision mediump float;
uniform sampler2D uTex; uniform float uTime;
varying vec2 vUv;
float hash(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
void main(){
  vec3 c = texture2D(uTex, vUv).rgb;
  // Light dither so deep gradients don't band.
  c += (hash(gl_FragCoord.xy + fract(uTime)*91.0) - 0.5) / 255.0 * 1.5;
  gl_FragColor = vec4(c, 1.0);
}`;

const POINTS_VS = `
precision highp float;
attribute vec4 aData;
attribute float aKind;
uniform vec2 uRes; uniform float uTime; uniform vec2 uFocus; uniform float uWake;
uniform float uEnergy; uniform float uListen;
uniform float uPx; uniform vec3 uAccent; uniform float uNight; uniform float uFlare;
varying vec4 vCol;
void main(){
  float aspect = uRes.x / uRes.y;
  vec2 pos;
  float bright;
  float size;
  float seed = aData.w;
  if (aKind < 0.5) {
    float z = aData.z;
    vec2 p = aData.xy + vec2(uTime*0.0028*z, sin(uTime*0.045 + seed*6.2831)*0.0035);
    pos = fract(p);
    bright = 0.34 + 0.56*z;
    size = 1.3 + 2.4*z*z;
  } else {
    float r = aData.x;
    float ang = aData.y + uTime * (0.010 / (r + 0.05));
    float z = aData.z;
    vec2 o = vec2(cos(ang) * r / aspect, sin(ang) * r * 0.58);
    pos = uFocus + o;
    bright = (0.38 + 0.9*exp(-r*7.5)) * (0.6 + 0.4*uWake);
    size = 1.4 + 1.8*z;
  }
  vec2 dv = (pos - uFocus) * vec2(aspect, 1.0);
  float d = length(dv);
  float tw = 0.62 + 0.38*sin(uTime*(0.5 + seed*1.7) + seed*40.0);
  float near = exp(-d*d*18.0);
  float a = bright * tw * (1.0 + (uEnergy*0.8 + uListen*0.5 + uFlare*1.6) * near);
  a *= mix(1.0, 0.72, uNight);
  vec3 c = mix(vec3(0.80, 0.88, 1.0), uAccent, aKind > 0.5 ? 0.62 : 0.18 + 0.25*seed);
  vCol = vec4(c, a);
  gl_PointSize = size * uPx;
  gl_Position = vec4(pos.x*2.0 - 1.0, 1.0 - pos.y*2.0, 0.0, 1.0);
}`;

const POINTS_FS = `
precision mediump float;
varying vec4 vCol;
void main(){
  vec2 c = gl_PointCoord - 0.5;
  float r = length(c) * 2.0;
  if (r > 1.0) discard;
  float a = pow(1.0 - r, 1.2);
  gl_FragColor = vec4(vCol.rgb, vCol.a * a);
}`;

/** Seeded LCG — the same sky every time (seed from Stonic). Pure. */
export function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/** Star and mote attributes: [x, y, depth, seed] / [radius, angle, depth, seed]. Pure. */
export function buildField(seed = 334462) {
  const rnd = lcg(seed);
  const total = STARS + MOTES;
  const data = new Float32Array(total * 4);
  const kind = new Float32Array(total);
  for (let i = 0; i < STARS; i++) {
    data[i * 4] = rnd();
    data[i * 4 + 1] = rnd();
    data[i * 4 + 2] = 0.2 + 0.8 * Math.pow(rnd(), 1.8);
    data[i * 4 + 3] = rnd();
  }
  for (let j = 0; j < MOTES; j++) {
    const i = STARS + j;
    data[i * 4] = Math.abs((rnd() + rnd() + rnd() - 1.5) / 1.5) * 0.42 + 0.01;
    data[i * 4 + 1] = rnd() * Math.PI * 2;
    data[i * 4 + 2] = rnd();
    data[i * 4 + 3] = rnd();
    kind[i] = 1;
  }
  return { data, kind, total };
}

/** Frame budget in ms. Pure. */
export function frameBudget(active: boolean, battery: boolean): number {
  if (battery) return active ? FRAME_ACTIVE_BATTERY : FRAME_CALM_BATTERY;
  return active ? FRAME_ACTIVE : FRAME_CALM;
}

function shader(gl: WebGLRenderingContext, type: number, src: string): WebGLShader | null {
  const s = gl.createShader(type);
  if (!s) return null;
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (gl.getShaderParameter(s, gl.COMPILE_STATUS)) return s;
  console.warn('[Wallpaper] shader:', gl.getShaderInfoLog(s));
  gl.deleteShader(s);
  return null;
}

function program(gl: WebGLRenderingContext, vs: string, fs: string, attribs: string[]): WebGLProgram | null {
  const v = shader(gl, gl.VERTEX_SHADER, vs);
  const f = shader(gl, gl.FRAGMENT_SHADER, fs);
  const p = gl.createProgram();
  if (!v || !f || !p) return null;
  gl.attachShader(p, v);
  gl.attachShader(p, f);
  attribs.forEach((name, i) => gl.bindAttribLocation(p, i, name));
  gl.linkProgram(p);
  gl.deleteShader(v);
  gl.deleteShader(f);
  if (gl.getProgramParameter(p, gl.LINK_STATUS)) return p;
  console.warn('[Wallpaper] link:', gl.getProgramInfoLog(p));
  gl.deleteProgram(p);
  return null;
}

export interface WallpaperRenderer {
  setActive(active: boolean): void;
  flare(amount?: number): void;
  dispose(): void;
}

export function createWallpaper(
  canvas: HTMLCanvasElement,
  inputs: () => WallpaperInputs,
  onFirstFrame?: () => void,
): WallpaperRenderer | null {
  let gl: WebGLRenderingContext | null = null;
  try {
    gl = canvas.getContext('webgl', {
      alpha: false, antialias: false, depth: false, stencil: false,
      premultipliedAlpha: false, powerPreference: 'low-power',
    });
  } catch { gl = null; }
  if (!gl) return null;
  const g = gl;

  const field = program(g, QUAD_VS, FIELD_FS, ['aPos']);
  const blit = program(g, QUAD_UV_VS, BLIT_FS, ['aPos']);
  const points = program(g, POINTS_VS, POINTS_FS, ['aData', 'aKind']);
  if (!field || !blit || !points) return null;

  const quad = g.createBuffer();
  g.bindBuffer(g.ARRAY_BUFFER, quad);
  g.bufferData(g.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), g.STATIC_DRAW);
  const sky = buildField();
  const dataBuf = g.createBuffer();
  g.bindBuffer(g.ARRAY_BUFFER, dataBuf);
  g.bufferData(g.ARRAY_BUFFER, sky.data, g.STATIC_DRAW);
  const kindBuf = g.createBuffer();
  g.bindBuffer(g.ARRAY_BUFFER, kindBuf);
  g.bufferData(g.ARRAY_BUFFER, sky.kind, g.STATIC_DRAW);

  const locs = (p: WebGLProgram, names: string[]) =>
    Object.fromEntries(names.map((n) => [n, g.getUniformLocation(p, n)])) as Record<string, WebGLUniformLocation | null>;
  const uf = locs(field, ['uRes', 'uTime', 'uFocus', 'uAccent', 'uEnergy', 'uWake', 'uNight', 'uFlare']);
  const ub = locs(blit, ['uTex', 'uTime']);
  const up = locs(points, ['uRes', 'uTime', 'uFocus', 'uWake', 'uEnergy', 'uListen', 'uPx', 'uAccent', 'uNight', 'uFlare']);

  const tex = g.createTexture();
  const fbo = g.createFramebuffer();
  let fw = 0, fh = 0, cw = 0, ch = 0, px = 1;
  let time = 0, lastTick = 0, lastFrame = 0;
  let lastField = -Infinity, lastResize = -Infinity, lastNight = -Infinity;
  let night = nightLevel();
  let fx = 0.7, fy = 0.42, wake = 0, energy = 0, listen = 0, flare = 0;
  const accent = [...DEFAULT_ACCENT];
  let active = true, raf: number | null = null, disposed = false, first = true;

  const resize = (): boolean => {
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    const dpr = window.devicePixelRatio || 1;
    let w = Math.round(rect.width * dpr);
    let h = Math.round(rect.height * dpr);
    const over = w / MAX_WIDTH;
    if (over > 1) { w = Math.round(w / over); h = Math.round(h / over); }
    px = w / rect.width;
    if (w !== cw || h !== ch) {
      cw = w; ch = h; canvas.width = w; canvas.height = h;
      fw = Math.max(64, Math.round(w * FIELD_SCALE));
      fh = Math.max(36, Math.round(h * FIELD_SCALE));
      g.bindTexture(g.TEXTURE_2D, tex);
      g.texImage2D(g.TEXTURE_2D, 0, g.RGBA, fw, fh, 0, g.RGBA, g.UNSIGNED_BYTE, null);
      g.texParameteri(g.TEXTURE_2D, g.TEXTURE_MIN_FILTER, g.LINEAR);
      g.texParameteri(g.TEXTURE_2D, g.TEXTURE_MAG_FILTER, g.LINEAR);
      g.texParameteri(g.TEXTURE_2D, g.TEXTURE_WRAP_S, g.CLAMP_TO_EDGE);
      g.texParameteri(g.TEXTURE_2D, g.TEXTURE_WRAP_T, g.CLAMP_TO_EDGE);
      g.bindFramebuffer(g.FRAMEBUFFER, fbo);
      g.framebufferTexture2D(g.FRAMEBUFFER, g.COLOR_ATTACHMENT0, g.TEXTURE_2D, tex, 0);
      g.bindFramebuffer(g.FRAMEBUFFER, null);
      lastField = -Infinity;
    }
    return true;
  };

  const quadAttrib = () => {
    g.bindBuffer(g.ARRAY_BUFFER, quad);
    g.enableVertexAttribArray(0);
    g.vertexAttribPointer(0, 2, g.FLOAT, false, 0, 0);
    g.disableVertexAttribArray(1);
  };

  const drawField = () => {
    g.bindFramebuffer(g.FRAMEBUFFER, fbo);
    g.viewport(0, 0, fw, fh);
    g.useProgram(field);
    g.disable(g.BLEND);
    quadAttrib();
    g.uniform2f(uf.uRes, fw, fh);
    g.uniform1f(uf.uTime, time);
    g.uniform2f(uf.uFocus, fx, fy);
    g.uniform3f(uf.uAccent, accent[0], accent[1], accent[2]);
    g.uniform1f(uf.uEnergy, energy);
    g.uniform1f(uf.uWake, wake);
    g.uniform1f(uf.uNight, night);
    g.uniform1f(uf.uFlare, flare);
    g.drawArrays(g.TRIANGLES, 0, 6);
    g.bindFramebuffer(g.FRAMEBUFFER, null);
  };

  const drawScreen = () => {
    g.viewport(0, 0, cw, ch);
    g.useProgram(blit);
    g.disable(g.BLEND);
    quadAttrib();
    g.activeTexture(g.TEXTURE0);
    g.bindTexture(g.TEXTURE_2D, tex);
    g.uniform1i(ub.uTex, 0);
    g.uniform1f(ub.uTime, time);
    g.drawArrays(g.TRIANGLES, 0, 6);

    g.useProgram(points);
    g.enable(g.BLEND);
    g.blendFunc(g.SRC_ALPHA, g.ONE);
    g.bindBuffer(g.ARRAY_BUFFER, dataBuf);
    g.enableVertexAttribArray(0);
    g.vertexAttribPointer(0, 4, g.FLOAT, false, 0, 0);
    g.bindBuffer(g.ARRAY_BUFFER, kindBuf);
    g.enableVertexAttribArray(1);
    g.vertexAttribPointer(1, 1, g.FLOAT, false, 0, 0);
    g.uniform2f(up.uRes, cw, ch);
    g.uniform1f(up.uTime, time);
    g.uniform2f(up.uFocus, fx, fy);
    g.uniform1f(up.uWake, wake);
    g.uniform1f(up.uEnergy, energy);
    g.uniform1f(up.uListen, listen);
    g.uniform1f(up.uPx, px);
    g.uniform3f(up.uAccent, accent[0], accent[1], accent[2]);
    g.uniform1f(up.uNight, night);
    g.uniform1f(up.uFlare, flare);
    g.drawArrays(g.POINTS, 0, sky.total);
  };

  const frame = (now: number) => {
    raf = null;
    if (disposed || !active) return;
    raf = requestAnimationFrame(frame);
    const inp = inputs();
    const busy = energy > 0.02 || listen > 0.02 || flare > 0.01
      || Math.abs(inp.focusX - fx) > 5e-4 || Math.abs(inp.focusY - fy) > 5e-4;
    if (lastFrame && now - lastFrame < frameBudget(busy, inp.battery) - 1) return;
    const dt = lastTick ? Math.min(0.1, (now - lastTick) / 1000) : 0.016;
    lastTick = now;
    lastFrame = now;
    time += dt;
    if (now - lastResize > 1000) {
      lastResize = now;
      if (!resize()) return;
    }
    if (!cw) return;
    if (now - lastNight > 60_000) { lastNight = now; night = nightLevel(); }
    const ease = Math.min(1, dt * 2.4);
    fx += (inp.focusX - fx) * ease;
    fy += (inp.focusY - fy) * ease;
    wake += (inp.wake - wake) * Math.min(1, dt * 1.2);
    const e = inp.ai * inp.wake;
    energy += (e - energy) * Math.min(1, dt * (e > energy ? 3 : 1.5));
    const l = inp.mic * inp.wake;
    listen += (l - listen) * Math.min(1, dt * (l > listen ? 3 : 1.5));
    flare = Math.max(0, flare - dt * 0.7);
    if (now - lastField > FIELD_EVERY_MS) { lastField = now; drawField(); }
    drawScreen();
    if (first) { first = false; onFirstFrame?.(); }
  };

  const onLost = (e: Event) => {
    e.preventDefault();
    active = false;
    if (raf !== null) cancelAnimationFrame(raf);
    raf = null;
  };
  const onResize = () => { lastResize = -Infinity; };
  canvas.addEventListener('webglcontextlost', onLost);
  window.addEventListener('resize', onResize);
  raf = requestAnimationFrame(frame);

  return {
    setActive(next) {
      if (disposed || next === active) return;
      active = next;
      if (next) { lastTick = 0; lastFrame = 0; if (raf === null) raf = requestAnimationFrame(frame); }
      else if (raf !== null) { cancelAnimationFrame(raf); raf = null; }
    },
    flare(amount = 1) { flare = Math.max(flare, Math.min(1, amount)); },
    dispose() {
      disposed = true;
      if (raf !== null) cancelAnimationFrame(raf);
      raf = null;
      canvas.removeEventListener('webglcontextlost', onLost);
      window.removeEventListener('resize', onResize);
    },
  };
}
