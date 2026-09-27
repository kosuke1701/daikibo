"""Real fixture subprocesses validate workflow mechanics, not LLM judgment."""
from __future__ import annotations
import copy
import json
import sqlite3
import sys
from pathlib import Path
import pytest
from daikibo.common import Actor, Fault, canonical, digest, parse_json
from conftest import make_task, finish_task


def changed_body(c,task,**changes):
    body={k:v for k,v in c.w.task(c.owner,task)['body'].items() if k!='task_kind'}
    return {**copy.deepcopy(body),'title':'Revised bounded work',**changes}


def dependent(c,project,deps):
    p,r,q,_=project
    task=c.w.create(c.owner,p,{'title':'Dependent work','goal':'Observe predecessor result',
       'read_artifacts':[q], 'write_paths':['derived.py'], 'acceptance':['AC-ADD'],
       'dependencies':deps,'repos':[r],'non_goals':[]})['id']
    c.w.plan_tests(c.owner,task,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],
                                        'kind':'pytest','required_tests':['test_add']}]})
    return task


@pytest.fixture
def revision_setup(full,full_project,tmp_path):
    c=full;task=make_task(c,full_project)
    # Return exact protocol coverage only. This is not an independent human/LLM.
    script=tmp_path/'revision_reviewer.py'
    script.write_text("import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({'verdict':'pass','rationale':'fixed protocol fixture, not semantic judgment','covered':p['context']['required_coverage'],'findings':[],'observations':[{'ref':p['subject'],'detail':'fixture received the proposal'}],'dispositions':[]}))\n")
    c.rt.adapters.register(c.owner,'revision-reviewer','fixture',sys.executable,[str(script)])
    return c,full_project,task


def propose(c,task,**body_changes):
    return c.task_revisions.propose(c.owner,task,c.w.task(c.owner,task)['revision'],
                                    changed_body(c,task,**body_changes),'Correct the implementation approach without changing the requirement')


def review(c,proposal):
    return c.rt.review(c.owner,proposal['id'],'impact','revision-reviewer')


def apply(c,proposal):
    receipt=review(c,proposal)
    return c.task_revisions.apply(c.owner,proposal['id'],proposal['digest'],receipt['receipt'])


def test_propose_review_apply_preserves_before_and_requires_new_tests(revision_setup):
    c,project,task=revision_setup;before=c.task_revisions.snapshot(c.owner,task)
    p=propose(c,task,write_paths=['calc.py','helper.py'])
    assert c.w.task(c.owner,task)==before['task']
    done=apply(c,p);after=c.w.task(c.owner,task)
    assert after['revision']==2 and after['status']=='planned' and after['candidate'] is None
    assert c.s.one('SELECT * FROM plans WHERE task=?',(task,)) is None
    h=c.task_revisions.history_record(c.owner,done['history'])
    assert h['body']['before']==before and h['body']['after']['task']==after
    assert h['body']['before']['test_plan']['body']['checks'][0]['id']=='unit'
    assert h['body']['after']['test_plan'] is None
    assert 'no_approved_test_plan' in c.g.evaluate_task(c.owner,task,'ready')['failures']
    assert c.task_revisions.history(c.owner,task)['complete_since_creation']


def test_new_revision_proposal_requires_actual_impact_review_even_for_owner(revision_setup):
    c,project,task=revision_setup;p=propose(c,task)
    with pytest.raises(Fault) as exc:c.task_revisions.apply(c.owner,p['id'],p['digest'],'not-run')
    assert exc.value.code=='review_required'
    assert c.w.task(c.owner,task)['revision']==1


def test_role_mismatch_is_not_an_impact_review(revision_setup):
    c,_,t=revision_setup;p=propose(c,t)
    with pytest.raises(Fault):c.rt.review(c.owner,p['id'],'spec','revision-reviewer')


def test_apply_replay_is_idempotent_and_does_not_claim_current_completion(revision_setup):
    c,_,t=revision_setup;p=propose(c,t);result=apply(c,p)
    again=c.task_revisions.apply(c.owner,p['id'],p['digest'],result['review_receipt'])
    assert again['replayed'] and again['revision']==2
    assert c.task_revisions.history(c.owner,t)['total']==1
    assert c.w.task(c.owner,t)['status']=='planned'
    with pytest.raises(Fault):c.task_revisions.apply(c.owner,p['id'],p['digest'],'different')


