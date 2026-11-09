import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { lintSources } from '../check.mjs';
import plugin from '../plugin.mjs';

const PLUGIN = fileURLToPath(new URL('../plugin.mjs', import.meta.url)).replaceAll(path.sep, '/');

// messageId oxlint не отдаёт: восстанавливаем по шаблону сообщения правила.
function messageId(rule, message) {
  const templates = Object.entries(plugin.rules[rule].meta.messages);
  const pattern = (text) => new RegExp(`^${text.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{\{\w+\}\}/g, '.*')}$`);
  return templates.find(([, text]) => pattern(text).test(message))?.[0];
}

// rule: имя правила или [имя, опции].
async function lint(code, filePath, rule) {
  const [ruleName, options] = Array.isArray(rule) ? rule : [rule];
  const config = {
    categories: { correctness: 'off' },
    plugins: [],
    jsPlugins: [PLUGIN],
    rules: { [`harness/${ruleName}`]: ['error', ...(options ? [options] : [])] },
  };
  const found = lintSources([{ path: filePath, source: code }], config);
  return found.map((v) => ({ ruleId: v.rule, message: v.message, severity: 2, messageId: messageId(ruleName, v.message) }));
}

// У layer-import три messageId с одним текстом: в выводе oxlint они неразличимы, проверяется правило.
const withId = new Set(['harness/fsd-import']);
const ids = (messages) => messages.map((m) => (withId.has(m.ruleId) ? `${m.ruleId}:${m.messageId}` : m.ruleId));

test('плагин называется harness и несёт пять правил', () => {
  assert.equal(plugin.meta.name, 'harness');
  assert.deepEqual(Object.keys(plugin.rules).sort(), [
    'fsd-import', 'layer-import', 'max-params', 'no-loop-in-test', 'one-render-per-test',
  ]);
});

// ---- max-params
const mp = { max: 3, ignoreConstructors: true };

test('max-params: функция с 4 параметрами нарушает, русский текст с числами', async () => {
  const m = await lint('function f(a, b, c, d) {}', 'src/a.ts', ['max-params', mp]);
  assert.deepEqual(ids(m), ['harness/max-params']);
  assert.equal(m[0].severity, 2);
  assert.equal(m[0].message, 'f: 4 параметров при лимите 3');
});

test('max-params: конструктор на 5 параметров чист', async () => {
  const code = 'class A { constructor(a, b, c, d, e) {} }';
  assert.deepEqual(await lint(code, 'src/a.ts', ['max-params', mp]), []);
});

test('max-params: метод и стрелка считаются, параметр this нет', async () => {
  assert.deepEqual(ids(await lint('class A { m(a, b, c, d) {} }', 'src/a.ts', ['max-params', mp])), ['harness/max-params']);
  assert.deepEqual(ids(await lint('const f = (a, b, c, d) => a;', 'src/a.ts', ['max-params', mp])), ['harness/max-params']);
  assert.deepEqual(await lint('function f(this: Foo, a, b, c) {}', 'src/a.ts', ['max-params', mp]), []);
});

test('max-params: без ignoreConstructors конструктор считается', async () => {
  const code = 'class A { constructor(a, b, c, d) {} }';
  assert.deepEqual(ids(await lint(code, 'src/a.ts', ['max-params', { max: 3 }])), ['harness/max-params']);
});

// ---- one-render-per-test
const spec = 'src/a.test.tsx';

test('one-render-per-test: два render в тесте нарушают', async () => {
  const code = "test('x', () => { render(<A/>); render(<B/>); });";
  const m = await lint(code, spec, 'one-render-per-test');
  assert.deepEqual(ids(m), ['harness/one-render-per-test']);
  assert.equal(m[0].message, 'второй render в одном тесте');
});

test('one-render-per-test: render и renderHook чисты', async () => {
  const code = "it('x', () => { render(<A/>); renderHook(() => 1); });";
  assert.deepEqual(await lint(code, spec, 'one-render-per-test'), []);
});

test('one-render-per-test: it.only, it.skip и it.each тоже тесты, два render в разных тестах чисты', async () => {
  const two = 'render(<A/>); render(<B/>);';
  assert.equal((await lint(`it.only('x', () => { ${two} });`, spec, 'one-render-per-test')).length, 1);
  assert.equal((await lint(`it.skip('x', () => { ${two} });`, spec, 'one-render-per-test')).length, 1);
  assert.equal((await lint(`it.each([1])('x', () => { ${two} });`, spec, 'one-render-per-test')).length, 1);
  const code = "it('a', () => { render(<A/>); }); it('b', () => { render(<B/>); });";
  assert.deepEqual(await lint(code, spec, 'one-render-per-test'), []);
});

// ---- no-loop-in-test
test('no-loop-in-test: for-of в тесте нарушает', async () => {
  const code = "test('x', () => { for (const x of xs) expect(x); });";
  const m = await lint(code, spec, 'no-loop-in-test');
  assert.deepEqual(ids(m), ['harness/no-loop-in-test']);
  assert.equal(m[0].message, 'цикл в тесте');
});

