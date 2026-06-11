/**
 * 配置存储：gatewayUrl / deviceId / refreshToken。
 * 优先走 bridge.setLocalStorage / getLocalStorage（数据持久化到 Even App 端）；
 * 环境里没有 bridge（harness 之外的纯浏览器调试）时降级到 window.localStorage。
 */
import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';

export interface LensConfig {
  gatewayUrl: string;
  deviceId: string;
  refreshToken: string;
}

const KEY_PREFIX = 'openclaw.lens.';
const KEYS: Record<keyof LensConfig, string> = {
  gatewayUrl: `${KEY_PREFIX}gatewayUrl`,
  deviceId: `${KEY_PREFIX}deviceId`,
  refreshToken: `${KEY_PREFIX}refreshToken`,
};

export class ConfigStore {
  private bridge: EvenAppBridge | null = null;

  setBridge(bridge: EvenAppBridge | null): void {
    this.bridge = bridge;
  }

  private async getRaw(key: string): Promise<string> {
    if (this.bridge) {
      try {
        const v = await this.bridge.getLocalStorage(key);
        return typeof v === 'string' ? v : '';
      } catch {
        // bridge 存储异常时降级
      }
    }
    try {
      return window.localStorage.getItem(key) ?? '';
    } catch {
      return '';
    }
  }

  private async setRaw(key: string, value: string): Promise<void> {
    let bridged = false;
    if (this.bridge) {
      try {
        await this.bridge.setLocalStorage(key, value);
        bridged = true;
      } catch {
        bridged = false;
      }
    }
    if (!bridged) {
      try {
        window.localStorage.setItem(key, value);
      } catch {
        // 隐私模式等场景下静默失败：配置只在本次会话内有效
      }
    }
  }

  async load(): Promise<LensConfig> {
    const [gatewayUrl, deviceId, refreshToken] = await Promise.all([
      this.getRaw(KEYS.gatewayUrl),
      this.getRaw(KEYS.deviceId),
      this.getRaw(KEYS.refreshToken),
    ]);
    return { gatewayUrl, deviceId, refreshToken };
  }

  async save(partial: Partial<LensConfig>): Promise<void> {
    const jobs: Promise<void>[] = [];
    for (const k of Object.keys(KEYS) as (keyof LensConfig)[]) {
      const v = partial[k];
      if (typeof v === 'string') jobs.push(this.setRaw(KEYS[k], v));
    }
    await Promise.all(jobs);
  }

  /** 吊销/重新配对时清除认证信息（保留网关地址方便重配）。 */
  async clearAuth(): Promise<void> {
    await Promise.all([this.setRaw(KEYS.deviceId, ''), this.setRaw(KEYS.refreshToken, '')]);
  }
}
