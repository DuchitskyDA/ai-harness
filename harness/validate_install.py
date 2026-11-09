"""Read-only проверка установки против текущего контракта install.py; без записей и моделей.

Коды выхода: 0 - контракт выполнен, 1 - расхождение, 2 - неверные аргументы.
Свежесть встроенных правил autoMode другой версии CLI здесь не утверждается:
её проверяет живой `claude auto-mode config` у оркестратора.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import install
from hook_spec import hook_text, is_stale_harness_hook, render_settings
from install_files import link_target, same_path, same_tree


def _settings(home: Path) -> dict:
    return install.read_json_object(home / ".claude" / "settings.json") or {}


def check_link(home: Path, repo: Path, sources: dict) -> list[str]:
    target = home / ".harness"
    ok = link_target(target) is not None and same_path(target, repo / "harness")
    return [] if ok else [f"{target} is not a link to {repo / 'harness'}"]


def check_files(home: Path, repo: Path, sources: dict) -> list[str]:
    pairs = [
        (sources["digest"], home / ".claude" / "CLAUDE.md"),
        (sources["codex_digest"], home / ".codex" / "AGENTS.md"),
    ]
    pairs += [(p, home / ".claude" / "agents" / p.name) for p in sources["agents"]]
    pairs += [(p, home / ".codex" / "agents" / p.name) for p in sources["codex"]]
    problems = [
        f"{target} differs from {source}"
        for source, target in pairs
        if not target.is_file() or target.read_bytes() != source.read_bytes()
    ]
    retired = [
        home / ".claude" / "agents" / f"{n}.md"
        for n in install.LEGACY_ROLES
        if n not in {p.stem for p in sources["agents"]}
    ]
    retired += [
        home / ".codex" / "agents" / f"{n}.toml"
        for n in install.LEGACY_ROLES
        if n not in {p.stem for p in sources["codex"]}
    ]
    return problems + [f"retired role {p} present" for p in retired if p.exists()]


def check_skills(home: Path, repo: Path, sources: dict) -> list[str]:
    canonical = home / ".agents" / "skills"
    problems = [
        f"skill {name} differs from {source}"
        for name, source in sources["skills"].items()
        if not same_tree(source, canonical / name)
    ]
    claude = home / ".claude" / "skills"
    if link_target(claude) is None or not same_path(claude, canonical):
        problems.append(f"{home / '.claude' / 'skills'} is not a link to {canonical}")
    codex = home / ".codex" / "skills"
    problems += [f"retired skill {p} present" for p in install.retired_skills(home, sources["skills"])]
    return problems + [f"duplicate skill {codex / n}" for n, s in sources["skills"].items() if same_tree(codex / n, s)]


def check_hooks(home: Path, repo: Path, sources: dict) -> list[str]:
    expected = render_settings(sources["template"], home=home, repo=repo, windows=sys.platform == "win32")
    hooks = _settings(home).get("hooks") or {}
    problems = []
    for event in set(hooks) | set(expected["hooks"]):
        want = [h for e in expected["hooks"].get(event, []) for h in e["hooks"]]
        got = [h for e in hooks.get(event, []) for h in e.get("hooks", []) if is_stale_harness_hook(h)]
        if got != want:
            problems.append(f"{event}: own hooks {[hook_text(h) for h in got]} != {[hook_text(h) for h in want]}")
    codex = install.read_json_object(home / ".codex" / "hooks.json") or {}
    problems += [
        f"codex hooks.json: legacy {event}"
        for event, entries in (codex.get("hooks") or {}).items()
        if any(is_stale_harness_hook(h) for e in entries for h in e.get("hooks", []))
    ]
    return problems


def check_settings(home: Path, repo: Path, sources: dict) -> list[str]:
    data = _settings(home)
    permissions, auto = data.get("permissions") or {}, data.get("autoMode") or {}
    owner_rules = [r for r in sources["template"]["autoMode"]["soft_deny"] if r != "$defaults"]
    facts = {
        "model opus": data.get("model") == "opus",
        "effortLevel medium": data.get("effortLevel") == "medium",
        "defaultMode auto": permissions.get("defaultMode") == "auto",
        "bypass disabled": permissions.get("disableBypassPermissionsMode") == "disable",
        "typescript-lsp off": (data.get("enabledPlugins") or {}).get("typescript-lsp@claude-plugins-official") is False,
        "no outputStyle harness": data.get("outputStyle") != "harness",
        "SessionEnd timeout env": (data.get("env") or {}).get("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS") == "15000",
        "hard_deny only template stub": bool(auto.get("hard_deny"))
        and auto["hard_deny"] == sources["template"]["autoMode"]["hard_deny"],
        "soft_deny owner rules": (auto.get("soft_deny") or [None])[0] == "$defaults"
        and all(r in auto["soft_deny"] for r in owner_rules),
    }
    return [f"settings: {name}" for name, ok in facts.items() if not ok]


def check_git(home: Path, repo: Path, sources: dict) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get-all", "core.hooksPath"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode == 1 and not result.stdout.strip():
        return []
    return [f"core.hooksPath still set ({result.returncode}): {result.stdout.strip()}"]


CHECKS = (check_link, check_files, check_skills, check_hooks, check_settings, check_git)


def validate(home: Path, repo: Path) -> dict:
    sources = install.read_sources(repo)
    checks = {}
    for check in CHECKS:
        try:
            problems = check(home, repo, sources)
        except (OSError, ValueError, RuntimeError, LookupError, TypeError) as error:
            problems = [f"{type(error).__name__}: {error}"]
        checks[check.__name__.removeprefix("check_")] = problems or "ok"
    return {"ok": all(v == "ok" for v in checks.values()), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validate_install.py", description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    result = validate(args.home.absolute(), args.repo.absolute())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
