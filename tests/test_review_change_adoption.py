"""Adoption must use judgments for current dependencies and the latest result."""
import copy
import sys

import pytest

from daikibo.common import Actor, Fault, timestamp
from conftest import make_task
from test_delivery_git_and_recovery import profile


REVIEWER = '''import json, sys
p = json.load(sys.stdin)
c = p['context']
bad = '--fail' in sys.argv
if p['role'] == 'delivery_profile':
    previous = c.get('previous')
    bad |= bool(previous and json.loads(previous['body'])['target_environment'] == 'New baseline')
elif 'change' in c or p['role'] == 'decision_proposal':
    bad |= any(a['statement'] == 'New constraint' for a in c.get('invariants', []))
scope_packets=[]
def walk(value):
    if isinstance(value, dict):
        scope=value.get('scope_review')
        if isinstance(scope, dict) and scope.get('format') == 'change-scope-review.v1':
            scope_packets.append((value, scope))
        for child in value.values(): walk(child)
    elif isinstance(value, list):
        for child in value: walk(child)
walk(c)
dispositions=[]
for material, scope in scope_packets:
    after_by={item.get('artifact'):item for item in material.get('before_after', [])}
    effects={}
    for item in scope.get('required_dispositions', []):
        if item.get('kind') != 'delta_effect': continue
        artifact=item['subject']; detail=after_by.get(artifact, {})
        before=detail.get('before', {}).get('body', {}); after=detail.get('after', {}).get('body', {})
        changed={key for key in set(before)|set(after) if before.get(key) != after.get(key)}
        if not changed or changed <= {'title'}:
            effects[artifact]='preserves_meaning'
        elif detail.get('kind') in {'design','component','test'} and any(
                path.get('root') == artifact for path in scope.get('upper_contracts', {}).get('upper_paths', [])):
            effects[artifact]='within_current_contract'
        else:
            effects[artifact]='changes_upper_contract'
    needs_upper=any(value in {'changes_upper_contract','unknown'} for value in effects.values())
    layer_scope='upper_scope_required' if needs_upper else 'within_scope'
    target='awaiting_product_decision' if needs_upper else scope.get('layer','local_repair')
    for item in scope.get('required_dispositions', []):
        kind=item.get('kind'); marker=item['id']
        if kind == 'layer_scope': resolution=layer_scope
        elif kind == 'layer_target': resolution=target
        elif kind == 'delta_effect': resolution=effects[item['subject']]
        elif kind == 'review_carry_and_task_fence': resolution='affected'
        elif kind == 'interface_consumer': resolution='addressed'
        elif kind == 'declared_unknown_consumer': resolution='unresolved'
        else: continue
        dispositions.append({'id':marker,'resolution':resolution,
                             'reason':'Finite test fixture reviewed the exact typed scope.'})
print(json.dumps({'verdict': 'fail' if bad else 'pass', 'rationale': 'Finite test fixture',
 'covered': c.get('required_coverage', []), 'findings': [],
 'observations': [{'ref': p['subject'], 'detail': 'Observed current review material'}],
 'dispositions': dispositions}))
'''


def register_reviewer(c, tmp_path):
    script = tmp_path / 'material_reviewer.py'
    script.write_text(REVIEWER)
    for name, args in [('material-pass', []), ('material-fail', ['--fail'])]:
        c.rt.adapters.register(c.owner, name, 'fixture', sys.executable, [str(script), *args])


