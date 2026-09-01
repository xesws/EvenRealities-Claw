/**
 * 按住说话（PTT）的状态机 —— 手机按钮与镜腿长按共用同一份。
 *
 * 抽成独立模块的理由不是"整洁"，是**两条入口必须共用一个 `active`**：
 * 手机按钮按下时镜腿长按也可能到，两份各自为政的布尔量会开两次麦、发两条 ptt start，
 * 网关那边看到的是一次说话被截成两段。
 *
 * 这里还负责一件 `main.ts` 里原本没有的事：**松手看门狗**。
 * `longPressRelease` 走 BLE，丢一帧就永远不来；那时网关会靠自己的软上限停掉聆听，
 * 而插件这头麦克风还开着、按钮还显示"正在说"、PCM 还在往外推 ——
 * 电量和流量都在烧，用户看不出任何异常。看门狗就是补这个洞的。
 */
import { t } from './strings';

/** PTT 的两条入口。只用于诊断日志：真机上要能一眼看出卡住的是镜腿还是手机按钮。 */
export type PttSource = 'button' | 'gesture';

export interface PttDeps {
  /** 开/关麦。返回值是**判断麦克风是否真的开了**的唯一依据（见 glasses.ts 的 B3）。 */
  audioControl: (open: boolean) => Promise<boolean>;
  sendStart: () => void;
  sendStop: () => void;
  sendCancel: () => void;
  /** 同步按钮外观（**不得**反过来再触发 start/stop，否则会自激）。 */
  setUi: (active: boolean) => void;
  toast: (msg: string) => void;
}

/**
 * 松手看门狗上限。**刻意高于网关的 `max_utterance_seconds`（默认 25s）**。
 *
 * 正常的"说太久"该由网关那条软上限先收尾 —— 它会把已经收到的 PCM 正常转成一次提问，
 * 用户还能拿到回答。插件这条只在**松手事件根本没到**时兜底，属于异常路径。
 * 两者取相等或更小就会反过来：插件先一步 cancel 掉一次本来能出结果的说话，
 * 用户看到的是"说了半天没反应"，而且永远走不到网关那条软上限。
 */
const WATCHDOG_MS = 30_000;

export class PttController {
  private pttActive = false;
  private source: PttSource = 'button';
  private watchdog: number | null = null;

  constructor(
    private readonly deps: PttDeps,
    private readonly maxMs: number = WATCHDOG_MS,
  ) {}

  /** 是否正在录音。`onAudioPcm` 靠它决定要不要把 PCM 往上推。 */
  get active(): boolean {
    return this.pttActive;
  }

  start(source: PttSource): void {
    // 防双开：镜腿长按与手机按钮可能同时到，第二次必须整个丢掉 ——
    // 不然会开两次麦、发两条 ptt start，网关把一次说话切成两段。
    if (this.pttActive) return;
    this.pttActive = true;
    this.source = source;
    this.arm();
    this.deps.setUi(true);

    // 修 B2/B3：**先开麦、确认成功、再告诉网关开始说话**。
    // 以前是先发 ptt start 再异步开麦，而网关只给 1.4s 等第一块 PCM ——
    // 这 1.4s 要塞下 WS RTT + BLE 下发 + 固件启麦 + 首块回传 + 插件攒包 + 上行，
    // 真机上几乎必然误报"麦克风没有声音"。
    void (async () => {
      const ok = await this.deps.audioControl(true);
      if (!this.pttActive) return;              // 期间已松手/取消
      if (!ok) {
        this.pttActive = false;
        this.disarm();
        this.deps.setUi(false);
        this.deps.toast(t.micFailed);
        return;
      }
      this.deps.sendStart();
    })();
  }

  /** 正常松手：把尾块发完再 stop（`sendPttStop` 内部会先 flush）。 */
  stop(): void {
    if (!this.pttActive) return;
    this.finish();
    this.deps.sendStop();
  }

  /**
   * 放弃这次录音（关麦 + 通知网关）。手势退出 / 前台交互层关闭 / 应用退出 /
   * beforeunload 四条路都走这里 —— 它们都是修过真机 bug 的收尾点。
   * 未在录音时是 no-op：这四条路里有三条随时可能在没说话的时候触发。
   */
  cancel(): void {
    if (!this.pttActive) return;
    this.finish();
    this.deps.sendCancel();
  }

  /** 收尾的公共部分。定时器必须在这里清，否则每次说话都会漏一个 timer 出去。 */
  private finish(): void {
    this.pttActive = false;
    this.disarm();
    this.deps.setUi(false);
    void this.deps.audioControl(false);
  }

  private arm(): void {
    this.disarm();
    this.watchdog = window.setTimeout(() => {
      this.watchdog = null;
      console.warn(`[ptt] ${this.maxMs}ms 未收到结束事件（来源 ${this.source}），自动取消`);
      this.cancel();
      // **不是** micFailed：这条路上麦克风开得好好的，丢的是松手事件。
      // 报「麦克风没能打开」会把人引到错误的方向去查（去看权限、去看蓝牙），
      // 而真正该看的是镜腿那一下有没有到。
      this.deps.toast(t.pttTimeout);
    }, this.maxMs);
  }

  private disarm(): void {
    if (this.watchdog !== null) {
      window.clearTimeout(this.watchdog);
      this.watchdog = null;
    }
  }
}
