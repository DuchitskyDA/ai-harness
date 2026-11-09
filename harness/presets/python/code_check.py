"""Проверяльщик правил кода Python на стандартной библиотеке (формат harness-json).

Запуск: python -X utf8 -B code_check.py [--config <json>] (--stdin-filename <путь> | <пути>...)
Вывод: JSON-массив {path, line, rule, message}; коды выхода 0 - чисто, 1 - есть нарушения,
2 - ошибка запуска (конфиг, аргументы, нечитаемый файл).
Пути не .py/.pyi пропускаются.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import os
import re
import sys
from collections.abc import Iterator
from typing import Any, TextIO


@dataclasses.dataclass(frozen=True)
class Layer:
    files: str
    forbid: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Config:
    function_lines: int = 80
    complexity: int = 20
    nesting: int = 4
    params: int = 5
    test_asserts: int = 5
    layers: tuple[Layer, ...] = ()


LIMIT_KEYS = ("function_lines", "complexity", "nesting", "params", "test_asserts")
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = FUNCS + (ast.ClassDef, ast.Lambda)
NESTING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)
BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.match_case)
LOOPS = (ast.For, ast.AsyncFor, ast.While)
CONDITIONS = (ast.If, ast.IfExp, ast.Match)
PY_SUFFIXES = (".py", ".pyi")


def _is_python(path: str) -> bool:
    return path.lower().endswith(PY_SUFFIXES)


def load_config(path: str | None) -> Config:
    """Читает JSON-конфиг; без пути - умолчания; битый файл - ValueError."""
    if path is None:
        return Config()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        limits = data.get("limits", {})
        layers = tuple(Layer(str(item["files"]), tuple(str(p) for p in item["forbid"]))
                       for item in data.get("layers", []))
        values = {key: limits[key] for key in LIMIT_KEYS if key in limits}
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in values.values()):
            raise ValueError("лимиты должны быть целыми")
        return Config(layers=layers, **values)
    except (OSError, AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"конфиг {path}: {error}") from error


def _violation(path: str, line: int, rule: str, message: str) -> dict[str, Any]:
    return {"path": path, "line": line, "rule": rule, "message": message}


def _functions(tree: ast.AST) -> Iterator[tuple[Any, bool, bool]]:
    """(функция, прямо в классе, внутри другой функции) для каждой функции файла."""
    def visit(node: ast.AST, in_class: bool, in_func: bool) -> Iterator[tuple[Any, bool, bool]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCS):
                yield child, in_class, in_func
                yield from visit(child, False, True)
            else:
                yield from visit(child, isinstance(child, ast.ClassDef), in_func)
    return visit(tree, False, False)


def _own_nodes(func: Any) -> Iterator[ast.AST]:
    """Узлы тела функции без вложенных def, class и lambda."""
    stack = list(reversed(func.body))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, SCOPES):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _code_lines(func: Any, lines: list[str]) -> int:
    skip: set[int] = set()
    first = func.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        skip = set(range(first.lineno, first.end_lineno + 1))
    count = 0
    for number in range(func.lineno, func.end_lineno + 1):
        text = lines[number - 1].strip() if number <= len(lines) else ""
        if text and not text.startswith("#") and number not in skip:
            count += 1
    return count


def _complexity(func: Any) -> int:
    total = 1
    for node in _own_nodes(func):
        if isinstance(node, BRANCHES):
            total += 1
        elif isinstance(node, ast.comprehension):
            total += 1 + len(node.ifs)
        elif isinstance(node, ast.BoolOp):
            total += len(node.values) - 1
    return total


def _depth(node: ast.AST, level: int) -> int:
    best = level
    for child in ast.iter_child_nodes(node):
        if isinstance(child, SCOPES):
            continue
        inner = level
        if isinstance(child, NESTING):
            is_elif = isinstance(node, ast.If) and node.orelse == [child]
            inner = level if is_elif else level + 1
        best = max(best, _depth(child, inner), inner)
    return best


def _param_count(func: Any, in_class: bool) -> int:
    args = func.args
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    if in_class and names and names[0] in ("self", "cls"):
        names = names[1:]
    return len(names)


def _size_violations(path: str, func: Any, in_class: bool, lines: list[str],
                     config: Config) -> list[dict[str, Any]]:
    name, found = func.name, []
    length = _code_lines(func, lines)
    if length > config.function_lines:
        found.append(_violation(path, func.lineno, "function-length",
                                f"функция {name} длиннее {config.function_lines} строк ({length})"))
    cplx = _complexity(func)
    if cplx > config.complexity:
        found.append(_violation(path, func.lineno, "complexity",
                                f"функция {name}: сложность {cplx} при лимите {config.complexity}"))
    depth = _depth(func, 0)
    if depth > config.nesting:
        found.append(_violation(path, func.lineno, "nesting",
                                f"функция {name}: вложенность {depth} при лимите {config.nesting}"))
    count = _param_count(func, in_class)
    if count > config.params:
        found.append(_violation(path, func.lineno, "params",
                                f"функция {name}: {count} параметров при лимите {config.params}"))
    return found


def _is_test_file(path: str) -> bool:
    parts = path.split("/")
    base = parts[-1]
    in_dir = any(part in ("test", "tests") for part in parts[:-1])
    return in_dir or (base.startswith("test_") and base.endswith(".py")) or base.endswith("_test.py")


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def _skip_without_reason(expr: ast.AST) -> bool:
    call = expr if isinstance(expr, ast.Call) else None
    name = _dotted(call.func if call else expr)
    has_reason = bool(call) and any(k.arg == "reason" for k in call.keywords)
    if name == "pytest.mark.skip":
        return not (has_reason or (call and call.args))
    if name == "pytest.mark.skipif":
        return not has_reason
    if name == "unittest.skip":
        first = call.args[0] if call and call.args else None
        return first is None or (isinstance(first, ast.Constant) and first.value == "")
    return False


def _is_assert_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
        and node.func.attr.startswith("assert") and _dotted(node.func.value) == "self"


def _test_violations(path: str, func: Any, config: Config) -> list[dict[str, Any]]:
    name, found, asserts = func.name, [], 0
    for node in _own_nodes(func):
        if isinstance(node, CONDITIONS):
            found.append(_violation(path, node.lineno, "test-conditional", f"условие в тесте {name}"))
        elif isinstance(node, LOOPS):
            found.append(_violation(path, node.lineno, "test-loop", f"цикл в тесте {name}"))
        elif isinstance(node, ast.Assert) or _is_assert_call(node):
            asserts += 1
        if isinstance(node, ast.Call) and _dotted(node.func) == "pytest.skip" \
                and not node.args and not node.keywords:
            found.append(_violation(path, node.lineno, "test-skip-reason",
                                    f"пропуск теста {name} без причины"))
    if asserts > config.test_asserts:
        found.append(_violation(path, func.lineno, "test-asserts",
                                f"тест {name}: {asserts} проверок при лимите {config.test_asserts}"))
    for deco in func.decorator_list:
        if _skip_without_reason(deco):
            found.append(_violation(path, deco.lineno, "test-skip-reason",
                                    f"пропуск теста {name} без причины"))
    return found


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            char = pattern[i]
            out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _relative(path: str) -> str:
    if os.path.isabs(path):
        try:
            path = os.path.relpath(path)
        except ValueError:
            pass
    path = path.replace("\\", "/")
    return path.removeprefix("./")


def _module_name(path: str) -> tuple[str, str]:
    """(имя модуля, пакет) по пути файла."""
    parts = _relative(path).removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), ".".join(parts[:-1])
    return ".".join(parts), ".".join(parts[:-1])


def _import_targets(node: ast.AST, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    base = node.module or ""
    if node.level:
        pkg = package.split(".") if package else []
        pkg = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
        base = ".".join(pkg + ([node.module] if node.module else []))
    if node.module is None:
        return [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
    return [base]


def _layer_violations(path: str, tree: ast.AST, config: Config) -> list[dict[str, Any]]:
    rel = _relative(path)
    layers = [layer for layer in config.layers if _glob_regex(layer.files).match(rel)]
    if not layers:
        return []
    module, package = _module_name(path)
    found = []
    for node in ast.walk(tree):
        for target in _import_targets(node, package):
            for layer in layers:
                if any(target == p or target.startswith(p + ".") for p in layer.forbid):
                    found.append(_violation(path, node.lineno, "layer-import",
                                            f"{module} не импортирует {target}: запрет слоя {layer.files}"))
    return found


def check_source(path: str, source: str, config: Config) -> list[dict[str, Any]]:
    """Нарушения одного исходника, отсортированные по (path, line, rule)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        line = error.lineno or 1
        return [_violation(path, line, "parse-error", f"файл не разбирается: {error.msg}")]
    except ValueError as error:
        return [_violation(path, 1, "parse-error", f"файл не разбирается: {error}")]
    lines, found = source.splitlines(), _layer_violations(path, tree, config)
    is_test = _is_test_file(_relative(path))
    for func, in_class, in_func in _functions(tree):
        found += _size_violations(path, func, in_class, lines, config)
        if is_test and not in_func and func.name.startswith("test_"):
            found += _test_violations(path, func, config)
    return sorted(found, key=lambda v: (v["path"], v["line"], v["rule"]))


def _parse_args(argv: list[str] | None) -> argparse.Namespace | None:
    parser = argparse.ArgumentParser(prog="code_check", add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--stdin-filename")
    parser.add_argument("paths", nargs="*")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return None
    if bool(args.paths) == bool(args.stdin_filename):
        print("укажи либо пути, либо --stdin-filename", file=sys.stderr)
        return None
    return args


def main(argv: list[str] | None = None, stdin: TextIO | None = None,
         stdout: TextIO | None = None) -> int:
    args = _parse_args(argv)
    if args is None:
        return 2
    try:
        config = load_config(args.config)
        sources = [(args.stdin_filename.replace("\\", "/"), (stdin or sys.stdin).read())] \
            if args.stdin_filename else [(p.replace("\\", "/"), _read(p))
                                         for p in args.paths if _is_python(p)]
        sources = [(path, text) for path, text in sources if _is_python(path)]
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    found = [v for path, text in sources for v in check_source(path, text, config)]
    (stdout or sys.stdout).write(json.dumps(found, ensure_ascii=False) + "\n")
    return 1 if found else 0


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    sys.exit(main())