def test_profile_review_cannot_follow_a_changed_previous_profile(full, full_project, tmp_path):
    c = full
    p, repo, requirement, _ = full_project
    register_reviewer(c, tmp_path)
    task = make_task(c, full_project)
    body = profile(p, repo, requirement, task)
    initial = c.d.configure(c.owner, p, body)
    proposed = {**copy.deepcopy(body), 'target_environment': 'Proposed release'}
    old = c.rt.review(c.owner, p, 'delivery_profile', 'material-pass', proposal=proposed)
    changed = {**copy.deepcopy(body), 'target_environment': 'New baseline'}
    approval = c.rt.review(c.owner, p, 'delivery_profile', 'material-pass', proposal=changed)
    current = c.d.configure(c.owner, p, changed, initial['digest'], approval['receipt'])
    new = c.rt.review(c.owner, p, 'delivery_profile', 'material-pass', proposal=proposed)
    assert new['result']['verdict'] == 'fail'
    assert c.g.receipt(old['receipt'])['binding'] != c.g.receipt(new['receipt'])['binding']
    with pytest.raises(Fault) as exc:
        c.d.configure(c.owner, p, proposed, current['digest'], old['receipt'])
    assert exc.value.code == 'stale_evidence'
    assert c.d.profile_current(c.owner, p)['digest'] == current['digest']


def test_profile_review_cannot_choose_old_pass_after_same_material_failure(full, full_project, tmp_path):
    c = full
    p, repo, requirement, _ = full_project
    register_reviewer(c, tmp_path)
    task = make_task(c, full_project)
    body = profile(p, repo, requirement, task)
    initial = c.d.configure(c.owner, p, body)
    proposed = {**body, 'reason': 'Clarify current release'}
    old = c.rt.review(c.owner, p, 'delivery_profile', 'material-pass', proposal=proposed)
    c.rt.review(c.owner, p, 'delivery_profile', 'material-fail', proposal=proposed)
    with pytest.raises(Fault):
        c.d.configure(c.owner, p, proposed, initial['digest'], old['receipt'])
    current = c.rt.review(c.owner, p, 'delivery_profile', 'material-pass', proposal=proposed)
    assert c.d.configure(c.owner, p, proposed, initial['digest'], current['receipt'])['digest']


def technical_change(c, p, artifact, receipt, statement):
    item = c.p.change(c.owner, p, {'title': 'Technical update', 'origin': 'design',
        'reason': 'Update a specification-preserving design', 'affected': [artifact['id']],
        'evidence': [receipt], 'deltas': [{'artifact': artifact['id'],
        'expected_revision': artifact['revision'], 'body': {**artifact['body'], 'statement': statement}}]})
    c.p.attempt(c.owner, item['id'], 'local_repair', {'hypothesis': 'Update design',
        'alternatives': ['Update design'], 'evidence': [receipt], 'outcome': 'solution',
        'remaining_unknown': ''})
    return item['id']


def test_technical_change_requires_current_invariants(full, full_project, tmp_path):
    c = full
    p, _, _, _ = full_project
    register_reviewer(c, tmp_path)
    policy = c.k.propose(c.owner, p, 'design', {'title': 'Constraint',
        'statement': 'Old constraint', 'critical': True})
    c.k.accept(c.owner, policy['id'], 1)
    c.k.link(c.owner,policy['id'],full_project[2],'realizes','asserted','Constraint governs this requirement')
    design = c.k.propose(c.owner, p, 'design', {'title': 'Design', 'statement': 'Old design'})
    c.k.accept(c.owner, design['id'], 1)
    c.k.link(c.owner,design['id'],full_project[2],'realizes','asserted','Design realizes this requirement')
    seed = c.rt.review(c.owner, design['id'], 'design', 'material-pass')['receipt']
    change = technical_change(c, p, design, seed, 'Updated design')
    old = c.rt.review(c.owner, change, 'consistency', 'material-pass')
    policy_change = technical_change(c, p, policy, seed, 'New constraint')
    approval = c.rt.review(c.owner, policy_change, 'consistency', 'material-pass')
    c.p.apply_technical_change(c.owner, policy_change, approval['receipt'])
    new = c.rt.review(c.owner, change, 'consistency', 'material-pass')
    assert new['result']['verdict'] == 'fail'
    assert c.g.receipt(old['receipt'])['binding'] != c.g.receipt(new['receipt'])['binding']
    with pytest.raises(Fault) as exc:
        c.p.apply_technical_change(Actor('change-agent', 'agent', p), change, old['receipt'])
    assert exc.value.code == 'stale_evidence'
    assert c.k.artifact(c.owner, design['id'])['revision'] == 1