@pytest.mark.parametrize('mutation',['source_revision','test_plan','task_claimed','policy','new_dependent','other_proposal_applied'])
def test_changes_after_proposal_make_impact_review_stale(revision_setup,mutation):
    c,project,t=revision_setup;p=propose(c,t);receipt=review(c,p)
    pid,_,q,_=project
    if mutation=='source_revision':
        art=c.k.artifact(c.owner,q);c.k._revise(c.owner,art,art['revision'],{**art['body'],'statement':'Clarified detail'},'test input change','accepted')
    elif mutation=='test_plan':
        c.w.plan_tests(c.owner,t,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],'kind':'pytest','required_tests':['test_add']}],'rationale':'Reconsidered plan'})
    elif mutation=='task_claimed':c.w.claim(c.owner,pid,t)
    elif mutation=='policy':
        body=c.g.policy(pid)['body'];body['max_parallel']+=1
        c.s.execute('UPDATE policies SET body=?,digest=? WHERE project=?',(canonical(body).decode(),digest(body),pid))
    elif mutation=='new_dependent':dependent(c,project,deps=[t])
    elif mutation=='other_proposal_applied':apply(c,propose(c,t,goal='A different legitimate implementation plan'))
    with pytest.raises(Fault) as exc:c.task_revisions.apply(c.owner,p['id'],p['digest'],receipt['receipt'])
    assert exc.value.code=='stale_task_proposal'


def test_workflow_and_policy_are_reviewable_and_workflow_changes_invalidate(revision_setup):
    c,project,task=revision_setup
    source=c.k.source(c.owner,project[0],'Preserve the complete addition request.')
    flow=c.p.begin(c.owner,project[0],source['id'],compact=True)['program']
    proposal=propose(c,task,workflow_id=flow)
    material=c.task_revisions.get(c.owner,proposal['id'])['body']['material']
    assert material['workflows'][0]['body']['source']==source['id']
    assert material['policy_definition']['body']==c.g.policy(project[0])['body']
    assert 'implementation' in material['workflow_phases']
    receipt=review(c,proposal)
    c.s.execute('UPDATE programs SET revision=revision+1 WHERE id=?',(flow,))
    with pytest.raises(Fault) as exc:
        c.task_revisions.apply(c.owner,proposal['id'],proposal['digest'],receipt['receipt'])
    assert exc.value.code=='stale_task_proposal'


def test_descendants_only_are_reassessed_and_counters_not_reset(revision_setup):
    c,project,t=revision_setup;child=dependent(c,project,deps=[t]);grandchild=dependent(c,project,deps=[child]);unrelated=make_task(c,project)
    unchanged=c.w.task(c.owner,unrelated)
    c.s.execute('UPDATE tasks SET attempts=2 WHERE id=?',(t,))
    p=propose(c,t);result=apply(c,p)
    assert set(result['affected_tasks'])=={child,grandchild}
    assert c.w.task(c.owner,t)['attempts']==2
    for dep in (child,grandchild):
        current=c.w.task(c.owner,dep)
        assert current['validity']=='needs_review' and any(b['kind']=='changed_task_definition' for b in current['blocks'])
    assert c.w.task(c.owner,unrelated)==unchanged
    c.w.replan(c.owner,child,1,'Refresh dependent after parent design revision')
    assert not any(b['kind']=='changed_task_definition' for b in c.w.task(c.owner,child)['blocks'])
    assert any('dependency:' in s for s in c.g.evaluate_task(c.owner,child,'ready')['failures'])


def test_reviewed_dependency_change_installs_real_graph_edges(revision_setup):
    c,project,t=revision_setup;independent=make_task(c,project)
    p=propose(c,t,dependencies=[independent]);apply(c,p)
    assert c.s.all('SELECT dependency FROM task_deps WHERE task=?',(t,))==[{'dependency':independent}]
    assert 'dependency:'+independent in c.g.evaluate_task(c.owner,t,'ready')['failures']


