"""Notification/Stop/UserPromptSubmit: решение, транспорт окна, баннер macOS, фокус Windows.

Процессы, ctypes и платформа подменены на границе: окно, osascript и терминал не запускаются.
Настоящие Win32-вызовы - только в тестах со skipif не-Windows.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(f"harness_hooks_{name}", HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notification = _load("notification")
notify_focus = _load("notify_focus")
WIN = pytest.mark.skipif(sys.platform != "win32", reason="настоящий Win32 API")


def _event(name="Stop", sid: object = "sess-1", **extra):
    return {"session_id": sid, "hook_event_name": name, "cwd": "C:\\work\\app", **extra}


class Effects:
    """Внешние эффекты main: показ окна и закрытие окна сессии."""

    def __init__(self, monkeypatch):
        self.shown, self.closed = [], []
        monkeypatch.setattr(notification, "show", lambda title, body, sid: self.shown.append((title, body, sid)))
        monkeypatch.setattr(notification, "close_session", self.closed.append)


@pytest.fixture
def effects(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_VERSION", raising=False)
    return Effects(monkeypatch)


def _main(monkeypatch, capsys, raw):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))
    code = notification.main()
    out, err = capsys.readouterr()
    return code, out, err


def _raw(payload, bom=False):
    return (b"\xef\xbb\xbf" if bom else b"") + json.dumps(payload, ensure_ascii=False).encode("utf-8")


# --- вход и решение -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _raw(_event(last_assistant_message="Готово")),
        _raw(_event(last_assistant_message="Готово"), bom=True),
        _raw(_event(last_assistant_message="Готово", stop_hook_active=True, permission_mode="auto", x={"y": 1})),
    ],
)
def test_input_transport(monkeypatch, capsys, effects, raw):
    code, out, _err = _main(monkeypatch, capsys, raw)
    assert (code, out) == (0, "")
    assert effects.shown == [("app: ход закончен", "Готово", "sess-1")]


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe{",
        b'{"session_id"',
        b"[]",
        b"null",
        _raw({"hook_event_name": "Stop"}),
        _raw(_event(sid=5)),
        _raw(_event(sid="")),
        _raw(_event(sid="sess.1")),
        _raw(_event(sid="sess_1")),
        _raw(_event(sid='a"b')),
        _raw(_event(sid="a\nb")),
        _raw(_event(sid="$(id)")),
        _raw(_event(name="SessionStart")),
        _raw(_event(name="SubagentStop")),
    ],
)
def test_invalid_input_no_effect(monkeypatch, capsys, effects, raw):
    code, out, err = _main(monkeypatch, capsys, raw)
    assert (code, out) == (0, "")
    assert (effects.shown, effects.closed) == ([], [])
    assert err.strip() == "harness hooks: invalid input"


@pytest.mark.parametrize(
    "payload",
    [
        _event("PreToolUse", tool_name="AskUserQuestion", tool_input={"questions": [{"question": "Какой путь?"}]}),
        _event("PreToolUse", tool_name="ExitPlanMode", tool_input={"plan": "План"}),
        _event("SubagentStop", last_assistant_message="Готово"),
    ],
)
def test_old_pretooluse_event_is_ignored(payload):
    assert notification.decide(payload, None) is None


@pytest.mark.parametrize(
    ("cwd", "label"),
    [
        ("C:\\work\\app\\", "app"),
        ("/home/u/proj", "proj"),
        ("/", "Claude"),
        ("", "Claude"),
        (None, "Claude"),
    ],
)
def test_decide_label_from_cwd(cwd, label):
    title, _body = notification.decide({**_event("Notification", message="x"), "cwd": cwd})
    assert title == f"{label}: нужно действие"


@pytest.mark.parametrize(
    ("message", "body"),
    [
        (None, "Ждёт действия"),
        ("", "Ждёт действия"),
        (42, "Ждёт действия"),
        (["x"], "Ждёт действия"),
        ("\n  \n Нужно разрешение\nвторая", "Нужно разрешение"),
        ("a\x1b[31mb\x07c", "a[31mbc"),
        ("я" * 140, "я" * 140),
        ("я" * 141, "я" * 139 + "…"),
    ],
)
def test_decide_text_boundaries(message, body):
    assert notification.decide(_event("Notification", message=message)) == ("app: нужно действие", body)


@pytest.mark.parametrize(
    ("message", "tasks", "body"),
    [
        (None, None, "Ждёт ввода"),
        ("", [], "Ждёт ввода"),
        ("Итог\nдальше", None, "Итог"),
        ("x" * 200, None, "x" * 139 + "…"),
        ("Готово", [{"type": "shell"}, {"type": "monitor"}, {"type": "other"}, {}], "Готово"),
    ],
)
def test_stop_notification(message, tasks, body):
    payload = _event(last_assistant_message=message, background_tasks=tasks)
    assert notification.decide(payload) == ("app: ход закончен", body)


@pytest.mark.parametrize(
    ("tasks", "silent"),
    [
        ([{"type": "subagent"}], True),
        ([{"type": "Workflow"}], True),
        ([{"type": "shell"}, {"type": "SUBAGENT"}], True),
        ({"type": "subagent"}, False),
        ("subagent", False),
        (["subagent", 7, None], False),
    ],
)
def test_stop_waking_tasks(tasks, silent):
    result = notification.decide(_event(last_assistant_message="Готово", background_tasks=tasks))
    assert (result is None) is silent


@pytest.mark.parametrize(
    "environ",
    [
        {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"},
        {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"},
        {"CLAUDE_CODE_ENTRYPOINT": "sdk"},
        {"CLAUDE_CODE_ENTRYPOINT": "t3-code", "CLAUDE_AGENT_SDK_VERSION": "0.3.284"},
        {"CLAUDE_CODE_ENTRYPOINT": "t3-code"},
        {"CLAUDE_CODE_ENTRYPOINT": "t3-other"},
        {"CLAUDE_AGENT_SDK_VERSION": "0.3.284"},
        {"CLAUDE_AGENT_SDK_VERSION": "1.0.0"},
    ],
)
def test_sdk_host_detected(environ):
    assert notification.hosted(environ) is True


@pytest.mark.parametrize("environ", [{}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}])
def test_terminal_is_not_host(environ):
    assert notification.hosted(environ) is False


@pytest.mark.parametrize("name", ["Notification", "Stop", "UserPromptSubmit"])
def test_host_is_silent(name):
    assert notification.decide(_event(name, message="m"), in_host=True) is None
    assert notification.decide(_event(name, message="m"), in_host=False) is not None


@pytest.mark.parametrize("name", ["Notification", "Stop"])
def test_t3_session_shows_no_window(monkeypatch, capsys, effects, name):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "t3-code")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_VERSION", "0.3.284")
    code, out, _err = _main(monkeypatch, capsys, _raw(_event(name, message="m", last_assistant_message="Готово")))
    assert (code, out, effects.shown, effects.closed) == (0, "", [], [])


def test_submit_closes_only_own_session(monkeypatch, capsys, effects):
    _main(monkeypatch, capsys, _raw(_event("UserPromptSubmit", sid="sess-A", prompt="дальше")))
    _main(monkeypatch, capsys, _raw(_event("UserPromptSubmit", sid="sess-A", prompt="дальше")))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    code, out, _err = _main(monkeypatch, capsys, _raw(_event("UserPromptSubmit", sid="sess-B")))
    assert (code, out) == (0, "")
    assert effects.closed == ["sess-A", "sess-A"]
    assert effects.shown == []


def test_platform_failure_is_quiet(monkeypatch, capsys, effects):
    monkeypatch.setattr(notification, "show", MagicMock(side_effect=OSError("fixture")))
    code, out, err = _main(monkeypatch, capsys, _raw(_event("Notification", message="m")))
    assert (code, out) == (0, "")
    assert err.strip() == "harness hooks: notification unavailable"


# --- окно Windows: аргументы, флаги запуска, ключ сессии ----------------------------


class FakePopen:
    def __init__(self, failures=0):
        self.calls, self.failures = [], failures

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if len(self.calls) <= self.failures:
            raise OSError("fixture")


def _arg(cmd, name):
    return cmd[cmd.index(name) + 1]


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("проект: вопрос", 'Путь "C:\\x\\y" и $(rm) `whoami`'),
        ("a'b", "строка; & | > <"),
    ],
)
def test_popup_arguments_are_data(monkeypatch, title, body):
    popen = FakePopen()
    monkeypatch.setattr(notification.subprocess, "Popen", popen)
    monkeypatch.setattr(notification, "_host", lambda: (4321, 133000000000000001))
    notification._launch_popup(title, body, "sess-1")
    cmd, kwargs = popen.calls[0]
    assert [base64.b64decode(_arg(cmd, k)).decode("utf-8") for k in ("-Title", "-Body")] == [title, body]
    assert (_arg(cmd, "-FocusPid"), _arg(cmd, "-FocusBorn"), _arg(cmd, "-FocusPython")) == (
        "4321",
        "133000000000000001",
        "python",
    )
    assert _arg(cmd, "-Session") == hashlib.sha256(b"sess-1").hexdigest()
    assert isinstance(cmd, list) and not kwargs.get("shell")


def test_popup_without_focus_target(monkeypatch):
    popen = FakePopen()
    monkeypatch.setattr(notification.subprocess, "Popen", popen)
    monkeypatch.setattr(notification, "_host", lambda: None)
    notification._launch_popup("t", "b", "sess-1")
    assert "-FocusPid" not in popen.calls[0][0] and "-FocusBorn" not in popen.calls[0][0]


@pytest.mark.parametrize(("failures", "attempts"), [(0, 1), (1, 2)])
def test_popup_launch_fallback(monkeypatch, failures, attempts):
    popen = FakePopen(failures)
    monkeypatch.setattr(notification.subprocess, "Popen", popen)
    monkeypatch.setattr(notification, "_host", lambda: None)
    notification._launch_popup("t", "b", "sess-1")
    flags = [kwargs["creationflags"] for _cmd, kwargs in popen.calls]
    assert len(flags) == attempts and flags[-1] & 0x08000000
    assert bool(flags[-1] & 0x01000000) is (attempts == 1)  # CREATE_BREAKAWAY_FROM_JOB только в первой


def test_popup_launch_failure(monkeypatch):
    popen = FakePopen(failures=2)
    monkeypatch.setattr(notification.subprocess, "Popen", popen)
    monkeypatch.setattr(notification, "_host", lambda: None)
    with pytest.raises(OSError):
        notification._launch_popup("t", "b", "sess-1")
    assert len(popen.calls) == 2


def test_session_window_key():
    key = notification._window_key
    assert key("sess-1") == key("sess-1") == hashlib.sha256(b"sess-1").hexdigest()
    assert key("sess-1") != key("sess1")


# --- macOS-баннер и прочие ОС --------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "body", "script"),
    [
        ("app: вопрос", 'Путь "C:\\x"', 'display notification "Путь \\"C:\\\\x\\"" with title "app: вопрос"'),
        ('a"b', "кириллица", 'display notification "кириллица" with title "a\\"b"'),
    ],
)
def test_macos_notification_adapter(monkeypatch, title, body, script):
    run = MagicMock()
    monkeypatch.setattr(notification.subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", "darwin")
    notification.show(title, body, "sess-1")
    notification.close_session("sess-1")
    monkeypatch.undo()
    assert run.call_args_list == [
        call(["osascript", "-e", script], timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    ]


@pytest.mark.parametrize("error", [OSError("fixture"), subprocess.TimeoutExpired("osascript", 10)])
def test_macos_failure_is_quiet(monkeypatch, capsys, error):
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_VERSION", raising=False)
    monkeypatch.setattr(notification.subprocess, "run", MagicMock(side_effect=error))
    monkeypatch.setattr(sys, "platform", "darwin")
    code, out, err = _main(monkeypatch, capsys, _raw(_event("Notification", message="m")))
    monkeypatch.undo()
    assert (code, out, err.strip()) == (0, "", "harness hooks: notification unavailable")


def test_other_platform_makes_no_calls(monkeypatch):
    run, popen = MagicMock(), MagicMock()
    monkeypatch.setattr(notification.subprocess, "run", run)
    monkeypatch.setattr(notification.subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "platform", "linux")
    notification.show("t", "b", "sess-1")
    notification.close_session("sess-1")
    monkeypatch.undo()
    assert (run.call_count, popen.call_count) == (0, 0)


@WIN
def test_close_session_signals_only_own_window():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    tag = f"test-{os.getpid()}"
    own, other = (
        k32.CreateEventW(None, True, False, "Local\\ClaudeAlert-" + notification._window_key(f"{tag}-{s}"))
        for s in ("A", "B")
    )
    try:
        notification.close_session(f"{tag}-A")
        notification.close_session(f"{tag}-A")
        notification.close_session(f"{tag}-C")  # окна нет - тихо
        assert (k32.WaitForSingleObject(own, 0), k32.WaitForSingleObject(other, 0)) == (0, 0x102)
    finally:
        k32.CloseHandle(own)
        k32.CloseHandle(other)


# --- фокус Windows: характеристика base и проверка цели ------------------------------


@pytest.fixture
def console(monkeypatch):
    console = MagicMock()
    console.title.return_value = "Claude Code"
    monkeypatch.setattr(notify_focus, "_Console", lambda pid: console)
    return console


def test_classic_console_detaches(console):
    console.root_window.return_value = (99, "ConsoleWindowClass")
    assert notify_focus.focus(123) == 99
    console.detach.assert_called_once_with()


def test_missing_window_detaches(console):
    console.root_window.return_value = (0, "")
    assert notify_focus.focus(123) == 0
    console.detach.assert_called_once_with()


def test_duplicate_title_restores_original(console, monkeypatch):
    console.root_window.return_value = (99, "CASCADIA_HOSTING_WINDOW_CLASS")
    monkeypatch.setattr(notify_focus, "_select_tab", MagicMock(side_effect=["many", "selected"]))
    assert notify_focus.focus(123) == 99
    assert console.set_title.call_args_list[-1] == call("Claude Code")
    console.detach.assert_called_once_with()


def test_selection_failure_restores_original(console, monkeypatch):
    console.root_window.return_value = (99, "CASCADIA_HOSTING_WINDOW_CLASS")
    monkeypatch.setattr(notify_focus, "_select_tab", MagicMock(side_effect=["many", RuntimeError("fixture")]))
    with pytest.raises(RuntimeError, match="fixture"):
        notify_focus.focus(123)
    assert console.set_title.call_args_list[-1] == call("Claude Code")
    console.detach.assert_called_once_with()


_CHAIN = {
    10: (20, "python.exe"),
    20: (30, "bash.exe"),
    30: (40, "claude.exe"),
    40: (50, "claude.exe"),
    50: (0, "wt.exe"),
}
_BORN = {10: 500, 20: 400, 30: 300, 40: 200, 50: 100}


def _long_chain(depth):
    """claude.exe - предок номер depth процесса 1."""
    procs = {pid: (pid + 1, "bash.exe") for pid in range(1, depth + 1)}
    procs[depth + 1] = (0, "claude.exe")
    return procs, {pid: 1000 - pid for pid in procs}, 1


@pytest.mark.parametrize(
    ("procs", "born", "start", "host"),
    [
        (_CHAIN, _BORN, 10, (30, 300)),  # ближайший claude.exe
        ({**_CHAIN, 30: (40, "Claude.EXE")}, _BORN, 10, (30, 300)),
        ({10: (20, "python.exe"), 20: (10, "bash.exe")}, {10: 2, 20: 1}, 10, None),  # цикл
        (*_long_chain(32), (33, 967)),
        (*_long_chain(33), None),  # дальше 32 предков
        ({10: (20, "python.exe")}, {10: 2}, 10, None),  # предка нет в снимке
        (_CHAIN, {**_BORN, 20: None}, 10, None),  # предок умер
        (_CHAIN, {**_BORN, 30: 600}, 10, None),  # PID родителя занят процессом новее потомка
    ],
)
def test_stale_focus_target_is_ignored(procs, born, start, host):
    assert notify_focus._ancestor(procs, born.get, start) == host


_FOCUSED, _REFUSED = (0, "777", 1), (1, "", 0)  # код выхода, stdout, вызовов focus


@pytest.mark.parametrize(
    ("argv", "alive_born", "expected"),
    [
        (["--pid", "30", "--born", "300"], 300, _FOCUSED),
        (["--pid", "30", "--born", "300"], 301, _REFUSED),  # PID занят другим процессом
        (["--pid", "30", "--born", "300"], None, _REFUSED),  # процесс исчез
        (["sess-1"], 300, _REFUSED),  # позиционный session_id больше не поддерживается
        (["--pid", "-30", "--born", "300"], 300, _REFUSED),
        (["--pid", "30", "--born", "x"], 300, _REFUSED),
    ],
)
def test_focus_cli_checks_target(monkeypatch, capsys, argv, alive_born, expected):
    focus = MagicMock(return_value=777)
    monkeypatch.setattr(notify_focus, "focus", focus)
    monkeypatch.setattr(notify_focus, "_birth", lambda pid: alive_born)
    monkeypatch.setattr(sys, "platform", "win32")
    result = notify_focus.main(argv)
    monkeypatch.undo()
    assert (result, capsys.readouterr().out.strip(), focus.call_count) == expected


@WIN
def test_native_process_queries():
    procs = notify_focus._snapshot()
    assert procs[os.getpid()][0] == os.getppid()
    assert notify_focus._birth(os.getpid()) > 0
    assert notify_focus._birth(0) is None
