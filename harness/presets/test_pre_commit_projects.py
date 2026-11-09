"""ProjectPresets: поставляемые configs, выбор проекта, слияние и проектный dispatch через main.

Проектные configs в поставку не входят: механизм выбора и dispatch проверяется на примерах
EXAMPLES, которые фикстура examples кладёт рядом с поставляемыми default.json и harness.json.
"""

import hashlib
import importlib
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from presets_testkit import FakeRepo, json_tool, run_main

HERE = Path(__file__).parent
BAD_PY = "def f(a, b, c, d, e, g):\n    return a\n"
TOOLS = "tools/ui"
# sha256 канонического JSON (sort_keys) поставляемых configs: правка config - осознанная правка хэша.
BASE_SHA = {
    "default": "e9ef521ad1c7fdafc5c383c8425ed844584472ace6ebdc7a3680ecfa35835422",
    "harness": "c39868e41ec4a0ea74e9bde2ef16bcb9183bfc7347f6a562544f7165ff5fd306",
}
PY_LINT = {
    "command": ["{python}", "-X", "utf8", "-B", "tools/lint/lint.py", "{files}"],
    "stdin_command": ["{python}", "-X", "utf8", "-B", "tools/lint/lint.py", "--stdin-filename", "{file}"],
    "cwd": ".",
    "format": "harness-json",
    "hints": "tools/lint/hints.json",
}
# Примеры проектных configs: python-lint без paths, oxlint с layout на весь проект,
# python-lint по paths плюс oxlint с layout на одном каталоге.
EXAMPLES = {
    "my-py": {
        "match": {"remotes": ["github.com/my-user/my-py"], "dirs": ["my-py"]},
        "test_patterns": ["tests/**", "**/test_*.py", "**/conftest.py"],
        "lint": PY_LINT,
    },
    "my-app": {
        "match": {"remotes": ["team/group/my-app"], "dirs": ["my-app"]},
        "exclude": ["apps/api/src/generated/**", "**/*.gen.ts"],
        "lint": {
            "format": "oxlint-json",
            "layout": [
                {"dir": "apps/web", "preset": "reactFsd"},
                {"dir": "apps/api", "preset": "backend"},
                {"dir": "packages", "preset": "base"},
            ],
            "hints": "harness/presets/ts/hints.json",
        },
    },
    "my-tools": {
        "match": {"dirs": ["my-tools"]},
        "lint": [
            {**PY_LINT, "paths": ["**/*.py", "**/*.pyi"]},
            {
                "format": "oxlint-json",
                "layout": [{"dir": TOOLS, "preset": "base", "options": {"testFiles": ["**/*.{test,spec}.ts"]}}],
                "hints": "harness/presets/ts/hints.json",
                "paths": [f"{TOOLS}/**"],
            },
        ],
    },
}


def load(name):
    assert importlib.util.find_spec(name) is not None, f"нет модуля {name}"
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def no_gates(monkeypatch):
    """Здесь проверяется lint; formatters и typecheck - в test_pre_commit_gates.py."""
    monkeypatch.setattr(load("pre_commit_checks"), "check_gates", lambda *args: [])


@pytest.fixture
def projects():
    return load("pre_commit_projects")


