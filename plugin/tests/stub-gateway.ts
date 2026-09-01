/**
 * 存根网关：一个装到 `globalThis.WebSocket` 上的受控传输层。
 *
 * 为什么是这个切法：jsdom 里唯一给不出的东西就是**真的网络**，其余（协议状态机、
 * 退避、seq 过滤、心跳）全在 `LensClient` 自己身上。所以打桩只打到传输为止 ——
 * 被测的是真的 `LensClient`，不是它的替身。
 *
 * 每条出站消息原样记下来，入站由测试推。连接的开与关也都由测试说了算，
 * 这样才能确定性地测退避与看门狗。
 */

export class StubSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  /** 本次测试里被创建过的所有 socket，按时间顺序 —— 重连会造新的。 */
  static instances: StubSocket[] = [];

  readyState = StubSocket.CONNECTING;
  binaryType = 'blob';
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  /** 出站的文本消息，已 JSON.parse */
  readonly sent: Record<string, unknown>[] = [];
  /** 出站的二进制消息（PCM） */
  readonly binary: ArrayBuffer[] = [];
  /**
   * 全部出站消息的**交错顺序**（文本与二进制在同一条序列上）。
   * 单独看 sent / binary 是看不出先后的 —— 而「PCM 必须在 ptt stop 之前发出」
   * 这类断言的全部内容就是先后，所以顺序必须被单独记下来。
   */
  readonly order: Array<{ kind: 'text'; type: string } | { kind: 'binary'; bytes: number }> = [];

  constructor(readonly url: string) {
    StubSocket.instances.push(this);
  }

  send(data: string | ArrayBuffer): void {
    if (this.readyState !== StubSocket.OPEN) throw new Error('send on non-open socket');
    if (typeof data === 'string') {
      const msg = JSON.parse(data);
      this.sent.push(msg);
      this.order.push({ kind: 'text', type: String(msg.type) });
    } else {
      this.binary.push(data);
      this.order.push({ kind: 'binary', bytes: data.byteLength });
    }
  }

  close(): void {
    if (this.readyState === StubSocket.CLOSED) return;
    this.readyState = StubSocket.CLOSED;
    this.onclose?.();
  }

  // ---------- 测试驱动 ----------

  /** 握手完成，触发 onopen */
  accept(): void {
    this.readyState = StubSocket.OPEN;
    this.onopen?.();
  }

  /** 服务器下发一条消息 */
  push(msg: unknown): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }

  /** 服务器/网络把连接掐了（非本端主动关闭） */
  drop(): void {
    this.readyState = StubSocket.CLOSED;
    this.onclose?.();
  }

  /** 按类型取出站消息 */
  ofType(type: string): Record<string, unknown>[] {
    return this.sent.filter((m) => m.type === type);
  }

  static get last(): StubSocket {
    const s = StubSocket.instances[StubSocket.instances.length - 1];
    if (!s) throw new Error('还没有创建过 socket');
    return s;
  }

  static reset(): void {
    StubSocket.instances = [];
  }
}

/** 装上存根传输层，返回卸载函数。 */
export function installStubTransport(): () => void {
  const original = (globalThis as Record<string, unknown>).WebSocket;
  StubSocket.reset();
  (globalThis as Record<string, unknown>).WebSocket = StubSocket;
  return () => {
    (globalThis as Record<string, unknown>).WebSocket = original;
    StubSocket.reset();
  };
}
