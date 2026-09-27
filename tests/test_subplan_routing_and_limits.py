import copy
import sys
import tempfile
import threading
from pathlib import Path
import pytest
from daikibo.common import Actor, Fault, canonical, digest, parse_json
from daikibo.rpc import Server, Client
from test_reviewed_breakdowns import setup, accepted, make_domain, make_work, leaf, review_all
from test_subplan_drafts import (
    draft, prepare_draft_admission, review_draft, all_packets, local, second,
)
from conftest import ensure_current_root


def test_managed_review_job_routes_to_the_draft_packet(setup):
    c=setup[0];v=draft(setup)
    for packet in all_packets(c,v['id']):
        for role in ('design','trace'):
            args={'subject':packet['id'],'role':role,'adapter':'markers'}
            assert c.jobs.subject_project('review',args)==setup[1]
            job=c.jobs.submit(c.owner,'review',args)
            result=c.jobs.run_one(c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],)))
            assert c.jobs.get(c.owner,job['id'])['status']=='succeeded'
            assert result['result']['result']['verdict']=='pass'
    assert c.subplans.compose(c.owner,v['id'])['breakdown']


def test_native_conversation_uses_the_same_workflow_operations(setup):
    c,p,r,q,program,d,t,units=setup
    workspace=c.s.one('SELECT path FROM repos WHERE id=?',(r,))['path']
    c.native.attach(c.owner,'partial-session',workspace,project=p)
    params={'program':program,'title':'Native partial design','rationale':'Preserve scope','obligations':[{'requirement':q,'acceptance':'AC-ADD'}],
            'units':local(units)}
    reply=c.native.actions(c.owner,'partial-session',[{'method':'subplan.propose','params':params}])
    assert reply['all_applied'];v=reply['actions'][0]['result']
    prepare_draft_admission(c,v['id'],requirements=[q],task_ids=[t])
    review_draft(c,v['id'])
    reply=c.native.actions(c.owner,'partial-session',[{'method':'subplan.compose','params':{'subplan':v['id']}}])
    assert reply['all_applied'] and not reply['actions'][0]['result']['root_adopted']


def test_real_unix_rpc_and_deduplication(setup):
    c,p,r,q,program,d,t,units=setup
    with tempfile.TemporaryDirectory(prefix='partial-rpc-') as tmp:
        server=Server(c,Path(tmp)/'rpc');th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
        try:
            client=Client(Path(tmp)/'rpc')
            args={'program':program,'title':'RPC draft','rationale':'No omitted obligations','obligations':[{'requirement':q,'acceptance':'AC-ADD'}], 'units':local(units),'byte_budget':4096}
            v=client.call('subplan.propose',args,request_id='partial-1')
            assert client.call('subplan.propose',args,request_id='partial-1')==v
            packets=all_packets(c,v['id']);assert len(packets)>1
            assert len(canonical(client.call('subplan.packet',{'packet':packets[0]['id']})['body']))<=4096
            prepare_draft_admission(c,v['id'],requirements=[q],task_ids=[t])
            review_draft(c,v['id'])
            result=client.call('subplan.compose',{'subplan':v['id']},request_id='compose-1')
            assert client.call('subplan.compose',{'subplan':v['id']},request_id='compose-1')==result
            assert c.s.one('SELECT count(*) AS n FROM subplans')['n']==1
        finally:server.shutdown();server.server_close();th.join(timeout=5)


def test_failed_newer_review_cannot_use_old_pass(setup,tmp_path):
    c=setup[0];v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,v['id'])
    file=tmp_path/'fail-review.py';file.write_text("""import sys,json
p=json.load(sys.stdin)
print(json.dumps({'verdict':'fail','rationale':'TEST FIXTURE: explicitly reject', 'covered':p['context']['required_coverage'], 'findings':[{'severity':'high','statement':'A missing boundary','evidence':'fixture counterexample'}], 'observations':[{'ref':p['subject'],'detail':'fixture observation'}], 'dispositions':[]}))
""")
    c.rt.adapters.register(c.owner,'fail-partial','fixture',sys.executable,[str(file)])
    packet=all_packets(c,v['id'])[0]['id'];c.rt.review(c.owner,packet,'design','fail-partial')
    assert not c.subplans.audit(c.owner,v['id'])['current']
    with pytest.raises(Fault):c.subplans.compose(c.owner,v['id'])


def test_coverage_markers_are_required_even_for_a_pass(setup,tmp_path):
    c=setup[0];v=draft(setup)
    for item in all_packets(c,v['id']):
        for role in ('design','trace'):c.rt.review(c.owner,item['id'],role,'fixture')
    assert not c.subplans.audit(c.owner,v['id'])['current']


