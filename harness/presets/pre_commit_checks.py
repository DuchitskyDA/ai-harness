"""Проверка снимка PresetHook: проектные lint, затем fallback на непокрытом остатке.

Python - check_source пакета в процессе со стандартным Config(); TS/JS - один вызов ts/check.mjs
(base()) на весь непокрытый набор. Сбой транспорта - ValueError/OSError, не успех.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from pre_commit_gates import check_gates
from pre_commit_git import PY_SUFFIXES, TS_SUFFIXES, Runner, StagedFile
from pre_commit_lint import ADAPTER, check_project_sources
from pre_commit_projects import ProjectPreset

CODE_CHECK = Path(__file__).resolve().parent / "python" / "code_check.py"
MODULE = "harness_presets_code_check"


def code_check() -> ModuleType:
    """code_check.py этого пакета по пути, не одноимённый модуль из sys.path."""
    loaded = sys.modules.get(MODULE)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(MODULE, CODE_CHECK)
    if spec is None or spec.loader is None:
        raise OSError(f"нет {CODE_CHECK}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = module  # dataclass ищет свой модуль в sys.modules при exec
    spec.loader.exec_module(module)
    return module


def _ts_fallback(files: list[StagedFile], root: Path, run: Runner) -> list[dict[str, object]]:
    payload = json.dumps({"files": [{"path": f.path, "source": f.source} for f in files]}, ensure_ascii=False).encode(
        "utf-8"
    )
    done = run(["node", str(ADAPTER), "--cwd", str(root)], root, payload)
    if done.returncode not in (0, 1):
        raise ValueError(f"node ts/check.mjs завершился с кодом {done.returncode}")
    try:
        found = json.loads(done.stdout.decode("utf-8"))
        allowed = {f.path for f in files}
        if not isinstance(found, list) or any(v["path"] not in allowed for v in found):
            raise TypeError("не массив нарушений переданных файлов")
        return found
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"node ts/check.mjs: вывод не читается ({type(error).__name__})") from error


def check_sources(
    files: Sequence[StagedFile], root: Path, run: Runner, *, preset: ProjectPreset
) -> list[dict[str, object]]:
    covered, found = check_project_sources(files, root, preset, run)
    found += check_gates(files, root, preset, run)
    rest = [f for f in files if f.path not in covered]
    python = [f for f in rest if f.path.lower().endswith(PY_SUFFIXES)]
    if python:
        module = code_check()
        found += [v for f in python for v in module.check_source(f.path, f.source, module.Config())]
    ts = [f for f in rest if f.path.lower().endswith(TS_SUFFIXES)]
    if ts:
        found += _ts_fallback(ts, root, run)
    return found
