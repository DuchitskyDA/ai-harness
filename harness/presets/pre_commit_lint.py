"""Проектные lint-команды PresetHook: staged тексты через stdin, без рабочих файлов и command/{files}.

harness-json - stdin_command на каждый файл; oxlint-json - один вызов ts/check.mjs (oxlint оснастки)
на группу с раскладкой lint.layout. Любой иной код, битый JSON или чужой путь - ValueError.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pre_commit_git import Runner, StagedFile
from pre_commit_projects import LintSpec, ProjectPreset, covers, lint_dir

ADAPTER = Path(__file__).resolve().parent / "ts" / "check.mjs"


def _cwd(root: Path, lint: LintSpec) -> tuple[Path, str]:
    folder = lint_dir(lint)
    path = (root / folder).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"lint.cwd вне корня репозитория: {lint.cwd}")
    return path, folder


def _rel(folder: str, path: str) -> str:
    return path[len(folder) + 1 :] if folder else path


def _parse(out: bytes, name: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(out.decode("utf-8"))
        if not isinstance(data, list):
            raise TypeError("не массив")
        return [
            {
                "path": str(v["path"]),
                "line": max(1, int(v.get("line") or 1)),
                "rule": str(v.get("rule") or "parse-error"),
                "message": str(v.get("message", "")).strip().split("\n")[0],
            }
            for v in data
        ]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"{name}: вывод линтера не читается ({type(error).__name__})") from error


def _invoke(run: Runner, argv: list[str], cwd: Path, stdin: bytes, label: str) -> list[dict[str, Any]]:
    done = run(argv, cwd, stdin)
    if done.returncode not in (0, 1):
        raise ValueError(f"{label}: {argv[0]} завершился с кодом {done.returncode}")
    return _parse(done.stdout, f"{label}: {argv[0]}")


def _back(found: list[dict[str, Any]], folder: str, allowed: set[str], label: str) -> list[dict[str, Any]]:
    """Пути от lint.cwd -> от корня; каждый должен быть из переданной группы."""
    for item in found:
        path = posixpath.normpath(posixpath.join(folder, item["path"].replace("\\", "/")))
        if path not in allowed:
            raise ValueError(f"{label}: линтер вернул путь вне проверяемых файлов")
        item["path"] = path
    return found


def _harness_json(lint: LintSpec, group: list[StagedFile], root: Path, run: Runner) -> list[dict[str, Any]]:
    cwd, folder = _cwd(root, lint)
    found = []
    for file in group:
        rel = _rel(folder, file.path)
        argv = [{"{python}": "python", "{file}": rel}.get(a, a) for a in lint.stdin_command]
        result = _invoke(run, argv, cwd, file.source.encode("utf-8"), file.path)
        found += _back(result, folder, {file.path}, file.path)
    return found


def _oxlint_json(lint: LintSpec, group: list[StagedFile], root: Path, run: Runner) -> list[dict[str, Any]]:
    cwd, folder = _cwd(root, lint)
    argv = ["node", str(ADAPTER), "--cwd", str(cwd), "--layout", lint.layout]
    payload = {"files": [{"path": _rel(folder, f.path), "source": f.source} for f in group]}
    result = _invoke(run, argv, cwd, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "oxlint")
    return _back(result, folder, {f.path for f in group}, "oxlint")


def check_project_sources(
    files: Sequence[StagedFile], root: Path, preset: ProjectPreset, run: Runner
) -> tuple[set[str], list[dict[str, object]]]:
    """Все покрывающие LintSpec по порядку; (проверенные проектом пути, нарушения)."""
    checked: set[str] = set()
    found: list[dict[str, Any]] = []
    for lint in preset.lints:
        group = [f for f in files if covers(lint, f.path)]
        if not group:
            continue
        handler = _oxlint_json if lint.format == "oxlint-json" else _harness_json
        found += handler(lint, group, root, run)
        checked.update(f.path for f in group)
    return checked, found
