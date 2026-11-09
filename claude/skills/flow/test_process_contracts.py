"""Контракты скиллов: заголовок загружается, у Codex есть алиасы, снятые инструменты не упоминаются.

Формулировки скиллов не сверяются: текст правится свободно (решение владельца 29.10).
"""

import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parent.parent
# Скиллы, написанные вне оснастки: их текст не наш, правила процесса к ним не относятся.
# В открытый каркас сторонние скиллы не входят; свои добавляй сюда.
EXTERNAL: set[str] = set()
RETIRED = {
    "runner.py": r"runner\.py",
    "старый runtime": r"~/\.harness/runtime/",
    "risky_paths": r"risky_paths\.json",
    "журнал мисфайров": r"guard-misfires\.tsv|мисфайр",
    "verify.json": r"verify\.json",
    "сторож": r"guardd|сторож",
    "spec-waive": r"spec-waive",
    "context_pack.py": r"context_pack\.py",
    "stall-timeout": r"stall-timeout",
}


def read(skill, name="SKILL.md"):
    return (SKILLS / skill / name).read_text(encoding="utf-8")


def frontmatter(text):
    head = text.split("---", 2)[1]
    return dict(re.findall(r"^([\w-]+): (.+)$", head, re.MULTILINE))


def own_markdown():
    for path in sorted(SKILLS.rglob("*.md")):
        if path.relative_to(SKILLS).parts[0] not in EXTERNAL:
            yield path


def violations(pattern):
    found = []
    for path in own_markdown():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(pattern, line):
                found.append(f"{path.relative_to(SKILLS).as_posix()}:{number}")
    return found


SKILL_NAMES = sorted(path.parent.name for path in SKILLS.glob("*/SKILL.md"))


@pytest.mark.parametrize("pattern", RETIRED.values(), ids=RETIRED.keys())
def test_retired_runtime_absent(pattern):
    assert violations(pattern) == []


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_skill_metadata(skill):
    meta = frontmatter(read(skill))
    assert (meta["name"], bool(meta["description"].strip())) == (skill, True)


@pytest.mark.parametrize("skill", ("grill-with-docs", "to-tickets"))
def test_codex_alias(skill):
    assert (SKILLS / skill / "agents" / "openai.yaml").is_file()
