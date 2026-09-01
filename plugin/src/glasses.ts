/**
 * 眼镜侧封装：容器创建/重建（HUD 契约见 protocol/hud-contract.json）、
 * 120ms 防抖渲染、镜腿事件归一与分发、麦克风透传、设备遥测。
 *
 * 本轮修掉的四个真机级缺陷（详见 docs/HARDWARE-SPEC.md 与 REPORT.md）：
 *
 * 1. `FOREGROUND_EXIT_EVENT(5)` 曾被当作"应用被销毁"，与 SYSTEM_EXIT/ABNORMAL_EXIT
 *    走同一个 teardown → 用户在眼镜上瞥一眼别的再回来，WS 就永久断了、画面冻在最后一帧。
 *    官方语义是"前台交互层关闭、页面仍挂载"，现在它只是暂停，`FOREGROUND_ENTER(4)` 恢复。
 * 2. `createStartUpPageContainer` **一个页面生命周期只能调一次**，之后必须
 *    `rebuildPageContainer`。以前重复调用在 harness 里跑得通、真机上第二次直接失败。
 * 3. `textContainerUpgrade` 的返回值被丢弃，且 `lastWritten` 在 await **之前**就写了 ——
 *    一次静默失败就会永久毒化去重缓存，该容器此后再也不会被重写。
 * 4. 事件只处理了单击/双击，`eventSource` 从未被读取 ⇒ 分不清左右镜腿与 R1 戒指；
 *    长按在 SDK 0.0.14 才有独立事件码（9/10），旧版会被降级成 CLICK（一次长按=两次误翻页）。
 */
import {
  CreateStartUpPageContainer,
  EventSourceType,
  formatEvenHubPageContainerValidationError,
  OsEventTypeList,
  RebuildPageContainer,
  TextContainerProperty,
  TextContainerUpgrade,
  validateEvenHubPageContainer,
  waitForEvenAppBridge,
  type DeviceInfo,
  type DeviceStatus,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk';
import { BLANK, EVENT_CAPTURE_CONTAINER, HUD_TEXT, LAYOUT } from './hud';
import type { FrameContainers, GlassesTelemetry } from './types';

export { LAYOUT } from './hud';

/** BLE 渲染队列慢：textContainerUpgrade 用 ~120ms 防抖合并写。 */
const RENDER_DEBOUNCE_MS = 120;

/**
 * 所有 bridge 调用的超时（修 B1）。BLE 链路卡住时 bridge 的 Promise 可能永不 settle，
 * 没有超时就意味着 HUD 永久冻结，启动路径甚至会死锁。
 */
const BRIDGE_TIMEOUT_MS = 5000;

/** 手势来源。protobuf 零值省略 ⇒ 缺省按 DUMMY_NULL 处理。 */
export type InputSource = 'glassesL' | 'glassesR' | 'ring' | 'unknown';

/** 归一化后的镜腿/戒指手势。 */
export interface InputGesture {
  kind: 'tap' | 'doubleTap' | 'swipeUp' | 'swipeDown' | 'longPress' | 'longPressRelease';
  source: InputSource;
}

export interface GlassesEvents {
  /** 归一化手势（含来源）。翻页/退出等策略由调用方决定。 */
  onGesture?: (gesture: InputGesture) => void;
  /** 应用被真正销毁（系统退出 / 异常退出）——此时才做 teardown */
  onExit?: () => void;
  /** 前台交互层关闭：页面仍挂载，只应暂停，绝不能断连接 */
  onForegroundExit?: () => void;
  /** 重新回到前台 */
  onForegroundEnter?: () => void;
  /** 眼镜麦克风 PCM（16kHz s16le mono） */
  onAudioPcm?: (pcm: Uint8Array) => void;
  /** 设备状态推送（电量/佩戴等） */
  onDeviceStatus?: (status: DeviceStatus) => void;
  /**
   * 一次遥测采样可以上报了（`onDeviceStatusChanged` 触发，即真实的状态变化）。
   * 与 `onDeviceStatus` 分开是因为这一路要过型号判定：戒指的状态推送不会走到这里。
   */
  onTelemetry?: (telemetry: GlassesTelemetry) => void;
}

/** 是否运行在 Even App（或 harness mock）宿主里。 */
export function hasEvenHost(): boolean {
  const w = window as unknown as {
    flutter_inappwebview?: { callHandler?: unknown };
  };
  return typeof w.flutter_inappwebview?.callHandler === 'function';
}

/**
 * 等待 bridge 就绪。注意：SDK 的 waitForEvenAppBridge 在纯浏览器里也会 resolve
 * （它只等 DOM ready），所以"是否在 Even App 内"必须用 flutter_inappwebview 判定。
 */
export async function connectBridge(timeoutMs = 3000): Promise<EvenAppBridge | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (hasEvenHost()) return waitForEvenAppBridge();
    await new Promise((r) => window.setTimeout(r, 100));
  }
  return null;
}

