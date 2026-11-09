"""Подмена транспорта PresetHook для тестов: ответы Git/Node/Python по argv, без подпроцессов."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

Tool = Callable[[list[str], Path, bytes | None], tuple[int, bytes]]


def done(argv, code=0, out=b""):
    return subprocess.CompletedProcess(list(argv), code, out, b"")


def oid_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class FakeRepo:
    """Индекс и ответы Git одного репозитория; calls - фактические (argv, cwd, stdin)."""

    def __init__(self, root: Path, *, remote: str = "", common: Path | None = None):
        self.root, self.remote = root, remote
        self.common = common or root / ".git"
        self.hook = self.common / "hooks" / "pre-commit"
        self.entries: list[tuple[str, str, int, str]] = []
        self.selection: list[str] = []
        self.blobs: dict[str, bytes] = {}
        self.tracked: set[str] = set()
        self.tools: dict[str, Tool] = {}
        self.fail: set[str] = set()
        self.on_ls: Callable[[int], None] | None = None
        self.calls: list[tuple[list[str], Path, bytes | None]] = []

    def stage(self, path: str, source: str | bytes, *, mode="100644", stage=0, select=True):
        data = source.encode("utf-8") if isinstance(source, str) else source
        oid = oid_of(data)
        self.blobs[oid] = data
        self.entries.append((mode, oid, stage, path))
        if select:
            self.selection.append(path)
        return oid

    def __call__(self, argv, cwd, stdin):
        argv = list(argv)
        self.calls.append((argv, Path(cwd), stdin))
        if argv[0] != "git":
            if argv[0] not in self.tools:
                raise FileNotFoundError(argv[0])
            code, out = self.tools[argv[0]](argv, Path(cwd), stdin)
            return done(argv, code, out)
        return self._git(argv[1:])

    def _git(self, args):
        key = " ".join(args[:2])
        if key in self.fail:
            return done(["git", *args], 128)
        handler = {
            "rev-parse --show-toplevel": lambda: self._text(self.root),
            "rev-parse --path-format=absolute": lambda: self._abs(args[2:]),
            "config --get": lambda: done(args, 0 if self.remote else 1, self.remote.encode() + b"\n"),
            "diff --cached": lambda: done(args, 0, b"".join(p.encode() + b"\0" for p in self.selection)),
            "ls-files --stage": self._ls_stage,
            "ls-files -z": lambda: self._tracked(args[-1]),
            "cat-file blob": lambda: done(args, 0 if args[2] in self.blobs else 128,
                                          self.blobs.get(args[2], b"")),
        }[key]
        return handler()

    def _text(self, path: Path):
        return done([], 0, path.as_posix().encode("utf-8") + b"\n")

    def _abs(self, rest):
        return self._text(self.common if rest == ["--git-common-dir"] else self.hook)

    def _ls_stage(self):
        count = sum(1 for argv, _, _ in self.calls if argv[1:3] == ["ls-files", "--stage"])
        if self.on_ls is not None:
            self.on_ls(count)
        rows = (f"{m} {o} {s}\t{p}".encode() + b"\0" for m, o, s, p in self.entries)
        return done([], 0, b"".join(rows))

    def _tracked(self, spec: str):
        rel = spec.removeprefix(":(literal)")
        return done([], 0, rel.encode() + b"\0" if rel in self.tracked else b"")

    def argvs(self, program: str) -> list[list[str]]:
        return [argv for argv, _, _ in self.calls if argv[0] == program]


def json_tool(result, code=None) -> Tool:
    """Инструмент, отвечающий готовым JSON-массивом (код 1, если нарушения есть)."""
    def tool(argv, cwd, stdin):
        return (code if code is not None else (1 if result else 0)), json.dumps(result).encode()
    return tool


def run_main(main, argv, repo) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, run=repo, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()
