"""Actual local subprocess fixtures; no real LLM semantic approval is claimed."""
import copy
import json
import sys
import pytest
from daikibo.common import Actor, Fault, canonical, digest, parse_json
from conftest import ensure_current_root, finish_task
from test_reviewed_breakdowns import setup, propose, review_all, adopt, accepted, make_domain, make_work, leaf


def ws_propose(s, units=('arithmetic',), **kw):
    return s[0].workstreams.propose(s[0].owner, s[4], 'Delegated arithmetic', 'Preserve full root scope; isolate assignment only', list(units), **kw)


def review_scope(c, scope, adapter='markers'):
    offset=0
    while True:
        page=c.workstreams.get(c.owner, scope, offset=offset, limit=3)
        for packet in page['packets']:
            for role in ('design','trace'): c.rt.review(c.owner,packet['id'],role,adapter)
        if page['next_offset'] is None: break
        offset=page['next_offset']


def activate_scope(s, **kw):
    p=ws_propose(s, **kw); c=s[0]
    review_scope(c,p['scope']); c.workstreams.activate(c.owner,p['scope']); return p['scope']


def second_unit(s, dependent=False):
    c,p,r,q,program,d,t,units=s
    q2=accepted(c,p,'requirement','Second output',acceptance=['AC-SECOND'])
    t2=make_work(c,p,r,q2,d,deps=[t] if dependent else [],acs=['AC-SECOND'])
    units=copy.deepcopy(units)
    units.append(leaf('second',d,[t2],q2,acs=['AC-SECOND'],parent='system',dependencies=[{'unit':'arithmetic','interface':None}] if dependent else []))
    value=propose(s,units);review_all(c,value['id']);c.breakdowns.activate(c.owner,value['id'])
    return t2,q2


def finish(c, p, task):
    ensure_current_root(c, p, task)
    if c.w.task(c.owner, task)["status"] == "planned":
        c.w.ready(c.owner,task)
    return finish_task(c,p,task)


def withdraw(c,scope,reason='Return work to parent without dropping tasks'):
    record=c.rt.review(c.owner,scope,'impact','markers',proposal={'reason':reason})
    return c.workstreams.withdraw(c.owner,scope,reason,record['receipt'])


def test_proposal_requires_reviewed_current_root(setup):
    with pytest.raises(Fault) as e: ws_propose(setup)
    assert e.value.code=='breakdown_required'
    p=propose(setup)
    with pytest.raises(Fault):ws_propose(setup)
    review_all(setup[0],p['id']);setup[0].breakdowns.activate(setup[0].owner,p['id'])
    assert ws_propose(setup)['tasks']==1


def test_actual_two_role_reviews_and_exact_markers_required(setup):
    c=setup[0];adopt(setup);w=ws_propose(setup)['scope']
    assert not c.workstreams.status(c.owner,w)['current']
    with pytest.raises(Fault): c.workstreams.activate(c.owner,w)
    page=c.workstreams.get(c.owner,w)
    for p in page['packets']:c.rt.review(c.owner,p['id'],'design','markers')
    with pytest.raises(Fault):c.workstreams.activate(c.owner,w)
    for p in page['packets']:c.rt.review(c.owner,p['id'],'trace','markers')
    assert not c.workstreams.activate(c.owner,w)['replayed']
    assert c.workstreams.activate(c.owner,w)['replayed']
    assert c.workstreams.status(c.owner,w)['current']
    assert c.s.one('SELECT count(*) n FROM workstream_records')['n']==1


@pytest.mark.parametrize('units',[[],['missing'],['system'],['arithmetic','arithmetic'],[None], 'arithmetic'])
def test_invalid_or_aggregate_assignment_rejected_atomically(setup, units):
    c=setup[0];adopt(setup)
    with pytest.raises(Fault):c.workstreams.propose(c.owner,setup[4],'Name','Reason',units)
    assert c.s.one('SELECT count(*) n FROM workstreams')['n']==0
    assert c.s.one('SELECT count(*) n FROM workstream_packets')['n']==0


def test_exact_parent_conditions_and_root_scope_retained(setup):
    c,p,r,q,program,d,t,units=setup;t2,q2=second_unit(setup)
    root_before=c.breakdowns.active(c.owner,program)
    w=activate_scope(setup)
    assert c.workstreams.selection(c.owner,w,'obligations')['items']==[{'requirement':q,'acceptance':'AC-ADD'}]
    assert c.workstreams.selection(c.owner,w)['items']==[t]
    assert c.breakdowns.active(c.owner,program)==root_before
    assert c.breakdowns.program_status(c.owner,program)['current']
    assert len(c.breakdowns._scope(c.owner,p)['requirements'])==2
    assert len(c.breakdowns._scope(c.owner,p)['tasks'])==2
    assert not c.workstreams.completion(c.owner,w)['ready']


