"""PresetHook install: настоящая файловая система tmp_path, ответы Git подменены."""
import importlib
import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest
from presets_testkit import FakeRepo, run_main

FOREIGN = b"#!/bin/sh\necho original\n"


def load(name):
    assert importlib.util.find_spec(name) is not None, f"нет модуля {name}"
    return importlib.import_module(name)


@pytest.fixture
def hook_module():
    return load("pre_commit")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "it's dir ю"
    (root / ".git" / "hooks").mkdir(parents=True)
    return FakeRepo(root)


def quoted(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "'\\''") + "'"


def expected_wrapper(hook_module, hook: Path) -> bytes:
    script = Path(hook_module.__file__).resolve()
    backup = hook.with_name("pre-commit.harness-original")
    return ("#!/bin/sh\n# harness-preset-hook-v1\n"
            f"backup={quoted(backup)}\n"
            'if [ -x "$backup" ]; then\n  "$backup" "$@" || exit $?\nfi\n'
            f'exec python -X utf8 -B {quoted(script)} check --cwd .\n').encode()


def install(hook_module, repo, cwd=None):
    return run_main(hook_module.main, ["install", "--cwd", str(cwd or repo.root)], repo)


def snapshot(folder: Path):
    files = [p for p in folder.rglob("*") if p.is_file()]
    return sorted((p.relative_to(folder).as_posix(), p.read_bytes(), p.stat().st_mode) for p in files)


def foreign_hook(repo, mode=0o755):
    repo.hook.write_bytes(FOREIGN)
    os.chmod(repo.hook, mode)
    return repo.hook.stat().st_mode


def test_fresh_install_writes_wrapper(hook_module, repo):
    code, out, err = install(hook_module, repo, repo.root)
    assert (code, err) == (0, "")
    assert out == f"pre-commit: установлен {repo.hook}\n"
    assert repo.hook.read_bytes() == expected_wrapper(hook_module, repo.hook)
    assert os.access(repo.hook, os.X_OK)
    assert [p.name for p in repo.hook.parent.iterdir()] == ["pre-commit"]


def test_wrapper_uses_python_from_path(hook_module, repo):
    install(hook_module, repo)
    text = repo.hook.read_text(encoding="utf-8")
    assert "exec python -X utf8 -B" in text
    assert sys.executable not in text and str(repo.root) not in text


def test_install_from_subdirectory_creates_missing_hooks_dir(hook_module, tmp_path):
    repo = FakeRepo(tmp_path / "repo")
    (repo.root / "sub").mkdir(parents=True)
    (repo.root / ".git").mkdir()
    assert install(hook_module, repo, repo.root / "sub")[0] == 0
    assert repo.hook.read_bytes() == expected_wrapper(hook_module, repo.hook)


def test_repeat_install_is_noop_after_path_change(hook_module, repo, monkeypatch):
    install(hook_module, repo)
    before = snapshot(repo.hook.parent)
    monkeypatch.setenv("PATH", "")
    code, out, _ = install(hook_module, repo)
    assert (code, out) == (0, f"pre-commit: уже подключён {repo.hook}\n")
    assert snapshot(repo.hook.parent) == before


def test_foreign_hook_saved_as_backup(hook_module, repo):
    mode = foreign_hook(repo)
    assert install(hook_module, repo)[0] == 0
    backup = repo.hook.with_name("pre-commit.harness-original")
    assert (backup.read_bytes(), backup.stat().st_mode) == (FOREIGN, mode)
    assert repo.hook.read_bytes() == expected_wrapper(hook_module, repo.hook)


def test_linked_worktree_shares_common_hooks(hook_module, tmp_path):
    common = tmp_path / "main" / ".git"
    (common / "hooks").mkdir(parents=True)
    repo = FakeRepo(tmp_path / "wt", common=common)
    repo.root.mkdir()
    assert install(hook_module, repo)[0] == 0
    assert str(repo.root.as_posix()) not in repo.hook.read_text(encoding="utf-8")


def make_symlink(target: Path, link: Path):
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink недоступен: {error}")


def refuse(hook_module, repo, folder):
    before = snapshot(folder)
    code, out, err = install(hook_module, repo)
    assert (code, out) == (2, "")
    assert err.startswith("presets: ")
    assert snapshot(folder) == before


def test_backup_occupied_refused(hook_module, repo):
    foreign_hook(repo)
    repo.hook.with_name("pre-commit.harness-original").write_bytes(b"old")
    refuse(hook_module, repo, repo.hook.parent)


def test_unexpected_backup_without_hook_refused(hook_module, repo):
    repo.hook.with_name("pre-commit.harness-original").write_bytes(b"old")
    refuse(hook_module, repo, repo.hook.parent)


def test_changed_marker_refused(hook_module, repo):
    repo.hook.write_bytes(b"#!/bin/sh\n# harness-preset-hook-v1\nexec true\n")
    refuse(hook_module, repo, repo.hook.parent)


def test_symlink_hook_refused(hook_module, repo, tmp_path):
    target = tmp_path / "elsewhere.sh"
    target.write_bytes(FOREIGN)
    make_symlink(target, repo.hook)
    refuse(hook_module, repo, tmp_path)


@pytest.mark.parametrize("tracked", [".githooks/pre-commit", ".githooks/pre-commit.harness-original"])
def test_tracked_hook_or_backup_refused(hook_module, repo, tracked):
    repo.hook = repo.root / ".githooks" / "pre-commit"
    repo.hook.parent.mkdir()
    foreign_hook(repo)
    repo.tracked.add(tracked)
    refuse(hook_module, repo, repo.hook.parent)


def test_external_hooks_path_refused(hook_module, repo, tmp_path):
    repo.hook = tmp_path / "shared-hooks" / "pre-commit"
    refuse(hook_module, repo, tmp_path)


def test_dev_null_hooks_path_refused(hook_module, repo):
    repo.hook = Path("/dev/null/pre-commit")
    refuse(hook_module, repo, repo.root)


def test_failed_replace_restores_original(hook_module, repo, monkeypatch):
    mode = foreign_hook(repo, 0o700)
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    code, out, _ = install(hook_module, repo)
    assert (code, out) == (2, "")
    assert (repo.hook.read_bytes(), repo.hook.stat().st_mode) == (FOREIGN, mode)
    assert [p.name for p in repo.hook.parent.iterdir()] == ["pre-commit"]


def test_install_uses_only_read_only_git(hook_module, repo):
    install(hook_module, repo)
    assert {argv[1] for argv in repo.argvs("git")} <= {"rev-parse", "ls-files"}
    assert stat.S_ISREG(repo.hook.stat().st_mode)
