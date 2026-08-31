#!/usr/bin/env node
/**
 * 官方模拟器自动化探针 —— 把 `probe/probe.ts` 排好的问题跑一遍，
 * 逐屏截图 + 逐屏像素判定，产出 `docs/GLYPH-TABLE.md` 与 `docs/assets/g2probe-*.png`。
 *
 * 依赖的是官方 `@evenrealities/evenhub-simulator` v0.7.0+ 的自动化接口：
 *   GET  /api/ping                 健康检查
 *   GET  /api/screenshot/glasses   576×288 RGBA PNG（眼镜帧缓冲原样导出）
 *   GET  /api/console?since_id=N   webview 的 console 输出（增量）
 *   POST /api/input {action}       触控板动作（本工具用 click 步进）
 *
 * 用法：
 *   node tools/g2probe.mjs                     # 自己拉起 vite + 模拟器，跑完自动收摊
 *   node tools/g2probe.mjs --keep              # 跑完保留模拟器窗口，方便肉眼看
 *   node tools/g2probe.mjs --port 9899         # 指定自动化端口
 *   node tools/g2probe.mjs --url http://…      # 用已经在跑的开发服务器
 *
 * 判定原则：**一格墨都没有 = 固件画不出这个字形**。这是"静默跳过、不留豆腐块"
 * 这条官方说法在渲染栈里的直接观测量，比任何间接推断都硬。
 */
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { inflateSync } from 'node:zlib';

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN = resolve(HERE, '..');
const REPO = resolve(PLUGIN, '..');
const ASSETS = resolve(REPO, 'docs/assets');

const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : fallback;
};
const has = (name) => argv.includes(`--${name}`);

const PORT = Number(flag('port', 9899));
const BASE = `http://127.0.0.1:${PORT}`;
const DEV_URL = flag('url', null);
const LINE_HEIGHT = 27;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- 极简 PNG 解码

/**
 * 只解 8-bit RGBA 非隔行 PNG —— 模拟器导出的就是这一种，不值得为它引一个依赖。
 * 逐扫描线反滤波（filter 0..4），返回 {width, height, data: Uint8Array(w*h*4)}。
 */
function decodePng(buf) {
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error('不是 PNG');
  let off = 8;
  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  const idat = [];
  while (off < buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString('ascii', off + 4, off + 8);
    const body = buf.subarray(off + 8, off + 8 + len);
    if (type === 'IHDR') {
      width = body.readUInt32BE(0);
      height = body.readUInt32BE(4);
      bitDepth = body[8];
      colorType = body[9];
      if (body[12] !== 0) throw new Error('不支持隔行 PNG');
    } else if (type === 'IDAT') {
      idat.push(body);
    } else if (type === 'IEND') break;
    off += 12 + len;
  }
  if (bitDepth !== 8 || colorType !== 6) throw new Error(`不支持的 PNG 格式 depth=${bitDepth} color=${colorType}`);
  const raw = inflateSync(Buffer.concat(idat));
  const bpp = 4;
  const stride = width * bpp;
  const out = new Uint8Array(width * height * bpp);
  let prev = new Uint8Array(stride);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    const cur = new Uint8Array(stride);
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? cur[i - bpp] : 0;
      const b = prev[i];
      const c = i >= bpp ? prev[i - bpp] : 0;
      let v = line[i];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      cur[i] = v & 0xff;
    }
    out.set(cur, y * stride);
    prev = cur;
  }
  return { width, height, data: out };
}

/**
 * 某个矩形里有没有墨。
 *
 * **判据是 alpha，不是 RGB**：模拟器导出的帧缓冲把每个像素的 RGB 固定成纯绿
 * (0,255,0)，真正的灰阶强度全在 alpha 通道里（实测整幅图只有 `0,255,0,*` 这一族颜色）。
 * 按 RGB 判会把整屏都算成有墨。
 */
function inkBox(img, x0, y0, w, h) {
  let ink = 0;
  let minX = Infinity;
  let maxX = -1;
  for (let y = y0; y < Math.min(y0 + h, img.height); y++) {
    for (let x = x0; x < Math.min(x0 + w, img.width); x++) {
      const alpha = img.data[(y * img.width + x) * 4 + 3];
      if (alpha > 32) {
        ink++;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
      }
    }
  }
  return { ink, minX: ink ? minX : -1, maxX };
}

// ---------------------------------------------------------------- 自动化接口

async function api(path, init) {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res;
}

