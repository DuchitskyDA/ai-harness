"""SessionEnd: уборка локальных Docker-контейнеров с точной меткой сессии.

Docker подменён в памяти (FakeDocker вместо _docker), часы - явная последовательность:
ни одного настоящего docker, процесса или ожидания.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('harness_hooks_session_end', HERE / 'session_end.py')
end = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(end)

SID = 'sess-1'
A, B, C = 'a' * 64, 'b' * 64, 'c' * 64
LOCAL = 'npipe:////./pipe/docker_engine'
ZERO = {'removed': 0, 'skipped': 0, 'errors': 0}


class FakeDocker:
    """Движок в памяти: контексты, контейнеры с метками, журнал вызовов с таймаутами."""

    def __init__(self):
        self.calls, self.boxes, self.ps_extra = [], {}, []
        self.contexts = {'': LOCAL}  # имя контекста ('' - текущий) -> endpoint
        self.answers, self.before = {}, {}  # глагол -> готовый ответ / разовое действие до ответа

    def add(self, cid, session=SID, **labels):
        self.boxes[cid] = {**({end.LABEL_SESSION: session} if session is not None else {}), **labels}

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if argv[:2] == ['context', 'inspect']:
            name = '' if argv[2] == '--format' else argv[2]
            host = self.contexts.get(name)
            return (0, host + '\n') if host is not None else (1, f'context "{name}" does not exist\n')
        verb, cid = argv[2], argv[-1]
        if verb in self.before:
            self.before.pop(verb)(cid)
        if verb in self.answers:
            return self.answers[verb]
        if verb == 'ps':
            want = argv[-1].split('=', 2)[2]
            ids = [c for c, labels in self.boxes.items() if labels.get(end.LABEL_SESSION) == want]
            return 0, '\n'.join(ids + self.ps_extra) + '\n'
        if cid not in self.boxes:
            return 1, (f'Error: No such object: {cid}\n' if verb == 'container'
                       else f'Error response from daemon: No such container: {cid}\n')
        if verb == 'container':
            return 0, json.dumps(self.boxes[cid]) + '\n'
        del self.boxes[cid]
        return 0, cid + '\n'

    def daemon(self):
        return [argv for argv, _timeout in self.calls if argv[:1] == ['--host']]

    def verbs(self):
        return [argv[2] for argv in self.daemon()]


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.delenv('DOCKER_HOST', raising=False)
    monkeypatch.delenv('DOCKER_CONTEXT', raising=False)
    monkeypatch.setattr(end, '_docker', fake)
    return fake


def _event(sid=SID, **extra):
    return {'session_id': sid, 'hook_event_name': 'SessionEnd', 'reason': 'other', **extra}


def _main(monkeypatch, capsys, raw):
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8'))
    code = end.main()
    out, err = capsys.readouterr()
    return code, out, err


def _raw(payload, bom=False):
    return (b'\xef\xbb\xbf' if bom else b'') + json.dumps(payload).encode('utf-8')


@pytest.mark.parametrize('raw', [
    _raw(_event()), _raw(_event(), bom=True), _raw(_event(reason='clear', cwd='/x', transcript_path='/t')),
    _raw(_event(reason='never-seen-reason')),
])
def test_input_transport(monkeypatch, capsys, docker, raw):
    docker.add(A)
    code, out, err = _main(monkeypatch, capsys, raw)
    assert (code, out, err) == (0, '', '')
    assert A not in docker.boxes


@pytest.mark.parametrize('raw', [
    b'\xff\xfe{', b'{"session_id"', b'[]', b'null', _raw({'hook_event_name': 'SessionEnd'}),
    _raw(_event(sid=1)), _raw(_event(sid='')), _raw(_event(sid='sess.1')), _raw(_event(sid='sess_1')),
    _raw(_event(sid='s"; docker rm -f x')), _raw({**_event(), 'hook_event_name': 'Stop'}),
])
def test_invalid_input_no_effect(monkeypatch, capsys, docker, raw):
    docker.add(A)
    code, out, err = _main(monkeypatch, capsys, raw)
    assert (code, out, err.strip()) == (0, '', 'harness hooks: invalid input')
    assert docker.calls == []


@pytest.mark.parametrize('payload', [{}, _event(sid=''), _event(sid=None), _event(sid='a b')])
def test_cleanup_without_valid_id_is_zero(docker, payload):
    assert end.cleanup(payload) == ZERO
    assert docker.calls == []


def test_cleanup_is_idempotent(docker):
    docker.add(A)
    assert end.cleanup(_event()) == {'removed': 1, 'skipped': 0, 'errors': 0}
    assert end.cleanup(_event()) == ZERO
    assert docker.verbs() == ['ps', 'container', 'rm', 'ps']


def test_cleanup_exact_session_label(docker):
    docker.add(A, **{'harness-agent': 'agent-9'})
    docker.add(B, session='sess-10')
    docker.add(C, session=None)
    assert end.cleanup(_event()) == {'removed': 1, 'skipped': 0, 'errors': 0}
    assert sorted(docker.boxes) == [B, C]
    assert [argv[-1] for argv in docker.daemon() if argv[2] == 'rm'] == [A]


def _relabel(docker):
    docker.before['container'] = lambda cid: docker.boxes[cid].update({end.LABEL_SESSION: 'sess-2'})


@pytest.mark.parametrize(('prepare', 'skipped'), [
    (lambda d: d.ps_extra.append(B) or d.add(B, session='sess-2'), 1),  # фильтр вернул чужой ID
    (_relabel, 1),  # метка сменилась к inspect
    (lambda d: d.ps_extra.extend(['web', 'A' * 64, 'a' * 12]), 3),  # имя или не полный ID
    (lambda d: d.answers.update(container=(0, 'not json\n')), 1),
    (lambda d: d.answers.update(container=(0, '["sess-1"]\n')), 1),
])
def test_cleanup_rechecks_labels(docker, prepare, skipped):
    docker.add(A)
    prepare(docker)
    counts = end.cleanup(_event())
    assert counts['skipped'] == skipped and counts['errors'] == 0
    assert 'rm' not in docker.verbs() or [argv[-1] for argv in docker.daemon() if argv[2] == 'rm'] == [A]
    assert all(argv[-1] != 'web' for argv in docker.daemon())


@pytest.mark.parametrize('verb', ['container', 'rm'])
def test_container_disappeared(docker, verb):
    docker.add(A)
    docker.before[verb] = lambda cid: docker.boxes.pop(cid)
    assert end.cleanup(_event()) == ZERO
    assert docker.verbs()[-1] == 'container'  # исчезновение подтверждено inspect


def test_rm_failure_with_container_alive_is_error(docker):
    docker.add(A)
    docker.answers['rm'] = (1, 'Error response from daemon: cannot remove container: permission denied\n')
    assert end.cleanup(_event()) == {'removed': 0, 'skipped': 0, 'errors': 1}
    assert A in docker.boxes


def _env(monkeypatch, context, host):
    monkeypatch.setenv('DOCKER_CONTEXT', context)
    monkeypatch.setenv('DOCKER_HOST', host)


@pytest.mark.parametrize(('env', 'contexts', 'counts'), [
    (('remote', ''), {'remote': 'ssh://user@build-host'}, {'removed': 0, 'skipped': 1, 'errors': 0}),
    (('remote', LOCAL), {'remote': 'tcp://10.0.0.5:2376'}, {'removed': 0, 'skipped': 1, 'errors': 0}),
    (('', 'tcp://docker.example:2376'), {}, {'removed': 0, 'skipped': 1, 'errors': 0}),
    (('missing', ''), {}, {'removed': 0, 'skipped': 0, 'errors': 1}),
    (('', ''), {'': ''}, {'removed': 0, 'skipped': 0, 'errors': 1}),
    (('', ''), {'': 'garbage'}, {'removed': 0, 'skipped': 0, 'errors': 1}),
])
def test_remote_context_no_daemon_calls(monkeypatch, docker, env, contexts, counts):
    docker.contexts = contexts
    docker.add(A)
    _env(monkeypatch, *env)
    assert end.cleanup(_event()) == counts
    assert docker.daemon() == []


@pytest.mark.parametrize(('env', 'endpoint', 'inspects'), [
    (('', ''), LOCAL, 1), (('dev', ''), 'unix:///var/run/docker.sock', 1),
    (('', 'tcp://127.0.0.1:2375'), 'tcp://127.0.0.1:2375', 0),
])
def test_endpoint_is_pinned(monkeypatch, docker, env, endpoint, inspects):
    docker.contexts = {'': LOCAL, 'dev': 'unix:///var/run/docker.sock'}
    docker.add(A)
    _env(monkeypatch, *env)
    docker.before['ps'] = lambda cid: docker.contexts.update({'': 'ssh://x@remote', 'dev': 'ssh://x@remote'})
    assert end.cleanup(_event())['removed'] == 1
    assert {tuple(argv[:2]) for argv in docker.daemon()} == {('--host', endpoint)}
    assert sum(argv[:2] == ['context', 'inspect'] for argv, _t in docker.calls) == inspects


@pytest.mark.parametrize(('endpoint', 'local'), [
    ('unix:///var/run/docker.sock', True), ('unix:///Users/u/.docker/run/docker.sock', True),
    ('npipe:////./pipe/docker_engine', True), ('npipe:////./pipe/dockerDesktopLinuxEngine', True),
    ('tcp://localhost', True), ('tcp://localhost:2375', True), ('tcp://127.0.0.1:2376/', True), ('tcp://[::1]:2375', True),
    ('tcp://localhost.evil:2375', False), ('tcp://127.0.0.1@remote:2375', False), ('tcp://user@localhost:2375', False),
    ('tcp://127.0.0.2:2375', False), ('tcp://localhost:port', False), ('ssh://user@localhost', False),
    ('npipe:////server/pipe/docker_engine', False), ('unix://host/sock', False), ('unix:///', False),
    ('', False), ('localhost', False), ('http://localhost:2375', False), ('tcp://localhost:2375\nx', False),
])
def test_endpoint_boundaries(endpoint, local):
    assert end.is_local_endpoint(endpoint) is local


def test_cleanup_service_failure_docker_missing(monkeypatch, capsys):
    monkeypatch.delenv('DOCKER_HOST', raising=False)
    monkeypatch.delenv('DOCKER_CONTEXT', raising=False)
    monkeypatch.setattr(end.subprocess, 'run', MagicMock(side_effect=FileNotFoundError('docker')))
    code, out, err = _main(monkeypatch, capsys, _raw(_event()))
    assert (code, out, err.strip()) == (0, '', 'harness hooks: cleanup incomplete')


@pytest.mark.parametrize('verb', ['ps', 'container'])
def test_cleanup_service_failure(docker, verb):
    docker.add(A)
    docker.answers[verb] = (1, 'Cannot connect to the Docker daemon\n')
    assert end.cleanup(_event()) == {'removed': 0, 'skipped': 0, 'errors': 1}
    assert A in docker.boxes and 'rm' not in docker.verbs()


def test_cleanup_budget(monkeypatch, docker):
    docker.add(A)
    ticks = iter([100.0, 100.0, 110.0, 112.5])
    monkeypatch.setattr(end, '_now', lambda: next(ticks))
    assert end.cleanup(_event()) == {'removed': 0, 'skipped': 0, 'errors': 1}
    assert [timeout for _argv, timeout in docker.calls] == [3.0, 2.0]
    assert A in docker.boxes


def test_sessions_remain_isolated(docker):
    docker.add(A, session='sess-A')
    docker.add(B, session='sess-B')
    end.cleanup(_event(sid='sess-A'))
    end.cleanup(_event(sid='sess-A'))  # запоздалый повтор после перехода к B
    assert sorted(docker.boxes) == [B]
    assert end.cleanup(_event(sid='sess-B'))['removed'] == 1


def test_docker_call_drops_context_env(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, 'out\n', 'err\n'))
    monkeypatch.setattr(end.subprocess, 'run', run)
    monkeypatch.setenv('DOCKER_CONTEXT', 'remote')
    monkeypatch.setenv('DOCKER_HOST', 'ssh://x@remote')
    assert end._docker(['--host', LOCAL, 'ps'], 2.5) == (0, 'out\n')
    argv, kwargs = run.call_args
    assert argv[0] == ['docker', '--host', LOCAL, 'ps'] and kwargs['timeout'] == 2.5
    assert 'DOCKER_CONTEXT' not in kwargs['env'] and 'DOCKER_HOST' not in kwargs['env']


@pytest.mark.parametrize(('run', 'expected'), [
    ({'return_value': subprocess.CompletedProcess([], 1, 'out\n', 'No such container: x\n')},
     (1, 'out\nNo such container: x\n')),
    ({'side_effect': subprocess.TimeoutExpired('docker', 3)}, (127, '')),
    ({'side_effect': OSError('no docker')}, (127, '')),
])
def test_docker_call_failures(monkeypatch, run, expected):
    monkeypatch.setattr(end.subprocess, 'run', MagicMock(**run))
    assert end._docker(['ps'], 3.0) == expected
