import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path
import pytest
from daikibo.common import Actor,Fault,canonical,digest,parse_json,timestamp
from daikibo.governance import Predicates,CORE_CRITERIA
from daikibo.db import Store
from daikibo.testreports import junit
from conftest import make_task,finish_task

@pytest.mark.parametrize('data',[b'{"a":1,"a":2}',b'{"n":NaN}',b'{"n":Infinity}',b'{"x":',b'\xff'])
def test_untrusted_json_rejected(data):
    with pytest.raises(Fault):parse_json(data)

@pytest.mark.parametrize('rule',[{'eval':'__import__("os").system("false")'},{'unknown':True},{'eq':['x']},{'all':'yes'}])
def test_closed_predicate_language(rule):
    with pytest.raises((Fault,TypeError,ValueError,KeyError)):Predicates.evaluate(rule,{})


def test_project_scope_and_nonforgeable_actor(full,full_project):
    c=full;p=full_project[0];other=c.k.create_project(c.owner,'other')['id']
    agent=Actor('test-agent','agent',p)
    with pytest.raises(Fault):c.k.project(agent,other)
    with pytest.raises(Fault):c.request(None,{'id':'spoof','method':'project.get','params':{'project':p,'actor':{'role':'owner'}}})
    for method,args in [('capability.issue',{'role':'owner'}),('keys.rotate',{}),('decision.respond',{'decision':'missing','expected_digest':'x','choice':'approve','utterance':'agent pretending human'})]:
        with pytest.raises(Fault):c.request(None,{'id':method,'method':method,'params':args})
    with pytest.raises(Fault):c.invoke(agent,'observe',{})
    with pytest.raises(Fault):c.invoke(agent,'store.execute',{'sql':'DELETE FROM tasks'})




def test_mutation_idempotency_does_not_duplicate_project(full):
    c=full;token=Path(c.sec.bootstrap()).read_text();req={'id':'one','method':'project.create','params':{'name':'same'}}
    a=c.request(token,req);b=c.request(token,req);assert a==b
    assert len(c.s.all('SELECT id FROM projects'))==1
    req['params']['name']='different'
    with pytest.raises(Fault,match='reused'):c.request(token,req)


def test_single_control_owner_and_transaction_rollback(full):
    c=full
    with pytest.raises(Fault,match='Another daemon'):Store(c.s.home)
    count=len(c.s.all('SELECT * FROM projects'))
    with pytest.raises(RuntimeError):
        with c.s.transaction():
            c.k.create_project(c.owner,'rolled back');raise RuntimeError('crash')
    assert len(c.s.all('SELECT * FROM projects'))==count
    assert c.sec.audit()['verified']


def test_audit_immutable_checksum_chain(full,full_project):
    c=full;c.sec.event(full_project[0],'sample',c.owner.id,{'hello':'world'})
    for table in ('events','revisions'):
        with pytest.raises(sqlite3.IntegrityError):c.s.execute(f'DELETE FROM {table}')
    old=c.sec.audit()['head'];c.sec.event(full_project[0],'next_event',c.owner.id,{'next': True})
    assert c.sec.audit()['verified'] and c.sec.audit()['head']!=old


def test_unobserved_completion_and_stale_epoch(full,full_project):
    c=full;t=make_task(c,full_project)
    with pytest.raises(Fault):c.w.complete(c.owner,t,1)
    c.w.claim(c.owner,full_project[0],t);before=c.w.task(c.owner,t)
    c.s.execute('UPDATE tasks SET lease_until=0 WHERE id=?',(t,));c.w.reconcile(c.owner)
    current=c.w.task(c.owner,t);assert current['epoch']>before['epoch'] and current['validity']=='needs_review'
    with pytest.raises(Fault):c.w.heartbeat(c.owner,t,before['epoch'])
    with pytest.raises(Fault):c.rt.execute(c.owner,t,'fixture')


