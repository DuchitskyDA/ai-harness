// Плагин harness: правила, которых нет в plugin-ах typescript-eslint, vitest и testing-library.
import path from 'node:path';

const EXT = /\.[cm]?[jt]sx?$/;
const norm = (p) => p.replaceAll('\\', '/');
const FUNCTIONS = ['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression'];

function functionName(node) {
  if (node.id?.name) return node.id.name;
  const parent = node.parent;
  if (parent?.type === 'MethodDefinition' || parent?.type === 'Property') return parent.key?.name ?? 'метод';
  if (parent?.type === 'VariableDeclarator') return parent.id?.name ?? 'функция';
  return 'функция';
}

const maxParams = {
  meta: {
    type: 'suggestion',
    schema: [{ type: 'object', properties: { max: { type: 'integer' }, ignoreConstructors: { type: 'boolean' } }, additionalProperties: false }],
    messages: { tooMany: '{{name}}: {{n}} параметров при лимите {{max}}' },
  },
  create(context) {
    const { max = 3, ignoreConstructors = false } = context.options[0] ?? {};
    const check = (node) => {
      if (ignoreConstructors && node.parent?.type === 'MethodDefinition' && node.parent.kind === 'constructor') return;
      const n = node.params.filter((p) => !(p.type === 'Identifier' && p.name === 'this')).length;
      if (n > max) context.report({ node, messageId: 'tooMany', data: { name: functionName(node), n, max } });
    };
    return Object.fromEntries(FUNCTIONS.map((t) => [t, check]));
  },
};

function rootName(node) {
  if (node.type === 'Identifier') return node.name;
  if (node.type === 'MemberExpression') return rootName(node.object);
  if (node.type === 'CallExpression') return rootName(node.callee);
  return null;
}

const isTestCall = (node) =>
  node?.type === 'CallExpression' && ['test', 'it'].includes(rootName(node.callee));

// Общий обход: слушатели видят только узлы внутри колбэка test/it.
function insideTest(visit) {
  return (context) => {
    const stack = [];
    const enter = (node) => {
      if (isTestCall(node.parent) && node.parent.arguments.includes(node)) stack.push({ renders: 0 });
    };
    const leave = (node) => {
      if (isTestCall(node.parent) && node.parent.arguments.includes(node)) stack.pop();
    };
    const listeners = visit(context, () => stack.at(-1));
    return {
      ArrowFunctionExpression: enter,
      FunctionExpression: enter,
      'ArrowFunctionExpression:exit': leave,
      'FunctionExpression:exit': leave,
      ...listeners,
    };
  };
}

const oneRender = {
  meta: { type: 'problem', schema: [], messages: { second: 'второй render в одном тесте' } },
  create: insideTest((context, current) => ({
    CallExpression(node) {
      const test = current();
      if (!test || node.callee.type !== 'Identifier' || !/^render(?!Hook)/.test(node.callee.name)) return;
      test.renders += 1;
      if (test.renders > 1) context.report({ node, messageId: 'second' });
    },
  })),
};

const noLoop = {
  meta: { type: 'problem', schema: [], messages: { loop: 'цикл в тесте' } },
  create: insideTest((context, current) => {
    const report = (node) => current() && context.report({ node, messageId: 'loop' });
    return {
      ForStatement: report,
      ForInStatement: report,
      ForOfStatement: report,
      WhileStatement: report,
      DoWhileStatement: report,
      CallExpression(node) {
        if (node.callee.type === 'MemberExpression' && node.callee.property.name === 'forEach') report(node);
      },
    };
  }),
};

// Источники импортов: import, export from, import().
function importListeners(handle) {
  const source = (node) => node.source?.type === 'Literal' && typeof node.source.value === 'string' && handle(node, node.source.value);
  return {
    ImportDeclaration: source,
    ExportAllDeclaration: source,
    ExportNamedDeclaration: source,
    ImportExpression: source,
  };
}

function fsdTarget(spec, alias, fromDir) {
  if (spec.startsWith(alias)) return spec.slice(alias.length);
  if (!spec.startsWith('.')) return null;
  const joined = path.posix.normalize(path.posix.join(fromDir, spec));
  return joined.startsWith('..') ? null : joined;
}

