import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { checkSources } from '../check.mjs';
import { PRESETS, VITEST_ALIAS, oxlintConfig } from '../index.mjs';

const cwd = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const hints = JSON.parse(readFileSync(path.join(cwd, 'hints.json'), 'utf8'));

const lint = (preset, code, filePath) =>
  checkSources([{ path: filePath, source: code }], { cwd, layout: [{ dir: '.', preset }] });

// Уникальные имена: oxlint, в отличие от ESLint, считает повторное объявление ошибкой разбора.
const consts = (n) => Array.from({ length: n }, (_, i) => `  const a${i} = ${i};\n`).join('');
const fn = (lines) => `function f() {\n${consts(lines)}}\n`;
const nest = 'function f(a) { if (a) { if (a) { if (a) { if (a) { if (a) { return 1; } } } } } }';
const branches = `function f(a) {\n${Array.from({ length: 20 }, (_, i) => `  if (a === ${i}) return ${i};`).join('\n')}\n  return -1;\n}\n`;
const expects = (n) => `test('x', () => {\n${'  expect(1).toBe(1);\n'.repeat(n)}});\n`;
const tsx = 'src/features/a/ui/x.test.tsx';

// Нарушение на каждое включённое правило: [пресет, код, файл, ожидаемое правило]
const violations = [
  ['base', fn(81), 'src/a.ts', 'max-lines-per-function'],
  ['base', branches, 'src/a.ts', 'complexity'],
  ['base', nest, 'src/a.ts', 'max-depth'],
  ['base', 'function f(a, b, c, d) { return a; }', 'src/a.ts', 'harness/max-params'],
  ['base', expects(6), 'src/a.test.ts', 'vitest/max-expects'],
  ['base', "test('x', () => { if (a) { expect(1).toBe(1); } });", 'src/a.test.ts', 'vitest/no-conditional-in-test'],
  ['base', "test.skip('x', () => { expect(1).toBe(1); });", 'src/a.test.ts', 'vitest/no-disabled-tests'],
  ['base', "test.only('x', () => { expect(1).toBe(1); });", 'src/a.test.ts', 'vitest/no-focused-tests'],
  ['base', "test('x', () => { for (const x of xs) expect(x).toBe(1); });", 'src/a.test.ts', 'harness/no-loop-in-test'],
  ['reactFsd', "import { a } from '@/features/auth';", 'src/entities/user/model/x.ts', 'harness/fsd-import'],
  ['reactFsd', "test('x', () => { const { getByText } = render(<A/>); getByText('a'); });", tsx, 'testing-library/prefer-screen-queries'],
  ['reactFsd', "import { fireEvent, screen } from '@testing-library/react';\ntest('x', () => { fireEvent.click(screen.getByText('a')); });", tsx, 'testing-library/prefer-user-event'],
  ['reactFsd', "import { screen } from '@testing-library/react';\ntest('x', () => { screen.getByText('a').parentElement; });", tsx, 'testing-library/no-node-access'],
  ['reactFsd', "import { render } from '@testing-library/react';\ntest('x', () => { const { container } = render(<A/>); container.foo(); });", tsx, 'testing-library/no-container'],
  ['reactFsd', "test('x', () => { render(<A/>); render(<B/>); });", tsx, 'harness/one-render-per-test'],
  ['backend', "import { R } from './orders.repository';", 'src/orders.controller.ts', 'harness/layer-import'],
];

for (const [preset, code, file, rule] of violations) {
  test(`нарушение: ${preset} ${rule} — ровно своё правило`, async () => {
    const found = await lint(preset, code, file);
    assert.ok(found.length > 0, 'нарушение не поймано');
    assert.ok(found.every((v) => v.rule === rule), JSON.stringify(found.map((v) => v.rule)));
  });
}

test('функция на 80 строк кода и 30 комментариев и пустых чиста', async () => {
  const body = `function f() {\n${consts(78)}${'  // c\n\n'.repeat(15)}}\n`;
  assert.deepEqual(await lint('base', body, 'src/a.ts'), []);
  const over = `function f() {\n${consts(79)}${'  // c\n\n'.repeat(15)}}\n`;
  assert.equal((await lint('base', over, 'src/a.ts'))[0].rule, 'max-lines-per-function');
});

test('чистые фикстуры base, reactFsd и backend дают 0 нарушений', async () => {
  assert.deepEqual(await lint('base', 'export function f(a, b) { return a + b; }\n', 'src/a.ts'), []);
  assert.deepEqual(await lint('base', expects(5), 'src/a.test.ts'), []);
  const ok = "import { screen } from '@testing-library/react';\nimport { u } from '@/entities/user';\ntest('x', () => { render(<A/>); screen.getByText('a'); });\n";
  assert.deepEqual(await lint('reactFsd', ok, 'src/widgets/w/ui/x.test.tsx'), []);
  assert.deepEqual(await lint('reactFsd', "import { u } from '@/entities/user';\nexport const a = 1;\n", 'src/widgets/w/ui/x.tsx'), []);
  assert.deepEqual(await lint('backend', "import { S } from './orders.service';\nexport const a = 1;\n", 'src/orders.controller.ts'), []);
});

test('fsd-import не действует в testing/, тесты base не ограничены размером функции', async () => {
  assert.deepEqual(await lint('reactFsd', "import { a } from '@/features/auth';", 'src/entities/user/testing/x.ts'), []);
  assert.deepEqual(await lint('base', `test('x', () => {\n${consts(90)}});\n`, 'src/a.test.ts'), []);
});

test('no-conditional-in-test как у @vitest/eslint-plugin: ?? и && в тесте не нарушение', async () => {
  assert.deepEqual(await lint('base', "test('x', () => { expect(a ?? b).toBe(1); expect(a && b).toBe(1); });", 'src/a.test.ts'), []);
});

test('раскладка: пресет действует только в своём dir, файл вне раскладки без правил', async () => {
  const layout = [{ dir: 'apps/web', preset: 'reactFsd' }];
  const files = [
    { path: 'apps/web/src/entities/user/model/x.ts', source: "import { a } from '@/features/auth';\n" },
    { path: 'tools/x.ts', source: 'function f(a, b, c, d) { return a; }\n' },
  ];
  const found = await checkSources(files, { cwd, layout });
  assert.deepEqual(found.map((v) => [v.path, v.rule]), [['apps/web/src/entities/user/model/x.ts', 'harness/fsd-import']]);
});

test('ignores раскладки и общие игноры (dist, generated) не проверяются', async () => {
  const layout = [{ dir: 'p', preset: 'base', ignores: ['types/**'] }];
  const four = 'function f(a, b, c, d) { return a; }\n';
  const files = ['p/types/a.ts', 'p/dist/a.ts', 'p/src/generated/a.ts'].map((file) => ({ path: file, source: four }));
  assert.deepEqual(await checkSources(files, { cwd, layout }), []);
});

test('каждое включённое правило пресетов есть в hints.json, ключ * есть', () => {
  assert.equal(typeof hints['*'], 'string');
  const rules = Object.keys(PRESETS).flatMap((preset) => oxlintConfig([{ dir: '.', preset }]).overrides)
    .flatMap((item) => Object.entries(item.rules))
    .filter(([, value]) => (Array.isArray(value) ? value[0] : value) !== 'off')
    .map(([rule]) => rule.replace(`${VITEST_ALIAS}/`, 'vitest/'));
  assert.deepEqual(rules.filter((rule) => !(rule in hints)), []);
});