def test_disjoint_siblings_and_nested_subset(setup):
    c=setup[0];second_unit(setup)
    outer=activate_scope(setup,units=('arithmetic','second'))
    first=activate_scope(setup,parent=outer)
    other=activate_scope(setup,units=('second',),parent=outer)
    assert c.workstreams.status(c.owner,first)['current'] and c.workstreams.status(c.owner,other)['current']
    with pytest.raises(Fault) as e:ws_propose(setup,parent=first,units=('second',))
    assert e.value.code=='outside_parent_scope'
    with pytest.raises(Fault) as e:ws_propose(setup,parent=outer)
    assert e.value.code=='overlapping_scope'


def test_concurrent_sibling_proposal_can_only_adopt_one(setup):
    c=setup[0];adopt(setup)
    a=ws_propose(setup)['scope'];b=ws_propose(setup)['scope']
    review_scope(c,a);review_scope(c,b);c.workstreams.activate(c.owner,a)
    with pytest.raises(Fault) as e:c.workstreams.activate(c.owner,b)
    assert e.value.code=='overlapping_scope'
    assert c.workstreams.get(c.owner,b)['status']=='proposed'


def test_unrelated_task_revision_does_not_stale_local_scope(setup):
    c,p,r,q,program,d,t,units=setup;t2,q2=second_unit(setup)
    first=activate_scope(setup);other=activate_scope(setup,units=('second',))
    c.w.replan(c.owner,t2,c.w.task(c.owner,t2)['revision'],'Only second unit changes')
    assert c.workstreams.status(c.owner,first)['current']
    assert not c.workstreams.status(c.owner,other)['current']
    assert not c.breakdowns.program_status(c.owner,program)['current']


@pytest.mark.parametrize('change',['task','requirement','policy','invariant','plan_replacement'])
def test_local_or_shared_input_change_invalidates_assignment(setup,change):
    c,p,r,q,program,d,t,units=setup;root=adopt(setup);w=activate_scope(setup)
    if change=='task':c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'Task definition revised')
    elif change=='requirement':
        a=c.k.artifact(c.owner,q);b=a['body'];b['statement']+=' changed';c.k._revise(c.owner,a,a['revision'],b,'new specification','accepted')
    elif change=='invariant':accepted(c,p,'requirement','Global rule',acceptance=['AC-GLOBAL'],critical=True)
    elif change=='policy':
        policy=c.g.policy(p)['body'];policy['max_run_seconds']+=1
        c.s.execute('UPDATE policies SET body=?,digest=? WHERE project=?',(canonical(policy).decode(),digest(policy),p))
    elif change=='plan_replacement':
        new=propose(setup,previous=root['id']);review_all(c,new['id']);c.breakdowns.activate(c.owner,new['id'],root['id'])
    assert not c.workstreams.status(c.owner,w)['current']
    with pytest.raises(Fault):c.rt.review(c.owner,c.workstreams.get(c.owner,w)['packets'][0]['id'],'design','markers')
    assert not c.workstreams.completion(c.owner,w)['ready']


def test_external_prerequisites_are_visible_and_not_owned(setup):
    c,p,r,q,program,d,t,units=setup;t2,q2=second_unit(setup,dependent=True)
    w=activate_scope(setup,units=('second',))
    ext=c.workstreams.selection(c.owner,w,'external_dependencies')['items']
    assert ext[0]['dependency']==t and ext[0]['task']==t2
    result=c.workstreams.completion(c.owner,w)
    assert result['owned_task_count']==1 and result['external_task_count']==1
    assert any(f['code']=='external_prerequisite_incomplete' for f in result['failures'])
    c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'Boundary predecessor changed')
    assert not c.workstreams.status(c.owner,w)['current']


def test_finish_needs_real_tests_and_is_never_deploy_ready(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup)
    with pytest.raises(Fault):c.workstreams.finish(c.owner,w)
    finish(c,p,t)
    assert c.workstreams.completion(c.owner,w)['ready']
    result=c.workstreams.finish(c.owner,w)
    assert result['state']=='work_verified' and result['deploy_ready'] is False
    assert c.workstreams.finish(c.owner,w)['replayed']
    root=c.lifecycle.completion(c.owner,program)
    assert not root['completed']
    assert any(f['code']=='integrated_delivery_required' for f in root['failures'])


