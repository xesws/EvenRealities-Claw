/* G2 HUD 回放器。
 *
 * 两件事，分清楚很重要：
 *
 * 1. **折行不在这里算。** 回放的每一帧都是网关真发出来的，`body` 里的 `\n` 就是
 *    服务器端排版引擎定下的断行位置 —— 也就是真机上的断行位置。这里只负责把
 *    已经断好的行画出来，不重新决定任何版式。
 *
 * 2. **字形位置是按固件度量算的。** 每个字形的 x 用 `g2_font_metrics.json`
 *    （从官方 @evenrealities/pretext 原样导出）累加得到，再把浏览器字体横向
 *    缩放进那个 advance 盒子里。所以字距、行宽与真机一致。
 *
 * 诚实的局限：**笔画形状是桌面字体的**，不是 G2 的点阵字形；4bit 灰阶的抗锯齿
 * 也没有复刻。位置、断行、每页行数、缺字丢弃是准的，字形长相不是。
 * 这一条在页面上明说，不藏。
 *
 * 度量算法逐行对应 gateway/lens_gateway/formatting/metrics.py（它本身又是官方
 * JS 的 Python 移植，两边由 tests/test_metrics_oracle.py 逐条比对）。
 */

const LINE_HEIGHT = 27;
const CANVAS = { w: 576, h: 288 };
const LAYOUT = [
  { name: 'status', x: 0, y: 0,   w: 576, h: 36,  textColor: 4 },
  { name: 'body',   x: 0, y: 36,  w: 576, h: 216, textColor: 3 },
  { name: 'foot',   x: 0, y: 252, w: 576, h: 36,  textColor: 2 },
];
const FONT_SIZE = 20;
const FONT_STACK =
  "'Helvetica Neue', Helvetica, 'PingFang SC', 'Noto Sans CJK SC', sans-serif";
//: 字库外可打印字符的占位宽度（px）。见 pretext getAdvWPx：box_w=2, adv_w=box_w+2。
const PLACEHOLDER_ADV_PX = 4;

/* ---------------------------------------------------------------- 度量 */

class FontMetrics {
  constructor(data) {
    this.lineHeight = data.line_height;
    this.stages = data.fonts.map((f) => ({
      name: f.name,
      glyphs: f.glyphs ? new Map(Object.entries(f.glyphs).map(([k, v]) => [+k, v])) : null,
      // cn 段是「范围 + 例外」表，不是显式字形表
      ranges: (f.ranges || []).map(([a, b]) => [+a, +b]).sort((p, q) => p[0] - q[0]),
      exceptions: new Map(Object.entries(f.exceptions || {}).map(([k, v]) => [+k, v])),
      defaultAdvW: f.default_adv_w || 0,
    }));
    this.kern = data.fonts.filter((f) => f.kern).map((f) => ({
      toLeft: new Map(Object.entries(f.kern.cp_to_left).map(([k, v]) => [+k, v])),
      toRight: new Map(Object.entries(f.kern.cp_to_right).map(([k, v]) => [+k, v])),
      rightCnt: f.kern.right_cnt,
      values: f.kern.values,
      owns: new Set(Object.keys(f.glyphs || {}).map(Number)),
    }));
    this._cache = new Map();
  }

  /** 原始 advance（1/16 px，不含 kerning）。字库外返回 0。 */
  advW(cp) {
    for (const s of this.stages) {
      if (s.glyphs) {
        const a = s.glyphs.get(cp);
        if (a !== undefined) return a;
      }
      if (s.ranges.length) {
        // ranges 已按起点排序，二分定位「起点 ≤ cp」的最后一段
        let lo = 0, hi = s.ranges.length - 1, hit = -1;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (s.ranges[mid][0] <= cp) { hit = mid; lo = mid + 1; } else hi = mid - 1;
        }
        if (hit >= 0 && cp <= s.ranges[hit][1]) {
          const e = s.exceptions.get(cp);
          return e !== undefined ? e : s.defaultAdvW;
        }
      }
    }
    return 0;
  }

  /** 这个码点固件到底画不画得出来 —— 画不出来的会被静默丢弃，不留豆腐块。 */
  inFont(cp) { return this.advW(cp) !== 0; }

  kernAdj(cp, next) {
    for (const t of this.kern) {
      const lc = t.toLeft.get(cp), rc = t.toRight.get(next);
      if (lc !== undefined && rc !== undefined) return t.values[(lc - 1) * t.rightCnt + (rc - 1)];
      // 只有「拥有」该码点的第一张表参与 kerning（与 pretext 的 break 一致）
      if (t.owns.has(cp)) break;
    }
    return 0;
  }

  /** 像素 advance。固件是**逐字形**取整再累加：(adv + kern + 8) >> 4。 */
  advPx(cp, next = 0) {
    if (next <= 0) {
      const c = this._cache.get(cp);
      if (c !== undefined) return c;
    }
    const raw = this.advW(cp);
    let px;
    if (raw === 0 && cp >= 32) px = PLACEHOLDER_ADV_PX;
    else px = (raw + (next > 0 ? this.kernAdj(cp, next) : 0) + 8) >> 4;
    if (next <= 0) this._cache.set(cp, px);
    return px;
  }

  textWidth(text) {
    const cps = [...text].map((c) => c.codePointAt(0));
    let w = 0;
    for (let i = 0; i < cps.length; i++) w += this.advPx(cps[i], cps[i + 1] ?? 0);
    return w;
  }
}

