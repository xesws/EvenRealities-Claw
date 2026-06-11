/**
 * 眼镜侧封装：容器创建（协议第 4 节布局契约）、120ms 防抖渲染、
 * 镜腿事件归一（含 protobuf 零值字段 CLICK_EVENT=0 → undefined 的归一）、
 * 麦克风透传、设备状态监听。
 */
import {
  CreateStartUpPageContainer,
  OsEventTypeList,
  TextContainerProperty,
  TextContainerUpgrade,
  waitForEvenAppBridge,
  type DeviceStatus,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk';
import type { FrameContainers } from './types';

/** 容器布局契约（protocol/PROTOCOL.md 第 4 节，坐标尺寸照表）。 */
export const LAYOUT: ReadonlyArray<{
  id: number;
  name: keyof FrameContainers;
  x: number;
  y: number;
  w: number;
  h: number;
}> = [
  { id: 1, name: 'status', x: 0, y: 0, w: 576, h: 32 },
  { id: 2, name: 'body', x: 0, y: 32, w: 576, h: 220 },
  { id: 3, name: 'foot', x: 0, y: 252, w: 576, h: 36 },
];

/** BLE 渲染队列慢：textContainerUpgrade 用 ~120ms 防抖合并写。 */
const RENDER_DEBOUNCE_MS = 120;

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

export interface GlassesEvents {
  /** 单击镜腿（阅读态翻页） */
  onTap?: () => void;
  /** 双击镜腿（已自动调用 shutDownPageContainer(1)，这里做收尾） */
  onDoubleTap?: () => void;
  /** 系统退出 / 异常退出 / 退到后台 */
  onExit?: () => void;
  /** 眼镜麦克风 PCM（16kHz s16le mono） */
  onAudioPcm?: (pcm: Uint8Array) => void;
  /** 设备状态（电量/佩戴等），用于手机页展示 */
  onDeviceStatus?: (status: DeviceStatus) => void;
}

export class GlassesController {
  private readonly unsubscribers: Array<() => void> = [];
  private lastWritten: Partial<Record<keyof FrameContainers, string>> = {};
  private pending: FrameContainers | null = null;
  private flushTimer: number | null = null;
  private flushing = false;
  private disposed = false;

  constructor(
    private readonly bridge: EvenAppBridge,
    private readonly events: GlassesEvents = {},
  ) {
    this.unsubscribers.push(
      this.bridge.onEvenHubEvent((event) => {
        // PCM 经 audioEvent 到达（Uint8Array）
        const pcm = event.audioEvent?.audioPcm;
        if (pcm && pcm.length > 0) this.events.onAudioPcm?.(pcm);

        // protobuf 零值字段不上线：CLICK_EVENT=0 会以 undefined 到达，
        // 因此凡是带 sys/text/list 事件体的，eventType 缺省都要归一成 0。
        let eventType: OsEventTypeList | null = null;
        if (event.sysEvent) eventType = event.sysEvent.eventType ?? OsEventTypeList.CLICK_EVENT;
        else if (event.textEvent) eventType = event.textEvent.eventType ?? OsEventTypeList.CLICK_EVENT;
        else if (event.listEvent) eventType = event.listEvent.eventType ?? OsEventTypeList.CLICK_EVENT;
        if (eventType === null) return;

        switch (eventType) {
          case OsEventTypeList.DOUBLE_CLICK_EVENT:
            // 双击镜腿 = 退出插件，官方标准退出模式
            void this.exit();
            this.events.onDoubleTap?.();
            break;
          case OsEventTypeList.CLICK_EVENT:
            this.events.onTap?.();
            break;
          case OsEventTypeList.SYSTEM_EXIT_EVENT:
          case OsEventTypeList.ABNORMAL_EXIT_EVENT:
          case OsEventTypeList.FOREGROUND_EXIT_EVENT:
            this.events.onExit?.();
            break;
          default:
            break;
        }
      }),
    );
    this.unsubscribers.push(
      this.bridge.onDeviceStatusChanged((status) => {
        this.events.onDeviceStatus?.(status);
      }),
    );
  }

  /**
   * 按布局契约创建 3 个文本 container（status/body/foot，ID 1/2/3）。
   * 返回宿主结果码：0 = 成功；非 0 时调用方应在手机页显示错误。
   */
  async createContainers(): Promise<number> {
    const textObject = LAYOUT.map(
      (def, i) =>
        new TextContainerProperty({
          xPosition: def.x,
          yPosition: def.y,
          width: def.w,
          height: def.h,
          borderWidth: 0,
          paddingLength: 0,
          containerID: def.id,
          containerName: def.name,
          content: def.name === 'status' ? '⛓ 等待连接…' : ' ',
          // 一个页面必须且仅能有一个容器 isEventCapture=1（最后一个容器处理事件）
          isEventCapture: i === LAYOUT.length - 1 ? 1 : 0,
        }),
    );
    try {
      const result = await this.bridge.createStartUpPageContainer(
        new CreateStartUpPageContainer({
          containerTotalNum: LAYOUT.length,
          textObject,
        }),
      );
      if (result === 0) {
        this.lastWritten = { status: '⛓ 等待连接…', body: ' ', foot: ' ' };
      }
      return result;
    } catch (err) {
      console.error('[glasses] createStartUpPageContainer 异常:', err);
      return -1;
    }
  }

  /** 渲染一帧（120ms 防抖合并；只更新内容有变化的 container）。 */
  renderFrame(containers: FrameContainers): void {
    this.pending = { ...containers };
    this.scheduleFlush();
  }

  /**
   * 本地看门狗专用：立即把一条状态写到 status container（绕过防抖），
   * 用于断线时消灭"旧帧撒谎"。
   */
  pushStatusNow(text: string): void {
    if (this.pending) this.pending.status = text;
    this.lastWritten.status = text;
    void this.bridge
      .textContainerUpgrade(
        new TextContainerUpgrade({ containerID: 1, containerName: 'status', content: text }),
      )
      .catch((err) => console.warn('[glasses] 看门狗帧写入失败:', err));
  }

  /** 麦克风开关透传。 */
  async audioControl(open: boolean): Promise<boolean> {
    try {
      return await this.bridge.audioControl(open);
    } catch (err) {
      console.warn('[glasses] audioControl 失败:', err);
      return false;
    }
  }

  /** 官方标准退出：弹出前台交互层由用户确认。 */
  async exit(): Promise<void> {
    try {
      await this.bridge.shutDownPageContainer(1);
    } catch (err) {
      console.warn('[glasses] shutDownPageContainer 失败:', err);
    }
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
          const content = raw === '' ? ' ' : raw;
          if (content === this.lastWritten[def.name]) continue;
          this.lastWritten[def.name] = content;
          try {
            // 串行写，避免 BLE 渲染队列并发
            await this.bridge.textContainerUpgrade(
              new TextContainerUpgrade({
                containerID: def.id,
                containerName: def.name,
                content,
              }),
            );
          } catch (err) {
            console.warn(`[glasses] textContainerUpgrade(${def.name}) 失败:`, err);
          }
        }
      }
    } finally {
      this.flushing = false;
      if (this.pending) this.scheduleFlush();
    }
  }
}
