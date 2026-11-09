"""PostToolUse: после gh pr create|edit или glab mr create|update - команда проверки тела PR, напоминание о формате
и запись ссылки для pr_merged.py: на следующее сообщение владельца сессия узнает о слиянии."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_merged

# gh/glab в начале команды или после ; & | ( - так «echo gh pr create» не считается
_PR = re.compile(r"(?:^|[;&|(]\s*)(?:gh\s+pr\s+(?:create|edit)|glab\s+mr\s+(?:create|update))\b")
_URL = re.compile(r"https://\S+?/(?:pull|-/merge_requests)/\d+")


def _output(response: object) -> str:
    if isinstance(response, dict):
        return " ".join(str(response.get(key) or "") for key in ("stdout", "stderr", "output"))
    return str(response or "")


def handle(payload: dict) -> dict | None:
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if _PR.search(command) is None:
        return None
    script = (Path.home() / ".harness" / "pr_assets.py").as_posix()
    found = _URL.search(_output(payload.get("tool_response")))
    url = found.group(0) if found else "<ссылка на PR или MR>"
    if found and payload.get("session_id"):
        pr_merged.remember(str(payload["session_id"]), url)
    text = (
        f'PR или MR создан или изменён. Проверь тело: python -X utf8 -B "{script}" check {url}. '
        "Нарушения исправь (gh pr edit --body-file, glab mr update --description) и проверь снова. "
        "Формат - POLICY, «PR»: заголовок conventional commit; Было, Стало, Почему, До | После, Проверка, модель. "
        f'Снимки - только python -X utf8 -B "{script}" upload <проект> <номер> <файлы>, '
        "в GitLab - вложением MR; ссылки руками не пиши."
    )
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


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
