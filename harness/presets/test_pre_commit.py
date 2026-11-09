"""PresetHook check: индекс через подменённый Git, настоящий Python check_source, Node подменён."""

import importlib
import importlib.util
import json
import subprocess
import sys
import types
from typing import Any

import pytest
from presets_testkit import FakeRepo, json_tool, run_main

BAD_PY = "def f(a, b, c, d, e, g):\n    return a\n"
GOOD_PY = "def f(a, b, c, d, e):\n    return a\n"
TS_VIOLATION = {"path": "web/a.ts", "line": 1, "rule": "harness/max-params", "message": "m"}


def load(name):
    assert importlib.util.find_spec(name) is not None, f"нет модуля {name}"
    return importlib.import_module(name)


@pytest.fixture
def main():
    return load("pre_commit").main


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return FakeRepo(root)


def check(main, repo, cwd=None) -> tuple[int, Any, str]:
    code, out, err = run_main(main, ["check", "--cwd", str(cwd or repo.root)], repo)
    return code, (json.loads(out) if out else None), err


def test_interface_present():
    assert callable(load("pre_commit").main)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["check"],
        ["check", "--cwd"],
        ["bogus", "--cwd", "."],
        ["check", "--cwd", ".", "extra"],
        ["check", "--path", "."],
    ],
)
def test_bad_arguments_exit_2(main, repo, argv):
    code, out, err = run_main(main, argv, repo)
    assert (code, out) == (2, "")
    assert err.startswith("presets: ")


def test_missing_directory_exit_2(main, repo):
    code, out, err = run_main(main, ["check", "--cwd", str(repo.root / "nope")], repo)
    assert (code, out) == (2, "")
    assert err.startswith("presets: ")


def test_not_git_exit_2(main, repo):
    repo.fail.add("rev-parse --show-toplevel")
    code, out, err = check(main, repo)
    assert (code, out) == (2, None)
    assert err.startswith("presets: ")


def test_empty_stage_is_clean_without_node(main, repo):
    repo.stage("README.md", "# x\n")
    repo.stage("old.py", BAD_PY, select=False)
    assert check(main, repo)[:2] == (0, {"checked": 0, "violations": []})
    assert repo.argvs("node") == []


def test_staged_debt_blocks_even_if_worktree_fixed(main, repo):
    (repo.root / "a.py").write_text(GOOD_PY, encoding="utf-8")
    repo.stage("a.py", "X = 1\n\n\n" + BAD_PY)
    code, result, _ = check(main, repo)
    assert (code, result["checked"]) == (1, 1)
    assert [(v["path"], v["line"], v["rule"]) for v in result["violations"]] == [("a.py", 4, "params")]
    assert not any("HEAD" in arg for argv, _, _ in repo.calls for arg in argv)


def test_staged_clean_passes_even_if_worktree_bad(main, repo):
    (repo.root / "a.py").write_text(BAD_PY, encoding="utf-8")
    repo.stage("a.py", GOOD_PY)
    assert check(main, repo)[:2] == (0, {"checked": 1, "violations": []})


def test_recheck_after_fixing_stage_reads_again(main, repo):
    repo.stage("a.py", BAD_PY)
    first = check(main, repo)[0]
    repo.entries, repo.selection = [], []
    repo.stage("a.py", GOOD_PY)
    assert (first, check(main, repo)[0]) == (1, 0)


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_symlink_and_gitlink_skipped_without_reading(main, repo, mode):
    repo.stage("link.py", BAD_PY, mode=mode)
    assert check(main, repo)[:2] == (0, {"checked": 0, "violations": []})
    assert [a for a in repo.argvs("git") if a[1] == "cat-file"] == []


def test_executable_file_checked(main, repo):
    repo.stage("run.py", BAD_PY, mode="100755")
    assert check(main, repo)[0] == 1


def test_conflict_exit_2_before_lint(main, repo):
    repo.stage("a.py", GOOD_PY)
    repo.stage("notes.md", "a", stage=2, select=False)
    code, out, _ = check(main, repo)
    assert (code, out) == (2, None)
    assert [a for a in repo.argvs("git") if a[1] == "cat-file"] == []


@pytest.mark.parametrize(
    "name",
    ["dir with space/a.py", "tab\there.py", "new\nline.py", "юникод/файл.py", "it's.py", "-dash.py", ":colon.py"],
)
def test_odd_names_keep_exact_path(main, repo, name):
    oid = repo.stage(name, BAD_PY)
    code, result, _ = check(main, repo)
    assert (code, [v["path"] for v in result["violations"]]) == (1, [name])
    assert ["git", "cat-file", "blob", oid] in repo.argvs("git")


def test_python_and_ts_together_sorted(main, repo):
    repo.stage("z.py", BAD_PY)
    repo.stage("web/a.ts", "export function f(a, b, c, d) { return a; }\n")
    repo.tools["node"] = json_tool([TS_VIOLATION])
    code, result, _ = check(main, repo)
    assert (code, result["checked"]) == (1, 2)
    assert [v["path"] for v in result["violations"]] == ["web/a.ts", "z.py"]
    assert len(repo.argvs("node")) == 1


