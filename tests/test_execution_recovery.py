"""Protocol fixtures and real local processes. No live provider/LLM is used."""
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path

import pytest

from daikibo.common import Actor, Fault, canonical, digest, parse_json, timestamp
from daikibo.control import Control
from daikibo.execution_errors import classify_error, decode, observe_failure
from daikibo.execution_ledger import usage_values
from conftest import make_task


def review_result(verdict='pass'):
    return {'verdict':verdict,'rationale':'Protocol fixture checks a specified file',
            'covered':['AC-ADD'],'findings':[],
            'observations':[{'ref':'calc.py','detail':'This is a fixture observation, not LLM reasoning.'}],
            'dispositions':[]}


def ok(result=None, usage=None, cost=None):
    value={'type':'result','subtype':'success','is_error':False,
           'structured_output':result if result is not None else review_result()}
    if usage is not None:value['usage']=usage
    if cost is not None:value['total_cost_usd']=cost
    return value


def limited(retry_after=None, **extra):
    value={'type':'result','subtype':'success','is_error':True,'api_error_status':429,'result':'Rate limit exceeded',**extra}
    if retry_after is not None:value['retry_after_seconds']=retry_after
    return value


def fake_cli(c, tmp_path, responses, *, writes=None, delay=0, name='fake-cli'):
    """Invoked with Claude print flags, but explicitly a generated local test script."""
    path=tmp_path/name;counter=tmp_path/(name+'.count')
    source='#!'+sys.executable+'\n'+'''import sys,json,time
from pathlib import Path
if '--version' in sys.argv:
    print('daikibo protocol fixture, not Claude');raise SystemExit
prompt=sys.stdin.buffer.read()
'''
    source+=f'counter=Path({str(counter)!r})\nn=int(counter.read_text()) if counter.exists() else 0\ncounter.write_text(str(n+1))\n'
    source+=f'time.sleep({delay!r})\n'
    if writes:
        for file, content in writes.items():source+=f'Path({file!r}).write_text({content!r})\n'
    source+=f'responses={responses!r}\nvalue=responses[min(n,len(responses)-1)]\nprint(json.dumps(value))\n'
    source+='raise SystemExit(1 if value.get("is_error") else 0)\n'
    path.write_text(source);path.chmod(0o755)
    c.rt.adapters.register(c.owner,name,'claude',str(path))
    return name,counter


def review_job(c,project,adapter,**retry):
    return c.jobs.submit(c.owner,'review',{'subject':project[2],'role':'requirements','adapter':adapter},
                         retry={'base_delay_seconds':.01,'max_delay_seconds':10,'max_elapsed_seconds':60,**retry})


def dispatch(c,job):
    row=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],),True)
    return c.jobs.run_one(row)


def due_now(c,job):
    c.s.execute('UPDATE jobs SET retry_due=? WHERE id=?',(timestamp()-1,job['id']))


@pytest.mark.parametrize('payload,expected,retryable',[
    ({'api_error_status':429},'rate_limit',True),
    ({'error':{'code':'insufficient_quota'},'api_error_status':429},'quota_exhausted',False),
    ({'subtype':'error_max_budget_usd'},'budget_exhausted',False),
    ({'error':{'type':'authentication_failed'}},'authentication_required',False),
    ({'status_code':403},'authentication_required',False),
    ({'status_code':503},'server_error',True),
    ({'error':{'code':'context_length_exceeded'}},'context_limit',False),
    ({'message':'stream disconnected before completion'},'connection_lost',True),
    ({'code':'model_not_found'},'configuration_error',False),
    ({'subtype':'never_seen_before'},'unknown_agent_failure',False),
])
def test_error_taxonomy(payload,expected,retryable):
    observed=classify_error(payload)
    assert observed['code']==expected and observed['retryable'] is retryable
    assert observed['technical_impossibility'] is False


def test_claude_success_subtype_with_error_is_still_failure_and_retains_usage():
    result,metadata,failed=decode('claude',canonical(limited(usage={'input_tokens':7,'output_tokens':3},total_cost_usd=.002)))
    assert failed['code']=='rate_limit'
    assert result['verdict']=='blocked' and metadata['usage']['input_tokens']==7
    assert metadata['total_cost_usd']==.002


