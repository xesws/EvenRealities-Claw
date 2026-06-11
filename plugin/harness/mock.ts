/**
 * 宿主 mock：在加载插件 bundle 之前注入，行为与真实 Even App 桥一致——
 * SDK 通过 window.flutter_inappwebview.callHandler('evenAppMessage', <JSON 字符串>)
 * 调宿主（信封 {type:'call_even_app_method', method, data}，宿主 handler 的返回值
 * 即 Promise 结果）；宿主通过 window._listenEvenAppMessage({type:'listen_even_app_data',
 * method, data}) 推事件（以上行为均已对照 SDK dist/index.js 实测确认）。
 */

interface CallEnvelope {
  type: string;
  method: string;
  data?: Record<string, unknown>;
}

interface TextContainerDef {
  containerID?: number;
  containerName?: string;
  xPosition?: number;
  yPosition?: number;
  width?: number;
  height?: number;
  content?: string;
}

export interface EvenHostMock {
  /** 模拟单击镜腿：故意不带 eventType（protobuf 零值省略，CLICK_EVENT=0 → undefined） */
  simulateTap(): void;
  /** 模拟双击镜腿（eventType=3） */
  simulateDoubleTap(): void;
  /** 断网模拟：关掉页面里所有活动 WebSocket */
  killSockets(): number;
  /** 推送设备状态（电量/佩戴等） */
  pushDeviceStatus(partial?: Record<string, unknown>): void;
  readonly micOpen: boolean;
}

export interface MockOptions {
  /** 576×288 的"眼镜屏"容器（黑底绿字 monospace） */
  screen: HTMLElement;
  onLog?: (line: string) => void;
  onMicState?: (open: boolean) => void;
}

const STORAGE_PREFIX = 'evenmock.';
const TARGET_RATE = 16000;
/** ~100ms @ 16kHz = 1600 样本 */
const CHUNK_SAMPLES = 1600;

/** 流式线性插值重采样器（任意输入采样率 → 16kHz）。 */
class StreamResampler {
  private tail = new Float32Array(0);
  private pos = 0;

  constructor(
    private readonly inRate: number,
    private readonly outRate: number,
  ) {}

  push(input: Float32Array): Float32Array {
    const step = this.inRate / this.outRate;
    const buf = new Float32Array(this.tail.length + input.length);
    buf.set(this.tail, 0);
    buf.set(input, this.tail.length);
    const out: number[] = [];
    let pos = this.pos;
    while (pos + 1 < buf.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      out.push(buf[i] * (1 - frac) + buf[i + 1] * frac);
      pos += step;
    }
    const keep = Math.min(Math.floor(pos), buf.length);
    this.tail = buf.slice(keep);
    this.pos = pos - keep;
    return Float32Array.from(out);
  }
}

