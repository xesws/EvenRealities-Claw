/**
 * 插件 WS 协议层冒烟（M7 / T-WS）。
 *
 * REPORT §13 里「插件 WS 协议冒烟」和「心跳看门狗专项」两条一直挂着「仍未实现」，
 * 已有的 44 个用例覆盖的是 bridge 层。这个文件补的就是那两条：
 * 配对 → resume → 翻页 → PTT 上行 → 看门狗 → 退避重连 → 自动 refresh → 旧 seq 丢弃。
 *
 * 打桩只打到传输层（`tests/stub-gateway.ts`），被测的是真的 `LensClient`。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LensClient, type ConnState } from '../src/ws';
import type { FrameMessage } from '../src/types';
import { StubSocket, installStubTransport } from './stub-gateway';

const URL = 'wss://gw.example/ws';

interface Harness {
  client: LensClient;
  states: ConnState[];
  frames: FrameMessage[];
  events: string[];
  paired: { deviceId: string; refreshToken: string }[];
}

let h: Harness;
let uninstall: () => void;

function frame(seq: number, body = '你好'): FrameMessage {
  return {
    type: 'frame',
    seq,
    state: 'S0',
    containers: { status: '· 待机', body, foot: '1/1' },
  } as FrameMessage;
}

/** 走完一次完整认证：accept → hello_ok。返回当前 socket。 */
function goOnline(seq = 1): StubSocket {
  const ws = StubSocket.last;
  ws.accept();
  ws.push({ type: 'hello_ok', resume: frame(seq) });
  return ws;
}

beforeEach(() => {
  uninstall = installStubTransport();
  vi.useFakeTimers();
  // 退避带 0.5~1.0x 抖动，钉死随机源才能断言具体毫秒数
  vi.spyOn(Math, 'random').mockReturnValue(0);
  const states: ConnState[] = [];
  const frames: FrameMessage[] = [];
  const events: string[] = [];
  const paired: { deviceId: string; refreshToken: string }[] = [];
  const client = new LensClient({
    onState: (s) => states.push(s),
    onFrame: (f) => frames.push(f),
    onPaired: (p) => paired.push(p),
    onPairFailed: (m) => events.push(`pairFailed:${m}`),
    onAuthLost: () => events.push('authLost'),
    onConnectionLost: () => events.push('lost'),
    onServerError: (c) => events.push(`err:${c}`),
  });
  h = { client, states, frames, events, paired };
});

afterEach(() => {
  h.client.disconnect();
  vi.useRealTimers();
  vi.restoreAllMocks();
  uninstall();
});

describe('配对流程', () => {
  it('pair → pair_ok → hello → hello_ok，一条连接走完', () => {
    h.client.startPairing(URL, '123456', '我的手机');
    const ws = StubSocket.last;
    expect(ws.url).toBe(URL);

    ws.accept();
    expect(ws.ofType('pair')[0]).toMatchObject({ code: '123456', deviceName: '我的手机' });

    ws.push({ type: 'pair_ok', deviceId: 'dev_1', accessToken: 'AT', refreshToken: 'RT' });
    expect(h.paired).toEqual([{ deviceId: 'dev_1', refreshToken: 'RT' }]);
    // 配对成功后必须在**同一条连接**上继续 hello，否则要多一次往返
    expect(ws.ofType('hello')[0]).toMatchObject({ token: 'AT', client: 'plugin' });

    ws.push({ type: 'hello_ok', resume: frame(7) });
    expect(h.client.getState()).toBe('online');
  });

  it('配对码无效：回未配对态，且**不重连**（重连只会再撞一次同样的错）', () => {
    h.client.startPairing(URL, '000000', '手机');
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'error', code: 'pair_failed', message: '配对码已过期' });

    expect(h.client.getState()).toBe('unpaired');
    expect(h.events).toContain('pairFailed:配对码已过期');
    const before = StubSocket.instances.length;
    vi.advanceTimersByTime(60_000);
    expect(StubSocket.instances.length).toBe(before);
  });
});

