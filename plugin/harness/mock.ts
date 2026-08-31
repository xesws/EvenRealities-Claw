/**
 * 宿主 mock —— **自动化夹具，不是保真基准**。
 *
 * 保真基准是官方 `@evenrealities/evenhub-simulator`（真字体、4-bit 灰阶、真固件折行）。
 * 本文件的职责是官方模拟器做不到的那一半：**可编程、可注入故障、可在 CI 里跑**。
 * 每条结论属于哪一档见 docs/SIMULATOR-PARITY.md。
 *
 * 桥接协议：SDK 通过 window.flutter_inappwebview.callHandler('evenAppMessage', <JSON 字符串>)
 * 调宿主（信封 {type:'call_even_app_method', method, data}，handler 返回值即 Promise 结果）；
 * 宿主通过 window._listenEvenAppMessage({type:'listen_even_app_data', method, data}) 推事件。
 * 以上信封形状均已用 SDK 0.0.14 实测确认（见 docs/HARDWARE-SPEC.md §桥接实测）。
 *
 * 本轮相对旧版的五处保真修复（旧版对真机风险的检出率为 0）：
 *
 * 1. **删掉两档字号**。旧版按容器高度在 24px/32px 之间切换、行高写死 44px —— G2 **根本没有
 *    字号控制**，行高恒为 27px。字号分层是虚构的，会让人在模拟器里看到真机上不存在的层次。
 * 2. **字库外字符静默丢弃**。判据来自官方 `@evenrealities/pretext`：`getAdvW(cp) === 0`
 *    即该码点不在 G2 字库，固件会跳过且**不画豆腐块**。旧版用桌面浏览器字体，什么都画得出来。
 * 3. **折行用 pretext 的真实字形度量**，并逐行与 `measureTextWrap` 的行数交叉校验；
 *    超出 `floor(h/27)` 行的内容按固件行为裁掉并告警（旧版靠 CSS `word-break` 瞎折）。
 * 4. **`createStartUpPageContainer` 一个页面生命周期只能调一次**，第二次返回 invalid(1)。
 *    旧版恒返回 0 且可无限重复 —— harness 跑得通、真机上第二次直接失败。
 * 5. **补齐 SDK 自己不校验的那些约束**（实测：`validateEvenHubPageContainer` 只管
 *    zOrder / textColor / menu，**不管**内容长度、isEventCapture 数量、containerName 长度、
 *    几何越界），并补 `getDeviceInfo` / 前台进出事件 / 5 手势 × 4 来源 / 故障注入。
 */
import { getAdvW, getTextWidth, measureTextWrap } from '@evenrealities/pretext';
import { CANVAS, LINE_HEIGHT } from '../src/hud';

// ---------------------------------------------------------------- 常量与出处

/**
 * 单容器内容长度上限 = **UTF-8 999 字节**（不是 1000 字符）。
 *
 * SDK README 写「≤1000 字符」，模拟器 v0.7.1 更新日志写「bytes limit 999」，差三倍。
 * 用 `tools/g2probe.mjs` 在官方模拟器上实测拍板（见 docs/HARDWARE-SPEC.md §2.1）：
 *   '中'×333 = 999 字节 → true ✅ ／ '中'×334 = 1002 字节 → false ❌
 *   'a'×1000 = 1000 **字符**、1000 字节 → false ❌  ← 决定性的一行：字符口径没超，仍然失败
 */
const CONTENT_BYTES_LIMIT = 999;
/** `containerName` ≤ 16 字符（官方文档站 /docs/build/containers）。 */
const NAME_MAX = 16;
/** 每页容器总数 1~12，其中文本容器 ≤ 8。 */
const TOTAL_MAX = 12;
const TEXT_MAX = 8;

/** `StartUpPageCreateResult`：0 成功 / 1 invalid / 2 oversize / 3 outOfMemory。 */
export type CreateResult = 0 | 1 | 2 | 3;

