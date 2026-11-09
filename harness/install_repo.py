"""Шаги install.py о самом клоне оснастки: ссылка ~/.harness и снятие core.hooksPath.

Модуль не импортирует install: зависимости (создание ссылки, записи бэкапа)
передаются аргументами.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from install_files import InstallFiles


def _link_target(path: Path) -> str | None:
    try:
        return os.readlink(path).removeprefix("\\\\?\\")
    except (OSError, ValueError):
        return None


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


def ensure_harness_root(home: Path, link_directory: Callable[[Path, Path], None], *, repo: Path | None = None) -> None:
    """~/.harness - ссылка на <checkout>/harness; создать, если нет.

    Правильная ссылка - no-op, кроме junction -> symlink. Реальный каталог, чужая или висячая ссылка -
    отказ без записи и без обхода цели: данные в нём установщик
    не переносит и не читает. Клон может лежать вне HOME.
    """
    source = (repo or Path(__file__).resolve().parents[1]) / "harness"
    target = home / ".harness"
    link = _link_target(target)
    if link is not None:
        if _key(target.parent / link) == _key(source):
            link_directory(target, source)  # junction -> symlink, если право есть
            return
        raise ValueError(
            f"{target} is a link to {link}, not to {source}; move it aside after preparing its data and rerun"
        )
    if os.path.lexists(target):
        raise ValueError(
            f"{target} exists and is not a link to {source}; move it aside after preparing its data and rerun"
        )
    link_directory(target, source)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "config", "--local", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def remove_git_hooks(repo: Path, files: InstallFiles) -> bool:
    """Снять локальный core.hooksPath checkout (pre-push удалён); нет значения - no-op.

    Старые значения уходят в manifest бэкапа. Global/worktree и чужие репозитории не трогаются.
    """
    found = _git(repo, "--get-all", "core.hooksPath")
    if found.returncode == 1 and not found.stdout.strip():
        return False
    if found.returncode != 0:
        raise RuntimeError(f"git config --get-all core.hooksPath exited {found.returncode}")
    values = [line for line in found.stdout.splitlines() if line]
    files.record(str(repo), "git-config", key="core.hooksPath", values=values)
    removed = _git(repo, "--unset-all", "core.hooksPath")
    if removed.returncode != 0:
        raise RuntimeError(f"git config --unset-all core.hooksPath exited {removed.returncode}")
    return True
