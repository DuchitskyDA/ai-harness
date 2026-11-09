"""PostToolUse: после успешного git push в репозитории с GitHub Actions - команда фоновой слежки за CI."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# git в начале команды или после ; & | ( - так «echo git push» и «git log --grep push» не считаются
_PUSH = re.compile(r'(?:^|[;&|(]\s*)git\s+(?:-C\s+(?P<c>"[^"]+"|\'[^\']+\'|\S+)\s+)?push\b(?P<rest>[^;&|]*)')
_CD = re.compile(r'(?:^|[;&|(]\s*)cd\s+(?P<d>"[^"]+"|\'[^\']+\'|\S+)\s*(?:&&|;)')


def _unquote(token: str) -> str:
    return shlex.split(token)[0] if token[:1] in "\"'" else token


def _git_toplevel(directory: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def handle(payload: dict, toplevel: Callable[[str], str | None] = _git_toplevel) -> dict | None:
    command = str((payload.get("tool_input") or {}).get("command") or "")
    push = _PUSH.search(command)
    if push is None or "--dry-run" in push.group("rest"):
        return None
    directory = str(payload.get("cwd") or ".")
    cds = [m for m in _CD.finditer(command) if m.start() < push.start()]
    cd = cds[-1] if cds else None
    if push.group("c"):
        directory = _unquote(push.group("c"))
    elif cd:
        directory = _unquote(cd.group("d"))
    directory = str(Path(directory).expanduser())
    root = toplevel(directory) if Path(directory).is_dir() else None
    workflows = Path(root, ".github", "workflows") if root else None
    if workflows is None or not any(workflows.glob("*.y*ml")):
        return None
    script = Path.home() / ".harness" / "ci_watch.py"
    text = (
        f"Пуш в репозиторий с GitHub Actions. Сразу запусти фоновой задачей (run_in_background: true, "
        f'timeout 3600000) ровно эту команду: python -X utf8 -B "{script.as_posix()}" --cwd "{root}". '
        "Она завершается и на успехе, и на падении; итог - последняя строка CI success|failure|none|timeout|error. "
        "Не жди её вручную. Красный CI - POLICY, «CI после пуша»."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return 0
    out = handle(payload) if isinstance(payload, dict) else None
    if out is not None:
        sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
