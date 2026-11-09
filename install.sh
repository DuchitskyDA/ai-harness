#!/usr/bin/env bash
# Установка оснастки: один вызов harness/install.py --apply, код выхода сохраняется.
# План без записи: python3 harness/install.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || command -v python)"
exec "$PYTHON" -X utf8 -B "$REPO_ROOT/harness/install.py" --apply "$@"
