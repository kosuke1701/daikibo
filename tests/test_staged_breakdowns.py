"""Uploads are persistence and coverage checks, not semantic review evidence."""
import copy
import pytest
from daikibo.common import Fault, canonical
from daikibo.control import Control
from test_reviewed_breakdowns import setup, review_all, make_work


def begin(s):
    c,p,r,q,program,d,t,units=s
    return c.breakdown_inputs.begin(c.owner,program,'Full plan','Do not discard any requirement')


def test_staged_plan_survives_restart_and_still_requires_real_reviews(setup):
    c,*_=setup; units=setup[-1]; up=begin(setup)
    a=c.breakdown_inputs.put(c.owner,up['upload'],0,[units[0]])
    assert a['revision']==1
    home=c.s.home;c.close()
    with_reopen=Control(home,mode='validation',start_workers=False)
    try:
        owner=with_reopen.sec.authenticate(None)
        assert with_reopen.breakdown_inputs.status(owner,up['upload'])['units']==1
        b=with_reopen.breakdown_inputs.put(owner,up['upload'],1,[units[1]])
        result=with_reopen.breakdown_inputs.finalize(owner,up['upload'],b['revision'])
        assert result['status']=='proposed'
        assert with_reopen.breakdown_inputs.finalize(owner,up['upload'],b['revision'])['replayed']
        with pytest.raises(Fault) as exc:with_reopen.breakdowns.activate(owner,result['id'])
        assert exc.value.code=='breakdown_gate_denied'
        with_reopen.owner=owner;review_all(with_reopen,result['id'])
        assert with_reopen.breakdowns.activate(owner,result['id'])['status']=='active'
    finally:with_reopen.close()


def test_same_batch_replay_is_exactly_once(setup):
    c=setup[0];up=begin(setup);units=setup[-1]
    first=c.breakdown_inputs.put(c.owner,up['upload'],0,units)
    assert c.breakdown_inputs.put(c.owner,up['upload'],0,units)['replayed']
    assert c.breakdown_inputs.status(c.owner,up['upload'])['units']==len(units)
    wrong=copy.deepcopy(units);wrong[0]['title']='other'
    with pytest.raises(Fault) as exc:c.breakdown_inputs.put(c.owner,up['upload'],0,wrong)
    assert exc.value.code=='idempotency_conflict'


@pytest.mark.parametrize('failure',['duplicate','stale','oversize','invalid'])
def test_rejected_batch_does_not_partial_write(setup,failure):
    c=setup[0];up=begin(setup);units=setup[-1];rev=0
    if failure=='duplicate':units=[units[0],units[0]]
    if failure=='stale':rev=3
    if failure=='oversize':units=copy.deepcopy(units);units[0]['rationale']='X'*(1024*1024)
    if failure=='invalid':units=[units[0],{'bad':'unit'}]
    with pytest.raises(Fault):c.breakdown_inputs.put(c.owner,up['upload'],rev,units)
    state=c.breakdown_inputs.status(c.owner,up['upload'])
    assert state['units']==0 and state['revision']==0


def test_duplicate_from_old_batch_rolls_back_entire_new_batch(setup):
    c=setup[0];up=begin(setup);units=setup[-1]
    c.breakdown_inputs.put(c.owner,up['upload'],0,[units[0]])
    with pytest.raises(Fault):c.breakdown_inputs.put(c.owner,up['upload'],1,[units[1],units[0]])
    assert c.breakdown_inputs.status(c.owner,up['upload'])['units']==1


def test_finalize_does_not_accept_omitted_obligation(setup):
    c=setup[0];up=begin(setup);units=copy.deepcopy(setup[-1]);units[1]['obligations']=[]
    c.breakdown_inputs.put(c.owner,up['upload'],0,units)
    with pytest.raises(Fault):c.breakdown_inputs.finalize(c.owner,up['upload'],1)
    assert c.breakdown_inputs.status(c.owner,up['upload'])['status']=='open'
    assert not c.s.one('SELECT id FROM breakdowns')


def test_changed_input_between_batches_requires_explicit_replan(setup):
    c,p,r,q,program,d,t,units=setup;up=begin(setup)
    c.breakdown_inputs.put(c.owner,up['upload'],0,units)
    make_work(c,p,r,q,d)
    with pytest.raises(Fault) as exc:c.breakdown_inputs.finalize(c.owner,up['upload'],1)
    assert exc.value.code=='stale_upload_scope'


def test_abandoned_upload_cannot_be_finalized_and_preserves_history(setup):
    c=setup[0];up=begin(setup);c.breakdown_inputs.put(c.owner,up['upload'],0,setup[-1])
    c.breakdown_inputs.abandon(c.owner,up['upload'],1,'Explicit replacement, no product scope change')
    with pytest.raises(Fault):c.breakdown_inputs.finalize(c.owner,up['upload'],2)
    state=c.breakdown_inputs.status(c.owner,up['upload'],limit=1)
    assert state['units']==2 and len(state['page'])==1 and state['next_offset']==1
    assert not c.s.one('SELECT id FROM breakdowns')


