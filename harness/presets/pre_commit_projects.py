"""ProjectPresets: выбор и чтение проектного config из пакетного projects/ (default + первый совпавший).

Семантика прежняя: match.remotes - подстрока нормализованного remote, match.dirs - точное имя
основной копии без регистра; limits сливаются по ключу, exclude - объединение, прочие списки
заменяются, lint - объект, список или null. code_lines/test_lines - ориентир, не гейт коммита.
Невалидный config - ValueError с именем файла и поля, без содержимого.
"""

from __future__ import annotations

import dataclasses
import json
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECTS = Path(__file__).resolve().parent / "projects"
FORMATS = ("harness-json", "oxlint-json")
LAYOUT_PRESETS = ("base", "reactFsd", "backend")
TYPE_FORMATS = ("pyright-json", "tsc")
LISTS = ("code_extensions", "test_patterns", "page_patterns", "dom_test_patterns", "protected_configs")


class ConfigError(ValueError):
    """Невалидный или отсутствующий пакетный config (main -> код 2)."""


@dataclasses.dataclass(frozen=True)
class RepoInfo:
    remote: str
    name: str


@dataclasses.dataclass(frozen=True)
class LintSpec:
    command: tuple[str, ...]
    stdin_command: tuple[str, ...]
    cwd: str
    format: str
    hints: str
    paths: tuple[str, ...]
    layout: str = ""


@dataclasses.dataclass(frozen=True)
class GateSpec:
    """formatters: stdin_command с {file}; typecheck: command по рабочей копии, format из TYPE_FORMATS."""

    command: tuple[str, ...]
    cwd: str
    paths: tuple[str, ...]
    format: str = ""


@dataclasses.dataclass(frozen=True)
class ProjectPreset:
    name: str
    limits: Mapping[str, int]
    code_extensions: tuple[str, ...]
    test_patterns: tuple[str, ...]
    exclude: tuple[str, ...]
    lints: tuple[LintSpec, ...]
    formatters: tuple[GateSpec, ...] = ()
    typecheck: tuple[GateSpec, ...] = ()


