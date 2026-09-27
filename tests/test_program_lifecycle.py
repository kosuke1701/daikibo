"""Composite end conditions; local fixture runs do not certify a real product."""
import copy
import json
from pathlib import Path
import pytest
from daikibo.common import Actor, Fault, canonical, parse_json
from daikibo.knowledge_history import validate_specifications
from test_reviewed_breakdowns import setup, propose, adopt, make_work, make_domain, accepted
from test_delivery_git_and_recovery import profile
from conftest import ensure_current_root, finish_task


def trace_for(c,p,q):
    design=accepted(c,p,'design','Design for '+q)
    test=accepted(c,p,'test','Test design for '+q)
    c.k.link(c.owner,design,q,'realizes','asserted','Fixture asserted design link')
    c.k.link(c.owner,test,q,'verifies','asserted','Fixture asserted test link')


def test_individual_task_success_is_not_program_completion(setup):
    c,p,r,q,program,d,t,units=setup
    adopt(setup);ensure_current_root(c,p,t);c.w.ready(c.owner,t);finish_task(c,p,t)
    trace_for(c,p,q)
    before=c.lifecycle.completion(c.owner,program)
    assert not before['completed']
    assert {'phase_incomplete','integrated_delivery_required','validation_not_final'} <= {f['code'] for f in before['failures']}
    assert c.w.task(c.owner,t)['status']=='completed'


def test_validation_cannot_close_a_workflow_even_after_all_fixture_integration_checks(setup):
    c,p,r,q,program,d,t,units=setup
    c.d.configure(c.owner,p,profile(p,r,q,t))
    adopt(setup);trace_for(c,p,q);ensure_current_root(c,p,t);c.w.ready(c.owner,t);finish_task(c,p,t)
    delivery=c.d.prepare(c.owner,p)['id'];results=c.d.verify(c.owner,delivery)
    assert all(x['passed'] for x in results['results'])
    for role in ('integration','goal_validation'):c.rt.review(c.owner,delivery,role,'markers')
    c.s.execute("UPDATE programs SET phase='delivery' WHERE id=?",(program,))
    receipt=c.rt.review(c.owner,program,'phase','markers')['receipt']
    rev=c.p.next(c.owner,program)['revision']
    with pytest.raises(Fault) as exc:c.lifecycle.finish(c.owner,program,rev,delivery,receipt)
    assert exc.value.code=='program_gate_denied'
    assert c.s.one('SELECT count(*) AS n FROM program_closures')['n']==0
    assert any(f['code']=='validation_not_final' for f in exc.value.details)


def test_phase_backtrack_requires_recorded_cause_and_only_moves_workflow(setup):
    c,p,r,q,program,d,t,units=setup
    c.s.execute("UPDATE programs SET phase='design' WHERE id=?",(program,))
    before=c.w.task(c.owner,t)
    result=c.lifecycle.reopen(c.owner,program,1,'boundaries','Module design was based on a disproven assumption',q)
    assert result['phase']=='boundaries' and result['revision']==2
    assert c.w.task(c.owner,t)==before
    history=parse_json(c.s.one('SELECT body FROM programs WHERE id=?',(program,))['body'])['history']
    assert history[-1]['cause']==q and history[-1]['to']=='boundaries'
    with pytest.raises(Fault) as exc:c.lifecycle.reopen(c.owner,program,1,'requirements','stale client',q)
    assert exc.value.code=='stale_revision'


@pytest.mark.parametrize('bad',['forward','unknown_phase','missing_cause','other_project'])
def test_invalid_phase_returns_are_rejected(setup,bad):
    c,p,r,q,program,d,t,units=setup
    target='delivery' if bad=='forward' else 'invented' if bad=='unknown_phase' else 'requirements'
    cause=q
    if bad=='missing_cause':cause='MISSING-FINDING'
    if bad=='other_project':
        p2=c.k.create_project(c.owner,'Other')['id'];cause=accepted(c,p2,'finding','Other project finding')
    with pytest.raises(Fault):c.lifecycle.reopen(c.owner,program,1,target,'Do not change unrelated work',cause)
    assert c.p.next(c.owner,program)['revision']==1


def test_agent_backtracking_requires_actual_current_impact_review(setup):
    c,p,r,q,program,d,t,units=setup;c.s.execute("UPDATE programs SET phase='design' WHERE id=?",(program,))
    actor=Actor('planner','agent',p)
    with pytest.raises(Fault) as exc:c.lifecycle.reopen(actor,program,1,'boundaries','Correct scope',q)
    assert exc.value.code=='review_required'
    run=c.rt.review(c.owner,program,'impact','markers')
    assert c.lifecycle.reopen(actor,program,1,'boundaries','Correct scope',q,run['receipt'])['revision']==2
    with pytest.raises(Fault):c.lifecycle.reopen(actor,program,2,'requirements','Reuse old review',q,run['receipt'])


def test_historical_closure_record_is_not_recertification(setup,monkeypatch):
    """UNIT TEST: inject gate outcome to exercise persistence, NOT final acceptance."""
    c,p,r,q,program,d,t,units=setup;adopt(setup)
    c.s.execute("UPDATE programs SET phase='delivery' WHERE id=?",(program,))
    # Only a unit fixture for the gate's successful branch. No deployed code is claimed.
    delivery='DELIVERY-UNIT-FIXTURE'
    c.s.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)",(delivery,p,'{}','fixture','prepared',0))
    def unit_result(*args,**kwargs):return {'completed':True,'failures':[],'fixture_injected':True}
    monkeypatch.setattr(c.lifecycle,'completion',unit_result)
    receipt=c.rt.review(c.owner,program,'phase','markers')['receipt']
    rev=c.p.next(c.owner,program)['revision']
    result=c.lifecycle.finish(c.owner,program,rev,delivery,receipt)
    assert c.lifecycle.finish(c.owner,program,rev,delivery,receipt)['replayed']
    assert c.lifecycle.status(c.owner,program)['state']=='closed_recorded'
    assert not c.lifecycle.status(c.owner,program)['current_completion_verified']
    c.k.source(c.owner,p,'New user request requiring assessment')
    assert c.lifecycle.status(c.owner,program)['state']=='reassessment_required'
    c.lifecycle.reopen(c.owner,program,rev,'requirements','New request',q)
    assert c.s.one('SELECT id FROM program_closures WHERE id=?',(result['id'],))
    assert c.lifecycle.status(c.owner,program)['state']=='reassessment_required'