def test_codex_recovered_error_not_mistaken_for_failed_final():
    events=[{'type':'thread.started','thread_id':'test'}, {'type':'error','message':'stream disconnected'},
            {'type':'item.completed','item':{'type':'agent_message','text':json.dumps(review_result())}},
            {'type':'turn.completed','usage':{'input_tokens':8,'cached_input_tokens':7,'output_tokens':2}}]
    result,metadata,failed=decode('codex',b'\n'.join(canonical(e) for e in events))
    assert failed is None and result['verdict']=='pass'
    assert metadata['recovered_error_events']==1
    assert usage_values('codex',metadata)==(10,None)


def test_codex_failure_and_missing_terminal_never_use_success_file(tmp_path):
    final=tmp_path/'last.json';final.write_bytes(canonical(review_result()))
    for events,expected in [([{'type':'turn.failed','error':{'code':'rate_limit_exceeded'}}],'rate_limit'),
                            ([{'type':'thread.started','thread_id':'test'}],'connection_lost')]:
        _,_,failed=decode('codex',b'\n'.join(canonical(e) for e in events),final)
        assert failed['code']==expected


@pytest.mark.parametrize('events',[
    [[],{'type':'turn.completed'}],
    [{'type':'turn.completed'},{'type':'turn.started'}],
    [{'type':'turn.completed'},{'type':'turn.completed'}],
    [{'type':'item.completed','item':{'type':'agent_message','text':'{}'}} ,{'type':'turn.completed'},{'type':'error','message':'late failure'}],
])
def test_malformed_codex_stream_rejected(events):
    with pytest.raises(Fault):decode('codex',b'\n'.join(canonical(e) for e in events))


def test_user_message_about_rate_limit_is_not_classified_as_transport_failure():
    result,_,failed=decode('claude',canonical(ok({'message':'Document HTTP 429 and rate limit handling'})))
    assert failed is None and '429' in result['message']
    assert not observe_failure(exit_code=1,stderr=b'HTTP 429',decoded=None)['retryable']


def test_retry_is_durable_and_success_keeps_failed_attempt_and_receipt(full,full_project,tmp_path,monkeypatch):
    c=full;adapter,counter=fake_cli(c,tmp_path,[limited(),ok()]);job=review_job(c,full_project,adapter)
    first=dispatch(c,job)
    assert first['status']=='retry_wait'
    # Observe the before-due branch explicitly; scheduling two Python processes
    # under load can otherwise take longer than this fixture's short retry delay.
    with monkeypatch.context() as clock:
        clock.setattr('daikibo.jobs.timestamp',lambda:first['retry_due']-1)
        assert dispatch(c,job)['status']=='retry_wait' and counter.read_text()=='1'
    due_now(c,job);assert dispatch(c,job)['status']=='succeeded'
    state=c.jobs.get(c.owner,job['id'])
    assert state['attempt_count']==2 and [a['status'] for a in state['attempts']]==['failed','succeeded']
    receipts=c.s.all('SELECT body FROM receipts WHERE subject=? ORDER BY created',(full_project[2],))
    assert len(receipts)==2
    assert parse_json(receipts[0]['body'])['failure']['code']=='rate_limit'
    assert parse_json(receipts[1]['body'])['failure'] is None
    assert not c.s.one("SELECT task FROM blocks WHERE kind='review_failed'")


def test_retry_wait_survives_controller_restart(full,full_project,tmp_path):
    c=full;adapter,counter=fake_cli(c,tmp_path,[limited(),ok()]);job=review_job(c,full_project,adapter)
    assert dispatch(c,job)['status']=='retry_wait';home=c.s.home;c.close()
    reopened=Control(home,mode='validation',start_workers=False)
    reopened.owner=Actor('local-user','owner')
    try:
        assert reopened.jobs.get(reopened.owner,job['id'])['status']=='retry_wait'
        due_now(reopened,job);assert dispatch(reopened,job)['status']=='succeeded'
        assert counter.read_text()=='2'
    finally:reopened.close()