test('no-loop-in-test: while, for, forEach нарушают; цикл вне теста и тест без цикла чисты', async () => {
  const bodies = ['while (a) {}', 'for (;;) {}', 'do {} while (a);', 'for (const k in o) {}', 'xs.forEach((x) => x);'];
  const counts = await Promise.all(bodies.map(async (body) => (await lint(`test('x', () => { ${body} });`, spec, 'no-loop-in-test')).length));
  assert.deepEqual(counts, bodies.map(() => 1));
  assert.deepEqual(await lint('for (const x of xs) {}', spec, 'no-loop-in-test'), []);
  assert.deepEqual(await lint("test('x', () => { expect(1); });", spec, 'no-loop-in-test'), []);
});

// ---- fsd-import
const fsd = { srcDir: 'src', alias: '@/', layers: ['shared', 'entities', 'features', 'widgets', 'pages', 'app'] };
const fsdLint = (code, file) => lint(code, file, ['fsd-import', fsd]);

test('fsd-import: нижний слой не импортирует верхний', async () => {
  const m = await fsdLint("import { a } from '@/features/auth';", 'src/entities/user/model/x.ts');
  assert.deepEqual(ids(m), ['harness/fsd-import:layer']);
  assert.equal(m[0].message, 'entities не импортирует верхний слой features');
});

test('fsd-import: соседний слайс запрещён', async () => {
  const m = await fsdLint("import { a } from '@/features/b';", 'src/features/a/ui/x.tsx');
  assert.deepEqual(ids(m), ['harness/fsd-import:crossSlice']);
  assert.equal(m[0].message, 'features/a не импортирует соседний слайс b');
});

test('fsd-import: глубокий импорт чужого слайса — publicApi, через index и shared чисто', async () => {
  const f = 'src/widgets/w/ui/x.tsx';
  const m = await fsdLint("import { s } from '@/entities/user/model/store';", f);
  assert.deepEqual(ids(m), ['harness/fsd-import:publicApi']);
  assert.equal(m[0].message, 'импортируй entities/user через его index, а не @/entities/user/model/store');
  assert.deepEqual(await fsdLint("import { u } from '@/entities/user';", f), []);
  assert.deepEqual(await fsdLint("import { x } from '../../../shared/lib/x';", f), []);
});

test('fsd-import: свой слайс, внешние модули и файл вне srcDir пропускаются; относительный глубокий в другой слайс ловится', async () => {
  assert.deepEqual(await fsdLint("import { x } from '@/features/a/model/x';", 'src/features/a/ui/x.tsx'), []);
  assert.deepEqual(await fsdLint("import React from 'react';", 'src/features/a/ui/x.tsx'), []);
  assert.deepEqual(await fsdLint("import { a } from '@/features/auth';", 'lib/x.ts'), []);
  const m = await fsdLint("import { x } from '../../../entities/user/model/x';", 'src/widgets/w/ui/x.tsx');
  assert.deepEqual(ids(m), ['harness/fsd-import:publicApi']);
});

test('fsd-import: в shared слайсов нет, импорт внутри shared чист', async () => {
  assert.deepEqual(await fsdLint("import { x } from '@/shared/lib/x';", 'src/shared/ui/b.ts'), []);
});

// ---- layer-import
const li = {
  layers: { handler: ['.controller', '.handler', '.routes', '.resolver'], service: ['.service'], repository: ['.repository'] },
  order: ['handler', 'service', 'repository'],
  data: ['@prisma/client', 'prisma.service'],
};
const liLint = (code, file) => lint(code, file, ['layer-import', li]);

test('layer-import: сервис импортирует данные напрямую — data', async () => {
  const m = await liLint("import { PrismaClient } from '@prisma/client';", 'src/orders.service.ts');
  assert.deepEqual(ids(m), ['harness/layer-import']);
  assert.equal(m[0].message, 'service не импортирует data: обработчик → сервис → репозиторий → данные');
});

test('layer-import: import type данных, и данные из репозитория чисты', async () => {
  assert.deepEqual(await liLint("import type { Order } from '@prisma/client';", 'src/orders.service.ts'), []);
  assert.deepEqual(await liLint("import { PrismaClient } from '@prisma/client';", 'src/orders.repository.ts'), []);
});

test('layer-import: контроллер -> репозиторий — skip', async () => {
  const m = await liLint("import { R } from './orders.repository';", 'src/orders.controller.ts');
  assert.deepEqual(ids(m), ['harness/layer-import']);
});

test('layer-import: репозиторий -> сервис — up; соседние слои вниз чисты', async () => {
  assert.deepEqual(ids(await liLint("import { S } from './orders.service';", 'src/orders.repository.ts')), ['harness/layer-import']);
  assert.deepEqual(await liLint("import { S } from './orders.service';", 'src/orders.controller.ts'), []);
  assert.deepEqual(await liLint("import { R } from './orders.repository';", 'src/orders.service.ts'), []);
});

test('layer-import: data по последнему сегменту, прочие файлы и модули пропускаются', async () => {
  assert.deepEqual(ids(await liLint("import { P } from '../prisma/prisma.service';", 'src/orders.controller.ts')), ['harness/layer-import']);
  assert.deepEqual(await liLint("import { x } from './util';", 'src/orders.controller.ts'), []);
  assert.deepEqual(await liLint("import { S } from './orders.service';", 'src/util.ts'), []);
});
