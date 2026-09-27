import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from daikibo.common import Actor, Fault, canonical, digest
from daikibo.control import Control
from daikibo.rpc import Server, Client
from daikibo.schema_evaluator import DIALECT, DEFAULT_LIMITS
from daikibo.testreports import junit
from daikibo.supervisor import ALLOWED, VIEW_METHODS
from conftest import make_task


def upload(c, project, value, mime='application/json'):
    raw = value if isinstance(value, bytes) else canonical(value)
    return c.documents.register(c.owner, project, base64.b64encode(raw).decode(), 'schema-check-fixture', mime)


def docs(c, project, schema=None, instance=b'{"amount":4.02}'):
    s = upload(c, project, schema or {'$schema': DIALECT, 'type': 'object', 'properties': {
        'amount': {'type': 'number', 'multipleOf': 0.01, 'minimum': 0}}, 'required': ['amount'], 'additionalProperties': False})
    i = upload(c, project, instance)
    return s, i


def args(s, i):
    return {'document': s['id'], 'expected_digest': s['raw_digest'],
            'instance_document': i['id'], 'instance_digest': i['raw_digest']}


def test_diagnostic_is_raw_bound_readonly_and_not_gate_evidence(full, full_project):
    c = full; s, i = docs(c, full_project[0])
    counts = {t: c.s.one(f'SELECT count(*) n FROM {t}')['n'] for t in ('runs', 'receipts', 'artifacts', 'gate_results')}
    result = c.invoke(c.owner, 'contract.check_instance', args(s, i))
    assert result['result']['valid'] is True
    assert result['schema']['raw_digest'] == s['raw_digest']
    assert result['instance']['raw_digest'] == i['raw_digest']
    assert result['digest'] == digest({k: v for k, v in result.items() if k != 'digest'})
    assert result['deploy_ready'] is False and result['test_receipt_created'] is False
    assert result['review_performed'] is False and result['adopted'] is False
    for t, n in counts.items():
        assert c.s.one(f'SELECT count(*) n FROM {t}')['n'] == n
    for method in ('contract.schema_capabilities', 'contract.check_schema', 'contract.check_instance'):
        assert method in ALLOWED and method in VIEW_METHODS and method in c.read_routes


def test_schema_only_never_claims_an_instance_was_tested(full, full_project):
    c = full; s, i = docs(c, full_project[0])
    report = c.invoke(c.owner, 'contract.check_schema', {'document': s['id'], 'expected_digest': s['raw_digest']})
    assert report['result']['schema_status'] == 'supported'
    assert report['result']['valid'] is None and report['instance'] is None
    assert not report['result']['instance_evaluated']


@pytest.mark.parametrize('which', ['expected_digest', 'instance_digest'])
def test_stale_source_digest_rejected(full, full_project, which):
    c = full; s, i = docs(c, full_project[0]); params = args(s, i); params[which] = '0' * 64
    with pytest.raises(Fault) as exc:
        c.invoke(c.owner, 'contract.check_instance', params)
    assert exc.value.code == 'stale_document'


def test_original_blob_corruption_is_not_validated(full, full_project):
    c = full; s, i = docs(c, full_project[0])
    c.s.blob_path(s['raw_digest']).write_bytes(b'{}')
    with pytest.raises(Fault):
        c.invoke(c.owner, 'contract.check_instance', args(s, i))


def test_cross_project_and_other_project_role_rejected(full, full_project):
    c = full; s, i = docs(c, full_project[0]); other = c.k.create_project(c.owner, 'other')['id']
    j = upload(c, other, {'amount': 1})
    with pytest.raises(Fault):
        c.invoke(c.owner, 'contract.check_instance', args(s, j))
    with pytest.raises(Fault):
        c.invoke(Actor('other', 'agent', other), 'contract.check_instance', args(s, i))


def test_raw_decimal_precision_is_not_lost_by_document_registration(full, full_project):
    c = full
    s = upload(c, full_project[0], b'{"$schema":"https://json-schema.org/draft/2020-12/schema","const":0.100000000000000000000000000001}')
    i = upload(c, full_project[0], b'0.100000000000000000000000000002')
    assert c.invoke(c.owner, 'contract.check_instance', args(s, i))['result']['valid'] is False


def test_selected_openapi31_schema_keeps_local_refs_and_no_http_claim(full, full_project):
    c = full
    api = {'openapi': '3.1.1', 'info': {'title': 'test', 'version': '1'},
           'paths': {'/': {'get': {'responses': {'200': {'description': 'ok', 'content': {
               'application/json': {'schema': {'$ref': '#/components/schemas/Value'}}}}}}}},
           'components': {'schemas': {'Value': {'type': 'integer'}}}}
    s = upload(c, full_project[0], api); i = upload(c, full_project[0], b'1.0')
    params = args(s, i) | {'entry': '#/paths/~1/get/responses/200/content/application~1json/schema'}
    r = c.invoke(c.owner, 'contract.check_instance', params)
    assert r['result']['valid'] is True and not r['result']['complete_standard_conformance']
    assert r['result']['semantic_review_required']
    inventory = c.standard_contracts.inspect(c.owner, s['id'], s['raw_digest'])
    assert inventory['document_structurally_validated'] is False


