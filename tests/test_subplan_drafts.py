"""Real process fixtures validate protocols, not semantic architecture quality."""
import copy
import json
import sys
from pathlib import Path
import pytest

from daikibo.common import Actor, Fault, canonical, digest, parse_json
from daikibo.subplans import pairs, unit_id
from test_reviewed_breakdowns import setup, accepted, make_work, make_domain, leaf, parent, review_all, packets


def local(units):
    return [{k:v for k,v in u.items() if k!='dependencies'} for u in units]


def draft(s,*,units=None,obligations=None,children=None,context=None,budget=24000,title='Partial architecture'):
    c,p,r,q,program,d,t,default=s
    return c.subplans.propose(c.owner,program,title,'Preserve original requirements and inspect partial boundaries',
                             obligations if obligations is not None else [{'requirement':q,'acceptance':'AC-ADD'}],
                             local(default) if units is None else units,children,context,budget)


def all_packets(c,ident):
    offset=0;result=[]
    while True:
        page=c.subplans.get(c.owner,ident,offset,2);result.extend(page['packets'])
        if page['next_offset'] is None:return result
        offset=page['next_offset']


def prepare_draft_admission(c, ident, *, requirements, task_ids, recursive=False):
    """Explicitly prepare the profile and selected Task-plan evidence.

    Packet review is intentionally separate.  Callers must name both the
    requirement roots used for a new-program profile and the exact current
    Task plans whose receipts are needed.  This keeps a missing/stale Task
    negative from being repaired by an unrelated packet-review helper.
    """
    from test_reviewed_breakdowns import _bootstrap_mandatory_profile, _review_current_task_plans
    row = c.s.one("SELECT project,program FROM subplans WHERE id=?", (ident,), True)
    project, program = row["project"], row["program"]
    if c.assurance.selected_profile(c.owner, project, program).get("profile_ref") is None:
        _bootstrap_mandatory_profile(
            c, project, program, list(requirements), list(task_ids),
        )
    else:
        _review_current_task_plans(c, project, "markers", task_ids=list(task_ids))
    if recursive:
        for child in c.subplans.get(c.owner,ident)['children']:
            prepare_draft_admission(
                c, child['id'], requirements=requirements, task_ids=task_ids,
                recursive=True,
            )


def review_draft(c, ident, recursive=False):
    """Review only the retained design/trace packet population."""
    if recursive:
        for child in c.subplans.get(c.owner, ident)['children']:
            review_draft(c, child['id'], True)
    for packet in all_packets(c,ident):
        for role in ('design','trace'):c.rt.review(c.owner,packet['id'],role,'markers')


def second(s,dependency=False,shared_label=False):
    c,p,r,q,program,d,t,units=s
    ac='AC-ADD' if shared_label else 'AC-SECOND'
    q2=accepted(c,p,'requirement','Another outcome',acceptance=[ac])
    t2=make_work(c,p,r,q2,d,deps=[t] if dependency else (),acs=[ac])
    return q2,t2,ac


def test_before_any_root_exists_partial_design_is_not_adoption(setup):
    c=setup[0];value=draft(setup)
    assert c.breakdowns.active(c.owner,setup[4]) is None
    assert value['state']=='partial_design_proposal' and not value['deploy_ready']
    assert not c.subplans.audit(c.owner,value['id'])['current']
    with pytest.raises(Fault,match='All current child'):c.subplans.compose(c.owner,value['id'])
    assert c.s.one('SELECT count(*) AS n FROM breakdowns')['n']==0


def test_recursive_packet_review_does_not_prepare_profile_or_task_plans(setup):
    c,p,r,q,program,d,t,units=setup
    child=draft(setup)
    root=draft(setup,units=[],children=[child['id']])
    before_selection=c.assurance.selected_profile(c.owner,p,program)
    before_plans=[row['id'] for row in c.s.all(
        "SELECT id FROM receipts WHERE subject=? AND role='test_plan' ORDER BY id",
        (t,),
    )]

    review_draft(c,root['id'],True)

    after_selection=c.assurance.selected_profile(c.owner,p,program)
    after_plans=[row['id'] for row in c.s.all(
        "SELECT id FROM receipts WHERE subject=? AND role='test_plan' ORDER BY id",
        (t,),
    )]
    assert before_selection.get('profile_ref') is None
    assert after_selection.get('profile_ref') is None
    assert after_plans == before_plans == []
    assert c.s.one(
        "SELECT count(*) AS n FROM receipts WHERE role IN ('design','trace')",
    )['n'] > 0