def test_native_completion_and_stop_hook_recognize_whole_program(setup):
    c,p,r,q,program,d,t,units=setup
    root=c.s.one('SELECT path FROM repos WHERE id=?',(r,))['path']
    c.native.attach(c.owner,'session',root,p)
    result=c.native.completion(c.owner,'session',program)
    assert not result['completed']
    assert c.native.stop_feedback(c.owner,'session')['decision']=='block'
    assert c.native.stop_feedback(c.owner,'session',stop_hook_active=True)=={}


def test_native_can_query_new_operations_without_a_real_cli(setup):
    c,p,r,q,program,d,t,units=setup
    root=c.s.one('SELECT path FROM repos WHERE id=?',(r,))['path']
    c.native.attach(c.owner,'conversation',root,p)
    result=c.native.actions(c.owner,'conversation',[{'method':'breakdown.propose','params':{'program':program,'title':'Conversation plan','rationale':'Full scope','units':units},'as':'planned'},
                                                  {'method':'breakdown.get','params':{'breakdown':{'$ref':'planned.id'}}}])
    assert result['all_applied'],result
    assert result['actions'][1]['result']['packet_count']>=2


def test_only_classifying_a_source_or_linking_artifacts_advances_planner_state(setup):
    c,p,r,q,program,d,t,units=setup
    source=c.k.source(c.owner,p,'An additional question')['id']
    before=c.supervisor.state_digest(p);binding=c.p.program_binding(program)
    c.k.classify(c.owner,source,0,len('An additional question'),'question',[],'Needs discussion')
    after=c.supervisor.state_digest(p)
    assert before!=after and binding!=c.p.program_binding(program)
    a=accepted(c,p,'design','Design');b=accepted(c,p,'test','Test')
    before=c.supervisor.state_digest(p);c.k.link(c.owner,a,b,'depends_on','inferred','Discovery connection')
    assert c.supervisor.state_digest(p)!=before
    # An observed reviewer run must not invalidate the phase binding it just reviewed.
    before=c.p.program_binding(program);c.rt.review(c.owner,program,'phase','markers')
    assert c.p.program_binding(program)==before


def test_derived_design_or_draft_child_cannot_hide_a_parent_acceptance(setup):
    c,p,r,q,program,d,t,units=setup
    child=c.k.propose(c.owner,p,'requirement',{'title':'Draft child','statement':'not yet agreed','acceptance':['AC-DRAFT']})['id']
    c.k.link(c.owner,q,child,'decomposes','inferred','Not a completed decomposition')
    trace=c.k.trace(c.owner,p)
    assert trace['leaf_count']==1 and trace['requirement_count']==1
    c.w.cancel(c.owner,t,'Cancelled implementation')
    assert any(x['requirement']==q and 'task' in x['missing'] and 'acceptance_task:AC-ADD' in x['missing'] for x in c.k.trace(c.owner,p)['missing'])


def test_draft_design_and_test_links_are_not_adopted_traceability(setup):
    c,p,r,q,program,d,t,units=setup
    uncovered=accepted(c,p,'requirement','Uncovered draft target',acceptance=['AC-DRAFT'])
    for kind,relation in [('design','realizes'),('test','verifies')]:
        draft=c.k.propose(c.owner,p,kind,{'title':kind,'statement':'Draft only'})['id']
        c.k.link(c.owner,draft,uncovered,relation,'asserted','Link alone is insufficient')
    gaps=next(item['missing'] for item in c.k.trace(c.owner,p)['missing']
              if item['requirement']==uncovered)
    assert 'design' in gaps and 'verification_design' in gaps


def test_breakdown_history_is_preserved_and_checked_in_portable_specifications(setup):
    c,p,r,q,program,d,t,units=setup;b=adopt(setup)
    exported=c.k.export(c.owner,p)
    assert exported['planning_history']['breakdowns'][0]['id']==b['id']
    assert exported['planning_history']['adoptions'] and not exported['planning_history']['fresh_evidence']
    assert validate_specifications(exported)['artifacts']>=2
    damaged=copy.deepcopy(exported);damaged['planning_history']['members'].pop()
    with pytest.raises(Fault) as exc:validate_specifications(damaged)
    assert exc.value.code=='invalid_snapshot'
    baseline=c.k.baseline(c.owner,p)
    assert c.history.verify(c.owner,baseline['id'])['snapshot_verified']


@pytest.mark.parametrize('corruption',['packet','member','previous','program','adoption'])
def test_portable_history_rejects_invalid_planning_references(setup,corruption):
    c=setup[0];adopt(setup);exported=c.k.export(c.owner,setup[1]);h=exported['planning_history']
    if corruption=='packet':h['packets'][0]['body']['serialized_fragment']='Changed content'
    if corruption=='member':h['members'][0]['packet']='MISSING'
    if corruption=='previous':h['breakdowns'][0]['previous']='MISSING'
    if corruption=='program':h['programs']=[]
    if corruption=='adoption':h['adoptions'][0]['breakdown']='MISSING'
    with pytest.raises(Fault):validate_specifications(exported)
