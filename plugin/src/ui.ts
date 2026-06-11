/**
 * 手机端页面（移动优先，深色 + 绿色点缀）。
 * 两个屏：配对屏（网关地址 + 6 位配对码）/ 主屏（按住说话、状态行、眼镜画面预览、
 * ●REC、打断/清屏、设置、眼镜连接状态+电量）。
 */
import './style.css';
import type { FrameContainers, FrameMessage } from './types';

export interface UiCallbacks {
  onPairSubmit: (url: string, code: string) => void;
  onPttStart: () => void;
  onPttStop: () => void;
  onPttCancel: () => void;
  onAbort: () => void;
  onReset: () => void;
  onSettingsSave: (url: string) => void;
  onRepair: () => void;
}

/** 从当前页面 origin 自动推导默认网关地址 ws(s)://host/ws。 */
export function defaultGatewayUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  return `${proto}${location.host}/ws`;
}

export class LensUi {
  private root!: HTMLElement;
  private els!: {
    connPill: HTMLElement;
    notice: HTMLElement;
    screenPair: HTMLElement;
    screenMain: HTMLElement;
    pairUrl: HTMLInputElement;
    pairCode: HTMLInputElement;
    pairBtn: HTMLButtonElement;
    pairError: HTMLElement;
    recDot: HTMLElement;
    statusText: HTMLElement;
    previewWrap: HTMLElement;
    preview: HTMLElement;
    pvStatus: HTMLElement;
    pvBody: HTMLElement;
    pvFoot: HTMLElement;
    glassesRow: HTMLElement;
    pttBtn: HTMLButtonElement;
    abortBtn: HTMLButtonElement;
    resetBtn: HTMLButtonElement;
    settingsBtn: HTMLButtonElement;
    settings: HTMLElement;
    settingsUrl: HTMLInputElement;
    settingsSave: HTMLButtonElement;
    repairBtn: HTMLButtonElement;
  };
  private pttPressed = false;
  private toastTimer: number | null = null;

  constructor(private readonly cb: UiCallbacks) {}

  mount(container?: HTMLElement): void {
    const host = container ?? document.getElementById('app') ?? document.body;
    const root = document.createElement('div');
    root.className = 'lens-app';
    root.innerHTML = `
      <header class="topbar">
        <div class="brand">Open<span class="claw">Claw</span> Lens</div>
        <div class="conn-pill" data-el="connPill">未连接</div>
      </header>
      <div class="notice" data-el="notice" hidden></div>

      <section class="screen" data-el="screenPair" hidden>
        <h1>配对网关</h1>
        <label class="field">网关地址
          <input data-el="pairUrl" type="text" autocapitalize="off" autocomplete="off"
                 spellcheck="false" placeholder="wss://example.com/ws">
        </label>
        <label class="field">配对码
          <input data-el="pairCode" class="code" type="text" inputmode="numeric"
                 maxlength="6" placeholder="······">
        </label>
        <button class="primary" data-el="pairBtn">配对</button>
        <p class="error" data-el="pairError" hidden></p>
        <p class="hint">在网关服务器上执行 lens-gateway pair-code 生成配对码（10 分钟有效，一次性）。</p>
      </section>

      <section class="screen" data-el="screenMain" hidden>
        <div class="statusline">
          <span class="rec" data-el="recDot" hidden>●REC</span>
          <span data-el="statusText">待机</span>
        </div>
        <div class="preview-wrap" data-el="previewWrap">
          <div class="preview" data-el="preview">
            <div class="pv pv-status" data-el="pvStatus"></div>
            <div class="pv pv-body" data-el="pvBody"></div>
            <div class="pv pv-foot" data-el="pvFoot"></div>
          </div>
        </div>
        <div class="glasses-row" data-el="glassesRow">眼镜：未知</div>
        <button class="ptt" data-el="pttBtn">按住说话</button>
        <div class="btnrow">
          <button data-el="abortBtn">打断</button>
          <button data-el="resetBtn">清屏</button>
          <button data-el="settingsBtn">设置</button>
        </div>
        <section class="settings" data-el="settings" hidden>
          <label class="field">网关地址
            <input data-el="settingsUrl" type="text" autocapitalize="off"
                   autocomplete="off" spellcheck="false">
          </label>
          <button data-el="settingsSave">保存并重连</button>
          <button class="danger" data-el="repairBtn">重新配对</button>
        </section>
      </section>
    `;
    host.appendChild(root);
    this.root = root;

    const pick = <T extends HTMLElement>(name: string): T => {
      const el = root.querySelector<T>(`[data-el="${name}"]`);
      if (!el) throw new Error(`UI 元素缺失: ${name}`);
      return el;
    };
    this.els = {
      connPill: pick('connPill'),
      notice: pick('notice'),
      screenPair: pick('screenPair'),
      screenMain: pick('screenMain'),
      pairUrl: pick('pairUrl'),
      pairCode: pick('pairCode'),
      pairBtn: pick('pairBtn'),
      pairError: pick('pairError'),
      recDot: pick('recDot'),
      statusText: pick('statusText'),
      previewWrap: pick('previewWrap'),
      preview: pick('preview'),
      pvStatus: pick('pvStatus'),
      pvBody: pick('pvBody'),
      pvFoot: pick('pvFoot'),
      glassesRow: pick('glassesRow'),
      pttBtn: pick('pttBtn'),
      abortBtn: pick('abortBtn'),
      resetBtn: pick('resetBtn'),
      settingsBtn: pick('settingsBtn'),
      settings: pick('settings'),
      settingsUrl: pick('settingsUrl'),
      settingsSave: pick('settingsSave'),
      repairBtn: pick('repairBtn'),
    };

    this.bindEvents();
    this.fitPreview();
    window.addEventListener('resize', () => this.fitPreview());
  }

