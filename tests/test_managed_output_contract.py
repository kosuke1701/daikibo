"""Finite real-process checks for the managed output collector contract."""

import sys

import pytest

from conftest import make_task
from daikibo.common import digest
from daikibo.execution_errors import observe_failure


def _limit_policy(control, project, monkeypatch, limit):
    original = control.g.policy(project)
    body = dict(original['body'])
    body['max_output_bytes'] = limit

    def policy(_project, create=True):
        return {**original, 'body': dict(body)}

    monkeypatch.setattr(control.g, 'policy', policy)


def _command(control, project_row, payload, stream, *, binding='output-contract'):
    snapshot = control.sn.capture(control.owner, project_row[0], [project_row[1]])
    literal = repr(bytes(payload))
    code = (
        'import sys; data=%s; '
        'sys.%s.buffer.write(data); sys.%s.buffer.flush()'
    ) % (literal, stream, stream)
    return control.rt.observe(
        project_row[0], None, 'managed-output-command', 'test:command', None,
        digest({'binding': binding}), snapshot,
        lambda work, home, cwd: ([sys.executable, '-c', code], None),
        prompt=b'', timeout=10,
    )[0]


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_capture_boundaries_are_per_stream_and_byte_based(full, full_project, monkeypatch, stream):
    cap = 64
    _limit_policy(full, full_project[0], monkeypatch, cap)

    for size, overflowing in ((cap - 1, False), (cap, False), (cap + 1, True)):
        observed = _command(full, full_project, b'x' * size, stream, binding=f'{stream}-{size}')
        capture = observed['output_capture']
        item = capture[stream]
        assert capture['format'] == 'daikibo.output-capture.v1'
        assert capture['limit_bytes_per_stream'] == cap
        assert item['bytes_retained_raw'] == min(size, cap)
        assert item['bytes_read'] >= item['bytes_retained_raw']
        assert item['truncated'] is overflowing
        assert observed['output_overflow'] is overflowing
        if overflowing:
            assert observed['failure']['code'] == 'output_limit'
            assert observed['failure']['message']
            assert observed['result']['error']['message'] == observed['failure']['message']
            assert stream in observed['failure']['message']
        else:
            assert observed['failure'] is None


def test_capture_does_not_turn_two_below_limit_streams_into_total_limit(full, full_project, monkeypatch):
    cap = 64
    _limit_policy(full, full_project[0], monkeypatch, cap)
    snapshot = full.sn.capture(full.owner, full_project[0], [full_project[1]])
    code = (
        'import sys; '
        'sys.stdout.buffer.write(b"a"*63); sys.stdout.flush(); '
        'sys.stderr.buffer.write(b"b"*63); sys.stderr.flush()'
    )
    observed, _, _ = full.rt.observe(
        full_project[0], None, 'two-streams', 'test:command', None,
        digest({'two_streams': True}), snapshot,
        lambda work, home, cwd: ([sys.executable, '-c', code], None),
        timeout=10,
    )
    assert observed['failure'] is None
    assert observed['output_overflow'] is False
    assert observed['output_capture']['stdout']['bytes_read'] == 63
    assert observed['output_capture']['stderr']['bytes_read'] == 63


def test_capture_records_raw_prefix_and_multibyte_overflow_without_guessing_unread_bytes(full, full_project, monkeypatch):
    cap = 64
    _limit_policy(full, full_project[0], monkeypatch, cap)
    payload = 'é'.encode('utf-8') * 40  # 80 bytes, one long UTF-8 line
    observed = _command(full, full_project, payload, 'stdout', binding='multibyte')
    item = observed['output_capture']['stdout']
    assert item['bytes_retained_raw'] == cap
    assert item['bytes_read'] >= cap + 1
    assert item['truncated'] is True
    assert len(full.s.blob_get(observed['stdout_blob'])) <= cap
    assert observed['failure']['code'] == 'output_limit'
    assert 'é' not in observed['failure']['message']