describe('resume 与 seq 过滤', () => {
  it('hello_ok 带的 resume 帧直接渲染 = 现场恢复', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'refresh_ok', accessToken: 'AT2' });
    StubSocket.last.push({ type: 'hello_ok', resume: frame(42, '恢复的正文') });

    expect(h.frames).toHaveLength(1);
    expect(h.frames[0].seq).toBe(42);
    expect(h.frames[0].containers.body).toBe('恢复的正文');
  });

  it('≤ 已渲染 seq 的帧被丢弃（乱序到达不能让屏幕倒退）', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok', resume: frame(10) });

    ws.push(frame(9, '迟到的旧帧'));
    ws.push(frame(10, '重复帧'));
    ws.push(frame(11, '新帧'));

    expect(h.frames.map((f) => f.seq)).toEqual([10, 11]);
  });

  it('新连接重置 seq 基线 —— 否则服务器重启后从小 seq 开始，屏幕永远不再更新', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'refresh_ok', accessToken: 'AT' });
    StubSocket.last.push({ type: 'hello_ok', resume: frame(999) });
    expect(h.frames.map((f) => f.seq)).toEqual([999]);

    StubSocket.last.drop();
    vi.advanceTimersByTime(1000);        // 退避 1s（random=0 ⇒ 0.5x）
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'hello_ok', resume: frame(1, '重启后的第一帧') });

    expect(h.frames.map((f) => f.seq)).toEqual([999, 1]);
  });
});

describe('上行控制消息', () => {
  beforeEach(() => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'refresh_ok', accessToken: 'AT' });
    StubSocket.last.push({ type: 'hello_ok' });
  });

  it('翻页 / 中止 / 复位 各发一条', () => {
    h.client.sendPage('next');
    h.client.sendPage('prev');
    h.client.sendAbort();
    h.client.sendReset();
    const ws = StubSocket.last;
    expect(ws.ofType('page')).toEqual([
      { type: 'page', dir: 'next' },
      { type: 'page', dir: 'prev' },
    ]);
    expect(ws.ofType('abort')).toHaveLength(1);
    expect(ws.ofType('reset')).toHaveLength(1);
  });

  it('PTT：start 前丢弃残留 PCM，stop 前先把尾块发完（否则截尾）', () => {
    const ws = StubSocket.last;
    const mark = ws.order.length;   // 跳过 beforeEach 里的认证握手
    h.client.sendPttStart();
    h.client.sendPcm(new Uint8Array(1000));
    h.client.sendPttStop();

    // ★ 断言必须落在**交错顺序**上。只查 binary.length===1 是不够的：
    // 就算 flushPcm 排在 sendJson(stop) 后面，断言时 binary 里照样有一块，
    // 「尾块被截掉」这个真实缺陷根本不会被发现。
    expect(ws.order.slice(mark)).toEqual([
      { kind: 'text', type: 'ptt' },      // start
      { kind: 'binary', bytes: 1000 },    // 尾块 —— 必须在 stop 之前
      { kind: 'text', type: 'ptt' },      // stop
    ]);
    expect(ws.ofType('ptt').map((m) => m.action)).toEqual(['start', 'stop']);
  });

  it('PCM 攒够 200ms（6400 字节）立刻发，不等定时器', () => {
    const ws = StubSocket.last;
    h.client.sendPttStart();
    h.client.sendPcm(new Uint8Array(4000));
    expect(ws.binary).toHaveLength(0);          // 还没攒够
    h.client.sendPcm(new Uint8Array(3000));
    expect(ws.binary).toHaveLength(1);
    expect(ws.binary[0].byteLength).toBe(7000); // 合并成一块
  });

  it('PCM 不足一块时由 200ms 定时器兜底', () => {
    const ws = StubSocket.last;
    h.client.sendPttStart();
    h.client.sendPcm(new Uint8Array(100));
    expect(ws.binary).toHaveLength(0);
    vi.advanceTimersByTime(200);
    expect(ws.binary).toHaveLength(1);
  });

  it('未 online 时的 PCM 直接丢弃，不排队', () => {
    h.client.disconnect();
    h.client.sendPcm(new Uint8Array(8000));
    expect(StubSocket.last.binary).toHaveLength(0);
  });

  it('cancel 丢弃已攒的 PCM（用户放弃这次说话，尾巴不能漏上去）', () => {
    const ws = StubSocket.last;
    h.client.sendPttStart();
    h.client.sendPcm(new Uint8Array(100));
    h.client.sendPttCancel();
    vi.advanceTimersByTime(500);
    expect(ws.binary).toHaveLength(0);
  });
});