@pytest.mark.parametrize('case', ['oas30', 'custom_dialect', 'whole_oas', 'override_draft07'])
def test_no_dialect_or_openapi_conversion_by_guess(full, full_project, case):
    c = full; value = {'openapi': '3.1.1', 'components': {'schemas': {'x': {'type': 'number'}}}}
    entry = '#/components/schemas/x'
    if case == 'oas30': value['openapi'] = '3.0.4'
    if case == 'custom_dialect': value['jsonSchemaDialect'] = 'https://example.org/custom'
    if case == 'whole_oas': entry = '#'
    if case == 'override_draft07': value = {'$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'number'}; entry = '#'
    s = upload(c, full_project[0], value); i = upload(c, full_project[0], b'1')
    with pytest.raises(Fault):
        c.invoke(c.owner, 'contract.check_instance', args(s, i) | {'entry': entry, 'dialect': DIALECT})


def test_schema_pointer_selects_subschema_without_losing_root_dialect(full, full_project):
    c = full
    s = upload(c, full_project[0], {'$schema': DIALECT, '$defs': {'x': {'type': 'integer'}}})
    i = upload(c, full_project[0], {'response': ['bad', 3]})
    r = c.invoke(c.owner, 'contract.check_instance', args(s, i) | {'entry': '#/$defs/x', 'instance_entry': '#/response/1'})
    assert r['result']['valid'] is True


def test_raw_schemas_unsupported_and_malformed_stay_retained(full, full_project):
    c = full
    for schema in (b'openapi: 3.1.1', b'{"type":1,"type":2}'):
        s, i = docs(c, full_project[0], schema)
        r = c.invoke(c.owner, 'contract.check_instance', args(s, i))
        assert r['result']['valid'] is None
        assert c.s.blob_get(s['raw_digest']) == schema
    s, i = docs(c, full_project[0], {'$schema': DIALECT, 'format': 'email'})
    assert c.invoke(c.owner, 'contract.check_instance', args(s, i))['result']['status'] == 'unsupported'


def test_diagnostic_is_stable_after_control_restart(full, full_project):
    c = full; s, i = docs(c, full_project[0]); before = c.invoke(c.owner, 'contract.check_instance', args(s, i))
    # Stop the sole writer first, then restart from exactly the same sources.
    home = c.s.home
    c.close()
    d = Control(home, mode='validation', start_workers=False)
    try:
        assert d.invoke(d.sec.authenticate(), 'contract.check_instance', args(s, i)) == before
    finally:
        d.close()


def test_native_actions_and_real_unix_rpc_use_registered_checks(full, full_project):
    c = full; pid, rid, req, root = full_project; s, i = docs(c, pid)
    c.native.attach(c.owner, 'session-schema', str(root), project=pid, register_repository=False)
    out = c.native.actions(c.owner, 'session-schema', [
        {'method': 'contract.schema_capabilities', 'params': {}},
        {'method': 'contract.check_instance', 'params': args(s, i)}])
    assert out['all_applied'] and out['actions'][1]['result']['result']['valid'] is True
    with tempfile.TemporaryDirectory(prefix='dd-schema-') as tmp:
        sock = Path(tmp) / 's'; server = Server(c, sock)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            response = Client(sock).call('contract.check_instance', args(s, i))
            assert response == out['actions'][1]['result']
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


def child_environment(monkeypatch):
    if os.environ.get('DAIKIBO_TEST_INSTALLED') != '1':
        monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))