export function installEvenHostMock(opts: MockOptions): EvenHostMock {
  const log = (line: string) => opts.onLog?.(line);
  const containers = new Map<number, HTMLElement>();
  const nameToId = new Map<string, number>();

  // ---------- 事件推送（宿主 → SDK） ----------

  function pushToSdk(method: string, data: unknown): void {
    const w = window as unknown as { _listenEvenAppMessage?: (m: unknown) => void };
    if (typeof w._listenEvenAppMessage !== 'function') {
      log(`(SDK 尚未加载，丢弃 ${method} 推送)`);
      return;
    }
    w._listenEvenAppMessage({ type: 'listen_even_app_data', method, data });
  }

  function pushHubEvent(type: string, jsonData: Record<string, unknown>): void {
    pushToSdk('evenHubEvent', { type, jsonData });
  }

  // ---------- 眼镜屏渲染 ----------

  function buildScreen(textObject: TextContainerDef[]): void {
    opts.screen.innerHTML = '';
    containers.clear();
    nameToId.clear();
    for (const def of textObject) {
      const el = document.createElement('div');
      const h = def.height ?? 0;
      el.style.position = 'absolute';
      el.style.left = `${def.xPosition ?? 0}px`;
      el.style.top = `${def.yPosition ?? 0}px`;
      el.style.width = `${def.width ?? 576}px`;
      el.style.height = `${h}px`;
      el.style.overflow = 'hidden';
      el.style.whiteSpace = 'pre-wrap';
      el.style.wordBreak = 'break-all';
      // G2 实际字号由固件决定，这里按布局契约近似：矮条 24px，正文 32px
      el.style.fontSize = h <= 48 ? '24px' : '32px';
      el.style.lineHeight = h <= 48 ? `${h}px` : '44px';
      el.textContent = def.content ?? '';
      const id = def.containerID ?? 0;
      containers.set(id, el);
      if (def.containerName) nameToId.set(def.containerName, id);
      opts.screen.appendChild(el);
    }
  }

  function showExitOverlay(exitMode: number): void {
    const overlay = document.createElement('div');
    overlay.style.cssText =
      'position:absolute;inset:0;background:rgba(0,0,0,.82);display:flex;flex-direction:column;' +
      'align-items:center;justify-content:center;gap:14px;font-size:22px;color:#9fe8bb;z-index:5;';
    overlay.textContent = exitMode === 1 ? '插件请求退出（exitMode=1）' : '插件已退出（exitMode=0）';
    if (exitMode === 1) {
      const btn = document.createElement('button');
      btn.textContent = '确认退出';
      btn.style.cssText =
        'font-size:18px;padding:8px 20px;background:#123523;color:#39ffa0;border:1px solid #2bdc7e;border-radius:8px;cursor:pointer;';
      btn.addEventListener('click', () => {
        overlay.remove();
        opts.screen.innerHTML = '';
        log('用户确认退出 → 推送 SYSTEM_EXIT_EVENT');
        pushHubEvent('sysEvent', { eventType: 7 }); // SYSTEM_EXIT_EVENT
      });
      overlay.appendChild(btn);
    }
    opts.screen.appendChild(overlay);
  }

  // ---------- 麦克风（getUserMedia → 16kHz s16le mono → audioEvent） ----------

  let micOpen = false;
  let micStream: MediaStream | null = null;
  let audioCtx: AudioContext | null = null;
  let processor: ScriptProcessorNode | null = null;
  let pcmCarry: number[] = [];

  async function startMic(): Promise<boolean> {
    if (micOpen) return true;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      log(`麦克风获取失败：${err instanceof Error ? err.message : String(err)}`);
      return false;
    }
    audioCtx = new AudioContext();
    await audioCtx.resume();
    const source = audioCtx.createMediaStreamSource(micStream);
    const resampler = new StreamResampler(audioCtx.sampleRate, TARGET_RATE);
    processor = audioCtx.createScriptProcessor(4096, 1, 1);
    pcmCarry = [];
    processor.onaudioprocess = (ev) => {
      const resampled = resampler.push(ev.inputBuffer.getChannelData(0));
      for (let i = 0; i < resampled.length; i++) {
        const v = Math.max(-1, Math.min(1, resampled[i]));
        pcmCarry.push(Math.round(v < 0 ? v * 0x8000 : v * 0x7fff));
      }
      while (pcmCarry.length >= CHUNK_SAMPLES) {
        const samples = pcmCarry.splice(0, CHUNK_SAMPLES);
        const i16 = Int16Array.from(samples);
        // s16le 字节（typed array 在主流平台均为小端）
        const bytes = new Uint8Array(i16.buffer);
        pushHubEvent('audioEvent', { audioPcm: Array.from(bytes) });
      }
    };
    const sink = audioCtx.createGain();
    sink.gain.value = 0; // 静音回路，仅为驱动 ScriptProcessor
    source.connect(processor);
    processor.connect(sink);
    sink.connect(audioCtx.destination);
    micOpen = true;
    opts.onMicState?.(true);
    log(`麦克风已打开（输入 ${audioCtx.sampleRate}Hz → 16kHz s16le mono，~100ms/块）`);
    return true;
  }

  function stopMic(): boolean {
    if (!micOpen) return true;
    processor?.disconnect();
    processor = null;
    micStream?.getTracks().forEach((t) => t.stop());
    micStream = null;
    void audioCtx?.close();
    audioCtx = null;
    pcmCarry = [];
    micOpen = false;
    opts.onMicState?.(false);
    log('麦克风已关闭');
    return true;
  }

  // ---------- callHandler（SDK → 宿主） ----------

  function handleCall(envelope: CallEnvelope): unknown {
    const data = envelope.data ?? {};
    switch (envelope.method) {
      case 'createStartUpPageContainer': {
        const textObject = (data.textObject as TextContainerDef[] | undefined) ?? [];
        buildScreen(textObject);
        log(`createStartUpPageContainer：${textObject.length} 个文本容器 → 0 (success)`);
        return 0;
      }
      case 'rebuildPageContainer': {
        const textObject = (data.textObject as TextContainerDef[] | undefined) ?? [];
        buildScreen(textObject);
        log(`rebuildPageContainer：${textObject.length} 个文本容器`);
        return true;
      }
      case 'textContainerUpgrade': {
        const id = Number(data.containerID ?? nameToId.get(String(data.containerName ?? '')));
        const el = containers.get(id);
        if (!el) {
          log(`textContainerUpgrade：未知容器 ${String(data.containerID)}/${String(data.containerName)}`);
          return false;
        }
        el.textContent = String(data.content ?? '');
        return true;
      }
      case 'audioControl': {
        const isOpen = Boolean(data.isOpen);
        return isOpen ? startMic() : stopMic();
      }
      case 'imuControl':
        log(`imuControl(${JSON.stringify(data)})：mock 不产生 IMU 数据`);
        return true;
      case 'shutDownPageContainer': {
        const exitMode = Number(data.exitMode ?? 0);
        log(`shutDownPageContainer(exitMode=${exitMode})`);
        stopMic();
        showExitOverlay(exitMode);
        return true;
      }
      case 'setLocalStorage':
        window.localStorage.setItem(STORAGE_PREFIX + String(data.key), String(data.value ?? ''));
        return true;
      case 'getLocalStorage':
        return window.localStorage.getItem(STORAGE_PREFIX + String(data.key)) ?? '';
      case 'getUserInfo':
        return { uid: 1, name: 'Harness 用户', avatar: '', country: 'CN' };
      case 'getGlassesInfo':
        return {
          model: 'g2',
          sn: 'MOCK-G2-0001',
          status: {
            sn: 'MOCK-G2-0001',
            connectType: 'connected',
            isWearing: true,
            batteryLevel: 86,
            isCharging: false,
            isInCase: false,
          },
        };
      default:
        log(`未实现的宿主方法：${envelope.method}`);
        throw new Error(`mock: unsupported method ${envelope.method}`);
    }
  }

  (window as unknown as Record<string, unknown>).flutter_inappwebview = {
    callHandler(name: string, raw: unknown): unknown {
      if (name !== 'evenAppMessage') {
        throw new Error(`mock: unknown handler ${name}`);
      }
      const envelope = (typeof raw === 'string' ? JSON.parse(raw) : raw) as CallEnvelope;
      if (envelope.type !== 'call_even_app_method') {
        throw new Error(`mock: unknown message type ${envelope.type}`);
      }
      return handleCall(envelope);
    },
  };

  // ---------- WebSocket 跟踪（断网模拟） ----------

  const liveSockets = new Set<WebSocket>();
  const NativeWebSocket = window.WebSocket;
  const TrackedWebSocket = class extends NativeWebSocket {
    constructor(url: string | URL, protocols?: string | string[]) {
      super(url, protocols);
      liveSockets.add(this);
      this.addEventListener('close', () => liveSockets.delete(this));
    }
  };
  (window as unknown as Record<string, unknown>).WebSocket = TrackedWebSocket;

  log('宿主 mock 已注入（flutter_inappwebview.callHandler 就绪）');

  return {
    simulateTap() {
      // 关键保真点：CLICK_EVENT=0 是 protobuf 零值，真机上 eventType 缺省，
      // 这里同样不带 eventType，专门测试插件的 undefined→0 归一逻辑。
      log('推送 sysEvent：单击（eventType 缺省 = CLICK_EVENT）');
      pushHubEvent('sysEvent', { eventSource: 1 });
    },
    simulateDoubleTap() {
      log('推送 sysEvent：双击（eventType=3）');
      pushHubEvent('sysEvent', { eventType: 3, eventSource: 1 });
    },
    killSockets() {
      const n = liveSockets.size;
      for (const ws of [...liveSockets]) ws.close();
      log(`断网模拟：关闭了 ${n} 个 WebSocket`);
      return n;
    },
    pushDeviceStatus(partial?: Record<string, unknown>) {
      const status = {
        sn: 'MOCK-G2-0001',
        connectType: 'connected',
        isWearing: true,
        batteryLevel: 86,
        isCharging: false,
        isInCase: false,
        ...partial,
      };
      log(`推送 deviceStatusChanged：电量 ${String(status.batteryLevel)}%`);
      pushToSdk('deviceStatusChanged', status);
    },
    get micOpen() {
      return micOpen;
    },
  };
}
