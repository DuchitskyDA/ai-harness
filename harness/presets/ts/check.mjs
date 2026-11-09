// Адаптер oxlint для PresetHook: полные staged тексты из stdin -> Violation[] пакетом, один oxlint на вызов.
// CLI: node check.mjs --cwd <abs> [--layout <json>] < {"files":[{path,source}]}
// Коды: 0 - чисто, 1 - нарушения, 2 - невозможность проверки (stdout пуст, причина в stderr).
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { PRESETS, VITEST_ALIAS, oxlintConfig } from './index.mjs';

const SUFFIX = /\.(ts|tsx|js|jsx|mjs|cjs|mts|cts)$/i;
// Inline-отключения в коммит-проверке не действуют: директива в копии перестаёт быть директивой.
const DIRECTIVE = /\b(eslint|oxlint)-(disable|enable)/g;
const require = createRequire(import.meta.url);

function relPath(cwd, value) {
  if (typeof value !== 'string' || !value || path.isAbsolute(value) || !SUFFIX.test(value)) {
    throw new Error(`недопустимый путь файла: ${JSON.stringify(value)}`);
  }
  const rel = path.relative(cwd, path.resolve(cwd, value));
  if (!rel || rel === '..' || rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel)) throw new Error(`путь вне cwd: ${value}`);
  return value.replaceAll('\\', '/');
}

function validFiles(files, cwd) {
  if (!Array.isArray(files)) throw new Error('files должен быть массивом');
  return files.map((file) => {
    if (typeof file?.source !== 'string') throw new Error('source должен быть строкой');
    return { path: relPath(cwd, file.path), source: file.source };
  });
}

export function validLayout(layout) {
  const ok = Array.isArray(layout) && layout.length > 0 && layout.every((item) =>
    typeof item?.dir === 'string' && item.dir && !path.isAbsolute(item.dir) && !item.dir.split('/').includes('..')
    && Object.hasOwn(PRESETS, item.preset));
  if (!ok) throw new Error('layout: нужен непустой массив {dir, preset: base|reactFsd|backend}');
  return layout.map((item) => ({ ...item, dir: item.dir.replace(/\/+$/, '') || '.' }));
}

function oxlintBin() {
  const manifest = require.resolve('oxlint/package.json');
  const { bin } = JSON.parse(readFileSync(manifest, 'utf8'));
  return path.join(path.dirname(manifest), typeof bin === 'string' ? bin : bin.oxlint);
}

// oxlint: eslint(max-depth) -> max-depth; vitest-js(max-expects) -> vitest/max-expects; без кода - parse-error.
export function ruleId(code) {
  const match = /^([\w@/-]+)\(([\w/-]+)\)$/.exec(code ?? '');
  if (!match) return 'parse-error';
  if (match[1] === 'eslint') return match[2];
  return `${match[1] === VITEST_ALIAS ? 'vitest' : match[1]}/${match[2]}`;
}

function toViolations(diagnostics) {
  return diagnostics.filter((d) => d.severity === 'error').map((d) => ({
    path: String(d.filename).replaceAll('\\', '/').replace(/^\.\//, ''),
    line: Math.max(1, Number(d.labels?.[0]?.span?.line) || 1),
    rule: ruleId(d.code),
    message: String(d.message ?? '').split('\n')[0],
  }));
}

function runOxlint(dir) {
  const config = path.join(dir, '.oxlintrc.json');
  const done = spawnSync(process.execPath, [oxlintBin(), '-c', config, '-f', 'json', '.'], {
    cwd: dir, encoding: 'utf8', maxBuffer: 256 * 1024 * 1024,
  });
  if (done.error || ![0, 1].includes(done.status)) {
    throw new Error(`oxlint завершился с кодом ${done.status}: ${(done.stderr || done.error?.message || '').trim()}`);
  }
  // Все файлы под игнорами: oxlint пишет текст вместо JSON.
  if (done.stdout.startsWith('No files found to lint')) return [];
  return JSON.parse(done.stdout).diagnostics;
}

const order = (a, b) => a.path.localeCompare(b.path) || a.line - b.line || a.rule.localeCompare(b.rule);

// Тексты -> временная копия -> oxlint с готовым конфигом -> Violation[]; директивы отключения не действуют.
export function lintSources(files, config) {
  const dir = mkdtempSync(path.join(tmpdir(), 'harness-oxlint-'));
  try {
    writeFileSync(path.join(dir, '.oxlintrc.json'), JSON.stringify(config));
    for (const file of files) {
      mkdirSync(path.dirname(path.join(dir, file.path)), { recursive: true });
      writeFileSync(path.join(dir, file.path), file.source.replace(DIRECTIVE, '$1-$2-off-in-commit-check'));
    }
    const known = new Set(files.map((file) => file.path));
    const found = toViolations(runOxlint(dir));
    if (found.some((v) => !known.has(v.path))) throw new Error('oxlint вернул путь вне проверяемых файлов');
    return found.sort(order);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

export async function checkSources(files, { cwd, layout = [{ dir: '.', preset: 'base' }] }) {
  const config = oxlintConfig(validLayout(layout));
  const checked = validFiles(files, cwd);
  return checked.length === 0 ? [] : lintSources(checked, config);
}

function parseArgs(argv) {
  const known = new Set(['--cwd', '--layout']);
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (!known.has(flag) || value === undefined || flag in options) throw new Error(`неверный аргумент: ${flag}`);
    options[flag] = value;
  }
  if (!options['--cwd'] || !path.isAbsolute(options['--cwd'])) throw new Error('нужен абсолютный --cwd');
  const layout = options['--layout'] === undefined ? undefined : JSON.parse(options['--layout']);
  return { cwd: options['--cwd'], ...(layout && { layout }) };
}

export async function runCli(argv, stdinText) {
  try {
    const options = parseArgs(argv);
    const found = await checkSources(JSON.parse(stdinText)?.files, options);
    return { code: found.length ? 1 : 0, stdout: JSON.stringify(found), stderr: '' };
  } catch (error) {
    return { code: 2, stdout: '', stderr: `presets: ${error.message.split('\n')[0]}` };
  }
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = await runCli(process.argv.slice(2), await readStdin());
  process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(`${result.stderr}\n`);
  process.exitCode = result.code;
}
