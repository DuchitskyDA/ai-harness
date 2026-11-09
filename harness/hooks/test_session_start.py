"""SessionStart: карточка, экспорт HARNESS_SESSION в CLAUDE_ENV_FILE, безопасный разбор входа.

Файл среды - настоящий файл в tmp_path: так проверяется режим дозаписи без затирания.
Процессов, сети и состояния вне tmp_path нет.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("harness_hooks_session_start", HERE / "session_start.py")
assert _spec is not None and _spec.loader is not None
start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(start)


def _event(sid: object = "sess-1", source="startup", **extra):
    return {"session_id": sid, "hook_event_name": "SessionStart", "source": source, "cwd": "/w/app", **extra}


def _run(monkeypatch, capsysbinary, raw, env_file=None):
    if env_file is None:
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))
    code = start.main()
    out, err = capsysbinary.readouterr()
    return code, out, err.decode("utf-8")


def _raw(payload, bom=False):
    return (b"\xef\xbb\xbf" if bom else b"") + json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("raw", [_raw(_event()), _raw(_event(), bom=True), _raw(_event(model="x", extra={"a": 1}))])
def test_input_transport(monkeypatch, capsysbinary, tmp_path, raw):
    code, out, _err = _run(monkeypatch, capsysbinary, raw, tmp_path / "env")
    assert code == 0
    assert out.endswith(b"}\n") and out.count(b"\n") == 1
    assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": start.CARD}}


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe{",
        b'{"session_id": ',
        b"[]",
        b"null",
        b'"sess-1"',
        _raw({"hook_event_name": "SessionStart"}),
        _raw(_event(sid=7)),
        _raw(_event(sid="")),
        _raw({**_event(), "hook_event_name": "SessionEnd"}),
        _raw({**_event(), "hook_event_name": "PreToolUse"}),
    ],
)
def test_invalid_input_no_effect(monkeypatch, capsysbinary, tmp_path, raw):
    env_file = tmp_path / "env"
    code, out, err = _run(monkeypatch, capsysbinary, raw, env_file)
    assert (code, out) == (0, b"")
    assert not env_file.exists()
    assert err.strip() == "harness hooks: invalid input"


def test_start_has_no_runtime_dependencies():
    tree = ast.parse((HERE / "session_start.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported |= {
        node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= {"__future__", "json", "os", "re", "sys"}


def test_card_points_to_rules_only():
    # beads снят из оснастки 30.10: карточка не отсылает к бидам
    assert start.CARD == "Оснастка harness активна. Правила: ~/.claude/CLAUDE.md и ~/.harness/POLICY.md."


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact", "fork", "unknown"])
def test_handle_is_card_for_any_source(source):
    assert start.handle(_event(source=source)) == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": start.CARD}
    }


def test_session_export_lifecycle(monkeypatch, capsysbinary, tmp_path):
    env_file = tmp_path / "env"
    _run(monkeypatch, capsysbinary, _raw(_event(sid="sess-A", source="startup")), env_file)
    _run(monkeypatch, capsysbinary, _raw(_event(sid="sess-A", source="compact")), env_file)
    _run(monkeypatch, capsysbinary, _raw(_event(sid="sess-A", source="resume")), env_file)
    _run(monkeypatch, capsysbinary, _raw(_event(sid="sess-B", source="clear")), env_file)
    lines = [line for line in env_file.read_text(encoding="utf-8").splitlines() if line]
    assert lines == ["export HARNESS_SESSION=sess-A"] * 3 + ["export HARNESS_SESSION=sess-B"]


def test_session_export_preserves_content(monkeypatch, capsysbinary, tmp_path):
    env_file = tmp_path / "env"
    env_file.write_bytes(b'export OTHER="a b"\nPATH_EXTRA=1')  # чужая последняя строка без LF
    code, out, _err = _run(monkeypatch, capsysbinary, _raw(_event()), env_file)
    assert code == 0 and out
    assert env_file.read_bytes() == b'export OTHER="a b"\nPATH_EXTRA=1\nexport HARNESS_SESSION=sess-1\n'


@pytest.mark.parametrize("target", ["unset", "empty", "directory", "missing-parent"])
def test_session_export_unavailable(monkeypatch, capsysbinary, tmp_path, target):
    env_file = {"unset": None, "empty": "", "directory": tmp_path, "missing-parent": tmp_path / "no" / "env"}[target]
    code, out, err = _run(monkeypatch, capsysbinary, _raw(_event()), env_file)
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == start.CARD
    assert err.strip() == "harness hooks: session export unavailable"
    assert not (tmp_path / "no").exists()


@pytest.mark.parametrize("sid", ["sess.1", "sess_1", "sess 1", '"sess"', "s'1", "sess\n1", "s$(id)", "s;x", "s/1", ""])
def test_session_id_boundaries_rejected(monkeypatch, capsysbinary, tmp_path, sid):
    env_file = tmp_path / "env"
    code, out, _err = _run(monkeypatch, capsysbinary, _raw(_event(sid=sid)), env_file)
    assert (code, out) == (0, b"")
    assert not env_file.exists()
    assert start.persist_session(sid, str(env_file)) is False


def test_session_id_boundaries_accepted(tmp_path):
    env_file = tmp_path / "env"
    assert start.persist_session("Ab-09-ffff", str(env_file)) is True
    assert env_file.read_bytes() == b"\nexport HARNESS_SESSION=Ab-09-ffff\n"
