"""Лёгкие тесты обёртки harness/codex.py: Fake Popen, без настоящего Codex."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib

MODULE = Path(__file__).with_name("codex.py")
ROLES = ("adversarial", "ops")
TASK = "Сделай дело.\nВторая строка\n"
MARKER = "НЕСЕКРЕТНЫЙ-МАРКЕР-42"


class FakeProc:
    def __init__(self, fake, argv, kwargs):
        self.fake, self.argv, self.kwargs = fake, argv, kwargs
        self.pid = 4242 + len(fake.procs)
        self.returncode = None
        self.inputs = []

    @property
    def last(self) -> Path:
        return Path(self.argv[self.argv.index("-o") + 1])

    def communicate(self, input=None, timeout=None):
        self.inputs.append((input, timeout))
        step = self.fake.steps.pop(0) if self.fake.steps else answer()
        return step(self)

    def poll(self):
        return self.returncode


class FakeCodex:
    def __init__(self):
        self.procs, self.steps, self.killed, self.runs = [], [], [], []
        self.popen_error = None
        self.taskkill = lambda: subprocess.CompletedProcess([], 0)

    def popen(self, argv, **kwargs):
        if self.popen_error:
            raise self.popen_error
        proc = FakeProc(self, list(argv), kwargs)
        self.procs.append(proc)
        return proc

    def run(self, cmd, **kwargs):
        self.runs.append((cmd, kwargs))
        return self.taskkill()

    def killpg(self, pid, sig):
        self.killed.append((pid, sig))


def answer(text: bytes | None = "готово\n".encode(), rc: int = 0):
    def step(proc):
        proc.kwargs["stdout"].write(b"stdout-event\nstderr-event\n")
        if text is not None:
            proc.last.write_bytes(text)
        proc.returncode = rc
        return None, None

    return step


def raising(exc, finished: bool = False):
    def step(proc):
        if finished:
            proc.returncode = 0
        raise exc

    return step


def write_profile(home: Path, role: str, text: str) -> Path:
    path = home / ".codex" / "agents" / f"{role}.toml"
    path.write_text(text, encoding="utf-8")
    return path


def full_profile(role: str) -> str:
    return f'model = "gpt-{role}"\nmodel_reasoning_effort = "high"\ndeveloper_instructions = "Ты {role}."\n'


@pytest.fixture
def env(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("harness_codex_entry", MODULE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    home = tmp_path / "дом с пробелом"
    (home / ".codex" / "agents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local app"))
    fake = FakeCodex()
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/fake/bin/codex")
    monkeypatch.setattr(mod.subprocess, "Popen", fake.popen)
    monkeypatch.setattr(mod.subprocess, "run", fake.run)
    monkeypatch.setattr(mod, "_is_windows", lambda: False)
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(mod.os, "killpg", fake.killpg, raising=False)
    work = tmp_path / "рабочий каталог"
    work.mkdir()
    task = tmp_path / "задача с пробелом.txt"
    task.write_bytes(TASK.encode())
    [write_profile(home, role, full_profile(role)) for role in ROLES]
    return SimpleNamespace(
        mod=mod,
        fake=fake,
        home=home,
        work=work,
        task=task,
        out=tmp_path / "выход прогона",
        tmp=tmp_path,
        mp=monkeypatch,
    )


def run(env, *extra: str, role: str = "adversarial", out: bool = True) -> int:
    args = ["--role", role, "--task", str(env.task), "--cwd", str(env.work), *extra]
    return env.mod.main(args + (["--out-dir", str(env.out)] if out else []))


def printed_from(out: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)


def printed(capsys) -> dict[str, str]:
    return printed_from(capsys.readouterr().out)


def tail_pairs(argv: list[str]) -> list[str]:
    start = argv.index("--skip-git-repo-check") + 1
    return argv[start:]


def set_config(env, text: str | bytes | None, home: Path | None = None) -> Path:
    path = (home or env.home / ".codex") / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    elif text is not None:
        path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(("role", "fast"), [("adversarial", []), ("ops", ["-c", 'service_tier="fast"'])])
def test_role_profile_and_task_to_child(env, capsys, role, fast):
    code = run(env, role=role)
    paths = printed(capsys)
    proc = env.fake.procs[0]
    assert (code, Path(paths["last"]).is_absolute(), Path(paths["live"]).is_absolute()) == (0, True, True)
    assert proc.argv == [
        "/fake/bin/codex",
        "exec",
        "--json",
        "-m",
        f"gpt-{role}",
        *fast,
        "-c",
        'model_reasoning_effort="high"',
        "-s",
        "danger-full-access",
        "-C",
        str(env.work),
        "-o",
        paths["last"],
        "--skip-git-repo-check",
        "-",
    ]
    assert proc.inputs == [(f"Ты {role}.\n\n{TASK}", 7200.0)]


@pytest.mark.parametrize(
    "profile",
    [
        'model = " gpt-x "\n',
        'model = "gpt-x"\nmodel_reasoning_effort = ""\ndeveloper_instructions = "   "\n',
        'model = "gpt-x"\nmodel_reasoning_effort = "  "\ndeveloper_instructions = ""\n',
    ],
)
def test_stdin_and_optional_profile_fields(env, profile):
    write_profile(env.home, "adversarial", profile)
    env.mp.setattr(sys, "stdin", io.StringIO("  задача из stdin\n\n"))
    code = env.mod.main(["--role", "adversarial", "--task", "-", "--cwd", str(env.work), "--out-dir", str(env.out)])
    proc = env.fake.procs[0]
    assert code == 0
    assert proc.inputs[0][0] == "  задача из stdin\n\n"
    assert proc.argv[2:6] == ["--json", "-m", "gpt-x", "-s"]


@pytest.mark.parametrize(
    "args",
    [
        ["--task", "{task}", "--cwd", "{work}"],
        ["--role", "adversarial", "--cwd", "{work}"],
        ["--role", "adversarial", "--task", "{task}"],
        ["--role", "auditor", "--task", "{task}", "--cwd", "{work}"],
        ["--role", "researcher", "--task", "{task}", "--cwd", "{work}"],
        ["--rol", "adversarial", "--task", "{task}", "--cwd", "{work}"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--check", "x"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--model", "m"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--resume"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--sandbox", "full"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{tmp}/нет такого"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{task}"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--label", ".."],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--label", "a/b"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--label", "{tmp}"],
        ["--role", "adversarial", "--task", "{task}", "--cwd", "{work}", "--out-dir", "{task}"],
    ],
)
def test_invalid_arguments_do_not_launch(env, capsys, args):
    filled = [a.format(task=env.task, work=env.work, tmp=env.tmp) for a in args]
    code = env.mod.main(filled)
    assert code == 2
    assert env.fake.procs == []
    assert "ошибка аргументов:" in capsys.readouterr().err
    assert not (env.home / ".local").exists()


def test_help_reads_nothing(env, capsys):
    (env.home / ".codex" / "agents" / "adversarial.toml").unlink()
    code = env.mod.main(["--help"])
    assert (code, env.fake.procs) == (0, [])
    assert "--sandbox" in capsys.readouterr().out
    assert not (env.home / ".local").exists()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "abc", ""])
def test_timeout_validation(env, value):
    assert run(env, f"--timeout={value}") == 2
    assert env.fake.procs == []


def test_timeout_positive_value_reaches_child(env):
    assert run(env, "--timeout", "0.1") == 0
    assert env.fake.procs[0].inputs[0][1] == 0.1


@pytest.mark.parametrize("content", [None, b"\xff\xfe\x00", b"", b"  \n\t\n"])
def test_task_validation(env, capsys, content):
    env.task.unlink()
    _ = content is not None and env.task.write_bytes(content)
    assert run(env) == 2
    assert env.fake.procs == []
    assert "ошибка аргументов:" in capsys.readouterr().err


def test_whitespace_stdin_task_is_rejected(env):
    env.mp.setattr(sys, "stdin", io.StringIO(" \n "))
    code = env.mod.main(["--role", "adversarial", "--task", "-", "--cwd", str(env.work)])
    assert (code, env.fake.procs) == (2, [])


def test_task_with_bom_is_accepted(env):
    env.task.write_bytes(b"\xef\xbb\xbf" + TASK.encode())
    assert run(env) == 0
    assert env.fake.procs[0].inputs[0][0] == f"Ты adversarial.\n\n{TASK}"


@pytest.mark.parametrize(
    "profile",
    [
        None,
        f'model = "m"\ndeveloper_instructions = "{MARKER}\nбитая строка',
        f'developer_instructions = "{MARKER}"\n',
        'model = ""\n',
        'model = "   "\n',
        "model = 5\n",
        'model = "m"\nmodel_reasoning_effort = 3\n',
        f'model = "m"\ndeveloper_instructions = ["{MARKER}"]\n',
    ],
)
def test_profile_validation(env, capsys, profile):
    path = env.home / ".codex" / "agents" / "adversarial.toml"
    path.unlink()
    _ = profile is not None and write_profile(env.home, "adversarial", profile)
    code = run(env)
    err = capsys.readouterr().err
    assert (code, env.fake.procs) == (2, [])
    assert "ошибка профиля:" in err
    assert MARKER not in err


def test_missing_codex_binary(env, capsys):
    env.mp.setattr(env.mod.shutil, "which", lambda name: None)
    code = run(env)
    err = capsys.readouterr().err
    assert (code, env.fake.procs) == (3, [])
    assert "ошибка запуска Codex:" in err and "PATH" in err


@pytest.mark.parametrize("popen_error", [OSError("нет файла"), None])
def test_launch_and_artifact_errors(env, capsys, popen_error):
    env.fake.popen_error = popen_error
    env.fake.steps = [raising(OSError("сломалось"))]
    code = run(env)
    captured = capsys.readouterr()
    paths = printed_from(captured.out)
    assert code == 3
    assert Path(paths["live"]).is_absolute() and Path(paths["live"]).exists()
    assert "ошибка запуска Codex:" in captured.err and paths["live"] in captured.err


def test_artifact_dir_error(env, capsys):
    blocker = env.tmp / "файл-вместо-каталога"
    blocker.write_text("x", encoding="utf-8")
    env.out = blocker / "внутри"
    code = run(env)
    assert (code, env.fake.procs) == (3, [])
    assert "ошибка запуска Codex:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "step, phrase",
    [
        (answer(None), "Codex не оставил непустой ответ:"),
        (answer(b""), "Codex не оставил непустой ответ:"),
        (answer(b"  \n\t"), "Codex не оставил непустой ответ:"),
        (answer(b"\xff\xfe\xfa"), "Codex не оставил непустой ответ:"),
        (answer("ответ".encode(), rc=7), "Codex завершился кодом 7"),
    ],
)
def test_no_false_success(env, capsys, step, phrase):
    env.out.mkdir()
    (env.out / "last-старый.md").write_text("старый ответ", encoding="utf-8")
    env.fake.steps = [step]
    code = run(env)
    assert code == 3
    assert phrase in capsys.readouterr().err


def test_repeat_and_stale_answer(env, capsys):
    env.out.mkdir()
    old = env.out / "last-старый.md"
    old.write_text("старое", encoding="utf-8")
    env.fake.steps = [answer("первый".encode()), raising(OSError("x")), answer(None, rc=-9)]
    codes = (run(env, "--label", "b-1"), run(env, "--label", "b-1"))
    lasts = [line.split(": ", 1)[1] for line in capsys.readouterr().out.splitlines() if line.startswith("last: ")]
    assert codes == (0, 3)
    assert len(set(lasts)) == 2 and not Path(lasts[1]).exists()
    assert old.read_text(encoding="utf-8") == "старое"


def test_run_collision(env, capsys):
    env.out.mkdir()
    (env.out / "live-r1.log").write_text("чужой лог", encoding="utf-8")
    (env.out / "last-r2.md").write_text("чужой ответ", encoding="utf-8")
    ids = iter(["r1", "r2", "r3"])
    env.mp.setattr(env.mod, "_new_run_id", lambda: next(ids))
    assert run(env) == 0
    assert printed(capsys)["last"] == str(env.out / "last-r3.md")
    assert (env.out / "live-r1.log").read_text(encoding="utf-8") == "чужой лог"
    assert (env.out / "last-r2.md").read_text(encoding="utf-8") == "чужой ответ"


def test_switch_entity_and_directory(env, capsys):
    other = env.tmp / "другой каталог"
    other.mkdir()
    before = os.getcwd()
    first = run(env, "--label", "label-1", out=False)
    write_profile(env.home, "ops", 'model = "gpt-new"\n')
    env.work = other
    second = run(env, role="ops", out=False)
    lasts = [Path(p.last).parent for p in env.fake.procs]
    root = env.home / ".local" / "state" / "harness" / "findings"
    assert (first, second, os.getcwd()) == (0, 0, before)
    assert lasts == [root / "label-1", root / "ops"]
    assert [p.argv[p.argv.index("-C") + 1] for p in env.fake.procs] == [str(env.tmp / "рабочий каталог"), str(other)]
    assert env.fake.procs[1].argv[4] == "gpt-new"


def set_localappdata(env, value: str | None) -> None:
    if value is None:
        env.mp.delenv("LOCALAPPDATA", raising=False)
    else:
        env.mp.setenv("LOCALAPPDATA", value.format(tmp=env.tmp))


@pytest.mark.parametrize(
    "windows, local, expected",
    [
        (True, "{tmp}/custom", "{tmp}/custom/harness/findings/adversarial"),
        (True, None, "{home}/AppData/Local/harness/findings/adversarial"),
        (True, "", "{home}/AppData/Local/harness/findings/adversarial"),
        (False, "{tmp}/custom", "{home}/.local/state/harness/findings/adversarial"),
    ],
)
def test_artifact_roots(env, capsys, windows, local, expected):
    env.mp.setattr(env.mod, "_is_windows", lambda: windows)
    set_localappdata(env, local)
    assert run(env, out=False) == 0
    last = Path(printed(capsys)["last"])
    assert last.parent == Path(expected.format(tmp=env.tmp, home=env.home))


def test_relative_out_dir_uses_caller_cwd(env, capsys):
    env.mp.chdir(env.tmp)
    assert (
        env.mod.main(
            ["--role", "adversarial", "--task", env.task.name, "--cwd", str(env.work), "--out-dir", "отн выход"]
        )
        == 0
    )
    assert Path(printed(capsys)["last"]).parent == env.tmp / "отн выход"


@pytest.mark.parametrize(
    "sandbox, windows, unelevated",
    [
        ("read-only", True, True),
        ("workspace-write", True, True),
        ("danger-full-access", True, False),
        ("read-only", False, False),
        ("workspace-write", False, False),
        ("danger-full-access", False, False),
    ],
)
def test_platform_argv(env, sandbox, windows, unelevated):
    env.mp.setattr(env.mod, "_is_windows", lambda: windows)
    assert run(env, "--sandbox", sandbox) == 0
    argv = env.fake.procs[0].argv
    assert argv[argv.index("-s") + 1] == sandbox
    assert (tail_pairs(argv) == ["-c", 'windows.sandbox="unelevated"', "-"]) is unelevated


CONFIG = """
[mcp_servers.context7]
command = "npx"
[mcp_servers."extra.server"]
enabled = true
[mcp_servers.off]
enabled = false
[mcp_servers.'кв"о\\та']
url = "x"
[plugins."fixture-plugin@fixture-market"]
enabled = true
[plugins.offplug]
enabled = false
[plugins.noflag]
"""


def test_run_disables_configured_mcp_and_plugins(env):
    set_config(env, CONFIG)
    assert run(env) == 0
    pairs = tail_pairs(env.fake.procs[0].argv)
    assert pairs == [
        "-c",
        "mcp_servers.context7.enabled=false",
        "-c",
        'mcp_servers."extra.server".enabled=false',
        "-c",
        'mcp_servers."кв\\"о\\\\та".enabled=false',
        "-c",
        'plugins."fixture-plugin@fixture-market".enabled=false',
        "-",
    ]
    parsed = [tomllib.loads(value) for value in pairs[1:-1:2]]
    assert [list(d[next(iter(d))]) for d in parsed] == [
        ["context7"],
        ["extra.server"],
        ['кв"о\\та'],
        ["fixture-plugin@fixture-market"],
    ]


def prepare_config(env, source: str, text) -> None:
    if source == "codex_home":
        set_config(env, "[mcp_servers.home]\n")
        env.mp.setenv("CODEX_HOME", str(env.tmp / "ch"))
        set_config(env, text, env.tmp / "ch")
        return
    if source == "empty":
        env.mp.setenv("CODEX_HOME", "")
    if text == "DIR":
        (env.home / ".codex" / "config.toml").mkdir()
        return
    set_config(env, text)


@pytest.mark.parametrize(
    "source, text, expected",
    [
        ("codex_home", "[mcp_servers.own]\n", ["-c", "mcp_servers.own.enabled=false"]),
        ("empty", "[mcp_servers.home]\n", ["-c", "mcp_servers.home.enabled=false"]),
        ("absent", "[mcp_servers.home]\n", ["-c", "mcp_servers.home.enabled=false"]),
        ("absent", None, []),
        ("absent", f'[mcp_servers.x]\nurl = "{MARKER}\n', []),
        ("absent", b"[mcp_servers.x]\nurl = '\xff'\n", []),
        ("absent", "DIR", []),
        ("absent", "mcp_servers = 5\nplugins = [1]\n", []),
        ("absent", "[mcp_servers]\na = 1\n[plugins]\nb = true\n", ["-c", "mcp_servers.a.enabled=false"]),
    ],
)
def test_optional_config_sources_and_errors(env, capsys, source, text, expected):
    prepare_config(env, source, text)
    assert run(env) == 0
    assert tail_pairs(env.fake.procs[0].argv) == [*expected, "-"]
    assert MARKER not in capsys.readouterr().err


def test_config_reload_and_no_mutation(env):
    first = set_config(env, "[mcp_servers.a]\n", env.tmp / "h1")
    second = set_config(env, "[mcp_servers.b]\n[plugins.p]\nenabled = true\n", env.tmp / "h2")
    before = (first.read_bytes(), second.read_bytes())
    env.mp.setenv("CODEX_HOME", str(first.parent))
    run(env)
    env.mp.setenv("CODEX_HOME", str(second.parent))
    run(env)
    env.mp.setenv("CODEX_HOME", str(first.parent))
    run(env)
    pairs = [tail_pairs(p.argv) for p in env.fake.procs]
    assert pairs[0] == pairs[2] == ["-c", "mcp_servers.a.enabled=false", "-"]
    assert pairs[1] == ["-c", "mcp_servers.b.enabled=false", "-c", 'plugins."p".enabled=false', "-"]
    assert (first.read_bytes(), second.read_bytes()) == before


def test_config_values_are_not_forwarded(env, capsys):
    set_config(
        env,
        f'[mcp_servers.s]\nurl = "{MARKER}"\nenv = {{ K = "{MARKER}" }}\n'
        f'[plugins.p]\nenabled = true\nnote = "{MARKER}"\nmodel = "{MARKER}"\n',
    )
    assert run(env) == 0
    proc = env.fake.procs[0]
    captured = capsys.readouterr()
    log = Path(printed_from(captured.out)["live"]).read_text(encoding="utf-8")
    assert MARKER not in " ".join(proc.argv) + log + captured.out + captured.err
    assert proc.inputs[0][0] == f"Ты adversarial.\n\n{TASK}" and "gpt-adversarial" in proc.argv


def test_live_log_and_launch_options(env, capsys):
    assert run(env) == 0
    proc = env.fake.procs[0]
    log = Path(printed(capsys)["live"]).read_text(encoding="utf-8")
    assert log == "stdout-event\nstderr-event\n"
    assert {k: proc.kwargs.get(k) for k in ("stdin", "stderr", "shell", "text", "encoding", "start_new_session")} == {
        "stdin": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "shell": False,
        "text": True,
        "encoding": "utf-8",
        "start_new_session": True,
    }


def test_windows_launch_uses_own_process_group(env):
    env.mp.setattr(env.mod, "_is_windows", lambda: True)
    assert run(env) == 0
    kwargs = env.fake.procs[0].kwargs
    assert kwargs["creationflags"] & 0x200 and "start_new_session" not in kwargs


@pytest.mark.parametrize(
    "exc, code, phrase",
    [
        (subprocess.TimeoutExpired("codex", 7200), 124, "таймаут Codex:"),
        (KeyboardInterrupt(), 130, "отмена Codex"),
    ],
)
def test_timeout_and_cancel_stop_only_child(env, capsys, exc, code, phrase):
    env.fake.steps = [raising(exc), answer("поздний ответ".encode())]
    result = run(env)
    proc = env.fake.procs[0]
    assert result == code
    assert [pid for pid, _ in env.fake.killed] == [proc.pid]
    assert proc.inputs[1][0] is None and 0 < proc.inputs[1][1] <= 5
    assert phrase in capsys.readouterr().err


def test_windows_stop_uses_taskkill_for_own_pid(env):
    env.mp.setattr(env.mod, "_is_windows", lambda: True)
    env.fake.steps = [raising(subprocess.TimeoutExpired("codex", 1))]
    assert run(env) == 124
    cmd, kwargs = env.fake.runs[0]
    assert cmd == ["taskkill", "/T", "/F", "/PID", str(env.fake.procs[0].pid)]
    assert kwargs.get("shell") is False and 0 < kwargs["timeout"] <= 5


def failed_taskkill():
    return subprocess.CompletedProcess([], 1)


KILLED = answer(None, rc=-9)
STUCK = raising(subprocess.TimeoutExpired("codex", 5))


@pytest.mark.parametrize(
    "case",
    [
        # windows, ребёнок уже завершён, pgid (None - своя группа), шаг после сигнала, сигналов, предупреждение
        (False, True, None, KILLED, 0, False),
        (False, False, 1, KILLED, 0, True),
        (False, False, ProcessLookupError(), KILLED, 0, False),
        (False, False, None, STUCK, 1, True),
        (True, True, None, KILLED, 0, False),
        (True, False, None, KILLED, 1, True),
    ],
)
def test_cleanup_races_and_failure(env, capsys, case):
    windows, finished, pgid, after, signals, warned = case
    env.mp.setattr(env.mod, "_is_windows", lambda: windows)
    env.mp.setattr(env.mod.os, "getpgid", lambda pid: _pgid(pid, pgid), raising=False)
    env.fake.taskkill = failed_taskkill
    env.fake.steps = [raising(subprocess.TimeoutExpired("codex", 1), finished), after]
    code = run(env)
    pid = str(env.fake.procs[0].pid)
    assert code == 124
    assert len(env.fake.killed) + len(env.fake.runs) == signals
    assert (f"PID {pid}" in capsys.readouterr().err) is warned


def _pgid(pid, override):
    if isinstance(override, Exception):
        raise override
    return pid if override is None else override


def test_restart_is_new_run(env, capsys):
    env.out.mkdir()
    old_log = env.out / "live-старый.log"
    old_log.write_text("старый лог без ответа", encoding="utf-8")
    assert run(env) == 0
    paths = printed(capsys)
    assert paths["live"] != str(old_log) and len(env.fake.procs) == 1
    assert old_log.read_text(encoding="utf-8") == "старый лог без ответа"
