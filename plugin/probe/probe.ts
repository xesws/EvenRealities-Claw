/**
 * 官方模拟器探针页 —— 把几个"只有真的送进固件渲染栈才能回答"的问题排成一队，
 * 由 `tools/g2probe.mjs` 通过自动化 HTTP 接口逐步驱动、逐步截图。
 *
 * 为什么需要它（pretext 与 SDK 校验器都回答不了这几个）：
 *   1. 仓库 LAYOUT 正好铺满 576×288，官方示例最大只到 420×270 —— 会不会被判 oversize？
 *   2. 内容上限到底是 1000 **字符**（SDK README）还是 999 **字节**（模拟器 v0.7.1 更新日志）？
 *   3. 字库外字符究竟是"静默跳过"（官方文档站）还是画占位框
 *      （模拟器 v0.7.0 说它开了 `LV_USE_FONT_PLACEHOLDER` 并用 lvgl 的 `g2` feature 对齐固件）？
 *
 * 步进方式：**用模拟器自己的 `/api/input` 点击**。第一屏走 createStartUpPageContainer
 * （一个页面生命周期只能调一次），之后每收到一次 CLICK 就 rebuildPageContainer 换下一屏 ——
 * 顺带把"建页只能一次 + rebuild 接力"这条真机约束也跑了一遍。
 */
import {
  CreateStartUpPageContainer,
  RebuildPageContainer,
  TextContainerProperty,
  waitForEvenAppBridge,
} from '@evenrealities/even_hub_sdk';
import { LAYOUT } from '../src/hud';

interface ProbeContainer {
  id: number;
  name: string;
  x: number;
  y: number;
  w: number;
  h: number;
  content: string;
  capture?: boolean;
  textColor?: number;
}

interface ProbeStep {
  id: string;
  label: string;
  /** 供 g2probe.mjs 做像素判定用的元信息 */
  meta?: Record<string, unknown>;
  containers: ProbeContainer[];
}

/** 一整屏就一个文本容器，铺满画布，事件捕获挂在它身上（否则 /api/input 点不动）。 */
function fullScreen(id: string, label: string, content: string, meta?: Record<string, unknown>): ProbeStep {
  return {
    id,
    label,
    meta,
    containers: [
      { id: 1, name: 'probe', x: 0, y: 0, w: 576, h: 288, content, capture: true, textColor: 4 },
    ],
  };
}

/**
 * 每行一个字形，行号即 floor(y/27)，g2probe 按行带判定"这一行有没有墨"。
 * 一屏最多 floor(288/27)=10 行 —— 多出来的会掉到画布外，看起来像"画不出"，
 * 所以这里强制切片，超过 10 个字形自动分屏。
 */
const MAX_ROWS = Math.floor(288 / 27);
function glyphLines(id: string, label: string, glyphs: string[]): ProbeStep[] {
  const out: ProbeStep[] = [];
  for (let i = 0; i < glyphs.length; i += MAX_ROWS) {
    const slice = glyphs.slice(i, i + MAX_ROWS);
    const n = out.length + 1;
    const total = Math.ceil(glyphs.length / MAX_ROWS);
    out.push(fullScreen(total > 1 ? `${id}-${n}` : id, total > 1 ? `${label}（${n}/${total}）` : label, slice.join('\n'), { glyphs: slice }));
  }
  return out;
}

/** 官方字形表判为**不在 G2 字库**的 10 个（仓库早期全在用）。 */
const MISSING = ['◉', '◔', '▸', '⚙', '⚠', '⛓', '✓', '✕', '⏸', '⏹'];
/** 现役 symbol 档的替代字形 + 三个此前存疑的（… ‹ ›）。 */
const SUBSTITUTES = ['·', '●', '→', '◐', '◆', '▶', '√', '×', '！', '‖', '■', '▌', '‹', '›', '…', '•'];

const HUD_SAMPLE: Record<string, string> = {
  status: '● 聆听 0:07',
  body: '像素盒分页：正文容器 576×216px，行高固定 27px，因此每页正好八行。这一段是用来确认真实排版容量的样本文本，请数一数它在屏幕上占了几行。',
  foot: '‹ 1/2 ›',
};

