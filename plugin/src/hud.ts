/**
 * HUD 契约（画布几何、容器版式、语义字形）——**与网关读同一个文件**。
 *
 * 单一真源是 `protocol/hud-contract.json`：网关的 `lens_gateway/formatting/layout.py`
 * 和 `glyphs.py` 读它，本模块也读它。版式或字形只要改一处，两端同时生效，
 * 不可能出现"服务器按 8 行分页、插件按 5 行建容器"这种漂移。
 *
 * 字形为什么不能随便挑：G2 的字库覆盖有限，字库外的字符固件画不出来
 * （官方文档记为静默跳过，官方度量库记为 4px 占位框）。仓库早期用的
 * `⛓ ◉ ◔ ⚙ ▸ ✓ ✕ ⚠ ⏸ ⏹` 十个字形全部不在库。
 * `tests/hud.test.ts` 用官方 `@evenrealities/pretext` 逐字校验本文件下发的每个字符。
 */
import contract from '../../protocol/hud-contract.json';
import type { FrameContainers } from './types';
import { lang } from './strings';

export interface ContainerSpec {
  id: number;
  name: keyof FrameContainers;
  x: number;
  y: number;
  w: number;
  h: number;
  /** 该容器能显示几行（floor(h / 27)），由契约声明并在网关侧断言过 */
  lines: number;
  /** 文本亮度 0~4（SDK 0.0.14+）。省略即设备默认 4。 */
  textColor?: number;
}

/** 可寻址画布：576×288 px/眼（物理面板另有 640×350，不是可寻址范围）。 */
export const CANVAS: { width: number; height: number } = {
  width: contract.canvas.width,
  height: contract.canvas.height,
};

/** LVGL 固定行高，G2 上恒为 27px —— 不可配，也没有字号控制。 */
export const LINE_HEIGHT: number = contract.lineHeight;

/** 三容器版式（协议第 4 节）。 */
export const LAYOUT: ReadonlyArray<ContainerSpec> = contract.containers as ContainerSpec[];

/** 哪个容器持有 isEventCapture=1（一页有且仅有一个）。 */
export const EVENT_CAPTURE_CONTAINER: string = contract.eventCaptureContainer;

export type GlyphName = keyof typeof contract.glyphProfiles.symbol;

/** 生效的字形档位。插件侧固定用 symbol；网关侧可由配置切换。 */
export const GLYPHS: Record<GlyphName, string> = contract.glyphProfiles.symbol;

/** 把 `{glyphName}` 占位替换成实际字形。 */
function render(template: string): string {
  return template.replace(/\{(\w+)\}/g, (whole, name: string) => {
    const g = (GLYPHS as Record<string, string | undefined>)[name];
    return g ?? whole;
  });
}

/**
 * 插件自己写死、会直接下发到眼镜的文案 —— 网关还没连上时屏幕上的字只能由插件来写。
 * 集中在契约里的唯一目的：让字形测试能一次扫完所有会上屏的字符串。
 *
 * `ALL_HUD_TEXT` 存在是因为**测试必须扫所有语言**：只扫当前选中的那个，
 * 切了 locale 另一种语言就再也没被检查过，而字库外的字符在真机上什么都不画。
 */
export const ALL_HUD_TEXT = {
  zh: {
    booting: render(contract.hudText.zh.booting),
    linkLost: render(contract.hudText.zh.linkLost),
  },
  en: {
    booting: render(contract.hudText.en.booting),
    linkLost: render(contract.hudText.en.linkLost),
  },
} as const;

/** 当前语言的那一份。运行时只用这个。 */
export const HUD_TEXT: { readonly booting: string; readonly linkLost: string } = ALL_HUD_TEXT[lang];

/** 空内容占位。protobuf 零值字段会被省略，空串到不了固件，用单空格做"清空"。 */
export const BLANK = ' ';
