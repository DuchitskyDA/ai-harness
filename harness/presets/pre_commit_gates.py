"""formatters и typecheck проекта поверх lint.

formatters - stdin_command на каждый покрытый staged файл, stdout сравнивается со staged текстом.
typecheck - command один раз по рабочей копии, если staged файлы покрыты; staged текст покрытого
файла должен совпадать с диском (иначе частичный stage). Нарушения - только в staged покрытых путях.
Любой иной код, нечитаемый вывод или чужой cwd - ValueError.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pre_commit_git import Runner, StagedFile
from pre_commit_projects import GateSpec, ProjectPreset, covers, lint_dir

TSC_LINE = re.compile(r"^(?P<path>[^\s(][^(]*)\((?P<line>\d+),\d+\): error (?P<rule>TS\d+): (?P<message>.*)$")


def _cwd(root: Path, spec: GateSpec) -> tuple[Path, str]:
    folder = lint_dir(spec)
    return root / folder, folder


def _first_diff(old: str, new: str) -> int:
    a, b = old.splitlines(), new.splitlines()
    return next((i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)) + 1)


def _format(spec: GateSpec, group: list[StagedFile], root: Path, run: Runner) -> list[dict[str, Any]]:
    cwd, folder = _cwd(root, spec)
    found = []
    for file in group:
        rel = file.path[len(folder) + 1 :] if folder else file.path
        argv = [{"{python}": "python", "{file}": rel}.get(a, a) for a in spec.command]
        done = run(argv, cwd, file.source.encode("utf-8"))
        if done.returncode != 0:
            raise ValueError(f"{file.path}: {argv[0]} завершился с кодом {done.returncode}")
        out = done.stdout.decode("utf-8")
        if out != file.source:
            found.append(
                {
                    "path": file.path,
                    "line": _first_diff(file.source, out),
                    "rule": "format",
                    "message": f"не отформатирован: {argv[0]}",
                }
            )
    return found


def _pyright(out: str, cwd: Path) -> list[tuple[str, int, str, str]]:
    return [
        (d["file"], int(d["range"]["start"]["line"]) + 1, str(d.get("rule") or "pyright"), str(d["message"]))
        for d in json.loads(out)["generalDiagnostics"]
        if d["severity"] == "error"
    ]


def _tsc(out: str, cwd: Path) -> list[tuple[str, int, str, str]]:
    hits = [TSC_LINE.match(line.strip()) for line in out.splitlines()]
    return [(str(cwd / m["path"]), int(m["line"]), m["rule"], m["message"]) for m in hits if m]


def _typecheck(spec: GateSpec, group: list[StagedFile], root: Path, run: Runner) -> list[dict[str, Any]]:
    for file in group:
        disk = root / file.path  # CRLF рабочей копии (autocrlf) - не частичный stage
        text = file.source.encode("utf-8").replace(b"\r\n", b"\n")
        if not disk.is_file() or disk.read_bytes().replace(b"\r\n", b"\n") != text:
            raise ValueError(f"{file.path}: частичный stage, typecheck идёт по рабочей копии")
    cwd, _ = _cwd(root, spec)
    command = [{"{python}": "python"}.get(a, a) for a in spec.command]
    done = run(command, cwd, None)
    try:
        hits = (_pyright if spec.format == "pyright-json" else _tsc)(done.stdout.decode("utf-8"), cwd)
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{command[0]}: вывод не читается ({type(error).__name__})") from error
    if done.returncode not in (0, 1, 2) or (done.returncode and not hits):
        raise ValueError(f"{command[0]} завершился с кодом {done.returncode}")
    staged = {f.path for f in group}
    found = []
    for path, line, rule, message in hits:
        rel = posixpath.normpath(os.path.relpath(path, root).replace("\\", "/"))
        if rel in staged:
            found.append({"path": rel, "line": line, "rule": rule, "message": message.strip().split("\n")[0]})
    return found


def check_gates(files: Sequence[StagedFile], root: Path, preset: ProjectPreset, run: Runner) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for specs, handler in ((preset.formatters, _format), (preset.typecheck, _typecheck)):
        for spec in specs:
            group = [f for f in files if covers(spec, f.path)]
            if group:
                found += handler(spec, group, root, run)
    return found
