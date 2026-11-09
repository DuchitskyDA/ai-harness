import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const pkg = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const adapter = path.join(pkg, 'check.mjs');
const FOUR = 'export function f(a, b, c, d) { return a + b + c + d; }\n';
const THREE = 'export function f(a, b, c) { return a + b + c; }\n';

async function api() {
  assert.ok(existsSync(adapter), 'нет адаптера ts/check.mjs');
  return import('../check.mjs');
}

test('fallback: чистый файл без нарушений', async () => {
  const { checkSources } = await api();
  assert.deepEqual(await checkSources([{ path: 'src/a.ts', source: THREE }], { cwd: pkg }), []);
});

test('fallback: четыре параметра - нарушение base', async () => {
  const { checkSources } = await api();
  const found = await checkSources([{ path: 'src/a.ts', source: FOUR }], { cwd: pkg });
  assert.deepEqual(found.map((v) => [v.path, v.line, v.rule]), [['src/a.ts', 1, 'harness/max-params']]);
});

test('eslint-disable и oxlint-disable не скрывают нарушение', async () => {
  const { checkSources } = await api();
  const source = `/* eslint-disable */\n/* oxlint-disable */\n// eslint-disable-next-line harness/max-params\n${FOUR}`;
  const found = await checkSources([{ path: 'a.mts', source }], { cwd: pkg });
  assert.deepEqual(found.map((v) => v.rule), ['harness/max-params']);
});

test('синтаксическая ошибка - parse-error без исходника', async () => {
  const { checkSources } = await api();
  const [found] = await checkSources([{ path: 'a.ts', source: 'const SECRET = ;\n' }], { cwd: pkg });
  assert.equal(found.rule, 'parse-error');
  assert.ok(!found.message.includes('SECRET'));
});

test('пустой набор - пустой результат', async () => {
  const { checkSources } = await api();
  assert.deepEqual(await checkSources([], { cwd: pkg }), []);
});

test('нарушения отсортированы по пути и строке', async () => {
  const { checkSources } = await api();
  const files = [{ path: 'b.ts', source: FOUR }, { path: 'a.tsx', source: `\n${FOUR}` }];
  const found = await checkSources(files, { cwd: pkg });
  assert.deepEqual(found.map((v) => [v.path, v.line]), [['a.tsx', 2], ['b.ts', 1]]);
});

test('раскладка: свой пресет в dir, остальное без правил', async () => {
  const { checkSources } = await api();
  const layout = [{ dir: 'apps/api', preset: 'backend' }];
  const files = [
    { path: 'apps/api/src/orders.controller.ts', source: "import { R } from './orders.repository';\n" },
    { path: 'web/a.ts', source: FOUR },
  ];
  const found = await checkSources(files, { cwd: pkg, layout });
  assert.deepEqual(found.map((v) => [v.path, v.rule]), [['apps/api/src/orders.controller.ts', 'harness/layer-import']]);
});

const invalid = [
  ['путь вне cwd', [{ path: '../a.ts', source: '' }], {}],
  ['абсолютный путь', [{ path: path.join(pkg, 'a.ts'), source: '' }], {}],
  ['не-TS суффикс', [{ path: 'a.py', source: '' }], {}],
  ['поле source не строка', [{ path: 'a.ts', source: 1 }], {}],
  ['files не массив', { path: 'a.ts' }, {}],
  ['неизвестный пресет', [{ path: 'a.ts', source: '' }], { layout: [{ dir: '.', preset: 'eslint' }] }],
  ['dir с ..', [{ path: 'a.ts', source: '' }], { layout: [{ dir: '../x', preset: 'base' }] }],
  ['пустая раскладка', [{ path: 'a.ts', source: '' }], { layout: [] }],
];

test('некорректный вход отклоняется', async () => {
  const { checkSources } = await api();
  const outcomes = await Promise.allSettled(invalid.map(([, files, extra]) => checkSources(files, { cwd: pkg, ...extra })));
  assert.deepEqual(outcomes.map((o) => o.status), invalid.map(() => 'rejected'));
});

const cliCases = [
  ['пустой набор', ['--cwd', pkg], '{"files":[]}', 0, '[]'],
  ['неизвестный флаг', ['--cwd', pkg, '--fix'], '{"files":[]}', 2, ''],
  ['нет --cwd', [], '{"files":[]}', 2, ''],
  ['битый JSON', ['--cwd', pkg], '{', 2, ''],
  ['битый --layout', ['--cwd', pkg, '--layout', '['], '{"files":[]}', 2, ''],
  ['относительный --cwd', ['--cwd', 'ts'], '{"files":[]}', 2, ''],
];

test('CLI: коды и пустой stdout при ошибке', async () => {
  const { runCli } = await api();
  const results = await Promise.all(cliCases.map(([, argv, stdin]) => runCli(argv, stdin)));
  assert.deepEqual(results.map((r) => [r.code, r.stdout]), cliCases.map(([, , , code, out]) => [code, out]));
});

test('CLI: нарушение даёт код 1 и JSON-массив, --layout применяется', async () => {
  const { runCli } = await api();
  const layout = JSON.stringify([{ dir: 'src', preset: 'base' }]);
  const result = await runCli(['--cwd', pkg, '--layout', layout], JSON.stringify({ files: [{ path: 'src/a.ts', source: FOUR }] }));
  assert.equal(result.code, 1);
  assert.deepEqual(JSON.parse(result.stdout).map((v) => v.rule), ['harness/max-params']);
});

test('ruleId: имена oxlint приводятся к прежним', async () => {
  const { ruleId } = await api();
  const codes = ['eslint(max-depth)', 'vitest-js(max-expects)', 'harness(fsd-import)', 'testing-library(no-container)', ''];
  assert.deepEqual(codes.map(ruleId), ['max-depth', 'vitest/max-expects', 'harness/fsd-import', 'testing-library/no-container', 'parse-error']);
});
