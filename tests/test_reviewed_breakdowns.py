"""Recorded local protocol runs; these fixtures do NOT make semantic judgments."""
from __future__ import annotations
import copy
import json
import sqlite3
import sys
from pathlib import Path
import pytest
from daikibo.common import Actor, Fault, canonical, digest, parse_json
from daikibo.breakdowns import topological
from conftest import ensure_current_root, finish_task, seed_execution_phase_material


def accepted(c,p,kind,title,**extra):
    if 'source_refs' not in extra:
        source = c.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (p,))
        if source is not None:
            extra['source_refs'] = [source['id']]
    body={'title':title,'statement':title,**extra}
    item=c.k.propose(c.owner,p,kind,body)
    c.k.accept(c.owner,item['id'],1)
    return item['id']


def make_domain(c,p,title='Arithmetic',data=None):
    source = c.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (p,))
    extra = {'source_refs': [source['id']]} if source is not None else {}
    return accepted(c,p,'domain',title,responsibilities=['Arithmetic'],non_responsibilities=['Billing'],
                    owned_data=data or [],interfaces=[],**extra)


def make_work(c,p,r,q,domain,other_reads=(),deps=(),acs=('AC-ADD',),phase=None):
    goal_files = {'calc.py': 'def add(a,b):\n    return a+b\n'}
    write_paths = ['calc.py']
    if phase is None:
        source = c.s.one(
            "SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (p,), True,
        )
        goal_files['artifact-output.json'] = json.dumps({
            'format': 'daikibo.artifact-output.v1',
            'outputs': [{
                'declaration_id': 'fixture-result', 'kind': 'finding',
                'body': {'title': 'Fixture result',
                         'statement': 'The bounded fixture emits its result.',
                         'source_refs': [source['id']]},
            }],
        }, sort_keys=True)
        write_paths.append('artifact-output.json')
    body={'title':'Arithmetic task','goal':'WRITE:'+json.dumps(goal_files),
          'read_artifacts':[q,domain,*other_reads],'write_paths':write_paths, 'acceptance':list(acs),
          'dependencies':list(deps),'repos':[r],'non_goals':[]}
    requirement = c.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (q, p), True)
    body['structural_obligations'] = {
        'format': 'daikibo.task-structural-obligations.v1',
        'required_outputs': [{
            'id': 'fixture-result', 'statement': 'The bounded fixture emits its result.',
            'artifact_refs': [{
                'kind': 'artifact', 'project': p, 'artifact': requirement['id'],
                'revision': requirement['revision'], 'body_digest': requirement['digest'],
            }],
            'realization_kind': 'artifact',
        }],
        'required_exercises': [],
    }
    if phase:
        body['phase']=phase;body['write_paths']=['.daikibo-research/experiment.py']
    task=c.w.create(c.owner,p,body)['id']
    c.w.plan_tests(c.owner,task,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest','required_tests':['test_add']}]})
    return task


def leaf(ident,domain,tasks,req,acs=('AC-ADD',),parent=None,interfaces=(),dependencies=()):
    return {'id':ident,'title':ident,'parent':parent,'domain':domain,'rationale':'One cohesive responsibility, explicit data and interface ownership',
            'obligations':[{'requirement':req,'acceptance':ac} for ac in acs], 'tasks':list(tasks),'interfaces':list(interfaces),'dependencies':list(dependencies)}


def parent(ident,above=None):
    return {'id':ident,'title':ident,'parent':above,'domain':None,'rationale':'Aggregate of independently checked children',
            'obligations':[],'tasks':[],'interfaces':[],'dependencies':[]}


@pytest.fixture
def setup(full,full_project,tmp_path):
    c=full;p,r,q,root=full_project
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    program=c.p.begin(c.owner,p,source)['program']
    domain=make_domain(c,p);task=make_work(c,p,r,q,domain)
    seed_execution_phase_material(c, p, [q])
    units=[parent('system'),leaf('arithmetic',domain,[task],q,parent='system')]
    script=tmp_path/'review_protocol.py'
    script.write_text('''import json,sys
p=json.load(sys.stdin); context=p.get('context',{})
print(json.dumps({'verdict':'pass','rationale':'TEST DOUBLE only: echo required markers; no semantic judgment',
'covered':context.get('required_coverage') or context.get('task',{}).get('acceptance',[]),'findings':[], 'observations':[{'ref':p['subject'],'detail':'Observed local fixture invocation.'}],'dispositions':[]}))
''')
    c.rt.adapters.register(c.owner,'markers','fixture',sys.executable,[str(script)])
    partition = c.traceability.propose(c.owner, p, kind='document', scope={'source': source})
    c.traceability.extract(c.owner, partition['id'])
    return c,p,r,q,program,domain,task,units


def _bootstrap_mandatory_profile(c, project, program, requirements, tasks):
    """Select the real canonical profile used by new-program fixtures.

    Unit4-P plan admission is now part of the public breakdown lifecycle.  The
    older breakdown tests predate that boundary, so their fixture must perform
    the same public scope/profile proposal and adoption that a new program
    does.  The profile keeps future output production deferred while its node
    reader consumes actual current Task-plan receipts.
    """
    from test_e3_selection_contract import _adopt, _artifact_ref, _register_fixture_review

    roots = []
    for ident in requirements:
        row = c.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (ident, project), True)
        roots.append(_artifact_ref(project, row))
    scope = c.assurance.scope_propose(
        c.owner, project,
        {"roots": roots, "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []},
    )
    relation_set = {
        "relation": "produced_by", "direction": "incoming",
        "centers": ["assigned_tasks"],
    }
    stages = {
        name: {
            "denominator": denominator,
            "relation_sets": [relation_set],
            "node_rules": ["plan-review"],
            "execution_results": execution,
        }
        for name, denominator, execution in (
            ("plan", "program_plan", "none"),
            ("task", "assigned_task_contributors", "assigned_checks"),
            ("integration", "program_integration", "integration_checks"),
            ("delivery", "actual_delivery", "certified_integration_and_actual_outputs"),
        )
    }
    body = {
        "format": "assurance.profile.v2", "project": project, "program": program,
        "scope_ref": scope["scope_ref"], "obligations_ref": scope["obligations_ref"],
        "previous_selection_ref": None, "application_mode": "mandatory",
        "stage_rules": stages,
        "node_review_rules": [{"id": "plan-review", "selector": "test_plan", "roles": ["test_plan"]}],
        "relation_selectors": ["produced_by"], "test_definition_bindings": [],
        "change_reason": "migrate breakdown fixture to canonical Unit4-P admission",
        "authority_refs": [],
    }
    _register_fixture_review(c)
    proposal = c.assurance.profile_propose(c.owner, project, program, body, None)
    _adopt(c, project, proposal, None)
    for task in tasks:
        plan = c.s.one("SELECT body FROM plans WHERE task=?", (task,), True)
        c.rt.review(c.owner, task, "test_plan", "markers", proposal=json.loads(plan["body"]))


def _review_current_task_plans(c, project, adapter, *, task_ids):
    """Observe the explicitly selected current Task plans for the node rule."""
    rows = c.s.all(
        "SELECT id FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id",
        (project,),
    )
    selected = set(task_ids)
    for row in rows:
        task = row["id"]
        if task not in selected:
            continue
        plan = c.s.one("SELECT body FROM plans WHERE task=?", (task,))
        if plan is None:
            continue
        # Re-observe the current immutable plan on every explicit review pass.
        # A prior receipt for the same Task may belong to an older plan
        # revision and cannot satisfy the current node binding.
        c.rt.review(c.owner, task, "test_plan", adapter, proposal=json.loads(plan["body"]))


def propose(s,units=None,previous=None,budget=24000):
    c,p,r,q,program,d,t,default=s
    return c.breakdowns.propose(c.owner,program,'Work plan','No requirement removed',default if units is None else units,previous,budget)


def packets(c,ident,limit=3):
    offset=0;result=[]
    while True:
        page=c.breakdowns.get(c.owner,ident,offset,limit);result.extend(page['packets'])
        if page['next_offset'] is None:return result
        offset=page['next_offset']


def review_all(c,ident,adapter='markers'):
    row = c.s.one("SELECT project FROM breakdowns WHERE id=?", (ident,), True)
    program = c.s.one("SELECT program FROM breakdowns WHERE id=?", (ident,), True)["program"]
    selection = c.assurance.selected_profile(c.owner, row["project"], program)
    requirements = [item["id"] for item in c.s.all(
        "SELECT id FROM artifacts WHERE project=? AND kind='requirement' "
        "AND status='accepted' ORDER BY id", (row["project"],)
    )]
    tasks = [item["id"] for item in c.s.all(
        "SELECT id FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id",
        (row["project"],),
    )]
    if selection.get("profile_ref") is None:
        _bootstrap_mandatory_profile(c, row["project"], program, requirements, tasks)
    _review_current_task_plans(c, row["project"], adapter, task_ids=tasks)
    for packet in packets(c,ident):
        for role in ('design','trace'):
            c.rt.review(c.owner,packet['id'],role,adapter)


def adopt(s):
    c=s[0];value=propose(s);review_all(c,value['id']);c.breakdowns.activate(c.owner,value['id'])
    return value


def test_split_has_explicit_scope_and_needs_two_observed_reviews(setup):
    c,p,r,q,program,d,t,units=setup;value=propose(setup)
    assert value['obligation_count']==1 and value['task_count']==1 and not value['semantic_reviewed']
    assert not c.breakdowns.audit(c.owner,value['id'])['current']
    with pytest.raises(Fault) as exc:c.breakdowns.activate(c.owner,value['id'])
    assert exc.value.code=='breakdown_gate_denied'
    review_all(c,value['id'])
    result=c.breakdowns.activate(c.owner,value['id'])
    assert result['assurance']=='validation' and not result['semantic_correctness_guaranteed']
    assert c.breakdowns.program_status(c.owner,program)['current']
    assert c.breakdowns.activate(c.owner,value['id'])['replayed']
    assert c.s.one('SELECT count(*) AS n FROM breakdown_adoptions')['n']==1


@pytest.mark.parametrize('fault', ['missing_ac','invented_ac','duplicate_ac','aggregate_ac','omitted_task','duplicate_task','wrong_domain','cycle','unknown_parent','aggregate_domain','missing_domain_read','empty_tasks','self_dependency'])
def test_malformed_breakdown_never_records_a_partial_plan(setup,fault):
    c,p,r,q,program,d,t,units=setup;units=copy.deepcopy(units)
    if fault=='missing_ac':units[1]['obligations']=[]
    if fault=='invented_ac':units[1]['obligations'][0]['acceptance']='AC-NOT-REQUESTED'
    if fault=='duplicate_ac':units[1]['obligations']*=2
    if fault=='aggregate_ac':units[0]['obligations']=units[1]['obligations']
    if fault=='omitted_task':make_work(c,p,r,q,d)
    if fault=='duplicate_task':units[1]['tasks']*=2
    if fault=='wrong_domain':units[1]['domain']=q
    if fault=='cycle':units[0]['parent']='arithmetic'
    if fault=='unknown_parent':units[1]['parent']='missing'
    if fault=='aggregate_domain':units[0]['domain']=d
    if fault=='missing_domain_read':
        task=c.w.task(c.owner,t);body=task['body'];body['read_artifacts']=[q]
        c.s.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(body).decode(),t))
    if fault=='empty_tasks':units[1]['tasks']=[]
    if fault=='self_dependency':units[1]['dependencies']=[{'unit':'arithmetic','interface':None}]
    with pytest.raises(Fault):propose(setup,units)
    assert c.s.one('SELECT count(*) AS n FROM breakdowns')['n']==0
    assert c.s.one('SELECT count(*) AS n FROM breakdown_packets')['n']==0