@pytest.mark.parametrize('cycle',['self','child','grandchild'])
def test_cycle_cannot_be_installed(revision_setup,cycle):
    c,project,t=revision_setup;child=dependent(c,project,deps=[t]);grand=dependent(c,project,deps=[child])
    dep={'self':t,'child':child,'grandchild':grand}[cycle]
    with pytest.raises(Fault) as exc:propose(c,t,dependencies=[dep])
    assert exc.value.code=='dependency_cycle'
    assert c.w.task(c.owner,t)['body']['dependencies']==[]


def test_cancelled_dependency_is_not_a_valid_replan(revision_setup):
    c,project,t=revision_setup;cancelled=make_task(c,project);c.w.cancel(c.owner,cancelled,'Withdraw this task')
    with pytest.raises(Fault) as exc:propose(c,t,dependencies=[cancelled])
    assert exc.value.code=='cancelled_dependency'


def test_risk_cannot_be_silently_lowered(revision_setup):
    c,_,t=revision_setup
    with pytest.raises(Fault) as exc:propose(c,t,risk='lite')
    assert exc.value.code=='risk_downgrade'


def test_cross_project_revision_ref_is_rejected(revision_setup):
    c,_,t=revision_setup;other=c.k.create_project(c.owner,'Other')['id']
    q=c.k.propose(c.owner,other,'requirement',{'title':'Other','statement':'Other','acceptance':['AC-ADD']})
    c.k.accept(c.owner,q['id'],1)
    with pytest.raises(Fault) as exc:propose(c,t,read_artifacts=[q['id']])
    assert exc.value.code=='cross_project'


def test_withdrawn_or_applied_proposals_are_not_deleted(revision_setup):
    c,_,t=revision_setup;p=propose(c,t)
    c.task_revisions.withdraw(c.owner,p['id'],p['digest'],'No longer the preferred approach')
    assert c.task_revisions.get(c.owner,p['id'])['status']=='withdrawn'
    with pytest.raises(Fault):apply(c,p)
    assert c.w.task(c.owner,t)['revision']==1
    applied=propose(c,t);apply(c,applied)
    with pytest.raises(Fault):c.task_revisions.withdraw(c.owner,applied['id'],applied['digest'],'Cannot erase an applied revision')


def test_replan_also_retains_frozen_plan_and_actual_candidate(revision_setup):
    c,project,t=revision_setup;finish_task(c,project[0],t)
    before=c.task_revisions.snapshot(c.owner,t)
    c.w.replan(c.owner,t,1,'New analysis without rewriting the old definition')
    page=c.task_revisions.history(c.owner,t)
    history=c.task_revisions.history_record(c.owner,page['records'][0]['id'])['body']
    assert history['before']==before and history['before']['task']['candidate']
    assert c.s.one('SELECT id FROM candidates WHERE id=?',(before['task']['candidate'],))
    candidate=parse_json(c.s.one('SELECT body FROM candidates WHERE id=?',(before['task']['candidate'],))['body'])
    assert c.g.receipt(candidate['implementation_receipt'])['role']=='implementer' 
    assert history['after']['task']['candidate'] is None
    assert c.w.task(c.owner,t)['attempts']==before['task']['attempts']


def test_legacy_missing_history_is_disclosed_not_synthesized(revision_setup):
    c,_,t=revision_setup;c.s.execute('UPDATE tasks SET revision=4 WHERE id=?',(t,))
    c.w.replan(c.owner,t,4,'First recorded post-upgrade change')
    result=c.task_revisions.history(c.owner,t)
    assert result['history_start_revision']==4 and not result['complete_since_creation']
    assert result['total']==1 and result['legacy_history_synthesized'] is False


