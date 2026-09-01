/**
 * 手机端页面（移动优先，深色 + 绿色点缀）。
 * 两个屏：配对屏（网关地址 + 6 位配对码）/ 主屏（按住说话、状态行、眼镜画面预览、
 * ●REC、打断/清屏、设置、眼镜连接状态+电量）。
 */
import { t } from './strings';
import { CANVAS, LAYOUT, LINE_HEIGHT } from './hud';
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
        <div class="conn-pill" data-el="connPill">${t.connIdle}</div>
      </header>
      <div class="notice" data-el="notice" hidden></div>

      <section class="screen" data-el="screenPair" hidden>
        <h1>${t.pairTitle}</h1>
        <label class="field">${t.fieldGateway}
          <input data-el="pairUrl" type="text" autocapitalize="off" autocomplete="off"
                 spellcheck="false" placeholder="wss://example.com/ws">
        </label>
        <label class="field">${t.fieldPairCode}
          <input data-el="pairCode" class="code" type="text" inputmode="numeric"
                 maxlength="6" placeholder="······">
        </label>
        <button class="primary" data-el="pairBtn">${t.pairBtn}</button>
        <p class="error" data-el="pairError" hidden></p>
        <p class="hint">${t.pairHint}</p>
      </section>

      <section class="screen" data-el="screenMain" hidden>
        <div class="statusline">
          <span class="rec" data-el="recDot" hidden>●REC</span>
          <span data-el="statusText">${t.statusIdle}</span>
        </div>
        <div class="preview-wrap" data-el="previewWrap">
          <div class="preview" data-el="preview">
            <div class="pv pv-status" data-el="pvStatus"></div>
            <div class="pv pv-body" data-el="pvBody"></div>
            <div class="pv pv-foot" data-el="pvFoot"></div>
          </div>
        </div>
        <div class="glasses-row" data-el="glassesRow">${t.glassesUnknown}</div>
        <button class="ptt" data-el="pttBtn">${t.ptt}</button>
        <div class="btnrow">
          <button data-el="abortBtn">${t.abort}</button>
          <button data-el="resetBtn">${t.reset}</button>
          <button data-el="settingsBtn">${t.settings}</button>
        </div>
        <section class="settings" data-el="settings" hidden>
          <label class="field">${t.fieldGateway}
            <input data-el="settingsUrl" type="text" autocapitalize="off"
                   autocomplete="off" spellcheck="false">
          </label>
          <button data-el="settingsSave">${t.save}</button>
          <button class="danger" data-el="repairBtn">${t.repair}</button>
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

    this.applyHudGeometry();
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
        this.showPairError(t.pairBadUrl);
        return;
      }
      if (!/^\d{6}$/.test(code)) {
        this.showPairError(t.pairBadCode);
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
      ptt.textContent = t.pttActive;
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
        this.toast(t.pairBadUrl);
        return;
      }
      this.cb.onSettingsSave(url);
    });
    e.repairBtn.addEventListener('click', () => this.cb.onRepair());
  }

  /**
   * 同步"按住说话"按钮的外观（**只改 UI，不触发任何回调**）。
   * 供外部取消路径复用：手势退出、前台交互层关闭、开麦失败。
   * 置 false 后手指真正抬起时 `pointerup` 会因 `pttPressed` 已为 false 而空转，
   * 不会重复发一次 ptt stop。
   */
  setPttActive(active: boolean): void {
    this.pttPressed = active;
    const ptt = this.els.pttBtn;
    ptt.classList.toggle('active', active);
    ptt.textContent = active ? t.pttActive : t.ptt;
  }

  private endPtt(): void {
    this.setPttActive(false);
  }

  /**
   * 把手机预览的三个区**按 HUD 契约**摆好，而不是在 CSS 里写死一套。
   * 契约（`protocol/hud-contract.json`）同时被网关的排版引擎和插件的建页逻辑读取，
   * 于是"预览里看到的版式"与"眼镜上真实的版式"不可能漂移。
   * 亮度用 opacity 近似 `textColor` 0~4 —— 那是 G2 上唯一真实存在的视觉分层手段。
   */
  private applyHudGeometry(): void {
    this.els.preview.style.width = `${CANVAS.width}px`;
    this.els.preview.style.height = `${CANVAS.height}px`;
    const slot: Record<string, HTMLElement> = {
      status: this.els.pvStatus,
      body: this.els.pvBody,
      foot: this.els.pvFoot,
    };
    for (const c of LAYOUT) {
      const el = slot[c.name];
      if (!el) continue;
      el.style.top = `${c.y}px`;
      el.style.left = `${c.x}px`;
      el.style.width = `${c.w}px`;
      el.style.height = `${c.h}px`;
      el.style.lineHeight = `${LINE_HEIGHT}px`;
      el.style.opacity = String(0.35 + 0.1625 * (c.textColor ?? 4));
    }
  }

  private fitPreview(): void {
    const w = this.els.previewWrap.clientWidth;
    if (w <= 0) return;
    const scale = Math.min(1, w / CANVAS.width);
    this.els.preview.style.transform = `scale(${scale})`;
    this.els.previewWrap.style.height = `${Math.round(CANVAS.height * scale)}px`;
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
    this.els.statusText.textContent = frame.containers.status || t.statusIdle;
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
