"""Regression checks for the narrowly retained audit-efficiency changes.

Fixtures prove controller contracts; they do not prove LLM judgment quality.
"""
import base64
import threading
from copy import deepcopy

import pytest

from daikibo.common import Actor, Fault, canonical, digest, parse_json, timestamp
from daikibo.rpc import Client, Server, MAX_FRAME
from conftest import route_change_to_product
from test_task_definition_revisions import revision_setup, propose, review


def draft(c, project):
    return c.k.propose(c.owner, project, 'finding', {'title':'Finding','statement':'Observed fact'})


def test_identical_draft_save_preserves_revision_but_intentional_revise_does_not(full, full_project):
    c=full; a=draft(c,full_project[0])
    saved=c.k.save(c.owner,a['id'],1,a['body'],'Save reviewed draft')
    replay=c.k.save(c.owner,a['id'],2,a['body'],'Save reviewed draft')
    assert saved['revision']==2 and replay['revision']==2 and replay['unchanged']
    assert c.k.revise(c.owner,a['id'],2,a['body'],'Save reviewed draft')['revision']==3
    assert c.k.save(c.owner,a['id'],3,a['body'],'New justification')['revision']==4
    with pytest.raises(Fault) as error:c.k.save(c.owner,a['id'],3,a['body'],'New justification')
    assert error.value.code=='stale_revision'
    c.k.accept(c.owner,a['id'],4)
    with pytest.raises(Fault) as error:c.k.save(c.owner,a['id'],4,a['body'],'New justification')
    assert error.value.code=='change_required'


def test_same_body_saved_by_another_actor_is_not_replayed(full, full_project):
    c=full;a=draft(c,full_project[0]);c.k.save(c.owner,a['id'],1,a['body'],'Save')
    result=c.k.save(Actor('another-agent','agent',full_project[0]),a['id'],2,a['body'],'Save')
    assert result['revision']==3


def test_reviewed_task_revision_survives_heartbeat(revision_setup):
    c,p,t=revision_setup
    c.w.claim(c.owner,p[0],t)
    proposal=propose(c,t);receipt=review(c,proposal)
    row=c.w.task(c.owner,t)
    c.w.heartbeat(c.owner,t,row['epoch'])
    result=c.task_revisions.apply(c.owner,proposal['id'],proposal['digest'],receipt['receipt'])
    assert result['revision']==2


@pytest.mark.parametrize('mutation',['expiry','owner','epoch','block','plan'])
def test_task_revision_still_rejects_real_changes(revision_setup, mutation):
    c,p,t=revision_setup;c.w.claim(c.owner,p[0],t);proposal=propose(c,t)
    if mutation=='expiry':c.s.execute('UPDATE tasks SET lease_until=? WHERE id=?',(timestamp()-1,t))
    elif mutation=='owner':c.s.execute('UPDATE tasks SET lease_owner=? WHERE id=?',('another',t))
    elif mutation=='epoch':c.s.execute('UPDATE tasks SET epoch=epoch+1 WHERE id=?',(t,))
    elif mutation=='block':c.s.execute('INSERT INTO blocks VALUES(?,?,?,?)',(t,'change','new','New ambiguity'))
    else:c.s.execute('UPDATE plans SET approved=? WHERE task=?',('new-observation',t))
    with pytest.raises(Fault) as error:c.task_revisions.current(c.owner,proposal['id'])
    assert error.value.code=='stale_task_proposal'


def test_dependency_heartbeat_does_not_change_revision_material(system, project, task):
    from daikibo.task_revisions import TaskRevisions
    s=system;s.w.claim(s.owner,project[0],task)
    body={k:v for k,v in s.w.task(s.owner,task)['body'].items() if k!='task_kind'}
    child=s.w.create(s.owner,project[0],{**body,'dependencies':[task],'write_paths':['child.py']})
    revisions=TaskRevisions(s.w)
    changed={k:v for k,v in child['body'].items() if k!='task_kind'}
    proposal=revisions.propose(s.owner,child['id'],1,{**changed,'title':'Reconsider child'},'Clarify')
    parent=s.w.task(s.owner,task);s.w.heartbeat(s.owner,task,parent['epoch'])
    assert revisions.current(s.owner,proposal['id'])['id']==proposal['id']


