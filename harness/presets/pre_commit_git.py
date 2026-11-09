"""Чтение Git для PresetHook: корень, RepoInfo, снимок staged blob-ов и его сверка.

Git только argv-массивами через Runner; stdout - bytes, NUL не делится по строкам.
Источник текста - blob индекса (stage 0), не рабочий файл и не HEAD. Ошибки - ValueError/OSError.
"""
from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pre_commit_projects import ProjectPreset, RepoInfo, covers, path_matches

Runner = Callable[[Sequence[str], Path, bytes | None], subprocess.CompletedProcess[bytes]]
SELECTION = ("git", "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACMT", "-z", "--")
ENTRIES = ("git", "ls-files", "--stage", "-z")
REGULAR = ("100644", "100755")
PY_SUFFIXES = (".py", ".pyi")
TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")


@dataclasses.dataclass(frozen=True)
class StagedFile:
    path: str
    oid: str
    mode: str
    source: str


@dataclasses.dataclass(frozen=True)
class IndexSnapshot:
    files: tuple[StagedFile, ...]
    selection: bytes
    entries: bytes


def git(run: Runner, root: Path, *args: str) -> bytes:
    done = run(["git", *args], root, None)
    if done.returncode != 0:
        raise ValueError(f"git {' '.join(args[:2])} завершился с кодом {done.returncode}")
    return done.stdout


def git_path(run: Runner, cwd: Path, *args: str) -> Path:
    """Путь из вывода git rev-parse (одна строка, UTF-8)."""
    text = git(run, cwd, "rev-parse", *args).decode("utf-8").rstrip("\n")
    if not text:
        raise ValueError(f"git rev-parse {args[-1]}: пустой ответ")
    return Path(text)


def repo_info(root: Path, common: Path, run: Runner) -> RepoInfo:
    """remote origin (только для match) и имя основной копии по git-common-dir."""
    done = run(["git", "config", "--get", "remote.origin.url"], root, None)
    if done.returncode not in (0, 1):
        raise ValueError(f"git config завершился с кодом {done.returncode}")
    remote = done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""
    name = common.parent.name if common.name == ".git" else root.name
    return RepoInfo(remote, name)


def _stage0(entries: bytes) -> dict[str, tuple[str, str]]:
    """path -> (mode, oid) записей индекса; конфликт (stage != 0) - ValueError."""
    found = {}
    for record in filter(None, entries.split(b"\0")):
        meta, _, raw = record.partition(b"\t")
        mode, oid, stage = meta.decode("ascii").split(" ")
        if stage != "0":
            raise ValueError("в индексе неразрешённый конфликт слияния")
        found[_decode_path(raw)] = (mode, oid)
    return found


def _decode_path(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("путь в индексе не в UTF-8") from error


def eligible(path: str, preset: ProjectPreset) -> bool:
    """Проверяется ли файл: не исключён, язык проекта, есть проектный lint или fallback."""
    if any(path_matches(p, path) for p in preset.exclude):
        return False
    lower = path.lower()
    if not lower.endswith(tuple(e.lower() for e in preset.code_extensions)):
        return False
    return any(covers(lint, path) for lint in preset.lints) or lower.endswith(PY_SUFFIXES + TS_SUFFIXES)


def _blob(run: Runner, root: Path, path: str, oid: str) -> str:
    data = git(run, root, "cat-file", "blob", oid)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{path}: staged текст не в UTF-8") from error


def read_snapshot(root: Path, run: Runner, *, preset: ProjectPreset) -> IndexSnapshot:
    selection = git(run, root, *SELECTION[1:])
    entries = git(run, root, *ENTRIES[1:])
    stage0 = _stage0(entries)
    files = []
    for path in dict.fromkeys(_decode_path(raw) for raw in filter(None, selection.split(b"\0"))):
        mode, oid = stage0.get(path, ("", ""))
        if mode in REGULAR and eligible(path, preset):
            files.append(StagedFile(path, oid, mode, _blob(run, root, path, oid)))
    return IndexSnapshot(tuple(files), selection, entries)


def assert_unchanged(root: Path, snapshot: IndexSnapshot, run: Runner) -> None:
    if (git(run, root, *SELECTION[1:]), git(run, root, *ENTRIES[1:])) != \
            (snapshot.selection, snapshot.entries):
        raise ValueError("индекс изменился во время проверки; повтори проверку")
