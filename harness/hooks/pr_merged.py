"""UserPromptSubmit: PR и MR сессии, слитые или закрытые с прошлого сообщения, - шаги после слияния в контекст.

Ссылки записывает post_pr.py в <state>/pr-watch/<session_id>.json. Ошибка gh или glab - тишина, ссылка остаётся.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

SESSION_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_GITHUB = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
_GITLAB = re.compile(r"https://(?P<host>[^/\s]+)/(?P<path>\S+?)/-/merge_requests/(?P<n>\d+)")

# url -> (состояние merged|closed|open, коммит слияния или "", ветка base или "")
State = Callable[[str], tuple[str, str, str]]
# base среди них или неизвестен - слияние в основную; иначе - PR волны в ветку эпика
MAIN_BRANCHES = ("main", "master", "develop")


def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "harness" if base else Path.home() / ".local" / "state" / "harness") / "pr-watch"


def _file(session_id: str) -> Path | None:
    return state_dir() / f"{session_id}.json" if SESSION_ID.fullmatch(session_id) else None


def load(session_id: str) -> list[str]:
    path = _file(session_id)
    try:
        urls = json.loads(path.read_text(encoding="utf-8")) if path else []
    except (OSError, ValueError):
        return []
    return [u for u in urls if isinstance(u, str)] if isinstance(urls, list) else []


def save(session_id: str, urls: list[str]) -> None:
    path = _file(session_id)
    if path is None:
        return
    if not urls:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(urls), encoding="utf-8")


def remember(session_id: str, url: str) -> None:
    urls = load(session_id)
    if url not in urls:
        save(session_id, [*urls, url])


def _run(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=8, check=True)
    return done.stdout


def host_state(url: str) -> tuple[str, str, str]:
    if _GITHUB.fullmatch(url):
        data = json.loads(_run(["gh", "pr", "view", url, "--json", "state,mergeCommit,baseRefName"]))
        return data["state"].lower(), (data.get("mergeCommit") or {}).get("oid", ""), str(data.get("baseRefName") or "")
    mr = _GITLAB.fullmatch(url)
    if mr is None:
        return "closed", "", ""
    endpoint = f"projects/{quote(mr['path'], safe='')}/merge_requests/{mr['n']}"
    data = json.loads(_run(["glab", "api", "--hostname", mr["host"], endpoint]))
    commit = str(data.get("squash_commit_sha") or data.get("merge_commit_sha") or "")
    status = str(data["state"])
    return ("open" if status in ("opened", "locked") else status), commit, str(data.get("target_branch") or "")


def handle(payload: dict, state: State = host_state) -> dict | None:
    session_id = str(payload.get("session_id") or "")
    urls = load(session_id)
    if not urls:
        return None
    notes, waves, left = [], [], []
    main_merged = False
    for url in urls:
        try:
            status, commit, base = state(url)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            left.append(url)
            continue
        if status == "merged" and base and base not in MAIN_BRANCHES:
            waves.append(base)
            notes.append(f"{url} слит владельцем в ветку эпика {base}, коммит {commit or 'не найден'}.")
        elif status == "merged":
            main_merged = True
            notes.append(f"{url} слит владельцем, коммит слияния {commit or 'не найден'}.")
        elif status == "closed":
            notes.append(f"{url} закрыт без слияния - скажи владельцу, если причина не ясна.")
        else:
            left.append(url)
    save(session_id, left)
    if not notes:
        return None
    steps = []  # волны раньше основной: уборка после слияния эпика удаляет копию ветки эпика
    if waves:
        steps.append(
            f"после слияния волны: в копии ветки {', '.join(dict.fromkeys(waves))} git pull --ff-only, удали ветку "
            "и worktree волны и её тикетов (эпик уже слит и копии нет - пропусти); install.py и ci_watch - "
            "только после слияния эпика в основную"
        )
    if main_merged:
        steps.append(
            "после слияния в основную: в общей копии git pull --ff-only, в оснастке install.py --apply, "
            "ci_watch.py --sha <коммит слияния> фоновой задачей, удали ветку и worktree"
        )
    text = " ".join(notes)
    if steps:
        text += " До ответа на сообщение выполни шаги (POLICY, «PR») " + "; ".join(steps)
        text += "; в отчёте строкой - что сделано."
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


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
