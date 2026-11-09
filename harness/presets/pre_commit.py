"""PresetHook: ручное подключение пресетов к локальному pre-commit и проверка индекса.

python -X utf8 -B pre_commit.py install --cwd <repo>  - локальный неотслеживаемый hook (0/2)
python -X utf8 -B pre_commit.py check --cwd <repo>    - JSON {checked, violations}; 0/1/2
Проверяются полные staged тексты; code_lines/test_lines - ориентир, при коммите не применяются.
Ошибки - код 2 и строка stderr «presets: ...», stdout пуст.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from pre_commit_checks import check_sources
from pre_commit_git import (
    Runner,
    assert_unchanged,
    git,
    git_path,
    read_snapshot,
    repo_info,
)
from pre_commit_projects import load_project

SUBPROCESS_TIMEOUT_S = 120  # tsc проекта ~17 с, холодный старт дольше
MARKER = "# harness-preset-hook-v1"
BACKUP = "pre-commit.harness-original"
SCRIPT = Path(__file__).resolve()


def default_run(argv: Sequence[str], cwd: Path, stdin: bytes | None) -> subprocess.CompletedProcess[bytes]:
    """Окружение наследуется целиком (GIT_INDEX_FILE и др.); предел на каждый вызов."""
    found = shutil.which(argv[0])  # pyright/tsc из npm на Windows - .cmd, без пути не запускается
    argv = [found if found and found.lower().endswith((".cmd", ".bat")) else argv[0], *argv[1:]]
    return subprocess.run(
        argv,
        cwd=cwd,
        input=stdin if stdin is not None else b"",
        capture_output=True,
        shell=False,
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )


def _parse(argv: Sequence[str]) -> tuple[str, Path]:
    if len(argv) != 3 or argv[0] not in ("install", "check") or argv[1] != "--cwd" or not argv[2]:
        raise ValueError("использование: pre_commit.py (install|check) --cwd <repo>")
    cwd = Path(argv[2])
    if not cwd.is_dir():
        raise ValueError(f"нет каталога {cwd}")
    return argv[0], cwd


def _locate(cwd: Path, run: Runner) -> tuple[Path, Path]:
    """(корень worktree, общий git-dir); не-Git и bare - ValueError."""
    root = git_path(run, cwd, "--show-toplevel")
    return root, git_path(run, root, "--path-format=absolute", "--git-common-dir")


def _check(cwd: Path, run: Runner) -> tuple[int, str]:
    root, common = _locate(cwd, run)
    preset = load_project(repo_info(root, common, run))
    snapshot = read_snapshot(root, run, preset=preset)
    found = check_sources(snapshot.files, root, run, preset=preset)
    assert_unchanged(root, snapshot, run)
    found = sorted(found, key=lambda v: (v["path"], v["line"], v["rule"]))
    result = {"checked": len(snapshot.files), "violations": found}
    return (1 if found else 0), json.dumps(result, ensure_ascii=False)


def _quote(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "'\\''") + "'"


def wrapper(hook: Path) -> bytes:
    """Текст hook: исполняемый оригинал первым, затем check по текущему worktree.

    --cwd . : Git запускает hook в корне вызвавшего worktree; "$PWD" в sh Windows - /c/...,
    такой путь Python не понимает.
    """
    return (
        "#!/bin/sh\n" + MARKER + "\n"
        f"backup={_quote(hook.with_name(BACKUP))}\n"
        'if [ -x "$backup" ]; then\n  "$backup" "$@" || exit $?\nfi\n'
        f"exec python -X utf8 -B {_quote(SCRIPT)} check --cwd .\n"
    ).encode("utf-8")


def _inside(path: Path, *folders: Path) -> bool:
    return any(path.is_relative_to(folder) for folder in folders)


def _tracked(run: Runner, root: Path, path: Path) -> bool:
    if not path.is_relative_to(root):
        return False
    rel = path.relative_to(root).as_posix()
    return bool(git(run, root, "ls-files", "-z", "--", f":(literal){rel}"))


def _plan(cwd: Path, run: Runner) -> tuple[Path, bytes]:
    """Цель hook и его текст; все отказы - до любой записи."""
    root, common = _locate(cwd, run)
    hook = git_path(run, root, "--path-format=absolute", "--git-path", "hooks/pre-commit")
    root, common, hook = root.resolve(), common.resolve(), hook.parent.resolve() / hook.name
    if not _inside(hook, common, root) or str(hook.parent.as_posix()).startswith("/dev/null"):
        raise ValueError(f"hooks вне репозитория ({hook.parent}); общий hooksPath не перенастраивается")
    backup = hook.with_name(BACKUP)
    if hook.is_symlink() or backup.is_symlink():
        raise ValueError(f"{hook.name}: symlink не заменяется")
    if _tracked(run, root, hook) or _tracked(run, root, backup):
        raise ValueError("hook или его резервная копия отслеживаются Git")
    return hook, wrapper(hook)


def _publish(hook: Path, text: bytes) -> None:
    """Временный файл рядом, копия оригинала, атомарная замена; при сбое - откат."""
    backup = hook.with_name(BACKUP)
    hook.parent.mkdir(exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix="pre-commit.", suffix=".harness-tmp", dir=hook.parent)
    saved = False
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(text)
        os.chmod(temp, 0o755)
        if hook.exists():
            shutil.copy2(hook, backup)
            saved = True
        os.replace(temp, hook)
    except OSError:
        Path(temp).unlink(missing_ok=True)
        if saved:
            backup.unlink(missing_ok=True)
        raise


def _install(cwd: Path, run: Runner) -> tuple[int, str]:
    hook, text = _plan(cwd, run)
    current = hook.read_bytes() if hook.exists() else None
    if current == text:
        return 0, f"pre-commit: уже подключён {hook}"
    if current is not None and MARKER.encode() in current:
        raise ValueError(f"{hook}: другой harness-hook (шаблон или checkout); восстанови вручную")
    if hook.with_name(BACKUP).exists():
        raise ValueError(f"{BACKUP} уже существует; разберись с ним вручную")
    if current is not None and not hook.is_file():
        raise ValueError(f"{hook}: не обычный файл")
    _publish(hook, text)
    return 0, f"pre-commit: установлен {hook}"


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    run = run or default_run
    out, err = stdout or sys.stdout, stderr or sys.stderr
    try:
        command, cwd = _parse(sys.argv[1:] if argv is None else list(argv))
        code, text = (_install if command == "install" else _check)(cwd, run)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        err.write(f"presets: {str(error).splitlines()[0] if str(error) else type(error).__name__}\n")
        return 2
    out.write(text + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