def test_parent_requirement_obligation_does_not_disappear_on_decomposition(setup):
    c,p,r,q,program,d,t,units=setup
    child=accepted(c,p,'requirement','Component condition',acceptance=['AC-CHILD'])
    c.k.link(c.owner,q,child,'decomposes','asserted','Parent still has an end-to-end acceptance condition')
    t2=make_work(c,p,r,child,d,acs=['AC-CHILD'])
    units=copy.deepcopy(units);units[1]['tasks'].append(t2)
    units[1]['obligations']=[{'requirement':child,'acceptance':'AC-CHILD'}]
    with pytest.raises(Fault) as exc:propose(setup,units)
    assert exc.value.code=='acceptance_scope_mismatch'
    units[1]['obligations'].append({'requirement':q,'acceptance':'AC-ADD'})
    assert propose(setup,units)['obligation_count']==2


def test_same_acceptance_label_in_different_requirements_is_not_deduplicated(setup):
    c,p,r,q,program,d,t,units=setup
    second=accepted(c,p,'requirement','Second outcome',acceptance=['AC-ADD'])
    task=make_work(c,p,r,second,d)
    units=copy.deepcopy(units);units[1]['tasks'].append(task)
    with pytest.raises(Fault):propose(setup,units)
    units[1]['obligations'].append({'requirement':second,'acceptance':'AC-ADD'})
    assert propose(setup,units)['obligation_count']==2


