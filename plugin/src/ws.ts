/**
 * 网关 WS 客户端（Lens 协议 v1）。
 * - 指数退避 + 抖动重连（1/2/4/8/16/封顶 30s），单飞锁
 * - seq 过滤：丢弃 ≤ 已渲染 seq 的 frame；每次新连接重置（resume 帧恢复现场）
 * - 心跳 ping 20s；两次无 pong → 判定断线 → 本地看门狗回调（向眼镜推"连接丢失"帧）
 * - hello / pair / refresh 全流程；token_expired 自动 refresh 重试一次
 * - 二进制 PCM 上行：合并成 ≤200ms（≤6400 字节）的块再发，减少帧数
 */
import {
  PLUGIN_VERSION,
  type FrameMessage,
  type GlassesTelemetry,
  type ServerMessage,
} from './types';

export type ConnState =
  | 'idle'
  | 'connecting'
  | 'authing'
  | 'online'
  | 'reconnecting'
  | 'unpaired';

export interface LensClientEvents {
  onState?: (state: ConnState, detail?: string) => void;
  onFrame?: (frame: FrameMessage) => void;
  /** 配对成功（调用方负责持久化 deviceId/refreshToken） */
  onPaired?: (info: { deviceId: string; refreshToken: string }) => void;
  onPairFailed?: (message: string) => void;
  /** token 无效/设备被吊销：回配对页 */
  onAuthLost?: () => void;
  /** 看门狗：连接已死，立刻向眼镜推「⛓ 连接丢失·重连中」 */
  onConnectionLost?: () => void;
  /** busy / internal 等服务器错误 */
  onServerError?: (code: string, message?: string) => void;
  /**
   * 网关下发的命令（协议 v1.1）。返回值就是回执载荷；抛错则回执 `ok:false`。
   * 未注册回调 = 该命令不被支持，会如实回 `unsupported`，而不是假装成功。
   */
  onCmd?: (cmd: string) => Promise<unknown>;
}

const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000];
const PING_INTERVAL_MS = 20_000;
const PCM_FLUSH_MS = 200;
/** 200ms @ 16kHz s16le mono = 6400 字节 */
const PCM_MAX_BYTES = 6400;

export class LensClient {
  private ws: WebSocket | null = null;
  private url = '';
  private deviceName = '';
  private accessToken = '';
  private refreshToken = '';
  private pairCode: string | null = null;

  private state: ConnState = 'idle';
  private intentionalClose = false;
  private backoffIndex = 0;
  private reconnectTimer: number | null = null;
  private refreshAttempted = false;
  private lostNotified = false;
  private lastRenderedSeq = Number.NEGATIVE_INFINITY;

  private pingTimer: number | null = null;
  private pendingPings = 0;

  private pcmChunks: Uint8Array[] = [];
  private pcmBytes = 0;
  private pcmTimer: number | null = null;

  constructor(private readonly events: LensClientEvents) {}

  getState(): ConnState {
    return this.state;
  }

  configure(opts: { url: string; refreshToken?: string }): void {
    this.url = opts.url;
    if (opts.refreshToken !== undefined) this.refreshToken = opts.refreshToken;
  }

  /** 配对流程：WS 打开后第一帧发 pair。 */
  startPairing(url: string, code: string, deviceName: string): void {
    this.url = url;
    this.pairCode = code;
    this.deviceName = deviceName;
    this.accessToken = '';
    this.refreshToken = '';
    this.intentionalClose = false;
    this.resetBackoff();
    this.teardownSocket();
    this.openSocket();
  }

  /** 单飞：已有在连/已连的 socket 或排程中的重连时不重复发起。 */
  connect(): void {
    if (
      this.ws &&
      (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)
    ) {
      return;
    }
    if (this.reconnectTimer !== null) return;
    this.intentionalClose = false;
    this.openSocket();
  }

  disconnect(): void {
    this.intentionalClose = true;
    this.clearReconnect();
    this.teardownSocket();
    this.setState('idle');
  }

  // ---------- 上行控制消息 ----------

  sendPttStart(): void {
    this.discardPcm();
    this.sendJson({ type: 'ptt', action: 'start' });
  }

  sendPttStop(): void {
    this.flushPcm(); // 先把尾块发完再 stop，避免截尾
    this.sendJson({ type: 'ptt', action: 'stop' });
  }

  sendPttCancel(): void {
    this.discardPcm();
    this.sendJson({ type: 'ptt', action: 'cancel' });
  }

