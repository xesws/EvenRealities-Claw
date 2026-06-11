/**
 * 模拟器入口：先同步注入宿主 mock，再动态加载插件主模块——
 * 顺序保证 SDK 初始化时 flutter_inappwebview 已存在，与真实 Even App 一致。
 */
import { installEvenHostMock } from './mock';

function $(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`harness 元素缺失: ${id}`);
  return el;
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

// 2) 控制按钮
$('btnTap').addEventListener('click', () => mock.simulateTap());
$('btnDouble').addEventListener('click', () => mock.simulateDoubleTap());
$('btnDrop').addEventListener('click', () => mock.killSockets());

// 3) 加载插件主模块（与 index.html 完全相同的代码路径）
void import('../src/main')
  .then(() => {
    log('插件 bundle 已加载');
    // 给插件留出订阅 onDeviceStatusChanged 的时间再推初始设备状态
    window.setTimeout(() => mock.pushDeviceStatus(), 1200);
  })
  .catch((err: unknown) => {
    log(`插件加载失败：${err instanceof Error ? err.message : String(err)}`);
  });