async function waitReady(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BASE}/api/ping`);
      if (res.ok) return true;
    } catch {
      /* 还没起来 */
    }
    await sleep(400);
  }
  return false;
}

let sinceId = 0;
async function drainConsole() {
  const res = await api(`/api/console?since_id=${sinceId}`);
  const { entries } = await res.json();
  for (const e of entries) sinceId = Math.max(sinceId, e.id + 1);
  return entries;
}

/** 等一条以 prefix 开头的 console 行。 */
async function waitLine(prefix, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const e of await drainConsole()) {
      if (typeof e.message === 'string' && e.message.startsWith(prefix)) return e.message;
    }
    await sleep(200);
  }
  throw new Error(`等待 console 行 "${prefix}" 超时`);
}

async function screenshot(name) {
  const res = await api('/api/screenshot/glasses');
  const buf = Buffer.from(await res.arrayBuffer());
  mkdirSync(ASSETS, { recursive: true });
  const file = resolve(ASSETS, `${name}.png`);
  writeFileSync(file, buf);
  // 对外一律用仓库相对路径：绝对路径写进 docs/assets/g2probe.json 会把本机目录结构带进仓库
  return { img: decodePng(buf), file: relative(REPO, file) };
}

const click = () =>
  api('/api/input', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action: 'click' }),
  });

// ---------------------------------------------------------------- 逐屏判定

function analyse(step, img) {
  const notes = [];
  if (step.id.startsWith('glyphs-')) {
    const glyphs = step.meta.glyphs ?? [];
    const verdict = glyphs.map((g, row) => {
      const box = inkBox(img, 0, row * LINE_HEIGHT, img.width, LINE_HEIGHT);
      return { glyph: g, cp: `U+${g.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}`, ink: box.ink, drawn: box.ink > 0, width: box.ink ? box.maxX - box.minX + 1 : 0 };
    });
    return { verdict, notes };
  }
  if (step.id === 'ruler') {
    const rows = [];
    for (let r = 0; r < 11; r++) {
      const box = inkBox(img, 0, r * LINE_HEIGHT, img.width, LINE_HEIGHT);
      if (box.ink > 0) rows.push({ row: r, rightmostInk: box.maxX });
    }
    notes.push(`有墨的行：${rows.map((r) => r.row).join(',')}（共 ${rows.length} 行）`);
    notes.push(`最右墨迹 x：${rows.map((r) => r.rightmostInk).join(',')}`);
    return { rows, notes };
  }
  const box = inkBox(img, 0, 0, img.width, img.height);
  const filled = [];
  for (let r = 0; r * LINE_HEIGHT < img.height; r++) {
    if (inkBox(img, 0, r * LINE_HEIGHT, img.width, LINE_HEIGHT).ink > 0) filled.push(r);
  }
  notes.push(`总墨迹像素 ${box.ink}，有墨行 ${filled.length} 行（行号 ${filled.join(',')}）`);
  return { notes };
}

// ---------------------------------------------------------------- 主流程

const children = [];
function spawnBg(label, cmd, args, opts = {}) {
  const p = spawn(cmd, args, { cwd: PLUGIN, stdio: 'ignore', detached: false, ...opts });
  p.on('error', (e) => console.error(`[${label}] 启动失败: ${e.message}`));
  children.push({ label, p });
  return p;
}
function cleanup() {
  for (const { p } of children) {
    try {
      p.kill('SIGTERM');
    } catch {
      /* 已退出 */
    }
  }
}
process.on('SIGINT', () => {
  cleanup();
  process.exit(130);
});

async function main() {
  let url = DEV_URL;
  if (!url) {
    console.log('▶ 启动 vite 开发服务器…');
    spawnBg('vite', resolve(PLUGIN, 'node_modules/.bin/vite'), []);
    for (let i = 0; i < 60; i++) {
      try {
        const r = await fetch('http://localhost:5173/');
        if (r.ok) break;
      } catch {
        /* 还没起来 */
      }
      await sleep(300);
    }
    url = 'http://localhost:5173';
  }
  const target = `${url.replace(/\/$/, '')}/probe/probe.html`;

  // 先清掉上一轮的产物：屏序号变了以后，旧文件会以"看起来像本轮证据"的样子留在 docs/assets 里
  mkdirSync(ASSETS, { recursive: true });
  for (const f of readdirSync(ASSETS)) {
    if (/^g2probe-.*\.png$/.test(f)) rmSync(resolve(ASSETS, f));
  }

  console.log(`▶ 启动官方模拟器（自动化端口 ${PORT}）→ ${target}`);
  // 直接起**平台原生二进制**，不走 bin/index.js 包装脚本 ——
  // 包装脚本用 execFileSync 起孙进程，杀掉它不会杀掉模拟器窗口，跑完会留一堆僵尸。
  const simBin = require.resolve(
    `@evenrealities/sim-${process.platform}-${process.arch}/bin/evenhub-simulator${
      process.platform === 'win32' ? '.exe' : ''
    }`,
  );
  spawnBg('simulator', simBin, ['--automation-port', String(PORT), target]);

  if (!(await waitReady())) throw new Error('模拟器自动化接口未就绪');
  console.log('  自动化接口就绪');

  const steps = [];
  for (;;) {
    const line = await waitLine('PROBE_RESULT ');
    const step = JSON.parse(line.slice('PROBE_RESULT '.length));
    if (step.error && step.index === undefined) throw new Error(`探针页报错：${step.error}`);
    // 给渲染管线一帧时间
    await sleep(500);
    const { img, file } = await screenshot(`g2probe-${String(step.index).padStart(2, '0')}-${step.id}`);
    const analysis = analyse(step, img);
    steps.push({ ...step, file, ...analysis });
    console.log(`\n[${step.index + 1}/${step.total}] ${step.id} — ${step.label}`);
    console.log(`  ${step.api} → ${JSON.stringify(step.result)}${step.error ? ` (error: ${step.error})` : ''}`);
    for (const c of step.containers) console.log(`  容器 ${c.name} ${c.box} · ${c.chars} 字符 / ${c.bytes} 字节`);
    for (const n of analysis.notes ?? []) console.log(`  ${n}`);
    if (analysis.verdict) {
      for (const v of analysis.verdict) {
        console.log(`  ${v.drawn ? '画得出' : '画不出'}  ${v.glyph} ${v.cp}  墨迹 ${v.ink}px  宽 ${v.width}px`);
      }
    }
    console.log(`  截图 → ${file}`);
    if (step.index + 1 >= step.total) break;
    await click();
  }

  const json = resolve(ASSETS, 'g2probe.json');
  writeFileSync(json, JSON.stringify(steps, null, 2));
  console.log(`\n✔ 全部 ${steps.length} 屏跑完，结构化结果 → ${relative(REPO, json)}`);
  if (!has('keep')) cleanup();
  else console.log('（--keep：模拟器窗口保留，手动关闭即可）');
}

main().then(
  () => {
    if (!has('keep')) process.exit(0);
  },
  (err) => {
    console.error('✘', err.message);
    cleanup();
    process.exit(1);
  },
);
