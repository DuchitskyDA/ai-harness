"""formatters и typecheck проекта: staged текст через stdin, typecheck по рабочей копии."""

import importlib
import json
from typing import Any

import pytest
from presets_testkit import FakeRepo, json_tool, run_main

PY = "X = 1\n"


def preset(tmp_path, monkeypatch, **fields):
    folder = tmp_path / "projects"
    folder.mkdir()
    (folder / "default.json").write_text(json.dumps({"code_extensions": [".py", ".ts"], **fields}), encoding="utf-8")
    monkeypatch.setattr(importlib.import_module("pre_commit_projects"), "PROJECTS", folder)


def repo_with(tmp_path, files, tools, disk=True):
    repo = FakeRepo(tmp_path / "work")
    for path, source in files.items():
        repo.stage(path, source)
        if disk:
            (repo.root / path).parent.mkdir(parents=True, exist_ok=True)
            (repo.root / path).write_text(source, encoding="utf-8", newline="")
    repo.root.mkdir(exist_ok=True)
    repo.tools.update({"node": json_tool([]), **tools})
    return repo


def check(repo) -> tuple[int, Any]:
    code, out, _ = run_main(importlib.import_module("pre_commit").main, ["check", "--cwd", str(repo.root)], repo)
    return code, (json.loads(out)["violations"] if out else None)


FMT = {"stdin_command": ["fmt", "--stdin-filename", "{file}"], "cwd": "src", "paths": ["**/*.py"]}


def test_formatter_reports_first_differing_line(tmp_path, monkeypatch):
    preset(tmp_path, monkeypatch, formatters=[FMT])
    repo = repo_with(
        tmp_path,
        {"src/a.py": "A=1\nB=2\n", "src/b.py": PY},
        {"fmt": lambda argv, cwd, stdin: (0, stdin.replace(b"B=2", b"B = 2"))},
    )
    code, found = check(repo)
    assert (code, [(v["path"], v["line"], v["rule"]) for v in found]) == (1, [("src/a.py", 2, "format")])
    assert [(a[-1], s) for a, _, s in repo.calls if a[0] == "fmt"] == [("a.py", b"A=1\nB=2\n"), ("b.py", PY.encode())]


def test_formatter_failure_exit_2(tmp_path, monkeypatch):
    preset(tmp_path, monkeypatch, formatters=[FMT])
    repo = repo_with(tmp_path, {"src/a.py": PY}, {"fmt": lambda argv, cwd, stdin: (2, b"")})
    assert check(repo) == (2, None)


def pyright_out(root):
    diags = [
        {
            "file": str(root / name),
            "severity": "error",
            "rule": "reportArgumentType",
            "message": "bad\nmore",
            "range": {"start": {"line": 4, "character": 0}},
        }
        for name in ("a.py", "b.py")
    ]
    return json.dumps({"generalDiagnostics": diags}).encode()


def test_pyright_only_staged_files(tmp_path, monkeypatch):
    preset(tmp_path, monkeypatch, typecheck=[{"command": ["pyright", "--outputjson"], "format": "pyright-json"}])
    repo = repo_with(tmp_path, {"a.py": PY}, {})
    repo.tools["pyright"] = lambda argv, cwd, stdin: (1, pyright_out(repo.root))
    code, found = check(repo)
    assert (code, found) == (1, [{"path": "a.py", "line": 5, "rule": "reportArgumentType", "message": "bad"}])


TSC = {"command": ["tsc", "--noEmit"], "cwd": "web", "format": "tsc"}


def test_tsc_only_staged_files(tmp_path, monkeypatch):
    preset(tmp_path, monkeypatch, typecheck=[TSC])
    out = b"src/a.ts(3,5): error TS2322: Type bad.\nsrc/b.ts(1,1): error TS2304: x\n"
    repo = repo_with(tmp_path, {"web/src/a.ts": "x\n"}, {"tsc": lambda argv, cwd, stdin: (2, out)})
    code, found = check(repo)
    assert (code, found) == (1, [{"path": "web/src/a.ts", "line": 3, "rule": "TS2322", "message": "Type bad."}])
    assert len(repo.argvs("tsc")) == 1


@pytest.mark.parametrize("tool", [lambda a, c, s: (2, b"error TS5083: no config\n"), lambda a, c, s: (3, b"")])
def test_tsc_failure_exit_2(tmp_path, monkeypatch, tool):
    preset(tmp_path, monkeypatch, typecheck=[TSC])
    assert check(repo_with(tmp_path, {"web/a.ts": "x\n"}, {"tsc": tool})) == (2, None)


def test_partial_stage_exit_2_without_run(tmp_path, monkeypatch):
    preset(tmp_path, monkeypatch, typecheck=[TSC])
    repo = repo_with(tmp_path, {"web/a.ts": "x\n"}, {"tsc": lambda a, c, s: (0, b"")})
    (repo.root / "web/a.ts").write_text("y\n", encoding="utf-8")
    assert check(repo) == (2, None)
    assert repo.argvs("tsc") == []