@pytest.mark.parametrize('bad',[
    {'method':'artifact.propose','params':{'project':'$project','kind':'finding','body':{'title':'B','statement':'B'}},'as':'first'},
    {'method':'artifact.propose','params':{'project':'$project','kind':'finding'},'as':'second'},
    {'method':'artifact.propose','params':{'project':'$project','kind':'finding','body':{'$ref':'future.body'}},'as':'second'},
    {'method':'artifact.propose','params':[]},
    ['invalid action'],
])
def test_batch_static_failure_cannot_write_earlier_actions(full, full_project, bad):
    c=full;p=full_project[0];c.native.attach(c.owner,'batch',str(full_project[3]),project=p)
    initial=c.s.one('SELECT count(*) n FROM artifacts')['n']
    good={'method':'artifact.propose','params':{'project':p,'kind':'finding','body':{'title':'A','statement':'A'}},'as':'first'}
    bad=deepcopy(bad)
    if isinstance(bad,dict) and isinstance(bad.get('params'),dict):bad['params']['project']=p
    result=c.native.actions(c.owner,'batch',[good,bad])
    assert not result['all_applied'] and result['executed']==0
    assert c.s.one('SELECT count(*) n FROM artifacts')['n']==initial


def test_batch_can_use_earlier_result(full, full_project):
    c=full;p=full_project[0];c.native.attach(c.owner,'batch',str(full_project[3]),project=p)
    result=c.native.actions(c.owner,'batch',[
        {'method':'artifact.propose','params':{'project':p,'kind':'finding','body':{'title':'A','statement':'A'}},'as':'a'},
        {'method':'artifact.get','params':{'artifact':{'$ref':'a.id'}}},
    ])
    assert result['all_applied'] and result['actions'][0]['result']['id']==result['actions'][1]['result']['id']


@pytest.mark.parametrize('valid',[True,False])
def test_batch_whole_result_reference_is_checked_when_resolved(full, full_project, valid):
    c=full;p=full_project[0];c.native.attach(c.owner,'whole-ref',str(full_project[3]),project=p)
    params={'project':p,'kind':'finding','body':{'title':'Second','statement':'Second'}} if valid else ['not an object']
    result=c.native.actions(c.owner,'whole-ref',[
        {'method':'artifact.propose','params':{'project':p,'kind':'finding','body':{'title':'First','statement':'First','next_params':params}},'as':'first'},
        {'method':'artifact.propose','params':{'$ref':'first.body.next_params'}},
    ])
    assert result['all_applied']==valid
    assert 'result' in result['actions'][0]
    if not valid: assert result['actions'][1]['error']['code']=='invalid_params'


def test_pause_replay_does_not_fence_but_explicit_fence_does(system, project, task):
    s=system;before=s.w.task(s.owner,task)
    assert s.w.pause(s.owner,project[0],task,paused=False)['unchanged']
    assert s.w.task(s.owner,task)==before
    s.w.pause(s.owner,project[0],task)
    epoch=s.w.task(s.owner,task)['epoch']
    s.w.pause(s.owner,project[0],task)
    assert s.w.task(s.owner,task)['epoch']==epoch
    s.w.pause(s.owner,project[0],task,fence=True)
    assert s.w.task(s.owner,task)['epoch']==epoch+1


def test_job_cancel_fences_even_already_paused_task(revision_setup):
    c,p,t=revision_setup;c.w.claim(c.owner,p[0],t)
    job=c.jobs.submit(c.owner,'execute',{'task':t,'adapter':'fixture'})['id']
    c.s.execute("UPDATE jobs SET status='running' WHERE id=?",(job,))
    c.w.pause(c.owner,p[0],t)
    before=c.w.task(c.owner,t)['epoch']
    c.jobs.cancel(c.owner,job,'Stop the managed execution')
    assert c.w.task(c.owner,t)['epoch']==before+1


def test_same_delta_preserves_answer_but_changed_reason_does_not(full, full_project):
    c=full;p,_,req,_=full_project
    src=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    change=c.p.change(c.owner,p,{'title':'Investigate','origin':'user','reason':'Check meaning','affected':[req],'evidence':[src],'source':src})
    c.p.set_delta(c.owner,change['id'],1,[],'Same proposal')
    assert route_change_to_product(c,change['id'])=='awaiting_product_decision'
    decision=c.p.propose_decision(c.owner,p,{'title':'Choose','reason':'Check meaning','options':['yes'],
        'recommendation':'yes','refs':[req],'requirement_affecting':True,'change':change['id'],
        'choice_effects':{'yes':'accept'}})
    c.p.respond(c.owner,decision['id'],decision['digest'],'yes','Yes')
    current_revision=c.p.change_get(c.owner,change['id'])['revision']
    replay=c.p.set_delta(c.owner,change['id'],current_revision,[],'Same proposal')
    assert replay['unchanged'] and c.decision_get(c.owner,decision['id'])['status']=='decision_received'
    c.p.set_delta(c.owner,change['id'],current_revision,[],'Different justification')
    assert c.decision_get(c.owner,decision['id'])['status']=='superseded'