/** 给任意 bridge 调用套一层超时，避免 BLE 卡死时永远挂着（修 B1）。 */
function withTimeout<T>(what: string, p: Promise<T>, fallback: T, ms = BRIDGE_TIMEOUT_MS): Promise<T> {
  return new Promise<T>((resolve) => {
    let settled = false;
    const timer = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      console.warn(`[glasses] ${what} 超时 ${ms}ms，按失败处理`);
      resolve(fallback);
    }, ms);
    p.then(
      (v) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve(v);
      },
      (err) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        console.warn(`[glasses] ${what} 失败:`, err);
        resolve(fallback);
      },
    );
  });
}

function sourceOf(raw: unknown): InputSource {
  switch (EventSourceType.fromJson(raw) ?? EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL) {
    case EventSourceType.TOUCH_EVENT_FROM_GLASSES_L:
      return 'glassesL';
    case EventSourceType.TOUCH_EVENT_FROM_GLASSES_R:
      return 'glassesR';
    case EventSourceType.TOUCH_EVENT_FROM_RING:
      return 'ring';
    default:
      return 'unknown';
  }
}

/** 把一个 SDK 没能解析的 audioPcm 载荷描述成一句人话，用于排障日志。 */
function describeShape(raw: unknown): string {
  if (typeof raw === 'string') return `字符串(${raw.length} 字符)`;
  if (Array.isArray(raw)) return `数组(${raw.length} 项)`;
  if (raw instanceof Uint8Array) return `Uint8Array(${raw.length} 字节)`;
  if (raw && typeof raw === 'object') {
    return `对象{${Object.keys(raw as object).slice(0, 4).join(',')}}`;
  }
  return typeof raw;
}

export class GlassesController {
  private readonly unsubscribers: Array<() => void> = [];
  /** SDK 解析失败而被丢掉的音频帧数。只用于日志降噪与测试断言。 */
  private pcmDropped = 0;
  private lastWritten: Partial<Record<keyof FrameContainers, string>> = {};
  private pending: FrameContainers | null = null;
  private flushTimer: number | null = null;
  private flushing = false;
  private disposed = false;
  /** createStartUpPageContainer 一个页面生命周期只能调一次，之后走 rebuild。 */
  private pageCreated = false;

  /** 最近一次设备状态推送。遥测的"新鲜"来源。 */
  private lastStatus: DeviceStatus | null = null;
  /** getDeviceInfo() 的结果，型号判定的唯一依据（DeviceStatus 里没有 model）。 */
  private deviceInfo: DeviceInfo | null = null;

  constructor(
    private readonly bridge: EvenAppBridge,
    private readonly events: GlassesEvents = {},
  ) {
    this.unsubscribers.push(this.bridge.onEvenHubEvent((event) => this.onHubEvent(event)));
    this.unsubscribers.push(
      this.bridge.onDeviceStatusChanged((status) => {
        this.lastStatus = status;
        this.events.onDeviceStatus?.(status);
        void this.emitTelemetry();
      }),
    );
  }

  // ---------------------------------------------------------------- 遥测

