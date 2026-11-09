"""Пользовательские settings.json: рендер шаблона под платформу и чистое слияние.

Все функции чистые и возвращают свежие структуры. Свои handlers узнаются точно
(is_stale_harness_hook); чужие handlers, matcher и прочие ключи entry сохраняются.
"""

from __future__ import annotations

import copy
import os
import re
import shlex
import shutil
from collections.abc import Sequence
from pathlib import Path

_TEMPLATE_HOOK = re.compile(r'^python -X utf8 -B "\{\{CLAUDE_HOME\}\}/\.harness/hooks/([\w.]+)"$')
_OWN_PATH = re.compile(
    r"\.claude/gates/|\.harness/(?:runtime|hooks|guardd-client)/|/harness/runtime/"
    r"|/harness/guardd-client/|/harness/hooks/(?:session_start|notification|session_end)\.py"
    r'|/harness/snapshots/releases/[^/"]+/payload/harness/(?:runtime/|guardd-client/|notify\.py)'
)
# Хуки beads, которые ставила оснастка до 30.10 (beads снят): снимаются точной строкой.
RETIRED_BD_HOOKS = frozenset(
    {
        "bd prime --hook-json",
        "bd codex-hook SessionStart",
        "bd codex-hook PostCompact",
        "bd codex-hook UserPromptSubmit",
        "bd codex-hook PreCompact",
    }
)
RETIRED_ENV = ("HARNESS_HANDOFF_TOKENS", "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY")
# Бывшие управляемые allow: Bash/PowerShell allow срабатывает до классификатора autoMode,
# поэтому снятые из шаблона правила снимаются и с живой установки.
RETIRED_ALLOW = frozenset(
    {
        "PowerShell(npx tsc*)",
        "PowerShell(npm run lint*)",
        "PowerShell(npm run test*)",
        "PowerShell(npm run typecheck*)",
        "PowerShell(npx eslint*)",
        "Bash(npx tsc*)",
        "Bash(npm run lint*)",
        "Bash(npm run test*)",
        "Bash(npm run typecheck*)",
        "Bash(npx eslint*)",
        "Bash(git fetch*)",
        "PowerShell(git branch*)",
        "PowerShell(git fetch*)",
        "PowerShell(git -C *)",
        "Bash(rg *)",
        "Bash(find *)",
        "Bash(git diff*)",
        "Bash(git log*)",
        "Bash(git show*)",
        "Bash(git stash list*)",
        "Bash(git stash show*)",
        "Bash(git branch -a*)",
        "Bash(git remote -v*)",
        "Bash(git grep *)",
        "PowerShell(git diff*)",
        "PowerShell(git log*)",
        "PowerShell(git show*)",
        "PowerShell(git stash list*)",
        "PowerShell(git stash show*)",
        "PowerShell(git remote -v*)",
    }
)
# Бывшие управляемые строки autoMode.environment (install_auto_mode.py до rebuild).
RETIRED_ENVIRONMENT = frozenset(
    {
        "Owner-authorized autonomous project work. Read-only SSH, production databases, credentials and access configuration may be inspected. Do not disclose secrets.",
        "Mandatory shared workflow and boundaries: ~/.harness/POLICY.md and skill flow. Never bypass a blocking hook or permissions.",
    }
)
AUTO_LISTS = ("environment", "allow", "soft_deny", "hard_deny")
AUTO_SCALARS = {"classifyAllShell": bool}
DICT_KEYS = (
    "env",
    "attribution",
    "enabledPlugins",
    "modelSettings",
    "permissions",
    "hooks",
    "extraKnownMarketplaces",
    "statusLine",
    "autoMode",
)


def hook_text(handler: object) -> str:
    """Командная строка handler в обеих формах: `command` или `command` + `args`."""
    if not isinstance(handler, dict):
        return ""
    command = str(handler.get("command") or "")
    args = handler.get("args")
    if isinstance(args, list) and args:
        command += " " + " ".join(f'"{a}"' for a in args)
    return command


def is_stale_harness_hook(handler: object) -> bool:
    """Свой handler любой прежней или текущей формы: gates, runtime, hooks, guardd-client,
    payload снимков, прежние хуки beads точной строкой. Чужой hooks.py/runtime/bd - не свой."""
    text = hook_text(handler)
    if text.strip() in RETIRED_BD_HOOKS:
        return True
    return bool(_OWN_PATH.search(text.replace("\\", "/").lower()))


def find_bash(windows: bool) -> str:
    if windows:
        git = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        return str(git) if git.is_file() else (shutil.which("bash") or str(git))
    return shutil.which("bash") or "/bin/bash"


def _render_handler(handler: dict, home: Path, windows: bool) -> dict:
    match = _TEMPLATE_HOOK.match(str(handler.get("command", "")))
    if match is None:
        raise ValueError(f"template hook of unknown form: {handler.get('command')!r}")
    script = home / ".harness" / "hooks" / match.group(1)
    rest = {k: v for k, v in handler.items() if k != "command"}
    if windows:
        return {"type": "command", "command": "python", "args": ["-X", "utf8", "-B", str(script)], **rest}
    return {**rest, "command": f"python3 -X utf8 -B {shlex.quote(script.as_posix())}"}


def render_settings(fragment: dict, *, home: Path, repo: Path, windows: bool) -> dict:
    """Шаблон с подставленными путями; меняется только платформенная форма."""
    result = copy.deepcopy(fragment)
    for entries in result.get("hooks", {}).values():
        for entry in entries:
            entry["hooks"] = [_render_handler(h, home, windows) for h in entry["hooks"]]
    script = repo / "claude" / "statusline-command.sh"
    bash = find_bash(windows)
    if windows:
        line = f'"{bash}" "{script.as_posix()}"'
    else:
        line = f"{shlex.quote(bash)} {shlex.quote(script.as_posix())}"
    result["statusLine"] = {**result.get("statusLine", {}), "command": line}
    return result


