"""post_pr: на создание и правку PR или MR - команда check со ссылкой из вывода, тишина в остальном."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("harness_hooks_post_pr", HERE / "post_pr.py")
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def event(command, stdout=""):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": ""},
    }


@pytest.mark.parametrize(
    ("command", "stdout", "url"),
    [
        (
            "gh pr create --title x --body-file b.md",
            "https://github.com/o/r/pull/15\n",
            "https://github.com/o/r/pull/15",
        ),
        (
            "cd r && gh pr edit 15 --body-file b.md | tail -1",
            "https://github.com/o/r/pull/15",
            "https://github.com/o/r/pull/15",
        ),
        (
            "glab mr create --target-branch develop",
            "!9 x\nhttps://git.example.com/a/b/-/merge_requests/9\n",
            "https://git.example.com/a/b/-/merge_requests/9",
        ),
        ("glab mr update 9 --description x", "", "<ссылка на PR или MR>"),
    ],
)
def test_pr_commands_get_check(command, stdout, url):
    out = hook.handle(event(command, stdout))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert f"check {url}" in text and "upload" in text


@pytest.mark.parametrize("command", ["gh pr view 15", "echo gh pr create", "git push", "gh pr list"])
def test_other_commands_silent(command):
    assert hook.handle(event(command)) is None