def test_assurance_adoption_rejects_old_pass_after_new_failure(full, full_project, tmp_path):
    c = full
    p, _, requirement, _ = full_project
    register_reviewer(c, tmp_path)
    row = c.k.artifact(c.owner, requirement)
    ref = {'kind': 'artifact', 'project': p, 'artifact': row['id'],
           'revision': row['revision'], 'body_digest': row['digest']}
    scope = c.assurance.scope_propose(c.owner, p, {'roots': [ref], 'selection_rules': {},
        'exclusion_proposals': [], 'authority_refs': [], 'discovery_unknowns': []})
    proposed = c.assurance.profile_propose(c.owner, p, None, {'scope_ref': scope['scope_ref'],
        'stage_rules': {'plan': {}}, 'relation_selectors': ['realizes'], 'test_definition_bindings': []})
    root = proposed['profile']
    requirements = c.assurance._review_requirements(p, c.assurance._adoption_roots(p, root))
    refs = []
    for packet, role in requirements:
        result = c.rt.review(c.owner, packet['id'], role, 'material-pass')
        refs.append({'packet': packet['id'], 'role': role, 'id': result['receipt']})
    packet, role = requirements[0]
    c.rt.review(c.owner, packet['id'], role, 'material-fail')
    agent = Actor('assurance-agent', 'agent', p)
    with pytest.raises(Fault):
        c.assurance.adopt(agent, p, root['id'], root['digest'], None, refs)
    assert not c.assurance._object_is_current(root)
    latest = c.rt.review(c.owner, packet['id'], role, 'material-pass')
    refs[0]['id'] = latest['receipt']
    assert c.assurance.adopt(agent, p, root['id'], root['digest'], None, refs)['status'] == 'adopted'


def provisional_proposal(requirement):
    return {'title': 'Provisional design assumption', 'reason': 'A reversible technical choice',
            'options': ['approve', 'keep_existing'], 'recommendation': 'approve',
            'refs': [requirement], 'requirement_affecting': False, 'provisional': True,
            'reversible': True, 'expires': timestamp() + 3600}


def test_provisional_decision_requires_current_invariants(full, full_project, tmp_path):
    c = full
    p, _, requirement, _ = full_project
    register_reviewer(c, tmp_path)
    policy = c.k.propose(c.owner, p, 'design', {'title': 'Constraint',
        'statement': 'Old constraint', 'critical': True})
    c.k.accept(c.owner, policy['id'], 1)
    c.k.link(c.owner,policy['id'],requirement,'realizes','asserted','Constraint governs this requirement')
    proposed = provisional_proposal(requirement)
    old = c.rt.review(c.owner, p, 'decision_proposal', 'material-pass', proposal=proposed)
    change = technical_change(c, p, policy, old['receipt'], 'New constraint')
    approval = c.rt.review(c.owner, change, 'consistency', 'material-pass')
    c.p.apply_technical_change(c.owner, change, approval['receipt'])
    new = c.rt.review(c.owner, p, 'decision_proposal', 'material-pass', proposal=proposed)
    assert new['result']['verdict'] == 'fail'
    assert c.g.receipt(old['receipt'])['binding'] != c.g.receipt(new['receipt'])['binding']
    with pytest.raises(Fault) as exc:
        c.p.propose_decision(Actor('proposal-agent', 'agent', p), p,
                             {**proposed, 'consistency_receipt': old['receipt']})
    assert exc.value.code == 'stale_evidence'
    assert not c.s.one("SELECT id FROM decisions WHERE project=? AND status='provisional'", (p,))


