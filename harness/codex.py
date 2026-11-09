"""CodexRun: блокирующий запуск `codex exec` по профилю роли.

python -X utf8 -B ~/.harness/codex.py --role adversarial|ops --task <файл|->
    --cwd <каталог> [--label <id>] [--timeout <секунды>] [--sandbox ...] [--out-dir <каталог>]

Профиль ~/.codex/agents/<роль>.toml даёт model, effort и инструкции; инструкции
и task уходят в stdin. Ответ - last-<run>.md, вывод Codex - live-<run>.log.
Коды: 0 непустой ответ, 2 аргументы/профиль, 3 запуск/сбой, 124 таймаут, 130 отмена.
Фон - штатная фоновая задача оболочки вокруг этого процесса.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import tomllib

ROLES = ("adversarial", "ops")
FAST_ROLES = frozenset({"ops"})  # adversarial (ревью) — без Fast
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "danger-full-access"
DEFAULT_TIMEOUT = 7200.0
CLEANUP_BUDGET = 5.0
RUN_ATTEMPTS = 100
EXIT_OK, EXIT_USAGE, EXIT_FAIL, EXIT_TIMEOUT, EXIT_CANCEL = 0, 2, 3, 124, 130
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


class UsageError(Exception):
    """Ошибка аргументов, task или профиля - код 2."""


@dataclass(frozen=True)
class RoleProfile:
    model: str
    effort: str | None
    instructions: str
    fast: bool = False


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise UsageError(f"ошибка аргументов: {message}")


def _is_windows() -> bool:
    return sys.platform == "win32"


def _timeout(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не число: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("нужно конечное число больше нуля")
    return value


def _directory(text: str) -> Path:
    path = Path(text).expanduser().absolute()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"не каталог: {path}")
    return path


def _label(text: str) -> str:
    if not _SAFE_COMPONENT.fullmatch(text):
        raise argparse.ArgumentTypeError("допустимы [A-Za-z0-9][A-Za-z0-9._-]*")
    return text


def _out_dir(text: str) -> Path:
    path = Path(text).expanduser().absolute()
    if path.exists() and not path.is_dir():
        raise argparse.ArgumentTypeError(f"указывает на файл: {path}")
    return path


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="codex.py", allow_abbrev=False, description="Блокирующий запуск codex exec по профилю роли.")
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--task", required=True, help="UTF-8 файл поручения или '-' для stdin")
    parser.add_argument("--cwd", required=True, type=_directory)
    parser.add_argument("--label", type=_label, help="только метка каталога артефактов")
    parser.add_argument("--timeout", type=_timeout, default=DEFAULT_TIMEOUT)
    parser.add_argument("--sandbox", choices=SANDBOXES, default=DEFAULT_SANDBOX)
    parser.add_argument("--out-dir", type=_out_dir)
    return parser.parse_args(argv)


def _read_task(source: str) -> str:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"ошибка аргументов: task не прочитан ({type(exc).__name__}): {source}") from None
    text = text.removeprefix("﻿")
    if not text.strip():
        raise UsageError(f"ошибка аргументов: пустой task: {source}")
    return text


def _optional_str(data: dict, key: str, path: Path) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise UsageError(f"ошибка профиля: {key} должен быть строкой: {path}")
    return value


def _load_profile(role: str) -> RoleProfile:
    path = Path.home() / ".codex" / "agents" / f"{role}.toml"
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except OSError:
        raise UsageError(f"ошибка профиля: не найден или недоступен: {path}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise UsageError(f"ошибка профиля: битый TOML: {path}") from None
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        raise UsageError(f"ошибка профиля: нет непустой строки model: {path}")
    effort = _optional_str(data, "model_reasoning_effort", path).strip() or None
    instructions = _optional_str(data, "developer_instructions", path)
    return RoleProfile(model.strip(), effort, instructions, role in FAST_ROLES)


def _toml_quoted(name: str) -> str:
    return json.dumps(name, ensure_ascii=False)  # JSON-строка - корректная базовая строка TOML


def _codex_config() -> dict:
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home) if codex_home else Path.home() / ".codex"
    try:
        return tomllib.loads((base / "config.toml").read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def _disabled_config_args() -> list[str]:
    """Выключение MCP и плагинов пользовательского config.toml только на этот запуск."""
    data, args = _codex_config(), []
    servers = data.get("mcp_servers")
    for name, cfg in servers.items() if isinstance(servers, dict) else ():
        if not (isinstance(cfg, dict) and cfg.get("enabled") is False):
            key = name if _BARE_KEY.fullmatch(name) else _toml_quoted(name)
            args += ["-c", f"mcp_servers.{key}.enabled=false"]
    plugins = data.get("plugins")
    for name, cfg in plugins.items() if isinstance(plugins, dict) else ():
        if isinstance(cfg, dict) and cfg.get("enabled") is True:
            args += ["-c", f"plugins.{_toml_quoted(name)}.enabled=false"]
    return args


def _build_argv(profile: RoleProfile, cwd: Path, last_path: Path, sandbox: str, codex_bin: str) -> list[str]:
    argv = [codex_bin, "exec", "--json", "-m", profile.model]
    if profile.fast:
        argv += ["-c", 'service_tier="fast"']
    if profile.effort:
        argv += ["-c", f"model_reasoning_effort={_toml_quoted(profile.effort)}"]
    argv += ["-s", sandbox, "-C", str(cwd), "-o", str(last_path), "--skip-git-repo-check"]
    if _is_windows() and sandbox != "danger-full-access":
        argv += ["-c", 'windows.sandbox="unelevated"']  # elevated-песочница падает (codex#46388)
    return argv + _disabled_config_args() + ["-"]


def _findings_root() -> Path:
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "harness" / "findings"
    return Path.home() / ".local" / "state" / "harness" / "findings"


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex}"


def _reserve_run(out_dir: Path):
    """Новый run: live создаётся эксклюзивно, чужой live или last не перезаписываются."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(RUN_ATTEMPTS):
        run = _new_run_id()
        live, last = out_dir / f"live-{run}.log", out_dir / f"last-{run}.md"
        if last.exists():
            continue
        try:
            return live, last, live.open("xb")
        except FileExistsError:
            continue
    raise FileExistsError(f"не найден свободный run в {out_dir}")