def strip_owned_hooks(hooks: dict) -> dict:
    """Снять свои handlers; смешанная entry сохраняет чужие, пустые свои entry/event уходят."""
    result = {}
    for event, entries in hooks.items():
        kept = []
        for entry in entries:
            handlers = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(handlers, list):
                kept.append(copy.deepcopy(entry))
                continue
            foreign = [copy.deepcopy(h) for h in handlers if not is_stale_harness_hook(h)]
            if foreign or not handlers:
                kept.append({**copy.deepcopy(entry), "hooks": foreign})
        if kept or not entries:
            result[event] = kept
    return result


def _stable(*groups) -> list:
    seen: list = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.append(item)
    return seen


def _field(data: dict, name: str, kind: type, where: str):
    value = data.get(name)
    if value is not None and not isinstance(value, kind):
        raise ValueError(f"{where}{name}: expected {kind.__name__}")
    return value


def _list_of(value, kind: type, where: str, label: str) -> None:
    if value is not None and not (isinstance(value, list) and all(isinstance(v, kind) for v in value)):
        raise ValueError(f"{where}: expected list of {label}")


def validate_settings(current: dict) -> None:
    """Типы ключей, которые слияние трогает; неверный тип - отказ с именем поля."""
    if not isinstance(current, dict):
        raise TypeError("settings: expected object")
    for name in DICT_KEYS:
        _field(current, name, dict, "settings.")
    _list_of((current.get("permissions") or {}).get("allow"), str, "settings.permissions.allow", "strings")
    for event, entries in (current.get("hooks") or {}).items():
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ValueError(f"settings.hooks.{event}: expected list of objects")
    for name, value in (current.get("env") or {}).items():
        if not isinstance(value, str):
            raise TypeError(f"settings.env.{name}: expected string")
    auto = current.get("autoMode") or {}
    for name in AUTO_LISTS:
        _list_of(auto.get(name), str, f"settings.autoMode.{name}", "strings")
    for name, kind in AUTO_SCALARS.items():
        _field(auto, name, kind, "settings.autoMode.")


def _merge_auto(current: dict, fragment: dict, builtins: list[str], previous: list[str]) -> dict:
    result = copy.deepcopy(current)
    frag = {k: [v for v in fragment.get(k, []) if v != "$defaults"] for k in AUTO_LISTS}
    cur = {k: [v for v in current.get(k, []) if v != "$defaults"] for k in AUTO_LISTS}
    cur["environment"] = [v for v in cur["environment"] if v not in RETIRED_ENVIRONMENT]
    for name in ("environment", "allow"):
        result[name] = _stable(["$defaults"], frag[name], cur[name])
    owner_soft = [v for v in cur["soft_deny"] if v not in previous]
    owner_hard = [v for v in cur["hard_deny"] if v not in frag["hard_deny"]]
    result["soft_deny"] = _stable(["$defaults"], builtins, frag["soft_deny"], owner_soft, owner_hard)
    # пустой hard_deny Claude Code заменяет встроенными; заглушка шаблона держит список непустым
    result["hard_deny"] = frag["hard_deny"]
    return result


def _merge_env(current: dict, fragment: dict) -> dict:
    env = {k: v for k, v in current.items() if k not in RETIRED_ENV}
    env.update(fragment)
    return env


def _merge_models(current: dict, fragment: dict) -> dict:
    models = copy.deepcopy(current)
    for model, values in fragment.items():
        existing = models.get(model)
        models[model] = {**(existing if isinstance(existing, dict) else {}), **values}
    return models


def _merge_permissions(current: dict, fragment: dict) -> dict:
    permissions = copy.deepcopy(current)
    for name, value in fragment.items():
        permissions[name] = (
            _stable(value, [v for v in permissions.get(name, []) if v not in RETIRED_ALLOW])
            if name == "allow"
            else copy.deepcopy(value)
        )
    return permissions


def _merge_hooks(current: dict, fragment: dict) -> dict:
    hooks = strip_owned_hooks(current)
    for event, entries in fragment.items():
        present = {hook_text(h) for e in hooks.get(event, []) for h in e.get("hooks", [])}
        for entry in entries:
            if not all(hook_text(h) in present for h in entry["hooks"]):
                hooks.setdefault(event, []).append(copy.deepcopy(entry))
    return hooks


def merge_settings(current: dict, fragment: dict, builtins: list[str], previous_builtins: Sequence[str] = ()) -> dict:
    """current + отрендеренный fragment: управляемые значения - из fragment, остальное - владельца."""
    validate_settings(current)
    result = copy.deepcopy(current)
    if result.get("outputStyle") == "harness":
        del result["outputStyle"]
    for name, value in fragment.items():
        if not isinstance(value, dict):
            result[name] = copy.deepcopy(value)
    result["env"] = _merge_env(result.get("env", {}), fragment["env"])
    result["attribution"] = {**result.get("attribution", {}), **copy.deepcopy(fragment["attribution"])}
    result["enabledPlugins"] = {**result.get("enabledPlugins", {}), **fragment["enabledPlugins"]}
    result["modelSettings"] = _merge_models(result.get("modelSettings", {}), fragment["modelSettings"])
    result["permissions"] = _merge_permissions(result.get("permissions", {}), fragment["permissions"])
    result["statusLine"] = copy.deepcopy(fragment["statusLine"])
    result["hooks"] = _merge_hooks(result.get("hooks", {}), fragment["hooks"])
    result["autoMode"] = _merge_auto(
        result.get("autoMode", {}), fragment["autoMode"], builtins, list(previous_builtins)
    )
    return result