const STEPS: ProbeStep[] = [
  {
    id: 'layout',
    label: '仓库真实 LAYOUT（铺满 576×288，官方示例最大只到 420×270）',
    containers: LAYOUT.map((c) => ({
      id: c.id,
      name: c.name,
      x: c.x,
      y: c.y,
      w: c.w,
      h: c.h,
      content: HUD_SAMPLE[c.name] ?? c.name,
      capture: c.name === 'foot',
      textColor: c.textColor,
    })),
  },
  ...glyphLines('glyphs-missing', '官方字形表判为缺失的 10 个字形，每行一个', MISSING),
  ...glyphLines('glyphs-substitutes', '现役 symbol 档字形 + 三个存疑字形，每行一个', SUBSTITUTES),
  fullScreen(
    'ruler',
    '行高与每行容量标尺：十行 × 每行 30 个汉字（超出 576px 的部分应被折走）',
    Array.from({ length: 10 }, (_, i) => `${i}${'汉'.repeat(29)}`).join('\n'),
    { rows: 10 },
  ),
  fullScreen('bytes-999', '内容 999 字节（333 汉字），刚好卡在模拟器 v0.7.1 的字节上限', '中'.repeat(333), {
    bytes: 999,
    chars: 333,
  }),
  fullScreen('bytes-1002', '内容 1002 字节（334 汉字），超出字节上限但远低于 1000 字符', '中'.repeat(334), {
    bytes: 1002,
    chars: 334,
  }),
  fullScreen('chars-1000', '内容 1000 个 ASCII 字符（= 1000 字节），卡在字符口径上限', 'a'.repeat(1000), {
    bytes: 1000,
    chars: 1000,
  }),
];

const out = document.getElementById('out') as HTMLElement;
let cursor = 0;

function build(step: ProbeStep): TextContainerProperty[] {
  return step.containers.map(
    (c) =>
      new TextContainerProperty({
        containerID: c.id,
        containerName: c.name,
        xPosition: c.x,
        yPosition: c.y,
        width: c.w,
        height: c.h,
        borderWidth: 0,
        paddingLength: 0,
        content: c.content,
        isEventCapture: c.capture ? 1 : 0,
        ...(c.textColor === undefined ? {} : { textColor: c.textColor }),
      }),
  );
}

async function main(): Promise<void> {
  const bridge = await waitForEvenAppBridge();

  async function show(index: number): Promise<void> {
    const step = STEPS[index];
    const textObject = build(step);
    let result: unknown;
    let error: string | null = null;
    try {
      result =
        index === 0
          ? await bridge.createStartUpPageContainer(
              new CreateStartUpPageContainer({ containerTotalNum: textObject.length, textObject }),
            )
          : await bridge.rebuildPageContainer(
              new RebuildPageContainer({ containerTotalNum: textObject.length, textObject }),
            );
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
    const payload = {
      index,
      total: STEPS.length,
      id: step.id,
      label: step.label,
      api: index === 0 ? 'createStartUpPageContainer' : 'rebuildPageContainer',
      result,
      error,
      meta: step.meta ?? {},
      containers: step.containers.map((c) => ({
        name: c.name,
        box: `${c.x},${c.y} ${c.w}x${c.h}`,
        bytes: new TextEncoder().encode(c.content).length,
        chars: [...c.content].length,
      })),
    };
    // g2probe.mjs 靠这一行前缀在 /api/console 里定位结果
    console.log(`PROBE_RESULT ${JSON.stringify(payload)}`);
    out.textContent = JSON.stringify(payload, null, 2);
  }

  bridge.onEvenHubEvent((event) => {
    const sys = (event as { sysEvent?: { eventType?: unknown } }).sysEvent;
    if (!sys) return;
    // CLICK_EVENT=0 是 protobuf 零值，到达时是 undefined
    if (sys.eventType !== undefined && sys.eventType !== 0) return;
    if (cursor + 1 >= STEPS.length) {
      console.log('PROBE_DONE');
      return;
    }
    cursor += 1;
    void show(cursor);
  });

  await show(0);
}

void main().catch((err: unknown) => {
  const msg = err instanceof Error ? err.message : String(err);
  console.log(`PROBE_RESULT ${JSON.stringify({ error: msg })}`);
  out.textContent = `probe 失败：${msg}`;
});