def test_delivery_conversation_does_not_create_new_workflow(full, full_project):
    c=full;p=full_project[0]
    first=c.i.intake(c.owner,'Start work',project=p)['workflow']['program']
    c.s.execute("UPDATE programs SET phase='delivery' WHERE id=?",(first,))
    assert c.i.intake(c.owner,'Status please',project=p)['workflow']['id']==first
    assert c.s.one('SELECT count(*) n FROM programs WHERE project=?',(p,))['n']==1
    second=c.i.intake(c.owner,'Start separate work',project=p,start_program=True)['workflow']['program']
    assert second!=first


def test_native_turn_retry_cannot_change_program_start_intent(full, full_project):
    c=full;c.native.attach(c.owner,'start-intent',str(full_project[3]),project=full_project[0])
    c.native.input(c.owner,'start-intent','Start',turn_id='one',start_program=True)
    assert c.native.input(c.owner,'start-intent','Start',turn_id='one',start_program=True)['replayed']
    with pytest.raises(Fault) as error:c.native.input(c.owner,'start-intent','Start',turn_id='one')
    assert error.value.code=='idempotency_conflict'


def test_notification_ack_binds_exact_notice_body(full, full_project):
    c=full;p=full_project[0]
    c.g.inbox(p,'warning','exact-notice',{'message':'Original'},'warning')
    notice=c.s.one("SELECT * FROM inbox WHERE project=? AND ref='exact-notice'",(p,))
    src=c.k.source(c.owner,p,'Acknowledged')
    sha=digest(notice['body'].encode())
    c.g.inbox(p,'warning','exact-notice',{'message':'Changed'},'warning')
    with pytest.raises(Fault) as error:c.i.acknowledge(c.owner,notice['id'],'Acknowledged',source=src['id'],expected_digest=sha)
    assert error.value.code=='stale_notice'
    assert c.s.one('SELECT status FROM inbox WHERE id=?',(notice['id'],))['status']=='open'
    current=c.s.one('SELECT body FROM inbox WHERE id=?',(notice['id'],))
    current_digest=digest(current['body'].encode())
    with pytest.raises(Fault) as error:
        c.i.acknowledge(c.owner,notice['id'],'Acknowledged',source=src['id'],expected_digest=current_digest)
    assert error.value.code=='stale_user_input'
    fresh=c.k.source(c.owner,p,'Acknowledged after reading the changed notice')
    c.i.acknowledge(c.owner,notice['id'],'Acknowledged after reading the changed notice',
                    source=fresh['id'],expected_digest=current_digest)
    event=c.s.one("SELECT body FROM events WHERE kind='inbox_acknowledged' ORDER BY seq DESC LIMIT 1")
    assert parse_json(event['body'])['notice_digest']==digest(current['body'].encode())


def test_one_native_source_supports_distinct_answers_and_ack(full, full_project):
    c=full;p,_,req,root=full_project;c.native.attach(c.owner,'answer',str(root),project=p)
    decisions=[]
    for title in ['A','B']:
        d=c.p.propose_decision(c.owner,p,{'title':title,'reason':'Choose','options':['yes'],
            'recommendation':'yes','refs':[req],'requirement_affecting':True})
        c.native.present_decision(c.owner,'answer',d['id']);decisions.append(d)
    c.g.inbox(p,'warning','test',{'message':'Review'},'warning')
    item=c.s.one("SELECT id FROM inbox WHERE project=? AND ref='test'",(p,))['id']
    source=c.native.input(c.owner,'answer','A yes; B yes; notice acknowledged')['source']
    before=c.s.one('SELECT count(*) n FROM sources')['n']
    for d,quote in zip(decisions,['A yes','B yes']):
        c.native.respond(c.owner,'answer',d['id'],d['digest'],source,'yes',quote)
        assert c.decision_get(c.owner,d['id'])['source']==source
    c.native.acknowledge(c.owner,'answer',item,source,'notice acknowledged')
    assert c.s.one('SELECT count(*) n FROM sources')['n']==before
    ev=c.p.response_evidence(decisions[1]['id'])['body']
    assert ev['source']==source and ev['quote']=='B yes' and ev['start']==7
    original_binding=c.p.decision_binding(decisions[1]['id'])
    c.native.respond(c.owner,'answer',decisions[1]['id'],decisions[1]['digest'],source,'yes','yes')
    assert c.p.decision_binding(decisions[1]['id'])!=original_binding
    assert not c.s.one('SELECT id FROM dispositions WHERE source=?',(source,))


