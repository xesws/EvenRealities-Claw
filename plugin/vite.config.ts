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

export default defineConfig({
  // 插件将被托管在 /plugin/ 子路径，必须用相对 base
  base: './',
  plugins: [copyAppManifest()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(root, 'index.html'),
        harness: resolve(root, 'harness/harness.html'),
      },
    },
  },
});