def test_complete_design_composes_into_unreviewed_root_only(setup):
    c=setup[0];value=draft(setup)
    prepare_draft_admission(c,value['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,value['id'])
    assert c.subplans.audit(c.owner,value['id'])['current']
    result=c.subplans.compose(c.owner,value['id'])
    assert not result['root_adopted'] and not result['deploy_ready']
    assert c.breakdowns.active(c.owner,setup[4]) is None
    with pytest.raises(Fault):c.breakdowns.activate(c.owner,result['breakdown'])
    review_all(c,result['breakdown']);c.breakdowns.activate(c.owner,result['breakdown'])
    assert c.breakdowns.active(c.owner,setup[4])['id']==result['breakdown']
    assert not c.lifecycle.completion(c.owner,setup[4])['completed']
    replay=c.subplans.compose(c.owner,value['id']);assert replay['replayed']
    assert c.s.one('SELECT count(*) AS n FROM tasks')['n']==1
    assert c.s.one('SELECT count(*) AS n FROM subplan_compositions')['n']==1


def test_parent_combines_children_and_keeps_residual_obligations(setup):
    c,p,r,q,program,d,t,units=setup;q2,t2,ac=second(setup)
    first=draft(setup)
    coverage=c.subplans.coverage(c.owner,first['id'])
    assert coverage['unassigned_obligation_count']==1 and coverage['unassigned_tasks']==[t2]
    review_draft(c,first['id'])
    with pytest.raises(Fault):c.subplans.compose(c.owner,first['id'])
    assert not c.s.one('SELECT 1 FROM breakdowns')
    other=draft(setup,units=local([leaf('second',d,[t2],q2,acs=[ac])]),obligations=[{'requirement':q2,'acceptance':ac}])
    root=draft(setup,units=[],children=[first['id'],other['id']],obligations=[{'requirement':q,'acceptance':'AC-ADD'},{'requirement':q2,'acceptance':ac}])
    # Prepare the complete canonical population once at the parent boundary;
    # packet reviews for the children remain independently scoped.
    prepare_draft_admission(c,root['id'],requirements=[q,q2],task_ids=[t,t2])
    review_draft(c,other['id']);review_draft(c,root['id'])
    result=c.subplans.compose(c.owner,root['id'])
    assert c.breakdowns._row(c.owner,result['breakdown'])['body']['structure']['task_count']==2
    assert c.subplans.coverage(c.owner,root['id'])['unassigned_obligation_count']==0


def test_partial_acceptance_is_not_the_parent_requirement(setup):
    c,p,r,q,program,d,t,units=setup
    q2=accepted(c,p,'requirement','Parent explicit condition',acceptance=['PARENT'])
    root=draft(setup)
    prepare_draft_admission(c,root['id'],requirements=[q],task_ids=[t])
    review_draft(c,root['id'])
    report=c.subplans.coverage(c.owner,root['id'])
    assert report['unassigned_obligations']==[{'requirement':q2,'acceptance':'PARENT'}]
    with pytest.raises(Fault) as exc:c.subplans.compose(c.owner,root['id'])
    assert exc.value.code=='acceptance_scope_mismatch'


def test_drafts_kept_canonical_until_normal_acceptance(setup):
    c,p,r,q,program,d,t,units=setup
    design=c.k.propose(c.owner,p,'design',{'title':'Draft algorithm','statement':'A proposed algorithm'})
    value=draft(setup,context=[design['id']])
    prepare_draft_admission(c,value['id'],requirements=[q],task_ids=[t])
    review_draft(c,value['id'])
    assert c.subplans.audit(c.owner,value['id'])['current']
    with pytest.raises(Fault) as exc:c.subplans.compose(c.owner,value['id'])
    assert exc.value.code=='unaccepted_input'
    assert c.k.artifact(c.owner,design['id'])['status']=='draft'
    c.k.accept(c.owner,design['id'],1)
    assert c.subplans.audit(c.owner,value['id'])['current']
    assert c.subplans.compose(c.owner,value['id'])['root_adopted'] is False


def test_draft_requirements_and_tasks_can_be_planned_independently(setup):
    c,p,r,q,program,d,t,units=setup
    q2=c.k.propose(c.owner,p,'requirement',{'title':'Derived condition','statement':'Preserve edge case','acceptance':['NEW']})['id']
    t2=make_work(c,p,r,q2,d,acs=['NEW'])
    # No automatic substitution for q / AC-ADD.
    part=draft(setup,units=local([leaf('derived',d,[t2],q2,acs=['NEW'])]),obligations=[{'requirement':q2,'acceptance':'NEW'}])
    assert c.k.artifact(c.owner,q2)['status']=='draft'
    assert c.subplans.coverage(c.owner,part['id'])['draft_or_not_current_pair_count']==1
    prepare_draft_admission(c,part['id'],requirements=[q],task_ids=[t2])
    review_draft(c,part['id'])
    with pytest.raises(Fault):c.subplans.compose(c.owner,part['id'])
    c.k.accept(c.owner,q2,1)
    original=draft(setup)
    combined=draft(setup,units=[],children=[part['id'],original['id']],obligations=[{'requirement':q,'acceptance':'AC-ADD'},{'requirement':q2,'acceptance':'NEW'}])
    prepare_draft_admission(c,combined['id'],requirements=[q,q2],task_ids=[t,t2],recursive=True)
    review_draft(c,combined['id'],True)
    assert c.subplans.compose(c.owner,combined['id'])['breakdown']


@pytest.mark.parametrize('edit',['draft','task','policy','global','external'])
def test_current_input_changes_cannot_use_old_review(setup,edit):
    c,p,r,q,program,d,t,units=setup
    extra=c.k.propose(c.owner,p,'design',{'title':'Draft','statement':'V1'})['id']
    if edit=='external':
        q2,t2,ac=second(setup,dependency=True)
        value=draft(setup,units=local([leaf('second',d,[t2],q2,acs=[ac])]),obligations=[{'requirement':q2,'acceptance':ac}])
    else:value=draft(setup,context=[extra])
    if edit=='external':
        prepare_draft_admission(c,value['id'],requirements=[q,q2],task_ids=[t,t2])
    else:
        prepare_draft_admission(c,value['id'],requirements=[q],task_ids=[t])
    review_draft(c,value['id'])
    if edit=='draft':c.k.revise(c.owner,extra,1,{'title':'Draft','statement':'V2'},'New evidence')
    elif edit in {'task','external'}:c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'Changed task definition inputs')
    elif edit=='global':accepted(c,p,'assumption','New global restriction',constraints={'maximum':5})
    else:
        row=c.s.one('SELECT * FROM policies WHERE project=?',(p,));body=parse_json(row['body']);body['revision_note']='test changed'
        c.s.execute('UPDATE policies SET body=?,digest=? WHERE project=?',(canonical(body).decode(),digest(body),p))
    assert not c.subplans.audit(c.owner,value['id'])['current']
    with pytest.raises(Fault):c.subplans.compose(c.owner,value['id'])
    with pytest.raises(Fault):c.rt.review(c.owner,all_packets(c,value['id'])[0]['id'],'design','markers')


def test_unrelated_sibling_artifact_does_not_stale_leaf(setup):
    c,p,r,q,program,d,t,units=setup;value=draft(setup)
    prepare_draft_admission(c,value['id'],requirements=[q],task_ids=[t])
    review_draft(c,value['id'])
    extra=c.k.propose(c.owner,p,'design',{'title':'Sibling','statement':'Independent'})
    c.k.revise(c.owner,extra['id'],1,{'title':'Sibling','statement':'Different'},'Independent technical detail')
    assert c.subplans.audit(c.owner,value['id'])['current']


def test_exact_acceptance_ids_across_siblings(setup):
    c,p,r,q,program,d,t,units=setup;q2,t2,ac=second(setup,shared_label=True)
    a=draft(setup);b=draft(setup,units=local([leaf('another',d,[t2],q2)]),obligations=[{'requirement':q2,'acceptance':ac}])
    with pytest.raises(Fault):draft(setup,units=[],children=[a['id'],b['id']])
    root=draft(setup,units=[],children=[a['id'],b['id']],obligations=[{'requirement':q,'acceptance':ac},{'requirement':q2,'acceptance':ac}])
    assert root['obligation_count']==2


def test_missing_child_review_blocks_parent_composition(setup):
    c=setup[0];a=draft(setup);root=draft(setup,units=[],children=[a['id']])
    prepare_draft_admission(c,root['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,root['id'])
    assert not c.subplans.audit(c.owner,root['id'])['current']
    with pytest.raises(Fault):c.subplans.compose(c.owner,root['id'])
    review_draft(c,a['id']);assert c.subplans.compose(c.owner,root['id'])['breakdown']


@pytest.mark.parametrize('shape',['direct','diamond','task','obligation'])
def test_duplicate_children_and_assignments_rejected(setup,shape):
    c=setup[0];a=draft(setup)
    with pytest.raises(Fault):
        if shape=='direct':draft(setup,units=[],children=[a['id'],a['id']])
        elif shape=='diamond':
            b=draft(setup,units=[],children=[a['id']]);d=draft(setup,units=[],children=[a['id']]);draft(setup,units=[],children=[b['id'],d['id']])
        elif shape=='task':draft(setup,children=[a['id']])
        else:
            c0,p,r,q,prog,dom,t,units=setup
            t2=make_work(c,p,r,q,dom)
            draft(setup,units=local([leaf('also',dom,[t2],q)]),children=[a['id']])


def test_external_prerequisite_cannot_disappear_at_composition(setup):
    c,p,r,q,program,d,t,units=setup;q2,t2,ac=second(setup,dependency=True)
    sub=draft(setup,units=local([leaf('dependent',d,[t2],q2,acs=[ac])]),obligations=[{'requirement':q2,'acceptance':ac}])
    row=c.subplans._row(c.owner,sub['id']);assert row['body']['structure']['external_dependencies'][0]['dependency']==t
    review_draft(c,sub['id'])
    with pytest.raises(Fault):c.subplans.compose(c.owner,sub['id'])
    first=draft(setup);root=draft(setup,units=[],children=[first['id'],sub['id']],obligations=[{'requirement':q,'acceptance':'AC-ADD'},{'requirement':q2,'acceptance':ac}])
    prepare_draft_admission(c,root['id'],requirements=[q,q2],task_ids=[t,t2])
    review_draft(c,root['id'],True);out=c.subplans.compose(c.owner,root['id'])
    body=c.breakdowns._row(c.owner,out['breakdown'])['body']
    assert sum(len(u['dependencies']) for u in body['units'])==1


def test_new_accepted_requirement_invalidates_full_coverage_not_unrelated_review(setup):
    c,p,r,q,program,d,t,units=setup;value=draft(setup)
    prepare_draft_admission(c,value['id'],requirements=[q],task_ids=[t])
    review_draft(c,value['id'])
    accepted(c,p,'requirement','Additional product requirement',acceptance=['NEW'])
    assert c.subplans.audit(c.owner,value['id'])['current']
    with pytest.raises(Fault) as exc:c.subplans.compose(c.owner,value['id'])
    assert exc.value.code=='acceptance_scope_mismatch'


def test_invalid_composition_is_atomic(setup):
    c,p,r,q,program,d,t,units=setup;q2,t2,ac=second(setup)
    value=draft(setup)
    prepare_draft_admission(c,value['id'],requirements=[q,q2],task_ids=[t,t2])
    review_draft(c,value['id'])
    before={table:c.s.one(f'SELECT count(*) AS n FROM {table}')['n'] for table in ('tasks','breakdowns','breakdown_packets','subplan_compositions')}
    with pytest.raises(Fault):c.subplans.compose(c.owner,value['id'])
    assert before=={table:c.s.one(f'SELECT count(*) AS n FROM {table}')['n'] for table in before}


def test_governed_mode_rejects_fixture_partial_reviews(setup):
    c=setup[0];value=draft(setup)
    prepare_draft_admission(c,value['id'],requirements=[setup[3]],task_ids=[setup[6]])
    review_draft(c,value['id'])
    c.g.mode='governed'
    assert not c.subplans.audit(c.owner,value['id'])['current']
    with pytest.raises(Fault):c.subplans.compose(c.owner,value['id'])
