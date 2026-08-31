/**
 * 从 @evenrealities/pretext 提取 G2 固件字形度量表，生成网关排版引擎用的 JSON。
 *
 * 为什么要这一步：排版必须在服务器（Python）完成，但官方度量库是 JS。
 * 本脚本把官方内嵌的字体度量数据**原样**导出为 JSON，Python 侧只做算法移植，
 * 不重新推导任何数值 —— 这样生成物是可审计的（与 npm 包逐字段可 diff）。
 *
 * 用法：
 *   node tools/extract_metrics.mjs            # 生成到默认路径
 *   node tools/extract_metrics.mjs --check    # 只校验现有生成物是否与当前 pretext 一致
 */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const PRETEXT_DIR = resolve(HERE, '../node_modules/@evenrealities/pretext');
const OUT = resolve(HERE, '../../gateway/lens_gateway/formatting/data/g2_font_metrics.json');

/** 从 pretext 的 dist 里取出内嵌的 fontData 字面量（它是文件第一条 `const fontData = {...};`）。 */
async function loadFontData() {
  const src = await readFile(resolve(PRETEXT_DIR, 'dist/font_measure.js'), 'utf8');
  const m = src.match(/^const fontData = ([\s\S]*?);\nconst fonts/m);
  if (!m) throw new Error('无法在 pretext dist 中定位 fontData —— 上游结构可能变了，先人工核对再改本脚本');
  return JSON.parse(m[1]);
}

const pkg = JSON.parse(await readFile(resolve(PRETEXT_DIR, 'package.json'), 'utf8'));
const fontData = await loadFontData();

if (fontData.line_height !== 27) {
  throw new Error(`line_height 期望 27，实际 ${fontData.line_height} —— 固件版式变了，需要复核 HARDWARE-SPEC.md`);
}

const payload = {
  _comment: '自动生成，请勿手改。来源见 source 字段；重新生成：node plugin/tools/extract_metrics.mjs',
  source: { package: pkg.name, version: pkg.version },
  line_height: fontData.line_height,
  fonts: fontData.fonts,   // 原样透传，不做任何再加工
};

const text = JSON.stringify(payload);

if (process.argv.includes('--check')) {
  if (!existsSync(OUT)) { console.error('缺少生成物:', OUT); process.exit(1); }
  const cur = await readFile(OUT, 'utf8');
  if (cur !== text) {
    console.error(`生成物与当前 ${pkg.name}@${pkg.version} 不一致，请重新运行 extract_metrics.mjs`);
    process.exit(1);
  }
  console.log(`✓ 度量表与 ${pkg.name}@${pkg.version} 一致`);
  process.exit(0);
}

await mkdir(dirname(OUT), { recursive: true });
await writeFile(OUT, text);
const stats = fontData.fonts.map(f =>
  `${f.name}(glyphs=${f.glyphs ? Object.keys(f.glyphs).length : 0}` +
  `${f.ranges ? `,ranges=${f.ranges.length}` : ''}${f.kern ? ',kern' : ''})`).join(' ');
console.log(`✓ 写入 ${OUT}`);
console.log(`  来源 ${pkg.name}@${pkg.version}  line_height=${fontData.line_height}  ${(text.length / 1024).toFixed(0)} KB`);
console.log(`  字体链 ${stats}`);