def test_analysis_task_is_not_implementation_coverage(setup):
    c,p,r,q,program,d,t,units=setup;c.w.cancel(c.owner,t,'Replacing with an experiment does not implement the feature')
    experiment=make_work(c,p,r,q,d,phase='feasibility')
    units=copy.deepcopy(units);units[1]['tasks']=[experiment]
    with pytest.raises(Fault) as exc:propose(setup,units)
    assert exc.value.code=='unmapped_acceptance'


def test_cross_project_inputs_cannot_be_allocated(setup):
    c,p,r,q,program,d,t,units=setup;other=c.k.create_project(c.owner,'Other')['id'];domain=make_domain(c,other)
    units=copy.deepcopy(units);units[1]['domain']=domain
    with pytest.raises(Fault) as exc:propose(setup,units)
    assert exc.value.code=='cross_project'
    b=propose(setup)
    with pytest.raises(Fault):c.breakdowns.get(Actor('different-project','agent',other),b['id'])


def test_missing_plan_or_unaccepted_input_is_rejected(setup):
    c,p,r,q,program,d,t,units=setup
    c.s.execute('DELETE FROM plans WHERE task=?',(t,))
    with pytest.raises(Fault) as exc:propose(setup)
    assert exc.value.code=='missing_test_plan'
    c.s.execute("UPDATE artifacts SET status='draft' WHERE id=?",(d,))
    with pytest.raises(Fault) as exc:propose(setup)
    assert exc.value.code=='unaccepted_input'