@pytest.mark.parametrize('bad_source',['agent','earlier','foreign','wrong_quote'])
def test_shared_answer_source_still_requires_human_exact_current_quote(full, full_project, bad_source):
    c=full;p,_,req,_=full_project
    old=c.k.source(c.owner,p,'Yes')
    d=c.p.propose_decision(c.owner,p,{'title':'Choose','reason':'Choose','options':['yes'],'recommendation':'yes','refs':[req],'requirement_affecting':True})
    if bad_source=='agent':src=c.k.source(Actor('agent','agent',p),p,'Yes')
    elif bad_source=='earlier':src=old
    elif bad_source=='foreign':src=c.k.source(c.owner,c.k.create_project(c.owner,'Other')['id'],'Yes')
    else:src=c.k.source(c.owner,p,'No')
    with pytest.raises(Fault):c.p.respond(c.owner,d['id'],d['digest'],'yes','Yes',source=src['id'])
    assert c.decision_get(c.owner,d['id'])['status']=='pending'


def test_filtered_job_pages_are_complete_and_reject_changed_catalog(full, full_project):
    c=full;p,r,_,_=full_project
    jobs=[c.jobs.submit(c.owner,'index',{'repo':r})['id'] for _ in range(3)]
    first=c.jobs.list(c.owner,p,limit=1,kind='index',subject=r,status='queued')
    second=c.jobs.list(c.owner,p,limit=2,kind='index',subject=r,status='queued',offset=1,expected_snapshot=first['snapshot'])
    assert {j['id'] for j in first['jobs']+second['jobs']}==set(jobs)
    assert first['total']==3 and second['next_offset'] is None
    c.jobs.cancel(c.owner,jobs[0],'No longer needed')
    with pytest.raises(Fault) as error:c.jobs.list(c.owner,p,limit=2,kind='index',subject=r,status='queued',offset=1,expected_snapshot=first['snapshot'])
    assert error.value.code=='stale_catalog'


def test_consumer_pages_reach_beyond_old_limit_and_pin_generation(full, full_project):
    c=full;p,r,_,root=full_project
    (root/'many.py').write_text('\n'.join('add(1,2)' for _ in range(1005)))
    c.idx.index(c.owner,r)
    first=c.idx.consumers(c.owner,p,'add',limit=1000)
    second=c.idx.consumers(c.owner,p,'add',limit=1000,offset=first['next_offset'],expected_snapshot=first['snapshot'])
    assert len(first['results'])==1000 and second['results'] and second['next_offset'] is None
    assert not set((r['path'],r['line']) for r in first['results']) & set((r['path'],r['line']) for r in second['results'])
    c.idx.index(c.owner,r)
    with pytest.raises(Fault) as error:c.idx.consumers(c.owner,p,'add',offset=1000,expected_snapshot=first['snapshot'])
    assert error.value.code=='stale_catalog'


def test_implementation_prompt_references_complete_stored_context(system, project, task):
    s=system;s.w.claim(s.owner,project[0],task)
    result=s.rt.execute(s.owner,task,'fixture')
    receipt=s.g.receipt(result['receipt']);prompt=parse_json(s.s.blob_get(receipt['input_blob']))
    view=prompt['context_package'];stored=s.s.one('SELECT body,digest FROM contexts WHERE id=?',(view['id'],))
    material=parse_json(stored['body']);assert stored['digest']==view['digest']==digest(material)
    assert 'task' not in view['package']['mandatory'] and 'artifacts' not in view['package']['mandatory']
    assert material['mandatory']['task']==prompt['task'] and material['mandatory']['test_plan']==prompt['test_plan']
    assert material['mandatory']['artifacts']==prompt['requirements']
    assert view['package']['mandatory_references']['test_plan']=='/test_plan'


def test_index_contention_waits_without_attempt_and_then_runs(full, full_project):
    c=full;job=c.jobs.submit(c.owner,'index',{'repo':full_project[1]})['id']
    c.idx.scan_lock.acquire()
    try:
        report=c.jobs.run_one({'id':job})
        assert report['waiting_for']=='index'
        assert c.jobs.get(c.owner,job)['status']=='queued' and c.jobs.get(c.owner,job)['attempt_count']==0
    finally:c.idx.scan_lock.release()
    assert c.jobs.run_one({'id':job})['status']=='succeeded'
    assert c.jobs.get(c.owner,job)['attempt_count']==1 and not c.idx.scan_lock.locked()