describe('心跳看门狗', () => {
  function online(): StubSocket {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });
    return ws;
  }

  it('每 20s 发一次 ping', () => {
    const ws = online();
    vi.advanceTimersByTime(20_000);
    expect(ws.ofType('ping')).toHaveLength(1);
    ws.push({ type: 'pong' });
    vi.advanceTimersByTime(20_000);
    expect(ws.ofType('ping')).toHaveLength(2);
  });

  it('两次 ping 无 pong ⇒ 判定断线：**先**通知看门狗，**再**撕 socket', () => {
    // ★ 顺序只能在回调**触发的那一刻**观测。事后查 events[0]==='lost' 且
    // readyState===CLOSED 是恒真的（两件事都发生了，先后无从分辨），
    // 那样写的话「先撕 socket 再通知」这个真实缺陷不会被发现。
    let stateWhenNotified: number | null = null;
    const client = new LensClient({
      onConnectionLost: () => {
        stateWhenNotified = StubSocket.last.readyState;
      },
    });
    client.configure({ url: URL, refreshToken: 'RT' });
    client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });

    vi.advanceTimersByTime(20_000);   // ping 1，pendingPings=1
    vi.advanceTimersByTime(20_000);   // ping 2，pendingPings=2
    expect(stateWhenNotified).toBeNull();

    vi.advanceTimersByTime(20_000);   // 第三次心跳发现 pendingPings>=2
    // 眼镜上必须先盖掉旧帧再断连，否则一块过期画面会一直撒谎
    expect(stateWhenNotified).toBe(StubSocket.OPEN);
    expect(ws.readyState).toBe(StubSocket.CLOSED);
    client.disconnect();
  });

  it('pong 按时到达就不会误判', () => {
    const ws = online();
    for (let i = 0; i < 5; i++) {
      vi.advanceTimersByTime(20_000);
      ws.push({ type: 'pong' });
    }
    expect(h.events).not.toContain('lost');
    expect(ws.readyState).toBe(StubSocket.OPEN);
  });

  it('断线只通知一次，重连成功后重新武装', () => {
    const ws = online();
    ws.drop();
    expect(h.events.filter((e) => e === 'lost')).toHaveLength(1);

    vi.advanceTimersByTime(1000);
    const ws2 = StubSocket.last;
    ws2.accept();
    ws2.push({ type: 'hello_ok' });
    ws2.drop();
    expect(h.events.filter((e) => e === 'lost')).toHaveLength(2);
  });
});

describe('退避重连', () => {
  function online(): StubSocket {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });
    return ws;
  }

  it('1/2/4/8/16/30s 递增并封顶（random=0 ⇒ 抖动取 0.5x）', () => {
    online().drop();
    const expected = [1000, 2000, 4000, 8000, 16000, 30000, 30000].map((b) => b * 0.5);
    for (const delay of expected) {
      const before = StubSocket.instances.length;
      vi.advanceTimersByTime(delay - 1);
      expect(StubSocket.instances.length).toBe(before);   // 提前 1ms 还没重连
      vi.advanceTimersByTime(1);
      expect(StubSocket.instances.length).toBe(before + 1);
      StubSocket.last.drop();                             // 又失败，继续退避
    }
  });

  it('抖动落在 0.5x~1.0x（random=1 时取满）', () => {
    vi.spyOn(Math, 'random').mockReturnValue(1);
    online().drop();
    const before = StubSocket.instances.length;
    vi.advanceTimersByTime(999);
    expect(StubSocket.instances.length).toBe(before);
    vi.advanceTimersByTime(1);
    expect(StubSocket.instances.length).toBe(before + 1);
  });

  it('单飞：排程中重复 connect() 不会开出第二条连接', () => {
    online().drop();
    const before = StubSocket.instances.length;
    h.client.connect();
    h.client.connect();
    expect(StubSocket.instances.length).toBe(before);
  });

  it('重连成功后退避归零', () => {
    online().drop();
    vi.advanceTimersByTime(500);
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'hello_ok' });

    StubSocket.last.drop();
    const before = StubSocket.instances.length;
    vi.advanceTimersByTime(500);          // 又是第一档 1000*0.5
    expect(StubSocket.instances.length).toBe(before + 1);
  });

  it('主动 disconnect 不触发重连', () => {
    online();
    h.client.disconnect();
    const before = StubSocket.instances.length;
    vi.advanceTimersByTime(60_000);
    expect(StubSocket.instances.length).toBe(before);
    expect(h.client.getState()).toBe('idle');
  });
});

