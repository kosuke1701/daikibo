"""REQ-012/029/063: exact consumers and test designs, not full standards parsing.

Reviewers are real subprocess protocol fixtures, never semantic reviewers.
"""
import copy
import pytest
from daikibo.common import Fault, canonical, digest
from test_reviewed_breakdowns import accepted


def interface(c,p,consumers=()):
    return accepted(c,p,'interface','Query API',input='No input',output='An integer',
                    authentication='none',errors=[],idempotency='Read only',
                    compatibility='Keep old consumers',consumers=list(consumers),verification=['declared check'])


@pytest.fixture
def change_case(full,full_project):
    c=full;p,r,q,root=full_project
    consumer=accepted(c,p,'component','Consumer',expected_unit='seconds')
    i=interface(c,p,[consumer])
    check=accepted(c,p,'test','Consumer check',cases=['unit remains seconds'])
    c.k.link(c.owner,consumer,i,'consumes','asserted','Calls this exact API')
    source=c.k.source(c.owner,p,'Document an unchanged unit more precisely')
    body=copy.deepcopy(c.k.artifact(c.owner,i)['body'])
    body['statement']='Return a nonnegative integer number of seconds'
    body['change_control']={'consumer_impact':{consumer:'Same units; run consumer acceptance'},
                            'verification_ids':[check], 'repository_order':[r],
                            'migration':'No data migration; retain seconds'}
    delta={'artifact':i,'expected_revision':1,'body':body}
    ch=c.p.change(c.owner,p,{'title':'Units clarification','origin':'design','reason':'Existing contract clarification',
                            'affected':[i],'evidence':[source['id']],'deltas':[delta]})
    return c,p,r,i,consumer,check,ch,delta


def review(c,ch):
    return c.rt.review(c.owner,ch['id'],'consistency','fixture')


def revise(c,ident,**fields):
    a=c.k.artifact(c.owner,ident)
    with c.s.transaction():
        c.k._revise(c.owner,a,a['revision'],{**a['body'],**fields},'Fixture concurrent accepted change','accepted')


def assert_stale(c,ch,ev):
    with pytest.raises(Fault):
        c.g.require_review(ev['receipt'],ch['id'],c.p.change_binding(ch['id']),{'consistency'})


