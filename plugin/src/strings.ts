/**
 * 手机端 UI 文案的两种语言。
 *
 * 只管**手机上这块页面**。眼镜 HUD 上的字不在这里 —— 那些由网关排版好整帧下发
 * （状态词见 `device/hud.py` 的 `STATE_LABELS`，回答由 agent 生成），插件一个字
 * 都不该改，否则同一帧在手机预览屏和眼镜上会长得不一样。
 *
 * 选语言的优先级：`?lang=` → 上次存的 → 浏览器语言 → zh。
 * URL 参数排第一是为了截图和演示时能一键切换，不用去动设置。
 */
export type Lang = 'zh' | 'en';

const DICT = {
  zh: {
    connIdle: '未连接',
    connConnecting: '连接中…',
    connAuthing: '认证中…',
    connOnline: '已连接',
    connReconnecting: '重连中…',
    connUnpaired: '未配对',
    connInit: '初始化…',

    pairTitle: '配对网关',
    fieldGateway: '网关地址',
    fieldPairCode: '配对码',
    pairBtn: '配对',
    pairHint: '在网关服务器上执行 lens-gateway pair-code 生成配对码（10 分钟有效，一次性）。',
    pairBadUrl: '网关地址必须以 ws:// 或 wss:// 开头',
    pairBadCode: '请输入 6 位数字配对码',
    pairOk: '配对成功',
    pairExpired: '设备认证失效，请重新配对',

    statusIdle: '待机',
    ptt: '按住说话',
    pttActive: '松开发送 · 上滑取消',
    abort: '打断',
    reset: '清屏',
    settings: '设置',
    save: '保存并重连',
    repair: '重新配对',
    savedReconnecting: '已保存网关地址，正在重连',

    micFailed: '麦克风没能打开，请重试',
    busy: '上一条还在处理，说“打断”或稍等',
    serverError: (code: string) => `服务器错误（${code}）`,

    glassesUnknown: '眼镜：未知',
    glassesChecking: '眼镜：检测中…',
    glassesOffline: '眼镜：未连接',
    glassesOnline: '眼镜：已连接',
    glassesNoHost: '眼镜：不可用（无宿主）',
    battery: (pct: number) => `电量 ${pct}%`,
    charging: '充电中',
    worn: '佩戴中',
    notWorn: '未佩戴',
    noHostNotice: '未在 Even App 内运行：眼镜画面不可用，手机端功能不受影响。',
    hudInitFailed: (code: unknown) => `眼镜画面初始化失败（错误码 ${code}），请重启插件重试。`,
  },
  en: {
    connIdle: 'Offline',
    connConnecting: 'Connecting…',
    connAuthing: 'Authenticating…',
    connOnline: 'Connected',
    connReconnecting: 'Reconnecting…',
    connUnpaired: 'Not paired',
    connInit: 'Starting…',

    pairTitle: 'Pair with gateway',
    fieldGateway: 'Gateway address',
    fieldPairCode: 'Pairing code',
    pairBtn: 'Pair',
    pairHint: 'Run lens-gateway pair-code on the gateway to get a code. Valid for 10 minutes, single use.',
    pairBadUrl: 'Gateway address must start with ws:// or wss://',
    pairBadCode: 'Enter the 6-digit pairing code',
    pairOk: 'Paired',
    pairExpired: 'Device credentials expired. Pair again.',

    statusIdle: 'Idle',
    ptt: 'Hold to talk',
    pttActive: 'Release to send · swipe up to cancel',
    abort: 'Stop',
    reset: 'Clear',
    settings: 'Settings',
    save: 'Save and reconnect',
    repair: 'Pair again',
    savedReconnecting: 'Gateway address saved, reconnecting',

    micFailed: 'Could not open the microphone. Try again.',
    busy: 'Still working on the last one. Say stop, or wait.',
    serverError: (code: string) => `Server error (${code})`,

    glassesUnknown: 'Glasses: unknown',
    glassesChecking: 'Glasses: checking…',
    glassesOffline: 'Glasses: not connected',
    glassesOnline: 'Glasses: connected',
    glassesNoHost: 'Glasses: unavailable (no host)',
    battery: (pct: number) => `battery ${pct}%`,
    charging: 'charging',
    worn: 'worn',
    notWorn: 'not worn',
    noHostNotice: 'Not running inside Even App: the glasses view is unavailable. Phone-side features still work.',
    hudInitFailed: (code: unknown) => `Could not initialise the glasses view (code ${code}). Restart the plugin.`,
  },
} as const;

const KEY = 'lens.lang';

function pick(): Lang {
  try {
    const q = new URLSearchParams(location.search).get('lang');
    if (q === 'en' || q === 'zh') {
      localStorage.setItem(KEY, q);
      return q;
    }
    const saved = localStorage.getItem(KEY);
    if (saved === 'en' || saved === 'zh') return saved;
    return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  } catch {
    // 隐私模式下 localStorage 会抛。语言是纯装饰，不该因此让页面起不来。
    return 'zh';
  }
}

export const lang: Lang = pick();
export const t = DICT[lang];