describe('令牌生命周期', () => {
  it('token_expired ⇒ 自动 refresh 并重发 hello，**只重试一次**', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });

    ws.push({ type: 'error', code: 'token_expired' });
    expect(ws.ofType('refresh')).toHaveLength(2);   // 首次认证 1 次 + 这次重试

    // 再来一次：不能无限刷，否则会对着已吊销的设备打无限循环
    ws.push({ type: 'error', code: 'token_expired' });
    expect(ws.ofType('refresh')).toHaveLength(2);
    expect(h.events).toContain('authLost');
    expect(h.client.getState()).toBe('unpaired');
  });

  it('auth_failed ⇒ 立刻回配对页，不重试', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'error', code: 'auth_failed' });
    expect(h.events).toContain('authLost');
    const before = StubSocket.instances.length;
    vi.advanceTimersByTime(60_000);
    expect(StubSocket.instances.length).toBe(before);
  });

  it('没有任何凭据时不做无谓重连', () => {
    h.client.configure({ url: URL });
    h.client.connect();
    StubSocket.last.accept();
    expect(h.client.getState()).toBe('unpaired');
  });

  it('其他服务器错误原样上报，不动连接', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    StubSocket.last.accept();
    StubSocket.last.push({ type: 'refresh_ok', accessToken: 'AT' });
    StubSocket.last.push({ type: 'hello_ok' });
    StubSocket.last.push({ type: 'error', code: 'busy', message: '忙' });
    expect(h.events).toContain('err:busy');
    expect(h.client.getState()).toBe('online');
  });
});

describe('下行命令回执（协议 v1.1）', () => {
  it('未注册 onCmd ⇒ 如实回 unsupported，而不是假装成功', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });
    ws.push({ type: 'cmd', cmd: 'telemetry', id: 'c1' });

    expect(ws.ofType('cmd_result')[0]).toEqual({
      type: 'cmd_result', id: 'c1', ok: false, error: 'unsupported',
    });
  });

  it('onCmd 成功 / 抛错都必须回执（否则网关永远挂着这个 id）', async () => {
    const client = new LensClient({
      onCmd: async (cmd) => {
        if (cmd === 'boom') throw new Error('设备没响应');
        return { batteryLevel: 41 };
      },
    });
    client.configure({ url: URL, refreshToken: 'RT' });
    client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });

    ws.push({ type: 'cmd', cmd: 'telemetry', id: 'c1' });
    ws.push({ type: 'cmd', cmd: 'boom', id: 'c2' });
    await vi.runAllTimersAsync();

    const results = ws.ofType('cmd_result');
    expect(results).toContainEqual({
      type: 'cmd_result', id: 'c1', ok: true, data: { batteryLevel: 41 },
    });
    expect(results).toContainEqual({
      type: 'cmd_result', id: 'c2', ok: false, error: '设备没响应',
    });
    client.disconnect();
  });
});

describe('协议扩展的加法安全', () => {
  it('未知消息类型静默忽略（两端都容忍未知，才能单侧升级）', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });

    expect(() => {
      ws.push({ type: 'future_feature', payload: { anything: true } });
      ws.push({ type: 'frame' });               // 缺 seq
    }).not.toThrow();
    expect(h.client.getState()).toBe('online');
  });

  it('非法 JSON 不会打死连接', () => {
    h.client.configure({ url: URL, refreshToken: 'RT' });
    h.client.connect();
    const ws = StubSocket.last;
    ws.accept();
    ws.push({ type: 'refresh_ok', accessToken: 'AT' });
    ws.push({ type: 'hello_ok' });
    expect(() => ws.onmessage?.({ data: '{ 这不是 json' })).not.toThrow();
    expect(h.client.getState()).toBe('online');
  });
});
