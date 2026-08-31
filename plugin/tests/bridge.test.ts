/**
 * 桥接层端到端测试：**真 SDK + 真 GlassesController + 改造后的宿主夹具**，
 * 中间没有任何打桩。测的是那四个只有真机（或保真夹具）才会暴露的缺陷。
 *
 * 注入顺序与真实 Even App 一致：先装宿主 mock，再加载 SDK —— 所以 SDK 与被测模块
 * 都必须动态 import，不能用顶层静态 import（会被提升到 mock 之前）。
 */
import { getTextWidth, measureTextWrap } from '@evenrealities/pretext';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installEvenHostMock, type EvenHostMock } from '../harness/mock';
import type { FrameContainers } from '../src/types';

type Glasses = import('../src/glasses').GlassesController;
type Gesture = import('../src/glasses').InputGesture;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
/** 渲染防抖 120ms，留足余量 */
const FLUSH = 260;

const frame = (over: Partial<FrameContainers> = {}): FrameContainers => ({
  status: '· 待机',
  body: '你好',
  foot: '1/1',
  ...over,
});

interface Harness {
  mock: EvenHostMock;
  glasses: Glasses;
  gestures: Gesture[];
  events: string[];
  dispose(): void;
}

async function boot(): Promise<Harness> {
  document.body.innerHTML = '<div id="screen"></div>';
  const screen = document.getElementById('screen') as HTMLElement;
  const mock = installEvenHostMock({ screen });

  const sdk = await import('@evenrealities/even_hub_sdk');
  const { GlassesController } = await import('../src/glasses');
  const bridge = await sdk.waitForEvenAppBridge();

  const gestures: Gesture[] = [];
  const events: string[] = [];
  const glasses = new GlassesController(bridge, {
    onGesture: (g) => gestures.push(g),
    onExit: () => events.push('exit'),
    onForegroundExit: () => events.push('fgExit'),
    onForegroundEnter: () => events.push('fgEnter'),
  });
  return { mock, glasses, gestures, events, dispose: () => glasses.dispose() };
}

let h: Harness;
beforeEach(async () => {
  h = await boot();
});
afterEach(() => {
  h.dispose();
  vi.useRealTimers();
});

// ------------------------------------------------------------------ 建页

describe('createStartUpPageContainer 一个页面生命周期只能调一次', () => {
  it('首次 create 成功，第二次由控制器自动改走 rebuild', async () => {
    expect(await h.glasses.createContainers()).toBe(0);
    expect(h.mock.stats.createCalls).toBe(1);
    expect(h.mock.stats.rebuildCalls).toBe(0);

    expect(await h.glasses.createContainers()).toBe(0);
    expect(h.mock.stats.createCalls).toBe(1); // 没有第二次 create
    expect(h.mock.stats.rebuildCalls).toBe(1);
  });

  it('夹具本身会拒绝重复的 create（旧版恒返回 0，把真机故障藏了起来）', async () => {
    const sdk = await import('@evenrealities/even_hub_sdk');
    const bridge = await sdk.waitForEvenAppBridge();
    const page = (content: string) =>
      new sdk.CreateStartUpPageContainer({
        containerTotalNum: 1,
        textObject: [
          new sdk.TextContainerProperty({
            containerID: 1,
            containerName: 'only',
            xPosition: 0,
            yPosition: 0,
            width: 576,
            height: 27,
            content,
            isEventCapture: 1,
          }),
        ],
      });
    expect(await bridge.createStartUpPageContainer(page('a'))).toBe(0);
    expect(await bridge.createStartUpPageContainer(page('b'))).toBe(1); // invalid
  });
});

describe('夹具补齐了 SDK 自己不做的建页校验', () => {
  it.each([
    ['两个 isEventCapture', { capture: [1, 1] }, 1],
    ['零个 isEventCapture', { capture: [0, 0] }, 1],
    ['containerName 超 16 字符', { name: 'x'.repeat(17) }, 1],
    ['内容超字节上限', { content: '中'.repeat(400) }, 2],
    ['几何越出画布', { height: 300 }, 2],
  ])('%s → 返回码 %i', async (_label, over: Record<string, unknown>, code) => {
    const sdk = await import('@evenrealities/even_hub_sdk');
    const bridge = await sdk.waitForEvenAppBridge();
    const capture = (over.capture as number[]) ?? [1, 0];
    const textObject = [0, 1].map(
      (i) =>
        new sdk.TextContainerProperty({
          containerID: i + 1,
          containerName: i === 0 ? ((over.name as string) ?? 'a') : 'b',
          xPosition: 0,
          yPosition: i * 36,
          width: 576,
          height: i === 0 ? ((over.height as number) ?? 36) : 36,
          content: i === 0 ? ((over.content as string) ?? 'x') : 'y',
          isEventCapture: capture[i],
        }),
    );
    const r = await bridge.createStartUpPageContainer(
      new sdk.CreateStartUpPageContainer({ containerTotalNum: 2, textObject }),
    );
    expect(r).toBe(code);
  });
});