def test_reviewer_receives_bound_current_consumer_and_test_bodies(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case
    _,binding,_,context,_=c.rt._subject(c.owner,ch['id'],'consistency',None)
    assert binding==c.p.change_binding(ch['id'])
    impact=context['interface_impact'][0]
    assert impact['registered_consumer_links'][0]['source']==consumer
    assert {x['id'] for x in impact['records']}=={consumer,check}
    assert impact['unknown_consumers_possible'] and not impact['semantic_compatibility_proven']
    ev=review(c,ch)
    assert c.g.require_review(ev['receipt'],ch['id'],binding,{'consistency'})


@pytest.mark.parametrize('target', ['consumer','test'])
def test_dependency_definition_change_invalidates_interface_review(change_case,target):
    c,p,r,i,consumer,check,ch,delta=change_case;ev=review(c,ch)
    revise(c,consumer if target=='consumer' else check,statement='Changed expectation without changing interface')
    assert c.k.artifact(c.owner,i)['revision']==1
    assert_stale(c,ch,ev)


def test_new_consumer_link_invalidates_review_without_bumping_interface(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case;ev=review(c,ch)
    other=accepted(c,p,'component','Another consumer')
    c.k.link(c.owner,other,i,'consumes','inferred','Discovered after review')
    assert_stale(c,ch,ev)
    with pytest.raises(Fault) as exc:c.p.validate_deltas(c.owner,p,[delta])
    assert exc.value.code=='consumer_impact_required'


def test_unlinked_declared_consumer_cannot_disappear_from_impact_plan(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case
    new=copy.deepcopy(delta);new['body']['consumers'].append('external-legacy-app')
    with pytest.raises(Fault) as exc:c.p.validate_deltas(c.owner,p,[new])
    assert exc.value.code=='consumer_impact_required'
    new['body']['change_control']['consumer_impact']['external-legacy-app']='Unknown deployment; hold until checked'
    assert c.p.validate_deltas(c.owner,p,[new])=={i}


@pytest.mark.parametrize('value', ['', ' ', None, {}, []])
def test_consumer_disposition_must_have_an_actual_rationale(change_case,value):
    c,p,r,i,consumer,check,ch,delta=change_case
    new=copy.deepcopy(delta);new['body']['change_control']['consumer_impact'][consumer]=value
    with pytest.raises(Fault):c.p.validate_deltas(c.owner,p,[new])


def test_declared_external_names_remain_unknown_not_invented_local_definitions(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case
    new=copy.deepcopy(delta);new['body']['consumers'].append('third-party')
    new['body']['change_control']['consumer_impact']['third-party']='Need external confirmation'
    c.p.set_delta(c.owner,ch['id'],1,[new],'Record external consumer')
    _,material=c.p.change_review_material(ch['id'])
    assert material['interface_impact'][0]['unresolved_names']==['third-party']


def test_changed_trace_basis_invalidates_same_artifact_review(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case;ev=review(c,ch)
    c.k.link(c.owner,consumer,i,'consumes','asserted','New discovered usage evidence')
    assert_stale(c,ch,ev)


def test_new_verification_link_invalidates_review(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case;ev=review(c,ch)
    c.k.link(c.owner,check,i,'verifies','asserted','Consumer asserts the same output units')
    assert_stale(c,ch,ev)


def test_unrelated_change_does_not_stale_interface_review(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case;ev=review(c,ch)
    a=accepted(c,p,'component','Unrelated');b=accepted(c,p,'test','Unrelated check')
    c.k.link(c.owner,b,a,'verifies','asserted','Independent area')
    assert c.g.require_review(ev['receipt'],ch['id'],c.p.change_binding(ch['id']),{'consistency'})


def test_withdrawn_contract_test_cannot_be_a_verification_plan(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case
    a=c.k.artifact(c.owner,check)
    with c.s.transaction():c.k._revise(c.owner,a,1,a['body'],'Fixture retired check','withdrawn')
    with pytest.raises(Fault) as exc:c.p.validate_deltas(c.owner,p,[delta])
    assert exc.value.code=='contract_test_required'


def test_apply_rechecks_the_same_consumer_snapshot(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case
    ev=review(c,ch)
    c.p.attempt(c.owner,ch['id'],'local_repair',{'hypothesis':'Keep seconds','alternatives':['Document semantics'],
        'evidence':[ev['receipt']], 'outcome':'solution','remaining_unknown':''})
    final=review(c,ch)
    revise(c,consumer,statement='A consumer now interprets milliseconds')
    with pytest.raises(Fault):c.p.apply_technical_change(c.owner,ch['id'],final['receipt'])
    assert c.k.artifact(c.owner,i)['revision']==1
    c.p.set_delta(c.owner,ch['id'],c.p.change_get(c.owner,ch['id'])['revision'],
                  [delta],'Refresh the controller impact fence after consumer revision')
    new=review(c,ch);out=c.p.apply_technical_change(c.owner,ch['id'],new['receipt'])
    assert out['stage']=='ready_for_reimplementation' and out['reassessment_required']
    assert c.k.artifact(c.owner,i)['revision']==2


def test_unknown_or_cross_project_verifier_is_rejected(change_case):
    c,p,r,i,consumer,check,ch,delta=change_case
    other=c.k.create_project(c.owner,'Other project')['id']
    t=accepted(c,other,'test','Not this project')
    new=copy.deepcopy(delta);new['body']['change_control']['verification_ids']=[t]
    with pytest.raises(Fault):c.p.validate_deltas(c.owner,p,[new])


@pytest.mark.parametrize('value', [None, 'consumer-as-prose', 12, [{}], [''], ['duplicate','duplicate']])
@pytest.mark.parametrize('where', ['proposal','current'])
def test_malformed_consumer_inventory_returns_actionable_fault(change_case,value,where):
    c,p,r,i,consumer,check,ch,delta=change_case
    if where=='proposal':
        new=copy.deepcopy(delta);new['body']['consumers']=value
        with pytest.raises(Fault) as exc:c.p.validate_deltas(c.owner,p,[new])
    else:
        revise(c,i,consumers=value)
        with pytest.raises(Fault) as exc:c.p.change_review_material(ch['id'])
    assert exc.value.code=='invalid_input'
