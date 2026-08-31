/**
 * Lens 协议 v1 的消息类型（见 protocol/PROTOCOL.md）。
 * 单一 WebSocket：JSON 文本帧 = 控制/渲染；二进制帧 = PCM（16kHz s16le mono，仅 PTT 期间上行）。
 */

export const PLUGIN_VERSION = '0.1.0';

/** 下行渲染帧的三个文本区。三个 key 恒在，空串 = 清空该区。 */
export interface FrameContainers {
  status: string;
  body: string;
  foot: string;
}

export interface FrameMeta {
  /** true = 聆听中，手机页显示 ●REC */
  rec?: boolean;
  page?: { cur: number; total: number };
  agent?: string;
}

/** 下行渲染帧：幂等整屏替换，seq 单调递增。 */
export interface FrameMessage {
  type: 'frame';
  seq: number;
  /** S0 待机 S2 聆听 S3 确认 S4 思考 S5 工具 S6 流式 S7 阅读 S8 错误 */
  state: string;
  containers: FrameContainers;
  meta?: FrameMeta;
}

export interface PairOkMessage {
  type: 'pair_ok';
  deviceId: string;
  accessToken: string;
  /** 只此一次下发 */
  refreshToken: string;
  exp: number;
}

export interface HelloOkMessage {
  type: 'hello_ok';
  deviceId: string;
  exp: number;
  server?: string;
  /** 当前帧原样重放，直接渲染即恢复现场 */
  resume?: FrameMessage;
}

export interface RefreshOkMessage {
  type: 'refresh_ok';
  accessToken: string;
  exp: number;
}

export interface ErrorMessage {
  type: 'error';
  code: string;
  message?: string;
}

export interface PongMessage {
  type: 'pong';
  t?: number;
}

/**
 * 网关下发的命令（协议 v1.1）。加它是**加法安全**的：
 * 两端对未知消息类型都静默忽略，旧插件遇到 cmd 什么也不做，旧网关收到 cmd_result 同理。
 */
export interface CmdMessage {
  type: 'cmd';
  cmd: string;
  id: string;
}

/**
 * 一次眼镜遥测采样。**每个字段都可能缺**——SDK 的 `DeviceStatus` 里除 `sn` 外
 * 全是可选字段，缺就是缺，不能填 0 冒充。
 *
 * `isGlasses` 是**必须**由插件判定的：`DeviceStatus` 只带 sn、不带 model
 * （SDK `dist/index.d.ts:143`），而 Even 生态里 R1 戒指与眼镜走同一套状态推送。
 * 网关只接受 `isGlasses === true` 的记录，否则会把戒指的电量当成眼镜的报出去。
 */
export interface GlassesTelemetry {
  model: string | null;
  sn: string | null;
  isGlasses: boolean;
  connectType: string | null;
  connected: boolean;
  batteryLevel: number | null;
  isCharging: boolean | null;
  isWearing: boolean | null;
  isInCase: boolean | null;
}

export type ServerMessage =
  | FrameMessage
  | PairOkMessage
  | HelloOkMessage
  | RefreshOkMessage
  | ErrorMessage
  | PongMessage
  | CmdMessage;
