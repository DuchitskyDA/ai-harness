"""Установка оснастки в ~/.claude, ~/.codex и ~/.agents из этого checkout.

Без аргументов - план без записей и команд; --apply - установка. Готовые своды,
профили ролей и скиллы копируются байт-в-байт, ~/.harness - ссылка на
<checkout>/harness, settings.json сливается с фрагментом claude/settings.example.json.
Повтор с теми же входами ничего не меняет. Последняя строка stdout - JSON итога.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import install_repo
import tomllib
from hook_spec import merge_settings, render_settings, strip_owned_hooks
from install_files import InstallFiles, link_target, same_path, same_tree

# Прежний набор ролей (снятый компилятор agent_profiles): лишние профили снимаются с бэкапом.
LEGACY_ROLES = (
    "worker",
    "mechanical",
    "researcher",
    "auditor",
    "spec",
    "plan-reviewer",
    "complex",
    "ops",
    "ops-lite",
    "reviewer",
    "adversarial",
    "validator",
)
# Снятые из поставки скиллы (beads - 30.10): копия, ссылка и дубль Codex снимаются с бэкапом.
LEGACY_SKILLS = ("beads",)
BUILTIN_DENY = Path(".claude") / "harness-builtin-deny.json"
PAYLOAD = "/harness/snapshots/releases/"


class StepError(Exception):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(reason)
        self.step = step


# --- входы ----------------------------------------------------------------------------------


def read_json_object(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except ValueError as error:
        raise ValueError(f"{path}: invalid JSON ({error.__class__.__name__})") from None
    if not isinstance(data, dict):
        raise TypeError(f"{path}: expected JSON object")
    return data


def claude_defaults() -> list[str]:
    """Встроенные hard_deny из `claude auto-mode defaults`; любой сбой - отказ до записи."""
    claude = shutil.which("claude")
    if claude is None:
        raise ValueError("claude CLI not found on PATH")
    try:
        result = subprocess.run(
            [claude, "auto-mode", "defaults"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"claude auto-mode defaults: {type(error).__name__}") from None
    if result.returncode != 0:
        raise ValueError(f"claude auto-mode defaults exited {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise ValueError("claude auto-mode defaults: stdout is not JSON") from None
    hard = data.get("hard_deny") if isinstance(data, dict) else None
    if (
        not isinstance(hard, list)
        or not hard
        or not all(isinstance(rule, str) and rule.strip() and rule != "$defaults" for rule in hard)
    ):
        raise ValueError("claude auto-mode defaults: hard_deny is not a non-empty list of rules")
    return hard


def read_sources(repo: Path) -> dict:
    agents = sorted((repo / "claude" / "agents").glob("*.md"))
    codex = sorted((repo / "harness" / "profiles" / "codex").glob("*.toml"))
    for path in codex:
        tomllib.loads(path.read_text(encoding="utf-8"))
    skills = {d.name: d for d in sorted((repo / "claude" / "skills").iterdir()) if (d / "SKILL.md").is_file()}
    projects = repo / "claude" / "projects"
    memory = sorted(d / "memory" for d in projects.iterdir() if (d / "memory").is_dir()) if projects.is_dir() else []
    template = read_json_object(repo / "claude" / "settings.example.json")
    if not agents or not codex or not skills or template is None:
        raise ValueError(f"{repo}: ready roles, skills or settings template missing")
    return {
        "agents": agents,
        "codex": codex,
        "skills": skills,
        "memory": memory,
        "template": template,
        "digest": repo / "harness" / "DIGEST.md",
        "codex_digest": repo / "harness" / "DIGEST.codex.md",
    }


# --- скиллы ---------------------------------------------------------------------------------


def _own_skill_link(entry: Path, name: str, repo: Path) -> bool:
    """Ссылка на поставку: repo/claude/skills/<name> или payload снимка релиза."""
    target = link_target(entry)
    if target is None:
        return False
    text = target.replace("\\", "/").rstrip("/").lower()
    if not text.endswith(f"/claude/skills/{name.lower()}"):
        return False
    return (PAYLOAD in text and "/payload/" in text) or same_path(
        entry.parent / target, repo / "claude" / "skills" / name
    )


def legacy_claude_skills(home: Path, managed: dict) -> dict[str, Path]:
    """Пользовательские скиллы реального ~/.claude/skills, которых нет в canonical."""
    root, canonical = home / ".claude" / "skills", home / ".agents" / "skills"
    if link_target(root) is not None or not root.is_dir():
        return {}
    add = {}
    for entry in sorted(root.iterdir()):
        if entry.name in managed or entry.name in LEGACY_SKILLS or entry.name.startswith("."):
            continue
        if not (entry / "SKILL.md").is_file():
            continue
        existing = canonical / entry.name
        if not os.path.lexists(existing):
            add[entry.name] = entry
        elif not same_tree(entry, existing):
            raise ValueError(f"skill {entry.name}: {root} and {canonical} hold different versions")
    return add


def codex_duplicates(home: Path, repo: Path, managed: dict) -> list[Path]:
    """Дубли поставки в ~/.codex/skills: Codex видит ~/.agents/skills сам. Прочее - сохранить."""
    root = home / ".codex" / "skills"
    if link_target(root) is not None or not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        if entry.name not in managed:
            continue
        if _own_skill_link(entry, entry.name, repo) or same_tree(entry, managed[entry.name]):
            found.append(entry)
        else:
            print(f"skills: {entry} differs from the managed {entry.name}; left in place")
    return found


def retired_skills(home: Path, managed: dict) -> list[Path]:
    """Снятые скиллы поставки в ~/.agents/skills и в реальном ~/.codex/skills (ссылку на каталог не обходим)."""
    roots = [home / ".agents" / "skills"]
    if link_target(home / ".codex" / "skills") is None:
        roots.append(home / ".codex" / "skills")
    return [
        root / name for name in LEGACY_SKILLS if name not in managed for root in roots if os.path.lexists(root / name)
    ]


# --- шаги -----------------------------------------------------------------------------------


def preflight(home: Path, repo: Path, files: InstallFiles) -> dict:
    """Всё чтение и проверки до первой записи: исходники, текущие settings, defaults."""
    sources = read_sources(repo)
    install_repo.ensure_harness_root(home, lambda link, target: None, repo=repo)
    targets = [
        home / ".claude" / "CLAUDE.md",
        home / ".codex" / "AGENTS.md",
        home / ".claude" / "agents" / "x",
        home / ".codex" / "agents" / "x",
        home / ".agents" / "skills" / "x",
        home / ".claude" / "skills",
        home / ".claude" / "settings.json",
        home / BUILTIN_DENY,
        home / ".codex" / "hooks.json",
    ]
    targets += [home / ".claude" / "projects" / m.parent.name / "memory" for m in sources["memory"]]
    for target in targets:
        files.check(target)
    sources["legacy_skills"] = legacy_claude_skills(home, sources["skills"])
    sources["codex_dups"] = codex_duplicates(home, repo, sources["skills"])
    sources["retired_skills"] = retired_skills(home, sources["skills"])
    for path in sources["retired_skills"]:
        files.check(path)
    current = read_json_object(home / ".claude" / "settings.json") or {}
    previous = read_json_object_list(home / BUILTIN_DENY)
    codex_hooks = read_json_object(home / ".codex" / "hooks.json")
    if codex_hooks is not None and not isinstance(codex_hooks.get("hooks", {}), dict):
        raise ValueError(f"{home / '.codex' / 'hooks.json'}: hooks: expected object")
    builtins = claude_defaults()
    fragment = render_settings(sources["template"], home=home, repo=repo, windows=sys.platform == "win32")
    sources["settings"] = merge_settings(current, fragment, builtins, previous)
    sources["builtins"] = builtins
    sources["codex_hooks"] = codex_hooks
    return sources


def read_json_object_list(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except ValueError:
        raise ValueError(f"{path}: invalid JSON") from None
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{path}: expected list of strings")
    return data


def install_files_step(home: Path, files: InstallFiles, sources: dict) -> None:
    files.copy(sources["digest"], home / ".claude" / "CLAUDE.md")
    files.copy(sources["codex_digest"], home / ".codex" / "AGENTS.md")
    claude_agents, codex_agents = home / ".claude" / "agents", home / ".codex" / "agents"
    for path in sources["agents"]:
        files.copy(path, claude_agents / path.name)
    for path in sources["codex"]:
        files.copy(path, codex_agents / path.name)
    kept_claude = {p.stem for p in sources["agents"]}
    kept_codex = {p.stem for p in sources["codex"]}
    for name in LEGACY_ROLES:
        if name not in kept_claude:
            files.remove(claude_agents / f"{name}.md")
        if name not in kept_codex:
            files.remove(codex_agents / f"{name}.toml")


def install_skills(home: Path, files: InstallFiles, sources: dict) -> None:
    canonical = home / ".agents" / "skills"
    for name, source in {**sources["skills"], **sources["legacy_skills"]}.items():
        files.copy(source, canonical / name)
    files.link_directory(home / ".claude" / "skills", canonical)
    for stale in sources["codex_dups"] + sources["retired_skills"]:
        files.remove(stale)


def install_memory_links(home: Path, files: InstallFiles, sources: dict) -> None:
    for memory in sources["memory"]:
        files.link_directory(home / ".claude" / "projects" / memory.parent.name / "memory", memory)


def install_settings(home: Path, files: InstallFiles, sources: dict) -> None:
    files.write_json(home / ".claude" / "settings.json", sources["settings"])
    files.write_json(home / BUILTIN_DENY, sources["builtins"])
    codex = sources["codex_hooks"]
    if codex is not None:
        stripped = {**codex, "hooks": strip_owned_hooks(codex.get("hooks", {}))}
        if "hooks" not in codex and not stripped["hooks"]:
            stripped.pop("hooks")
        files.write_json(home / ".codex" / "hooks.json", stripped)


def run_apply(home: Path, repo: Path, files: InstallFiles, done: list[str]) -> None:
    step = "preflight"
    try:
        sources = preflight(home, repo, files)

        def link_harness(link: Path, target: Path) -> None:
            files.link_directory(link, target)

        steps = [
            (
                "harness link",
                lambda: install_repo.ensure_harness_root(home, link_harness, repo=repo),
            ),
            ("policy and roles", lambda: install_files_step(home, files, sources)),
            ("skills", lambda: install_skills(home, files, sources)),
            ("memory links", lambda: install_memory_links(home, files, sources)),
            ("settings", lambda: install_settings(home, files, sources)),
            ("git hooksPath", lambda: install_repo.remove_git_hooks(repo, files)),
        ]
        for step, action in steps:
            action()
            done.append(step)
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        raise StepError(step, str(error)) from None


def plan(home: Path, repo: Path) -> None:
    print(f"install plan: {repo} -> {home}")
    for line in (
        "~/.harness -> <checkout>/harness (symlink; junction on Windows without symlink privilege)",
        "~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, roles in ~/.claude/agents and ~/.codex/agents",
        "skills in ~/.agents/skills, ~/.claude/skills -> ~/.agents/skills",
        "~/.claude/settings.json merged with claude/settings.example.json",
        "core.hooksPath of the checkout removed",
        "nothing written; run with --apply",
    ):
        print("  " + line)


def main(argv: list[str] | None = None, *, home: Path | None = None, repo: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install.py", description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--apply", action="store_true", help="install (default: show the plan)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    home = Path(home or Path.home()).absolute()
    repo = Path(repo or Path(__file__).resolve().parents[1]).absolute()
    if not args.apply:
        plan(home, repo)
        return 0
    files, done = InstallFiles(home, repo), []
    try:
        run_apply(home, repo, files, done)
    except (StepError, KeyboardInterrupt) as error:
        summary = files.finish()
        if isinstance(error, KeyboardInterrupt):
            print("install: interrupted", file=sys.stderr)
        else:
            print(f"install: {error.step}: {error}", file=sys.stderr)
        if summary["backup"] or done:
            print(f"install: backup: {summary['backup']}; done: {', '.join(done) or 'none'}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    summary = files.finish()
    changed = summary["changed"]
    print(
        json.dumps(
            {
                "status": "installed" if changed else "unchanged",
                "changed": changed,
                "backup": summary["backup"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