def two_domains(s,*,same_data=False,contract=True):
    c,p,r,q,program,d,t,units=s
    domain=make_domain(c,p,'Reporting',['ledger'] if same_data else [])
    if same_data:
        art=c.k.artifact(c.owner,d);body=art['body'];body['owned_data']=['ledger'];c.k._revise(c.owner,art,1,body,'fixture domain update','accepted')
        c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'Refresh fixture definition')
        c.w.plan_tests(c.owner,t,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest'}]})
    interface=None
    if contract:
        interface=accepted(c,p,'interface','Arithmetic output',input={},output={},authentication='same user',errors=[],idempotency='read only',compatibility='additive',consumers=[domain],verification=['component test'])
        old=c.w.task(c.owner,t);new_body=copy.deepcopy(old['body']);new_body['read_artifacts'].append(interface)
        new_body.pop('task_kind', None)
        proposal=c.task_revisions.propose(c.owner,t,old['revision'],new_body,'Bind the shared interface through the public revision path')
        review=c.rt.review(c.owner,proposal['id'],'impact','markers')
        c.task_revisions.apply(c.owner,proposal['id'],proposal['digest'],review['receipt'])
        c.w.plan_tests(c.owner,t,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest','required_tests':['test_add']}]})
    second=accepted(c,p,'requirement','Report the outcome',acceptance=['AC-REPORT'])
    task=make_work(c,p,r,second,domain,other_reads=[interface] if interface else [],deps=[t],acs=['AC-REPORT'])
    leaves=[leaf('producer',d,[t],q,interfaces=[interface] if interface else []),
            leaf('consumer',domain,[task],second,acs=['AC-REPORT'],interfaces=[interface] if interface else [],dependencies=[{'unit':'producer','interface':interface}])]
    return leaves,task,interface


def test_cross_domain_order_uses_real_task_edges_and_shared_contract(setup):
    c=setup[0];units,task,contract=two_domains(setup)
    result=propose(setup,units)
    assert result['unit_order']==['producer','consumer']
    review_all(c,result['id']);c.breakdowns.activate(c.owner,result['id'])
    assert c.w.task(c.owner,task)['body']['dependencies']==[setup[6]]


@pytest.mark.parametrize('missing',['contract','edge','context','provider_contract','invented_edge','data_owner'])
def test_boundary_errors_are_not_hidden_by_a_complete_requirement_count(setup,missing):
    c=setup[0];units,task,contract=two_domains(setup,contract=missing!='contract',same_data=missing=='data_owner')
    if missing=='edge':units[1]['dependencies']=[]
    if missing=='provider_contract':units[0]['interfaces']=[]
    if missing=='context':
        row=c.w.task(c.owner,task);row['body']['read_artifacts'].remove(contract)
        c.s.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(row['body']).decode(),task))
    if missing=='invented_edge':units[0]['dependencies']=[{'unit':'consumer','interface':contract}]
    with pytest.raises(Fault):propose(setup,units)


