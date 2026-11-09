"""Свод как поставляемый артефакт: копия Claude, нет output style, правило туннеля дословно."""
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
REPO = HARNESS.parent
TUNNEL_HEADING = "## Туннели и установка"
TUNNEL_SECTION = (
    "## Туннели и установка — явное правило владельца\n\n"
    "- Сеть локальной машины не трогай: клиент туннеля и его ядро, адаптеры, маршруты, DNS, прокси, брандмауэр."
    " Запуск Docker (Desktop, Colima, контейнеры) разрешён: его сеть в другом диапазоне и туннель не задевает.\n"
    "- Ничего не устанавливай на компьютер владельца без его явного «да». Если устанавливаемое после «да» "
    "может хоть как-то задеть туннель — сеть, адаптеры, маршруты, DNS, сертификаты, прокси, перезагрузку, — "
    "скажи об этом до установки.")
DIGESTS = ("DIGEST.md", "DIGEST.codex.md")


def read(name):
    return (HARNESS / name).read_text(encoding="utf-8")


def section(text, heading):
    """Раздел от заголовка до следующего «\\n## » (или конца), без хвостовых пробелов."""
    start = text.index(heading)
    end = text.find("\n## ", start + 1)
    return (text[start:] if end < 0 else text[start:end]).rstrip()


def test_claude_source_matches_digest():
    assert (REPO / "claude" / "CLAUDE.md").read_bytes() == (HARNESS / "DIGEST.md").read_bytes()


def test_output_style_is_not_shipped():
    assert sorted((REPO / "claude" / "output-styles").rglob("*.md")) == []


def test_tunnel_section_verbatim():
    assert [section(read(name), TUNNEL_HEADING) for name in DIGESTS] == [TUNNEL_SECTION] * len(DIGESTS)