def test_cancellation_during_retry_wait_prevents_second_process(full,full_project,tmp_path):
    c=full;adapter,counter=fake_cli(c,tmp_path,[limited(),ok()]);job=review_job(c,full_project,adapter)
    dispatch(c,job);stale=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],))
    c.jobs.cancel(c.owner,job['id'],'No more attempts')
    assert c.jobs.run_one(stale)['status']=='cancelled' and counter.read_text()=='1'


def test_changed_requirement_invalidates_scheduled_retry(full,full_project,tmp_path):
    c=full
    draft=c.k.propose(c.owner,full_project[0],'design',{'title':'Pending design','statement':'Initial design'})
    project=(full_project[0],full_project[1],draft['id'],full_project[3])
    adapter,counter=fake_cli(c,tmp_path,[limited(),ok()]);job=review_job(c,project,adapter)
    dispatch(c,job)
    artifact=c.k.artifact(c.owner,draft['id']);body={**artifact['body'],'statement':'Changed design input'}
    c.k.revise(c.owner,draft['id'],artifact['revision'],body,'New input')
    due_now(c,job);outcome=dispatch(c,job)
    assert outcome['status']=='failed' and outcome['error']['code']=='retry_stale'
    assert counter.read_text()=='1'


def test_retry_limit_and_provider_wait_are_not_ignored(full,full_project,tmp_path):
    c=full;adapter,counter=fake_cli(c,tmp_path,[limited()]);job=review_job(c,full_project,adapter,max_attempts=2)
    assert dispatch(c,job)['status']=='retry_wait';due_now(c,job)
    assert dispatch(c,job)['status']=='failed' and counter.read_text()=='2'
    assert dispatch(c,job)['replayed'] and counter.read_text()=='2'
    second,_=fake_cli(c,tmp_path,[limited(retry_after=3600)],name='long-wait')
    assert dispatch(c,review_job(c,full_project,second))['status']=='failed'


def test_semantic_rejection_is_not_transport_retry(full,full_project,tmp_path):
    c=full;adapter,counter=fake_cli(c,tmp_path,[ok(review_result('blocked'))]);job=review_job(c,full_project,adapter)
    result=dispatch(c,job)
    assert result['status']=='succeeded' and result['result']['result']['verdict']=='blocked'
    assert c.jobs.get(c.owner,job['id'])['attempt_count']==1


def test_partial_implementation_is_retained_without_auto_retry(full,full_project,tmp_path):
    c=full;task=make_task(c,full_project);c.w.claim(c.owner,full_project[0],task)
    adapter,counter=fake_cli(c,tmp_path,[limited()],writes={'calc.py':'partially implemented = True\n'})
    job=c.jobs.submit(c.owner,'execute',{'task':task,'adapter':adapter})
    outcome=dispatch(c,job);assert outcome['status']=='failed'
    partial=outcome['error']['partial_work'];assert partial['adopted'] is False
    snapshot=parse_json(c.s.blob_get(partial['snapshot_blob']))
    entry=snapshot['repos'][full_project[1]]['files']['calc.py']
    assert b'partially implemented' in c.s.blob_get(entry['blob'])
    assert c.w.task(c.owner,task)['candidate'] is None
    assert c.w.task(c.owner,task)['validity']=='needs_review'
    assert counter.read_text()=='1'
    with pytest.raises(Fault):c.jobs.retry(c.owner,job['id'],'Repeat blindly')


def test_mutating_review_failure_not_auto_retried(full,full_project,tmp_path):
    c=full;task=make_task(c,full_project)
    adapter,counter=fake_cli(c,tmp_path,[limited()],writes={'calc.py':'changed by reviewer\n'})
    job=c.jobs.submit(c.owner,'review',{'subject':task,'role':'spec','adapter':adapter})
    result=dispatch(c,job)
    assert result['status']=='failed' and result['error']['failure']['code']=='review_modified_input'


