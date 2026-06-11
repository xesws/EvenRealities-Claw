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

export type ServerMessage =
  | FrameMessage
  | PairOkMessage
  | HelloOkMessage
  | RefreshOkMessage
  | ErrorMessage
  | PongMessage;