@pytest.mark.parametrize('schema,data,expect,code', [
    ({'type': 'number'}, 1, 'valid', 0), ({'type': 'number'}, 'x', 'invalid', 0),
    ({'type': 'number'}, 1, 'invalid', 1), ({'type': 'number'}, 'x', 'valid', 1),
    ({'format': 'email'}, 'x', 'invalid', 2), ({'type': 42}, 'x', 'invalid', 2),
    ({'type': 'number'}, b'{"x":1,"x":2}', 'invalid', 2),
])
def test_real_cli_positive_negative_unknown_and_junit(tmp_path, monkeypatch, schema, data, expect, code):
    child_environment(monkeypatch)
    a, b, report = tmp_path/'schema.json', tmp_path/'data.json', tmp_path/'result.xml'
    a.write_bytes(canonical({'$schema': DIALECT, **schema})); b.write_bytes(data if isinstance(data, bytes) else canonical(data))
    before = (a.read_bytes(), b.read_bytes())
    run = subprocess.run([sys.executable, '-m', 'daikibo.schema_cli', '--schema', str(a), '--instance', str(b),
                          '--expect', expect, '--report', str(report)], capture_output=True, timeout=10)
    assert run.returncode == code, run.stderr.decode()
    output = json.loads(run.stdout)
    assert output['binding']['schema_sha256'] == digest(a.read_bytes())
    assert output['binding']['instance_sha256'] == digest(b.read_bytes())
    measured = junit(report.read_bytes(), ['contract-instance'])
    assert measured['count'] == 1 and measured['skipped'] == 0
    assert measured['passed'] is (code == 0)
    assert output['matched'] is (code == 0) and not output['deploy_ready']
    assert before == (a.read_bytes(), b.read_bytes())


def test_missing_file_cli_produces_error_not_skip(tmp_path, monkeypatch):
    child_environment(monkeypatch); report = tmp_path/'result.xml'
    result = subprocess.run([sys.executable, '-m', 'daikibo.schema_cli', '--schema', str(tmp_path/'missing'),
                             '--instance', str(tmp_path/'none'), '--expect', 'invalid', '--report', str(report)],
                            capture_output=True, timeout=10)
    assert result.returncode == 2
    assert junit(report.read_bytes())['passed'] is False


def test_oversized_file_does_not_publish_prefix_as_whole_digest(tmp_path, monkeypatch):
    child_environment(monkeypatch)
    a, b, r = tmp_path/'s', tmp_path/'i', tmp_path/'r.xml'
    a.write_text('{}'); b.write_bytes(b'"' + b'x' * DEFAULT_LIMITS.max_bytes + b'"')
    result = subprocess.run([sys.executable, '-m', 'daikibo.schema_cli', '--schema', str(a), '--instance', str(b),
                             '--dialect', DIALECT, '--expect', 'invalid', '--report', str(r)], capture_output=True, timeout=10)
    output = json.loads(result.stdout)
    assert result.returncode == 2 and output['result']['status'] == 'limit_exceeded'
    assert output['binding'] == {}


def test_cli_will_not_overwrite_input(tmp_path, monkeypatch):
    child_environment(monkeypatch)
    a = tmp_path/'input.json'; a.write_bytes(b'{}')
    result = subprocess.run([sys.executable, '-m', 'daikibo.schema_cli', '--schema', str(a), '--instance', str(a),
                             '--report', str(a)], capture_output=True, timeout=10)
    assert result.returncode != 0 and a.read_bytes() == b'{}'


@pytest.mark.parametrize('supported', [True, False])
def test_existing_runtime_executes_contract_check_and_gate_still_requires_reviews(full, full_project, monkeypatch, supported):
    child_environment(monkeypatch)
    c = full; pid, rid, req, root = full_project
    schema = {'$schema': DIALECT, 'type': 'integer'} if supported else {'$schema': DIALECT, 'format': 'email'}
    (root/'contract.json').write_bytes(canonical(schema)); (root/'instance.json').write_bytes(b'"bad"')
    task = make_task(c, full_project)
    c.w.plan_tests(c.owner, task, {'checks': [
        {'id': 'unit', 'argv': ['python', '-m', 'pytest', '-q', 'test_calc.py'], 'kind': 'pytest', 'required_tests': ['test_add']},
        {'id': 'contract', 'argv': ['python', '-m', 'daikibo.schema_cli', '--schema', 'contract.json', '--instance', 'instance.json',
                                   '--expect', 'invalid', '--report', 'contract.xml'], 'kind': 'junit', 'report': 'contract.xml',
         'required_tests': ['contract-instance']}], 'rationale': 'A negative contract sample does not replace normal requirements tests.'})
    c.w.ready(c.owner, task); c.w.claim(c.owner, pid, task); c.rt.execute(c.owner, task, 'fixture')
    results = c.rt.tests(c.owner, task)
    record = c.g.receipt(results['checks'][1]['receipt'])
    assert record['process_started'] and record['role'] == 'test:contract'
    assert record['result']['count'] == 1 and record['result']['passed'] is supported
    with pytest.raises(Fault):
        c.w.complete(c.owner, task, c.w.task(c.owner, task)['revision'])
    for role in ('spec', 'quality', 'test_adequacy'):
        c.rt.review(c.owner, task, role, 'fixture')
    if supported:
        assert c.w.complete(c.owner, task, c.w.task(c.owner, task)['revision'])['status'] == 'completed'
    else:
        with pytest.raises(Fault):
            c.w.complete(c.owner, task, c.w.task(c.owner, task)['revision'])
