"""Режим авто-решений Claude Code, который install.py пишет в settings.json (ключ autoMode).

Единственный источник - autoMode шаблона claude/settings.example.json.
"""
import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / 'claude' / 'settings.example.json'


def auto_mode() -> dict:
    return json.loads(SETTINGS.read_text(encoding='utf-8'))['autoMode']
