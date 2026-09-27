import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
import pytest
from daikibo.common import Actor,Fault,canonical,digest,parse_json,timestamp
from daikibo.rpc import Server,Client,receive
from conftest import make_task,finish_task


def test_real_unix_rpc_auth_idempotency_and_job(full,full_project):
    c=full;p=full_project[0]
    with tempfile.TemporaryDirectory(prefix='dd-rpc-') as d:
        path=Path(d)/'rpc.sock';server=Server(c,path);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            owner=Client(path,Path(c.sec.bootstrap()).read_text())
            agent=Client(path)
            assert agent.call('project.get',{'project':p})['id']==p
            with pytest.raises(Fault):agent.call('capability.issue',{'role':'owner'})
            assert Client(path).call('project.get',{'project':p})['id']==p
            req={'project':p,'kind':'finding','body':{'title':'Observed code','statement':'Need to inspect'}}
            a=agent.call('artifact.propose',req,'repeat');b=agent.call('artifact.propose',req,'repeat');assert a['id']==b['id']
            job=agent.call('job.submit',{'kind':'index','args':{'repo':full_project[1]},'dedup':'index-once'})
            row=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],));result=c.jobs.run_one(row)
            assert result['status']=='succeeded'
            assert agent.call('job.get',{'job':job['id']})['status']=='succeeded'
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_oversized_protocol_frame_is_rejected_without_allocation():
    a,b=socket.socketpair()
    try:
        a.sendall(struct.pack('!I',0xffffffff))
        with pytest.raises(Fault):receive(b)
    finally:a.close();b.close()


def test_parallel_task_claims_serialize_conflicts(full,full_project):
    c=full;p=full_project[0];a=make_task(c,full_project);b=make_task(c,full_project)
    c.w.claim(c.owner,p,a)
    with pytest.raises(Fault):c.w.claim(c.owner,p,b)
    # Distinct write ownership can proceed without stopping the whole project.
    d=make_task(c,full_project,goal='WRITE:'+json.dumps({'other.py':'x=1\n'}),paths=['other.py'])
    assert c.w.claim(c.owner,p,d)['id']==d


def test_autonomy_runs_real_process_tests_and_review_jobs(full,full_project):
    c=full;p=full_project[0];t=make_task(c,full_project)
    c.jobs.configure(c.owner,p,'fixture','fixture',budget_seconds=60,concurrency=2)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        c.jobs.tick()
        if c.w.task(c.owner,t)['status']=='completed':break
        time.sleep(.05)
    assert c.w.task(c.owner,t)['status']=='completed',c.w.status(c.owner,p)
    roles={r['role'] for r in c.s.all('SELECT role FROM runs WHERE task=?',(t,))}
    assert {'implementer','test:unit','spec','quality','test_adequacy'}<=roles
    # This proves scheduling and the fixture protocol, not an actual LLM judgment.
    assert all(parse_json(r['body'])['assurance']=='validation' for r in c.s.all('SELECT body FROM receipts'))




def test_resource_budget_stops_new_work_without_claiming_impossible(full,full_project):
    c=full;p=full_project[0]
    c.jobs.configure(c.owner,p,'fixture','fixture',budget_seconds=1)
    c.s.execute('UPDATE automation SET spent=2 WHERE project=?',(p,));c.jobs._automate()
    assert not c.jobs.automation_status(c.owner,p)['enabled']
    alert=c.s.one("SELECT body FROM inbox WHERE project=? AND kind='autonomy_limit'",(p,))
    assert parse_json(alert['body'])['technical_impossibility'] is False




def test_readonly_reviewer_cannot_modify_input(full,full_project):
    c=full;p=full_project[0];snapshot=c.sn.capture(c.owner,p)
    script='from pathlib import Path\nPath("calc.py").write_text("compromised")\n'
    receipt,_,_=c.rt.observe(p,None,'readonly','probe',None,digest(script),snapshot,lambda w,h,d:([sys.executable,'-c',script],None),timeout=5,readonly=True)
    assert receipt['exit_code']==0 and not receipt['readonly_verified']
    assert receipt['worker_uid']==os.geteuid()


def test_command_timeout_records_failure_and_kills_children(full,full_project):
    c=full;p=full_project[0];snapshot=c.sn.capture(c.owner,p)
    script='import time;time.sleep(30)'
    receipt,_,_=c.rt.observe(p,None,'timeout','probe',None,digest(script),snapshot,lambda w,h,d:([sys.executable,'-c',script],None),timeout=.15,readonly=True)
    assert receipt['timed_out'] and receipt['exit_code']!=0
    assert c.g.receipt(receipt['id'])['timed_out']


def test_missing_executable_observed_as_failed_not_success(full,full_project):
    c=full;p=full_project[0];snapshot=c.sn.capture(c.owner,p)
    receipt,_,_=c.rt.observe(p,None,'missing','probe',None,'binding',snapshot,lambda w,h,d:(['/nonexistent/daikibo-command'],None),timeout=2)
    assert receipt['exit_code']!=0


def test_scope_escape_symlink_is_not_adopted(full,full_project):
    c=full;p=full_project[0];root=full_project[3]
    (root/'escape').symlink_to('/etc/passwd')
    with pytest.raises(Fault):c.sn.capture(c.owner,p)


def test_context_stale_file_detected(full,full_project):
    c=full;p,r,q,root=full_project;t=make_task(c,full_project);c.idx.index(c.owner,r)
    package=c.ctx.task_context(c.owner,t,query='add')
    (root/'calc.py').write_text('def add(a,b): return "changed"\n')
    assert not c.ctx.fresh(c.owner,package['id'])['fresh']