def test_ts_fallback_gets_staged_sources_once(main, repo):
    repo.stage("a.ts", "const a = 1;\n")
    repo.stage("b.mts", "const b = 2;\n")
    repo.tools["node"] = json_tool([])
    assert check(main, repo)[:2] == (0, {"checked": 2, "violations": []})
    [(argv, _, stdin)] = [c for c in repo.calls if c[0][0] == "node"]
    assert argv[2:] == ["--cwd", str(repo.root)]
    assert json.loads(stdin) == {
        "files": [{"path": "a.ts", "source": "const a = 1;\n"}, {"path": "b.mts", "source": "const b = 2;\n"}]
    }


def test_python_parse_error_is_violation(main, repo):
    repo.stage("a.py", "def f(:\n")
    code, result, _ = check(main, repo)
    assert (code, [v["rule"] for v in result["violations"]]) == (1, ["parse-error"])


def test_bom_source_decoded(main, repo):
    repo.stage("a.py", "﻿" + GOOD_PY)
    assert check(main, repo)[:2] == (0, {"checked": 1, "violations": []})


def test_non_utf8_exit_2_without_content(main, repo):
    repo.stage("bad.py", b"SECRET = '\xff\xfe'\n")
    code, out, err = check(main, repo)
    assert (code, out) == (2, None)
    assert "bad.py" in err and "SECRET" not in err


@pytest.mark.parametrize("failing", ["diff --cached", "ls-files --stage", "cat-file blob"])
def test_git_failure_exit_2(main, repo, failing):
    repo.stage("a.py", GOOD_PY)
    repo.fail.add(failing)
    assert check(main, repo)[:2] == (2, None)


@pytest.mark.parametrize(
    "node",
    [
        json_tool([], code=2),
        json_tool([TS_VIOLATION], code=3),
        lambda argv, cwd, stdin: (0, b"not json"),
        lambda argv, cwd, stdin: (1, b'{"x": 1}'),
    ],
)
def test_node_failure_exit_2(main, repo, node):
    repo.stage("z.py", BAD_PY)
    repo.stage("a.ts", "const a = 1;\n")
    repo.tools["node"] = node
    assert check(main, repo)[:2] == (2, None)


def test_node_missing_exit_2(main, repo):
    repo.stage("a.ts", "const a = 1;\n")
    assert check(main, repo)[:2] == (2, None)


def test_index_changed_during_check(main, repo):
    repo.stage("a.py", GOOD_PY)
    repo.on_ls = lambda count: count == 2 and repo.entries.append(("100644", "f" * 40, 0, "b.py"))
    code, out, err = check(main, repo)
    assert (code, out) == (2, None)
    assert "индекс изменился во время проверки; повтори проверку" in err


def test_size_is_not_a_commit_gate(main, repo):
    repo.stage("big.py", "X = 1\n" * 401)
    repo.stage("test_big.py", "Y = 1\n" * 601)
    assert check(main, repo)[:2] == (0, {"checked": 2, "violations": []})


def test_size_limit_still_keeps_function_rules(main, repo):
    repo.stage("big.py", "X = 1\n" * 401 + BAD_PY)
    assert check(main, repo)[0] == 1


def test_second_repository_uses_its_own_root(main, repo, tmp_path):
    repo.stage("a.py", BAD_PY)
    other = FakeRepo(tmp_path / "other")
    other.root.mkdir()
    other.stage("b.py", BAD_PY)
    check(main, repo)
    assert [v["path"] for v in check(main, other)[1]["violations"]] == ["b.py"]
    assert {cwd for _, cwd, _ in other.calls} == {other.root}


def test_subdirectory_cwd_resolves_root(main, repo):
    (repo.root / "sub").mkdir()
    repo.stage("a.py", GOOD_PY)
    assert check(main, repo, repo.root / "sub")[:2] == (0, {"checked": 1, "violations": []})
    assert {cwd for _, cwd, _ in repo.calls[1:]} == {repo.root}


def test_python_fallback_ignores_foreign_code_check(main, repo, monkeypatch):
    monkeypatch.setitem(sys.modules, "code_check", types.ModuleType("code_check"))
    repo.stage("a.py", BAD_PY)
    assert check(main, repo)[0] == 1


def test_default_runner_timeout_and_env(monkeypatch, repo):
    hook = load("pre_commit")
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs)
        return repo(argv, kwargs["cwd"], kwargs.get("input"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_INDEX_FILE", "alt-index")
    repo.stage("a.py", GOOD_PY)
    assert run_main(hook.main, ["check", "--cwd", str(repo.root)], None)[0] == 0
    assert {(k["timeout"], k["shell"], "env" in k) for k in seen} == {(120, False, False)}
    assert hook.SUBPROCESS_TIMEOUT_S == 120


def test_default_runner_timeout_exit_2(monkeypatch, repo):
    hook = load("pre_commit")

    def fake_run(argv, **kwargs):
        if argv[0] == "node":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return repo(argv, kwargs["cwd"], kwargs.get("input"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    repo.stage("z.py", BAD_PY)
    repo.stage("a.ts", "const a = 1;\n")
    assert run_main(hook.main, ["check", "--cwd", str(repo.root)], None)[:2] == (2, "")


def test_rename_checks_new_path_only(main, repo):
    repo.stage("new.py", BAD_PY)
    result = check(main, repo)[1]
    assert [v["path"] for v in result["violations"]] == ["new.py"]
    assert ["git", "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACMT", "-z", "--"] in repo.argvs(
        "git"
    )


def test_unknown_blob_exit_2(main, repo):
    repo.entries.append(("100644", "0" * 40, 0, "a.py"))
    repo.selection.append("a.py")
    assert check(main, repo)[:2] == (2, None)
