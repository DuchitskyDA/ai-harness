"""validate_install: read-only сверка установки с текущим контрактом; внешние операции fake."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install
import validate_install
from test_install import snapshot

pytest_plugins = ["test_install"]  # фикстуры home, repo, fake


@pytest.fixture
def installed(home, repo, fake):
    assert install.main(["--apply"], home=home, repo=repo) == 0
    return home, repo


def run(target: Path, checkout: Path, capsys):
    before = snapshot(target)
    code = validate_install.main(["--home", str(target), "--repo", str(checkout)])
    report = json.loads(capsys.readouterr().out)
    assert snapshot(target) == before, "validator changed the installation"
    return code, report


def test_validate_current_contract(installed, capsys):
    target, checkout = installed
    code, report = run(target, checkout, capsys)
    assert (code, report["ok"]) == (0, True), report


def test_validate_missing_role(installed, capsys):
    target, checkout = installed
    (target / ".claude" / "agents" / "worker.md").unlink()
    code, report = run(target, checkout, capsys)
    assert code == 1 and report["checks"]["files"] != "ok"


def test_validate_retired_skill(installed, capsys):
    target, checkout = installed
    (target / ".agents" / "skills" / "beads").mkdir()
    code, report = run(target, checkout, capsys)
    assert code == 1 and report["checks"]["skills"] != "ok"


def test_validate_broken_hook(installed, capsys):
    target, checkout = installed
    path = target / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    code, report = run(target, checkout, capsys)
    assert code == 1 and report["checks"]["hooks"] != "ok"


def test_validate_local_hooks_path(installed, fake, capsys):
    fake.hooks_path = ["harness/git-hooks"]
    target, checkout = installed
    code, report = run(target, checkout, capsys)
    assert code == 1 and report["checks"]["git"] != "ok"


def test_validate_bad_arguments():
    assert validate_install.main(["--nope"]) == 2
