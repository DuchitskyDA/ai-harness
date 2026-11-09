"""Лёгкая проверка формата готовых профилей ролей: Claude Markdown и Codex TOML."""

import os
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = ROOT / "claude" / "agents"
CODEX_DIR = ROOT / "harness" / "profiles" / "codex"

CLAUDE = {
    "auditor": ("claude-sonnet-5-5", "medium"),
    "researcher": ("claude-haiku-5-5", "medium"),
    "worker": ("claude-opus-5-5", "high"),
    "mechanical": ("claude-sonnet-5-5", "low"),
}
CODEX = {
    "adversarial": ("gpt-6.1-sol", "xhigh"),
    "ops": ("gpt-6.1-sol", "medium"),
}
FORBIDDEN = ("tools", "disallowedTools", "hooks", "permissionMode", "omitClaudeMd", "skills")


def _claude_profile(path: Path) -> tuple[dict[str, str], str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("нет разделителей frontmatter")
    end = lines.index("---", 1)
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(": ")
        if not sep or not value.strip() or ":" in value or key in meta:
            raise ValueError(f"плохая строка: {line!r}")
        meta[key] = value.strip()
    return meta, "\n".join(lines[end + 1 :]).strip()


def _codex_profile(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "x.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_claude_role_set():
    assert {p.stem for p in CLAUDE_DIR.glob("*.md")} == set(CLAUDE)


@pytest.mark.parametrize("role", CLAUDE)
def test_claude_model_effort(role):
    meta, _ = _claude_profile(CLAUDE_DIR / f"{role}.md")
    assert meta["name"] == role
    assert (meta["model"], meta["effort"]) == CLAUDE[role]


@pytest.mark.parametrize("role", CLAUDE)
def test_claude_frontmatter(role):
    meta, body = _claude_profile(CLAUDE_DIR / f"{role}.md")
    assert set(meta) == {"name", "description", "model", "effort"}
    assert body
    assert not set(FORBIDDEN) & set(meta)


def test_codex_role_set():
    assert {p.stem for p in CODEX_DIR.glob("*.toml")} == set(CODEX)


@pytest.mark.parametrize("role", CODEX)
def test_codex_model_effort(role):
    path = CODEX_DIR / f"{role}.toml"
    assert path.exists()
    data = _codex_profile(path)
    assert data["name"] == role
    assert (data["model"], data["model_reasoning_effort"]) == CODEX[role]


@pytest.mark.parametrize("role", CODEX)
def test_codex_schema(role):
    data = _codex_profile(CODEX_DIR / f"{role}.toml")
    assert set(data) == {"name", "description", "model", "model_reasoning_effort", "developer_instructions"}
    assert all(isinstance(v, str) and v.strip() for v in data.values())


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: \ndescription: d\nmodel: m\neffort: e\n---\nb\n",
        "---\nname: a\nname: a\ndescription: d\nmodel: m\neffort: e\n---\nb\n",
        "---\nname: a\ndescription: d\nmodel: m\neffort: e\nb\n",
        "---\nname: a\ndescription: d\nmodel: m\neffort: e\npermissions: x\n---\nb\n",
    ],
)
def test_frontmatter_rejects_invalid(tmp_path, text):
    meta_ok = True
    try:
        meta, _ = _claude_profile(_write(tmp_path, text))
        meta_ok = set(meta) == {"name", "description", "model", "effort"}
    except ValueError:
        meta_ok = False
    assert not meta_ok


def test_codex_invalid_toml(tmp_path):
    p = tmp_path / "x.toml"
    p.write_text("name = ", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        _codex_profile(p)


def test_profile_reading_ignores_cwd(tmp_path, monkeypatch):
    path = CLAUDE_DIR / "worker.md"
    before = _claude_profile(path)
    monkeypatch.chdir(tmp_path)
    assert os.getcwd() == str(tmp_path)
    assert _claude_profile(path) == before
