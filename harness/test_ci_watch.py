"""ci_watch: ожидание прогонов коммита и итог на любом исходе. gh подменён списком ответов, часы - счётчиком."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("harness_ci_watch", Path(__file__).resolve().parent / "ci_watch.py")
assert _spec is not None and _spec.loader is not None
ci = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ci  # dataclass ищет свой модуль в sys.modules
_spec.loader.exec_module(ci)


def run(status="completed", conclusion: str | None = "success", rid=1, name="tests"):
    return {
        "databaseId": rid,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "url": f"https://x/{rid}",
    }


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def watch(answers, appear=180, timeout=3000):
    answers = list(answers)
    clock = Clock()
    lines = []
    code = ci.watch(
        lambda: answers.pop(0) if len(answers) > 1 else answers[0],
        lambda rid: f"log {rid}",
        lines.append,
        clock,
        ci.Limits(appear=appear, timeout=timeout),
    )
    return code, lines


def test_success_after_in_progress():
    code, lines = watch([[], [run("in_progress", None)], [run()]])
    assert code == 0 and lines[-1].startswith("CI success")


def test_failure_prints_failed_log_and_exits_1():
    code, lines = watch([[run(conclusion="failure", rid=7), run(rid=8, name="lint")]])
    assert code == 1
    assert lines[-1].startswith("CI failure") and "log 7" in lines and "log 8" not in lines


def test_cancelled_counts_as_failure():
    assert watch([[run(conclusion="cancelled")]])[0] == 1


def test_waits_for_all_runs_of_commit():
    code, lines = watch([[run(), run("queued", None, rid=2)], [run(), run(rid=2)]])
    assert code == 0 and lines[-1].startswith("CI success")


def test_no_runs_within_appear_window():
    code, lines = watch([[]], appear=60)
    assert code == 3 and lines[-1].startswith("CI none")


def test_timeout():
    code, lines = watch([[run("in_progress", None)]], timeout=100)
    assert code == 124 and lines[-1].startswith("CI timeout")


def test_gh_error_is_reported_not_hidden():
    def broken():
        raise ci.GhError("gh: not logged in")

    lines = []
    assert ci.watch(broken, str, lines.append, Clock(), ci.Limits(appear=60, timeout=100)) == 3
    assert lines[-1].startswith("CI error") and "not logged in" in lines[-1]


def test_failed_log_ends_at_last_error_not_runner_cleanup():
    lines = ["setup"] * 100 + [
        "FAILED test_x",
        "##[error]Process completed with exit code 1.",
        "Cleaning up",
    ]
    tail = ci.tail_to_error(lines).splitlines()
    assert tail[-1].startswith("##[error]") and "FAILED test_x" in tail and len(tail) == ci.LOG_TAIL


def test_short_sha_resolves_to_full_hash():
    full = "2f2f961" + "a" * 33
    calls = []

    def git(*args):
        calls.append(args)
        return full + "\n"

    assert ci.resolve_sha("2f2f961", git) == full
    assert calls == [("rev-parse", "--verify", "2f2f961^{commit}")]


def test_missing_sha_resolves_head():
    calls = []
    ci.resolve_sha(None, lambda *args: calls.append(args) or "f" * 40)
    assert calls == [("rev-parse", "--verify", "HEAD^{commit}")]


def test_main_passes_full_hash_to_gh(monkeypatch, capsys):
    full = "2f2f961" + "b" * 33
    gh_calls = []
    monkeypatch.setattr(ci, "_git", lambda cwd, *args: full)
    monkeypatch.setattr(ci, "_gh", lambda cwd, *args: gh_calls.append(args) or json.dumps([run()]))
    assert ci.main(["--cwd", ".", "--sha", "2f2f961"]) == 0
    assert gh_calls[0][gh_calls[0].index("--commit") + 1] == full


def test_main_unknown_rev_is_error_without_gh(monkeypatch, capsys):
    def bad_git(cwd, *args):
        raise subprocess.CalledProcessError(128, ["git", *args])

    monkeypatch.setattr(ci, "_git", bad_git)
    monkeypatch.setattr(ci, "_gh", lambda *args: pytest.fail("gh не нужен"))
    assert ci.main(["--cwd", ".", "--sha", "nosuchrev"]) == 3
    assert capsys.readouterr().out.splitlines()[-1].startswith("CI error: git rev-parse nosuchrev")