def test_staged_api_is_exposed_to_native_agent(full):
    for method in ('begin','put','status','finalize','abandon'):
        assert 'breakdown.upload_'+method in full.routes
    from daikibo.supervisor import ALLOWED
    assert 'breakdown.upload_finalize' in ALLOWED


def test_units_and_summary_are_bounded_pages(setup):
    from test_reviewed_breakdowns import propose
    c=setup[0];result=propose(setup)
    page=c.breakdowns.units(c.owner,result['id'],limit=1)
    assert len(page['units'])==1 and page['next_offset']==1
    assert c.breakdowns.units(c.owner,result['id'],offset=1)['next_offset'] is None
    summary=c.breakdowns.get(c.owner,result['id'],include_structure=False)
    assert set(summary['structure'])=={'obligation_count','task_count'}


def test_read_cache_expires_after_each_scope_view(setup):
    c,p,r,q,program,d,t,units=setup
    before=c.breakdowns._scope(c.owner,p)
    make_work(c,p,r,q,d)
    after=c.breakdowns._scope(c.owner,p)
    assert len(after['tasks'])==len(before['tasks'])+1


def test_task_definition_scan_avoids_repeated_per_task_queries(setup):
    c,p,r,q,program,d,t,units=setup
    for _ in range(50):make_work(c,p,r,q,d)
    queries=[];c.s.conn.set_trace_callback(queries.append)
    try:value=c.breakdowns._scope(c.owner,p)
    finally:c.s.conn.set_trace_callback(None)
    assert len(value['tasks'])==51
    assert len([q for q in queries if q.startswith('SELECT')])<20


def test_v6_to_v7_migration_retains_canonical_records(full,full_project):
    import sqlite3
    from daikibo.control import Control
    c=full;home=c.s.home;req=full_project[2];old=c.k.artifact(c.owner,req);c.close()
    db=sqlite3.connect(home/'state.sqlite3')
    db.execute('DROP TABLE program_origins')
    for table in ('breakdown_upload_batches','breakdown_upload_units','breakdown_uploads'):db.execute('DROP TABLE '+table)
    db.execute('PRAGMA user_version=6');db.commit();db.close()
    restored=Control(home,mode='validation',start_workers=False)
    try:
        actual=restored.k.artifact(restored.sec.authenticate(None),req)
        assert actual['digest']==old['digest'] and actual['body']==old['body']
        from daikibo.db import SCHEMA_VERSION
        assert restored.s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
        assert (home/'pre-migration-v6.sqlite3').is_file()
    finally:restored.close()


def test_staged_writes_are_real_progress_but_replay_is_not(setup):
    c,p,*_=setup;engineering=c.supervisor.state_digest(p);before=c.supervisor.progress_digest(p)
    up=begin(setup)
    assert c.supervisor.progress_digest(p)!=before
    assert c.supervisor.state_digest(p)==engineering  # Staging is not an adopted design.
    before=c.supervisor.progress_digest(p)
    c.breakdown_inputs.put(c.owner,up['upload'],0,[setup[-1][0]])
    after=c.supervisor.progress_digest(p);assert before!=after
    c.breakdown_inputs.put(c.owner,up['upload'],0,[setup[-1][0]])
    assert c.supervisor.progress_digest(p)==after
    c.breakdown_inputs.abandon(c.owner,up['upload'],1,'Explicit replacement')
    assert c.supervisor.progress_digest(p)!=after


def test_uploads_can_be_discovered_after_lost_conversation_state(setup):
    c,p,r,q,program,d,t,units=setup
    first=begin(setup);second=begin(setup)
    page=c.invoke(c.owner,'breakdown.upload_list',{'project':p,'limit':1,'status':'open'})
    assert len(page['uploads'])==1 and page['next_offset']==1
    other=c.invoke(c.owner,'breakdown.upload_list',{'project':p,'offset':1,'limit':1})
    assert {page['uploads'][0]['id'],other['uploads'][0]['id']}=={first['upload'],second['upload']}
    c.breakdown_inputs.abandon(c.owner,second['upload'],0,'Obsolete unsubmitted draft')
    assert len(c.breakdown_inputs.list(c.owner,p,status='open')['uploads'])==1
    assert 'breakdown.upload_list' in c.read_routes


def test_upload_listing_is_project_scoped_and_bounded(setup):
    c,p,r,q,program,d,t,units=setup;begin(setup)
    other=c.k.create_project(c.owner,'Separate project')['id']
    assert c.breakdown_inputs.list(c.owner,other)['uploads']==[]
    with pytest.raises(Fault):c.breakdown_inputs.list(c.owner,other,program=program)
    for params in ({'limit':0},{'limit':201},{'offset':-1},{'status':'unknown'}):
        with pytest.raises(Fault):c.breakdown_inputs.list(c.owner,p,**params)