def test_history_pages_are_bound_to_one_snapshot(revision_setup):
    c,_,t=revision_setup
    c.w.replan(c.owner,t,1,'First');c.w.replan(c.owner,t,2,'Second')
    page=c.task_revisions.history(c.owner,t,limit=1)
    assert page['next_offset']==1
    next_page=c.task_revisions.history(c.owner,t,1,1,page['snapshot'])
    assert next_page['records'][0]['to_revision']==3
    c.w.replan(c.owner,t,3,'Third')
    with pytest.raises(Fault) as exc:c.task_revisions.history(c.owner,t,1,1,page['snapshot'])
    assert exc.value.code=='stale_history'


def test_revision_records_immutable_at_normal_database_layer(revision_setup):
    c,_,t=revision_setup;p=propose(c,t);result=apply(c,p)
    for sql,args in [('UPDATE task_revision_history SET body=? WHERE id=?',('{}',result['history'])),
                     ('DELETE FROM task_revision_history WHERE id=?',(result['history'],)),
                     ('UPDATE task_revision_proposals SET body=? WHERE id=?',('{}',p['id']))]:
        with pytest.raises(sqlite3.IntegrityError):c.s.execute(sql,args)


def test_failed_apply_rolls_back_task_and_all_dependents(revision_setup,monkeypatch):
    c,project,t=revision_setup;child=dependent(c,project,deps=[t]);p=propose(c,t);receipt=review(c,p)
    before=c.task_revisions.snapshot(c.owner,t);old_child=c.w.task(c.owner,child)
    def broken(*args,**kwargs):raise RuntimeError('injected storage failure before journaling')
    monkeypatch.setattr(c.task_revisions,'_record',broken)
    with pytest.raises(RuntimeError):c.task_revisions.apply(c.owner,p['id'],p['digest'],receipt['receipt'])
    assert c.task_revisions.snapshot(c.owner,t)==before and c.w.task(c.owner,child)==old_child
    assert c.task_revisions.get(c.owner,p['id'])['status']=='proposed'


def test_methods_are_available_to_same_conversation_agent(revision_setup,tmp_path):
    c,project,t=revision_setup;c.native.attach(c.owner,'revision-session',str(project[3]),project=project[0],register_repository=False)
    p=propose(c,t)
    result=c.native.actions(c.owner,'revision-session',[{'method':'task.revision_get','params':{'proposal':p['id']}}])
    assert result['all_applied']
    assert all('error' not in action for action in result['actions'])
    from daikibo.supervisor import ALLOWED
    assert {'task.propose_revision','task.apply_revision','task.revision_history'}<=ALLOWED


@pytest.mark.parametrize('verdict,coverage,findings', [
    ('fail',True,[]),('blocked',True,[]),('pass',False,[]),
    ('pass',True,[{'severity':'warning','message':'Unresolved consequence','ref':'proposal'}]),
])
def test_later_nonpass_or_incomplete_review_never_reuses_old_pass(revision_setup,tmp_path,verdict,coverage,findings):
    c,_,t=revision_setup;p=propose(c,t);old=review(c,p)
    script=tmp_path/'later_review.py'
    script.write_text('import json,sys\np=json.load(sys.stdin)\nprint(json.dumps('+repr({
       'verdict':verdict,'rationale':'fixed negative protocol fixture, not LLM judgment',
       'covered':[], 'findings':findings,'observations':[{'ref':'proposal','detail':'recorded fixture input'}],
       'dispositions':[]})+" | {'covered':p['context']['required_coverage'] if "+repr(coverage)+" else []}))\n")
    c.rt.adapters.register(c.owner,'later-review','fixture',sys.executable,[str(script)])
    newest=c.rt.review(c.owner,p['id'],'impact','later-review')
    with pytest.raises(Fault):c.task_revisions.apply(c.owner,p['id'],p['digest'],old['receipt'])
    with pytest.raises(Fault):c.task_revisions.apply(c.owner,p['id'],p['digest'],newest['receipt'])
    assert c.w.task(c.owner,t)['revision']==1


def test_exact_proposal_receipt_cannot_authorize_another_definition(revision_setup):
    c,_,t=revision_setup;one=propose(c,t);two=propose(c,t,goal='A distinct proposal')
    receipt=review(c,one)
    with pytest.raises(Fault):c.task_revisions.apply(c.owner,two['id'],two['digest'],receipt['receipt'])
    assert c.w.task(c.owner,t)['revision']==1