/** `OsEventTypeList`（SDK 0.0.14）。CLICK=0 是 protobuf 零值，上线时会被省略。 */
const EVT = {
  CLICK: 0,
  SCROLL_TOP: 1,
  SCROLL_BOTTOM: 2,
  DOUBLE_CLICK: 3,
  FOREGROUND_ENTER: 4,
  FOREGROUND_EXIT: 5,
  ABNORMAL_EXIT: 6,
  SYSTEM_EXIT: 7,
  LONG_PRESS: 9,
  LONG_PRESS_RELEASE: 10,
} as const;

/** `EventSourceType`：0 DUMMY_NULL / 1 GLASSES_R / 2 RING / 3 GLASSES_L。 */
const SRC = { unknown: 0, glassesR: 1, ring: 2, glassesL: 3 } as const;

export type GestureKind =
  | 'tap'
  | 'doubleTap'
  | 'swipeUp'
  | 'swipeDown'
  | 'longPress'
  | 'longPressRelease';
export type GestureSource = keyof typeof SRC;

const GESTURE_EVENT: Record<GestureKind, number> = {
  tap: EVT.CLICK,
  doubleTap: EVT.DOUBLE_CLICK,
  swipeUp: EVT.SCROLL_TOP,
  swipeDown: EVT.SCROLL_BOTTOM,
  longPress: EVT.LONG_PRESS,
  longPressRelease: EVT.LONG_PRESS_RELEASE,
};

// ---------------------------------------------------------------- 类型

interface CallEnvelope {
  type: string;
  method: string;
  data?: Record<string, unknown>;
}

interface TextContainerDef {
  containerID?: number;
  containerName?: string;
  xPosition?: number;
  yPosition?: number;
  width?: number;
  height?: number;
  borderWidth?: number;
  paddingLength?: number;
  content?: string;
  isEventCapture?: number;
  textColor?: number;
}

/** 可注入的故障。`null` = 按真实校验结果，不注入。 */
export interface MockFaults {
  /** 强制 createStartUpPageContainer 的返回码 */
  createResult: CreateResult | null;
  /** 强制 rebuildPageContainer 的返回值 */
  rebuildOk: boolean | null;
  /** 强制 textContainerUpgrade 的返回值（测"写失败不能毒化去重缓存"） */
  upgradeOk: boolean | null;
  /** 麦克风被系统/其它 App 抢占 → audioControl(true) 返回 false */
  micDenied: boolean;
  /** 每次 bridge 调用的模拟 BLE 往返延迟 */
  bridgeDelayMs: number;
  /** bridge 调用永不 settle（测 B1 的 5s 超时保护） */
  bridgeHang: boolean;
}

export interface MockStats {
  createCalls: number;
  rebuildCalls: number;
  upgradeCalls: number;
  upgradeFailures: number;
  droppedGlyphs: number;
  overflowLines: number;
}

export interface EvenHostMock {
  /** 5 种手势 × 4 种来源。tap/unknown 会**故意省略** protobuf 零值字段。 */
  simulateGesture(kind: GestureKind, source?: GestureSource): void;
  /** 前台交互层关闭（页面仍挂载）——插件绝不能借此断开 WS */
  simulateForegroundExit(): void;
  /** 重新回到前台 */
  simulateForegroundEnter(): void;
  /** 系统退出 / 异常退出（真正的 teardown） */
  simulateExit(abnormal?: boolean): void;
  /** 断网模拟：关掉页面里所有活动 WebSocket */
  killSockets(): number;
  /** 推送设备状态（电量/佩戴等） */
  pushDeviceStatus(partial?: Record<string, unknown>): void;
  /** 读回眼镜屏上**实际渲染**的文本（已丢弃字库外字符、已按固件折行、已裁掉溢出行） */
  screenText(): Record<string, string[]>;
  readonly micOpen: boolean;
  readonly faults: MockFaults;
  readonly stats: MockStats;
}

export interface MockOptions {
  /** 576×288 的"眼镜屏"宿主元素 */
  screen: HTMLElement;
  onLog?: (line: string) => void;
  onMicState?: (open: boolean) => void;
}

const STORAGE_PREFIX = 'evenmock.';
const TARGET_RATE = 16000;
/** ~100ms @ 16kHz = 1600 样本 */
const CHUNK_SAMPLES = 1600;