def test_split_induces_unit_cycle_even_when_task_graph_is_acyclic(setup):
    c,p,r,q,program,d,t,units=setup
    # A1 -> B -> A2 is a DAG of tasks, but grouping A1/A2 hides a cycle of units.
    b=make_work(c,p,r,q,d,deps=[t]);a2=make_work(c,p,r,q,d,deps=[b])
    units=[leaf('A',d,[t,a2],q,dependencies=[{'unit':'B','interface':None}]),
           leaf('B',d,[b],q,acs=[],dependencies=[{'unit':'A','interface':None}])]
    with pytest.raises(Fault) as exc:propose(setup,units)
    assert exc.value.code=='unit_cycle'


def test_deep_hierarchy_uses_iterative_graph_validation(setup):
    depth=1200;units=[parent('n'+str(i),'n'+str(i-1) if i else None) for i in range(depth)]
    units.append(leaf('work',setup[5],[setup[6]],setup[3],parent='n'+str(depth-1)))
    result=propose(setup,units,budget=100000)
    assert len(result['hierarchy_order'])==depth+1
    assert result['leaf_units']==['work']


@pytest.mark.parametrize('budget',[4096,8000,24000])
def test_packet_fragment_bytes_and_manifest_are_lossless(setup,budget):
    c,p,r,q,program,d,t,units=setup
    art=c.k.artifact(c.owner,d);body=art['body'];body['statement']='日本語😀と引用 "\\"\n'*1300
    c.k._revise(c.owner,art,1,body,'Long canonical domain input','accepted')
    c.w.replan(c.owner,t,1,'Rebind long domain specification')
    c.w.plan_tests(c.owner,t,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest'}]})
    result=propose(setup,budget=budget)
    ps=packets(c,result['id'],limit=1);assert all(x['bytes']<=budget for x in ps)
    groups={}
    for packet in ps:
        value=c.breakdowns.packet(c.owner,packet['id'])['body'];groups.setdefault(value['unit'],[]).append(value)
    for unit,items in groups.items():
        text=''.join(x['serialized_fragment'] for x in sorted(items,key=lambda i:i['start']))
        assert digest(text.encode())==items[0]['material_digest']
        value=json.loads(text)
        if unit=='arithmetic':assert next(a for a in value['artifacts'] if a['id']==d)['body']['statement']==body['statement']


@pytest.mark.parametrize('kind',['missing_role','empty_coverage','failed_review','missing_fragment','stale_domain','missing_receipt_bytes','new_task','cancelled_task','new_requirement','changed_test_plan'])
def test_activation_cannot_use_unexecuted_incomplete_or_stale_evidence(setup,kind):
    c,p,r,q,program,d,t,units=setup;value=propose(setup)
    if kind=='missing_role':
        for packet in packets(c,value['id']):c.rt.review(c.owner,packet['id'],'design','markers')
    elif kind=='empty_coverage':review_all(c,value['id'],'fixture')
    else:
        review_all(c,value['id'])
        if kind=='failed_review':
            script=c.s.home.parent/'fail_protocol.py';script.write_text("import json,sys\np=json.load(sys.stdin);print(json.dumps({'verdict':'fail','rationale':'fixture fail','covered':[],'findings':[],'observations':[{'ref':p['subject'],'detail':'failure fixture'}],'dispositions':[]}))\n")
            c.rt.adapters.register(c.owner,'failure','fixture',sys.executable,[str(script)])
            c.rt.review(c.owner,packets(c,value['id'])[0]['id'],'design','failure')
        if kind=='missing_fragment':c.s.execute('DELETE FROM breakdown_members WHERE breakdown=? AND ordinal=0',(value['id'],))
        if kind=='stale_domain':
            art=c.k.artifact(c.owner,d);c.k._revise(c.owner,art,1,{**art['body'],'statement':'Changed responsibility'},'domain change','accepted')
        if kind=='missing_receipt_bytes':
            ev=c.g.receipt(c.s.one('SELECT id FROM receipts ORDER BY created DESC LIMIT 1')['id']);h=ev['stdout_blob'];(c.s.blobs/h[:2]/h[2:]).unlink()
        if kind=='new_task':make_work(c,p,r,q,d)
        if kind=='cancelled_task':c.w.cancel(c.owner,t,'Cancellation must not erase obligations')
        if kind=='new_requirement':accepted(c,p,'requirement','Additional scope',acceptance=['AC-NEW'])
        if kind=='changed_test_plan':c.w.plan_tests(c.owner,t,{'checks':[{'id':'new-check','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest'}]})
    with pytest.raises(Fault) as exc:c.breakdowns.activate(c.owner,value['id'])
    assert exc.value.code=='breakdown_gate_denied'
    assert c.breakdowns.active(c.owner,program) is None


def test_current_packet_cannot_be_loaded_after_its_input_changes(setup):
    c,p,r,q,program,d,t,units=setup;b=propose(setup)
    packet=next(x for x in packets(c,b['id']) if x['unit']=='arithmetic')
    art=c.k.artifact(c.owner,d);c.k._revise(c.owner,art,1,{**art['body'],'statement':'Revised boundary'},'change','accepted')
    with pytest.raises(Fault) as exc:c.breakdowns.packet(c.owner,packet['id'])
    assert exc.value.code=='stale_breakdown_packet'


def test_normal_execution_does_not_stale_a_definition_plan(setup):
    c,p,r,q,program,d,t,units=setup;b=adopt(setup)
    ensure_current_root(c,p,t);c.w.ready(c.owner,t);finish_task(c,p,t)
    assert c.breakdowns.audit(c.owner,b['id'])['current']
    # Replanning is different from normal execution: definitions must be reviewed again.
    c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'New design inputs')
    assert not c.breakdowns.audit(c.owner,b['id'])['current']


