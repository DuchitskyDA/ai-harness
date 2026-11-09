"""Архитектурные пробы: поддерживаемые входы и штатный сбор тестов не тянут снятый движок."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "harness"
# notify_focus, search — одноимённые или оставленные модули, не снятый движок.
RETIRED = {
    "agent_profiles",
    "agent_lean_body",
    "agent_review_body",
    "agent_spec_body",
    "install_claude_session",
    "install_codex_hooks",
    "install_codex_plugins",
    "install_codex_config",
    "install_plugin_dirs",
    "inspect_runtime",
    "cli_smoke",
    "guard",
    "devinrun",
    "code_rules",
    "acp_client",
    "agent_worktrees",
    "beads_hygiene",
    "bulk_reader",
    "classifier",
    "claude_events",
    "claude_impl",
    "claude_profile",
    "code_rules_lint",
    "code_rules_measure",
    "codex_executor",
    "codex_hook_probe",
    "codex_impl",
    "codex_speed",
    "commit_marks",
    "context_pack",
    "devin_impl",
    "guard_test_support",
    "guardd",
    "guardd_pool",
    "guardd_protocol",
    "guardd_state",
    "heavy_slots",
    "heavy_test_command",
    "heavy_test_policy",
    "lsp_client",
    "lsp_preflight",
    "lsp_usage",
    "notify",
    "process_registry",
    "project_roots",
    "redcheck_record",
    "request_env",
    "role_cli",
    "role_codex",
    "role_codex_events",
    "role_codex_profile",
    "role_observe",
    "role_process",
    "role_prompt",
    "role_records",
    "role_tracker",
    "runner",
    "runner_bind",
    "runner_checks",
    "runner_code_rules",
    "runner_generated",
    "runner_roles",
    "runner_test_support",
    "semgrep_gate",
    "session_cleanup",
    "session_containers",
    "session_context",
    "session_processes",
    "skill_log",
    "beads_sync",
}
OLD_PATHS = {"agent_profiles.py", "inspect_runtime.py", "cli_smoke.py", "runner.py"}
RETIRED_BD_CODE = re.compile(r"\bbd_platform\b|\bensure_bd\b|bdshim|bd\.json|beads_sync")
ENTRYPOINTS = [
    "install.py",
    "install_repo.py",
    "install_files.py",
    "validate_install.py",
    "hook_spec.py",
    "codex.py",
    "install_auto_mode.py",
    "hooks/session_start.py",
    "hooks/notification.py",
    "hooks/session_end.py",
]


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _call_names(node: ast.Call) -> list[str]:
    func = node.func
    if isinstance(func, (ast.Attribute, ast.Name)) and getattr(func, "attr", getattr(func, "id", "")) == (
        "spec_from_file_location"
    ):
        # загрузка по файлу удаляемого пути
        return [
            Path(a.value).stem
            for a in ast.walk(node)
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and Path(a.value).name in OLD_PATHS
        ]
    if isinstance(func, ast.Attribute) and func.attr in {"append", "insert"}:
        # sys.path.append/insert(..., 'runtime')
        return ["guard" for a in ast.walk(node) if isinstance(a, ast.Constant) and a.value == "runtime"]
    return []


def _imported_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    if isinstance(node, ast.Call):
        return _call_names(node)
    return []


def legacy_edges(paths):
    found = []
    for path in paths:
        for node in ast.walk(_parse(path)):
            found += [
                f"{path.relative_to(ROOT).as_posix()}:{getattr(node, 'lineno', 0)}:{name}"
                for name in _imported_names(node)
                if name.split(".")[0] in RETIRED
            ]
    return sorted(found)


def test_supported_entrypoints_do_not_import_retired_engine():
    assert legacy_edges(HARNESS / p for p in ENTRYPOINTS) == []


def test_default_kernel_discovery_does_not_import_retired_compiler():
    tests = [
        p
        for p in HARNESS.rglob("test_*.py")
        if not any(part in {"runtime", "staging", "ui", "presets"} for part in p.relative_to(HARNESS).parts)
    ]
    assert legacy_edges(tests) == []


def test_entrypoints_do_not_use_bd():
    found = [
        f"{p}:{n}"
        for p in ENTRYPOINTS
        for n, line in enumerate((HARNESS / p).read_text(encoding="utf-8-sig").splitlines(), 1)
        if RETIRED_BD_CODE.search(line)
    ]
    assert found == []
