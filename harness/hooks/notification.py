"""Notification/Stop/UserPromptSubmit: окно владельцу (Windows - notify_popup.ps1, macOS - баннер); stdout пуст."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # TypeIs - с Python 3.13, в рантайме не нужен
    from typing import TypeIs

CLOSE = "close"
# BackgroundTaskSummary.type хука Stop: такие задачи сами вернут ход сессии
WAKING_TASKS = ("subagent", "workflow")
SESSION_ID_PATTERN = "[A-Za-z0-9-]+"
_MAX_BODY = 140
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
_HERE = os.path.dirname(os.path.abspath(__file__))
_POPUP, _FOCUS = os.path.join(_HERE, "notify_popup.ps1"), os.path.join(_HERE, "notify_focus.py")


def _read() -> object:
    try:
        return json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return None


def _valid(payload: object) -> TypeIs[dict]:
    if not isinstance(payload, dict):
        return False
    sid = payload.get("session_id")
    return (
        isinstance(sid, str)
        and re.fullmatch(SESSION_ID_PATTERN, sid) is not None
        and payload.get("hook_event_name") in ("Notification", "Stop", "UserPromptSubmit")
    )


def _line(text: object, fallback: str) -> str:
    for line in text.splitlines() if isinstance(text, str) else []:
        line = _CONTROL.sub("", line).strip()
        if line:
            return line if len(line) <= _MAX_BODY else line[: _MAX_BODY - 1] + "…"
    return fallback


def hosted(environ: Mapping[str, str]) -> bool:
    """Сессию запустил Agent SDK (T3 и др.): уведомляет сам хост, окна оснастки лишние."""
    return environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(("sdk", "t3")) or "CLAUDE_AGENT_SDK_VERSION" in environ


def decide(payload: dict, in_host: bool = False) -> tuple[str, str] | str | None:
    """(заголовок, текст) - показать, CLOSE - закрыть окно сессии, None - молчать."""
    if not _valid(payload) or in_host:
        return None
    event = payload["hook_event_name"]
    if event == "UserPromptSubmit":
        return CLOSE
    label = _line(re.split(r"[\\/]", str(payload.get("cwd") or "").rstrip("\\/"))[-1], "Claude")
    if event == "Notification":
        return f"{label}: нужно действие", _line(payload.get("message"), "Ждёт действия")
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list) and any(
        isinstance(task, dict) and str(task.get("type")).lower() in WAKING_TASKS for task in tasks
    ):
        return None
    return f"{label}: ход закончен", _line(payload.get("last_assistant_message"), "Ждёт ввода")


def _window_key(session_id: str) -> str:
    """Имя системных Mutex/Event окна сессии: различимо для любых допустимых id."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def close_session(session_id: str) -> None:
    """Windows: сигнал событию окна своей сессии - окно закрывается; окна нет - ничего."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.SetEvent.argtypes = k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenEventW(0x0002, False, "Local\\ClaudeAlert-" + _window_key(session_id))  # EVENT_MODIFY_STATE
    if handle:
        try:
            k32.SetEvent(handle)
        finally:
            k32.CloseHandle(handle)


def _host() -> tuple[int, int] | None:
    import notify_focus  # сосед по каталогу: у запущенного скрипта sys.path[0] - harness/hooks

    return notify_focus.find_host()


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _launch_popup(title: str, body: str, session_id: str) -> None:
    """Окно отдельным процессом: хук не ждёт, окно живёт дольше таймаута хука."""
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        _POPUP,
        "-Title",
        _b64(title),
        "-Body",
        _b64(body),
        "-Session",
        _window_key(session_id),
        "-FocusPython",
        "python",
        "-FocusScript",
        _FOCUS,
    ]
    host = _host()  # PID и время создания claude.exe - до отсоединения окна, пока хук жив
    if host:
        cmd += ["-FocusPid", str(host[0]), "-FocusBorn", str(host[1])]
    # DETACHED_PROCESS нельзя: powershell.exe без консоли выходит сразу (проба u2rz);
    # скрытая своя консоль (CREATE_NO_WINDOW) от консоли хука отвязывает так же
    base = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    # хост может держать хук в задании: сначала с выходом из него, без права - откат
    for flags in (base | 0x01000000, base):  # CREATE_BREAKAWAY_FROM_JOB
        try:
            subprocess.Popen(
                cmd,
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            continue
    raise OSError("popup did not start")


def show(title: str, body: str, session_id: str) -> None:
    if sys.platform == "win32":
        _launch_popup(title, body, session_id)
    elif sys.platform == "darwin":

        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        subprocess.run(
            ["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'],
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def main() -> int:
    if not _valid(payload := _read()):
        print("harness hooks: invalid input", file=sys.stderr)
        return 0
    result = decide(payload, hosted(os.environ))
    try:
        if result == CLOSE:
            close_session(payload["session_id"])
        elif result:
            show(result[0], result[1], payload["session_id"])
    except Exception:  # noqa: BLE001 - окно - удобство: сбой платформы не роняет хук
        print("harness hooks: notification unavailable", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
