/**
 * 按住说话：状态机 + 松手看门狗（缺口二 / 镜腿长按）。
 *
 * 这个文件盯的是两类真机故障，两类都不会在界面上留下任何痕迹：
 * 1. **顺序**：先发 ptt start 再异步开麦 → 网关只给 1.4s 等第一块 PCM，
 *    真机上几乎必然误报"麦克风没有声音"（B2/B3）。所以断言用的是**有序日志**，
 *    "两个都被调用了"在这里等于没测。
 * 2. **松手事件丢帧**：`longPressRelease` 走 BLE，丢一帧就永远不来。
 *    网关那头 25s 后自己停了聆听，插件这头麦克风还开着、按钮还写着"正在说"、
 *    PCM 还在往外推 —— 电量和流量都在烧，而用户看不出任何异常。
 *
 * 最后一个 describe 不测 `PttController`，测的是**接线**：
 * 从宿主推一个真的 `LONG_PRESS(9)` 系统事件进去，走 SDK → GlassesController →
 * `main.ts` 的 handleGesture，看按钮是不是真的进了"正在说"。
 * 绑定漏了、绑错了 kind，只有这条会红。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PttController, type PttDeps } from '../src/ptt';
import { t } from '../src/strings';
import { installEvenHostMock, type EvenHostMock } from '../harness/mock';

/** 手动控制 `audioControl(true)` 何时返回，用来复现"开麦还没回来就松手"。 */
function defer(): { promise: Promise<void>; release: () => void } {
  let release!: () => void;
  const promise = new Promise<void>((r) => (release = r));
  return { promise, release };
}