def test_atomic_dispatch_does_not_double_invoke(full,full_project,tmp_path):
    c=full;adapter,counter=fake_cli(c,tmp_path,[ok()],delay=.15);job=review_job(c,full_project,adapter)
    row=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(c.jobs.run_one,[row,row]))
    assert sum(r['status']=='succeeded' and not r.get('replayed') for r in results)==1
    assert counter.read_text()=='1'


def test_manual_retry_is_new_job_and_does_not_erase_outcome(full,full_project,tmp_path):
    c=full;adapter,counter=fake_cli(c,tmp_path,[{'type':'result','subtype':'error_during_execution','is_error':True,'errors':['unknown failure']},ok()])
    old=review_job(c,full_project,adapter);assert dispatch(c,old)['status']=='failed'
    new=c.jobs.retry(c.owner,old['id'],'Configuration inspected; repeat a read-only review')
    assert new['id']!=old['id'] and dispatch(c,new)['status']=='succeeded'
    assert c.jobs.get(c.owner,old['id'])['status']=='failed'


def test_invocation_cap_prevents_start_without_inventing_impossibility(full,full_project,tmp_path):
    c=full;p=full_project[0];adapter,counter=fake_cli(c,tmp_path,[ok()])
    c.rt.ledger.configure(c.owner,p,{'max_invocations':1},'Limit this session')
    assert dispatch(c,review_job(c,full_project,adapter))['status']=='succeeded'
    result=dispatch(c,review_job(c,full_project,adapter))
    assert result['status']=='failed' and result['error']['failure']['code']=='execution_budget'
    assert result['error']['failure']['technical_impossibility'] is False
    assert counter.read_text()=='1'
    assert c.rt.ledger.summary(c.owner,p)['invocations']==1


def test_observed_usage_retained_on_error_and_limits_never_reset(full,full_project,tmp_path):
    c=full;p=full_project[0]
    adapter,counter=fake_cli(c,tmp_path,[limited(usage={'input_tokens':7,'output_tokens':3},total_cost_usd=.12),ok()])
    c.rt.ledger.configure(c.owner,p,{'max_estimated_cost_usd':.1},'Estimated-cost stop threshold')
    job=review_job(c,full_project,adapter);assert dispatch(c,job)['status']=='retry_wait'
    usage=c.rt.ledger.summary(c.owner,p)
    assert usage['reported_tokens']==10 and usage['reported_estimated_cost_usd']==.12
    assert usage['actual_bill_verified'] is False
    due_now(c,job);assert dispatch(c,job)['status']=='failed'
    assert counter.read_text()=='1'
    c.rt.ledger.configure(c.owner,p,{'max_invocations':10},'New explicit cap, not a reset')
    assert c.rt.ledger.summary(c.owner,p)['invocations']==1


def test_missing_usage_is_unknown_not_zero(full,full_project,tmp_path):
    c=full;p=full_project[0];adapter,counter=fake_cli(c,tmp_path,[ok()])
    c.rt.ledger.configure(c.owner,p,{'max_reported_tokens':10000},'Track reported tokens')
    assert dispatch(c,review_job(c,full_project,adapter))['status']=='succeeded'
    stats=c.rt.ledger.summary(c.owner,p)
    assert stats['unknown_tokens']==1 and stats['reported_tokens']==0
    assert dispatch(c,review_job(c,full_project,adapter))['status']=='failed'
    assert counter.read_text()=='1'


@pytest.mark.parametrize('metadata',[
    {},{'usage':{}},{'usage':{'input_tokens':True,'output_tokens':2}},
    {'usage':{'input_tokens':-1,'output_tokens':2}},
    {'total_cost_usd':float('nan')},{'total_cost_usd':True},
])
def test_invalid_or_missing_usage_does_not_create_zero_measurements(metadata):
    assert usage_values('claude',metadata)==(None,None)


def test_token_cache_accounting_is_provider_specific():
    assert usage_values('claude',{'usage':{'input_tokens':2,'output_tokens':3,'cache_read_input_tokens':7,'cache_creation_input_tokens':11}})==(23,None)
    assert usage_values('codex',{'usage':{'input_tokens':20,'output_tokens':3,'cached_input_tokens':15}})==(23,None)


