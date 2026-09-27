"""Fresh governed protocol fixture derived from retained canonical producer.

The finite subprocess reviewer is not live LLM or independent semantic evidence.
"""
import json, os, subprocess, tempfile, traceback
from pathlib import Path
from daikibo.control import Control
from daikibo.common import Fault, digest
from test_unit5_governed_subprocess_fixture import _write_finite_codex
from daikibo.assurance_relations import REGISTRY_V2_DIGEST
from daikibo.task_revisions import task_definition_digest
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.delivery_material_reader import read_delivery_material
from test_e3_unit2b_delivery_criteria import _receipt_delivery_snapshot

def aref(c, p, ident):
    r = c.s.one('SELECT * FROM artifacts WHERE id=? AND project=?', (ident, p), True)
    return {'kind': 'artifact', 'project': p, 'artifact': ident, 'revision': r['revision'], 'body_digest': r['digest']}

def tref(c, p, ident):
    r = c.s.one('SELECT * FROM tasks WHERE id=? AND project=?', (ident, p), True)
    body = json.loads(r['body'])
    return {'kind': 'task_revision', 'project': p, 'task': ident, 'revision': r['revision'], 'definition_digest': task_definition_digest(body)}

def accept(c, p, kind, title, **extra):
    x = c.k.propose(c.owner, p, kind, {'title': title, 'statement': title, **extra})
    return c.k.accept(c.owner, x['id'], 1)

def reviews_adopt(c, p, root, adapter, expected_head=None):
    refs = []
    roots = c.assurance._adoption_roots(p, root)
    for packet, role in c.assurance._review_requirements(p, roots):
        e = c.rt.review(c.owner, packet['id'], role, adapter)
        assert e['result']['verdict']=='pass', e
        refs.append({'packet': packet['id'], 'role': role, 'id': e['receipt']})
    adopted = c.assurance.adopt(c.owner, p, root['id'], root['digest'], expected_head, refs)
    return {'status': adopted['status'], 'id': adopted['id'], 'subject': adopted['subject']}

def git_commit(root):
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'unit5-canonical', 'GIT_AUTHOR_EMAIL': 'unit5-canonical@example.invalid', 'GIT_COMMITTER_NAME': 'unit5-canonical', 'GIT_COMMITTER_EMAIL': 'unit5-canonical@example.invalid'}
    subprocess.run(['git', '-c', 'user.name=unit5-canonical', '-c', 'user.email=unit5-canonical@example.invalid', 'commit', '-qm', 'canonical source'], cwd=root, check=True, env=env)
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()

def adopt_trace(c, p, subject, revision, adapter):
    packet = c.traceability.review_subject(c.owner, subject)
    ev = c.rt.review(c.owner, packet['subject'], packet['packet']['role'], adapter)
    assert ev['result']['verdict'] == 'pass', (subject, ev)
    return c.traceability.adopt(c.owner, p, revision=revision, subject=subject, review_refs=[ev['receipt']])

def git_file_ref(c, p, revision, repo, path):
    row = c.s.one('SELECT * FROM traceability_revisions WHERE id=?', (revision,), True)
    body = json.loads(row['body'])
    item = next((x for x in body['inventory'] if x['path'] == path))
    return {'ref_type': 'git_file', 'repository': repo, 'object_format': body['scope']['object_format'], 'commit': body['scope']['commit'], 'path': path, 'blob_oid': item['blob_oid'], 'sha256': item['sha256'], 'mode': item['mode'], 'pin_revision': revision, 'pin_revision_digest': row['digest']}

def populate_realizes(c, p, realize_sources, reqs, scope, prof):
    for source_id in realize_sources:
        for req in reqs:
            owned_oids = [x['id'] for x in scope['obligations']['body']['obligations'] if x.get('source_ref', {}).get('artifact') == req]
            edge = c.assurance.edge_propose(c.owner, p, {'source_ref': aref(c, p, source_id), 'target_ref': aref(c, p, req), 'relation': 'realizes', 'relation_contract_digest': REGISTRY_V2_DIGEST, 'scope_ref': prof['profile_ref'], 'claim': 'shared design/interface realizes requirement', 'obligation_ids': owned_oids, 'required_evidence_refs': [], 'authority_refs': []})
            reviews_adopt(c, p, edge['edge'], 'finite')
        aset = c.assurance.set_propose(c.owner, p, {'center_ref': aref(c, p, source_id), 'relation': 'realizes', 'direction': 'outgoing', 'relation_contract_digest': REGISTRY_V2_DIGEST, 'scope_ref': prof['profile_ref'], 'criteria': {}, 'required_evidence_refs': []})
        aset.get('missing_obligations')
        reviews_adopt(c, p, aset['set'], 'finite')

