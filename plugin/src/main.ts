/**
 * 装配层。启动顺序：UI 先行 → bridge 等待（3s 超时提示"未在 Even App 内"但 UI 仍可用）
 * → 容器创建 → WS 连接 → hello_ok 的 resume 帧直接渲染。
 */
import { t } from './strings';
import type { DeviceStatus } from '@evenrealities/even_hub_sdk';
import { connectBridge, GlassesController, type InputGesture } from './glasses';
import { HUD_TEXT } from './hud';
import { ConfigStore } from './store';
import type { FrameContainers } from './types';
import { defaultGatewayUrl, LensUi } from './ui';
import { LensClient, type ConnState } from './ws';

/** 看门狗文案与字形都来自共享 HUD 契约（字形已按 G2 字库校验）。 */
const WATCHDOG_STATUS = HUD_TEXT.linkLost;

const store = new ConfigStore();
let glasses: GlassesController | null = null;
let pttActive = false;
let gatewayUrl = '';
/** 最近一帧，用于从前台交互层返回时重绘（overlay 可能盖过画面）。 */
let lastFrame: FrameContainers | null = null;

function deviceName(): string {
  const ua = navigator.userAgent;
  const platform = /iPhone|iPad/.test(ua) ? 'iOS' : /Android/.test(ua) ? 'Android' : 'Web';
  return `OpenClaw Lens (${platform}) / Even App`;
}

const CONN_TEXT: Record<ConnState, [string, 'online' | 'bad' | 'plain']> = {
  idle: [t.connIdle, 'plain'],
  connecting: [t.connConnecting, 'plain'],
  authing: [t.connAuthing, 'plain'],
  online: [t.connOnline, 'online'],
  reconnecting: [t.connReconnecting, 'bad'],
  unpaired: [t.connUnpaired, 'bad'],
};

const ui = new LensUi({
  onPairSubmit(url, code) {
    gatewayUrl = url;
    client.startPairing(url, code, deviceName());
  },
  onPttStart() {
    // 修 B2/B3：**先开麦、确认成功、再告诉网关开始说话**。
    // 以前是先发 ptt start 再异步开麦，而网关只给 1.4s 等第一块 PCM ——
    // 这 1.4s 要塞下 WS RTT + BLE 下发 + 固件启麦 + 首块回传 + 插件攒包 + 上行，
    // 真机上几乎必然误报"麦克风没有声音"。
    pttActive = true;
    void (async () => {
      const ok = glasses ? await glasses.audioControl(true) : true;
      if (!pttActive) return;                 // 期间已松手/取消
      if (!ok) {
        pttActive = false;
        ui.setPttActive(false);
        ui.toast(t.micFailed);
        return;
      }
      client.sendPttStart();
    })();
  },
  onPttStop() {
    if (!pttActive) return;
    pttActive = false;
    void glasses?.audioControl(false);
    client.sendPttStop();
  },
  onPttCancel() {
    if (!pttActive) return;
    pttActive = false;
    void glasses?.audioControl(false);
    client.sendPttCancel();
  },
  onAbort() {
    client.sendAbort();
  },
  onReset() {
    client.sendReset();
  },
  onSettingsSave(url) {
    gatewayUrl = url;
    void store.save({ gatewayUrl: url });
    client.disconnect();
    client.configure({ url });
    client.connect();
    ui.toast(t.savedReconnecting);
  },
  onRepair() {
    client.disconnect();
    void store.clearAuth();
    ui.showPairScreen(gatewayUrl || defaultGatewayUrl());
    ui.setConn('未配对', 'bad');
  },
});

const client = new LensClient({
  onState(state, detail) {
    const [text, kind] = CONN_TEXT[state];
    ui.setConn(detail ? `${text}（${detail}）` : text, kind);
  },
  onFrame(frame) {
    lastFrame = frame.containers;
    glasses?.renderFrame(frame.containers);
    ui.setFrame(frame);
  },
  onPaired({ deviceId, refreshToken }) {
    void store.save({ gatewayUrl, deviceId, refreshToken });
    ui.showMainScreen(gatewayUrl);
    ui.toast(t.pairOk);
  },
  onPairFailed(message) {
    ui.showPairScreen(gatewayUrl || defaultGatewayUrl());
    ui.showPairError(message);
  },
  onAuthLost() {
    void store.clearAuth();
    ui.showPairScreen(gatewayUrl || defaultGatewayUrl());
    ui.showPairError(t.pairExpired);
  },
  onConnectionLost() {
    // 本地看门狗：眼镜与手机预览同时盖掉旧帧，消灭"旧帧撒谎"
    void glasses?.pushStatusNow(WATCHDOG_STATUS);
    ui.setPreview({ status: WATCHDOG_STATUS });
    ui.setStatusLine(WATCHDOG_STATUS);
  },
  onServerError(code, message) {
    if (code === 'busy') ui.toast(message ?? t.busy);
    else ui.toast(message ?? t.serverError(code));
  },
  async onCmd(cmd) {
    // 协议 v1.1：目前只有 telemetry 一条。不认识的命令**抛错**，
    // 让网关收到 ok:false —— 假装成功会让它把空对象当成真实遥测。
    if (cmd !== 'telemetry') throw new Error(`unsupported cmd: ${cmd}`);
    if (!glasses) throw new Error('no_bridge');
    return glasses.telemetry();
  },
});

