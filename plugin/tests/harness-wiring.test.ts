/**
 * harness.html 与 harness.ts 的接线一致性。
 *
 * `harness.ts` 里的 `$(id)` 找不到元素时会**在页面加载时直接抛异常**，整个模拟器打不开。
 * 这类错误 tsc 查不出来（id 是字符串），又不值得为它跑一个真浏览器 —— 静态比对就够了。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const html = read('../harness/harness.html');
const ts = read('../harness/harness.ts');

/** harness.ts 里所有 $('x') / $sel('x') 引用到的 id */
const referenced = [...ts.matchAll(/\$(?:sel)?\('([^']+)'\)/g)].map((m) => m[1]);
/** harness.html 里所有 id="x" */
const declared = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));

describe('浏览器夹具接线', () => {
  it('每个被引用的 id 都存在于 harness.html', () => {
    const missing = [...new Set(referenced)].filter((id) => !declared.has(id));
    expect(missing).toEqual([]);
  });

  it('引用集合非空（防止正则失效导致这个测试变成空断言）', () => {
    expect(referenced.length).toBeGreaterThan(8);
  });

  it('故障注入开关都接了事件', () => {
    for (const id of ['fxUpgradeFail', 'fxMicDenied', 'fxHang', 'fxDelay']) {
      expect(declared.has(id), `harness.html 缺少 #${id}`).toBe(true);
      expect(ts.includes(`'${id}'`), `harness.ts 未接线 #${id}`).toBe(true);
    }
  });

  it('五种手势与四个来源都能从下拉框选到', () => {
    for (const kind of ['tap', 'doubleTap', 'swipeUp', 'swipeDown', 'longPress', 'longPressRelease']) {
      expect(html.includes(`value="${kind}"`), `缺少手势选项 ${kind}`).toBe(true);
    }
    for (const src of ['glassesR', 'glassesL', 'ring', 'unknown']) {
      expect(html.includes(`value="${src}"`), `缺少来源选项 ${src}`).toBe(true);
    }
  });
});
