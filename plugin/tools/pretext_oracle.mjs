/**
 * 排版引擎的**外部 oracle**：用官方 @evenrealities/pretext 测量语料，输出给 Python 测试比对。
 *
 * 它与我们的 Python 实现相互独立（一个是官方 JS，一个是我们移植的 Python），
 * 所以两边一致就是真正的交叉验证，而不是自己验自己。
 *
 * 用法（stdin/stdout 均为 JSON）：
 *   echo '{"cases":[{"text":"你好 world","maxWidth":576}]}' | node tools/pretext_oracle.mjs
 * 输出：
 *   {"pretext":"0.1.4","results":[{"lineCount":1,"height":27,"lineWidths":[...],"textWidth":...}]}
 */
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { getAdvW, getTextWidth, measureTextWrap, pxTruncate } from '@evenrealities/pretext';

const HERE = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(
  await readFile(resolve(HERE, '../node_modules/@evenrealities/pretext/package.json'), 'utf8'));

const stdin = await new Promise((res, rej) => {
  let buf = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', c => { buf += c; });
  process.stdin.on('end', () => res(buf));
  process.stdin.on('error', rej);
});

const req = JSON.parse(stdin);
const results = (req.cases ?? []).map(c => {
  const m = measureTextWrap(c.text, c.maxWidth);
  return {
    lineCount: m.lineCount,
    height: m.height,
    lineWidths: m.lineWidths,
    textWidth: getTextWidth(c.text),
  };
});

// 可选：逐码点的 advW，用于校验度量表移植是否无损
const advs = (req.codepoints ?? []).map(cp => getAdvW(cp));
const truncs = (req.truncate ?? []).map(t => pxTruncate(t.text, t.maxPx));

process.stdout.write(JSON.stringify({ pretext: pkg.version, results, advs, truncs }));
