/**
 * PCM 载荷契约测试（M7 / B-PCM）。
 *
 * 计划里原本要在插件侧写一个「兼容 Uint8Array / base64 / number[] 三种」的归一函数。
 * 实测推翻了这个前提：**SDK 已经把三种都归一了**。所以这里不测我们自己的归一函数
 * （没有），而是把 SDK 的这个行为**钉成契约** —— 它是我们省掉那段代码的唯一依据，
 * 哪天 SDK 回归了，必须是这个文件先红，而不是真机上用户说话没反应。
 *
 * 载荷的三种形状不是想象出来的：SDK 自己的 `index.d.ts:1050` 写着
 * 「audioPcm: Uint8Array（宿主侧 Uint8List 经 JSON 后多为 number[] 或 base64 字符串）」。
 *
 * 注入顺序与 bridge.test.ts 一致：先装宿主 mock，再动态 import SDK。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installEvenHostMock } from '../harness/mock';

type Glasses = import('../src/glasses').GlassesController;

/** [1, 2, 250] 的三种等价写法 —— base64 'AQL6' 就是这三个字节 */
const BYTES = [1, 2, 250];
const B64 = 'AQL6';

interface Harness {
  glasses: Glasses;
  /** 每次 onAudioPcm 收到的东西，原样存下（不转类型，断言要看真实构造器） */
  received: unknown[];
  push(data: unknown): void;
  dispose(): void;
}

async function boot(): Promise<Harness> {
  document.body.innerHTML = '<div id="screen"></div>';
  const screen = document.getElementById('screen') as HTMLElement;
  installEvenHostMock({ screen });

  const sdk = await import('@evenrealities/even_hub_sdk');
  const { GlassesController } = await import('../src/glasses');
  const bridge = await sdk.waitForEvenAppBridge();

  const received: unknown[] = [];
  const glasses = new GlassesController(bridge, { onAudioPcm: (pcm) => received.push(pcm) });
  return {
    glasses,
    received,
    // 走**完整**入站路径：宿主注入的全局钩子 → SDK 解析 → GlassesController
    push: (data) =>
      (window as unknown as { _listenEvenAppMessage(m: unknown): void })._listenEvenAppMessage({
        type: 'listen_even_app_data',
        method: 'evenHubEvent',
        data,
      }),
    dispose: () => glasses.dispose(),
  };
}

let h: Harness;
beforeEach(async () => {
  h = await boot();
});
afterEach(() => {
  h.dispose();
  vi.restoreAllMocks();
});

/** 交付物必须是真的 Uint8Array —— ws.ts 的 `chunk.byteLength` 依赖这一点。 */
function expectDeliveredBytes(received: unknown[]): void {
  expect(received).toHaveLength(1);
  const pcm = received[0];
  expect(pcm).toBeInstanceOf(Uint8Array);
  expect(Array.from(pcm as Uint8Array)).toEqual(BYTES);
  // NaN 防线：ws.ts 用 byteLength 累加待发字节，number[] 会让它变成 undefined → NaN
  expect((pcm as Uint8Array).byteLength).toBe(3);
}

describe('SDK 把三种 PCM 载荷都归一成 Uint8Array', () => {
  it('number[]（Flutter Uint8List 经 JSON 的常见形态）', () => {
    h.push({ type: 'audioEvent', jsonData: { audioPcm: BYTES } });
    expectDeliveredBytes(h.received);
  });

  it('base64 字符串（protobuf-JSON 对 bytes 字段的标准编码）', () => {
    h.push({ type: 'audioEvent', jsonData: { audioPcm: B64 } });
    expectDeliveredBytes(h.received);
  });

  it('Uint8Array（宿主直接给二进制）', () => {
    h.push({ type: 'audioEvent', jsonData: { audioPcm: new Uint8Array(BYTES) } });
    expectDeliveredBytes(h.received);
  });
});

describe('SDK 把三种信封都认得出来', () => {
  // SDK 的 index.d.ts 明说不同宿主 App 的信封写法不一样，这三种都要活
  it('{ type, jsonData }', () => {
    h.push({ type: 'audioEvent', jsonData: { audioPcm: BYTES } });
    expectDeliveredBytes(h.received);
  });

  it('{ type: snake_case, data }', () => {
    h.push({ type: 'audio_event', data: { audioPcm: BYTES } });
    expectDeliveredBytes(h.received);
  });

  it('[ type, payload ] 数组形', () => {
    h.push(['audio_event', { audioPcm: BYTES }]);
    expectDeliveredBytes(h.received);
  });
});

describe('解不出来的载荷：丢弃，但要留下痕迹', () => {
  it('SDK 认不出的形状不会崩，也不会把坏数据交给 ws', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    // Node 的 Buffer JSON 形态。实测 SDK 对它交付 audioEvent === undefined。
    h.push({ type: 'audioEvent', jsonData: { audioPcm: { type: 'Buffer', data: BYTES } } });

    expect(h.received).toHaveLength(0); // 不能把 {type:'Buffer'} 塞给 sendPcm
    // 静默丢弃是最坏的结局：用户说了话、一个字节没上行，网关只会报「麦克风没有声音」。
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain('解不出来');
    expect(String(warn.mock.calls[0][0])).toContain('对象{type,data}'); // 形状要写进日志才可排障
  });

  it('刷屏防护：连续丢帧只在前三帧出声', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    for (let i = 0; i < 20; i++) {
      h.push({ type: 'audioEvent', jsonData: { audioPcm: { type: 'Buffer', data: BYTES } } });
    }
    expect(h.received).toHaveLength(0);
    // 16kHz 下 200ms 一帧，每帧都喊会把控制台淹掉
    expect(warn).toHaveBeenCalledTimes(3);
  });

  it('真的没有音频字段时保持安静（不是每个 hub 事件都是音频）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    h.push({ type: 'sysEvent', jsonData: { eventType: 1 } });
    expect(h.received).toHaveLength(0);
    expect(warn).not.toHaveBeenCalled();
  });

  it('空音频帧不算解析失败', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    h.push({ type: 'audioEvent', jsonData: { audioPcm: [] } });
    expect(h.received).toHaveLength(0); // 空帧不必上行
    expect(warn).not.toHaveBeenCalled(); // 但也不是错误
  });
});