def test_provisional_decision_requires_latest_valid_judgment(full, full_project, tmp_path):
    c = full
    p, _, requirement, _ = full_project
    register_reviewer(c, tmp_path)
    proposed = provisional_proposal(requirement)
    old = c.rt.review(c.owner, p, 'decision_proposal', 'material-pass', proposal=proposed)
    c.rt.review(c.owner, p, 'decision_proposal', 'material-fail', proposal=proposed)
    agent = Actor('proposal-agent', 'agent', p)
    with pytest.raises(Fault):
        c.p.propose_decision(agent, p, {**proposed, 'consistency_receipt': old['receipt']})
    new = c.rt.review(c.owner, p, 'decision_proposal', 'material-pass', proposal=proposed)
    decision = c.p.propose_decision(agent, p, {**proposed, 'consistency_receipt': new['receipt']})
    assert c.s.one('SELECT status FROM decisions WHERE id=?', (decision['id'],))['status'] == 'provisional'


CLI_REVIEWER = '''import json, sys
from pathlib import Path
if '--version' in sys.argv:
    print('explicit protocol test double 1')
    raise SystemExit(0)
p = json.load(sys.stdin)
result = {'verdict': 'fail' if '--fail' in sys.argv else 'pass',
 'rationale': 'Protocol test double, not an LLM judgment',
 'covered': p['context'].get('required_coverage', []), 'findings': [],
 'observations': [{'ref': p['subject'], 'detail': 'Read the supplied packet'}],
 'dispositions': []}
if '-p' in sys.argv:
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                     'structured_output': result, 'session_id': 'protocol-fixture'}))
else:
    Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(result))
    print(json.dumps({'type': 'thread.started', 'thread_id': 'protocol-fixture'}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result)}}))
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
'''


@pytest.mark.parametrize('kind', ['claude', 'codex'])
def test_traceability_adoption_requires_latest_packet_judgment(full, full_project, tmp_path, kind):
    c = full
    p = full_project[0]
    script = tmp_path / 'protocol_double.py'
    script.write_text('#!' + sys.executable + '\n' + CLI_REVIEWER)
    script.chmod(0o755)
    c.rt.adapters.register(c.owner, 'protocol-pass', kind, str(script))
    c.rt.adapters.register(c.owner, 'protocol-fail', kind, str(script), ['--fail'])
    source = c.k.source(c.owner, p, 'Preserve every traceability source item.')
    proposed = c.traceability.propose(c.owner, p, kind='document', scope={'source': source['id']})
    extracted = c.traceability.extract(c.owner, proposed['id'])
    first = c.traceability.review_subject(c.owner, proposed['id'])
    refs = []
    for index in range(first['packet']['packet_count']):
        page = c.traceability.review_subject(c.owner, proposed['id'], packet=index)
        result = c.rt.review(c.owner, page['subject'], page['packet']['role'], 'protocol-pass')
        refs.append(result['receipt'])
    latest_fail = c.rt.review(c.owner, first['subject'], first['packet']['role'], 'protocol-fail')
    assert latest_fail['result']['verdict'] == 'fail'
    assert c.g.receipt(refs[0])['simulated'] is False
    assert c.rt.adapters.get('protocol-pass')['qualified'] is False
    with pytest.raises(Fault) as exc:
        c.traceability.adopt(c.owner, p, revision=extracted['revision'],
                            subject=proposed['id'], review_refs=refs)
    assert exc.value.code == 'stale_evidence'
    assert c.s.one('SELECT status FROM traceability_revisions WHERE id=?',
                   (extracted['revision'],))['status'] != 'active'
    latest_pass = c.rt.review(c.owner, first['subject'], first['packet']['role'], 'protocol-pass')
    refs[0] = latest_pass['receipt']
    result = c.traceability.adopt(c.owner, p, revision=extracted['revision'],
                                subject=proposed['id'], review_refs=refs)
    assert result['adopted'] and result['effective_status'] == 'active'