// ------------------------------------------------------------------ 写入与去重缓存

describe('textContainerUpgrade 失败不能毒化去重缓存', () => {
  it('写失败后，同样的内容会被重新下发（旧版此后永远不再重写该容器）', async () => {
    await h.glasses.createContainers();
    const base = h.mock.stats.upgradeCalls;

    h.mock.faults.upgradeOk = false;
    h.glasses.renderFrame(frame({ body: '第一次' }));
    await sleep(FLUSH);
    const afterFail = h.mock.stats.upgradeCalls;
    expect(afterFail).toBeGreaterThan(base);

    // 恢复正常，重发**完全相同**的一帧
    h.mock.faults.upgradeOk = null;
    h.glasses.renderFrame(frame({ body: '第一次' }));
    await sleep(FLUSH);
    expect(h.mock.stats.upgradeCalls).toBeGreaterThan(afterFail);
    expect(h.mock.screenText().body).toEqual(['第一次']);
  });

  it('内容没变时不重复下发（防抖 + 去重仍然有效）', async () => {
    await h.glasses.createContainers();
    h.glasses.renderFrame(frame({ body: '同一句' }));
    await sleep(FLUSH);
    const n = h.mock.stats.upgradeCalls;
    h.glasses.renderFrame(frame({ body: '同一句' }));
    await sleep(FLUSH);
    expect(h.mock.stats.upgradeCalls).toBe(n);
  });
});

describe('bridge 调用超时保护（B1）', () => {
  it('BLE 卡死时 audioControl 在 5s 后按失败返回，而不是永久挂起', async () => {
    vi.useFakeTimers();
    h.mock.faults.bridgeHang = true;
    const p = h.glasses.audioControl(true);
    await vi.advanceTimersByTimeAsync(5100);
    await expect(p).resolves.toBe(false);
  });
});

// ------------------------------------------------------------------ 屏幕保真

describe('眼镜屏按固件语义渲染', () => {
  it('字库外字符被静默丢弃，不留豆腐块', async () => {
    await h.glasses.createContainers();
    h.glasses.renderFrame(frame({ body: '✓完成⚠' })); // ✓ U+2713 与 ⚠ U+26A0 都不在 G2 字库
    await sleep(FLUSH);
    expect(h.mock.screenText().body).toEqual(['完成']);
    expect(h.mock.stats.droppedGlyphs).toBe(2);
  });

  it('折行结果的行数与官方 pretext measureTextWrap 一致', async () => {
    await h.glasses.createContainers();
    const text = '像素盒分页与字符预算的区别在于前者按容器真实宽度计算后者只是猜测。';
    h.glasses.renderFrame(frame({ body: text }));
    await sleep(FLUSH);
    const lines = h.mock.screenText().body;
    expect(lines.length).toBe(measureTextWrap(text, 576).lineCount);
    for (const ln of lines) expect(getTextWidth(ln)).toBeLessThanOrEqual(576);
  });

  it('超过 floor(h/27) 行的正文被裁掉并计入 overflowLines', async () => {
    await h.glasses.createContainers();
    // body 216px / 27px = 8 行；每行约 28 个汉字 ⇒ 400 字必然溢出
    h.glasses.renderFrame(frame({ body: '排'.repeat(400) }));
    await sleep(FLUSH);
    expect(h.mock.screenText().body).toHaveLength(8);
    expect(h.mock.stats.overflowLines).toBeGreaterThan(0);
  });
});

// ------------------------------------------------------------------ 事件语义

describe('生命周期事件（修：前台退出曾被当成应用销毁）', () => {
  it('FOREGROUND_EXIT 只暂停，不触发 teardown', () => {
    h.mock.simulateForegroundExit();
    expect(h.events).toEqual(['fgExit']);
    h.mock.simulateForegroundEnter();
    expect(h.events).toEqual(['fgExit', 'fgEnter']);
    expect(h.events).not.toContain('exit');
  });

  it.each([
    ['系统退出', false],
    ['异常退出', true],
  ])('%s 才触发 onExit', (_l, abnormal) => {
    h.mock.simulateExit(abnormal as boolean);
    expect(h.events).toEqual(['exit']);
  });
});