const fsdImport = {
  meta: {
    type: 'problem',
    schema: [{ type: 'object', properties: { srcDir: { type: 'string' }, alias: { type: 'string' }, layers: { type: 'array', items: { type: 'string' } } }, additionalProperties: false }],
    messages: {
      layer: '{{from}} не импортирует верхний слой {{to}}',
      crossSlice: '{{layer}}/{{fromSlice}} не импортирует соседний слайс {{toSlice}}',
      publicApi: 'импортируй {{layer}}/{{slice}} через его index, а не {{spec}}',
    },
  },
  create(context) {
    const { srcDir = 'src', alias = '@/', layers = ['shared', 'entities', 'features', 'widgets', 'pages', 'app'] } = context.options[0] ?? {};
    const file = norm(context.filename);
    const marker = `/${srcDir}/`;
    const at = file.lastIndexOf(marker);
    if (at < 0) return {};
    const rel = file.slice(at + marker.length).split('/');
    const noSlices = new Set([layers[0], layers.at(-1)]);
    const fromLayer = rel[0];
    if (!layers.includes(fromLayer)) return {};
    const fromSlice = !noSlices.has(fromLayer) && rel.length > 2 ? rel[1] : null;
    return importListeners((node, spec) => {
      const target = fsdTarget(spec, alias, path.posix.dirname(rel.join('/')));
      const parts = target?.split('/').filter((s) => s && s !== '.');
      if (!parts || !layers.includes(parts[0])) return;
      const [toLayer] = parts;
      const toSlice = !noSlices.has(toLayer) ? parts[1] ?? null : null;
      const loc = { node: node.source };
      if (layers.indexOf(toLayer) > layers.indexOf(fromLayer)) {
        context.report({ ...loc, messageId: 'layer', data: { from: fromLayer, to: toLayer } });
      } else if (toSlice && toLayer === fromLayer && toSlice !== fromSlice) {
        context.report({ ...loc, messageId: 'crossSlice', data: { layer: fromLayer, fromSlice, toSlice } });
      } else if (toSlice && (toLayer !== fromLayer || toSlice !== fromSlice) && parts.length > 2) {
        context.report({ ...loc, messageId: 'publicApi', data: { layer: toLayer, slice: toSlice, spec } });
      }
    });
  },
};

const lastSegment = (spec) => spec.split('/').at(-1).replace(EXT, '');

const layerImport = {
  meta: {
    type: 'problem',
    schema: [{ type: 'object', properties: { layers: { type: 'object' }, order: { type: 'array', items: { type: 'string' } }, data: { type: 'array', items: { type: 'string' } } }, additionalProperties: false }],
    messages: {
      up: '{{from}} не импортирует {{to}}: обработчик → сервис → репозиторий → данные',
      skip: '{{from}} не импортирует {{to}}: обработчик → сервис → репозиторий → данные',
      data: '{{from}} не импортирует {{to}}: обработчик → сервис → репозиторий → данные',
    },
  },
  create(context) {
    const { layers = {}, order = [], data = [] } = context.options[0] ?? {};
    const layerOf = (name) => order.find((l) => (layers[l] ?? []).some((suffix) => name.endsWith(suffix)));
    const fromLayer = layerOf(path.posix.basename(norm(context.filename)).replace(EXT, ''));
    if (!fromLayer) return {};
    const fromIdx = order.indexOf(fromLayer);
    const dataSet = new Set(data);
    return importListeners((node, spec) => {
      if (node.importKind === 'type' || node.exportKind === 'type') return;
      const report = (messageId, to) => context.report({ node: node.source, messageId, data: { from: fromLayer, to } });
      if (dataSet.has(spec) || dataSet.has(lastSegment(spec))) {
        if (fromIdx !== order.length - 1) report('data', 'data');
        return;
      }
      const toLayer = layerOf(lastSegment(spec));
      if (!toLayer) return;
      const toIdx = order.indexOf(toLayer);
      if (toIdx < fromIdx) report('up', toLayer);
      else if (toIdx > fromIdx + 1) report('skip', toLayer);
    });
  },
};

export default {
  meta: { name: 'harness' },
  rules: {
    'max-params': maxParams,
    'one-render-per-test': oneRender,
    'no-loop-in-test': noLoop,
    'fsd-import': fsdImport,
    'layer-import': layerImport,
  },
};
