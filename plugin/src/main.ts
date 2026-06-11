/**
 * 装配层。启动顺序：UI 先行 → bridge 等待（3s 超时提示"未在 Even App 内"但 UI 仍可用）
 * → 容器创建 → WS 连接 → hello_ok 的 resume 帧直接渲染。
 */
import type { DeviceStatus } from '@evenrealities/even_hub_sdk';
import { connectBridge, GlassesController } from './glasses';
import { ConfigStore } from './store';
import { defaultGatewayUrl, LensUi } from './ui';
import { LensClient, type ConnState } from './ws';

const WATCHDOG_STATUS = '⛓ 连接丢失·重连中';

const store = new ConfigStore();
let glasses: GlassesController | null = null;
let pttActive = false;
let gatewayUrl = '';

function deviceName(): string {
  const ua = navigator.userAgent;
  const platform = /iPhone|iPad/.test(ua) ? 'iOS' : /Android/.test(ua) ? 'Android' : 'Web';
  return `OpenClaw Lens (${platform}) / Even App`;
}

const CONN_TEXT: Record<ConnState, [string, 'online' | 'bad' | 'plain']> = {
  idle: ['未连接', 'plain'],
  connecting: ['连接中…', 'plain'],
  authing: ['认证中…', 'plain'],
  online: ['已连接', 'online'],
  reconnecting: ['重连中…', 'bad'],
  unpaired: ['未配对', 'bad'],
};

const ui = new LensUi({
  onPairSubmit(url, code) {
    gatewayUrl = url;
    client.startPairing(url, code, deviceName());
  },
  onPttStart() {
    pttActive = true;
    client.sendPttStart();
    void glasses?.audioControl(true);
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
    ui.toast('已保存网关地址，正在重连');
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
    glasses?.renderFrame(frame.containers);
    ui.setFrame(frame);
  },
  onPaired({ deviceId, refreshToken }) {
    void store.save({ gatewayUrl, deviceId, refreshToken });
    ui.showMainScreen(gatewayUrl);
    ui.toast('配对成功');
  },
  onPairFailed(message) {
    ui.showPairScreen(gatewayUrl || defaultGatewayUrl());
    ui.showPairError(message);
  },
  onAuthLost() {
    void store.clearAuth();
    ui.showPairScreen(gatewayUrl || defaultGatewayUrl());
    ui.showPairError('设备认证失效，请重新配对');
  },
  onConnectionLost() {
    // 本地看门狗：眼镜与手机预览同时盖掉旧帧，消灭"旧帧撒谎"
    glasses?.pushStatusNow(WATCHDOG_STATUS);
    ui.setPreview({ status: WATCHDOG_STATUS });
    ui.setStatusLine(WATCHDOG_STATUS);
  },
  onServerError(code, message) {
    if (code === 'busy') ui.toast(message ?? '上一条还在处理，说"打断"或稍等');
    else ui.toast(message ?? `服务器错误（${code}）`);
  },
});

function formatGlassesStatus(status: DeviceStatus): string {
  if (!status.isConnected()) return '眼镜：未连接';
  const parts = ['眼镜：已连接'];
  if (typeof status.batteryLevel === 'number') parts.push(`电量 ${status.batteryLevel}%`);
  if (status.isCharging) parts.push('充电中');
  parts.push(status.isWearing ? '佩戴中' : '未佩戴');
  return parts.join(' · ');
}

async function bootstrap(): Promise<void> {
  // 1) UI 先行
  ui.mount();
  ui.setConn('初始化…');
  ui.setGlassesStatus('眼镜：检测中…');

  // 2) bridge 等待（3s 超时）
  const bridge = await connectBridge(3000);
  if (!bridge) {
    ui.setBridgeNotice('未在 Even App 内运行：眼镜画面不可用，手机端功能不受影响。');
    ui.setGlassesStatus('眼镜：不可用（无宿主）');
  } else {
    store.setBridge(bridge);
    glasses = new GlassesController(bridge, {
      onTap() {
        // 单击镜腿 = 翻页
        client.sendPage('next');
      },
      onDoubleTap() {
        // 双击镜腿 = 退出插件（shutDownPageContainer 已在 GlassesController 内调用）
        if (pttActive) {
          pttActive = false;
          client.sendPttCancel();
        }
      },
      onExit() {
        if (pttActive) {
          pttActive = false;
          client.sendPttCancel();
        }
        void glasses?.audioControl(false);
        client.disconnect();
      },
      onAudioPcm(pcm) {
        if (pttActive) client.sendPcm(pcm);
      },
      onDeviceStatus(status) {
        ui.setGlassesStatus(formatGlassesStatus(status));
      },
    });

    // 3) 容器创建；失败（非 0）时手机页显示错误
    const result = await glasses.createContainers();
    if (result !== 0) {
      ui.setBridgeNotice(`眼镜画面初始化失败（错误码 ${result}），请重启插件重试。`);
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
  if (pttActive) client.sendPttCancel();
  void glasses?.audioControl(false);
});

void bootstrap();