  sendPage(dir: 'next' | 'prev'): void {
    this.sendJson({ type: 'page', dir });
  }

  sendAbort(): void {
    this.sendJson({ type: 'abort' });
  }

  sendReset(): void {
    this.sendJson({ type: 'reset' });
  }

  /**
   * 主动上报一次遥测（协议 v1.1）。**只在设备真的报告了状态变化时调用** ——
   * 这是唯一能保证"新鲜"的来源，网关会把它记作 `source="push"`。
   */
  sendTelemetry(data: GlassesTelemetry): void {
    this.sendJson({ type: 'telemetry', data });
  }

  /** PCM 上行：合并 ≤200ms 的块。 */
  sendPcm(chunk: Uint8Array): void {
    if (this.state !== 'online' || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.pcmChunks.push(chunk);
    this.pcmBytes += chunk.byteLength;
    if (this.pcmBytes >= PCM_MAX_BYTES) {
      this.flushPcm();
    } else if (this.pcmTimer === null) {
      this.pcmTimer = window.setTimeout(() => {
        this.pcmTimer = null;
        this.flushPcm();
      }, PCM_FLUSH_MS);
    }
  }

  // ---------- 内部 ----------

  private setState(state: ConnState, detail?: string): void {
    this.state = state;
    this.events.onState?.(state, detail);
  }

  private openSocket(): void {
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch (err) {
      console.warn('[ws] 创建 WebSocket 失败:', err);
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.binaryType = 'arraybuffer';
    this.setState(this.backoffIndex > 0 ? 'reconnecting' : 'connecting');

    ws.onopen = () => {
      if (ws !== this.ws) return;
      this.refreshAttempted = false;
      // 新连接：服务器会用 resume 重放当前帧，seq 过滤基线重置
      this.lastRenderedSeq = Number.NEGATIVE_INFINITY;
      if (this.pairCode) {
        this.sendJson({ type: 'pair', code: this.pairCode, deviceName: this.deviceName });
      } else if (this.accessToken) {
        this.sendHello();
      } else if (this.refreshToken) {
        // 重启后没有内存中的 accessToken：先用 refreshToken 换新
        this.sendJson({ type: 'refresh', refreshToken: this.refreshToken });
      } else {
        this.setState('unpaired');
        this.intentionalClose = true;
        ws.close();
        return;
      }
      this.setState('authing');
    };
    ws.onmessage = (ev) => {
      if (ws !== this.ws) return;
      this.handleMessage(ev);
    };
    ws.onclose = () => {
      if (ws !== this.ws) return;
      this.handleClosed();
    };
    ws.onerror = () => {
      // onclose 必随其后，统一在 onclose 里处理
    };
  }

  private sendHello(): void {
    this.sendJson({
      type: 'hello',
      token: this.accessToken,
      client: 'plugin',
      version: PLUGIN_VERSION,
    });
  }

  private handleMessage(ev: MessageEvent): void {
    if (typeof ev.data !== 'string') return; // 下行没有二进制帧
    let msg: ServerMessage;
    try {
      msg = JSON.parse(ev.data) as ServerMessage;
    } catch {
      return;
    }
    switch (msg.type) {
      case 'pair_ok':
        this.pairCode = null;
        this.accessToken = msg.accessToken;
        this.refreshToken = msg.refreshToken;
        this.events.onPaired?.({ deviceId: msg.deviceId, refreshToken: msg.refreshToken });
        // 同连接继续走每次连接的认证，拿 hello_ok + resume
        this.sendHello();
        break;
      case 'hello_ok':
        this.resetBackoff();
        this.lostNotified = false;
        this.refreshAttempted = false;
        this.setState('online');
        this.startHeartbeat();
        // resume 帧直接渲染即恢复现场
        if (msg.resume && msg.resume.type === 'frame') this.handleFrame(msg.resume);
        break;
      case 'refresh_ok':
        this.accessToken = msg.accessToken;
        this.sendHello();
        break;
      case 'pong':
        this.pendingPings = 0;
        break;
      case 'frame':
        this.handleFrame(msg);
        break;
      case 'error':
        this.handleServerError(msg.code, msg.message);
        break;
      case 'cmd':
        void this.handleCmd(msg.cmd, msg.id);
        break;
      default:
        // 未知消息类型静默忽略：协议扩展靠"两端都容忍未知"来保持加法安全
        break;
    }
  }

  /** 执行一条下行命令并回执。失败也必须回执，否则网关会一直挂着这个 id。 */
  private async handleCmd(cmd: string, id: string): Promise<void> {
    if (!this.events.onCmd) {
      this.sendJson({ type: 'cmd_result', id, ok: false, error: 'unsupported' });
      return;
    }
    try {
      const data = await this.events.onCmd(cmd);
      this.sendJson({ type: 'cmd_result', id, ok: true, data });
    } catch (err) {
      this.sendJson({
        type: 'cmd_result',
        id,
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private handleFrame(frame: FrameMessage): void {
    if (typeof frame.seq === 'number' && frame.seq <= this.lastRenderedSeq) return;
    this.lastRenderedSeq = frame.seq;
    this.events.onFrame?.(frame);
  }

  private handleServerError(code: string, message?: string): void {
    switch (code) {
      case 'token_expired':
        if (!this.refreshAttempted && this.refreshToken) {
          this.refreshAttempted = true;
          this.accessToken = '';
          this.sendJson({ type: 'refresh', refreshToken: this.refreshToken });
        } else {
          this.dropAuth();
        }
        break;
      case 'auth_failed':
        this.dropAuth();
        break;
      case 'pair_failed':
        this.pairCode = null;
        this.intentionalClose = true;
        this.teardownSocket();
        this.setState('unpaired');
        this.events.onPairFailed?.(message ?? '配对码无效或已过期');
        break;
      default:
        this.events.onServerError?.(code, message);
        break;
    }
  }

  private dropAuth(): void {
    this.accessToken = '';
    this.refreshToken = '';
    this.pairCode = null;
    this.intentionalClose = true;
    this.clearReconnect();
    this.teardownSocket();
    this.setState('unpaired');
    this.events.onAuthLost?.();
  }

  private handleClosed(): void {
    this.stopHeartbeat();
    this.discardPcm();
    this.ws = null;
    if (this.intentionalClose) return;
    if (this.state === 'online' && !this.lostNotified) {
      this.lostNotified = true;
      this.events.onConnectionLost?.();
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return; // 单飞
    if (!this.refreshToken && !this.accessToken && !this.pairCode) {
      this.setState('unpaired');
      return;
    }
    const base = BACKOFF_MS[Math.min(this.backoffIndex, BACKOFF_MS.length - 1)];
    this.backoffIndex += 1;
    // 抖动：0.5x ~ 1.0x
    const delay = Math.round(base * (0.5 + Math.random() * 0.5));
    this.setState('reconnecting', `${Math.ceil(delay / 1000)}s 后重试`);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.openSocket();
    }, delay);
  }

  private resetBackoff(): void {
    this.backoffIndex = 0;
    this.clearReconnect();
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  // ---------- 心跳 / 看门狗 ----------

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.pendingPings = 0;
    this.pingTimer = window.setInterval(() => {
      if (this.pendingPings >= 2) {
        // 两次 ping 都没等到 pong：判定断线。先让看门狗去眼镜上盖掉旧帧，
        // 再撕掉 socket 走重连——这是消灭"旧帧撒谎"的关键。
        if (!this.lostNotified) {
          this.lostNotified = true;
          this.events.onConnectionLost?.();
        }
        this.teardownSocket();
        this.scheduleReconnect();
        return;
      }
      this.pendingPings += 1;
      this.sendJson({ type: 'ping', t: Date.now() });
    }, PING_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.pingTimer !== null) {
      window.clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    this.pendingPings = 0;
  }

  // ---------- PCM ----------

  private flushPcm(): void {
    if (this.pcmTimer !== null) {
      window.clearTimeout(this.pcmTimer);
      this.pcmTimer = null;
    }
    if (this.pcmChunks.length === 0) return;
    const chunks = this.pcmChunks;
    this.pcmChunks = [];
    const total = this.pcmBytes;
    this.pcmBytes = 0;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const merged = new Uint8Array(total);
    let offset = 0;
    for (const c of chunks) {
      merged.set(c, offset);
      offset += c.byteLength;
    }
    this.ws.send(merged.buffer);
  }

  private discardPcm(): void {
    if (this.pcmTimer !== null) {
      window.clearTimeout(this.pcmTimer);
      this.pcmTimer = null;
    }
    this.pcmChunks = [];
    this.pcmBytes = 0;
  }

  // ---------- 发送 ----------

  private sendJson(payload: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(JSON.stringify(payload));
      return true;
    } catch {
      return false;
    }
  }

  private teardownSocket(): void {
    this.stopHeartbeat();
    this.discardPcm();
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        // ignore
      }
    }
  }
}
