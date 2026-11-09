"""Слежка за GitHub Actions коммита: ждёт появления прогонов, затем их конца, и выходит на любом исходе.

python -X utf8 -B ~/.harness/ci_watch.py --cwd <репо> [--sha <коммит>] [--appear 180] [--timeout 3000]
Последняя строка - итог: `CI success|failure|none|timeout|error ...`. Коды: 0 успех, 1 падение,
3 прогонов нет или ошибка gh, 124 таймаут. При падении перед итогом - хвост `gh run view --log-failed`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

OK = frozenset({"success", "skipped", "neutral"})
FIELDS = "databaseId,name,status,conclusion,url"
LOG_TAIL = 80


def tail_to_error(lines: list[str]) -> str:
    """Хвост лога, оканчивающийся последней строкой ##[error]: после неё идёт уборка раннера."""
    end = max(
        (i for i, line in enumerate(lines) if "##[error]" in line),
        default=len(lines) - 1,
    )
    return "\n".join(lines[max(0, end + 1 - LOG_TAIL) : end + 1])


class GhError(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    appear: float = 180
    timeout: float = 3000
    interval: float = 15


def watch(
    list_runs: Callable[[], list],
    failed_log: Callable[[int], str],
    emit: Callable[[str], None],
    clock,
    limits: Limits,
) -> int:
    appear, timeout, interval = limits.appear, limits.timeout, limits.interval
    start = clock.time()
    try:
        while True:
            runs = list_runs()
            elapsed = clock.time() - start
            if runs and all(r["status"] == "completed" for r in runs):
                break
            if not runs and elapsed >= appear:
                emit(f"CI none: прогонов коммита нет за {int(appear)} с")
                return 3
            if elapsed >= timeout:
                emit(f"CI timeout: прогоны не завершились за {int(timeout)} с")
                return 124
            clock.sleep(interval)
        failed = [r for r in runs if r["conclusion"] not in OK]
        for r in runs:
            emit(f"{r['conclusion']}: {r['name']} {r['url']}")
        for r in failed:
            emit(failed_log(r["databaseId"]))
    except GhError as exc:
        emit(f"CI error: {exc}")
        return 3
    if failed:
        emit("CI failure: " + ", ".join(f"{r['name']} {r['url']}" for r in failed))
        return 1
    emit(f"CI success: {len(runs)} прогон(ов)")
    return 0


def _gh(cwd: str, *args: str) -> str:
    try:
        done = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(str(exc)) from exc
    if done.returncode != 0:
        raise GhError((done.stderr or done.stdout).strip()[-500:])
    return done.stdout


def resolve_sha(rev: str | None, git: Callable[..., str]) -> str:
    """Полный хеш коммита: gh run list --commit не находит прогоны по короткому."""
    return git("rev-parse", "--verify", f"{rev or 'HEAD'}^{{commit}}").strip()


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci_watch.py")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--sha")
    parser.add_argument("--appear", type=float, default=180)
    parser.add_argument("--timeout", type=float, default=3000)
    args = parser.parse_args(argv)
    try:
        sha = resolve_sha(args.sha, lambda *git_args: _git(args.cwd, *git_args))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"CI error: git rev-parse {args.sha or 'HEAD'}: {exc}", flush=True)
        return 3
    print(f"CI watch: {sha[:12]} в {args.cwd}", flush=True)

    def list_runs() -> list:
        return json.loads(
            _gh(
                args.cwd,
                "run",
                "list",
                "--commit",
                sha,
                "--json",
                FIELDS,
                "--limit",
                "50",
            )
            or "[]"
        )

    def failed_log(run_id: int) -> str:
        try:
            lines = _gh(args.cwd, "run", "view", str(run_id), "--log-failed").splitlines()
        except GhError as exc:
            return f"(лог прогона {run_id} недоступен: {exc})"
        return tail_to_error(lines)

    return watch(
        list_runs,
        failed_log,
        lambda line: print(line, flush=True),
        time,
        Limits(appear=args.appear, timeout=args.timeout),
    )


if __name__ == "__main__":
    sys.exit(main())
