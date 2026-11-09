"""SessionStart: карточка одной строкой JSON на stdout, export HARNESS_SESSION дописывается в CLAUDE_ENV_FILE."""

from __future__ import annotations

import json
import os
import re
import sys

CARD = "Оснастка harness активна. Правила: ~/.claude/CLAUDE.md и ~/.harness/POLICY.md."
SESSION_ID_PATTERN = "[A-Za-z0-9-]+"


def _read() -> object:
    try:
        return json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return None


def handle(payload: dict) -> dict | None:
    sid = payload.get("session_id")
    ok = (
        payload.get("hook_event_name") == "SessionStart"
        and isinstance(sid, str)
        and re.fullmatch(SESSION_ID_PATTERN, sid)
    )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": CARD}} if ok else None


def persist_session(session_id: str, env_file: str | None) -> bool:
    if not env_file or not re.fullmatch(SESSION_ID_PATTERN, session_id):
        return False
    try:
        # ведущий LF отделяет запись от чужой последней строки без перевода строки
        with open(env_file, "a", encoding="utf-8", newline="\n") as handle_:
            handle_.write(f"\nexport HARNESS_SESSION={session_id}\n")
    except OSError:
        return False
    return True


def main() -> int:
    payload = _read()
    card = handle(payload) if isinstance(payload, dict) else None
    if card is None or not isinstance(payload, dict):
        print("harness hooks: invalid input", file=sys.stderr)
        return 0
    if not persist_session(payload["session_id"], os.environ.get("CLAUDE_ENV_FILE")):
        print("harness hooks: session export unavailable", file=sys.stderr)
    sys.stdout.buffer.write((json.dumps(card, ensure_ascii=False) + "\n").encode("utf-8"))  # LF и на Windows
    return 0


if __name__ == "__main__":
    sys.exit(main())