def test_missing_observed_run_cannot_be_replaced_by_plan_pass(setup):
    c=setup[0];v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,v['id']);pid=all_packets(c,v['id'])[0]['id']
    receipt=c.s.one("SELECT body FROM receipts WHERE subject=? AND role='design'",(pid,))
    body=parse_json(receipt['body']);c.s.blob_path(body['stdout_blob']).unlink()
    with pytest.raises(Fault):c.subplans.compose(c.owner,v['id'])


def test_cancelled_task_cannot_retain_partial_pass(setup):
    c=setup[0];v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,v['id'])
    c.s.execute("UPDATE tasks SET status='cancelled' WHERE id=?",(setup[6],))
    assert not c.subplans.audit(c.owner,v['id'])['current']
    with pytest.raises(Fault):c.subplans.compose(c.owner,v['id'])


def test_stale_composed_root_is_not_replayed(setup):
    c,p,r,q,program,d,t,units=setup;v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[q],task_ids=[t])
    review_draft(c,v['id']);result=c.subplans.compose(c.owner,v['id'])
    accepted(c,p,'requirement','More work',acceptance=['ADDED'])
    with pytest.raises(Fault) as e:c.subplans.compose(c.owner,v['id'])
    assert e.value.code=='stale_composition'
    assert c.s.one('SELECT count(*) AS n FROM subplan_compositions')['n']==1


def test_conflicting_root_adoption_stops_composition(setup):
    c,p,r,q,program,d,t,units=setup;v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[q],task_ids=[t])
    review_draft(c,v['id'])
    other=c.breakdowns.propose(c.owner,program,'Other plan','Independent root',units)
    review_all(c,other['id']);c.breakdowns.activate(c.owner,other['id'])
    with pytest.raises(Fault) as e:c.subplans.compose(c.owner,v['id'])
    assert e.value.code=='stale_breakdown'
    assert c.subplans.compose(c.owner,v['id'],expected_active=other['id'])['breakdown']


@pytest.mark.parametrize('bad',[-1,0,4095,100001,True,'24000'])
def test_invalid_packet_budget_rejected_without_writes(setup,bad):
    c=setup[0]
    with pytest.raises(Fault):draft(setup,budget=bad)
    assert c.s.one('SELECT count(*) AS n FROM subplans')['n']==0


def test_multibyte_material_is_complete_under_byte_budget(setup):
    c,p,r,q,program,d,t,units=setup
    ref=c.k.propose(c.owner,p,'design',{'title':'日本語資料','statement':'境界・要件・例外を省かない。'*1800})['id']
    v=draft(setup,context=[ref],budget=4096)
    rows=[c.subplans.packet(c.owner,p['id'])['body'] for p in all_packets(c,v['id'])]
    assert len(rows)>10 and all(len(canonical(p))<=4096 for p in rows)
    raw=''.join(p['serialized_fragment'] for p in rows)
    content=parse_json(raw,limit=128*1024*1024)
    assert next(a['body']['statement'] for a in content['artifacts'] if a['id']==ref)=='境界・要件・例外を省かない。'*1800
    audit=c.subplans.audit(c.owner,v['id'],limit=2)
    assert not audit['current'] and audit['failure_count']>len(audit['failures']) and audit['next_failure_offset']==2


def test_supervisor_progress_is_observed_and_reads_are_bounded(setup):
    from daikibo.supervisor import ALLOWED,VIEW_METHODS
    c=setup[0];before=c.supervisor.state_digest(setup[1]);v=draft(setup)
    assert c.supervisor.state_digest(setup[1])!=before
    assert {'subplan.propose','subplan.compose'}<=ALLOWED
    assert {'subplan.get','subplan.packet','subplan.audit'}<=VIEW_METHODS
    first=c.subplans.list(c.owner,setup[4],limit=1)
    assert first['total']==1 and first['next_offset'] is None
    state=c.supervisor.state_digest(setup[1]);c.subplans.get(c.owner,v['id']);assert c.supervisor.state_digest(setup[1])==state


def test_cross_program_and_scoped_actor_rejected(setup):
    c,p,r,q,program,d,t,units=setup;v=draft(setup)
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id'];other=c.p.begin(c.owner,p,source)['program']
    with pytest.raises(Fault):c.subplans.propose(c.owner,other,'Other','Cross flow', [{'requirement':q,'acceptance':'AC-ADD'}],[],[v['id']])
    foreign=c.k.create_project(c.owner,'foreign')['id']
    with pytest.raises(Fault):c.subplans.get(Actor('elsewhere','agent',foreign),v['id'])
    with pytest.raises(Fault):c.subplans.compose(Actor('read','observer',p),v['id'])


