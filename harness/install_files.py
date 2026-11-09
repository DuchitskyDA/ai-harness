"""Записи установщика: сравнение до записи, ленивый бэкап, ссылки без записи сквозь них.

Модуль не импортирует install: состояние одной установки живёт в экземпляре
InstallFiles, глобального HOME/BACKUP нет.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path


class UnsafePath(ValueError):
    """Цель записи уводит вне назначенного HOME или в checkout."""


def link_target(path: Path) -> str | None:
    """Цель symlink или junction; None - не ссылка. Без is_junction (Python 3.11)."""
    try:
        return os.readlink(path).removeprefix("\\\\?\\")
    except (OSError, ValueError):
        return None


def resolved_link(path: Path) -> Path | None:
    target = link_target(path)
    if target is None:
        return None
    return Path(os.path.realpath(path.parent / target))


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _tree(root: Path) -> dict[str, bytes | None]:
    """Относительный путь -> bytes файла (None для каталога); ссылки внутри не обходятся."""
    result: dict[str, bytes | None] = {}
    for current, dirs, names in os.walk(root):
        base = Path(current)
        for name in dirs:
            result[(base / name).relative_to(root).as_posix()] = None
        for name in names:
            result[(base / name).relative_to(root).as_posix()] = (base / name).read_bytes()
    return result


def same_tree(left: Path, right: Path) -> bool:
    if link_target(left) is not None or link_target(right) is not None:
        return False
    return left.is_dir() and right.is_dir() and _tree(left) == _tree(right)


def _remove(path: Path) -> None:
    if link_target(path) is not None:
        try:
            os.unlink(path)
        except (IsADirectoryError, PermissionError):
            os.rmdir(path)  # junction на Windows
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def make_link(link: Path, target: Path) -> None:
    """Каталог-ссылка: symlink; на Windows без права на symlink - junction."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        # shortcut: symlink требует режима разработчика; junction - запасная форма той же ссылки
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def _upgrade_junction(link: Path, target: Path) -> bool:
    """Junction -> symlink на ту же цель, если право на symlink есть."""
    probe = link.with_name(link.name + ".symlink")
    try:
        os.symlink(target, probe, target_is_directory=True)
    except OSError:
        return False
    os.rmdir(link)
    os.replace(probe, link)
    return True


class InstallFiles:
    def __init__(self, home: Path, repo: Path) -> None:
        self.home = Path(os.path.realpath(home))
        self.repo = Path(os.path.realpath(repo))
        self.backup_dir: Path | None = None
        self.operations: list[dict] = []
        self.changed = 0

    # --- проверки ---------------------------------------------------------------------
    def check(self, path: Path) -> Path:
        """Абсолютный путь с разрешённым родителем: внутри HOME и не в checkout."""
        absolute = Path(os.path.realpath(path.parent)) / path.name
        inside = absolute.is_relative_to(self.home) and absolute != self.home
        if not inside or absolute.is_relative_to(self.repo):
            raise UnsafePath(f"unsafe target {path} -> {absolute}")
        return absolute

    # --- бэкап ------------------------------------------------------------------------
    def _backup_root(self) -> Path:
        if self.backup_dir is None:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            self.backup_dir = self.home / ".codex" / "backups" / f"harness-install-{stamp}-{uuid.uuid4().hex[:8]}"
            self.backup_dir.mkdir(parents=True)
        return self.backup_dir

    def backup(self, path: Path) -> None:
        """Сохранить исходный объект: ссылку - как ссылку, файл - bytes и mode, каталог - копией."""
        path = self.check(path)
        target = link_target(path)
        if target is not None:
            self._backup_root()
            self.operations.append({"path": str(path), "kind": "link", "target": target})
            return
        if not path.exists():
            return
        destination = self._backup_root() / path.relative_to(self.home)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, destination, symlinks=True)
        else:
            shutil.copy2(path, destination)
        self.operations.append(
            {"path": str(path), "kind": "dir" if path.is_dir() else "file", "backup": str(destination)}
        )

    def record(self, path: str, kind: str, **data) -> None:
        """Операция без файла (git config): старые значения в manifest."""
        self._backup_root()
        self.operations.append({"path": path, "kind": kind, **data})
        self.changed += 1

    # --- записи -----------------------------------------------------------------------
    def write(self, path: Path, content: bytes) -> bool:
        path = self.check(path)
        if link_target(path) is None and path.is_file() and path.read_bytes() == content:
            return False
        self.backup(path)
        if link_target(path) is not None:
            _remove(path)  # запись не идёт сквозь ссылку: цель остаётся прежней
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.changed += 1
        return True

    def write_json(self, path: Path, data: object) -> bool:
        """Сравнить смысл JSON; при равенстве файл не сериализуется заново."""
        if link_target(path) is None and path.is_file():
            try:
                if json.loads(path.read_bytes().decode("utf-8-sig")) == data:
                    return False
            except ValueError:
                pass
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        return self.write(path, text.encode("utf-8"))

    def copy(self, source: Path, target: Path) -> bool:
        """Файл или каталог целиком; совпадающий - без изменений."""
        if not source.is_dir():
            return self.write(target, source.read_bytes())
        target = self.check(target)
        if same_tree(source, target):
            return False
        self.backup(target)
        _remove(target)
        shutil.copytree(source, target)
        self.changed += 1
        return True

    def remove(self, path: Path) -> bool:
        path = self.check(path)
        if link_target(path) is None and not path.exists():
            return False
        self.backup(path)
        _remove(path)
        self.changed += 1
        return True

    def replace(self, part: Path, target: Path) -> bool:
        """Атомарно поставить готовый .part на место target с бэкапом прежнего."""
        target = self.check(target)
        self.backup(target)
        os.replace(part, target)
        self.changed += 1
        return True

    def link_directory(self, link: Path, target: Path) -> bool:
        link = self.check(link)
        current = resolved_link(link)
        if current is not None and same_path(current, target):
            if link.is_symlink() or not _upgrade_junction(link, target):
                return False
            self.changed += 1
            return True
        if current is not None or link.exists():
            self.backup(link)
            _remove(link)
        make_link(link, target)
        self.changed += 1
        return True

    def finish(self) -> dict:
        if self.backup_dir is not None:
            manifest = {"schema": 1, "repo": str(self.repo), "operations": self.operations}
            (self.backup_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return {"changed": self.changed, "backup": str(self.backup_dir) if self.backup_dir else None}