def _left(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.01)


def _signal_child(proc: subprocess.Popen, deadline: float) -> bool:
    """Сигнал только своему ребёнку; False - остановить не удалось."""
    if _is_windows():
        done = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            shell=False,
            capture_output=True,
            timeout=_left(deadline),
            check=False,
        )
        return done.returncode == 0 or proc.poll() is not None
    if os.getpgid(proc.pid) != proc.pid:  # pyright: ignore[reportAttributeAccessIssue] - только POSIX
        return False  # группа не наша - не сигналим
    os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # pyright: ignore[reportAttributeAccessIssue]
    return True


def _stop_process(proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + CLEANUP_BUDGET
    if proc.poll() is not None:
        return
    try:
        stopped = _signal_child(proc, deadline)
        proc.communicate(timeout=_left(deadline))
    except ProcessLookupError:
        return  # ребёнок уже завершился
    except (OSError, subprocess.SubprocessError) as exc:
        stopped = False
        print(f"остановка Codex: {type(exc).__name__}", file=sys.stderr)
    if not stopped:
        print(f"остановка Codex не подтверждена: возможно, остался свой процесс PID {proc.pid}", file=sys.stderr)


def _popen_options() -> dict:
    if _is_windows():
        return {"creationflags": _NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _answer_ok(last: Path) -> bool:
    try:
        return bool(last.read_bytes().decode("utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def _communicate(argv: list[str], prompt: str, log, timeout: float, live: Path) -> int | None:
    """Запуск и ожидание; код выхода обёртки при сбое, None - ребёнок завершился сам."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            shell=False,
            **_popen_options(),
        )
    except OSError as exc:
        print(f"ошибка запуска Codex: {type(exc).__name__}: {exc.strerror}; лог: {live}", file=sys.stderr)
        return EXIT_FAIL
    try:
        proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_process(proc)
        print(f"таймаут Codex: {timeout} с; лог: {live}", file=sys.stderr)
        return EXIT_TIMEOUT
    except KeyboardInterrupt:
        _stop_process(proc)
        print(f"отмена Codex; лог: {live}", file=sys.stderr)
        return EXIT_CANCEL
    except OSError as exc:
        _stop_process(proc)
        print(f"ошибка запуска Codex: {type(exc).__name__}; лог: {live}", file=sys.stderr)
        return EXIT_FAIL
    if proc.returncode != 0:
        print(f"Codex завершился кодом {proc.returncode}; лог: {live}", file=sys.stderr)
        return EXIT_FAIL
    return None


def _run(args: argparse.Namespace, profile: RoleProfile, task: str, codex_bin: str) -> int:
    out_dir = args.out_dir or _findings_root() / (args.label or args.role)
    try:
        live, last, log = _reserve_run(out_dir)
    except OSError as exc:
        print(
            f"ошибка запуска Codex: каталог или артефакт не создан в {out_dir}: {type(exc).__name__}", file=sys.stderr
        )
        return EXIT_FAIL
    print(f"live: {live}\nlast: {last}", flush=True)
    instructions = profile.instructions.strip()
    prompt = f"{instructions}\n\n{task}" if instructions else task
    argv = _build_argv(profile, args.cwd, last, args.sandbox, codex_bin)
    with log:
        failed = _communicate(argv, prompt, log, args.timeout, live)
    if failed is not None:
        return failed
    if not _answer_ok(last):
        print(f"Codex не оставил непустой ответ: {last}; лог: {live}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse(argv)
        task = _read_task(args.task)
        profile = _load_profile(args.role)
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # --help
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    codex_bin = shutil.which("codex")
    if not codex_bin:
        print("ошибка запуска Codex: codex не найден - проверьте установку и PATH", file=sys.stderr)
        return EXIT_FAIL
    return _run(args, profile, task, codex_bin)


if __name__ == "__main__":
    sys.exit(main())
