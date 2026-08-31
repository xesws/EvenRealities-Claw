/**
 * 模拟器入口：先同步注入宿主 mock，再动态加载插件主模块——
 * 顺序保证 SDK 初始化时 flutter_inappwebview 已存在，与真实 Even App 一致。
 */
import { installEvenHostMock, type GestureKind, type GestureSource } from './mock';

function $(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`harness 元素缺失: ${id}`);
  return el;
}

function $sel(id: string): HTMLSelectElement {
  return $(id) as HTMLSelectElement;
}

const logEl = $('mockLog');
const micStateEl = $('micState');

function log(line: string): void {
  const ts = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const row = document.createElement('div');
  row.innerHTML = `<span class="t">[${ts}]</span> `;
  row.appendChild(document.createTextNode(line));
  logEl.appendChild(row);
  logEl.scrollTop = logEl.scrollHeight;
  while (logEl.childElementCount > 300) logEl.firstElementChild?.remove();
}

// 1) 注入宿主 mock（必须在插件 bundle 之前）
const mock = installEvenHostMock({
  screen: $('glassesScreen'),
  onLog: log,
  onMicState: (open) => {
    micStateEl.textContent = open ? '打开（16kHz s16le → audioEvent）' : '关闭';
  },
});

// 2) 手势：5 种 × 4 个来源。以前只有单击/双击两个按钮，
//    分不清左右镜腿与 R1 戒指，也从来没测过滑动与长按。
$('btnGesture').addEventListener('click', () => {
  mock.simulateGesture($sel('gestureKind').value as GestureKind, $sel('gestureSource').value as GestureSource);
});

// 3) 生命周期：前台进出与真正的退出必须分开测 —— 混为一谈正是"瞥一眼别的就永久断连"的成因
$('btnFgExit').addEventListener('click', () => mock.simulateForegroundExit());
$('btnFgEnter').addEventListener('click', () => mock.simulateForegroundEnter());
$('btnExit').addEventListener('click', () => mock.simulateExit(false));
$('btnAbnormal').addEventListener('click', () => mock.simulateExit(true));
$('btnDrop').addEventListener('click', () => mock.killSockets());
$('btnBattery').addEventListener('click', () => {
  mock.pushDeviceStatus({ batteryLevel: 12, isWearing: true });
});
$('btnUnworn').addEventListener('click', () => {
  mock.pushDeviceStatus({ isWearing: false });
});
// 遥测通路上最容易错的一条：戒指与眼镜走同一套 deviceStatusChanged，
// DeviceStatus 里只有 sn、没有 model。插件必须靠 getDeviceInfo() 的型号 + sn 比对
// 把它挡掉，否则网关会把 41% 的戒指电量当成眼镜电量报给 MCP。
$('btnRing').addEventListener('click', () => mock.pushRingStatus());

// 4) 故障注入
function bindCheck(id: string, apply: (on: boolean) => void): void {
  const el = $(id) as HTMLInputElement;
  el.addEventListener('change', () => {
    apply(el.checked);
    log(`故障开关 ${id} → ${el.checked ? '开' : '关'}`);
  });
}
bindCheck('fxUpgradeFail', (on) => (mock.faults.upgradeOk = on ? false : null));
bindCheck('fxMicDenied', (on) => (mock.faults.micDenied = on));
bindCheck('fxHang', (on) => (mock.faults.bridgeHang = on));
$('fxDelay').addEventListener('change', (ev) => {
  mock.faults.bridgeDelayMs = Number((ev.target as HTMLInputElement).value) || 0;
  log(`模拟 BLE 往返延迟 → ${mock.faults.bridgeDelayMs}ms`);
});
$('btnStats').addEventListener('click', () => {
  log(`统计：${JSON.stringify(mock.stats)}`);
  log(`屏上实际文本：${JSON.stringify(mock.screenText())}`);
});

// 5) 加载插件主模块（与 index.html 完全相同的代码路径）
void import('../src/main')
  .then(() => {
    log('插件 bundle 已加载');
    // 给插件留出订阅 onDeviceStatusChanged 的时间再推初始设备状态
    window.setTimeout(() => mock.pushDeviceStatus(), 1200);
  })
  .catch((err: unknown) => {
    log(`插件加载失败：${err instanceof Error ? err.message : String(err)}`);
  });