def canonical_flow(base, *, profile_format='assurance.profile.v4', relation_ready=True):
    base.mkdir(parents=True, exist_ok=True)
    c = Control(base / 'control', mode='governed', start_workers=False)
    c.owner = c.sec.authenticate(Path(c.sec.bootstrap()).read_text())
    p = c.k.create_project(c.owner, 'canonical phase probe')['id']
    repos = []
    repo_commits = []
    for label in ('alpha', 'beta'):
        root = base / label
        root.mkdir()
        (root / 'calc.py').write_text('def add(a,b):\n    return a+b\n')
        (root / 'test_calc.py').write_text('from calc import add\n\ndef test_add():\n    assert add(2,3)==5\n    assert add(-2,1)==-1\n')
        commit = git_commit(root)
        repos.append(c.sn.register(c.owner, p, label, str(root))['id'])
        repo_commits.append(commit)
    src = c.k.source(c.owner, p, 'Both repositories expose exact integer addition. AC-ALPHA and AC-BETA are required.')
    reqs = []
    for label in ('ALPHA', 'BETA'):
        req = accept(c, p, 'requirement', label + ' exact sum', source_refs=[src['id']], acceptance=['AC-' + label])
        reqs.append(req['id'])
    c.k.classify(c.owner, src['id'], 0, src['characters'], 'requirement', reqs, 'complete source')
    partition = c.traceability.propose(c.owner, p, kind='document', scope={'source': src['id']})
    c.traceability.extract(c.owner, partition['id'])
    sc = accept(c, p, 'scenario', 'Shared repository journey', source_refs=[src['id']], success_journey=['sum in alpha and beta'], failure_journey=['block'], concurrent_journey=['independent'], nonfunctional=['deterministic'])
    dom = accept(c, p, 'domain', 'Shared arithmetic domain', source_refs=[src['id']], responsibilities=['integer addition in both repositories'], non_responsibilities=['network'], owned_data=['arithmetic'], interfaces=[])
    iface = accept(c, p, 'interface', 'Shared arithmetic interface', source_refs=[src['id']], input='two integers', output='sum', authentication='none', errors='out of scope', idempotency='pure', compatibility='stable', consumers=['alpha', 'beta'], verification='pytest')
    find = accept(c, p, 'finding', 'Shared arithmetic feasibility', source_refs=[src['id']], hypothesis='Python integer addition is exact', bounded_experiment='inspect', observed_results='local source', remaining_unknowns=['implementation'])
    shared_design = accept(c, p, 'design', 'Shared exact addition design', source_refs=[src['id']], responsibilities=['sum in both repositories'], rejected_alternatives=['subtract'], failure_handling='tests', rollout='git')
    test = accept(c, p, 'test', 'Shared arithmetic test', source_refs=[src['id']], cases=['positive', 'negative'], command='pytest', statement='exact sum')
    for req in reqs:
        c.k.link(c.owner, shared_design['id'], req, 'realizes', 'asserted', 'shared design')
        c.k.link(c.owner, iface['id'], req, 'realizes', 'asserted', 'shared interface')
        c.k.link(c.owner, test['id'], req, 'verifies', 'asserted', 'shared test')
    artifacts = [(dom['id'], shared_design['id'], test['id'], iface['id']) for _ in reqs]
    for r in repos:
        c.idx.index(c.owner, r)
    exe = base / 'finite'
    _write_finite_codex(exe)
    c.rt.adapters.register(c.owner, 'finite', 'codex', str(exe))
    assert c.supervisor.qualify(c.owner, p, 'finite')['qualified']
    program = c.p.begin(c.owner, p, src['id'], mode='brownfield', compact=True)['program']
    roots = [aref(c, p, x) for x in reqs]
    v5 = profile_format == 'assurance.profile.v5'
    if v5: roots += [aref(c,p,x['id']) for x in (dom,shared_design,iface)]
    scope = c.assurance.scope_propose(c.owner, p, {**({'format':'assurance.scope.v2'} if v5 else {}), 'roots': roots, 'selection_rules': {}, 'exclusion_proposals': [], 'authority_refs': [], 'discovery_unknowns': []})
    relation_set = {'relation': 'realizes', 'direction': 'outgoing', 'centers': ['realization_sources' if profile_format in {'assurance.profile.v4','assurance.profile.v5'} else 'design_artifacts']}
    output_set = {'relation': 'produced_by', 'direction': 'incoming', 'centers': ['assigned_tasks']}
    contains_set = {'relation': 'contains', 'direction': 'outgoing', 'centers': ['delivery_snapshots']}
    delivery_output_set = {'relation': 'produced_by', 'direction': 'outgoing', 'centers': ['delivery_snapshots']}
    actual_contains_set = {'relation': 'contains', 'direction': 'outgoing', 'centers': ['actual_commits']}
    stages = {'plan': {'denominator': 'program_plan', 'relation_sets': [relation_set], 'node_rules': ['consistency', 'design', 'requirements', 'test_plan'], 'execution_results': 'none'}, 'task': {'denominator': 'assigned_task_contributors', 'relation_sets': sorted([relation_set, output_set], key=lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))), 'node_rules': ['consistency', 'design', 'requirements', 'test_plan'], 'execution_results': 'assigned_checks'}, 'integration': {'denominator': 'program_integration', 'relation_sets': sorted([delivery_output_set, contains_set], key=lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))), 'node_rules': ['consistency', 'design', 'requirements', 'test_plan'], 'execution_results': 'integration_checks'}, 'delivery': {'denominator': 'actual_delivery', 'relation_sets': sorted([delivery_output_set, contains_set, actual_contains_set], key=lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))), 'node_rules': ['consistency', 'design', 'requirements', 'test_plan'], 'execution_results': 'certified_integration_and_actual_outputs'}}
    profile_body = {'format': profile_format, 'project': p, 'program': program, 'scope_ref': scope['scope_ref'], 'obligations_ref': scope['obligations_ref'], 'previous_selection_ref': None, 'application_mode': 'mandatory', 'stage_rules': stages, 'node_review_rules': [{'id': 'consistency', 'selector': 'interface', 'roles': ['consistency']}, {'id': 'design', 'selector': 'design', 'roles': ['design']}, {'id': 'requirements', 'selector': 'requirement', 'roles': ['requirements']}, {'id': 'test_plan', 'selector': 'test_plan', 'roles': ['test_plan']}], 'relation_selectors': ['contains', 'produced_by', 'realizes'], 'test_definition_bindings': [], 'change_reason': 'canonical phase probe', 'authority_refs': [], 'required_relation_contract_digest': REGISTRY_V2_DIGEST}
    if v5:
        profile_body.update(required_scope_contract='assurance.scope.v2',required_node_contract='assurance.node-contract.v2')
        profile_body['node_review_rules'].append({'id':'domain','selector':'domain','roles':['domain_responsibility']})
        profile_body['node_review_rules'].sort(key=lambda x:x['id'])
        for stage in stages.values(): stage['node_rules']=sorted([*stage['node_rules'],'domain'])
        for name in ('plan','task'):
            stages[name]['relation_sets'].append({'relation':'implements','direction':'incoming','centers':['design_artifacts']})
            stages[name]['relation_sets'].sort(key=lambda x:json.dumps(x,sort_keys=True,separators=(',',':')))
        profile_body['relation_selectors']=sorted([*profile_body['relation_selectors'],'implements'])
    prof = c.assurance.profile_propose(c.owner, p, program, profile_body, None)
    if v5: c.rt.review(c.owner,dom['id'],'domain_responsibility','finite')
    reviews_adopt(c, p, prof['profile'], 'finite')
    for subject, role in ((shared_design['id'], 'design'), (iface['id'], 'consistency')):
        review = c.rt.review(c.owner, subject, role, 'finite')
    realize_sources = [shared_design['id'], iface['id']]
    if relation_ready:
        populate_realizes(c, p, realize_sources, reqs, scope, prof)
    for req in reqs:
        ev = c.rt.review(c.owner, req, 'requirements', 'finite')
    tasks = []
    for i, label in enumerate(('ALPHA', 'BETA')):
        req = reqs[i]
        dom, design, test, iface = artifacts[i]
        manifest = {'format': 'daikibo.artifact-output.v1', 'outputs': [{'declaration_id': 'artifact-result', 'kind': 'finding', 'body': {'title': label + ' output', 'statement': 'Observed ' + label}}]}
        goal = {'calc.py': 'def add(a,b):\n    return a+b\n', 'artifact-output.json': json.dumps(manifest, sort_keys=True)}
        body = {'title': label + ' task', 'goal': 'WRITE:' + json.dumps(goal), 'read_artifacts': [req, dom, design, iface, test], 'write_paths': ['calc.py', 'artifact-output.json'], 'acceptance': ['AC-' + label], 'acceptance_refs': [{'requirement': req, 'acceptance': 'AC-' + label}], 'dependencies': [], 'repos': [repos[i]], 'non_goals': ['network', 'tests'], 'workflow_id': program, 'structural_obligations': {'format': 'daikibo.task-structural-obligations.v1', 'required_outputs': [{'id': 'artifact-result', 'statement': 'one finding', 'artifact_refs': [aref(c, p, req)], 'realization_kind': 'artifact'}], 'required_exercises': []}}
        t = c.w.create(c.owner, p, body)
        tasks.append(t['id'])
        c.w.plan_tests(c.owner, t['id'], {'checks': [{'id': 'unit', 'argv': ['python', '-m', 'pytest', '-q', 'test_calc.py'], 'kind': 'pytest', 'required_tests': ['test_add']}]})
        plan_row = c.s.one('SELECT body FROM plans WHERE task=?', (t['id'],), True)
        plan_review = c.rt.review(c.owner, t['id'], 'test_plan', 'finite', proposal=json.loads(plan_row['body']))
    checks = []
    for i, rid in enumerate(repos):
        label = ('ALPHA', 'BETA')[i]
        output_id = label.lower() + '-build'
        output_path = '.daikibo-build/' + label.lower() + '.txt'
        checks += [{'id': label + '-build', 'category': 'build', 'repo': rid, 'kind': 'command', 'argv': ['python', '-c', f"from pathlib import Path; Path('{output_path}').parent.mkdir(exist_ok=True); Path('{output_path}').write_text('{label} build')"], 'purpose': 'compile', 'produces': [output_id]}, {'id': label + '-start', 'category': 'start', 'repo': rid, 'kind': 'command', 'argv': ['python', '-c', 'import calc; assert calc.add(2,3)==5'], 'purpose': 'start', 'uses': [output_id]}, {'id': label + '-smoke', 'category': 'smoke', 'repo': rid, 'kind': 'command', 'argv': ['python', '-c', 'import calc; assert calc.add(-2,1)==-1'], 'purpose': 'smoke'}, {'id': label + '-integration', 'category': 'integration', 'repo': rid, 'kind': 'command', 'argv': ['python', '-c', 'import calc; assert calc.add(0,0)==0'], 'purpose': 'integration'}, {'id': label + '-scenario', 'category': 'scenario', 'repo': rid, 'kind': 'pytest', 'argv': ['python', '-m', 'pytest', '-q', 'test_calc.py'], 'required_tests': ['test_add'], 'purpose': 'scenario'}]
        if i == 0:
            build_outputs = [{'id': output_id, 'repo': rid, 'path': output_path}]
        else:
            build_outputs.append({'id': output_id, 'repo': rid, 'path': output_path})
    profbody = {'program': program, 'target_environment': 'CPython 3.13 local', 'required_requirements': reqs, 'required_tasks': tasks, 'repo_order': repos, 'rollback': 'restore initial commits', 'applicability': {'migration': {'applicable': False, 'reason': 'no state'}, 'security': {'applicable': False, 'reason': 'pure local'}, 'performance': {'applicable': False, 'reason': 'no SLA'}, 'contract': {'applicable': False, 'reason': 'No external contract migration'}}, 'checks': checks, 'build_outputs': build_outputs}
    c.d.configure(c.owner, p, profbody)
    trace_info = []
    for i, repo in enumerate(repos):
        pop = c.traceability.propose(c.owner, p, kind='code', name='trace-' + ('alpha', 'beta')[i], scope={'repository': repo, 'commit': repo_commits[i]})
        ext = c.traceability.extract(c.owner, pop['id'])
        rev = ext['revision']
        adopt_trace(c, p, pop['id'], rev, 'finite')
        req = reqs[i]
        req_row = c.s.one('SELECT * FROM artifacts WHERE id=? AND project=?', (req, p), True)
        req_ref = {'ref_type': 'artifact_ac', 'artifact': req, 'revision': req_row['revision'], 'body_digest': req_row['digest'], 'ac_pointer': '/acceptance/0', 'ac_digest': digest('AC-' + ('ALPHA', 'BETA')[i])}
        leaf_rows = c.s.all('SELECT * FROM traceability_items WHERE revision=? AND leaf=1 AND status=? ORDER BY ordinal,id', (rev, 'known'))
        assert leaf_rows, (repo, rev)
        contributor = [{'task': tasks[i], 'revision': 1, 'required': True}]
        decisions = []
        target_paths = []
        for leaf_row in leaf_rows:
            leaf_body = json.loads(leaf_row['body'])
            path = leaf_body.get('path')
            if path and path not in target_paths:
                target_paths.append(path)
            decisions.append({'item': leaf_row['id'], 'handling': 'port', 'reason': 'canonical repository implementation', 'evidence': [src['id']], 'purpose': 'code_port', 'task': tasks[i], 'contributors': contributor, 'requirement': req_ref, 'acceptance': req_ref})
        dec = c.traceability.decide_propose(c.owner, p, rev, decisions)
        adopt_trace(c, p, dec['proposal'], rev, 'finite')
        targets = [git_file_ref(c, p, rev, repo, path) for path in target_paths]
        mapping = c.traceability.map_propose(c.owner, p, rev, [{'leaf_ids': [row['id'] for row in leaf_rows], 'purpose': 'code_port', 'decision_ref': dec['id'], 'contributors': contributor, 'target_refs': targets, 'evidence_refs': [src['id']]}])
        adopt_trace(c, p, mapping['proposal'], rev, 'finite')
        trace_info.append({'revision': rev, 'requirement': req, 'requirement_ref': req_ref, 'mapping': mapping['id'], 'leaves': [row['id'] for row in leaf_rows], 'task': tasks[i], 'repo': repo})
        len(leaf_rows)
    units = [{'id': 'system', 'title': 'all', 'parent': None, 'domain': None, 'rationale': 'aggregate', 'obligations': [], 'tasks': [], 'interfaces': [], 'dependencies': []}]
    for i, label in enumerate(('alpha', 'beta')):
        req = reqs[i]
        dom, design, test, iface = artifacts[i]
        units.append({'id': label, 'title': label, 'parent': 'system', 'domain': dom, 'rationale': 'owned', 'obligations': [{'requirement': req, 'acceptance': 'AC-' + ('ALPHA', 'BETA')[i]}], 'tasks': [tasks[i]], 'interfaces': [], 'dependencies': []})
    bd = c.breakdowns.propose(c.owner, program, 'canonical', 'all', units)
    for packet in c.breakdowns.get(c.owner, bd['id'])['packets']:
        for role in ('design', 'trace'):
            ev = c.rt.review(c.owner, packet['id'], role, 'finite')
            len(ev['result']['covered'])
    if v5: populate_implements(c,p,[dom,shared_design['id'],iface],trace_info[0]['revision'],repos[0],scope,prof)
    return {'control': c, 'base': base, 'project': p, 'program': program, 'requirements': reqs, 'domain': dom, 'design': shared_design['id'], 'interface': iface, 'tasks': tasks, 'repos': repos, 'scope': scope, 'profile': prof, 'profile_body': profile_body, 'breakdown': bd['id'], 'realize_sources': realize_sources}


def populate_implements(c,p,targets,revision,repository,scope,prof):
    for target in targets:
        source={'kind':'traceability_ref','project':p,'locator':git_file_ref(c,p,revision,repository,'calc.py')}
        ids=[x['id'] for x in scope['obligations']['body']['obligations'] if x.get('source_ref',{}).get('artifact')==target]
        edge=c.assurance.edge_propose(c.owner,p,{'source_ref':source,'target_ref':aref(c,p,target),
            'relation':'implements','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':prof['profile_ref'],
            'claim':'Pinned implementation covers the complete selected target responsibility population',
            'obligation_ids':ids,'required_evidence_refs':[],'authority_refs':[]})
        reviews_adopt(c,p,edge['edge'],'finite')
        aset=c.assurance.set_propose(c.owner,p,{'center_ref':aref(c,p,target),'relation':'implements','direction':'incoming',
            'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':prof['profile_ref'],'criteria':{},'required_evidence_refs':[]})
        reviews_adopt(c,p,aset['set'],'finite')
