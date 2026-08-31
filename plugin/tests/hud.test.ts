/**
 * 字形在库校验 —— 用官方 `@evenrealities/pretext` 逐字检查**插件会下发到眼镜的每个字符**。
 *
 * 为什么必须有这个测试：G2 字库覆盖有限，字库外的字符固件**静默跳过、不画豆腐块**
 * （官方 /docs/build/display）。仓库早期用的 `⛓ ◉ ◔ ⚙ ▸ ✓ ✕ ⚠ ⏸ ⏹` 十个字形全部不在库 ——
 * 状态条最左边那个"0.5 秒瞥视锚点"在真机上根本不会出现，而本地浏览器里一切正常。
 * 判据：`getAdvW(cp) === 0` ⇔ 回退链（evenroster → crylgrek → cn → emoji）无任何字体覆盖它。
 */
import { getAdvW } from '@evenrealities/pretext';
import { describe, expect, it } from 'vitest';
import contract from '../../protocol/hud-contract.json';
import { BLANK, GLYPHS, HUD_TEXT, LAYOUT, LINE_HEIGHT, CANVAS, EVENT_CAPTURE_CONTAINER } from '../src/hud';

function missing(text: string): string[] {
  const out: string[] = [];
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if (ch !== '\n' && getAdvW(cp) === 0) {
      out.push(`${ch} U+${cp.toString(16).toUpperCase().padStart(4, '0')}`);
    }
  }
  return out;
}

describe('字形在库（G2 字库）', () => {
  it('symbol 档每个语义字形都在库', () => {
    for (const [name, glyph] of Object.entries(GLYPHS)) {
      expect(missing(glyph), `symbol.${name} = ${glyph}`).toEqual([]);
    }
  });

  it.each(Object.keys(contract.glyphProfiles))('%s 档每个语义字形都在库', (profile) => {
    const table = (contract.glyphProfiles as Record<string, Record<string, string>>)[profile];
    for (const [name, glyph] of Object.entries(table)) {
      expect(missing(glyph), `${profile}.${name} = ${glyph}`).toEqual([]);
    }
  });

  it('插件自己写死、会直接上屏的文案全部在库', () => {
    for (const [key, text] of Object.entries(HUD_TEXT)) {
      expect(missing(text), `HUD_TEXT.${key} = ${text}`).toEqual([]);
    }
    expect(missing(BLANK)).toEqual([]);
  });

  it('被替换掉的旧字形确实不在库（否则这份替换表就是白改的）', () => {
    const replaced = contract._replaces as Record<string, string>;
    for (const [key, note] of Object.entries(replaced)) {
      if (key.startsWith('_')) continue;
      const glyph = [...note][0];
      if (key === 'bullet') {
        // `·` 是唯一一个"在库但不合适"的：它是中文间隔号，属行首禁排字符
        expect(missing(glyph), `bullet 的原字形 ${glyph} 应当在库`).toEqual([]);
        continue;
      }
      expect(missing(glyph).length, `${key} 的原字形 ${note} 本应缺失`).toBe(1);
    }
  });
});

describe('HUD 契约自洽', () => {
  it('三个容器铺满 576×288 且高度都是 27px 的整数倍容量', () => {
    let y = 0;
    for (const c of LAYOUT) {
      expect(c.x).toBe(0);
      expect(c.y).toBe(y);
      expect(c.w).toBe(CANVAS.width);
      expect(c.lines).toBe(Math.floor(c.h / LINE_HEIGHT));
      y += c.h;
    }
    expect(y).toBe(CANVAS.height);
  });

  it('恰好一个容器持有 isEventCapture', () => {
    expect(LAYOUT.filter((c) => c.name === EVENT_CAPTURE_CONTAINER)).toHaveLength(1);
  });

  it('textColor 全部落在 SDK 的 0~4 五级亮度内', () => {
    for (const c of LAYOUT) {
      expect(c.textColor).toBeGreaterThanOrEqual(0);
      expect(c.textColor).toBeLessThanOrEqual(4);
    }
  });

  it('containerName ≤ 16 字符', () => {
    for (const c of LAYOUT) expect(c.name.length).toBeLessThanOrEqual(16);
  });
});