def test_concurrent_proposals_cannot_overwrite_a_new_active_plan(setup):
    c=setup[0];a=propose(setup);b=propose(setup)
    review_all(c,a['id']);c.breakdowns.activate(c.owner,a['id'])
    with pytest.raises(Fault) as exc:c.breakdowns.activate(c.owner,b['id'])
    assert exc.value.code=='stale_breakdown'


def test_local_repartition_reuses_unchanged_packet_runs_and_keeps_work(setup):
    c,p,r,q,program,d,t,units=setup
    support_requirement=accepted(c,p,'requirement','Support outcome',acceptance=['AC-ADD'])
    task2=make_work(c,p,r,support_requirement,d,acs=('AC-ADD',))
    initial=[parent('system'),leaf('owned',d,[t],q,parent='system'),leaf('support',d,[task2],support_requirement,acs=('AC-ADD',),parent='system')]
    a=propose(setup,initial);review_all(c,a['id']);c.breakdowns.activate(c.owner,a['id'])
    before=packets(c,a['id']);unchanged={x['id'] for x in before if x['unit']=='owned'}
    altered=copy.deepcopy(initial);altered[2]['rationale']='New technical split rationale; requirement scope unchanged'
    b=propose(setup,altered,previous=a['id'])
    after=packets(c,b['id']);assert unchanged<={x['id'] for x in after}
    for packet in after:
        if packet['id'] in unchanged:continue
        for role in ('design','trace'):c.rt.review(c.owner,packet['id'],role,'markers')
    c.breakdowns.activate(c.owner,b['id'],expected_active=a['id'])
    assert c.breakdowns.get(c.owner,a['id'])['status']=='superseded'
    assert c.w.task(c.owner,t)['status']=='planned' and c.w.task(c.owner,task2)['status']=='planned'
    assert c.s.one('SELECT count(*) AS n FROM breakdown_adoptions')['n']==2
    for packet in unchanged:
        assert c.s.one('SELECT count(*) AS n FROM receipts WHERE subject=?',(packet,))['n']==2