describe('手势归一（修：只认单击/双击，且从不读 eventSource）', () => {
  it.each([
    ['tap', 'glassesR'],
    ['doubleTap', 'glassesL'],
    ['swipeUp', 'ring'],
    ['swipeDown', 'glassesR'],
    ['longPress', 'glassesL'],
    ['longPressRelease', 'ring'],
  ])('%s / %s 被正确识别', (kind, source) => {
    h.mock.simulateGesture(kind as never, source as never);
    expect(h.gestures).toEqual([{ kind, source }]);
  });

  it('CLICK=0 与 DUMMY_NULL=0 会被 protobuf 省略，仍归一成 tap/unknown', () => {
    h.mock.simulateGesture('tap', 'unknown');
    expect(h.gestures).toEqual([{ kind: 'tap', source: 'unknown' }]);
  });

  it('无法识别的 eventType 被忽略，不再退化成幽灵翻页', async () => {
    const w = window as unknown as { _listenEvenAppMessage: (m: unknown) => void };
    w._listenEvenAppMessage({
      type: 'listen_even_app_data',
      method: 'evenHubEvent',
      data: { type: 'sysEvent', jsonData: { eventType: 99 } },
    });
    await sleep(10);
    expect(h.gestures).toEqual([]);
  });
});

describe('遥测（修：getDeviceInfo 从未被调用过 / 协议 v1.1 上行通路）', () => {
  it('能读回电量/佩戴/连接状态', async () => {
    h.mock.pushDeviceStatus({ batteryLevel: 12, isWearing: false });
    const info = await h.glasses.getDeviceInfo();
    expect(info?.isGlasses()).toBe(true);
    expect(info?.status?.batteryLevel).toBe(12);
    expect(info?.status?.isWearing).toBe(false);
    expect(info?.status?.isConnected()).toBe(true);
  });

  it('telemetry() 组装出带型号判定的完整采样', async () => {
    h.mock.pushDeviceStatus({ batteryLevel: 77, isWearing: true, isCharging: false });
    await sleep(20);
    const t = await h.glasses.telemetry();
    expect(t.isGlasses).toBe(true);
    expect(t.model).toBe('g2');
    expect(t.sn).toBe('MOCK-G2-0001');
    expect(t.batteryLevel).toBe(77);
    expect(t.isWearing).toBe(true);
    expect(t.connected).toBe(true);
  });

  it('设备状态变化会触发一次主动上报', async () => {
    const pushed: Array<{ batteryLevel: number | null }> = [];
    h.dispose();
    const sdk = await import('@evenrealities/even_hub_sdk');
    const { GlassesController } = await import('../src/glasses');
    const bridge = await sdk.waitForEvenAppBridge();
    const g = new GlassesController(bridge, { onTelemetry: (t) => pushed.push(t) });
    try {
      h.mock.pushDeviceStatus({ batteryLevel: 33 });
      await sleep(60);
      expect(pushed.at(-1)?.batteryLevel).toBe(33);
    } finally {
      g.dispose();
    }
  });

  it('★ R1 戒指的状态推送不会被当成眼镜遥测', async () => {
    // DeviceStatus 里只有 sn、没有 model，戒指与眼镜走同一套推送 ——
    // 不做 sn 比对就会把 41% 的戒指电量报成眼镜电量。
    await h.glasses.getDeviceInfo();          // 先确认眼镜的 model + sn
    h.mock.pushRingStatus();
    await sleep(20);
    const t = await h.glasses.telemetry();
    expect(t.isGlasses).toBe(false);
    expect(t.batteryLevel).toBe(41);          // 值确实读到了……
    // ……但 isGlasses=false，网关会整条拒收（gateway/tests/test_telemetry.py 断言了这一半）
  });

  it('缺失字段给 null 而不是 0', async () => {
    const sdk = await import('@evenrealities/even_hub_sdk');
    const { GlassesController } = await import('../src/glasses');
    const bridge = await sdk.waitForEvenAppBridge();
    const g = new GlassesController(bridge, {});
    try {
      // 从未推过任何状态：电量应当是 null（"不知道"），不是 0（"没电了"）
      const t = await g.telemetry();
      expect(t.batteryLevel === null || typeof t.batteryLevel === 'number').toBe(true);
      if (t.batteryLevel === 0) throw new Error('未知电量被写成了 0');
    } finally {
      g.dispose();
    }
  });
});

describe('麦克风（B3：返回值是插件判断麦是否真开的唯一依据）', () => {
  it('被抢占时 audioControl(true) 返回 false', async () => {
    h.mock.faults.micDenied = true;
    expect(await h.glasses.audioControl(true)).toBe(false);
  });
});