def test_large_revision_is_rejected_before_creating_unreviewable_proposal(revision_setup):
    c,project,t=revision_setup
    from test_reviewed_breakdowns import accepted
    huge=accepted(c,project[0],'design','Large requirement context',details='x'*750000)
    refs=c.w.task(c.owner,t)['body']['read_artifacts']+[huge]
    with pytest.raises(Fault) as exc:propose(c,t,read_artifacts=refs)
    assert exc.value.code=='revision_context_too_large'
    assert not c.s.all('SELECT id FROM task_revision_proposals')


def test_unrelated_task_changes_do_not_stale_local_revision(revision_setup):
    c,project,t=revision_setup;other=make_task(c,project);p=propose(c,t);receipt=review(c,p)
    c.w.replan(c.owner,other,1,'Independent work, no dependency')
    result=c.task_revisions.apply(c.owner,p['id'],p['digest'],receipt['receipt'])
    assert result['revision']==2 and not result['affected_tasks']


def test_completed_work_can_be_revised_and_completed_only_with_new_execution(revision_setup):
    c,project,t=revision_setup
    first=finish_task(c,project[0],t)
    old_candidate=first['candidate']
    old_binding=c.g.task_binding(t)
    p=propose(c,t,title='Recheck same behavior after a task-definition correction')
    revised=apply(c,p)
    assert 'no_approved_test_plan' in c.g.evaluate_task(c.owner,t,'ready')['failures']
    with pytest.raises(Fault):c.w.complete(c.owner,t,2)
    c.w.plan_tests(c.owner,t,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],
                                        'kind':'pytest','required_tests':['test_add']}]})
    c.w.ready(c.owner,t)
    result=finish_task(c,project[0],t)
    assert result['status']=='completed' and result['revision']==2
    assert c.g.task_binding(t)!=old_binding
    history=c.task_revisions.history_record(c.owner,revised['history'])
    assert history['body']['before']['task']['candidate']==old_candidate
    assert history['body']['before']['task']['status']=='completed'
    assert history['body']['after']['task']['status']=='planned'
    assert c.g.evaluate_task(c.owner,t,'recheck')['verdict']=='pass'


def test_legacy_ambiguous_task_is_repairable_by_reviewed_explicit_references(revision_setup):
    c,project,t=revision_setup;p,_,q,_=project
    from test_reviewed_breakdowns import accepted
    second=accepted(c,p,'requirement','Different meaning; same condition label',acceptance=['AC-ADD'])
    body=c.w.task(c.owner,t)['body'];body['read_artifacts'].append(second)
    art=c.k.artifact(c.owner,second)
    with c.s.transaction():
        c.s.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(body).decode(),t))
        c.s.execute('INSERT INTO task_reads VALUES(?,?,?,?)',(t,second,art['revision'],art['digest']))
    assert 'acceptance_identity:ambiguous_acceptance' in c.g.evaluate_task(c.owner,t,'ready')['failures']
    proposal=propose(c,t,acceptance_refs=[{'requirement':q,'acceptance':'AC-ADD'}])
    revised=apply(c,proposal)
    from daikibo.obligations import from_store
    assert from_store(c.s,c.w.task(c.owner,t)['body'])['pairs']=={(q,'AC-ADD')}
    historic=c.task_revisions.history_record(c.owner,revised['history'])
    assert 'acceptance_refs' not in historic['body']['before']['task']['body']
    assert any(x['requirement']==second and 'acceptance_task:AC-ADD' in x['missing'] for x in c.k.trace(c.owner,p)['missing'])


def test_replan_cannot_clear_unresolved_product_decision(revision_setup):
    c,_,t=revision_setup
    c.s.execute('INSERT INTO blocks VALUES(?,?,?,?)',(t,'decision','DEC-PENDING','Unresolved product choice'))
    revised=apply(c,propose(c,t))
    assert c.w.task(c.owner,t)['revision']==2
    assert any(x['kind']=='decision' for x in c.w.task(c.owner,t)['blocks'])
    assert c.g.evaluate_task(c.owner,t,'ready')['verdict']=='fail'