def test_capture_is_shared_by_receipt_run_status_and_job_failure(full, full_project, monkeypatch, tmp_path):
    cap = 64
    _limit_policy(full, full_project[0], monkeypatch, cap)
    observed = _command(full, full_project, b'z' * (cap + 1), 'stdout', binding='shared')
    receipt = full.g.receipt(observed['id'])
    run = full.rt.run_status(full.owner, observed['run'])
    assert receipt['output_capture'] == observed['output_capture']
    assert run['body']['output_capture'] == receipt['output_capture']
    assert run['output_capture'] == receipt['output_capture']
    assert run['result'] == observed['result']

    task = make_task(full, full_project)
    script = tmp_path / 'overflow-agent.py'
    script.write_text(
        'import sys\n'
        'sys.stdin.buffer.read()\n'
        'sys.stdout.buffer.write(b"q" * 65)\n'
        'sys.stdout.buffer.flush()\n'
    )
    full.rt.adapters.register(full.owner, 'overflow-agent', 'fixture', sys.executable, [str(script)])
    full.w.claim(full.owner, full_project[0], task)
    job = full.jobs.submit(full.owner, 'execute', {'task': task, 'adapter': 'overflow-agent'})
    outcome = full.jobs.run_one(full.s.one('SELECT * FROM jobs WHERE id=?', (job['id'],), True))
    assert outcome['status'] == 'failed'
    assert outcome['error']['failure']['code'] == 'output_limit'
    assert outcome['error']['failure']['message']
    if outcome.get('result'):
        assert outcome['result']['output_capture'] == outcome['error']['output_capture']
    job_receipt = full.g.receipt(outcome['error']['receipt'])
    assert outcome['error']['output_capture'] == job_receipt['output_capture']
    assert full.w.task(full.owner, task)['candidate'] is None


def test_output_limit_formatter_is_safe_and_preserves_precedence():
    capture = {
        'format': 'daikibo.output-capture.v1',
        'limit_bytes_per_stream': 64,
        'stdout': {'bytes_read': 65, 'bytes_retained_raw': 64, 'truncated': True},
        'stderr': {'bytes_read': 0, 'bytes_retained_raw': 0, 'truncated': False},
    }
    failure = observe_failure(exit_code=-9, stderr=b'secret-body', overflow=True,
                              output_capture=capture)
    assert failure['code'] == 'output_limit'
    assert failure['message']
    assert 'secret-body' not in failure['message']
    assert 'stdout' in failure['message'] and '65' in failure['message']
    assert observe_failure(exit_code=-9, stderr=b'', overflow=True)['message']
    assert observe_failure(exit_code=-9, stderr=b'', cancelled=True, timed_out=True,
                           overflow=True, output_capture=capture)['code'] == 'cancelled'
    assert observe_failure(exit_code=-9, stderr=b'', timed_out=True, overflow=True,
                           output_capture=capture)['code'] == 'timeout'
    malformed = {**capture, 'stdout': {'bytes_read': True, 'bytes_retained_raw': 64, 'truncated': True}}
    assert 'configured capture limit' in observe_failure(
        exit_code=-9, stderr=b'', overflow=True, output_capture=malformed)['message']


def test_output_limit_formatter_falls_back_for_unrepresentably_large_integer():
    capture = {
        'format': 'daikibo.output-capture.v1',
        'limit_bytes_per_stream': 64,
        'stdout': {'bytes_read': 10 ** 5000, 'bytes_retained_raw': 64, 'truncated': True},
        'stderr': {'bytes_read': 0, 'bytes_retained_raw': 0, 'truncated': False},
    }
    failed = observe_failure(exit_code=-9, stderr=b'', overflow=True,
                             output_capture=capture)
    assert failed['code'] == 'output_limit'
    assert failed['message'] == (
        'Command output exceeded the configured capture limit; '
        'capture is incomplete and the run failed.')


def test_redacted_blob_size_is_not_raw_capture_count(full, full_project, monkeypatch):
    cap = 128
    _limit_policy(full, full_project[0], monkeypatch, cap)
    secret = 'managed-secret-token'
    payload = secret.encode()
    snapshot = full.sn.capture(full.owner, full_project[0], [full_project[1]])
    code = 'import sys; sys.stdout.write(%r); sys.stdout.flush()' % secret
    observed, _, _ = full.rt.observe(
        full_project[0], None, 'redaction', 'test:command', None,
        digest({'redaction': True}), snapshot,
        lambda work, home, cwd: ([sys.executable, '-c', code], None),
        timeout=10, extra_env={'DAIKIBO_CAPTURE_SECRET': secret},
    )
    stored = full.s.blob_get(observed['stdout_blob'])
    assert observed['output_capture']['stdout']['bytes_read'] == len(payload)
    assert len(stored) != len(payload)
    assert b'[REDACTED]' in stored
    assert secret.encode() not in observed['failure']['message'] if observed['failure'] else True