// ---------------------------------------------------------------- 字形与折行

const utf8 = new TextEncoder();

/** 该码点是否在 G2 字库内。判据：pretext 的 advance 为 0 ⇒ 回退链上没有任何字体覆盖它。 */
function inFont(cp: number): boolean {
  return getAdvW(cp) > 0;
}

/**
 * 固件的缺字行为：**静默跳过，不留占位框**。
 * 返回过滤后的字符串与被丢弃的字符列表（`\n` 永远保留，官方明确它是换行符）。
 */
function dropMissingGlyphs(text: string): { kept: string; dropped: string[] } {
  const dropped: string[] = [];
  let kept = '';
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if (ch === '\n' || inFont(cp)) kept += ch;
    else dropped.push(`${ch}(U+${cp.toString(16).toUpperCase().padStart(4, '0')})`);
  }
  return { kept, dropped };
}

/** 断行机会：空格、连字符之后可断；CJK 每字之间都可断。 */
function isCjk(cp: number): boolean {
  return (
    (cp >= 0x1100 && cp <= 0x11ff) ||
    (cp >= 0x2e80 && cp <= 0xa4cf) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe30 && cp <= 0xfe4f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x20000 && cp <= 0x3ffff)
  );
}

/** 把一段无换行文本切成最小不可分单元（一个 CJK 字 / 一个拉丁词含尾随空格或连字符）。 */
function tokenize(text: string): string[] {
  const out: string[] = [];
  let buf = '';
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if (isCjk(cp)) {
      if (buf) {
        out.push(buf);
        buf = '';
      }
      out.push(ch);
    } else if (ch === ' ' || ch === '-') {
      out.push(buf + ch);
      buf = '';
    } else {
      buf += ch;
    }
  }
  if (buf) out.push(buf);
  return out;
}

/**
 * 复刻固件折行（*"Text wraps at the container width."*）。
 * 宽度全部走 pretext —— 与固件 LVGL 同一套 advance + kerning + 逐字形取整。
 * `\n` 是硬换行（官方：*"'\n' is a line break."*）。
 */
function wrapForFirmware(text: string, maxWidth: number): string[] {
  if (maxWidth <= 0) return text.split('\n');
  const lines: string[] = [];
  for (const para of text.split('\n')) {
    let line = '';
    for (const unit of tokenize(para)) {
      if (line !== '' && getTextWidth(line + unit) > maxWidth) {
        lines.push(line);
        line = unit.startsWith(' ') ? unit.slice(1) : unit;
        // 单个 token 本身就超宽（超长拉丁词）：按字符硬切
        while (getTextWidth(line) > maxWidth && [...line].length > 1) {
          const chars = [...line];
          let cut = chars.length - 1;
          while (cut > 1 && getTextWidth(chars.slice(0, cut).join('')) > maxWidth) cut--;
          lines.push(chars.slice(0, cut).join(''));
          line = chars.slice(cut).join('');
        }
      } else {
        line += unit;
      }
    }
    lines.push(line);
  }
  return lines;
}

// ---------------------------------------------------------------- 主体

