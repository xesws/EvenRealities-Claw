import { defineConfig, type Plugin } from 'vite';
import { fileURLToPath } from 'node:url';
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

const root = dirname(fileURLToPath(import.meta.url));

/** 构建结束后把 Even Hub 清单 app.json 拷进 dist/，方便整目录托管 + QR sideload。 */
function copyAppManifest(): Plugin {
  return {
    name: 'copy-app-manifest',
    closeBundle() {
      const src = resolve(root, 'app.json');
      const outDir = resolve(root, 'dist');
      if (existsSync(src)) {
        mkdirSync(outDir, { recursive: true });
        copyFileSync(src, resolve(outDir, 'app.json'));
      }
    },
  };
}

/**
 * 浏览器夹具与模拟器探针只在开发/验证时用，**不进 .ehpk**：
 * harness 那条 chunk 里含官方 pretext 的完整字形度量表（~130KB），
 * 装到用户眼镜上是纯负担。`npm run dev` 照常能访问 /harness/ 与 /probe/。
 * 需要构建出可托管的夹具页时：`EVENHUB_HARNESS=1 npm run build`。
 */
const withHarness = process.env.EVENHUB_HARNESS === '1';

export default defineConfig({
  // 插件将被托管在 /plugin/ 子路径，必须用相对 base
  base: './',
  plugins: [copyAppManifest()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(root, 'index.html'),
        ...(withHarness
          ? {
              harness: resolve(root, 'harness/harness.html'),
              probe: resolve(root, 'probe/probe.html'),
            }
          : {}),
      },
    },
  },
});