  /**
   * 组装一次遥测采样。**型号判定在这里，不在网关**。
   *
   * `DeviceStatus`（`onDeviceStatusChanged` 的载荷）只有 `sn`，没有 `model`
   * —— 而 Even 生态里 R1 戒指与眼镜走的是同一套状态推送。分辨的唯一办法是
   * 拿 `getDeviceInfo()`（宿主侧方法名 `getGlassesInfo`）返回的 `model` + `sn`
   * 去比对：sn 对不上就不是这副眼镜的状态，`isGlasses` 置 false，网关会拒收。
   *
   * 缺失的字段一律给 `null`，**绝不补 0** —— 网关那边分不清"电量 0%"和"没读到电量"。
   */
  async telemetry(): Promise<GlassesTelemetry> {
    if (this.deviceInfo === null) this.deviceInfo = await this.getDeviceInfo();
    const info = this.deviceInfo;
    const status = this.lastStatus ?? info?.status ?? null;

    // 型号确认为眼镜，且这条状态确实来自同一台设备
    const modelIsGlasses = info?.isGlasses() === true;
    const snMatches = !status || !info ? true : status.sn === info.sn;
    if (!snMatches) {
      console.debug('[glasses] 状态推送来自别的设备（多半是 R1 戒指），不作为眼镜遥测上报', {
        statusSn: status?.sn,
        glassesSn: info?.sn,
      });
    }

    return {
      model: info?.model ?? null,
      sn: info?.sn ?? status?.sn ?? null,
      isGlasses: modelIsGlasses && snMatches,
      connectType: status?.connectType ?? null,
      connected: status?.isConnected() ?? false,
      batteryLevel: typeof status?.batteryLevel === 'number' ? status.batteryLevel : null,
      isCharging: typeof status?.isCharging === 'boolean' ? status.isCharging : null,
      isWearing: typeof status?.isWearing === 'boolean' ? status.isWearing : null,
      isInCase: typeof status?.isInCase === 'boolean' ? status.isInCase : null,
    };
  }

  private async emitTelemetry(): Promise<void> {
    if (!this.events.onTelemetry) return;
    try {
      this.events.onTelemetry(await this.telemetry());
    } catch (err) {
      console.debug('[glasses] 遥测组装失败', err);
    }
  }

  // ---------------------------------------------------------------- 事件

  /**
   * 交付一帧 PCM。**这里刻意不做载荷归一**，因为 SDK 已经做完了。
   *
   * `audioPcm` 在宿主侧是 Flutter 的 `Uint8List`，经 JSON 之后可能是 `number[]`、
   * 也可能是 base64 字符串——SDK 自己的 `index.d.ts:1050` 就是这么写的，而它声明的
   * 类型却是 `Uint8Array`。实测结论：`evenHubEventFromJson` 把 `number[]` / base64 /
   * `Uint8Array` 三种载荷、`jsonData` / `data` / 数组三种信封**全部**归一成
   * `Uint8Array`（契约钉在 `tests/audio-pcm.test.ts`）。再写一遍归一函数只是重复实现，
   * 而且会掩盖真正的失败模式——
   *
   * 会出事的是 SDK **认不出**的载荷形状（实测：Node 的 `{type:'Buffer',data:[…]}`）：
   * 此时 `event.audioEvent` 整个是 undefined，音频被**静默丢弃**。用户说了一整句话、
   * 一个字节都没上行，而网关那头只会报「麦克风没有声音」——根因完全查不到。
   * 判据在 `jsonData` 里：原始载荷带 audioPcm、解析结果却没有 ⇒ 是解析失败，不是没音频。
   */
  private deliverPcm(event: {
    audioEvent?: { audioPcm?: Uint8Array };
    jsonData?: Record<string, unknown>;
  }): void {
    const pcm = event.audioEvent?.audioPcm;
    if (pcm && pcm.length > 0) {
      this.events.onAudioPcm?.(pcm);
      return;
    }
    if (pcm === undefined && event.jsonData?.audioPcm != null) {
      this.pcmDropped += 1;
      // 每帧都喊会把控制台淹掉（16kHz 下 200ms 一帧），只在头几帧与整十倍处出声
      if (this.pcmDropped <= 3 || this.pcmDropped % 100 === 0) {
        console.warn(
          `[glasses] 宿主发来了音频但 SDK 解不出来，已丢弃第 ${this.pcmDropped} 帧。` +
            `载荷形状：${describeShape(event.jsonData.audioPcm)}`,
        );
      }
    }
  }

