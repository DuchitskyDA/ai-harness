"""pr_merged: слитый и закрытый PR - шаги после слияния и запись снята; открытый и ошибка хоста - тишина, запись остаётся."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(f"harness_hooks_{name}", HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load("pr_merged")
post_pr = load("post_pr")

SID = "0f3c-session"
MERGED, OPEN, CLOSED, BROKEN, WAVE = (f"https://github.com/o/r/pull/{n}" for n in (1, 2, 3, 4, 5))


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def host(url):
    if url == BROKEN:
        raise subprocess.CalledProcessError(1, "gh")
    return {
        MERGED: ("merged", "abc123", "main"),
        OPEN: ("open", "", "main"),
        CLOSED: ("closed", "", "main"),
        WAVE: ("merged", "w1sha", "pay-epic"),
    }[url]


def test_post_pr_remembers_link_once():
    event = {
        "session_id": SID,
        "tool_input": {"command": "gh pr create --title x"},
        "tool_response": {"stdout": f"{MERGED}\n"},
    }
    post_pr.handle(event)
    post_pr.handle(event)
    assert hook.load(SID) == [MERGED]


def test_merged_and_closed_reported_open_and_broken_kept():
    hook.save(SID, [MERGED, OPEN, CLOSED, BROKEN])

    out = hook.handle({"session_id": SID}, state=host)

    text = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert f"{MERGED} слит" in text and "abc123" in text and f"{CLOSED} закрыт" in text
    assert "git pull --ff-only" in text and "install.py --apply" in text and "ci_watch.py" in text
    assert hook.load(SID) == [OPEN, BROKEN]


def test_nothing_new_is_silent_and_last_link_removes_file():
    hook.save(SID, [OPEN])
    assert hook.handle({"session_id": SID}, state=host) is None
    hook.save(SID, [MERGED])
    hook.handle({"session_id": SID}, state=host)
    assert not (hook.state_dir() / f"{SID}.json").exists()


def test_no_session_or_bad_id_is_silent():
    assert hook.handle({}, state=host) is None
    hook.remember("../escape", MERGED)
    assert hook.handle({"session_id": "../escape"}, state=host) is None


def test_wave_merge_into_epic_branch_gives_wave_steps_only():
    hook.save(SID, [WAVE])

    text = hook.handle({"session_id": SID}, state=host)["hookSpecificOutput"]["additionalContext"]

    assert f"{WAVE} слит владельцем в ветку эпика pay-epic" in text and "w1sha" in text
    assert "в копии ветки pay-epic git pull --ff-only" in text
    assert "install.py --apply" not in text and "ci_watch.py --sha" not in text
    assert hook.load(SID) == []


def test_wave_and_main_merges_give_both_steps():
    hook.save(SID, [WAVE, MERGED])

    text = hook.handle({"session_id": SID}, state=host)["hookSpecificOutput"]["additionalContext"]

    assert "после слияния в основную" in text and "install.py --apply" in text
    assert text.index("после слияния волны: в копии ветки pay-epic") < text.index("после слияния в основную")


@pytest.mark.parametrize("base", ["main", "master", "develop", ""])
def test_main_like_or_unknown_base_is_main_merge(base):
    hook.save(SID, [MERGED])

    out = hook.handle({"session_id": SID}, state=lambda url: ("merged", "abc123", base))

    text = out["hookSpecificOutput"]["additionalContext"]
    assert "install.py --apply" in text and "ветку эпика" not in text


def test_closed_only_gives_no_steps():
    hook.save(SID, [CLOSED])

    text = hook.handle({"session_id": SID}, state=host)["hookSpecificOutput"]["additionalContext"]

    assert f"{CLOSED} закрыт" in text and "git pull" not in text


GITLAB = "https://git.example.ru/team/app/-/merge_requests/7"


GH_FIELDS = "state,mergeCommit,baseRefName"
GL_ENDPOINT = "projects/team%2Fapp/merge_requests/7"


@pytest.mark.parametrize(
    ("url", "asks", "reply", "expected"),
    [
        (
            MERGED,
            GH_FIELDS,
            '{"state": "MERGED", "mergeCommit": {"oid": "abc"}, "baseRefName": "pay-epic"}',
            ("merged", "abc", "pay-epic"),
        ),
        (MERGED, GH_FIELDS, '{"state": "OPEN", "mergeCommit": null, "baseRefName": "main"}', ("open", "", "main")),
        (
            GITLAB,
            GL_ENDPOINT,
            '{"state": "merged", "squash_commit_sha": "s1", "target_branch": "pay-epic"}',
            ("merged", "s1", "pay-epic"),
        ),
        (
            GITLAB,
            GL_ENDPOINT,
            '{"state": "opened", "merge_commit_sha": null, "target_branch": "develop"}',
            ("open", "", "develop"),
        ),
    ],
)
def test_host_state_reads_base_branch(monkeypatch, url, asks, reply, expected):
    calls = []
    monkeypatch.setattr(hook, "_run", lambda cmd: (calls.append(cmd), reply)[1])

    assert hook.host_state(url) == expected
    assert asks in calls[0]


def test_gitlab_wave_merge_reaches_wave_steps(monkeypatch):
    reply = '{"state": "merged", "squash_commit_sha": "s1", "target_branch": "pay-epic"}'
    monkeypatch.setattr(hook, "_run", lambda cmd: reply)
    hook.save(SID, [GITLAB])

    text = hook.handle({"session_id": SID})["hookSpecificOutput"]["additionalContext"]

    assert "ветку эпика pay-epic" in text and "install.py --apply" not in text
