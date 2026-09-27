"""Integration of selected planning revision and whole-program deliverability.

All reviewer subprocesses here are protocol fixtures, not semantic acceptance.
"""
import copy
import pytest
from daikibo.common import Fault, canonical, digest, timestamp
from test_reviewed_breakdowns import setup, adopt, propose, review_all
from test_delivery_git_and_recovery import profile
from conftest import ensure_current_root, finish_task, make_task


def prepared(s):
    c,p,r,q,program,d,t,units=s
    body=profile(p,r,q,t);body['program']=program
    c.d.configure(c.owner,p,body)
    adopted=adopt(s)
    ensure_current_root(c,p,t);c.w.ready(c.owner,t);finish_task(c,p,t)
    return c.d.prepare(c.owner,p)['id'],adopted


def test_delivery_without_engineering_program_cannot_be_certified(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project)
    c.d.configure(c.owner,p,profile(p,r,q,t));finish_task(c,p,t)
    delivery=c.d.prepare(c.owner,p)['id']
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery)
    assert 'engineering_workflow_required' in exc.value.details
    assert c.s.one('SELECT status FROM deliveries WHERE id=?',(delivery,))['status']=='prepared'


def test_prepared_delivery_binds_exact_selected_program_and_plan(setup):
    c,p,r,q,program,d,t,units=setup;delivery,adopted=prepared(setup)
    row,body=c.d.current(delivery)
    assert body['binding']['program']==program
    assert body['binding']['breakdown']=={'id':adopted['id'],'digest':adopted['digest']}
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery)
    assert 'engineering_phases_incomplete' in exc.value.details


def test_adopting_a_new_plan_invalidates_previous_delivery(setup):
    c=setup[0];delivery,old=prepared(setup)
    units=copy.deepcopy(setup[-1]);units[1]['rationale']='Revised explanation of the same task responsibility'
    new=propose(setup,units,previous=old['id']);review_all(c,new['id'])
    c.breakdowns.activate(c.owner,new['id'],old['id'])
    with pytest.raises(Fault) as exc:c.d.current(delivery)
    assert exc.value.code=='stale_delivery' and 'breakdown_replaced' in exc.value.details
    replacement=c.d.prepare(c.owner,setup[1])
    assert replacement['id']!=delivery


def test_other_program_cannot_use_this_programs_delivery(setup):
    c,p,r,q,program,d,t,units=setup;delivery,_=prepared(setup)
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    second=c.p.begin(c.owner,p,source)['program']
    report=c.lifecycle.completion(c.owner,second,delivery)
    assert 'delivery_program_mismatch' in {f['code'] for f in report['failures']}


def test_an_unrelated_new_program_does_not_replace_frozen_profile_program(setup):
    c,p,r,q,program,d,t,units=setup;delivery,_=prepared(setup)
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    second=c.p.begin(c.owner,p,source)['program']
    assert second!=program
    replacement=c.d.prepare(c.owner,p)['id']
    assert c.d.current(replacement)[1]['binding']['program']==program
    c.s.execute("UPDATE programs SET phase='integration' WHERE id=?",(program,))
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,replacement)
    assert not any('breakdown_not_current:'+second==x for x in exc.value.details)


@pytest.mark.parametrize('status',['expired','provisional','pending'])
def test_unresolved_decision_is_not_erased_from_final_gate(setup,status):
    c,p,r,q,program,d,t,units=setup;delivery,_=prepared(setup)
    body={'reason':'Requirement-related decision still awaiting confirmation'}
    c.s.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('DEC-unresolved',p,1,canonical(body).decode(),digest(body),status,None,None,None,timestamp()))
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery)
    assert 'unconfirmed_decisions' in exc.value.details


def test_supervisor_may_request_certification_but_cannot_bypass_the_gate(setup,tmp_path):
    import sys,json
    c,p,r,q,program,d,t,units=setup;delivery,_=prepared(setup)
    reply={'message':'Request, do not self-certify','actions':[{'method':'delivery.certify','params':{'delivery':delivery}}],'questions':[]}
    script=tmp_path/'certification_planner.py';script.write_text('import json,sys\njson.load(sys.stdin)\nprint(json.dumps('+repr(reply)+'))\n')
    c.rt.adapters.register(c.owner,'cert-planner','fixture',sys.executable,[str(script)])
    result=c.supervisor.turn(c.owner,p,'cert-planner')
    assert result['actions'][0]['error']['code']=='release_gate_denied'
    assert c.s.one('SELECT status FROM deliveries WHERE id=?',(delivery,))['status']=='prepared'


def test_profile_and_delivery_progress_is_visible_to_the_supervisor(setup):
    c,p,r,q,program,d,t,units=setup
    before=c.supervisor.state_digest(p);c.d.configure(c.owner,p,profile(p,r,q,t))
    assert c.supervisor.state_digest(p)!=before
    adopt(setup);ensure_current_root(c,p,t);c.w.ready(c.owner,t);finish_task(c,p,t)
    before=c.supervisor.progress_digest(p);delivery=c.d.prepare(c.owner,p)['id']
    assert c.supervisor.progress_digest(p)!=before
    before=c.supervisor.progress_digest(p)
    # Unit-level state observation only; this does NOT certify any test delivery.
    c.s.execute("UPDATE deliveries SET status='verified' WHERE id=?",(delivery,))
    assert c.supervisor.progress_digest(p)!=before
    with pytest.raises(Fault):c.d.certify(c.owner,delivery)