def _glob(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            char = pattern[i]
            out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(pattern: str, path: str) -> bool:
    """Glob от корня: * и ? не пересекают /, ** - любая глубина, **/ - ноль и больше каталогов."""
    return _glob(pattern.replace("\\", "/")).match(path.replace("\\", "/")) is not None


def lint_dir(lint: LintSpec | GateSpec) -> str:
    """lint.cwd как относительный posix-каталог ('' - корень); выход из корня - ValueError."""
    rel = posixpath.normpath(lint.cwd.replace("\\", "/"))
    if posixpath.isabs(rel) or rel == ".." or rel.startswith("../"):
        raise ConfigError(f"lint.cwd вне корня репозитория: {lint.cwd}")
    return "" if rel == "." else rel


def covers(lint: LintSpec | GateSpec, rel: str) -> bool:
    """Файл rel (от корня) лежит под lint.cwd и подходит под lint.paths (пусто - любой)."""
    folder = lint_dir(lint)
    under = not folder or rel.startswith(folder + "/")
    return under and (not lint.paths or any(path_matches(p, rel) for p in lint.paths))


def _strings(data: dict[str, Any], key: str, where: str, *, nonempty=False) -> tuple[str, ...] | None:
    value = data.get(key)
    if value is None and key not in data:
        return None
    if not isinstance(value, list):
        raise ConfigError(f"{where}: поле {key} - не список строк")
    ok = all(isinstance(v, str) and (v or not nonempty) for v in value)
    if not ok or (nonempty and not value):
        raise ConfigError(f"{where}: поле {key} - не список строк")
    return tuple(value)


def _layout(value: Any, where: str) -> str:
    """oxlint-json: раскладка [{dir, preset, options?, ignores?}] -> JSON для ts/check.mjs --layout."""
    ok = (
        isinstance(value, list)
        and value
        and all(
            isinstance(item, dict)
            and isinstance(item.get("dir"), str)
            and item["dir"]
            and item.get("preset") in LAYOUT_PRESETS
            and isinstance(item.get("options", {}), dict)
            and isinstance(item.get("ignores", []), list)
            and set(item) <= {"dir", "preset", "options", "ignores"}
            for item in value
        )
    )
    if not ok:
        raise ConfigError(f"{where}: поле lint.layout - не список {{dir, preset}}")
    return json.dumps(value, ensure_ascii=False)


def _lint(value: Any, where: str) -> LintSpec:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: поле lint - не объект")
    cwd, fmt, hints = value.get("cwd", "."), value.get("format", "harness-json"), value.get("hints", "")
    paths = _strings(value, "paths", where + " lint", nonempty=True) or ()
    if not (fmt in FORMATS and isinstance(cwd, str) and isinstance(hints, str)):
        raise ConfigError(f"{where}: поле lint - неверный шаблон команды")
    if fmt == "oxlint-json":
        return LintSpec((), (), cwd or ".", fmt, hints, paths, _layout(value.get("layout"), where))
    command = _strings(value, "command", where + " lint", nonempty=True) or ()
    stdin = _strings(value, "stdin_command", where + " lint", nonempty=True) or ()
    pair = any(stdin[i : i + 2] == ("--stdin-filename", "{file}") for i in range(len(stdin)))
    if not ("{files}" in command and pair):
        raise ConfigError(f"{where}: поле lint - неверный шаблон команды")
    return LintSpec(command, stdin, cwd or ".", fmt, hints, paths)


def _lints(value: Any, where: str) -> tuple[LintSpec, ...]:
    if value is None:
        return ()
    return tuple(_lint(item, where) for item in (value if isinstance(value, list) else [value]))


def _gates(data: dict[str, Any], key: str, where: str) -> tuple[GateSpec, ...] | None:
    if key not in data:
        return None
    items, out = data[key], []
    for item in items if isinstance(items, list) else [None]:
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: поле {key} - не список объектов")
        name = "stdin_command" if key == "formatters" else "command"
        command = _strings(item, name, f"{where} {key}", nonempty=True) or ()
        cwd, fmt = item.get("cwd", "."), item.get("format", "")
        valid = isinstance(cwd, str) and (("{file}" in command) if key == "formatters" else fmt in TYPE_FORMATS)
        if not valid:
            raise ConfigError(f"{where}: поле {key} - неверный шаблон команды")
        out.append(GateSpec(command, cwd or ".", _strings(item, "paths", f"{where} {key}") or (), fmt))
    return tuple(out)


def _limits(data: dict[str, Any], where: str) -> dict[str, int]:
    limits = data.get("limits", {})
    if not isinstance(limits, dict) or not all(isinstance(v, int) and not isinstance(v, bool) for v in limits.values()):
        raise ConfigError(f"{where}: поле limits - не объект целых")
    return limits


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ConfigError(f"нет config {path.name}") from error
    except (OSError, ValueError) as error:
        raise ConfigError(f"config {path.name} не читается ({type(error).__name__})") from error
    if not isinstance(data, dict):
        raise ConfigError(f"config {path.name}: не объект")
    return data


def _pick(value: tuple[Any, ...] | None, fallback: tuple[Any, ...]) -> tuple[Any, ...]:
    return fallback if value is None else value


def _merge(preset: ProjectPreset, data: dict[str, Any], name: str, where: str) -> ProjectPreset:
    lists = {key: _strings(data, key, where) for key in LISTS}
    exclude = _strings(data, "exclude", where) or ()
    return dataclasses.replace(
        preset,
        name=name,
        limits={**preset.limits, **_limits(data, where)},
        code_extensions=_pick(lists["code_extensions"], preset.code_extensions),
        test_patterns=_pick(lists["test_patterns"], preset.test_patterns),
        exclude=tuple(dict.fromkeys((*preset.exclude, *exclude))),
        lints=_lints(data["lint"], where) if "lint" in data else preset.lints,
        formatters=_pick(_gates(data, "formatters", where), preset.formatters),
        typecheck=_pick(_gates(data, "typecheck", where), preset.typecheck),
    )


def _match(data: dict[str, Any], where: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    match = data.get("match")
    if match is None:
        return None
    if not isinstance(match, dict):
        raise ConfigError(f"{where}: поле match - не объект")
    return _strings(match, "remotes", where) or (), _strings(match, "dirs", where) or ()


def _remote_key(value: str) -> str:
    key = value.strip().lower().replace("\\", "/").rstrip("/")
    return key.removesuffix(".git").rstrip("/")


def _matches(match: tuple[tuple[str, ...], tuple[str, ...]], info: RepoInfo) -> bool:
    have = _remote_key(info.remote)
    if any(have and _remote_key(w) and _remote_key(w) in have for w in match[0]):
        return True
    return any(name.lower() == info.name.lower() for name in match[1])


def load_project(info: RepoInfo, *, folder: Path | None = None) -> ProjectPreset:
    """default.json и первый по имени файла проект с совпавшим match; читается при каждом вызове."""
    folder = PROJECTS if folder is None else folder
    empty = ProjectPreset("default", {}, (), (), (), ())
    preset = _merge(empty, _read(folder / "default.json"), "default", "default.json")
    for path in sorted(folder.glob("*.json"), key=lambda p: p.name):
        if path.name.lower() == "default.json":
            continue
        data = _read(path)
        match = _match(data, path.name)
        if match is not None and _matches(match, info):
            return _merge(preset, data, path.stem, path.name)
    return preset