def interface(c,p,name):
    return accepted(c,p,'interface',name,input={},output={},authentication='none',errors=[],idempotency='caller key',compatibility='versioned',consumers=[],verification=[])


@pytest.mark.parametrize('contract_count',[0,1,2])
def test_cross_child_contract_edges_use_exact_real_task_dependencies(setup,contract_count):
    c,p,r,q,program,d,t,units=setup
    q2=accepted(c,p,'requirement','Other domain',acceptance=['OTHER']);dom=make_domain(c,p,'Other',data=['other-data'])
    contracts=[interface(c,p,'Contract '+str(i)) for i in range(contract_count)]
    # Create two new canonical tasks with explicit contract reads. Remove initial
    # fixture task via cancellation to keep current full scope exact.
    c.s.execute("UPDATE tasks SET status='cancelled' WHERE id=?",(t,))
    a=make_work(c,p,r,q,d,other_reads=contracts)
    b=make_work(c,p,r,q2,dom,other_reads=contracts,deps=[a],acs=['OTHER'])
    left=draft(setup,units=local([leaf('a',d,[a],q,interfaces=contracts)]))
    right=draft(setup,units=local([leaf('b',dom,[b],q2,acs=['OTHER'],interfaces=contracts)]),obligations=[{'requirement':q2,'acceptance':'OTHER'}])
    args=dict(program=program,title='Combined boundary',rationale='One cross-domain edge',obligations=[{'requirement':q,'acceptance':'AC-ADD'},{'requirement':q2,'acceptance':'OTHER'}],units=[],children=[left['id'],right['id']])
    if contract_count!=1:
        with pytest.raises(Fault):c.subplans.propose(c.owner,**args)
        if contract_count==0:return
        args['boundary_contracts']=[{'task':b,'dependency':a,'interface':contracts[1]}]
    whole=c.subplans.propose(c.owner,**args)
    prepare_draft_admission(c,whole['id'],requirements=[q,q2],task_ids=[a,b],recursive=True)
    review_draft(c,whole['id'],True)
    result=c.subplans.compose(c.owner,whole['id'])
    assert c.breakdowns.audit(c.owner,result['breakdown'],reviews=False)['current']


def test_reviewed_partial_task_executes_only_with_normal_completion_gates(setup):
    from conftest import finish_task
    c,p,r,q,program,d,t,units=setup;v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[q],task_ids=[t])
    review_draft(c,v['id']);res=c.subplans.compose(c.owner,v['id'])
    review_all(c,res['breakdown']);c.breakdowns.activate(c.owner,res['breakdown'])
    with pytest.raises(Fault):c.w.complete(c.owner,t,c.w.task(c.owner,t)['revision'])
    ensure_current_root(c,p,t)
    c.w.ready(c.owner,t)
    assert finish_task(c,p,t)['status']=='completed'
    assert c.subplans.audit(c.owner,v['id'])['current']
    assert not c.lifecycle.completion(c.owner,program)['completed']


def test_root_cannot_bypass_changed_partial_trace_relations(setup):
    c,p,r,q,program,d,t,units=setup
    design=accepted(c,p,'design','Approach for addition')
    c.k.link(c.owner,design,q,'realizes','asserted','Initial confirmed mapping')
    v=draft(setup,context=[design])
    prepare_draft_admission(c,v['id'],requirements=[q],task_ids=[t])
    review_draft(c,v['id']);root=c.subplans.compose(c.owner,v['id'])['breakdown']
    review_all(c,root);c.breakdowns.activate(c.owner,root)
    assert c.breakdowns.audit(c.owner,root)['current']
    c.k.link(c.owner,design,q,'realizes','inferred','Mapping now uncertain')
    assert not c.subplans.audit(c.owner,v['id'])['current']
    assert not c.breakdowns.audit(c.owner,root)['current']
    with pytest.raises(Fault):c.breakdowns.activate(c.owner,root)