def test_durable_job_can_really_invoke_a_packet_reviewer(setup):
    c=setup[0];b=propose(setup);packet=packets(c,b['id'])[0]
    job=c.jobs.submit(c.owner,'review',{'subject':packet['id'],'role':'design','adapter':'markers'})
    c.jobs.run_one(c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],),True))
    result=c.jobs.get(c.owner,job['id'])
    assert result['status']=='succeeded',result
    assert c.g.receipt(result['result']['receipt'])['result']['covered']


def test_new_breakdown_tables_are_migrated_without_rewriting_old_history(full,tmp_path):
    from daikibo.control import Control
    c=full;home=c.s.home;p=c.k.create_project(c.owner,'Preserve this')['id']
    c.close()
    db=sqlite3.connect(home/'state.sqlite3')
    db.execute('DROP TABLE program_origins')
    for trigger in ('breakdowns_no_rewrite','breakdown_packets_no_rewrite','program_closures_no_rewrite'):db.execute('DROP TRIGGER '+trigger)
    for table in ('supervisor_views','program_closures','breakdown_adoptions','breakdown_members','breakdown_packets','breakdowns'):db.execute('DROP TABLE '+table)
    db.execute('PRAGMA user_version=5');db.commit();db.close()
    with_context=Control(home,mode='validation',start_workers=False)
    try:
        from daikibo.db import SCHEMA_VERSION
        assert with_context.s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
        assert with_context.s.one('SELECT name FROM projects WHERE id=?',(p,))['name']=='Preserve this'
        assert (home/'pre-migration-v5.sqlite3').is_file()
    finally:with_context.close()


def test_plan_and_observed_reviews_survive_process_restart(setup):
    from daikibo.control import Control
    c=setup[0];b=adopt(setup);home=c.s.home;c.close()
    restored=Control(home,mode='validation',start_workers=False)
    try:
        assert restored.breakdowns.audit(Actor('user','owner'),b['id'])['current']
        assert restored.s.one('SELECT count(*) AS n FROM breakdown_adoptions')['n']==1
    finally:restored.close()


def test_phase_plan_requires_current_reviewed_breakdown(setup):
    c,p,r,q,program,d,t,units=setup
    row=c.s.one('SELECT * FROM programs WHERE id=?',(program,));row['phase']='plan'
    assert 'breakdown_incomplete' in c.p.phase_blockers(row)
    adopt(setup)
    assert 'breakdown_incomplete' not in c.p.phase_blockers(row)


def test_immutable_proposal_cannot_be_rewritten(setup):
    c=setup[0];b=propose(setup)
    with pytest.raises(sqlite3.IntegrityError):c.s.execute("UPDATE breakdowns SET body='{}' WHERE id=?",(b['id'],))


def test_unit_order_is_a_pure_reachability_property_not_a_semantic_verdict():
    assert topological({'a','b','c'},{'a':[],'b':['a'],'c':['b']})==['a','b','c']
    with pytest.raises(Fault):topological({'a','b'},{'a':['b'],'b':['a']})
