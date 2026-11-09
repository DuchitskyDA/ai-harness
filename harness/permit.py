"""Строка autoMode.allow в ~/.claude/settings.json по явному «да» владельца на её точный текст.

python -X utf8 -B ~/.harness/permit.py add "<строка>"
python -X utf8 -B ~/.harness/permit.py remove "<строка>"
Последняя строка вывода - итог: `allow added|present|removed|absent: <строка>`. Коды: 0 успех, 2 ошибка.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULTS = "$defaults"
BOM = "\ufeff"
LOCK_WAIT = 10.0


def change(allow: list[str], action: str, rule: str) -> tuple[list[str], str]:
    """Новый список allow и итог; $defaults остаётся первым."""
    rest = [v for v in allow if v != DEFAULTS]
    if action == "add":
        status = "present" if rule in rest else "added"
        rest = rest if rule in rest else [*rest, rule]
    else:
        status = "removed" if rule in rest else "absent"
        rest = [v for v in rest if v != rule]
    return [DEFAULTS, *rest], status


def _try_lock(fileno: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fileno: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileno, fcntl.LOCK_UN)


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Чтение-изменение-запись под замком ОС: параллельный add/remove не вернёт снятую строку.

    Файл замка не удаляется; замок держит открытый дескриптор, после падения процесса его снимает ОС.
    """
    lock = path.with_name(path.name + ".permit-lock")
    deadline = time.monotonic() + LOCK_WAIT
    with open(lock, "a+b") as handle:
        handle.seek(0)
        while True:
            try:
                _try_lock(handle.fileno())
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError(f"занят замок {lock}") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            _unlock(handle.fileno())


def apply(path: Path, action: str, rule: str) -> str:
    with locked(path):
        raw = path.read_bytes().decode("utf-8")
        bom = BOM if raw.startswith(BOM) else ""
        data = json.loads(raw[len(bom) :])
        auto = data.setdefault("autoMode", {})
        auto["allow"], status = change(auto.get("allow") or [], action, rule)
        newline = "\r\n" if "\r\n" in raw else "\n"
        text = bom + json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        with os.fdopen(fd, "wb") as out:
            out.write(text.replace("\n", newline).encode("utf-8"))
        os.replace(tmp, path)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="permit.py")
    parser.add_argument("action", choices=("add", "remove"))
    parser.add_argument("rule")
    parser.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    args = parser.parse_args(argv)
    rule = args.rule.strip()
    if not rule or rule == DEFAULTS:
        print("allow error: пустая строка или $defaults", flush=True)
        return 2
    try:
        status = apply(Path(args.settings), args.action, rule)
    except (OSError, ValueError) as exc:
        print(f"allow error: {args.settings}: {exc}", flush=True)
        return 2
    print(f"allow {status}: {rule}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