def test_preflight_is_readonly_and_failed_preflight_does_not_claim(revision_setup):
    c,p,t=revision_setup;before=c.w.task(c.owner,t)
    counts=[c.s.one('SELECT count(*) n FROM '+table)['n'] for table in ('events','contexts','runs','execution_attempts')]
    report=c.rt.preflight(c.owner,t,'missing-adapter')
    assert not report['ready'] and not report['authorizes_execution']
    assert report['failures'][0]['stage']=='adapter'
    assert [c.s.one('SELECT count(*) n FROM '+table)['n'] for table in ('events','contexts','runs','execution_attempts')]==counts
    with pytest.raises(Fault):c.w.claim(c.owner,p[0],t,adapter='missing-adapter')
    assert c.w.task(c.owner,t)==before
    assert c.rt.preflight(c.owner,t,'fixture')['ready']
    assert c.w.claim(c.owner,p[0],t,adapter='fixture')['status']=='running'


def test_preflight_keeps_context_capacity_and_reports_component_sizes(system, project, task):
    s=system;p=project[0]
    artifact=s.k.propose(s.owner,p,'finding',{'title':'Large required contract','statement':'あ'*70000})
    s.k.accept(s.owner,artifact['id'],1)
    body={k:v for k,v in s.w.task(s.owner,task)['body'].items() if k!='task_kind'}
    large=s.w.create(s.owner,p,{**body,'read_artifacts':body['read_artifacts']+[artifact['id']]})
    plan=parse_json(s.s.one('SELECT body FROM plans WHERE task=?',(task,))['body'])
    s.w.plan_tests(s.owner,large['id'],plan)
    before=s.w.task(s.owner,large['id'])
    report=s.rt.preflight(s.owner,large['id'],'fixture')
    failure=next(f for f in report['failures'] if f['stage']=='context')
    assert failure['code']=='context_insufficient' and failure['details']['components']['artifacts']>200000
    assert s.w.task(s.owner,large['id'])==before


def test_completion_keeps_release_gate_details(full, full_project, monkeypatch):
    c=full;p=full_project[0];c.native.attach(c.owner,'complete',str(full_project[3]),project=p)
    c.s.execute('INSERT INTO deliveries(id,project,status,body,digest,created) VALUES(?,?,?,?,?,?)',('D-test',p,'verified','{}','binding',timestamp()))
    def denied(*args,**kwargs):raise Fault('release_gate_denied','Not current',[{'code':'missing_review','role':'integration'}])
    monkeypatch.setattr(c.d,'certify',denied)
    result=c.native.completion(c.owner,'complete','D-test')
    assert not result['completed'] and result['blockers']==['delivery_recheck:release_gate_denied']
    assert result['blocker_details'][0]['details'][0]['role']=='integration'


def test_large_rpc_result_replays_once_and_is_readable(full, tmp_path):
    c=full;calls=[];payload={'content':'あ'*(MAX_FRAME//3+100)}
    def large(actor):calls.append(True);return payload
    c.register('test.large',large)
    request={'id':'large-once','method':'test.large','params':{}}
    reference=c.request(None,request)
    assert reference['format']=='daikibo.rpc-result-reference.v1'
    assert c.request(None,request)==reference and len(calls)==1
    with pytest.raises(Fault):c.request_result(Actor('other','owner'),'large-once',reference['sha256'])
    with pytest.raises(Fault):c.request_result(c.owner,'large-once','0'*64)
    path=tmp_path/'rpc.sock';server=Server(c,path);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        assert Client(path).call('test.large',request_id='large-once')==payload
        assert len(calls)==1
        page=Client(path).call('request.result',{'request_id':'large-once','expected_digest':reference['sha256'],'limit':5})
        assert base64.b64decode(page['base64'])==canonical(payload)[:5]
    finally:server.shutdown();server.server_close();thread.join()


def test_claim_scans_beyond_first_hundred_and_distinguishes_unsearched(system, project, task):
    s=system;p=project[0];s.w.claim(s.owner,p,task)
    body={k:v for k,v in s.w.task(s.owner,task)['body'].items() if k!='task_kind'}
    plan=parse_json(s.s.one('SELECT body FROM plans WHERE task=?',(task,))['body'])
    ids=[]
    for i in range(101):
        row=s.w.create(s.owner,p,{**body,'title':f'Work {i}','write_paths':['free.py'] if i==100 else ['calc.py']})
        s.w.plan_tests(s.owner,row['id'],plan);s.w.ready(s.owner,row['id']);ids.append(row['id'])
    with pytest.raises(Fault) as error:s.w.claim(s.owner,p,scan_limit=100)
    assert error.value.code=='search_incomplete' and error.value.details['next_after_task']==ids[99]
    claimed=s.w.claim(s.owner,p,after_task=ids[99])
    assert claimed['id']==ids[-1]