def test_retry_wait_does_not_block_independent_work(full,full_project,tmp_path):
    c=full;p=full_project[0];adapter,counter=fake_cli(c,tmp_path,[limited(retry_after=5),ok()])
    job=review_job(c,full_project,adapter);assert dispatch(c,job)['status']=='retry_wait'
    task=make_task(c,full_project)
    c.jobs.configure(c.owner,p,'fixture','fixture',concurrency=1,budget_seconds=60)
    c.jobs._automate()
    queued=c.s.one("SELECT * FROM jobs WHERE kind='execute' AND status='queued'")
    assert queued and c.jobs.run_one(queued)['status']=='succeeded'
    assert c.w.task(c.owner,task)['status']=='submitted'
    assert counter.read_text()=='1'


def test_retry_fingerprint_includes_changed_project_invariant(full,full_project,tmp_path):
    c=full;p=full_project[0];adapter,counter=fake_cli(c,tmp_path,[limited(),ok()]);job=review_job(c,full_project,adapter)
    assert dispatch(c,job)['status']=='retry_wait'
    invariant=c.k.propose(c.owner,p,'assumption',{'title':'New invariant','statement':'Changed global assumption','constraints':{'sync':True}})
    c.k.accept(c.owner,invariant['id'],1)
    due_now(c,job);result=dispatch(c,job)
    assert result['status']=='failed' and result['error']['code']=='retry_stale'
    assert counter.read_text()=='1'


def test_cli_wait_waits_through_retry_state(monkeypatch):
    from daikibo import cli
    states=iter([{'status':'retry_wait'},{'status':'running'},{'status':'succeeded','result':{'ok':True}}])
    class Client:
        def call(self,*args,**kwargs):return next(states)
    pauses=[];monkeypatch.setattr(cli.time,'sleep',pauses.append)
    assert cli.wait(Client(),'job')['status']=='succeeded'
    assert pauses==[.5,.5]


def test_v4_migration_retains_reported_usage_and_does_not_backfill_twice(full,full_project,tmp_path):
    import sqlite3
    from daikibo.db import SCHEMA_VERSION
    c=full;p=full_project[0]
    adapter,_=fake_cli(c,tmp_path,[ok(usage={'input_tokens':8,'output_tokens':2},cost=.003)])
    assert dispatch(c,review_job(c,full_project,adapter))['status']=='succeeded'
    home=c.s.home;c.close()
    db=sqlite3.connect(home/'state.sqlite3')
    db.execute('DROP TABLE program_origins')
    for table in ('execution_usage','execution_limits','job_attempts','knowledge_snapshots'):
        db.execute('DROP TABLE '+table)
    for column in ('attempt_count','retry_due','retry_deadline','retry_policy','retry_fingerprint'):
        db.execute('ALTER TABLE jobs DROP COLUMN '+column)
    db.execute('PRAGMA user_version=4');db.commit();db.close()
    for _ in range(2):
        restored=Control(home,mode='validation',start_workers=False)
        try:
            owner=Actor('local-user','owner')
            assert restored.s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
            usage=restored.rt.ledger.summary(owner,p)
            assert usage['invocations']==1 and usage['reported_tokens']==10
            assert usage['reported_estimated_cost_usd']==.003
            assert (home/'pre-migration-v4.sqlite3').is_file()
        finally:restored.close()


def test_running_attempt_is_unknown_after_restart_and_not_retried(full,full_project,tmp_path):
    c=full;adapter,counter=fake_cli(c,tmp_path,[ok()]);job=review_job(c,full_project,adapter)
    c.s.execute("UPDATE jobs SET status='running',attempt_count=1 WHERE id=?",(job['id'],))
    c.s.execute("INSERT INTO job_attempts(job,attempt,status,started) VALUES(?,1,'running',?)",(job['id'],timestamp()))
    home=c.s.home;c.close()
    restored=Control(home,mode='validation',start_workers=False)
    try:
        owner=Actor('local-user','owner');state=restored.jobs.get(owner,job['id'])
        assert state['status']=='unknown' and state['attempts'][0]['status']=='unknown'
        assert dispatch(restored,job)['replayed'] and not counter.exists()
    finally:restored.close()


