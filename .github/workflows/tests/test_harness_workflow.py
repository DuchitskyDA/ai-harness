"""Контракт CI оснастки: .github/workflows/harness-tests.yml (только YAML, без запуска)."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "harness-tests.yml"

PY = "python -X utf8 -B -m pytest harness claude .github/workflows/tests -q --ignore=harness/runtime --ignore=harness/staging"
PRESET = "node --test \"harness/presets/ts/test/*.test.mjs\""
SUITES = ["Run harness test suite", "Run preset test suite"]


@pytest.fixture(scope="module")
def workflow():
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.fixture(scope="module")
def steps(workflow):
    out = {}
    for step in workflow["jobs"]["test"]["steps"]:
        if "name" in step:
            assert step["name"] not in out, step["name"]
            out[step["name"]] = step
    return out


def names(workflow):
    return [s.get("name") for s in workflow["jobs"]["test"]["steps"]]


def runs(workflow):
    return [s["run"] for s in workflow["jobs"]["test"]["steps"] if "run" in s]


def test_workflow_is_mapping(workflow):
    assert isinstance(workflow, dict)


def test_push_main_only(workflow):
    assert workflow["on"] == {"push": {"branches": ["main"]}}


def test_linux_runner_without_dispatch_matrix(workflow):
    job = workflow["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-latest"
    assert "strategy" not in job


@pytest.mark.parametrize("relative", ["harness/git-hooks/pre-push", "harness/ci_local.py", "harness/test_ci_local.py"])
def test_local_full_push_gate_removed(relative):
    assert not (ROOT / relative).exists()


def test_python_suite_uses_surviving_roots(steps):
    assert steps["Run harness test suite"]["run"].strip() == PY


def test_no_global_lsp_install(workflow):
    assert not [r for r in runs(workflow) if "npm install -g" in r]


@pytest.mark.parametrize("name,command", [
    ("Run preset test suite", PRESET),
])
def test_package_suite(steps, name, command):
    assert steps[name]["run"].strip() == command
    assert "continue-on-error" not in steps[name]


def test_dependency_preparation_precedes_suites(workflow):
    order = names(workflow)
    prep = ["Install preset dependencies", "Install pytest"]
    assert max(order.index(p) for p in prep) < min(order.index(s) for s in SUITES)


def test_python_environment(steps):
    assert steps["Run harness test suite"]["env"] == {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONWARNINGS": "error::SyntaxWarning",
    }


def test_no_live_evals(workflow):
    assert not [r for r in runs(workflow) if "run_scenarios" in r or r.lstrip().startswith(("claude ", "codex "))]