def test_root_cannot_bypass_later_failed_child_review(setup,tmp_path):
    c=setup[0];v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,v['id']);root=c.subplans.compose(c.owner,v['id'])['breakdown']
    review_all(c,root);c.breakdowns.activate(c.owner,root)
    f=tmp_path/'blocked.py';f.write_text("""import json,sys
p=json.load(sys.stdin)
print(json.dumps({'verdict':'blocked','rationale':'Fixture: new uncertainty', 'covered':[], 'findings':[], 'observations':[{'ref':p['subject'],'detail':'Need evidence'}], 'dispositions':[]}))
""")
    c.rt.adapters.register(c.owner,'blocked-child','fixture',sys.executable,[str(f)])
    c.rt.review(c.owner,all_packets(c,v['id'])[0]['id'],'trace','blocked-child')
    assert not c.breakdowns.audit(c.owner,root)['current']


def test_unrelated_trace_relation_does_not_stale_child(setup):
    c,p,r,q,program,d,t,units=setup;v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[q],task_ids=[t])
    review_draft(c,v['id'])
    one=accepted(c,p,'design','Unrelated one');two=accepted(c,p,'component','Unrelated two')
    c.k.link(c.owner,one,two,'realizes','asserted','Independent internal detail')
    assert c.subplans.audit(c.owner,v['id'])['current']


def test_composed_root_remembers_a_missing_provenance_record(setup):
    c=setup[0];v=draft(setup)
    prepare_draft_admission(c,v['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,v['id']);root=c.subplans.compose(c.owner,v['id'])['breakdown']
    review_all(c,root);c.breakdowns.activate(c.owner,root)
    # Deliberate storage-corruption fixture, not a normal application operation.
    c.s.execute('DROP TRIGGER subplan_compositions_no_delete');c.s.execute('DELETE FROM subplan_compositions')
    result=c.breakdowns.audit(c.owner,root)
    assert not result['current'] and any(f['code']=='composition_history_missing' for f in result['failures'])
    with pytest.raises(Fault):c.breakdowns.activate(c.owner,root)


@pytest.mark.parametrize('case',['empty','reserved','duplicate_local','bad_parent','wrong_parent_type','bad_domain_type','missing_scope','cycle','unknown_context'])
def test_invalid_partial_structure_cannot_create_history(setup,case):
    c=setup[0];units=local(setup[7]);args={'units':units}
    if case=='empty':args['units']=[]
    elif case=='reserved':units[0]['id']='__group__'
    elif case=='duplicate_local':units.append(copy.deepcopy(units[-1]))
    elif case=='bad_parent':units[-1]['parent']='not_present'
    elif case=='wrong_parent_type':units[-1]['parent']=[]
    elif case=='bad_domain_type':units[-1]['domain']={}
    elif case=='missing_scope':args['obligations']=[]
    elif case=='cycle':units[0]['parent']=units[-1]['id']
    elif case=='unknown_context':args['context']=['MISSING']
    with pytest.raises(Fault):draft(setup,**args)
    assert c.s.one('SELECT count(*) AS n FROM subplans')['n']==0
    assert c.s.one('SELECT count(*) AS n FROM subplan_packets')['n']==0


def test_deep_composition_has_explicit_limit_and_atomic_rejection(setup,monkeypatch):
    import daikibo.subplans as impl
    monkeypatch.setattr(impl,'MAX_DEPTH',2)
    c=setup[0];a=draft(setup);b=draft(setup,units=[],children=[a['id']]);d=draft(setup,units=[],children=[b['id']])
    before=c.s.one('SELECT count(*) AS n FROM subplan_packets')['n']
    with pytest.raises(Fault) as e:draft(setup,units=[],children=[d['id']])
    assert e.value.code=='subplan_capacity'
    assert c.s.one('SELECT count(*) AS n FROM subplans')['n']==3
    assert c.s.one('SELECT count(*) AS n FROM subplan_packets')['n']==before


def test_combining_child_designs_checks_unique_data_ownership(setup):
    c,p,r,q,program,d,t,units=setup
    domains=[make_domain(c,p,'First owner',['same-table']),make_domain(c,p,'Second owner',['same-table'])]
    q2,t2,ac=second(setup)
    c.s.execute("UPDATE tasks SET status='cancelled' WHERE id IN (?,?)",(t,t2))
    tasks=[make_work(c,p,r,q,domains[0]),make_work(c,p,r,q2,domains[1],acs=[ac])]
    a=draft(setup,units=local([leaf('one',domains[0],[tasks[0]],q)]))
    b=draft(setup,units=local([leaf('two',domains[1],[tasks[1]],q2,acs=[ac])]),obligations=[{'requirement':q2,'acceptance':ac}])
    with pytest.raises(Fault) as e:draft(setup,units=[],children=[a['id'],b['id']],obligations=[{'requirement':q,'acceptance':'AC-ADD'},{'requirement':q2,'acceptance':ac}])
    assert e.value.code=='duplicate_data_owner'
