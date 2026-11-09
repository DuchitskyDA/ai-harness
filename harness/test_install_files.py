"""InstallFiles: сравнение до записи, ленивый бэкап, ссылки без записи сквозь них."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_files import (
    InstallFiles,
    UnsafePath,
    link_target,
    make_link,
    same_path,
)


@pytest.fixture
def env(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    (repo / "skills" / "s").mkdir(parents=True)
    (repo / "skills" / "s" / "SKILL.md").write_bytes(b"new")
    return home, repo, InstallFiles(home, repo)


def manifest(files):
    backup = Path(files.finish()["backup"])
    return backup, json.loads((backup / "manifest.json").read_text(encoding="utf-8"))


def test_fresh_write_has_no_backup(env):
    home, _, files = env
    assert files.write(home / "a" / "b.json", b"{}") is True
    assert files.finish() == {"changed": 1, "backup": None}


def test_write_json_compares_meaning(env):
    home, _, files = env
    target = home / "settings.json"
    target.write_bytes(b'{"a":1,   "b":[2]}')
    assert files.write_json(target, {"b": [2], "a": 1}) is False
    assert target.read_bytes() == b'{"a":1,   "b":[2]}'


def test_backup_restores_bytes(env):
    home, _, files = env
    target = home / "f.txt"
    target.write_bytes(b"original\r\n")
    files.write(target, b"new")
    _, data = manifest(files)
    assert data["schema"] == 1 and data["operations"][0]["kind"] == "file"
    shutil.copy2(Path(data["operations"][0]["backup"]), target)
    assert target.read_bytes() == b"original\r\n"


def test_copy_directory_replaces_with_backup(env):
    home, repo, files = env
    target = home / "skills" / "s"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old")
    assert files.copy(repo / "skills" / "s", target) is True
    assert files.copy(repo / "skills" / "s", target) is False
    backup, _ = manifest(files)
    assert (target / "SKILL.md").read_bytes() == b"new"
    assert (backup / "skills" / "s" / "SKILL.md").read_bytes() == b"old"


def test_link_replaced_by_copy_keeps_target(env, tmp_path):
    home, repo, files = env
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "SKILL.md").write_bytes(b"theirs")
    make_link(home / "s", foreign)
    files.copy(repo / "skills" / "s", home / "s")
    _, data = manifest(files)
    assert link_target(home / "s") is None and (foreign / "SKILL.md").read_bytes() == b"theirs"
    assert data["operations"] == [{"path": str((home / "s").resolve()), "kind": "link", "target": str(foreign)}]


def test_link_directory_correct_is_noop(env):
    home, repo, files = env
    assert files.link_directory(home / "l", repo / "skills") is True
    assert files.link_directory(home / "l", repo / "skills") is False
    assert same_path(home / "l", repo / "skills") and files.finish()["backup"] is None


windows = pytest.mark.skipif(sys.platform != "win32", reason="junction только на Windows")


def _junction(link, target):
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@windows
def test_make_link_prefers_symlink(env):
    # symlink - основная форма; junction - только без права на symlink
    home, repo, _ = env
    make_link(home / "l", repo / "skills")
    assert (home / "l").is_symlink() and same_path(home / "l", repo / "skills")


@windows
def test_make_link_falls_back_to_junction(env, monkeypatch):
    home, repo, _ = env

    def denied(*args, **kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", denied)
    make_link(home / "l", repo / "skills")
    assert not (home / "l").is_symlink() and same_path(home / "l", repo / "skills")


@windows
def test_link_directory_upgrades_junction_to_symlink(env):
    home, repo, files = env
    _junction(home / "l", repo / "skills")
    assert files.link_directory(home / "l", repo / "skills") is True
    assert (home / "l").is_symlink() and same_path(home / "l", repo / "skills")
    assert files.link_directory(home / "l", repo / "skills") is False


@windows
def test_link_directory_keeps_junction_without_privilege(env, monkeypatch):
    home, repo, files = env
    _junction(home / "l", repo / "skills")

    def denied(*args, **kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", denied)
    assert files.link_directory(home / "l", repo / "skills") is False
    assert same_path(home / "l", repo / "skills") and not (home / "l.symlink").exists()


def test_unsafe_targets_refused(env, tmp_path):
    home, repo, files = env
    make_link(home / "into-repo", repo)
    with pytest.raises(UnsafePath):
        files.write(home / "into-repo" / "x", b"x")
    with pytest.raises(UnsafePath):
        files.write(tmp_path / "outside.txt", b"x")
    assert not (repo / "x").exists() and not (tmp_path / "outside.txt").exists()


def test_remove_backs_up(env):
    home, _, files = env
    (home / "old.md").write_bytes(b"role")
    assert files.remove(home / "old.md") is True and files.remove(home / "old.md") is False
    backup, _ = manifest(files)
    assert (backup / "old.md").read_bytes() == b"role" and not os.path.lexists(home / "old.md")
