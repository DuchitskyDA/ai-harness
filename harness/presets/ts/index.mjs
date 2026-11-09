// Пресеты oxlint harness для TypeScript: base, reactFsd, backend. Каждая функция возвращает overrides oxlint;
// oxlintConfig собирает их по раскладке проекта в полный конфиг.
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const PLUGIN = fileURLToPath(new URL('./plugin.mjs', import.meta.url));
const TESTING_LIBRARY = require.resolve('eslint-plugin-testing-library');
// Правила vitest - из @vitest/eslint-plugin, как раньше: нативные vitest/* у oxlint считают иначе.
// Имя vitest у oxlint занято нативным плагином; адаптер возвращает правилам префикс vitest/.
export const VITEST_ALIAS = 'vitest-js';
const VITEST = fileURLToPath(import.meta.resolve('@vitest/eslint-plugin'));

const DEFAULT_FILES = ['**/*.{ts,tsx,js,jsx,mjs,cjs,mts,cts}'];
const DEFAULT_TEST_FILES = ['**/*.{test,spec}.{ts,tsx,js,jsx,mjs}'];
const DEFAULT_IGNORES = ['**/dist/**', '**/node_modules/**', '**/generated/**', '**/*.gen.ts'];
export const PRESETS = { base, reactFsd, backend };

// oxlint overrides не знают ignores: исключение - поздний override, выключающий правило.
const off = (files, rules) => ({ files, rules: Object.fromEntries(rules.map((rule) => [rule, 'off'])) });

export function base({ files = DEFAULT_FILES, testFiles = DEFAULT_TEST_FILES } = {}) {
  return [
    {
      files,
      rules: {
        'max-lines-per-function': ['error', { max: 80, skipBlankLines: true, skipComments: true }],
        complexity: ['error', 20],
        'max-depth': ['error', 4],
        'harness/max-params': ['error', { max: 3, ignoreConstructors: true }],
      },
    },
    {
      files: testFiles,
      rules: {
        'max-lines-per-function': 'off',
        [`${VITEST_ALIAS}/max-expects`]: ['error', { max: 5 }],
        [`${VITEST_ALIAS}/no-conditional-in-test`]: 'error',
        [`${VITEST_ALIAS}/no-disabled-tests`]: 'error',
        [`${VITEST_ALIAS}/no-focused-tests`]: 'error',
        'harness/no-loop-in-test': 'error',
      },
    },
  ];
}

export function reactFsd(options = {}) {
  const {
    srcDir = 'src',
    alias = '@/',
    layers = ['shared', 'entities', 'features', 'widgets', 'pages', 'app'],
    testFiles = DEFAULT_TEST_FILES,
    files = DEFAULT_FILES,
  } = options;
  return [
    ...base(options),
    { files, rules: { 'harness/fsd-import': ['error', { srcDir, alias, layers }] } },
    off([...testFiles, '**/testing/**'], ['harness/fsd-import']),
    {
      files: testFiles,
      rules: {
        'testing-library/prefer-screen-queries': 'error',
        'testing-library/prefer-user-event': 'error',
        'testing-library/no-node-access': 'error',
        'testing-library/no-container': 'error',
        'harness/one-render-per-test': 'error',
      },
    },
  ];
}

export function backend(options = {}) {
  const {
    layers = {
      handler: ['.controller', '.handler', '.routes', '.resolver'],
      service: ['.service'],
      repository: ['.repository'],
    },
    order = ['handler', 'service', 'repository'],
    data = ['@prisma/client', 'prisma.service'],
    testFiles = DEFAULT_TEST_FILES,
  } = options;
  return [
    ...base(options),
    { files: ['**/*.ts'], rules: { 'harness/layer-import': ['error', { layers, order, data }] } },
    off(testFiles, ['harness/layer-import']),
  ];
}

const posix = (file) => file.replaceAll(path.sep, '/');
const prefixed = (dir, patterns) => (dir === '.' ? patterns : patterns.map((pattern) => `${dir}/${pattern}`));

// Раскладка: [{ dir, preset: 'base'|'reactFsd'|'backend', options?, ignores? }]; шаблоны пресетов - от dir.
export function oxlintConfig(layout = [{ dir: '.', preset: 'base' }]) {
  const overrides = layout.flatMap(({ dir, preset, options = {} }) =>
    PRESETS[preset](options).map((item) => ({ ...item, files: prefixed(dir, item.files) })),
  );
  const ignores = layout.flatMap(({ dir, ignores = [] }) => prefixed(dir, ignores));
  return {
    categories: { correctness: 'off' },
    plugins: ['eslint'],
    jsPlugins: [PLUGIN, TESTING_LIBRARY, { name: VITEST_ALIAS, specifier: VITEST }].map((item) =>
      typeof item === 'string' ? posix(item) : { ...item, specifier: posix(item.specifier) }),
    ignorePatterns: [...DEFAULT_IGNORES, ...ignores],
    overrides,
  };
}
