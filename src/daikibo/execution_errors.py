"""Normalize CLI terminal envelopes; never infer a completed action from prose.

No SDK dependency is needed. The fixtures test this protocol boundary, not a live
provider. Costs are client estimates; missing usage is kept as unknown.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .common import Fault, parse_json

TRANSIENT = frozenset({'rate_limit', 'server_error', 'connection_lost'})


def failure(code: str, *, source: str, message: str = '', retry_after: float | None = None) -> dict:
    return {'code': code, 'retryable': code in TRANSIENT,
            'source': source, 'message': message[:2000],
            'retry_after_seconds': retry_after, 'technical_impossibility': False}


def _delay(value: Any, divisor: float = 1.0) -> float | None:
    # Accept explicitly reported durations, not guesses from reset clock times.
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return min(float(value) / divisor, 7 * 86400.0)


def classify_error(value: dict | str, source: str = 'cli_terminal_error') -> dict:
    envelope = value if isinstance(value, dict) else {'message': str(value)}
    codes: list[str] = []
    messages: list[str] = []
    statuses: list[int] = []
    delays: list[float] = []

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(item, dict):
            for key in ('code', 'type', 'subtype', 'error', 'terminal_reason'):
                if isinstance(item.get(key), str):
                    codes.append(item[key].lower())
            for key in ('message', 'result', 'detail'):
                if isinstance(item.get(key), str):
                    messages.append(item[key][:2000])
            for key in ('status', 'status_code', 'http_status', 'api_error_status', 'error_status'):
                if type(item.get(key)) is int:
                    statuses.append(item[key])
            for key in ('retry_after', 'retry_after_seconds'):
                delay = _delay(item.get(key))
                if delay is not None:
                    delays.append(delay)
            headers = item.get('headers')
            if isinstance(headers, dict):
                for key, val in headers.items():
                    if str(key).lower() == 'retry-after' and (d := _delay(val)) is not None:
                        delays.append(d)
            if (d := _delay(item.get('retry_delay_ms'), 1000.0)) is not None:
                delays.append(d)
            for key in ('error', 'errors'):
                child = item.get(key)
                if isinstance(child, (dict, list)):
                    visit(child, depth + 1)
                elif isinstance(child, str):
                    messages.append(child[:2000])
        elif isinstance(item, list):
            for child in item[:100]:
                visit(child, depth + 1)
        elif isinstance(item, str):
            messages.append(item[:2000])

    visit(envelope)
    joined = ' '.join(messages).lower()
    code_set = set(codes)
    # Non-retryable quota/budget errors take precedence over a generic HTTP 429.
    if code_set & {'error_max_budget_usd', 'budget_exceeded', 'budget_limit'}:
        code = 'budget_exhausted'
    elif code_set & {'billing_error', 'insufficient_quota', 'quota_exceeded', 'account_on_hold', 'usage_limit_reached'} or re.search(r'insufficient[_ ]quota|exceeded your (?:current )?quota|billing (?:error|limit)|usage limit (?:reached|exceeded)', joined):
        code = 'quota_exhausted'
    elif code_set & {'authentication_failed', 'invalid_api_key', 'unauthorized', 'oauth_org_not_allowed', 'cloud_credential_error'} or 401 in statuses or 403 in statuses:
        code = 'authentication_required'
    elif code_set & {'context_length_exceeded', 'context_window_exceeded', 'max_output_tokens', 'error_max_turns'} or re.search(r'context (?:length|window).*(?:exceed|limit)|maximum context length', joined):
        code = 'context_limit'
    elif code_set & {'rate_limit', 'rate_limit_error', 'rate_limit_exceeded', 'too_many_requests'} or 429 in statuses or re.search(r'\brate limit(?:ed|ing| exceeded| reached)?\b|\bHTTP\s*429\b|\btoo many requests\b', joined):
        code = 'rate_limit'
    elif code_set & {'overloaded', 'overloaded_error', 'server_error', 'internal_server_error', 'service_unavailable'} or any(s in {500, 502, 503, 504, 529} for s in statuses):
        code = 'server_error'
    elif code_set & {'connection_error', 'connection_lost', 'connection_reset', 'network_error', 'stream_disconnected', 'api_connection_error'} or re.search(r'connection (?:reset|refused)|stream (?:disconnected|closed prematurely)|network (?:error|unreachable)', joined):
        code = 'connection_lost'
    elif code_set & {'invalid_request', 'invalid_request_error', 'model_not_found', 'error_max_structured_output_retries'} or any(s in {400, 404, 422} for s in statuses):
        code = 'configuration_error'
    else:
        code = 'unknown_agent_failure'
    record = failure(code, source=source, message='; '.join(messages), retry_after=max(delays) if delays else None)
    record['reported_codes'] = sorted(code_set)[:30]
    record['reported_statuses'] = sorted(set(statuses))[:30]
    return record


def _result_value(value: Any) -> dict:
    if isinstance(value, str):
        try:
            parsed = parse_json(value)
        except Fault:
            return {'message': value}
        if isinstance(parsed, dict):
            return parsed
        raise Fault('invalid_agent_output', 'Final structured result must be an object')
    if not isinstance(value, dict):
        raise Fault('invalid_agent_output', 'CLI did not produce an object or final text')
    return value


def decode(kind: str, stdout: bytes, result_file: Path | None = None) -> tuple[dict, dict, dict | None]:
    """Returns (result, metadata, failure). Raises Fault only on protocol errors."""
    if kind == 'fixture':
        result = parse_json(stdout)
        if not isinstance(result, dict):
            raise Fault('invalid_agent_output', 'Fixture result must be an object')
        return result, {}, None
    if kind == 'claude':
        value = parse_json(stdout)
        if isinstance(value, list):
            values = [v for v in value if isinstance(v, dict) and v.get('type') == 'result']
            if len(values) != 1:
                raise Fault('agent_incomplete', 'Expected exactly one Claude terminal result')
            value = values[0]
        if not isinstance(value, dict) or value.get('type') != 'result':
            raise Fault('agent_incomplete', 'No Claude terminal result envelope')
        meta = {key: value[key] for key in ('session_id', 'usage', 'total_cost_usd', 'duration_ms', 'subtype', 'num_turns') if key in value}
        if value.get('is_error') or str(value.get('subtype', '')).startswith('error_'):
            fail = classify_error(value)
            return {'verdict': 'blocked', 'error': {'code': fail['code'], 'message': fail['message']}}, meta, fail
        # An unknown terminal subtype does not become success by default.
        if value.get('subtype') not in (None, 'success'):
            raise Fault('agent_incomplete', 'Unrecognized Claude terminal subtype')
        return _result_value(value.get('structured_output', value.get('result'))), meta, None
    if kind != 'codex':
        raise Fault('invalid_adapter', 'Unknown CLI protocol')
    events = [parse_json(line) for line in stdout.splitlines() if line.strip()]
    if not events or any(not isinstance(e, dict) or not isinstance(e.get('type'), str) for e in events):
        raise Fault('invalid_agent_output', 'Invalid Codex JSONL event stream')
    failed = [e for e in events if e['type'] == 'turn.failed']
    completed = [e for e in events if e['type'] == 'turn.completed']
    errors = [e for e in events if e['type'] == 'error']
    meta = {'event_count': len(events)}
    starts = [e for e in events if e['type'] == 'thread.started']
    if starts:
        meta['session_id'] = starts[-1].get('thread_id')
    terminal = (failed or completed)
    if terminal and isinstance(terminal[-1].get('usage'), dict):
        meta['usage'] = terminal[-1]['usage']
    if failed:
        fail = classify_error(failed[-1])
        return {'verdict': 'blocked', 'error': {'code': fail['code'], 'message': fail['message']}}, meta, fail
    if not completed:
        if errors:
            fail = classify_error(errors[-1])
        else:
            fail = failure('connection_lost', source='missing_terminal_event', message='Codex stream ended without a terminal turn result')
        return {'verdict': 'blocked', 'error': {'code': fail['code'], 'message': fail['message']}}, meta, fail
    if len(completed) != 1:
        raise Fault('invalid_agent_output', 'Multiple completed turns in a single managed request')
    terminal_index = events.index(completed[0])
    if any(e['type'] in {'error', 'turn.started'} for e in events[terminal_index + 1:]):
        raise Fault('agent_incomplete', 'New turn or error after final completion')
    # Nonterminal retry notices can precede a successful terminal event.
    meta['recovered_error_events'] = len(errors)
    if result_file and result_file.is_file() and not result_file.is_symlink():
        if result_file.stat().st_size > 8 * 1024 * 1024:
            raise Fault('invalid_agent_output', 'Final message exceeds size limit')
        value = result_file.read_bytes().decode('utf-8')
    else:
        messages = [e.get('item', {}).get('text') for e in events[:terminal_index]
                    if e['type'] == 'item.completed' and isinstance(e.get('item'), dict) and e['item'].get('type') == 'agent_message']
        if not messages:
            raise Fault('agent_incomplete', 'No final agent message before completion')
        value = messages[-1]
    return _result_value(value), meta, None


def _output_limit_message(output_capture: Any) -> str:
    """Build a bounded output-limit diagnostic from collector-owned telemetry.

    This formatter deliberately accepts only the small typed capture envelope
    emitted by Runtime.  Worker supplied JSON, paths, command lines, and log
    contents must never become part of a failure message.  A malformed or
    incomplete envelope falls back to a useful generic diagnostic rather than
    guessing a byte count.
    """
    generic = ('Command output exceeded the configured capture limit; '
               'capture is incomplete and the run failed.')

    def safe_decimal(value: int) -> str | None:
        """Return a small diagnostic integer without invoking huge int repr."""
        # Collector byte counters are ordinary small integers.  This finite
        # representation guard handles malformed but type-correct telemetry
        # before Python's decimal conversion can allocate or raise its own
        # digit-limit ValueError; it is a formatter safety bound, not a
        # capture policy or a new worker output limit.
        try:
            if value.bit_length() > 4096:
                return None
            text = str(value)
        except (MemoryError, OverflowError, ValueError):
            return None
        return text if len(text) <= 1200 else None

    if not isinstance(output_capture, dict) or output_capture.get('format') != 'daikibo.output-capture.v1':
        return generic
    limit = output_capture.get('limit_bytes_per_stream')
    if type(limit) is not int or limit <= 0:
        return generic
    limit_text = safe_decimal(limit)
    if limit_text is None:
        return generic
    streams = []
    for name in ('stdout', 'stderr'):
        value = output_capture.get(name)
        if not isinstance(value, dict):
            return generic
        read = value.get('bytes_read')
        retained = value.get('bytes_retained_raw')
        truncated = value.get('truncated')
        if (type(read) is not int or read < 0 or type(retained) is not int or
                retained < 0 or retained > limit or read < retained or
                type(truncated) is not bool or truncated != (read > retained)):
            return generic
        if truncated:
            read_text = safe_decimal(read)
            retained_text = safe_decimal(retained)
            if read_text is None or retained_text is None:
                return generic
            streams.append((name, read_text, retained_text))
    if not streams:
        return generic
    details = '; '.join(f'{name} observed at least {read} bytes, retained {retained} raw bytes'
                        for name, read, retained in streams)
    return (f'Command output exceeded the per-stream capture limit ({limit_text} bytes); '
            f'{details}. Capture is truncated; the run failed.')


def observe_failure(*, exit_code: int, stderr: bytes, decoded: dict | None = None,
                    protocol_error: Fault | None = None, cancelled: bool = False,
                    timed_out: bool = False, overflow: bool = False,
                    collector_error: dict | None = None,
                    output_capture: dict | None = None) -> dict | None:
    """Observer facts override any success-looking text from the worker."""
    if cancelled:
        return failure('cancelled', source='collector')
    if timed_out:
        return failure('timeout', source='collector')
    if overflow:
        return failure('output_limit', source='collector',
                       message=_output_limit_message(output_capture))
    if collector_error and collector_error.get('code') == 'execution_budget':
        result=failure('execution_budget', source='admission', message=collector_error.get('message', ''))
        result['details']=collector_error.get('details')
        return result
    if collector_error:
        return failure('collector_error', source='collector', message=str(collector_error.get('message', '')))
    if decoded:
        return decoded
    if exit_code != 0:
        inferred = classify_error(stderr.decode(errors='replace')[-4000:], source='stderr_hint')
        # A stderr hint alone cannot authorize an automatic repeat.
        inferred['retryable'] = False
        if inferred['code'] == 'unknown_agent_failure':
            inferred['code'] = 'process_exit'
        return inferred
    if protocol_error:
        return failure('protocol_error', source='cli_protocol', message=protocol_error.message)
    return None
