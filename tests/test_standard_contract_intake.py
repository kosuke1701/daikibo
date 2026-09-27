"""Source-preserving contract support. No full-standard validation claims."""
import base64
import copy
import json
import pytest

from daikibo.common import Actor, Fault, canonical, digest
from daikibo.standard_contracts import inventory, material, pointer
from daikibo.operations import restore_backup
from daikibo.control import Control


def schema():
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'type': 'object',
            'properties': {'amount': {'type': 'integer', 'minimum': 1}}, 'required': ['amount'],
            'additionalProperties': False, 'description': '日本語の原文を保持します。'}


def api():
    return {'openapi': '3.1.1', 'info': {'title': 'Payments', 'version': '1'},
            'servers': [{'url': 'https://payments.example'}], 'security': [{'session': []}],
            'paths': {'/payments/{id}': {
                'parameters': [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'get': {'operationId': 'getPayment', 'responses': {
                    '200': {'description': 'found', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Payment'}}}},
                    '404': {'description': 'missing'}}},
                'delete': {'operationId': 'cancel', 'security': [], 'responses': {'204': {'description': 'cancelled'}}}}},
            'components': {'schemas': {'Payment': schema()}, 'securitySchemes': {'session': {'type': 'http', 'scheme': 'bearer'}}},
            'x-project-rule': 'Do not silently delete this extension'}


def upload(c, project, value, mime='application/json'):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode() if not isinstance(value, bytes) else value
    doc = c.documents.register(c.owner, project, base64.b64encode(raw).decode(), 'uploaded-contract', mime)
    return doc


def inspect(c, doc, **kwargs):
    return c.invoke(c.owner, 'contract.inspect_document', {'document': doc['id'], 'expected_digest': doc['raw_digest'], **kwargs})


def read(c, doc, entry, **kwargs):
    result = c.invoke(c.owner, 'contract.read_entry', {'document': doc['id'], 'expected_digest': doc['raw_digest'], 'entry': entry, **kwargs})
    return result


def semantics():
    return {'title': 'Payment contract', 'statement': 'Review against the recorded requirements.',
            'idempotency': 'Must be checked against application requirements',
            'compatibility': 'Review clients and all status codes', 'consumers': ['orders'], 'verification': ['TC-payment']}


@pytest.mark.parametrize('version', ['3.0.0', '3.0.4', '3.1.0', '3.1.1', '3.1.2'])
def test_versioned_openapi_inventory_keeps_operations_and_schemas(full, full_project, version):
    c = full; value = api(); value['openapi'] = version
    doc = upload(c, full_project[0], value)
    report = inspect(c, doc)
    assert report['totals']['entries'] == 3 and not report['document_structurally_validated']
    assert not report['adopted'] and report['semantic_review_required']
    assert {e['kind'] for e in report['items']} == {'operation', 'schema'}
    assert json.loads(c.s.blob_get(doc['raw_digest'])) == value
    assert not c.k.source_coverage(c.owner, full_project[0])['structurally_complete']


@pytest.mark.parametrize('mime', ['application/schema+json', 'application/vnd.oai.openapi+json; charset=utf-8', 'Application/JSON'])
def test_standard_json_media_has_original_text_source(full, full_project, mime):
    doc = upload(full, full_project[0], schema(), mime)
    assert doc['text_source'] and not doc['unknown']
    assert inspect(full, doc)['standard'] == 'jsonschema'


def test_pointer_escaping_percent_unicode_and_array_index():
    root = {'a/b~c': {'日本': ['value']}, 'x': False}
    assert pointer(root, '#/a~1b~0c/%E6%97%A5%E6%9C%AC/0') == 'value'
    assert pointer(root, '#/x') is False
    for bad in ('#/a~2b', '#/missing', '#/a~1b~0c/日本/01', '#/a~1b~0c/日本/9', 'file.json#/a'):
        with pytest.raises(Fault): pointer(root, bad)


def test_operation_keeps_inherited_security_parameters_responses_and_extensions(full, full_project):
    c = full; doc = upload(c, full_project[0], api())
    get = json.loads(read(c, doc, '#/paths/~1payments~1{id}/get')['content'])
    delete = json.loads(read(c, doc, '#/paths/~1payments~1{id}/delete')['content'])
    assert get['context']['effective_security'] == [{'session': []}]
    assert delete['context']['effective_security'] == []
    assert get['context']['parameter_sources']['path'][0]['name'] == 'id'
    assert set(get['definition']['responses']) == {'200', '404'}
    assert get['resolved_local_nodes']['#/components/schemas/Payment']['required'] == ['amount']
    assert get['context']['global']['x-project-rule'] == 'Do not silently delete this extension'
    assert get['context']['security_schemes']['session']['scheme'] == 'bearer'


@pytest.mark.parametrize('feature', ['oneOf', 'pattern', 'unevaluatedProperties', '$dynamicRef', 'prefixItems'])
def test_untranslated_schema_features_never_become_relaxed_finite_types(full, full_project, feature):
    value = schema(); value[feature] = {'oneOf': [{'type': 'string'}, {'type': 'number'}], 'pattern': 'x+',
                                     'unevaluatedProperties': False, '$dynamicRef': '#node', 'prefixItems': [{'type': 'boolean'}]}[feature]
    doc = upload(full, full_project[0], value)
    body = json.loads(read(full, doc, '#')['content'])
    assert body['definition'][feature] == value[feature]
    assert body['semantic_review_required'] and not body['document_structurally_validated']


def test_cycle_is_finite_and_external_refs_remain_explicit(full, full_project):
    value = schema(); value['$defs'] = {'Node': {'type': 'object', 'properties': {'child': {'$ref': '#/$defs/Node'}}}}
    value['properties']['other'] = {'$ref': 'https://example.org/external.json'}
    value['properties']['node'] = {'$ref': '#/$defs/Node'}
    doc = upload(full, full_project[0], value)
    report = inspect(full, doc, section='references')
    assert any(x['status'] == 'unresolved' for x in report['items'])
    body = json.loads(read(full, doc, '#')['content'])
    assert list(body['resolved_local_nodes']) == ['#/$defs/Node']
    assert any('external' in i['reason'] for i in body['issues'])


def test_nested_id_does_not_resolve_pointer_against_wrong_base():
    value = schema(); value['$defs'] = {'n': {'$id': 'child.json', '$ref': '#/properties/amount'}}
    report = inventory(value)
    assert report['references'][0]['status'] == 'unresolved'
    assert 'base_uri' in report['references'][0]['reason']


def test_ref_only_path_is_not_lost_from_entry_count(full, full_project):
    value = api(); value['paths']['/external'] = {'$ref': 'paths.json#/external'}
    doc = upload(full, full_project[0], value)
    report = inspect(full, doc)
    assert any(e['kind'] == 'path_item_reference' for e in report['items'])
    assert report['totals']['issues'] > 0


@pytest.mark.parametrize('root', [True, False, {'$schema': 'unknown-dialect', 'not': {}}, {}])
def test_boolean_and_unknown_schema_are_preserved_with_no_conformance_claim(full, full_project, root):
    doc = upload(full, full_project[0], root)
    body = json.loads(read(full, doc, '#')['content'])
    assert body['definition'] == root
    assert not body['document_structurally_validated']
    assert body['issues']


@pytest.mark.parametrize('bad', [b'openapi: 3.1.1\ninfo: {}', b'{"x":1,"x":2}', b'\xff\xfe', b'{"type":1e400}', b'["not a schema"]'])
def test_unparseable_input_is_retained_not_guessed(full, full_project, bad):
    doc = upload(full, full_project[0], bad)
    with pytest.raises(Fault): inspect(full, doc)
    assert full.s.blob_get(doc['raw_digest']) == bad


@pytest.mark.parametrize('version', ['3.2.0', '2.0', 'bad', 3.1])
def test_unknown_openapi_version_is_not_silently_reinterpreted(full, full_project, version):
    value = api(); value['openapi'] = version
    doc = upload(full, full_project[0], value)
    with pytest.raises(Fault) as exc: inspect(full, doc)
    assert exc.value.code == 'unsupported_standard_version'


def test_asyncapi_not_misidentified_as_jsonschema(full, full_project):
    doc = upload(full, full_project[0], {'asyncapi': '3.0.0', 'channels': {}})
    with pytest.raises(Fault) as exc: inspect(full, doc)
    assert exc.value.code == 'unsupported_standard'


def test_derived_interface_is_only_a_draft_with_exact_raw_and_material(full, full_project):
    c = full; doc = upload(c, full_project[0], api())
    params = {'document': doc['id'], 'expected_digest': doc['raw_digest'],
              'entry': '#/paths/~1payments~1{id}/get', 'semantics': semantics()}
    draft = c.invoke(c.owner, 'contract.propose_document', params)
    assert draft['status'] == 'draft'
    binding = draft['body']['standard_contract']
    assert binding['raw_digest'] == doc['raw_digest']
    assert draft['body']['input']['format'] != 'daikibo.type.v1'
    assert binding['material_digest'] == digest(draft['body']['standard_contract_material'])
    _, _, _, context, _ = c.rt._subject(c.owner, draft['id'], 'design')
    assert context['sources'][0]['content'] == c.s.blob_get(doc['raw_digest']).decode()
    assert context['artifact']['body']['standard_contract_material']['definition']['responses']['404']
    assert not c.s.one('SELECT id FROM runs')  # Import is not an executed review.
    assert not c.k.source_coverage(c.owner, full_project[0])['structurally_complete']
    compare = c.contracts.compare(c.owner, draft['id'], draft['body'])
    assert not compare['type_compatible_proven'] and compare['semantic_review_required']


def test_draft_mutations_use_normal_change_workflow_and_import_is_idempotent(full, full_project):
    c = full; doc = upload(c, full_project[0], api())
    req = {'id': 'once', 'method': 'contract.propose_document', 'params': {
        'document': doc['id'], 'expected_digest': doc['raw_digest'], 'entry': '#/components/schemas/Payment', 'semantics': semantics()}}
    first, second = c.request(None, req), c.request(None, req)
    assert first == second
    assert len(c.s.all("SELECT id FROM artifacts WHERE kind='interface'")) == 1


def test_stale_or_cross_project_reads_are_rejected(full, full_project):
    c = full; doc = upload(c, full_project[0], api())
    with pytest.raises(Fault) as exc:
        c.standard_contracts.inspect(c.owner, doc['id'], '0' * 64)
    assert exc.value.code == 'stale_document'
    other = c.k.create_project(c.owner, 'Other')['id']
    with pytest.raises(Fault): c.standard_contracts.inspect(Actor('other', 'agent', other), doc['id'], doc['raw_digest'])


def test_material_chunks_reconstruct_exactly_and_inventory_pages_cover_all(full, full_project):
    c = full; value = api(); value['paths']['/payments/{id}']['get']['description'] = '日本語\n' * 3000
    doc = upload(c, full_project[0], value)
    pages, offset = [], 0
    while True:
        part = inspect(c, doc, offset=offset, limit=1); pages.extend(part['items'])
        if part['next_offset'] is None: break
        offset = part['next_offset']
    assert len(pages) == 3
    parts, start = [], 0
    while True:
        chunk = read(c, doc, '#/paths/~1payments~1{id}/get', start=start, limit=1000)
        parts.append(chunk['content'])
        if chunk['next_start'] is None: break
        start = chunk['next_start']
    content = json.loads(''.join(parts))
    assert digest(content) == chunk['digest']
    assert content['definition']['description'] == value['paths']['/payments/{id}']['get']['description']


def test_component_and_global_auth_changes_reach_operations(full, full_project):
    c = full; old = api(); new = copy.deepcopy(old)
    new['components']['schemas']['Payment']['required'].append('other')
    a = upload(c, full_project[0], old); b = upload(c, full_project[0], new)
    report = c.standard_contracts.compare(c.owner, a['id'], a['raw_digest'], b['id'], b['raw_digest'])
    assert '#/paths/~1payments~1{id}/get' in {v['entry'] for v in report['changes']}
    assert not report['type_compatible_proven']
    new = copy.deepcopy(old); new['security'] = []
    b = upload(c, full_project[0], new)
    report = c.standard_contracts.compare(c.owner, a['id'], a['raw_digest'], b['id'], b['raw_digest'])
    assert '#/paths/~1payments~1{id}/get' in {v['entry'] for v in report['changes']}


def test_identical_and_removed_entries_never_claim_semantic_compatibility(full, full_project):
    c = full; value = api(); a = upload(c, full_project[0], value)
    same = c.standard_contracts.compare(c.owner, a['id'], a['raw_digest'], a['id'], a['raw_digest'])
    assert same['byte_identical'] and same['total_changes'] == 0 and not same['type_compatible_proven']
    del value['paths']['/payments/{id}']['delete']
    b = upload(c, full_project[0], value)
    changed = c.standard_contracts.compare(c.owner, a['id'], a['raw_digest'], b['id'], b['raw_digest'])
    assert {'entry': '#/paths/~1payments~1{id}/delete', 'change': 'removed'} in changed['changes']


def test_node_limit_fails_instead_of_incomplete_inventory(monkeypatch):
    import daikibo.standard_contracts as module
    monkeypatch.setattr(module, 'MAX_NODES', 2)
    with pytest.raises(Fault) as exc: inventory(api())
    assert exc.value.code == 'contract_capacity'


def test_backup_and_spec_archive_keep_original_and_import_binding(full, full_project, tmp_path):
    c = full; doc = upload(c, full_project[0], api())
    draft = c.standard_contracts.propose(c.owner, doc['id'], doc['raw_digest'], '#/paths/~1payments~1{id}/get', semantics())
    base = c.k.baseline(c.owner, full_project[0])
    exported = c.history.export_archive(c.owner, base['id'])
    report = c.history.inspect_archive(c.owner, exported['path'], exported['sha256'])
    assert report
    backup = c.ops.backup(c.owner)
    target = tmp_path / 'restored'
    restore_backup(backup['path'], target, backup['sha256'])
    other = Control(target, mode='validation', start_workers=False)
    try:
        actor = other.sec.authenticate()
        restored = other.k.artifact(actor, draft['id'])
        assert restored['body'] == draft['body']
        assert other.standard_contracts.inspect(actor, doc['id'], doc['raw_digest'])['totals']['entries'] == 3
    finally: other.close()


@pytest.mark.parametrize('field', ['input', 'source_refs', 'standard_contract_material', 'standard_contract'])
def test_forged_or_weakened_generated_projection_is_rejected(full, full_project, field):
    c = full; doc = upload(c, full_project[0], api())
    draft = c.standard_contracts.propose(c.owner, doc['id'], doc['raw_digest'], '#/paths/~1payments~1{id}/get', semantics())
    body = copy.deepcopy(draft['body'])
    if field == 'input': body[field] = {'format': 'daikibo.type.v1', 'schema': {'type': 'null'}}
    elif field == 'source_refs': body[field] = []
    elif field == 'standard_contract_material': body[field]['definition']['responses'].pop('404')
    else: body[field]['complete_standard_validation'] = True
    with pytest.raises(Fault): c.k.revise(c.owner, draft['id'], 1, body, 'Attempt to replace protocol details')
    assert c.k.artifact(c.owner, draft['id'])['revision'] == 1


def test_missing_original_is_detected_before_using_an_imported_interface(full, full_project):
    c = full; doc = upload(c, full_project[0], api())
    draft = c.standard_contracts.propose(c.owner, doc['id'], doc['raw_digest'], '#/paths/~1payments~1{id}/get', semantics())
    h = doc['raw_digest']
    # Use store's content-addressed path, then simulate disk loss (not malicious access).
    path = c.s.blob_path(h)
    path.unlink()
    with pytest.raises(Fault): c.k.artifact(c.owner, draft['id'])
