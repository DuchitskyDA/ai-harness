"""post_push: подсказка на успешный git push в репозитории с GitHub Actions, тишина в остальном."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("harness_hooks_post_push", HERE / "post_push.py")
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    return tmp_path


def event(command, cwd, tool="Bash"):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push -q origin main",
        "git commit -m x && git push -q 2>&1 | tail -1",
    ],
)
def test_push_gives_watch_command(repo, command):
    out = hook.handle(event(command, repo), toplevel=lambda d: d)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "ci_watch.py" in text and f'--cwd "{repo}"' in text and "run_in_background" in text


@pytest.mark.parametrize("template", ['cd "{repo}" && git push', "git -C {repo} push"])
def test_cd_and_git_c_target_other_repo(repo, tmp_path_factory, template):
    other = tmp_path_factory.mktemp("elsewhere")
    out = hook.handle(event(template.format(repo=repo), other), toplevel=lambda d: d)
    assert f'--cwd "{repo}"' in out["hookSpecificOutput"]["additionalContext"]


def test_powershell_push(repo):
    assert hook.handle(event("git push", repo, tool="PowerShell"), toplevel=lambda d: d)


@pytest.mark.parametrize(
    "command",
    ["git status", "git push --dry-run", "echo git push", "git log --grep push"],
)
def test_not_a_push(repo, command):
    assert hook.handle(event(command, repo), toplevel=lambda d: d) is None


def test_repo_without_workflows_is_silent(tmp_path):
    assert hook.handle(event("git push", tmp_path), toplevel=lambda d: d) is None


def test_not_a_git_repo_is_silent(repo):
    assert hook.handle(event("git push", repo), toplevel=lambda d: None) is None