@pytest.fixture
def examples(tmp_path, monkeypatch):
    """Поставляемые default.json и harness.json плюс примеры EXAMPLES в отдельном каталоге."""
    folder = tmp_path / "example-projects"
    folder.mkdir()
    [shutil.copy2(HERE / "projects" / f"{name}.json", folder) for name in BASE_SHA]
    [(folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8") for name, data in EXAMPLES.items()]
    monkeypatch.setattr(load("pre_commit_projects"), "PROJECTS", folder)
    return folder


@pytest.fixture
def main():
    return load("pre_commit").main


def canonical(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    [data.pop(key, None) for key in ("formatters", "typecheck")]  # добавлены после переноса
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def test_interface_present():
    assert callable(load("pre_commit_projects").load_project)


@pytest.mark.parametrize("name", sorted(BASE_SHA))
def test_configs_carried_structurally(name):
    assert canonical(HERE / "projects" / f"{name}.json") == BASE_SHA[name]


def test_only_shared_configs_shipped():
    assert sorted(p.stem for p in (HERE / "projects").glob("*.json")) == sorted(BASE_SHA)


@pytest.mark.parametrize(
    ("remote", "folder", "expected"),
    [
        ("ssh://git@github.com/my-user/my-py.git", "x", "my-py"),
        ("git@github.com:my-user/my-py.git", "x", "default"),
        ("https://GITHUB.com/MY-USER/my-py/", "x", "my-py"),
        ("", "My-Py", "my-py"),
        ("https://github.com/my-user/ai.git", "zzz", "harness"),
        ("", "claude-config-repo", "harness"),
        ("https://gitlab.example/team/group/my-app.git", "", "my-app"),
        ("https://example.com/other.git", "unknown", "default"),
    ],
)
def test_project_match(projects, examples, remote, folder, expected):
    assert projects.load_project(projects.RepoInfo(remote, folder)).name == expected


def test_first_match_by_name_wins(projects, tmp_path):
    (tmp_path / "default.json").write_text('{"lint": null}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"match": {"dirs": ["x"]}}', encoding="utf-8")
    (tmp_path / "a.json").write_text('{"match": {"dirs": ["x"]}}', encoding="utf-8")
    assert projects.load_project(projects.RepoInfo("", "x"), folder=tmp_path).name == "a"


def test_merge_harness(projects):
    preset = projects.load_project(projects.RepoInfo("", "claude-config-repo"))
    assert dict(preset.limits) == {"code_lines": 400, "test_lines": 600, "function_lines": 80}
    assert preset.exclude[0] == "**/node_modules/**" and "harness/staging/**" in preset.exclude
    assert len(preset.exclude) == len(set(preset.exclude))
    assert [lint.paths for lint in preset.lints] == [("**/*.py", "**/*.pyi")]
    assert ".py" in preset.code_extensions


def test_merge_replaces_lists_and_default_has_no_lint(projects, examples):
    example = projects.load_project(projects.RepoInfo("", "my-py"))
    default = projects.load_project(projects.RepoInfo("", "nothing"))
    assert example.test_patterns[0] == "tests/**" and len(example.lints) == 1
    assert (default.name, default.lints) == ("default", ())


@pytest.mark.parametrize(
    ("files", "field"),
    [
        ({"default.json": "{bad"}, "default.json"),
        ({"default.json": '{"limits": []}'}, "limits"),
        ({"default.json": '{"lint": {"command": ["x"], "stdin_command": ["x"]}}'}, "lint"),
        ({"default.json": '{"exclude": [1]}'}, "exclude"),
        ({"default.json": '{"formatters": [{"stdin_command": ["fmt"]}]}'}, "formatters"),
        ({"default.json": '{"typecheck": [{"command": ["tsc"], "format": "mypy"}]}'}, "typecheck"),
        ({"default.json": '{"typecheck": {"command": ["tsc"]}}'}, "typecheck"),
        ({"default.json": "{}", "p.json": '{"match": {"dirs": ["x"]}, "lint": 5}'}, "p.json"),
        ({}, "default.json"),
    ],
)
def test_invalid_config_raises_with_name(projects, tmp_path, files, field):
    [(tmp_path / name).write_text(text, encoding="utf-8") for name, text in files.items()]
    with pytest.raises(ValueError, match=field.replace(".", r"\.")):
        projects.load_project(projects.RepoInfo("secret-remote", "x"), folder=tmp_path)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/*.py", "a.py", True),
        ("**/*.py", "x/y/a.py", True),
        ("*.py", "x/a.py", False),
        ("a/?.py", "a/b.py", True),
        ("a/?.py", "a/bc.py", False),
        ("**/*.PY", "a.py", False),
        (f"{TOOLS}/**", f"{TOOLS}/src/a.ts", True),
        ("tests/**", "src/tests/a.py", False),
    ],
)
def test_path_matches(projects, pattern, path, expected):
    assert projects.path_matches(pattern, path) is expected


@pytest.mark.parametrize(
    ("cwd", "paths", "rel", "expected"),
    [
        (".", (), "a/b.py", True),
        ("web", (), "web/a.ts", True),
        ("web", (), "api/a.ts", False),
        (".", ("**/*.py",), "a.ts", False),
        ("./web/", ("src/**",), "web/src/a.ts", False),
    ],
)
def test_covers(projects, cwd, paths, rel, expected):
    lint = projects.LintSpec(("x", "{files}"), ("x", "--stdin-filename", "{file}"), cwd, "harness-json", "", paths)
    assert projects.covers(lint, rel) is expected


def staged(tmp_path, folder, files, tools):
    repo = FakeRepo(tmp_path / folder)
    repo.root.mkdir()
    [repo.stage(path, source) for path, source in files.items()]
    repo.tools.update(tools)
    return repo


def checked(main, repo) -> tuple[int, Any, str]:
    code, out, err = run_main(main, ["check", "--cwd", str(repo.root)], repo)
    return code, (json.loads(out) if out else None), err


def echo_finding(argv, cwd, stdin):
    return 1, json.dumps([{"path": argv[-1], "line": 0, "rule": "params", "message": "m\nmore"}]).encode()


def test_harness_python_lint_per_file_on_staged_bytes(main, tmp_path):
    repo = staged(tmp_path, "claude-config-repo", {"b.py": "X = 1\n", "a.py": BAD_PY}, {"python": echo_finding})
    code, result, _ = checked(main, repo)
    assert (code, result["checked"]) == (1, 2)
    assert result["violations"][0] == {"path": "a.py", "line": 1, "rule": "params", "message": "m"}
    assert repo.argvs("python")[1] == [
        "python",
        "-X",
        "utf8",
        "-B",
        "harness/presets/python/code_check.py",
        "--config",
        "harness/code_check.json",
        "--stdin-filename",
        "a.py",
    ]
    assert [c[2] for c in repo.calls if c[0][0] == "python"] == [b"X = 1\n", BAD_PY.encode()]


def test_project_oxlint_batch_one_call(main, tmp_path, examples):
    files = {f"{TOOLS}/a.ts": "const a = 1;\n", f"{TOOLS}/b.tsx": "const b = 2;\n"}
    repo = staged(tmp_path, "my-tools", files, {"node": json_tool([])})
    assert checked(main, repo)[:2] == (0, {"checked": 2, "violations": []})
    [(argv, _, stdin)] = [c for c in repo.calls if c[0][0] == "node"]
    assert argv[2:4] == ["--cwd", str(repo.root)]
    assert argv[4] == "--layout"
    assert [(item["dir"], item["preset"]) for item in json.loads(argv[5])] == [(TOOLS, "base")]
    assert [f["path"] for f in json.loads(stdin or b"")["files"]] == sorted(files)


def test_project_oxlint_and_fallback_are_separate(main, tmp_path, examples):
    finding = [{"path": "tools/a.ts", "line": 2, "rule": "harness/max-params", "message": "m"}]
    repo = staged(tmp_path, "my-app", {"tools/a.ts": "x\n", "README.md": "x"}, {"node": json_tool(finding)})
    code, result, _ = checked(main, repo)
    assert (code, result["violations"]) == (1, finding)
    assert len(repo.argvs("node")) == 1 and "--layout" in repo.argvs("node")[0]


def test_project_lint_without_paths_covers_all_files(main, tmp_path, examples):
    repo = staged(
        tmp_path,
        "my-py",
        {"src/a.py": BAD_PY, "frontend/a.ts": "x\n"},
        {"python": json_tool([]), "node": json_tool([])},
    )
    assert checked(main, repo)[:2] == (0, {"checked": 2, "violations": []})
    assert [a[-1] for a in repo.argvs("python")] == ["src/a.py", "frontend/a.ts"]
    assert repo.argvs("node") == []


def test_uncovered_files_get_fallback(main, tmp_path):
    repo = staged(tmp_path, "claude-config-repo", {"web/a.ts": "x\n"}, {"node": json_tool([])})
    assert checked(main, repo)[:2] == (0, {"checked": 1, "violations": []})
    assert "--layout" not in repo.argvs("node")[0]


def test_python_projects_use_their_stdin_command(main, tmp_path, examples):
    repo = staged(tmp_path, "my-py", {"a.py": "X = 1\n"}, {"python": json_tool([])})
    assert checked(main, repo)[0] == 0
    assert repo.argvs("python")[0][-2:] == ["--stdin-filename", "a.py"]


def test_excluded_files_not_read(main, tmp_path, examples):
    repo = staged(tmp_path, "my-app", {"apps/api/src/generated/x.ts": "x", "a.d.ts": "x"}, {})
    assert checked(main, repo)[:2] == (0, {"checked": 0, "violations": []})
    assert [a for a in repo.argvs("git") if a[1] == "cat-file"] == []


@pytest.mark.parametrize(
    "tool",
    [
        json_tool([], code=2),
        json_tool([], code=3),
        lambda argv, cwd, stdin: (0, b"{"),
        json_tool([{"path": "../evil.py", "line": 1, "rule": "r", "message": "m"}]),
        json_tool([{"path": "other.py", "line": 1, "rule": "r", "message": "m"}]),
    ],
)
def test_project_lint_failure_exit_2(main, tmp_path, examples, tool):
    repo = staged(tmp_path, "my-py", {"a.py": "X = 1\n"}, {"python": tool})
    code, result, err = checked(main, repo)
    assert (code, result) == (2, None)
    assert err.startswith("presets: ")


def test_missing_project_linter_not_masked(main, tmp_path, examples):
    repo = staged(tmp_path, "my-py", {"a.py": BAD_PY}, {})
    assert checked(main, repo)[:2] == (2, None)


@pytest.mark.parametrize(
    "lint",
    [
        {"format": "oxlint-json"},
        {"format": "oxlint-json", "layout": []},
        {"format": "oxlint-json", "layout": [{"dir": "apps", "preset": "eslint"}]},
        {"format": "oxlint-json", "layout": [{"dir": "", "preset": "base"}]},
        {"format": "oxlint-json", "layout": [{"dir": "apps", "preset": "base", "extra": 1}]},
        {"format": "oxlint-json", "layout": [{"dir": "apps", "preset": "base", "ignores": "x"}]},
    ],
)
def test_bad_oxlint_layout_exit_2(main, tmp_path, monkeypatch, lint):
    folder = tmp_path / "projects"
    folder.mkdir()
    (folder / "default.json").write_text(json.dumps({"code_extensions": [".ts"], "lint": lint}), encoding="utf-8")
    monkeypatch.setattr(load("pre_commit_projects"), "PROJECTS", folder)
    repo = staged(tmp_path, "any", {"a.ts": "x"}, {"node": json_tool([])})
    assert checked(main, repo)[:2] == (2, None)
    assert repo.argvs("node") == []


def test_lint_cwd_outside_root_exit_2(main, tmp_path, monkeypatch):
    folder = tmp_path / "projects"
    folder.mkdir()
    (folder / "default.json").write_text(
        json.dumps(
            {
                "code_extensions": [".py"],
                "lint": {
                    "command": ["{python}", "l.py", "{files}"],
                    "cwd": "..",
                    "stdin_command": ["{python}", "l.py", "--stdin-filename", "{file}"],
                    "format": "harness-json",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(load("pre_commit_projects"), "PROJECTS", folder)
    repo = staged(tmp_path, "any", {"a.py": "X = 1\n"}, {"python": json_tool([])})
    assert checked(main, repo)[:2] == (2, None)