  private bindEvents(): void {
    const e = this.els;

    e.pairBtn.addEventListener('click', () => {
      const url = e.pairUrl.value.trim();
      const code = e.pairCode.value.trim();
      if (!/^wss?:\/\//.test(url)) {
        this.showPairError('网关地址必须以 ws:// 或 wss:// 开头');
        return;
      }
      if (!/^\d{6}$/.test(code)) {
        this.showPairError('请输入 6 位数字配对码');
        return;
      }
      this.showPairError('');
      this.cb.onPairSubmit(url, code);
    });

    // 按住说话：pointerdown 开始；pointerup 结束；cancel/离开 取消
    const ptt = e.pttBtn;
    ptt.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      if (this.pttPressed) return;
      this.pttPressed = true;
      try {
        ptt.setPointerCapture(ev.pointerId);
      } catch {
        // 某些 WebView 不支持，忽略
      }
      ptt.classList.add('active');
      ptt.textContent = '松开发送 · 上滑取消';
      this.cb.onPttStart();
    });
    ptt.addEventListener('pointerup', () => {
      if (!this.pttPressed) return;
      this.endPtt();
      this.cb.onPttStop();
    });
    const cancel = () => {
      if (!this.pttPressed) return;
      this.endPtt();
      this.cb.onPttCancel();
    };
    ptt.addEventListener('pointercancel', cancel);
    ptt.addEventListener('pointerleave', cancel);

    e.abortBtn.addEventListener('click', () => this.cb.onAbort());
    e.resetBtn.addEventListener('click', () => this.cb.onReset());
    e.settingsBtn.addEventListener('click', () => {
      e.settings.hidden = !e.settings.hidden;
    });
    e.settingsSave.addEventListener('click', () => {
      const url = e.settingsUrl.value.trim();
      if (!/^wss?:\/\//.test(url)) {
        this.toast('网关地址必须以 ws:// 或 wss:// 开头');
        return;
      }
      this.cb.onSettingsSave(url);
    });
    e.repairBtn.addEventListener('click', () => this.cb.onRepair());
  }

  private endPtt(): void {
    this.pttPressed = false;
    this.els.pttBtn.classList.remove('active');
    this.els.pttBtn.textContent = '按住说话';
  }

  private fitPreview(): void {
    const w = this.els.previewWrap.clientWidth;
    if (w <= 0) return;
    const scale = Math.min(1, w / 576);
    this.els.preview.style.transform = `scale(${scale})`;
    this.els.previewWrap.style.height = `${Math.round(288 * scale)}px`;
  }

  // ---------- 屏切换 ----------

  showPairScreen(prefillUrl: string): void {
    this.els.screenMain.hidden = true;
    this.els.screenPair.hidden = false;
    if (!this.els.pairUrl.value) this.els.pairUrl.value = prefillUrl;
    requestAnimationFrame(() => this.fitPreview());
  }

  showMainScreen(gatewayUrl: string): void {
    this.els.screenPair.hidden = true;
    this.els.screenMain.hidden = false;
    this.els.settingsUrl.value = gatewayUrl;
    requestAnimationFrame(() => this.fitPreview());
  }

  // ---------- 状态展示 ----------

  setConn(text: string, kind: 'online' | 'bad' | 'plain' = 'plain'): void {
    this.els.connPill.textContent = text;
    this.els.connPill.classList.toggle('online', kind === 'online');
    this.els.connPill.classList.toggle('bad', kind === 'bad');
  }

  setBridgeNotice(text: string): void {
    this.els.notice.textContent = text;
    this.els.notice.hidden = !text;
  }

  showPairError(message: string): void {
    this.els.pairError.textContent = message;
    this.els.pairError.hidden = !message;
  }

  /** 镜像渲染帧：状态行 + 眼镜画面预览 + ●REC。 */
  setFrame(frame: FrameMessage): void {
    this.setPreview(frame.containers);
    this.els.statusText.textContent = frame.containers.status || '待机';
    this.els.recDot.hidden = !frame.meta?.rec;
  }

  setPreview(containers: Partial<FrameContainers>): void {
    if (containers.status !== undefined) this.els.pvStatus.textContent = containers.status;
    if (containers.body !== undefined) this.els.pvBody.textContent = containers.body;
    if (containers.foot !== undefined) this.els.pvFoot.textContent = containers.foot;
  }

  /** 本地看门狗时同步盖掉手机侧状态。 */
  setStatusLine(text: string): void {
    this.els.statusText.textContent = text;
    this.els.recDot.hidden = true;
  }

  setGlassesStatus(text: string): void {
    this.els.glassesRow.textContent = text;
  }

  toast(message: string): void {
    let el = this.root.querySelector<HTMLElement>('.toast');
    if (!el) {
      el = document.createElement('div');
      el.className = 'toast';
      this.root.appendChild(el);
    }
    const toastEl = el;
    toastEl.textContent = message;
    toastEl.hidden = false;
    if (this.toastTimer !== null) window.clearTimeout(this.toastTimer);
    this.toastTimer = window.setTimeout(() => {
      toastEl.hidden = true;
      this.toastTimer = null;
    }, 3000);
  }
}