describe('PttController', () => {
  /** 所有副作用按发生顺序记在同一条时间线上 —— 顺序断言只能靠它。 */
  let log: string[];
  let toasts: string[];
  let ptt: PttController;
  /** `audioControl(true)` 的返回值：false = 麦克风被别的 App 占着 */
  let micOk: boolean;
  /** 非空时开麦会挂在这里，直到测试放行 */
  let micGate: { promise: Promise<void>; release: () => void } | null;

  /** 让 start() 里那个异步 IIFE 跑完（开麦是 await 出来的，同步断言看不到 sendStart）。 */
  const settle = () => vi.advanceTimersByTimeAsync(0);

  beforeEach(() => {
    vi.useFakeTimers();
    log = [];
    toasts = [];
    micOk = true;
    micGate = null;
    const deps: PttDeps = {
      audioControl: async (open) => {
        log.push(`mic:${open}`);          // 同步记，await 之前 —— 顺序断言要的就是这一刻
        if (open && micGate) await micGate.promise;
        return open ? micOk : true;
      },
      sendStart: () => log.push('sendStart'),
      sendStop: () => log.push('sendStop'),
      sendCancel: () => log.push('sendCancel'),
      setUi: (active) => log.push(`ui:${active}`),
      toast: (msg) => {
        log.push('toast');
        toasts.push(msg);
      },
    };
    ptt = new PttController(deps);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('★ 先开麦、确认成功，才告诉网关开始说话', async () => {
    ptt.start('gesture');
    // 同步阶段绝不能已经发出 start：网关只给 1.4s 等第一块 PCM，
    // 先 start 后开麦要在这 1.4s 里塞下 WS RTT + BLE 下发 + 固件启麦 + 首块回传。
    expect(log).toEqual(['ui:true', 'mic:true']);
    expect(ptt.active).toBe(true);

    await settle();
    expect(log).toEqual(['ui:true', 'mic:true', 'sendStart']);
  });

  it('开麦失败：不发 start、按钮复位、明确告诉用户', async () => {
    micOk = false;
    ptt.start('button');
    await settle();

    expect(log).toEqual(['ui:true', 'mic:true', 'ui:false', 'toast']);
    expect(toasts).toEqual([t.micFailed]);
    expect(ptt.active).toBe(false);
    // 开麦都没成，还发 start 就是让网关空等一个永远不会来的 PCM 流
    expect(log).not.toContain('sendStart');
  });

  it('★ 松手事件丢了：看门狗到点自己关麦 + 通知网关取消', async () => {
    ptt.start('gesture');
    await settle();
    log.length = 0;

    vi.advanceTimersByTime(29_999);
    expect(log).toEqual([]);          // 早一毫秒都不该动手
    vi.advanceTimersByTime(1);

    expect(log).toEqual(['ui:false', 'mic:false', 'sendCancel', 'toast']);
    expect(ptt.active).toBe(false);
    // 文案要说的是「没等到松手」而不是「麦克风没打开」—— 后者会把排障引到权限和蓝牙上去，
    // 而这条路上麦克风一直是好的。
    expect(toasts).toEqual([t.pttTimeout]);
  });

  it('看门狗默认 30s：**晚于**网关 25s 的软上限，正常超时该由网关先收尾', async () => {
    ptt.start('gesture');
    await settle();
    log.length = 0;

    // 网关的 max_utterance_seconds 默认 25s，它能把已收到的 PCM 正常转成一次提问。
    // 插件这条若不晚于它，就会抢先 cancel 掉一次本来能出结果的说话。
    vi.advanceTimersByTime(25_000);
    expect(log).toEqual([]);
  });

  it('★ 说完之后看门狗必须被清掉，stop 与 cancel 两条路都要清', async () => {
    ptt.start('gesture');
    await settle();
    ptt.stop();
    log.length = 0;
    // 定时器漏出去的话，用户说完一句话 30s 后会平白多一条提示
    vi.advanceTimersByTime(120_000);
    expect(log).toEqual([]);

    ptt.start('gesture');
    await settle();
    ptt.cancel();
    log.length = 0;
    vi.advanceTimersByTime(120_000);
    expect(log).toEqual([]);
  });

  it('手势起的 PTT 会同步手机按钮外观', async () => {
    ptt.start('gesture');
    await settle();
    // 不同步的话按钮会一直写着"按住说话"，而麦克风已经开着 ——
    // 顺带地，`setPttActive(true)` 会让手机按钮的 pointerdown 空转，这正是防双开那一半。
    expect(log[0]).toBe('ui:true');
  });

  it('★ 手势与手机按钮混用：不会双开，也不会双关', async () => {
    ptt.start('gesture');
    await settle();
    log.length = 0;

    ptt.start('button');            // 长按还没松，手指又戳了屏幕上的按钮
    await settle();
    expect(log).toEqual([]);        // 开两次麦 = 网关把一次说话切成两段

    ptt.stop();
    expect(log).toEqual(['ui:false', 'mic:false', 'sendStop']);

    log.length = 0;
    ptt.stop();                     // 松手事件到了两次（BLE 重传）
    expect(log).toEqual([]);
  });

  it('没在录音时 cancel 是 no-op —— 四条收尾路径随时可能在没说话时触发', async () => {
    // 手势退出 / 前台交互层关闭 / 应用退出 / beforeunload 全走 cancel()，
    // 其中三条随时可能发生。不判 active 就会往网关发一串莫名其妙的 ptt cancel。
    ptt.cancel();
    expect(log).toEqual([]);

    ptt.start('button');
    await settle();
    ptt.stop();
    log.length = 0;
    ptt.cancel();                   // 说完就退出应用
    expect(log).toEqual([]);
  });

  it('★ 开麦还没回来就松手：不能再发 start', async () => {
    micGate = defer();
    ptt.start('gesture');
    expect(log).toEqual(['ui:true', 'mic:true']);

    ptt.stop();                     // 开麦期间松手
    expect(log).toEqual(['ui:true', 'mic:true', 'ui:false', 'mic:false', 'sendStop']);

    micGate.release();              // 开麦这时才姗姗来迟地成功返回
    await settle();
    // 少了这个 re-entrancy 检查，网关会开始聆听一个已经被关掉的麦克风
    expect(log).not.toContain('sendStart');
    expect(ptt.active).toBe(false);
  });
});

describe('接线：镜腿长按真的走到 PttController', () => {
  let mock: EvenHostMock;

  /** 主屏的"按住说话"按钮。配对屏没走完时它是 hidden 的，但一直在 DOM 里。 */
  const pttBtn = (): HTMLElement =>
    document.querySelector<HTMLElement>('[data-el="pttBtn"]') as HTMLElement;

  beforeEach(async () => {
    vi.resetModules();
    document.body.innerHTML = '<div id="app"></div><div id="screen"></div>';
    mock = installEvenHostMock({ screen: document.getElementById('screen') as HTMLElement });
    // 宿主必须先在位：main.ts 一被 import 就跑 bootstrap()，那里等的就是它
    await import('../src/main');
    await vi.waitFor(() => expect(pttBtn()).not.toBeNull());
  });

  afterEach(() => {
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('★ LONG_PRESS(9) 从宿主一路走到"正在说"', () => {
    expect(pttBtn().textContent).toBe(t.ptt);
    mock.simulateGesture('longPress', 'glassesR');
    // 同步：这一刻开麦的 Promise 还没 settle，能看到的只有绑定本身的效果
    expect(pttBtn().classList.contains('active')).toBe(true);
    expect(pttBtn().textContent).toBe(t.pttActive);
  });

  it('★ LONG_PRESS_RELEASE(10) 同步复位（松手那半边也接上了）', () => {
    mock.simulateGesture('longPress', 'glassesR');
    mock.simulateGesture('longPressRelease', 'glassesR');
    expect(pttBtn().classList.contains('active')).toBe(false);
    expect(pttBtn().textContent).toBe(t.ptt);
  });

  it('来源不设限：戒指长按同样能说话', () => {
    mock.simulateGesture('longPress', 'ring');
    expect(pttBtn().classList.contains('active')).toBe(true);
  });

  // 这四条收尾路径都是修过真机 bug 的，重构成 `ptt.cancel()` 之后必须逐条回归：
  // 漏掉任何一条，用户离开时麦克风就一直开着，而画面上什么都看不出来。
  const teardowns: [string, () => void][] = [
    ['镜腿双击退出', () => mock.simulateGesture('doubleTap', 'glassesR')],
    ['前台交互层关闭', () => mock.simulateForegroundExit()],
    ['应用退出', () => mock.simulateExit()],
    ['页面卸载', () => window.dispatchEvent(new Event('beforeunload'))],
  ];
  for (const [name, fire] of teardowns) {
    it(`★ 收尾路径「${name}」会把正在进行的录音收掉`, () => {
      mock.simulateGesture('longPress', 'glassesR');
      expect(pttBtn().classList.contains('active')).toBe(true);
      fire();
      expect(pttBtn().classList.contains('active')).toBe(false);
    });
  }

  // 真链路上抓到的 bug，17 个单测全绿也照样漏 —— 因为它们把 setUi 注入成桩，
  // 永远碰不到真的 LensUi 指针处理器。现象：镜腿长按开麦成功，约一秒后录音
  // 自己没了，日志里有 mic open 紧跟 mic closed，却没有任何 release 事件。
  it('★ 手势起的录音，不能被一个「光标掠过按钮」的 pointerleave 杀掉', () => {
    mock.simulateGesture('longPress', 'glassesR');
    expect(pttBtn().classList.contains('active')).toBe(true);

    // 注意**没有** pointerdown：从来没有手指按下过这个按钮。
    // 光标只是离开了它 —— 而按钮文字刚从"按住说话"变成更长的"松开发送"，
    // 边界会自己移到静止的光标下面，浏览器就合成一个 pointerleave。
    pttBtn().dispatchEvent(new Event('pointerleave', { bubbles: true }));

    expect(pttBtn().classList.contains('active')).toBe(true);
    expect(pttBtn().textContent).toBe(t.pttActive);
  });

  it('但手机按钮真的被按下—抬起时，仍然要能结束一次手势起的录音', () => {
    mock.simulateGesture('longPress', 'glassesR');
    // pointerdown 是唯一有资格置 pttPressed 的入口
    pttBtn().dispatchEvent(new Event('pointerdown', { bubbles: true }));
    pttBtn().dispatchEvent(new Event('pointerup', { bubbles: true }));
    expect(pttBtn().classList.contains('active')).toBe(false);
    expect(pttBtn().textContent).toBe(t.ptt);
  });

  it('jsdom 里没有麦克风 ⇒ 走开麦失败那条路：按钮自己复位', async () => {
    mock.simulateGesture('longPress', 'glassesR');
    expect(pttBtn().classList.contains('active')).toBe(true);
    // 真机上这条对应"麦克风被别的 App 占着"。复位不做的话按钮会永远停在"正在说"。
    await vi.waitFor(() => expect(pttBtn().classList.contains('active')).toBe(false));
  });
});