def test_child_success_does_not_discharge_sibling_or_root(setup):
    c,p,r,q,program,d,t,units=setup;t2,q2=second_unit(setup)
    outer=activate_scope(setup,units=('arithmetic','second'))
    child=activate_scope(setup,parent=outer);other=activate_scope(setup,units=('second',),parent=outer)
    finish(c,p,t);c.workstreams.finish(c.owner,child)
    assert not c.workstreams.completion(c.owner,outer)['ready']
    assert not c.workstreams.program_audit(c.owner,program)['current']
    finish(c,p,t2);c.workstreams.finish(c.owner,other);c.workstreams.finish(c.owner,outer)
    assert c.workstreams.program_audit(c.owner,program)['current']


def test_three_levels_recheck_descendant_and_missing_receipt(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup)
    a=activate_scope(setup);b=activate_scope(setup,parent=a);child=activate_scope(setup,parent=b)
    finish(c,p,t)
    with pytest.raises(Fault):c.workstreams.finish(c.owner,a)
    for scope in (child,b,a):c.workstreams.finish(c.owner,scope)
    assert c.workstreams.completion(c.owner,a)['ready']
    receipt=c.s.one("SELECT body FROM receipts WHERE subject=? AND role='test'",(t,))
    if receipt is None:receipt=c.s.one("SELECT body FROM receipts WHERE subject=? AND role='spec'",(t,))
    record=parse_json(receipt['body']);c.s.blob_path(record['stdout_blob']).unlink()
    assert not c.workstreams.completion(c.owner,a)['ready']


def test_no_phase_or_task_state_changes_when_delegating_or_withdrawing(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup)
    before=c.w.task(c.owner,t);w=activate_scope(setup)
    res=withdraw(c,w)
    assert res['tasks_cancelled']==[] and res['root_scope_reduced'] is False
    assert c.w.task(c.owner,t)==before
    assert c.workstreams.get(c.owner,w)['history_only']
    assert c.workstreams.program_audit(c.owner,program)['current']


def test_withdrawal_requires_exact_observed_review(setup):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    review=c.rt.review(c.owner,w,'impact','markers',proposal={'reason':'Return to root'})
    with pytest.raises(Fault):c.workstreams.withdraw(c.owner,w,'Different effect',review['receipt'])
    assert c.workstreams.withdraw(c.owner,w,'Return to root',review['receipt'])['status']=='withdrawn'


def test_active_children_cannot_be_implicitly_dropped(setup):
    c=setup[0];adopt(setup);p=activate_scope(setup);ch=activate_scope(setup,parent=p)
    with pytest.raises(Fault):withdraw(c,p)
    with pytest.raises(Fault):ws_propose(setup,previous=p)
    withdraw(c,ch);withdraw(c,p)
    assert c.workstreams.get(c.owner,ch)['status']=='withdrawn'


def test_replacement_preserves_original_and_tasks(setup):
    c=setup[0];adopt(setup);original=activate_scope(setup)
    new=activate_scope(setup,previous=original)
    assert c.workstreams.get(c.owner,original)['status']=='superseded'
    assert c.workstreams.status(c.owner,new)['current']
    assert c.s.one('SELECT count(*) n FROM tasks')['n']==1
    assert c.s.one('SELECT count(*) n FROM workstreams')['n']==2


def test_stale_scope_can_be_withdrawn_without_erasing_root_requirements(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup)
    c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'Need a new design')
    assert not c.workstreams.status(c.owner,w)['current']
    assert withdraw(c,w)['status']=='withdrawn'
    assert c.k.artifact(c.owner,q)['status']=='accepted'


