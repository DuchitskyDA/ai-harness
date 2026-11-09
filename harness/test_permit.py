"""permit: строка autoMode.allow добавляется и убирается, остальное в settings.json не трогается."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("harness_permit", Path(__file__).resolve().parent / "permit.py")
assert _spec is not None and _spec.loader is not None
permit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(permit)

RULE = "Local UI stands: running stand role on a local stand."
LF, CRLF, BOM = "\n", "\r\n", "﻿"


def settings(tmp_path: Path, allow: list[str], newline: str = LF, bom: str = "") -> Path:
    path = tmp_path / "settings.json"
    data = {
        "model": "opus",
        "autoMode": {"allow": allow, "soft_deny": ["$defaults", "Keep"]},
    }
    text = bom + json.dumps(data, ensure_ascii=False, indent=2) + LF
    path.write_bytes(text.replace(LF, newline).encode("utf-8"))
    return path


def load(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def test_add_appends_after_owner_rules_and_keeps_rest(tmp_path, capsys):
    path = settings(tmp_path, ["$defaults", "Owner rule"])
    assert permit.main(["add", RULE, "--settings", str(path)]) == 0
    data = load(path)
    assert data["autoMode"]["allow"] == ["$defaults", "Owner rule", RULE]
    assert data["autoMode"]["soft_deny"] == ["$defaults", "Keep"] and data["model"] == "opus"
    assert capsys.readouterr().out.strip() == f"allow added: {RULE}"


def test_add_twice_keeps_one(tmp_path, capsys):
    path = settings(tmp_path, ["$defaults", RULE])
    assert permit.main(["add", RULE, "--settings", str(path)]) == 0
    assert load(path)["autoMode"]["allow"] == ["$defaults", RULE]
    assert capsys.readouterr().out.startswith("allow present")


def test_add_restores_defaults_first(tmp_path):
    path = settings(tmp_path, ["Owner rule"])
    permit.main(["add", RULE, "--settings", str(path)])
    assert load(path)["autoMode"]["allow"] == ["$defaults", "Owner rule", RULE]


def test_remove_only_that_rule(tmp_path, capsys):
    path = settings(tmp_path, ["$defaults", "Owner rule", RULE])
    assert permit.main(["remove", RULE, "--settings", str(path)]) == 0
    assert capsys.readouterr().out.strip() == f"allow removed: {RULE}"
    assert load(path)["autoMode"]["allow"] == ["$defaults", "Owner rule"]
    assert permit.main(["remove", RULE, "--settings", str(path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("allow absent")


def test_lf_file_stays_lf(tmp_path):
    path = settings(tmp_path, ["$defaults"], newline=LF)
    permit.main(["add", RULE, "--settings", str(path)])
    raw = path.read_bytes()
    assert raw.count(CRLF.encode()) == 0 and raw.count(LF.encode()) > 1


def test_crlf_file_stays_crlf(tmp_path):
    path = settings(tmp_path, ["$defaults"], newline=CRLF)
    permit.main(["add", RULE, "--settings", str(path)])
    raw = path.read_bytes()
    assert raw.count(CRLF.encode()) == raw.count(LF.encode()) > 1


@pytest.mark.parametrize("action", ["add", "remove"])
def test_bom_file_read_and_bom_kept(tmp_path, action):
    path = settings(tmp_path, ["$defaults", RULE], bom=BOM)
    assert permit.main([action, "Other rule", "--settings", str(path)]) == 0
    assert path.read_bytes().startswith(BOM.encode("utf-8"))
    assert RULE in load(path)["autoMode"]["allow"]


def test_held_lock_blocks_and_file_untouched(tmp_path, monkeypatch, capsys):
    path = settings(tmp_path, ["$defaults"])
    before = path.read_bytes()
    monkeypatch.setattr(permit, "LOCK_WAIT", 0.1)
    with permit.locked(path):
        assert permit.main(["add", RULE, "--settings", str(path)]) == 2
    assert path.read_bytes() == before
    assert "занят замок" in capsys.readouterr().out


def test_busy_lock_is_retried_until_free(tmp_path, monkeypatch):
    path = settings(tmp_path, ["$defaults"])
    real_try = permit._try_lock
    attempts = []

    def busy_once(fileno):
        attempts.append(fileno)
        if len(attempts) == 1:
            raise OSError("busy")
        real_try(fileno)

    monkeypatch.setattr(permit, "_try_lock", busy_once)
    assert permit.main(["add", RULE, "--settings", str(path)]) == 0
    assert len(attempts) == 2 and RULE in load(path)["autoMode"]["allow"]


def test_leftover_lock_file_does_not_block(tmp_path):
    path = settings(tmp_path, ["$defaults"])
    (tmp_path / "settings.json.permit-lock").write_bytes(b"x")
    assert permit.main(["add", RULE, "--settings", str(path)]) == 0
    assert RULE in load(path)["autoMode"]["allow"]


def test_remove_during_add_waits_and_rule_stays_removed(tmp_path, monkeypatch):
    # remove R приходит, пока add S держит прочитанный список: он ждёт замка, а не теряется
    path = settings(tmp_path, ["$defaults", "R"])
    monkeypatch.setattr(permit, "LOCK_WAIT", 0.1)
    inner = []
    real_change = permit.change

    def change_with_race(allow, action, rule):
        if not inner:
            inner.append(permit.main(["remove", "R", "--settings", str(path)]))
        return real_change(allow, action, rule)

    monkeypatch.setattr(permit, "change", change_with_race)
    assert permit.main(["add", "S", "--settings", str(path)]) == 0
    assert inner == [2]
    assert load(path)["autoMode"]["allow"] == ["$defaults", "R", "S"]


def test_defaults_and_empty_rule_refused(tmp_path, capsys):
    path = settings(tmp_path, ["$defaults"])
    before = path.read_bytes()
    assert permit.main(["add", "$defaults", "--settings", str(path)]) == 2
    assert permit.main(["add", "  ", "--settings", str(path)]) == 2
    assert path.read_bytes() == before
    assert capsys.readouterr().out.splitlines()[-1].startswith("allow error")


def test_broken_json_is_error_and_untouched(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_bytes(b"{broken")
    assert permit.main(["add", RULE, "--settings", str(path)]) == 2
    assert path.read_bytes() == b"{broken"
    assert capsys.readouterr().out.startswith("allow error")
    with permit.locked(path):  # ошибка не оставила замок занятым
        pass
