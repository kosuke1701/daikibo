"""DRI-01: retained malformed material keeps Q population, never complete proof.

Malformed retained rows are injected only for compatibility diagnostics; ordinary
Knowledge writers must continue to reject these bodies.
"""
import copy
import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_node_reviews import domain_review_material
from daikibo.domain_responsibility import responsibility_material, responsibility_records
from test_e3_selection_contract import _fixture
from test_unit4p_domain_profile_v4 import _snapshot
from test_domain_responsibility_v5 import scope_v2
from unit4p_domain_fixture import aref, accept

SUPPLEMENT = {'format': 'daikibo.structural-obligations.v1', 'responsibilities': [
    {'id': 'supplement', 'type': 'statement', 'statement': 'separate duty'}]}


@pytest.mark.parametrize('case', ['empty_after', 'invalid_before', 'mixed', 'missing', 'null', 'structural_null'])
def test_DRI01_retained_population_and_diagnostics_match_base_contract(full, case):
    p, source, _, program, _ = _fixture(full)
    domain = accept(full, p, 'domain', 'retained partial', source_refs=[source['id']],
                    responsibilities=['valid duty', 'other duty'], non_responsibilities=[], owned_data=[], interfaces=[])
    body = copy.deepcopy(domain['body'])
    body['structural_obligations'] = copy.deepcopy(SUPPLEMENT)
    if case == 'empty_after': body['responsibilities'] = ['valid duty', '']
    elif case == 'invalid_before': body['responsibilities'] = ['', 'valid duty']
    elif case == 'mixed': body['responsibilities'] = [None, 'valid duty', 1, 'other duty', ' ', '\x00']
    elif case == 'missing': body.pop('responsibilities')
    elif case == 'null': body['responsibilities'] = None
    else: body['structural_obligations'] = None
    # The public writer still rejects the malformed body without DB/CAS writes.
    before = _snapshot(full)
    with pytest.raises(Fault): full.k.propose(full.owner, p, 'domain', body)
    assert _snapshot(full) == before
    full.s.execute('DROP TRIGGER revisions_no_update')
    full.s.execute('UPDATE artifacts SET body=?,digest=? WHERE id=?', (canonical(body).decode(), digest(body), domain['id']))
    full.s.execute('UPDATE revisions SET body=?,digest=? WHERE artifact=?', (canonical(body).decode(), digest(body), domain['id']))
    ref = aref(full, p, domain['id'])
    before = _snapshot(full)
    den = derive_denominator(collect_stage_context(full, full.owner, project=p, program=program, stage='plan'))
    actual = [q for q in den['obligations'] if q['source_ref'].get('artifact') == domain['id']]
    # Independent old identity formula; array offsets are never compacted.
    values = []
    canonical_values = body.get('responsibilities')
    if isinstance(canonical_values, list):
        values.extend(('artifact_responsibility', f'/responsibilities/{i}', value)
                      for i, value in enumerate(canonical_values)
                      if type(value) is str and value.strip() and '\x00' not in value)
    if case != 'structural_null':
        values.append(('artifact_structural_responsibility', '/structural_obligations/responsibilities/0', SUPPLEMENT['responsibilities'][0]))
    expected = []
    for category, pointer, value in values:
        identity = {'category': category, 'source_ref': ref, 'pointer': pointer, 'value_digest': digest(value)}
        expected.append({'id': 'obligation:' + digest(identity), **identity, 'contributors': [], 'introduced_at': 'plan', 'required_at': 'plan'})
    assert actual == sorted(expected, key=lambda q: q['id'])
    diagnostics = [x for x in den['unresolved'] if x.get('artifact') == domain['id']]
    if case in {'missing', 'null'}:
        assert diagnostics == [{'code': 'artifact_responsibilities_unavailable',
            'reason': 'Domain responsibility array is missing from retained artifact body',
            'artifact': domain['id'], 'status': 'legacy_unavailable'}]
    elif case == 'structural_null':
        assert diagnostics == [{'code': 'artifact_structural_invalid',
            'reason': 'Stored artifact structural_obligations is invalid and cannot be verified',
            'artifact': domain['id'], 'detail': 'explicit_null'}]
    else:
        invalid = [i for i, value in enumerate(canonical_values)
                   if type(value) is not str or not value.strip() or '\x00' in value]
        assert diagnostics == [{'code': 'artifact_responsibility_invalid',
            'reason': 'Domain responsibility is not a nonempty string', 'artifact': domain['id'], 'index': i} for i in invalid]
    assert den['unresolved']
    assert _snapshot(full) == before
    with pytest.raises(Fault): scope_v2(full, p, [ref])
    with pytest.raises(Fault): domain_review_material(full, full.owner, p, full.s.one('SELECT * FROM artifacts WHERE id=?', (domain['id'],)))
    with pytest.raises(Fault): responsibility_records(ref, 'domain', body)
    # A saved DOMAIN packet cannot certify the same partial body as complete.
    from daikibo.domain_responsibility import validate_domain_history
    artifact = {'id': domain['id'], 'project': p, 'kind': 'domain',
                'revision': ref['revision'], 'digest': ref['body_digest'], 'body': body}
    material = {'format': 'assurance.domain-node-review.v1', 'node_contract': 'assurance.node-contract.v2',
                'node_ref': ref, 'artifact': artifact, 'sources': [], 'accepted_invariants': [],
                'responsibility_obligations': actual, 'dependency_refs': [], 'required_coverage': []}
    prompt = canonical({'context': {'domain_review': material}})
    retained = {'runs': [{'id': 'retained-run', 'role': 'domain_responsibility',
                         'body': {'input_digest': digest(prompt)}}]}
    with pytest.raises(Fault):
        validate_domain_history(retained, p, lambda _: artifact, lambda _: None, lambda _: prompt)
    assert _snapshot(full) == before


@pytest.mark.parametrize('structural', [None, {}, {'format': SUPPLEMENT['format'], 'responsibilities': [SUPPLEMENT['responsibilities'][0], {'type': 'unknown'}]}])
def test_DRI01_invalid_structural_declaration_preserves_canonical_material(structural):
    body = {'responsibilities': ['valid duty'], 'structural_obligations': structural}
    ref = {'kind': 'artifact', 'project': 'project', 'artifact': 'domain', 'revision': 1, 'body_digest': digest(body)}
    records, dependencies, diagnostics = responsibility_material(ref, 'domain', body)
    assert [x['pointer'] for x in records] == ['/responsibilities/0']
    assert dependencies == []
    assert [x.unresolved['code'] for x in diagnostics] == ['artifact_structural_invalid']
    with pytest.raises(Fault): responsibility_records(ref, 'domain', body)
