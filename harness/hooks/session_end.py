"""SessionEnd: удаляет контейнеры локального Docker с точной меткой harness-session=<session_id>; stdout пуст."""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import time

LABEL_SESSION = 'harness-session'
CLEANUP_BUDGET_SECONDS = 12.0
DOCKER_TIMEOUT_SECONDS = 3.0
SESSION_ID_PATTERN = '[A-Za-z0-9-]+'
_ID = re.compile('[0-9a-f]{64}')
_LOCAL = re.compile(r'unix:///\S+|npipe:////\./\S+|tcp://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?/?')
_REMOTE = re.compile(r'(?:tcp|ssh)://[^\s/]')
_GONE, _UNCLEAR = object(), object()
_now = time.monotonic


def _read() -> object:
    try:
        return json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
    except (ValueError, RecursionError):
        return None


def _valid(payload: object) -> bool:
    sid = payload.get('session_id') if isinstance(payload, dict) else None
    return (isinstance(sid, str) and re.fullmatch(SESSION_ID_PATTERN, sid) is not None
            and payload.get('hook_event_name') == 'SessionEnd')


def _docker(argv: list[str], timeout: float) -> tuple[int, str]:
    """(0, stdout); ошибка - (код, stdout+stderr) для разбора, не для журнала; нет docker - (127, '')."""
    env = {key: value for key, value in os.environ.items() if key not in ('DOCKER_CONTEXT', 'DOCKER_HOST')}
    try:
        proc = subprocess.run(['docker', *argv], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout, env=env, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return 127, ''
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stdout + proc.stderr


def is_local_endpoint(endpoint: str) -> bool:
    return isinstance(endpoint, str) and _LOCAL.fullmatch(endpoint) is not None


def _endpoint(run) -> str:
    context, host = (os.environ.get(name, '').strip() for name in ('DOCKER_CONTEXT', 'DOCKER_HOST'))
    if host and not context:
        return host
    code, out = run('context', 'inspect', *([context] if context else []), '--format', '{{.Endpoints.docker.Host}}')
    return out.strip() if code == 0 else ''


def _owner(pinned, cid: str) -> object:
    """Метка сессии контейнера; _GONE - контейнера уже нет; _UNCLEAR - Docker ответил ошибкой."""
    code, out = pinned('container', 'inspect', '--format', '{{json .Config.Labels}}', cid)
    if code != 0:
        return _GONE if any(f'{text}: {cid}' in out for text in ('No such container', 'No such object')) else _UNCLEAR
    try:
        labels = json.loads(out)
    except ValueError:
        return None
    return labels.get(LABEL_SESSION) if isinstance(labels, dict) else None


def _remove(pinned, sid: str, cid: str, counts: dict[str, int]) -> None:
    owner = _owner(pinned, cid) if _ID.fullmatch(cid) else None
    if owner is _GONE:
        return
    if owner is _UNCLEAR:
        counts['errors'] += 1
    elif owner != sid:
        counts['skipped'] += 1
    elif pinned('rm', '-f', cid)[0] == 0:
        counts['removed'] += 1
    elif _owner(pinned, cid) is not _GONE:
        counts['errors'] += 1


def cleanup(payload: dict) -> dict[str, int]:
    """{'removed', 'skipped', 'errors'} уборки контейнеров сессии payload; не бросает."""
    counts = {'removed': 0, 'skipped': 0, 'errors': 0}
    if not _valid(payload):
        return counts
    sid, deadline = payload['session_id'], _now() + CLEANUP_BUDGET_SECONDS

    def run(*argv: str) -> tuple[int, str]:
        left = deadline - _now()
        if left <= 0:
            raise TimeoutError('cleanup budget')
        return _docker(list(argv), min(DOCKER_TIMEOUT_SECONDS, left))

    try:
        endpoint = _endpoint(run)
        if not is_local_endpoint(endpoint):  # понятный адрес другой машины - не наш, но и не сбой
            counts['skipped' if _REMOTE.match(endpoint) else 'errors'] += 1
            return counts
        pinned = functools.partial(run, '--host', endpoint)
        code, out = pinned('ps', '-aq', '--no-trunc', '--filter', f'label={LABEL_SESSION}={sid}')
        if code != 0:
            raise OSError('docker ps')
        for cid in dict.fromkeys(out.split()):
            _remove(pinned, sid, cid, counts)
    except Exception:  # noqa: BLE001 - бюджет вышел или сбой: удалённое не откатывается
        counts['errors'] += 1
    return counts


def main() -> int:
    if not _valid(payload := _read()):
        print('harness hooks: invalid input', file=sys.stderr)
        return 0
    if cleanup(payload)['errors']:
        print('harness hooks: cleanup incomplete', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