def test_large_provider_millisecond_delay_is_not_shortened_before_unit_conversion():
    from daikibo.execution_errors import classify_error
    value=classify_error({'code':'rate_limit','retry_delay_ms':3_600_000})
    assert value['retry_after_seconds']==3600.0
    assert classify_error({'code':'rate_limit','retry_delay_ms':900_000_000})['retry_after_seconds']==7*86400


def test_failed_work_can_be_inspected_through_agent_api_without_adoption(full,full_project,tmp_path):
    import base64
    c=full;p,rid,_,_=full_project;task=make_task(c,full_project);c.w.claim(c.owner,p,task)
    adapter,_=fake_cli(c,tmp_path,[limited()],writes={'calc.py':'retained failure\n'})
    out=dispatch(c,c.jobs.submit(c.owner,'execute',{'task':task,'adapter':adapter}))
    receipt=c.g.receipt(out['error']['receipt']);agent=Actor('planner','agent',project=p)
    changes=c.invoke(agent,'run.work_changes',{'run':receipt['run']})
    change=next(x for x in changes['changes'] if x['path']=='calc.py')
    read=c.invoke(agent,'run.work_read',{'run':receipt['run'],'repo':rid,'path':'calc.py','expected_digest':change['after']['blob']})
    assert base64.b64decode(read['base64'])==b'retained failure\n'
    assert not read['adopted_by_read'] and changes['candidate'] is None
    assert not c.w.task(c.owner,task)['candidate']
    with pytest.raises(Fault):c.execution_history.read_file(agent,receipt['run'],rid,'calc.py','0'*64)
    with pytest.raises(Fault):c.execution_history.changes(Actor('other','agent',project='other-project'),receipt['run'])


def test_successful_process_with_rejected_scope_still_retains_work(full,full_project,tmp_path):
    c=full;p,rid,_,_=full_project;task=make_task(c,full_project);c.w.claim(c.owner,p,task)
    adapter,_=fake_cli(c,tmp_path,[ok({'message':'changed outside assigned scope'})],writes={'unassigned.txt':'must not vanish'})
    out=dispatch(c,c.jobs.submit(c.owner,'execute',{'task':task,'adapter':adapter}))
    assert out['status']=='failed' and out['error']['code']=='scope_violation'
    receipt=c.g.receipt(out['error']['receipt']);assert receipt['failure'] is None
    assert out['error']['partial_work']['snapshot_blob']
    changes=c.execution_history.changes(c.owner,receipt['run'])
    assert any(x['path']=='unassigned.txt' for x in changes['changes'])
    assert changes['candidate'] is None


def test_large_retained_manifest_does_not_lose_referenced_files_on_gc(full,tmp_path):
    from daikibo.operations import referenced_blob_hashes
    c=full;child=c.s.blob_put(b'precious observed working file')
    # Cross a one-megabyte read boundary in a >32MiB manifest; old GC skipped these.
    data=b' '*(33*1024*1024-31)+child.encode()+b' end'
    parent=c.s.blob_put(data)
    c.s.execute('INSERT INTO meta VALUES(?,?)',('retained.large.snapshot',parent))
    orphan=c.s.blob_put(b'unreferenced old file')
    for h in (child,parent,orphan):os.utime(c.s.blobs/h[:2]/h[2:],(timestamp()-172800,timestamp()-172800))
    c.ops.garbage_collect(c.owner,dry_run=False)
    assert c.s.blob_get(child)==b'precious observed working file'
    assert not (c.s.blobs/orphan[:2]/orphan[2:]).exists()
    tiny=tmp_path/'split-hash';tiny.write_bytes(b'---'+child.encode()+b'---')
    assert child in set(referenced_blob_hashes(tiny,chunk_bytes=17))