def test_source_coverage_cannot_hide_unclassified_text(full,full_project):
    c=full;p=full_project[0];source=c.k.source(c.owner,p,'First requirement. Second requirement.')
    c.k.classify(c.owner,source['id'],0,18,'question',[],'Needs clarification')
    coverage=c.k.source_coverage(c.owner,p)
    assert not coverage['structurally_complete']
    with pytest.raises(Fault):c.k.classify(c.owner,source['id'],1,5,'reference',[],'overlap')


def test_graph_cycles_and_inferred_coverage(full,full_project):
    c=full;p=full_project[0]
    a=c.k.propose(c.owner,p,'design',{'title':'A','statement':'one'})['id']
    b=c.k.propose(c.owner,p,'design',{'title':'B','statement':'two'})['id']
    c.k.link(c.owner,a,b,'decomposes',confidence='asserted',basis='test design relation')
    with pytest.raises(Fault):c.k.link(c.owner,b,a,'decomposes',confidence='asserted',basis='cycle')
    c.k.link(c.owner,a,full_project[2],'realizes',confidence='inferred',basis='heuristic')
    assert not c.k.trace(c.owner,p)['structural_complete']

@pytest.mark.parametrize('data',[
 b'<testsuite tests="0"/>',
 b'<testsuite><testcase name="x"><skipped/></testcase></testsuite>',
 b'<testsuite><testcase name="x"><failure/></testcase></testsuite>',
 b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuite/>',
 b'<garbage>success</garbage>',b'<testsuite><testcase/></testsuite>'
])
def test_fake_empty_failed_or_unsafe_test_reports_not_pass(data):
    try:assert not junit(data).get('passed')
    except Fault:pass


def test_missing_required_test_rejected():
    data=b'<testsuite><testcase name="wrong_test" classname="sample"/></testsuite>'
    assert not junit(data,['test_important'])['passed']


def test_nonruntime_evidence_cannot_be_imported(full,full_project):
    c=full;p=full_project[0];fake={'id':'EVD-fake','verdict':'pass','reviewer':'independent'}
    blob=c.s.blob_put(canonical(fake))
    assert blob
    with pytest.raises(Fault):c.g.receipt('EVD-fake')
    assert 'evidence.register' not in c.routes


def test_failed_test_does_not_become_complete(full,full_project):
    c=full;t=make_task(c,full_project,goal='WRITE:'+json.dumps({'calc.py':'def add(a,b):\n    return 99\n'}))
    c.w.claim(c.owner,full_project[0],t);c.rt.execute(c.owner,t,'fixture')
    report=c.rt.tests(c.owner,t);assert not report['checks'][0]['result']['passed']
    for role in ('spec','quality','test_adequacy'):c.rt.review(c.owner,t,role,'fixture')
    assert c.g.evaluate_task(c.owner,t)['verdict']=='fail'


def test_forbidden_path_and_environment_overrides(full,full_project):
    c=full;p,r,q,_=full_project
    body={'title':'x','goal':'x','read_artifacts':[q],'write_paths':['../control'],'acceptance':['a'],'dependencies':[],'repos':[r],'non_goals':[]}
    with pytest.raises(Fault):c.w.create(c.owner,p,body)
    t=make_task(c,full_project)
    with pytest.raises(Fault):c.w.plan_tests(c.owner,t,{'checks':[{'id':'t','argv':['python','-m','pytest'],'kind':'pytest','env':{'LD_PRELOAD':'/tmp/evil.so'}}]})


def test_critical_risk_cannot_be_self_downgraded(full,full_project):
    c=full;p,r,q,_=full_project
    row=c.w.create(c.owner,p,{'title':'auth','goal':'fix auth','read_artifacts':[q],'write_paths':['auth.py'],'risk':'lite','acceptance':['auth'],'dependencies':[],'repos':[r],'non_goals':[]})
    assert row['body']['risk']=='critical'
    with pytest.raises(Fault):c.g.waiver(c.owner,p,row['id'],'spec_review','urgent',timestamp()+1000,row['id'],['manual'])


def test_real_adapter_qualification_cannot_use_fixture(full,full_project):
    with pytest.raises(Fault):full.supervisor.qualify(full.owner,full_project[0],'fixture')