export function installEvenHostMock(opts: MockOptions): EvenHostMock {
  const log = (line: string) => opts.onLog?.(line);
  const containers = new Map<number, { el: HTMLElement; def: TextContainerDef; lines: string[] }>();
  const nameToId = new Map<string, number>();

  const faults: MockFaults = {
    createResult: null,
    rebuildOk: null,
    upgradeOk: null,
    micDenied: false,
    bridgeDelayMs: 0,
    bridgeHang: false,
  };

  const stats: MockStats = {
    createCalls: 0,
    rebuildCalls: 0,
    upgradeCalls: 0,
    upgradeFailures: 0,
    droppedGlyphs: 0,
    overflowLines: 0,
  };

  /** 一个页面生命周期只能 createStartUpPageContainer 一次。 */
  let pageCreated = false;

  // ---------- 事件推送（宿主 → SDK） ----------

  function pushToSdk(method: string, data: unknown): void {
    const w = window as unknown as { _listenEvenAppMessage?: (m: unknown) => void };
    if (typeof w._listenEvenAppMessage !== 'function') {
      log(`(SDK 尚未加载，丢弃 ${method} 推送)`);
      return;
    }
    w._listenEvenAppMessage({ type: 'listen_even_app_data', method, data });
  }

  function pushHubEvent(type: string, jsonData: Record<string, unknown>): void {
    pushToSdk('evenHubEvent', { type, jsonData });
  }

  function pushSysEvent(eventType: number, source: GestureSource): void {
    // 保真点：protobuf **零值字段不上线**。CLICK_EVENT=0 与 DUMMY_NULL=0 到达时是 undefined，
    // 这里同样省略，专门压测插件的"缺字段 → 归一"逻辑。
    const data: Record<string, unknown> = {};
    if (eventType !== 0) data.eventType = eventType;
    if (SRC[source] !== 0) data.eventSource = SRC[source];
    pushHubEvent('sysEvent', data);
  }

  // ---------- 眼镜屏渲染 ----------

  /**
   * 按固件语义渲染一个容器：丢弃字库外字符 → pretext 折行 → 裁到 floor(h/27) 行。
   * 每个字形单独定位在 pretext 算出的 x 偏移上，横向位置与真机一致；
   * **字形本身仍是桌面字体**（形状不保真，那是官方模拟器的活）。
   */
  function paint(entry: { el: HTMLElement; def: TextContainerDef; lines: string[] }): void {
    const { el, def } = entry;
    const pad = (def.borderWidth ?? 0) + (def.paddingLength ?? 0);
    const innerW = (def.width ?? CANVAS.width) - pad * 2;
    const maxLines = Math.max(1, Math.floor((def.height ?? LINE_HEIGHT) / LINE_HEIGHT));
    const name = def.containerName ?? String(def.containerID ?? '?');

    const { kept, dropped } = dropMissingGlyphs(def.content ?? '');
    if (dropped.length) {
      stats.droppedGlyphs += dropped.length;
      log(`⚠ ${name}：静默丢弃 ${dropped.length} 个字库外字符 → ${dropped.join(' ')}`);
    }

    const lines = wrapForFirmware(kept, innerW);
    // 交叉校验：本地折行的行数必须与官方 pretext 的 measureTextWrap 一致
    if (!kept.includes('\n')) {
      const official = measureTextWrap(kept, innerW).lineCount;
      if (kept !== '' && official !== lines.length) {
        log(`⚠ ${name}：折行行数与 pretext 不一致（本地 ${lines.length} / 官方 ${official}）`);
      }
    }
    if (lines.length > maxLines) {
      stats.overflowLines += lines.length - maxLines;
      const capture = def.isEventCapture === 1;
      log(
        `⚠ ${name}：固件折成 ${lines.length} 行，容器只放得下 ${maxLines} 行 → 溢出 ${
          lines.length - maxLines
        } 行（isEventCapture=${capture ? 1 : 0}${capture ? '，固件会滚动' : '，直接看不见'}）`,
      );
    }
    entry.lines = lines.slice(0, maxLines);

    el.innerHTML = '';
    entry.lines.forEach((text, row) => {
      const lineEl = document.createElement('div');
      lineEl.style.cssText = `position:absolute;left:${pad}px;top:${
        pad + row * LINE_HEIGHT
      }px;height:${LINE_HEIGHT}px;white-space:pre;`;
      const chars = [...text];
      for (let i = 0; i < chars.length; i++) {
        const span = document.createElement('span');
        // 逐字形位置 = pretext 对前缀的累计宽度（含 kerning 与逐字形取整），与固件同源
        const left = getTextWidth(chars.slice(0, i).join(''));
        const w = getTextWidth(chars.slice(0, i + 1).join('')) - left;
        span.style.cssText = `position:absolute;left:${left}px;width:${w}px;text-align:center;overflow:hidden;`;
        span.textContent = chars[i];
        lineEl.appendChild(span);
      }
      el.appendChild(lineEl);
    });
  }

  function buildScreen(textObject: TextContainerDef[]): void {
    opts.screen.innerHTML = '';
    containers.clear();
    nameToId.clear();
    for (const def of textObject) {
      const el = document.createElement('div');
      el.style.cssText =
        `position:absolute;left:${def.xPosition ?? 0}px;top:${def.yPosition ?? 0}px;` +
        `width:${def.width ?? CANVAS.width}px;height:${def.height ?? 0}px;overflow:hidden;` +
        // G2 **没有字号控制**，全屏单一字号；行高恒 27px。这里按 CJK advance(20px) 取字号，
        // 使中文占位与真机接近；拉丁字形会被压进 pretext 算出的 advance 盒里。
        `font-size:20px;line-height:${LINE_HEIGHT}px;` +
        // textColor 0~4 五级亮度是 G2 上唯一真实存在的视觉分层手段
        `opacity:${0.35 + 0.1625 * (def.textColor ?? 4)};`;
      const id = def.containerID ?? 0;
      const entry = { el, def, lines: [] as string[] };
      containers.set(id, entry);
      if (def.containerName) nameToId.set(def.containerName, id);
      opts.screen.appendChild(el);
      paint(entry);
    }
  }

  function showExitOverlay(exitMode: number): void {
    const overlay = document.createElement('div');
    overlay.style.cssText =
      'position:absolute;inset:0;background:rgba(0,0,0,.82);display:flex;flex-direction:column;' +
      'align-items:center;justify-content:center;gap:14px;font-size:22px;color:#9fe8bb;z-index:5;';
    overlay.textContent = exitMode === 1 ? '插件请求退出（exitMode=1）' : '插件已退出（exitMode=0）';
    if (exitMode === 1) {
      const btn = document.createElement('button');
      btn.textContent = '确认退出';
      btn.style.cssText =
        'font-size:18px;padding:8px 20px;background:#123523;color:#39ffa0;border:1px solid #2bdc7e;border-radius:8px;cursor:pointer;';
      btn.addEventListener('click', () => {
        overlay.remove();
        opts.screen.innerHTML = '';
        containers.clear();
        pageCreated = false;
        log('用户确认退出 → 推送 SYSTEM_EXIT_EVENT');
        pushHubEvent('sysEvent', { eventType: EVT.SYSTEM_EXIT });
      });
      overlay.appendChild(btn);
    }
    opts.screen.appendChild(overlay);
  }

  // ---------- 建页校验（补 SDK 自己不做的那些） ----------

  /**
   * 实测：SDK 的 `validateEvenHubPageContainer` **只**校验 zOrderIndex、textColor 与 menu，
   * 对内容长度、isEventCapture 数量、containerName 长度、几何越界一概放行。
   * 这些约束写在官方文档里、由宿主/固件执行，所以夹具必须自己兜住，否则本地永远发现不了。
   * 返回 null 表示通过，否则返回该返回给插件的错误码。
   */
  function validatePage(textObject: TextContainerDef[], totalNum: number): CreateResult | null {
    const fail = (code: CreateResult, why: string): CreateResult => {
      log(`建页校验失败（返回 ${code}）：${why}`);
      return code;
    };
    if (totalNum < 1 || totalNum > TOTAL_MAX) return fail(1, `containerTotalNum=${totalNum} 不在 1~${TOTAL_MAX}`);
    if (textObject.length > TEXT_MAX) return fail(1, `文本容器 ${textObject.length} 个 > ${TEXT_MAX}`);
    if (textObject.length !== totalNum) {
      return fail(1, `containerTotalNum=${totalNum} 与实际 ${textObject.length} 个容器不符`);
    }
    const capture = textObject.filter((d) => d.isEventCapture === 1).length;
    if (capture !== 1) return fail(1, `isEventCapture=1 的容器有 ${capture} 个，必须恰好 1 个`);
    const ids = new Set<number>();
    for (const d of textObject) {
      const name = d.containerName ?? '';
      if (name.length > NAME_MAX) return fail(1, `containerName "${name}" 超过 ${NAME_MAX} 字符`);
      const id = d.containerID ?? 0;
      if (ids.has(id)) return fail(1, `containerID ${id} 重复`);
      ids.add(id);
      const bytes = utf8.encode(d.content ?? '').length;
      if (bytes > CONTENT_BYTES_LIMIT) {
        return fail(2, `${name} 内容 ${bytes} 字节 > ${CONTENT_BYTES_LIMIT}`);
      }
      const x = d.xPosition ?? 0;
      const y = d.yPosition ?? 0;
      const w = d.width ?? 0;
      const h = d.height ?? 0;
      if (x < 0 || y < 0 || x + w > CANVAS.width || y + h > CANVAS.height) {
        return fail(2, `${name} 几何 ${x},${y} ${w}×${h} 超出 ${CANVAS.width}×${CANVAS.height} 画布`);
      }
    }
    return null;
  }

  // ---------- 麦克风（getUserMedia → 16kHz s16le mono → audioEvent） ----------

  let micOpen = false;
  let micStream: MediaStream | null = null;
  let audioCtx: AudioContext | null = null;
  let processor: ScriptProcessorNode | null = null;
  let pcmCarry: number[] = [];

  /** 流式线性插值重采样器（任意输入采样率 → 16kHz）。 */
  class StreamResampler {
    private tail = new Float32Array(0);
    private pos = 0;
    constructor(
      private readonly inRate: number,
      private readonly outRate: number,
    ) {}
    push(input: Float32Array): Float32Array {
      const step = this.inRate / this.outRate;
      const buf = new Float32Array(this.tail.length + input.length);
      buf.set(this.tail, 0);
      buf.set(input, this.tail.length);
      const out: number[] = [];
      let pos = this.pos;
      while (pos + 1 < buf.length) {
        const i = Math.floor(pos);
        const frac = pos - i;
        out.push(buf[i] * (1 - frac) + buf[i + 1] * frac);
        pos += step;
      }
      const keep = Math.min(Math.floor(pos), buf.length);
      this.tail = buf.slice(keep);
      this.pos = pos - keep;
      return Float32Array.from(out);
    }
  }

  async function startMic(): Promise<boolean> {
    if (faults.micDenied) {
      log('故障注入：麦克风被占用 → audioControl(true) 返回 false');
      return false;
    }
    if (micOpen) return true;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      log(`麦克风获取失败：${err instanceof Error ? err.message : String(err)}`);
      return false;
    }
    audioCtx = new AudioContext();
    await audioCtx.resume();
    const source = audioCtx.createMediaStreamSource(micStream);
    const resampler = new StreamResampler(audioCtx.sampleRate, TARGET_RATE);
    processor = audioCtx.createScriptProcessor(4096, 1, 1);
    pcmCarry = [];
    processor.onaudioprocess = (ev) => {
      const resampled = resampler.push(ev.inputBuffer.getChannelData(0));
      for (let i = 0; i < resampled.length; i++) {
        const v = Math.max(-1, Math.min(1, resampled[i]));
        pcmCarry.push(Math.round(v < 0 ? v * 0x8000 : v * 0x7fff));
      }
      while (pcmCarry.length >= CHUNK_SAMPLES) {
        const samples = pcmCarry.splice(0, CHUNK_SAMPLES);
        const i16 = Int16Array.from(samples);
        // s16le 字节（typed array 在主流平台均为小端）
        const bytes = new Uint8Array(i16.buffer);
        pushHubEvent('audioEvent', { audioPcm: Array.from(bytes) });
      }
    };
    const sink = audioCtx.createGain();
    sink.gain.value = 0; // 静音回路，仅为驱动 ScriptProcessor
    source.connect(processor);
    processor.connect(sink);
    sink.connect(audioCtx.destination);
    micOpen = true;
    opts.onMicState?.(true);
    log(`麦克风已打开（输入 ${audioCtx.sampleRate}Hz → 16kHz s16le mono，~100ms/块）`);
    return true;
  }

  function stopMic(): boolean {
    if (!micOpen) return true;
    processor?.disconnect();
    processor = null;
    micStream?.getTracks().forEach((t) => t.stop());
    micStream = null;
    void audioCtx?.close();
    audioCtx = null;
    pcmCarry = [];
    micOpen = false;
    opts.onMicState?.(false);
    log('麦克风已关闭');
    return true;
  }

  // ---------- callHandler（SDK → 宿主） ----------

  function handleCall(envelope: CallEnvelope): unknown {
    const data = envelope.data ?? {};
    switch (envelope.method) {
      case 'createStartUpPageContainer': {
        stats.createCalls += 1;
        const textObject = (data.textObject as TextContainerDef[] | undefined) ?? [];
        const total = Number(data.containerTotalNum ?? textObject.length);
        if (faults.createResult !== null) {
          log(`故障注入：createStartUpPageContainer → ${faults.createResult}`);
          return faults.createResult;
        }
        if (pageCreated) {
          // 官方：一个页面生命周期内只能调一次，之后必须走 rebuildPageContainer
          log('createStartUpPageContainer 被重复调用 → 1 (invalid)。真机同样会失败，请改用 rebuild。');
          return 1;
        }
        const bad = validatePage(textObject, total);
        if (bad !== null) return bad;
        buildScreen(textObject);
        pageCreated = true;
        log(`createStartUpPageContainer：${textObject.length} 个文本容器 → 0 (success)`);
        return 0;
      }
      case 'rebuildPageContainer': {
        stats.rebuildCalls += 1;
        const textObject = (data.textObject as TextContainerDef[] | undefined) ?? [];
        const total = Number(data.containerTotalNum ?? textObject.length);
        if (faults.rebuildOk !== null) {
          log(`故障注入：rebuildPageContainer → ${String(faults.rebuildOk)}`);
          return faults.rebuildOk;
        }
        if (!pageCreated) {
          log('rebuildPageContainer 先于 createStartUpPageContainer 调用 → false');
          return false;
        }
        if (validatePage(textObject, total) !== null) return false;
        buildScreen(textObject);
        log(`rebuildPageContainer：${textObject.length} 个文本容器 → true`);
        return true;
      }
      case 'textContainerUpgrade': {
        stats.upgradeCalls += 1;
        if (faults.upgradeOk !== null) {
          if (!faults.upgradeOk) stats.upgradeFailures += 1;
          log(`故障注入：textContainerUpgrade → ${String(faults.upgradeOk)}`);
          return faults.upgradeOk;
        }
        const id = Number(data.containerID ?? nameToId.get(String(data.containerName ?? '')));
        const entry = containers.get(id);
        if (!entry) {
          stats.upgradeFailures += 1;
          log(`textContainerUpgrade：未知容器 ${String(data.containerID)}/${String(data.containerName)} → false`);
          return false;
        }
        const content = String(data.content ?? '');
        // textContainerUpgrade 的官方上限是 2000 字符，比建页宽松一倍
        const bytes = utf8.encode(content).length;
        if (bytes > CONTENT_BYTES_LIMIT * 2) {
          stats.upgradeFailures += 1;
          log(`textContainerUpgrade：内容 ${bytes} 字节超限 → false`);
          return false;
        }
        entry.def = { ...entry.def, content };
        paint(entry);
        return true;
      }
      case 'audioControl': {
        const isOpen = Boolean(data.isOpen);
        return isOpen ? startMic() : stopMic();
      }
      case 'imuControl':
        log(`imuControl(${JSON.stringify(data)})：mock 不产生 IMU 数据`);
        return true;
      case 'shutDownPageContainer': {
        const exitMode = Number(data.exitMode ?? 0);
        log(`shutDownPageContainer(exitMode=${exitMode})`);
        stopMic();
        showExitOverlay(exitMode);
        return true;
      }
      case 'setLocalStorage':
        window.localStorage.setItem(STORAGE_PREFIX + String(data.key), String(data.value ?? ''));
        return true;
      case 'getLocalStorage':
        return window.localStorage.getItem(STORAGE_PREFIX + String(data.key)) ?? '';
      case 'getUserInfo':
        return { uid: 1, name: 'Harness 用户', avatar: '', country: 'CN' };
      case 'getGlassesInfo':
        // bridge.getDeviceInfo() 走的就是这个方法（实测），返回 DeviceInfo 的 JSON 形状
        return { model: 'g2', sn: 'MOCK-G2-0001', status: { ...deviceStatus } };
      default:
        log(`未实现的宿主方法：${envelope.method}`);
        throw new Error(`mock: unsupported method ${envelope.method}`);
    }
  }

  let deviceStatus: Record<string, unknown> = {
    sn: 'MOCK-G2-0001',
    connectType: 'connected',
    isWearing: true,
    batteryLevel: 86,
    isCharging: false,
    isInCase: false,
  };

  (window as unknown as Record<string, unknown>).flutter_inappwebview = {
    callHandler(name: string, raw: unknown): unknown {
      if (name !== 'evenAppMessage') {
        throw new Error(`mock: unknown handler ${name}`);
      }
      const envelope = (typeof raw === 'string' ? JSON.parse(raw) : raw) as CallEnvelope;
      if (envelope.type !== 'call_even_app_method') {
        throw new Error(`mock: unknown message type ${envelope.type}`);
      }
      // 故障注入：模拟 BLE 卡死（永不 settle）——插件必须靠自己的 5s 超时兜住
      if (faults.bridgeHang) {
        log(`故障注入：${envelope.method} 永不返回（模拟 BLE 卡死）`);
        return new Promise(() => {});
      }
      if (faults.bridgeDelayMs > 0) {
        return new Promise((resolve) => {
          window.setTimeout(() => resolve(handleCall(envelope)), faults.bridgeDelayMs);
        });
      }
      return handleCall(envelope);
    },
  };

  // ---------- WebSocket 跟踪（断网模拟） ----------

  const liveSockets = new Set<WebSocket>();
  const NativeWebSocket = window.WebSocket;
  const TrackedWebSocket = class extends NativeWebSocket {
    constructor(url: string | URL, protocols?: string | string[]) {
      super(url, protocols);
      liveSockets.add(this);
      this.addEventListener('close', () => liveSockets.delete(this));
    }
  };
  (window as unknown as Record<string, unknown>).WebSocket = TrackedWebSocket;

  log('宿主 mock 已注入（flutter_inappwebview.callHandler 就绪，字形/折行走官方 pretext）');

  return {
    simulateGesture(kind: GestureKind, source: GestureSource = 'glassesR') {
      const code = GESTURE_EVENT[kind];
      log(`推送 sysEvent：${kind} / ${source}（eventType=${code === 0 ? '缺省' : code}）`);
      pushSysEvent(code, source);
    },
    simulateForegroundExit() {
      log('推送 FOREGROUND_EXIT_EVENT(5)：前台交互层关闭，**页面仍挂载** —— 插件不该断 WS');
      pushHubEvent('sysEvent', { eventType: EVT.FOREGROUND_EXIT });
    },
    simulateForegroundEnter() {
      log('推送 FOREGROUND_ENTER_EVENT(4)：重新回到前台');
      pushHubEvent('sysEvent', { eventType: EVT.FOREGROUND_ENTER });
    },
    simulateExit(abnormal = false) {
      const code = abnormal ? EVT.ABNORMAL_EXIT : EVT.SYSTEM_EXIT;
      log(`推送 ${abnormal ? 'ABNORMAL' : 'SYSTEM'}_EXIT_EVENT(${code})：真正的销毁`);
      pushHubEvent('sysEvent', { eventType: code });
    },
    killSockets() {
      const n = liveSockets.size;
      for (const ws of [...liveSockets]) ws.close();
      log(`断网模拟：关闭了 ${n} 个 WebSocket`);
      return n;
    },
    pushDeviceStatus(partial?: Record<string, unknown>) {
      deviceStatus = { ...deviceStatus, ...partial };
      log(`推送 deviceStatusChanged：电量 ${String(deviceStatus.batteryLevel)}%`);
      pushToSdk('deviceStatusChanged', { ...deviceStatus });
    },
    screenText() {
      const out: Record<string, string[]> = {};
      for (const [id, entry] of containers) {
        out[entry.def.containerName ?? String(id)] = entry.lines;
      }
      return out;
    },
    get micOpen() {
      return micOpen;
    },
    faults,
    stats,
  };
}
