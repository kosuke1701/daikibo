from pathlib import Path
import pytest
from daikibo.common import Fault

def test_actual_execution_review_gate_path(system,project,task):
    s=system;pid=project[0]
    s.w.claim(s.owner,pid,task)
    observed=s.rt.execute(s.owner,task,'fixture')
    assert observed['status']=='submitted'
    assert s.g.evaluate_task(s.owner,task)['verdict']=='fail'
    tests=s.rt.tests(s.owner,task)
    assert tests['checks'][0]['result']['passed'],tests
    for role in ['spec','quality','test_adequacy']:
        result=s.rt.review(s.owner,task,role,'fixture')
        assert result['result']['verdict']=='pass',result
    g=s.g.evaluate_task(s.owner,task)
    assert g['verdict']=='pass',g
    result=s.w.complete(s.owner,task,1)
    assert result['status']=='completed'
    assert s.sec.audit()['verified']
    # Fixture receipts cannot satisfy governed assurance.
    s.g.mode='governed'
    assert s.g.evaluate_task(s.owner,task)['verdict']=='fail'

def test_no_self_claimed_completion(system,task):
    with pytest.raises(Fault,match='Completion rejected'):
        system.w.complete(system.owner,task,1)

def test_offline_multilanguage_index(system,project):
    s=system;pid,rid,_,root=project
    cases={'x.ts':'function f(x: number) { return x; }\nf(1);\n','x.rs':'fn foo() {}\nfn main() { foo(); }\n','x.cs':'class C { public int F() { return 1; } }\n'}
    for path,content in cases.items():(root/path).write_text(content)
    report=s.idx.index(s.owner,rid)
    assert report['files']==5
    result=s.idx.search(s.owner,pid,'add')
    assert any(r['path']=='calc.py' for r in result['results'])
    assert s.idx.consumers(s.owner,pid,'add')['results']
    again=s.idx.index(s.owner,rid)
    assert again['changed']==0

def test_actor_cannot_impersonate_owner(system,project):
    from daikibo.common import Actor
    s=system
    agent=Actor('test-agent','agent',project[0])
    with pytest.raises(Fault):agent.require('owner')