@pytest.mark.parametrize('fault',['missing_packet','missing_stdout','wrong_marker','later_fail','later_findings'])
def test_review_corruption_or_new_failure_cannot_reuse_old_pass(setup,tmp_path,fault):
    c=setup[0];adopt(setup);w=ws_propose(setup)['scope'];review_scope(c,w)
    packet=c.workstreams.get(c.owner,w)['packets'][0]
    if fault=='missing_packet':c.s.execute('DELETE FROM workstream_packets WHERE id=?',(packet['id'],))
    elif fault=='missing_stdout':
        ref=c.g.evidence_for(packet['id'],packet['digest'],'design')[0]
        rec=c.g.receipt(ref['id']);c.s.blob_path(rec['stdout_blob']).unlink()
    else:
        script=tmp_path/'negative_scope_review.py'
        result={'verdict':'fail' if fault=='later_fail' else 'pass','rationale':'Explicit negative fixture',
                'covered':[],'findings':[],'observations':[{'ref':packet['id'],'detail':'Fixture only.'}],'dispositions':[]}
        if fault=='later_findings':result['findings']=[{'severity':'high','statement':'Missing condition','evidence':'Fixture example'}]
        script.write_text("import json,sys\np=json.load(sys.stdin)\nr="+repr(result)+"\n"+("r['covered']=p['context']['required_coverage']\n" if fault!='wrong_marker' else '')+"print(json.dumps(r))\n")
        c.rt.adapters.register(c.owner,'negative','fixture',sys.executable,[str(script)])
        c.rt.review(c.owner,packet['id'],'design','negative')
    assert not c.workstreams.status(c.owner,w)['current']
    with pytest.raises(Fault):c.workstreams.activate(c.owner,w)


def test_project_scope_permissions_and_normal_conversation_routes(setup):
    from daikibo.supervisor import ALLOWED
    c,p,*_=setup;adopt(setup);w=activate_scope(setup)
    for name in ('propose','get','selection','list','packet','status','activate','completion','finish','withdraw','program_audit'):
        assert 'workstream.'+name in c.routes and 'workstream.'+name in ALLOWED
    with pytest.raises(Fault):c.workstreams.get(Actor('other','agent','OTHER'),w)
    packet=c.workstreams.get(c.owner,w)['packets'][0]['id']
    assert c.jobs.subject_project('review',{'subject':packet})==p
    job=c.jobs.submit(c.owner,'review',{'subject':packet,'role':'design','adapter':'markers'})
    assert job


@pytest.mark.parametrize('offset,limit',[(-1,1),(0,0),(0,201),(True,1),(0,True)])
def test_invalid_pages_rejected(setup,offset,limit):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    for fn in (lambda:c.workstreams.get(c.owner,w,offset,limit),lambda:c.workstreams.selection(c.owner,w,offset=offset,limit=limit),lambda:c.workstreams.list(c.owner,setup[4],offset,limit)):
        with pytest.raises(Fault):fn()


def test_small_review_packets_preserve_all_material(setup):
    c=setup[0];adopt(setup);p=ws_propose(setup,byte_budget=4096);w=p['scope']
    assert p['packet_count']>1
    offset=0;fragments=[]
    while True:
        page=c.workstreams.get(c.owner,w,offset,1)
        for row in page['packets']:
            packet=c.workstreams.packet(c.owner,row['id'])['body']
            assert len(canonical(packet))<=4096
            fragments.append(packet['serialized_fragment'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert json.loads(''.join(fragments))['selection']['tasks']==[setup[6]]
    review_scope(c,w);assert c.workstreams.activate(c.owner,w)['status']=='active'


def test_same_conversation_reports_scoped_work_not_project_completion(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup)
    repo=c.s.one('SELECT path FROM repos WHERE id=?',(r,))['path']
    c.native.attach(c.owner,'scope-conversation',repo,project=p,register_repository=False)
    assert not c.native.completion(c.owner,'scope-conversation',w)['completed']
    finish(c,p,t)
    pending=c.native.completion(c.owner,'scope-conversation',w)
    assert not pending['completed'] and any(x['code']=='workstream_finish_required' for x in pending['blockers'])
    c.workstreams.finish(c.owner,w)
    done=c.native.completion(c.owner,'scope-conversation',w)
    assert done['completed'] and done['completion_kind']=='delegated_work_only' and not done['deploy_ready']
    c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'New facts require reassessment')
    assert c.native.stop_feedback(c.owner,'scope-conversation')['decision']=='block'


def test_scope_events_and_bounded_reads_are_supervisor_progress(setup):
    from daikibo.supervisor import VIEW_METHODS
    c,p,*_=setup;adopt(setup)
    before=c.supervisor.state_digest(p);w=ws_propose(setup)['scope']
    assert before!=c.supervisor.state_digest(p)
    assert {'workstream.selection','workstream.packet','workstream.status'}<=VIEW_METHODS
    before=c.supervisor.state_digest(p);review_scope(c,w);c.workstreams.activate(c.owner,w)
    assert before!=c.supervisor.state_digest(p)
