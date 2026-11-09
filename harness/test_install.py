"""Новая установка: временный HOME, копия репозитория, все внешние операции fake."""

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

HARNESS = Path(__file__).resolve().parent
REPO = HARNESS.parent
sys.path.insert(0, str(HARNESS))
import hook_spec
import install
import install_files

BUILTINS = ["Fixture rule alpha\nsecond line", "Fixture rule beta"]
# пустой hard_deny Claude Code читает как «не задано» и возвращает встроенные (проверено 21.10)
HARD_STUB = json.loads((REPO / "claude" / "settings.example.json").read_text(encoding="utf-8"))["autoMode"]["hard_deny"]
REPO_PARTS = (
    "claude/agents",
    "claude/skills",
    "claude/settings.example.json",
    "claude/statusline-command.sh",
    "harness/DIGEST.md",
    "harness/DIGEST.codex.md",
    "harness/profiles",
)


def make_repo(root: Path) -> Path:
    for part in REPO_PARTS:
        source, target = REPO / part, root / part
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return root


@pytest.fixture(scope="session")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("src") / "repo")


class Fake:
    """subprocess.run и shutil.which установщика: только claude defaults и git config."""

    def __init__(self, builtins=None, hooks_path=()):
        self.defaults = {
            "allow": [],
            "soft_deny": [],
            "environment": [],
            "hard_deny": list(BUILTINS if builtins is None else builtins),
        }
        self.hooks_path, self.calls = list(hooks_path), []
        self.stdout = None

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if list(argv[-2:]) == ["auto-mode", "defaults"]:
            stdout = self.stdout if self.stdout is not None else json.dumps(self.defaults)
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        if argv[0] == "git" and "--get-all" in argv:
            values = "".join(v + "\n" for v in self.hooks_path)
            return subprocess.CompletedProcess(argv, 0 if values else 1, values, "")
        if argv[0] == "git" and "--unset-all" in argv:
            self.hooks_path = []
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    fake = Fake()
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(shutil, "which", lambda name: {"claude": "claude", "git": "git"}.get(name))
    return fake


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def apply(home, repo, capsys=None, *args) -> tuple[int, dict]:
    code = install.main(["--apply", *args], home=home, repo=repo)
    result = {}
    if capsys is not None and code == 0:
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    elif capsys is not None:
        capsys.readouterr()
    return code, result


def settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def put_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def snapshot(root):
    """Листинг, bytes и mtime всего дерева без каталога бэкапов; ссылки не обходятся."""
    state = {}
    for current, dirs, names in os.walk(root):
        base = Path(current)
        dirs[:] = [d for d in dirs if d != "backups" and install_files.link_target(base / d) is None]
        for name in names:
            path = base / name
            state[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
    return state


def own_handlers(data, event):
    return [h for e in data["hooks"].get(event, []) for h in e["hooks"] if hook_spec.is_stale_harness_hook(h)]


# --- Risky: регрессионные свидетели base -------------------------------------------------


def test_identical_write_does_not_touch_file_or_backup(home, repo):
    target = home / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_bytes(b"{}\n")
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    files = install_files.InstallFiles(home, repo)
    assert files.write(target, b"{}\n") is False
    assert target.stat().st_mtime_ns == 1_000_000_000, "identical write changed mtime"
    assert files.finish()["backup"] is None, "no-op created backup"


def test_builtin_hard_deny_copied_before_emptying(home, repo, fake):
    assert apply(home, repo)[0] == 0
    auto = settings(home)["autoMode"]
    assert auto["soft_deny"][:3] == ["$defaults", *BUILTINS], "hard_deny texts not copied"
    assert auto["hard_deny"] == HARD_STUB, "empty hard_deny lets Claude Code restore builtins"
    assert any(argv[-2:] == ["auto-mode", "defaults"] for argv in fake.calls)


def imported_names(source):
    names = []
    for item in ast.walk(ast.parse(source)):
        if isinstance(item, ast.Import):
            names.extend(n.name for n in item.names)
        elif isinstance(item, ast.ImportFrom):
            names.append(item.module)
    return names


def test_installer_has_no_role_compiler_import():
    names = imported_names((HARNESS / "install.py").read_text(encoding="utf-8-sig"))
    assert "agent_profiles" not in names, "installer imports legacy compiler"


# --- Ситуация -> ожидаемо ------------------------------------------------------------------


def test_apply_fresh_home(home, repo, fake, capsys):
    code, result = apply(home, repo, capsys)
    data = settings(home)
    assert (code, result["status"]) == (0, "installed")
    counts = (
        len(list((home / ".claude" / "agents").glob("*.md"))),
        len(list((home / ".codex" / "agents").glob("*.toml"))),
        len(list((home / ".agents" / "skills").glob("*/SKILL.md"))),
    )
    assert counts == (4, 2, 6)
    assert install_files.same_path(home / ".harness", repo / "harness")
    assert (
        data["model"],
        data["effortLevel"],
        data["permissions"]["defaultMode"],
        data["permissions"]["disableBypassPermissionsMode"],
    ) == ("opus", "medium", "auto", "disable")
    assert data["autoMode"]["soft_deny"][:3] == ["$defaults", *BUILTINS] and data["autoMode"]["hard_deny"] == HARD_STUB


def test_codex_files_without_config(home, repo, fake):
    assert apply(home, repo)[0] == 0
    codex = home / ".codex"
    assert (codex / "AGENTS.md").read_bytes() == (repo / "harness" / "DIGEST.codex.md").read_bytes()
    assert sorted(p.name for p in (codex / "agents").iterdir()) == ["adversarial.toml", "ops.toml"]
    assert not (codex / "config.toml").exists()


def test_merge_preserves_owner_fields(home, repo, fake):
    owner = {
        "myKey": {"deep": [1]},
        "env": {"MINE": "1", "HARNESS_HANDOFF_TOKENS": "9"},
        "permissions": {"allow": ["Bash(make*)"], "ask": ["Bash(rm*)"]},
        "enabledPlugins": {"x@y": True},
        "modelSettings": {"claude-opus-5-5": {"effortLevel": "high", "z": 1}, "other": {"effortLevel": "high"}},
        "autoMode": {"soft_deny": ["C"], "custom": 5},
    }
    put_json(home / ".claude" / "settings.json", owner)
    apply(home, repo)
    assert apply(home, repo)[0] == 0
    data = settings(home)
    assert (data["myKey"], data["env"]["MINE"], "HARNESS_HANDOFF_TOKENS" in data["env"]) == ({"deep": [1]}, "1", False)
    assert data["permissions"]["ask"] == ["Bash(rm*)"] and "Bash(make*)" in data["permissions"]["allow"]
    assert (
        data["modelSettings"]["claude-opus-5-5"] == {"effortLevel": "medium", "autoCompactWindow": 500000, "z": 1}
        and data["modelSettings"]["other"] == {"effortLevel": "high"}
        and data["enabledPlugins"]["x@y"] is True
    )
    assert data["autoMode"]["soft_deny"].count("C") == 1 and data["autoMode"]["custom"] == 5


# allow прежних шаблонов (base 049b5a8 и rebuild), которых нет в текущем - без классификатора они опасны.
FORMER_ALLOW = [
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
]
FORMER_ENVIRONMENT = [
    "Owner-authorized autonomous project work. Read-only SSH, production databases, credentials and access configuration may be inspected. Do not disclose secrets.",
    "Mandatory shared workflow and boundaries: ~/.harness/POLICY.md and skill flow. Never bypass a blocking hook or permissions.",
]


def test_former_managed_allow_removed_foreign_kept(home, repo, fake):
    put_json(home / ".claude" / "settings.json", {"permissions": {"allow": [*FORMER_ALLOW, "Bash(make*)"]}})
    assert apply(home, repo)[0] == 0
    allow = settings(home)["permissions"]["allow"]
    assert [a for a in FORMER_ALLOW if a in allow] == [] and "Bash(make*)" in allow


def test_former_managed_environment_removed_foreign_kept(home, repo, fake):
    put_json(
        home / ".claude" / "settings.json",
        {"autoMode": {"environment": ["$defaults", *FORMER_ENVIRONMENT, "Owner line"]}},
    )
    assert apply(home, repo)[0] == 0
    env = settings(home)["autoMode"]["environment"]
    assert [e for e in FORMER_ENVIRONMENT if e in env] == [] and "Owner line" in env


def test_existing_hard_rules_preserved(home, repo, fake):
    put_json(home / ".claude" / "settings.json", {"autoMode": {"hard_deny": ["$defaults", "C"]}})
    assert apply(home, repo)[0] == 0
    auto = settings(home)["autoMode"]
    assert auto["soft_deny"][:3] == ["$defaults", *BUILTINS] and "C" in auto["soft_deny"]
    assert auto["hard_deny"] == HARD_STUB and auto["soft_deny"].count("$defaults") == 1


def test_hard_stub_not_moved_to_soft_on_reapply(home, repo, fake):
    apply(home, repo)
    assert apply(home, repo)[0] == 0
    auto = settings(home)["autoMode"]
    assert auto["hard_deny"] == HARD_STUB and not set(HARD_STUB) & set(auto["soft_deny"])


def test_defaults_verbatim_stable_union(home, repo, fake):
    fake.defaults["hard_deny"] = ["  Rule A\n  keeps\tlayout ", "Rule B", "  Rule A\n  keeps\tlayout "]
    assert apply(home, repo)[0] == 0
    assert settings(home)["autoMode"]["soft_deny"][:3] == ["$defaults", "  Rule A\n  keeps\tlayout ", "Rule B"]


def break_defaults(case, fake, monkeypatch):
    hard = {"nolist": None, "empty": [], "blank": [""], "object": [{}], "defaults": ["$defaults"]}
    if case == "missing":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    elif case in hard:
        fake.stdout = json.dumps({} if hard[case] is None else {"hard_deny": hard[case]})
    else:
        fake.stdout = "not json" if case == "json" else None
        errors = {"nonzero": None, "timeout": subprocess.TimeoutExpired("claude", 30), "oserror": OSError("boom")}

        def run(argv, **kwargs):
            if case in errors and errors[case] is not None and "auto-mode" in argv:
                raise errors[case]
            if case == "nonzero" and "auto-mode" in argv:
                return subprocess.CompletedProcess(argv, 3, "", "boom")
            return fake.run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize(
    "case", ["missing", "nonzero", "timeout", "oserror", "json", "nolist", "empty", "blank", "object", "defaults"]
)
def test_invalid_defaults_fail_closed(home, repo, fake, monkeypatch, case):
    target = home / ".claude" / "settings.json"
    put_json(target, {"autoMode": {"hard_deny": ["keep"]}})
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    break_defaults(case, fake, monkeypatch)
    assert apply(home, repo)[0] == 1
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert not (home / ".claude" / "CLAUDE.md").exists()


@pytest.mark.parametrize(
    "case",
    [
        ({"env": []}, "settings.env"),
        ({"permissions": {"allow": "x"}}, "settings.permissions.allow"),
        ({"autoMode": {"soft_deny": [1]}}, "settings.autoMode.soft_deny"),
        ({"autoMode": {"classifyAllShell": "yes"}}, "classifyAllShell"),
    ],
)
def test_invalid_settings_no_write(home, repo, fake, capsys, case):
    owner, field = case
    put_json(home / ".claude" / "settings.json", owner)
    assert apply(home, repo)[0] == 1
    assert field in capsys.readouterr().err
    assert not (home / ".claude" / "CLAUDE.md").exists()


OLD_GUARD = {
    "type": "command",
    "command": "python",
    "args": ["-X", "utf8", "-B", "C:\\Users\\u\\.harness\\runtime\\hooks.py"],
}
PAYLOAD_HOOK = {
    "type": "command",
    "command": 'python -X utf8 -B "C:\\Users\\u\\AppData\\Local\\harness\\'
    'snapshots\\releases\\r7\\payload\\harness\\notify.py"',
}
FOREIGN = {"type": "command", "command": "node /other/runtime/hooks.py"}


def test_hook_migration_preserves_foreign(home, repo, fake):
    put_json(
        home / ".claude" / "settings.json",
        {
            "hooks": {
                "SessionStart": [{"matcher": "startup", "extra": 1, "hooks": [FOREIGN, OLD_GUARD, PAYLOAD_HOOK]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [OLD_GUARD]}],
                "Stop": [{"hooks": [{"type": "command", "command": "bd prime --hook-json"}]}],
            }
        },
    )
    apply(home, repo)
    assert apply(home, repo)[0] == 0
    hooks = settings(home)["hooks"]
    assert hooks["SessionStart"][0] == {"matcher": "startup", "extra": 1, "hooks": [FOREIGN]}
    assert "PreToolUse" not in hooks
    assert {event: len(own_handlers(settings(home), event)) for event in hooks} == {
        "SessionStart": 1,
        "Notification": 1,
        "Stop": 1,
        "UserPromptSubmit": 2,
        "PostToolUse": 2,
        "SessionEnd": 1,
    }


def test_hook_ownership_is_narrow():
    foreign = [
        FOREIGN,
        {"command": "bd prime --stealth"},
        {"command": "python /x/notify.py"},
        {"command": "bd codex-hook SessionStart --extra"},
        {"command": "python /p/hooks.py"},
    ]
    owned = [
        OLD_GUARD,
        PAYLOAD_HOOK,
        {"command": "bd prime --hook-json"},
        {"command": "bd codex-hook PreCompact"},
        {"command": "python", "args": ["/Users/u/.harness/hooks/notification.py"]},
        {"command": "/Users/u/repo/harness/guardd-client/guardd-client --timeout 15"},
        {"command": 'python "C:/Users/u/.claude/gates/x.py"'},
    ]
    assert [hook_spec.is_stale_harness_hook(h) for h in foreign] == [False] * len(foreign)
    assert all(hook_spec.is_stale_harness_hook(h) for h in owned)


def test_codex_strip_only(home, repo, fake):
    hooks = home / ".codex" / "hooks.json"
    put_json(
        hooks,
        {
            "other": 1,
            "hooks": {
                "Stop": [{"hooks": [OLD_GUARD, FOREIGN]}],
                "SessionStart": [{"hooks": [PAYLOAD_HOOK]}],
                "PostCompact": [{"hooks": [{"command": "bd codex-hook PostCompact"}]}],
                "PreCompact": [
                    {
                        "matcher": "manual|auto",
                        "hooks": [{"command": "bd codex-hook PreCompact", "statusMessage": "x", "type": "command"}],
                    }
                ],
            },
        },
    )
    assert apply(home, repo)[0] == 0
    data = json.loads(hooks.read_text(encoding="utf-8"))
    assert data == {"other": 1, "hooks": {"Stop": [{"hooks": [FOREIGN]}]}}
    hooks.unlink()
    assert apply(home, repo)[0] == 0 and not hooks.exists()


def test_retired_lsp_disabled(home, repo, fake):
    put_json(
        home / ".claude" / "settings.json",
        {"enabledPlugins": {"typescript-lsp@claude-plugins-official": True, "mine@x": True}},
    )
    assert apply(home, repo)[0] == 0
    plugins = settings(home)["enabledPlugins"]
    assert (plugins["typescript-lsp@claude-plugins-official"], plugins["mine@x"]) == (False, True)


def test_retired_lsp_not_added_to_fresh_config(home, repo, fake):
    assert apply(home, repo)[0] == 0
    plugins = settings(home)["enabledPlugins"]
    assert plugins["typescript-lsp@claude-plugins-official"] is False


@pytest.mark.parametrize("style,expected", [("harness", None), ("mine", "mine")])
def test_output_style_migration(home, repo, fake, style, expected):
    put_json(home / ".claude" / "settings.json", {"outputStyle": style})
    assert apply(home, repo)[0] == 0
    assert settings(home).get("outputStyle") == expected
    assert not (home / ".claude" / "output-styles").exists()


def test_platform_paths(tmp_path, fake, monkeypatch):
    repo = make_repo(tmp_path / "clone в 'стороне'" / "repo")
    home = tmp_path / "дом o'neil"
    home.mkdir()
    before = snapshot(repo)
    monkeypatch.chdir(tmp_path)
    assert apply(home, repo)[0] == 0
    data = settings(home)
    handler = data["hooks"]["SessionEnd"][0]["hooks"][0]
    script = home / ".harness" / "hooks" / "session_end.py"
    assert handler_argv(handler) == [PYTHON, "-X", "utf8", "-B", str(script)]
    assert (repo / "claude" / "statusline-command.sh").as_posix() in data["statusLine"]["command"] or str(
        repo / "claude" / "statusline-command.sh"
    ) in shlex_split(data["statusLine"]["command"])
    assert snapshot(repo) == before


PYTHON = "python" if sys.platform == "win32" else "python3"


def handler_argv(handler):
    # на Windows хук - command + args, на POSIX - одна строка для shell
    if "args" in handler:
        return [handler["command"], *handler["args"]]
    return shlex_split(handler["command"])


def shlex_split(text):
    import shlex

    return shlex.split(text)


def test_render_posix_quotes_paths():
    template = json.loads((REPO / "claude" / "settings.example.json").read_text(encoding="utf-8"))
    home, repo = PurePosixPath("/Users/o'neil/дом"), PurePosixPath("/Volumes/my disk/repo")
    rendered = hook_spec.render_settings(template, home=cast(Path, home), repo=cast(Path, repo), windows=False)
    command = rendered["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert shlex_split(command) == ["python3", "-X", "utf8", "-B", f"{home}/.harness/hooks/notification.py"]
    assert shlex_split(rendered["statusLine"]["command"])[1] == "/Volumes/my disk/repo/claude/statusline-command.sh"


def test_harness_link(home, repo, fake, capsys):
    assert apply(home, repo)[0] == 0
    assert install_files.link_target(home / ".harness") is not None
    assert install_files.same_path(home / ".harness", repo / "harness")
    capsys.readouterr()
    code, result = apply(home, repo, capsys)
    assert (code, result["changed"]) == (0, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="junction только на Windows")
def test_harness_junction_upgraded_to_symlink(home, repo, fake):
    # junction - запасная форма: при праве на symlink повтор установки её заменяет
    import _winapi

    _winapi.CreateJunction(str(repo / "harness"), str(home / ".harness"))
    assert apply(home, repo)[0] == 0
    assert (home / ".harness").is_symlink()
    assert install_files.same_path(home / ".harness", repo / "harness")


def occupy(target, other, gone, kind):
    if kind == "real":
        shutil.copytree(other, target)
        return
    gone.mkdir()
    install_files.make_link(target, other if kind == "foreign" else gone)
    gone.rmdir()


@pytest.mark.parametrize("kind", ["real", "foreign", "dangling"])
def test_harness_conflict_preserves_data(home, repo, fake, tmp_path, kind):
    other = tmp_path / "other"
    (other / "data").mkdir(parents=True)
    (other / "data" / "db").write_bytes(b"data")
    target = home / ".harness"
    occupy(target, other, tmp_path / "gone", kind)
    assert apply(home, repo)[0] == 1
    assert (other / "data" / "db").read_bytes() == b"data"
    assert kind != "real" or (target / "data" / "db").read_bytes() == b"data"
    assert not (home / ".claude").exists()


def test_no_write_through_links(home, repo, fake, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    install_files.make_link(home / ".claude", outside)
    assert apply(home, repo)[0] == 1
    assert list(outside.iterdir()) == []


def test_no_write_through_file_link(home, repo, fake, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"mine")
    (home / ".claude").mkdir()
    try:
        os.symlink(outside, home / ".claude" / "CLAUDE.md")
    except OSError:
        pytest.skip("file symlinks need Developer Mode on Windows")
    assert apply(home, repo)[0] == 0
    assert outside.read_bytes() == b"mine"
    assert install_files.link_target(home / ".claude" / "CLAUDE.md") is None


def _skill(path, text="x"):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")


def test_skills_preserve_foreign(home, repo, fake, tmp_path):
    canonical, legacy = home / ".agents" / "skills", home / ".claude" / "skills"
    _skill(canonical / "mine")
    _skill(canonical / ".system" / "inner")
    payload = (
        tmp_path / "store" / "harness" / "snapshots" / "releases" / "r1" / "payload" / "claude" / "skills" / "flow"
    )
    _skill(payload, "old")
    install_files.make_link(canonical / "flow", payload)
    _skill(legacy / "own-x")
    shutil.copytree(repo / "claude" / "skills" / "flow", home / ".codex" / "skills" / "flow")
    _skill(home / ".codex" / "skills" / "foreign")
    assert apply(home, repo)[0] == 0
    assert (
        install_files.same_tree(canonical / "flow", repo / "claude" / "skills" / "flow")
        and (payload / "SKILL.md").read_text(encoding="utf-8") == "old"
    )
    assert [(canonical / n).is_dir() for n in ("mine", ".system", "own-x")] == [True] * 3
    assert install_files.same_path(legacy, canonical)
    assert sorted(p.name for p in (home / ".codex" / "skills").iterdir()) == ["foreign"]


@pytest.fixture(params=["link", "real"])
def retired_beads(request, home, tmp_path):
    """Установка до снятия beads (30.10): ссылка на payload, дубль Codex; real - ещё и реальный ~/.claude/skills."""
    payload = (
        tmp_path / "store" / "harness" / "snapshots" / "releases" / "r1" / "payload" / "claude" / "skills" / "beads"
    )
    _skill(payload, "old")
    (home / ".agents" / "skills").mkdir(parents=True)
    install_files.make_link(home / ".agents" / "skills" / "beads", payload)
    _skill(home / ".codex" / "skills" / "beads", "old")
    extra = {"link": None, "real": home / ".claude" / "skills" / "beads"}[request.param]
    _skill(extra or home / "elsewhere" / "beads", "older")
    return payload


def test_skills_retire_beads_removed(home, repo, fake, retired_beads):
    assert apply(home, repo)[0] == 0
    assert not os.path.lexists(home / ".agents" / "skills" / "beads")
    assert not os.path.lexists(home / ".codex" / "skills" / "beads")
    assert not os.path.lexists(home / ".claude" / "skills" / "beads")


def test_skills_retire_beads_keeps_payload_with_backup(home, repo, fake, retired_beads):
    assert apply(home, repo)[0] == 0
    assert (retired_beads / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert list((home / ".codex" / "backups").rglob("SKILL.md")), "retired skill removed without backup"


def test_skills_retire_beads_idempotent(home, repo, fake, retired_beads):
    assert apply(home, repo)[0] == 0
    assert apply(home, repo)[0] == 0 and not os.path.lexists(home / ".agents" / "skills" / "beads")


def test_skills_conflict_refused(home, repo, fake):
    _skill(home / ".agents" / "skills" / "dup", "one")
    _skill(home / ".claude" / "skills" / "dup", "two")
    assert apply(home, repo)[0] == 1
    assert (home / ".claude" / "skills" / "dup" / "SKILL.md").read_text(encoding="utf-8") == "two"
    assert not (home / ".claude" / "CLAUDE.md").exists()


def test_backup_only_changed(home, repo):
    changed, same = home / "a.txt", home / "b.txt"
    changed.write_bytes(b"old")
    same.write_bytes(b"same")
    os.utime(same, ns=(1_000_000_000, 1_000_000_000))
    files = install_files.InstallFiles(home, repo)
    assert (files.write(changed, b"new"), files.write(same, b"same")) == (True, False)
    backup = Path(files.finish()["backup"])
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert [op["path"] for op in manifest["operations"]] == [str(changed.resolve())]
    assert (backup / "a.txt").read_bytes() == b"old" and same.stat().st_mtime_ns == 1_000_000_000


def test_apply_twice_is_noop(home, repo, fake, capsys):
    apply(home, repo)
    capsys.readouterr()
    before, calls = snapshot(home), len(fake.calls)
    code, result = apply(home, repo, capsys)
    assert (code, result["changed"], result["backup"], result["status"]) == (0, 0, None, "unchanged")
    assert snapshot(home) == before
    assert [argv[-2:] for argv in fake.calls[calls:]] == [["auto-mode", "defaults"], ["--get-all", "core.hooksPath"]]


def test_defaults_version_change(home, repo, fake, capsys):
    apply(home, repo)
    fake.defaults["hard_deny"] = ["Rule new"]
    capsys.readouterr()
    code, result = apply(home, repo, capsys)
    assert (code, result["changed"] > 0, result["backup"] is not None) == (0, True, True)
    soft = settings(home)["autoMode"]["soft_deny"]
    assert soft[:2] == ["$defaults", "Rule new"] and not set(BUILTINS) & set(soft)
    assert apply(home, repo, capsys)[1]["changed"] == 0


def test_restart_after_partial_failure(home, repo, fake, capsys, monkeypatch):
    remove_git_hooks = install.install_repo.remove_git_hooks

    def failing(*args):
        raise RuntimeError("git config exited 1")

    monkeypatch.setattr(install.install_repo, "remove_git_hooks", failing)
    put_json(home / ".claude" / "settings.json", {"mine": 1})
    assert apply(home, repo)[0] == 1
    err = capsys.readouterr().err
    assert "install: git hooksPath: git config exited 1" in err and "backup: " in err and "settings" in err
    monkeypatch.setattr(install.install_repo, "remove_git_hooks", remove_git_hooks)
    assert apply(home, repo)[0] == 0
    assert len(own_handlers(settings(home), "Stop")) == 1 and settings(home)["mine"] == 1


def test_remove_local_hooks_path(home, repo, fake, capsys):
    fake.hooks_path = ["harness/git-hooks"]
    code, result = apply(home, repo, capsys)
    unset = [argv for argv in fake.calls if "--unset-all" in argv]
    assert code == 0 and unset == [
        ["git", "-C", str(repo.absolute()), "config", "--local", "--unset-all", "core.hooksPath"]
    ]
    manifest = json.loads((Path(result["backup"]) / "manifest.json").read_text(encoding="utf-8"))
    assert {
        "path": str(repo.absolute()),
        "kind": "git-config",
        "key": "core.hooksPath",
        "values": ["harness/git-hooks"],
    } in manifest["operations"]
    calls = len(fake.calls)
    assert apply(home, repo)[0] == 0 and not [a for a in fake.calls[calls:] if "--unset-all" in a]


def test_checkout_change_requires_preparation(home, tmp_path, fake):
    first, second = make_repo(tmp_path / "a"), make_repo(tmp_path / "b")
    assert apply(home, first)[0] == 0
    assert apply(home, second)[0] == 1
    assert install_files.same_path(home / ".harness", first / "harness")
    install_files._remove(home / ".harness")
    assert apply(home, second)[0] == 0 and install_files.same_path(home / ".harness", second / "harness")


def test_cli_contract(home, repo, fake, capsys):
    assert install.main([], home=home, repo=repo) == 0
    assert (list(home.iterdir()), fake.calls) == ([], [])
    assert install.main(["--help"], home=home, repo=repo) == 0
    assert install.main(["--check-projects"], home=home, repo=repo) == 2
    assert "nothing written" in capsys.readouterr().out


def plant(*paths):
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old", encoding="utf-8")


def test_retired_roles_removed(home, repo, fake):
    claude, codex = home / ".claude" / "agents", home / ".codex" / "agents"
    plant(
        claude / "spec.md",
        claude / "complex.md",
        claude / "mine.md",
        codex / "worker.toml",
        codex / "complex.toml",
        codex / "mine.toml",
    )
    assert apply(home, repo)[0] == 0
    assert sorted(p.stem for p in claude.iterdir()) == sorted(
        [p.stem for p in (repo / "claude" / "agents").glob("*.md")] + ["mine"]
    )
    assert sorted(p.stem for p in codex.iterdir()) == ["adversarial", "mine", "ops"]


def test_memory_links(home, fake, capsys, tmp_path):
    # память проектов в поставку не входит: проект-пример кладётся в копию клона
    repo = make_repo(tmp_path / "with-memory" / "repo")
    project = "my-app"
    (repo / "claude" / "projects" / project / "memory").mkdir(parents=True)
    memory = home / ".claude" / "projects" / project / "memory"
    memory.mkdir(parents=True)
    (memory / "old.md").write_text("old", encoding="utf-8")
    code, result = apply(home, repo, capsys)
    assert code == 0 and install_files.same_path(memory, repo / "claude" / "projects" / project / "memory")
    backup = Path(result["backup"]) / ".claude" / "projects" / project / "memory" / "old.md"
    assert backup.read_text(encoding="utf-8") == "old"


def test_shell_wrappers_call_installer():
    def code(name):
        text = (REPO / name).read_text(encoding="utf-8-sig")
        return "\n".join(line for line in text.splitlines() if not line.startswith("#"))

    sh, ps = code("install.sh"), code("install.ps1")
    assert 'harness/install.py" --apply "$@"' in sh and sh.count("install.py") == 1
    assert "install.py') --apply @args" in ps and ps.count("install.py") == 1
    assert "exit $LASTEXITCODE" in ps and "exec " in sh