let _metrics = null;
export async function loadMetrics(url = 'data/g2_font_metrics.json') {
  if (!_metrics) _metrics = new FontMetrics(await (await fetch(url)).json());
  return _metrics;
}

/* ---------------------------------------------------------------- 渲染 */

let _measureCtx = null;
const _browserW = new Map();
/** 浏览器把这个字形画多宽 —— 缩放系数的分母。 */
function browserWidth(ch) {
  const hit = _browserW.get(ch);
  if (hit !== undefined) return hit;
  if (!_measureCtx) {
    _measureCtx = document.createElement('canvas').getContext('2d');
    _measureCtx.font = `${FONT_SIZE}px ${FONT_STACK}`;
  }
  const w = _measureCtx.measureText(ch).width;
  _browserW.set(ch, w);
  return w;
}

/** 建三个容器 div，返回 {status, body, foot}。 */
export function buildScreen(screen) {
  screen.textContent = '';
  screen.style.cssText =
    `position:relative;width:${CANVAS.w}px;height:${CANVAS.h}px;background:#000;` +
    `overflow:hidden;font:${FONT_SIZE}px/${LINE_HEIGHT}px ${FONT_STACK};color:#39ffa0;`;
  const els = {};
  for (const c of LAYOUT) {
    const el = document.createElement('div');
    // 亮度就是 G2 上唯一真实存在的视觉分层手段（textColor 0~4，五级）
    el.style.cssText =
      `position:absolute;left:${c.x}px;top:${c.y}px;width:${c.w}px;height:${c.h}px;` +
      `overflow:hidden;opacity:${(0.35 + 0.1625 * c.textColor).toFixed(4)};`;
    screen.appendChild(el);
    els[c.name] = el;
  }
  return els;
}

/** 把一段**已经断好行**的文本画进容器。 */
export function paint(el, text, box) {
  const m = _metrics;
  el.textContent = '';
  if (!text) return;
  const maxLines = Math.max(1, Math.floor(box.h / LINE_HEIGHT));
  // 字库外的字符固件会静默丢弃 —— 这里也丢，否则页面比真机好看，那就是撒谎
  const lines = text.split('\n')
    .map((ln) => [...ln].filter((ch) => m.inFont(ch.codePointAt(0))).join(''))
    .slice(0, maxLines);

  lines.forEach((line, row) => {
    const lineEl = document.createElement('div');
    lineEl.style.cssText =
      `position:absolute;left:0;top:${row * LINE_HEIGHT}px;` +
      `height:${LINE_HEIGHT}px;white-space:pre;`;
    const chars = [...line];
    let x = 0;
    for (let i = 0; i < chars.length; i++) {
      const cp = chars[i].codePointAt(0);
      const adv = m.advPx(cp, chars[i + 1] ? chars[i + 1].codePointAt(0) : 0);
      const span = document.createElement('span');
      const bw = browserWidth(chars[i]);
      // 把浏览器字形横向压/拉进固件的 advance 盒子。**不能居中**：居中会让
      // 宽字形（W）压到邻居身上、窄字形（i l）之间留出空隙。
      const scale = bw > 0.01 ? adv / bw : 1;
      span.style.cssText =
        `position:absolute;left:${x}px;top:0;transform-origin:0 0;` +
        `transform:scaleX(${scale.toFixed(4)});`;
      span.textContent = chars[i];
      lineEl.appendChild(span);
      x += adv;
    }
    el.appendChild(lineEl);
  });
}

export function render(els, frame) {
  for (const c of LAYOUT) paint(els[c.name], frame[c.name] || '', c);
}

/* ---------------------------------------------------------------- 回放 */

/**
 * 按录制时的真实时间轴回放一段帧序列。
 * 帧自带 `t`（秒，相对按下说话那一刻），所以节奏就是当时的真实节奏 ——
 * 不是我编的动画曲线。
 */
export class Replay {
  constructor(els, { onFrame } = {}) {
    this.els = els;
    this.onFrame = onFrame || (() => {});
    this.timers = [];
    this.frames = [];
    this.i = -1;
  }

  stop() {
    this.timers.forEach(clearTimeout);
    this.timers = [];
  }

  /** @param speed 1 = 原速。提醒那段有 20 秒空等，页面上用 2~3 倍速跳过。 */
  play(frames, { speed = 1, onEnd } = {}) {
    this.stop();
    this.frames = frames;
    this.i = -1;
    if (!frames.length) return;
    const t0 = frames[0].t;
    frames.forEach((f, i) => {
      this.timers.push(setTimeout(() => {
        this.i = i;
        render(this.els, f);
        this.onFrame(f, i);
        if (i === frames.length - 1 && onEnd) onEnd();
      }, ((f.t - t0) / speed) * 1000));
    });
  }

  /** 直接跳到第 i 帧（拖时间轴用）。 */
  seek(i) {
    this.stop();
    const f = this.frames[i];
    if (!f) return;
    this.i = i;
    render(this.els, f);
    this.onFrame(f, i);
  }
}

export { LINE_HEIGHT, CANVAS, LAYOUT };
