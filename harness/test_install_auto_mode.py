import fnmatch
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_auto_mode as iam

TEMPLATE = Path(__file__).resolve().parents[1] / "claude" / "settings.example.json"

READ_ONLY_ALLOW = [
    "Bash(git status*)",
    "Bash(git rev-parse*)",
    "Bash(git ls-files*)",
    "Bash(git branch --list*)",
    "PowerShell(Get-Content*)",
    "PowerShell(Get-ChildItem*)",
    "PowerShell(Get-Item *)",
    "PowerShell(Get-ItemProperty*)",
    "PowerShell(Select-String*)",
    "PowerShell(Test-Path*)",
    "PowerShell(Get-Command*)",
    "PowerShell(Resolve-Path*)",
    "PowerShell(Get-Date*)",
    "PowerShell(Measure-Object*)",
    "PowerShell(Compare-Object*)",
    "PowerShell(ConvertFrom-Json*)",
    "PowerShell(git status*)",
    "PowerShell(git blame*)",
    "PowerShell(git ls-files*)",
    "PowerShell(git rev-parse*)",
    "Bash(ls*)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(wc *)",
    "Bash(git blame*)",
]

PLUGINS = {
    "superpowers@claude-plugins-official": False,
    "context7@claude-plugins-official": True,
    "typescript-lsp@claude-plugins-official": False,
    "pyright-lsp@claude-plugins-official": False,
    "claude-security@claude-plugins-official": True,
    "duckdb-skills@claude-plugins-official": True,
    "mcp-server-dev@claude-plugins-official": False,
    "semgrep@claude-plugins-official": False,
    "security-guidance@claude-code-plugins": False,
    "exa@claude-plugins-official": True,
    "firecrawl@claude-plugins-official": False,
    "sentry@claude-plugins-official": False,
    "cc-plugin-you-should-know@builtin": True,
    "warp@claude-code-warp": True,
}


def settings():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def test_settings_and_auto_mode_agree():
    assert settings()["autoMode"] == iam.auto_mode()


def test_settings_disable_bypass():
    perms = settings()["permissions"]
    assert perms.get("disableBypassPermissionsMode") == "disable"
    assert perms["defaultMode"] == "auto"
    assert "deny" not in perms and "ask" not in perms
    assert settings()["skipDangerousModePermissionPrompt"] is False


def test_auto_mode_defines_soft_boundaries():
    am = iam.auto_mode()
    assert set(am) == {"environment", "allow", "soft_deny", "hard_deny"}
    assert len(am["soft_deny"]) == 7
    assert any("infra" in r and "read-only access is allowed" in r for r in am["soft_deny"])
    assert any("develop" in r for r in am["soft_deny"])


def test_defaults_preserved():
    am = iam.auto_mode()
    assert [am[k][0] for k in ("environment", "allow", "soft_deny")] == ["$defaults"] * 3
    # единственное правило шаблона: строка allow через permit.py по «да» владельца
    assert len(am["allow"]) == 2 and "permit.py" in am["allow"][1] and "explicitly answered yes" in am["allow"][1]
    # пустой список Claude Code заменяет встроенными hard: нужна одна заглушка без запрета
    assert len(am["hard_deny"]) == 1 and "matches no action" in am["hard_deny"][0]
    assert "classifyAllShell" not in am


def test_auto_mode_returns_fresh_value():
    first = iam.auto_mode()
    first["soft_deny"].clear()
    first["extra"] = 1
    assert iam.auto_mode() == settings()["autoMode"]
    assert "extra" not in iam.auto_mode()


def test_auto_mode_missing_source(monkeypatch, tmp_path):
    monkeypatch.setattr(iam, "SETTINGS", tmp_path / "settings.example.json")
    with pytest.raises(FileNotFoundError):
        iam.auto_mode()


@pytest.mark.parametrize(
    "body, error",
    [
        ("{broken", json.JSONDecodeError),
        ('{"permissions": {}}', KeyError),
    ],
)
def test_auto_mode_invalid_source(monkeypatch, tmp_path, body, error):
    path = tmp_path / "settings.example.json"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(iam, "SETTINGS", path)
    with pytest.raises(error):
        iam.auto_mode()


def test_model_and_env():
    s = settings()
    assert (s["model"], s["effortLevel"]) == ("opus", "medium")
    assert s["env"] == {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "15000",
    }
    assert s["statusLine"] == {"type": "command", "command": "{{SH}} {{REPO}}/claude/statusline-command.sh"}


def hook(script, timeout):
    command = f'python -X utf8 -B "{{{{CLAUDE_HOME}}}}/.harness/hooks/{script}"'
    return [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]


def test_hook_events_and_timeouts():
    assert settings()["hooks"] == {
        "SessionStart": hook("session_start.py", 10),
        "Notification": hook("notification.py", 10),
        "Stop": hook("notification.py", 10),
        "UserPromptSubmit": [{"hooks": hook("notification.py", 10)[0]["hooks"] + hook("pr_merged.py", 30)[0]["hooks"]}],
        "PostToolUse": [
            {
                "matcher": "Bash|PowerShell",
                "hooks": hook("post_push.py", 15)[0]["hooks"] + hook("post_pr.py", 15)[0]["hooks"],
            }
        ],
        "SessionEnd": hook("session_end.py", 15),
    }
    assert settings()["env"]["CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"] == "15000"


@pytest.mark.parametrize(
    "legacy", ["bd prime", "resume.py", "runtime", "guardd", "HARNESS_HANDOFF_TOKENS", "CLAUDE_CODE_MAX"]
)
def test_no_legacy_hooks_or_limits(legacy):
    assert legacy not in TEMPLATE.read_text(encoding="utf-8")


def test_plugin_map():
    plugins = settings()["enabledPlugins"]
    assert plugins == PLUGINS


def test_permissions_keep_read_only_allow():
    assert settings()["permissions"]["allow"] == READ_ONLY_ALLOW


@pytest.mark.parametrize(
    "old",
    [
        "Bash(git fetch*)",
        "PowerShell(git fetch*)",
        "PowerShell(git branch*)",
        "PowerShell(git -C *)",
        "Bash(find *)",
        "Bash(rg *)",
        "Bash(npm run test*)",
        "Bash(*)",
        "PowerShell(*)",
        "Bash",
        "PowerShell",
    ],
)
def test_permissions_exclude_mutating_allow(old):
    assert old not in settings()["permissions"]["allow"]


# Команды, которые флагом пишут файл, запускают программу, удаляют или ходят в сеть.
WRITING = [
    "git diff --output=../infra/README.md",
    "git log --output=x",
    "git show --output=x",
    "git format-patch -o x HEAD~1",
    "git stash list --output=x",
    "git stash show --output=x",
    "git grep -Ocalc x",
    "git grep --open-files-in-pager=calc x",
    "git branch -a -D main",
    "git remote -v remove origin",
    "git fetch origin",
    "git -C ../infra push",
]


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("command", WRITING)
def test_allow_does_not_cover_writing_flags(tool, command):
    # Правило Tool(шаблон) срабатывает до классификатора: шаблон с * - glob по всей команде.
    rules = [r[len(tool) + 1 : -1] for r in settings()["permissions"]["allow"] if r.startswith(tool + "(")]
    assert [r for r in rules if fnmatch.fnmatchcase(command, r)] == []