  private onHubEvent(event: {
    audioEvent?: { audioPcm?: Uint8Array };
    sysEvent?: { eventType?: unknown; eventSource?: unknown };
    textEvent?: { eventType?: unknown; eventSource?: unknown };
    listEvent?: { eventType?: unknown; eventSource?: unknown };
    /** 宿主发来的**原始载荷**，SDK 原样透传。见下方对"缺字段 vs 无法识别"的判别。 */
    jsonData?: Record<string, unknown>;
  }): void {
    this.deliverPcm(event);

    const body = event.sysEvent ?? event.textEvent ?? event.listEvent;
    if (!body) return;

    // 「字段缺省」与「字段存在但 SDK 认不出来」必须区分开，否则任何未知系统事件
    // 都会退化成一次幽灵翻页（B4-①）。两者在解析后的 sysEvent 里长得一模一样：
    // 实测 SDK 对 eventType=99 交付的是 `sysEvent:{}`，与 `{}` 无从分辨。
    // 但 SDK 会**原样透传 jsonData**，判据就在那里：
    //   - 两处都没有 eventType ⇒ 真的是 protobuf 零值省略 ⇒ CLICK_EVENT
    //   - jsonData 里有、但 fromJson 认不出 ⇒ 未知事件 ⇒ 忽略
    const rawType =
      (body as { eventType?: unknown }).eventType ?? event.jsonData?.eventType;
    const eventType =
      rawType === undefined || rawType === null
        ? OsEventTypeList.CLICK_EVENT
        : OsEventTypeList.fromJson(rawType);
    if (eventType === undefined) {
      console.warn('[glasses] 未识别的 eventType，已忽略:', rawType);
      return;
    }

    const source = sourceOf(
      (body as { eventSource?: unknown }).eventSource ?? event.jsonData?.eventSource,
    );

    switch (eventType) {
      case OsEventTypeList.CLICK_EVENT:
        this.events.onGesture?.({ kind: 'tap', source });
        break;
      case OsEventTypeList.DOUBLE_CLICK_EVENT:
        this.events.onGesture?.({ kind: 'doubleTap', source });
        break;
      case OsEventTypeList.SCROLL_TOP_EVENT:
        this.events.onGesture?.({ kind: 'swipeUp', source });
        break;
      case OsEventTypeList.SCROLL_BOTTOM_EVENT:
        this.events.onGesture?.({ kind: 'swipeDown', source });
        break;
      case OsEventTypeList.LONG_PRESS_EVENT:
        this.events.onGesture?.({ kind: 'longPress', source });
        break;
      case OsEventTypeList.LONG_PRESS_RELEASE_EVENT:
        this.events.onGesture?.({ kind: 'longPressRelease', source });
        break;
      case OsEventTypeList.FOREGROUND_ENTER_EVENT:
        this.events.onForegroundEnter?.();
        break;
      case OsEventTypeList.FOREGROUND_EXIT_EVENT:
        // 前台交互层关闭，页面**仍然挂载** —— 只暂停，不断连接
        this.events.onForegroundExit?.();
        break;
      case OsEventTypeList.SYSTEM_EXIT_EVENT:
      case OsEventTypeList.ABNORMAL_EXIT_EVENT:
        this.events.onExit?.();
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------- 容器

  private buildProperties(): TextContainerProperty[] {
    return LAYOUT.map(
      (def) =>
        new TextContainerProperty({
          xPosition: def.x,
          yPosition: def.y,
          width: def.w,
          height: def.h,
          borderWidth: 0,
          paddingLength: 0,
          containerID: def.id,
          containerName: def.name,
          content: def.name === 'status' ? HUD_TEXT.booting : BLANK,
          // 一个页面必须且仅能有一个容器 isEventCapture=1。挂在 foot：服务器已做完分页，
          // body 不该由固件滚动（固件只对 isEventCapture=1 的容器做溢出滚动）。
          isEventCapture: def.name === EVENT_CAPTURE_CONTAINER ? 1 : 0,
          // 五级亮度（0~4，SDK 0.0.14+）。没有字号也没有对齐控制，这是仅有的视觉分层手段。
          ...(def.textColor === undefined ? {} : { textColor: def.textColor }),
        }),
    );
  }

  /**
   * 建立三个文本 container。首次走 createStartUpPageContainer（**只能调一次**），
   * 之后走 rebuildPageContainer。返回 0 表示成功。
   */
  async createContainers(): Promise<number> {
    const textObject = this.buildProperties();

    // SDK 自带的建页校验（zOrderIndex / textColor / menu 三类）。它**不**覆盖内容长度、
    // isEventCapture 数量、containerName 长度与几何越界——那几项由宿主/固件执行，
    // harness 夹具补上了。这里先跑一遍能在本地拦下的，免得白白发一次 BLE 往返。
    const verdict = validateEvenHubPageContainer({ textObject });
    if (!verdict.valid) {
      console.error('[glasses] 建页参数未通过 SDK 校验:', formatEvenHubPageContainerValidationError(verdict));
      this.lastWritten = {};
      return 1; // StartUpPageCreateResult.invalid
    }

    let result: number;
    if (!this.pageCreated) {
      result = await withTimeout(
        'createStartUpPageContainer',
        this.bridge.createStartUpPageContainer(
          new CreateStartUpPageContainer({ containerTotalNum: LAYOUT.length, textObject }),
        ) as Promise<number>,
        -1,
      );
      if (result === 0) this.pageCreated = true;
    } else {
      const ok = await withTimeout(
        'rebuildPageContainer',
        this.bridge.rebuildPageContainer(
          new RebuildPageContainer({ containerTotalNum: LAYOUT.length, textObject }),
        ),
        false,
      );
      result = ok ? 0 : -1;
    }
    if (result === 0) {
      this.lastWritten = { status: HUD_TEXT.booting, body: BLANK, foot: BLANK };
    } else {
      this.lastWritten = {}; // 建页失败：去重缓存必须清空，否则后续写入会被误判为"无变化"
    }
    return result;
  }

  // ---------------------------------------------------------------- 渲染

  /** 渲染一帧（120ms 防抖合并；只更新内容有变化的 container）。 */
  renderFrame(containers: FrameContainers): void {
    this.pending = { ...containers };
    this.scheduleFlush();
  }

  /**
   * 本地看门狗专用：立即把一条状态写到 status container（绕过防抖），
   * 用于断线时消灭"旧帧撒谎"。
   */
  async pushStatusNow(text: string): Promise<boolean> {
    if (this.pending) this.pending.status = text;
    const ok = await withTimeout(
      'textContainerUpgrade(status/watchdog)',
      this.bridge.textContainerUpgrade(
        new TextContainerUpgrade({ containerID: 1, containerName: 'status', content: text }),
      ),
      false,
    );
    // 只有确认写成功才更新去重缓存（修：以前无条件写，一次失败就永久毒化缓存）
    if (ok) this.lastWritten.status = text;
    else delete this.lastWritten.status;
    return ok;
  }

  /** 麦克风开关。返回值是**插件判断麦克风是否真的开了**的唯一依据（修 B3）。 */
  async audioControl(open: boolean): Promise<boolean> {
    return withTimeout('audioControl', this.bridge.audioControl(open), false);
  }

  /**
   * 读设备信息（型号 + SN + 状态）。以前从未被调用 ⇒ 网关对电量/佩戴一无所知。
   *
   * 官方**没有说明**它是否真的触发一次 BLE 读取，手机端很可能直接返回缓存值。
   * 所以由它得到的遥测在网关侧记作 `source="poll"`，与设备主动上报的 `push` 分开 ——
   * 把 poll 无条件标成"新鲜"就是在编数据。
   */
  async getDeviceInfo(): Promise<DeviceInfo | null> {
    const info = await withTimeout('getDeviceInfo', this.bridge.getDeviceInfo(), null);
    if (info) this.deviceInfo = info;
    return info;
  }

  /** 官方标准退出：弹出前台交互层由用户确认。 */
  async exit(): Promise<void> {
    await withTimeout('shutDownPageContainer', this.bridge.shutDownPageContainer(1), false);
  }

  dispose(): void {
    this.disposed = true;
    if (this.flushTimer !== null) {
      window.clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    for (const u of this.unsubscribers.splice(0)) u();
  }

  private scheduleFlush(): void {
    if (this.disposed || this.flushTimer !== null || this.flushing) return;
    this.flushTimer = window.setTimeout(() => {
      this.flushTimer = null;
      void this.flush();
    }, RENDER_DEBOUNCE_MS);
  }

  private async flush(): Promise<void> {
    if (this.flushing || this.disposed) return;
    this.flushing = true;
    try {
      while (this.pending) {
        const target = this.pending;
        this.pending = null;
        for (const def of LAYOUT) {
          // 协议规定三个 key 恒在；此处仍容错 undefined。
          // 空串经 protobuf 零值省略可能到不了固件，用单个空格做"清空"。
          const raw = target[def.name] ?? '';
          const content = raw === '' ? BLANK : raw;
          if (content === this.lastWritten[def.name]) continue;
          // 串行写，避免 BLE 渲染队列并发
          const ok = await withTimeout(
            `textContainerUpgrade(${def.name})`,
            this.bridge.textContainerUpgrade(
              new TextContainerUpgrade({
                containerID: def.id,
                containerName: def.name,
                content,
              }),
            ),
            false,
          );
          if (ok) {
            // 写确认之后才记缓存。以前记在 await 之前，一次静默失败就会让
            // 这个容器"内容与缓存永远一致"，此后再也不会被重写。
            this.lastWritten[def.name] = content;
          } else {
            delete this.lastWritten[def.name];
            console.warn(`[glasses] ${def.name} 写入未确认，已清除去重缓存以便下次重试`);
          }
        }
      }
    } finally {
      this.flushing = false;
      if (this.pending) this.scheduleFlush();
    }
  }
}