/** 停掉正在进行的录音（关麦 + 通知网关），供多处复用。 */
function cancelPtt(): void {
  if (!pttActive) return;
  pttActive = false;
  ui.setPttActive(false);
  client.sendPttCancel();
  void glasses?.audioControl(false);
}

/**
 * 镜腿 / R1 戒指手势 → 动作。
 *
 * 区分来源是有意的：以前 `eventSource` 从未被读取，戒指双击会直接退出插件。
 * 官方的"双击退出"是镜腿手势，戒指上把它映射成向前翻页更合理，也不会误退出。
 */
function handleGesture(g: InputGesture): void {
  switch (g.kind) {
    case 'tap':
      client.sendPage('next');
      break;
    case 'swipeDown':
      client.sendPage('next');
      break;
    case 'swipeUp':
      client.sendPage('prev');
      break;
    case 'doubleTap':
      if (g.source === 'ring') {
        client.sendPage('prev');
      } else {
        cancelPtt();
        void glasses?.exit();   // 官方标准退出
      }
      break;
    case 'longPress':
    case 'longPressRelease':
      // 长按暂未绑定动作。关键是**不能**把它误判成单击 ——
      // SDK 0.0.10 上长按会被降级成 CLICK，一次长按等于两次误翻页。
      console.debug('[main] 未绑定的长按手势', g);
      break;
  }
}

function formatGlassesStatus(status: DeviceStatus): string {
  if (!status.isConnected()) return t.glassesOffline;
  const parts: string[] = [t.glassesOnline];   // 标注：文案表是 as const，否则数组会被推成字面量联合
  if (typeof status.batteryLevel === 'number') parts.push(t.battery(status.batteryLevel));
  if (status.isCharging) parts.push(t.charging);
  parts.push(status.isWearing ? t.worn : t.notWorn);
  return parts.join(' · ');
}

async function bootstrap(): Promise<void> {
  // 1) UI 先行
  ui.mount();
  ui.setConn(t.connInit);
  ui.setGlassesStatus(t.glassesChecking);

  // 2) bridge 等待（3s 超时）
  const bridge = await connectBridge(3000);
  if (!bridge) {
    ui.setBridgeNotice(t.noHostNotice);
    ui.setGlassesStatus(t.glassesNoHost);
  } else {
    store.setBridge(bridge);
    glasses = new GlassesController(bridge, {
      onGesture: handleGesture,
      onForegroundExit() {
        // 前台交互层关闭，**页面仍挂载**：只暂停录音，绝不断开 WS。
        // 以前它和 SYSTEM_EXIT 走同一条 teardown，用户瞥一眼别的再回来连接就永久丢了。
        cancelPtt();
      },
      onForegroundEnter() {
        // 回到前台：overlay 可能盖过画面，重绘最近一帧
        if (lastFrame) glasses?.renderFrame(lastFrame);
      },
      onExit() {
        cancelPtt();
        client.disconnect();
      },
      onAudioPcm(pcm) {
        if (pttActive) client.sendPcm(pcm);
      },
      onDeviceStatus(status) {
        ui.setGlassesStatus(formatGlassesStatus(status));
      },
      onTelemetry(telemetry) {
        // 设备真的报了状态变化 ⇒ 主动上报。这是网关唯一"新鲜"的遥测来源；
        // 网关那边的 poll 只能拿到手机可能缓存的值（见 device/telemetry.py）。
        client.sendTelemetry(telemetry);
      },
    });

    // 3) 容器创建；失败（非 0）时手机页显示错误
    const result = await glasses.createContainers();
    if (result !== 0) {
      ui.setBridgeNotice(t.hudInitFailed(result));
    }
  }

  // 4) 读配置，决定进配对屏还是直接连接
  const cfg = await store.load();
  gatewayUrl = cfg.gatewayUrl || defaultGatewayUrl();
  if (cfg.gatewayUrl && cfg.refreshToken) {
    ui.showMainScreen(gatewayUrl);
    client.configure({ url: cfg.gatewayUrl, refreshToken: cfg.refreshToken });
    client.connect();
  } else {
    ui.showPairScreen(gatewayUrl);
    ui.setConn('未配对', 'bad');
  }
}

window.addEventListener('beforeunload', () => {
  cancelPtt();
});

void bootstrap();
