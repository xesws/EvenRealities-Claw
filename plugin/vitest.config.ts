import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts'],
    // 桥接测试要等 120ms 防抖 + 真实 setTimeout，给足余量
    testTimeout: 15000,
  },
});
