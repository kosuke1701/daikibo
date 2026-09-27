import base64,copy,io,json,os,socket,sys,threading,time,urllib.error,urllib.request
from pathlib import Path
import pytest
from daikibo.common import Fault,canonical,digest,parse_json
from daikibo.gitops import git
from daikibo.integrations import public_http
from daikibo.operations import restore_backup
from daikibo.control import Control
from conftest import make_task,finish_task

@pytest.mark.parametrize('url',['https://127.0.0.1/','https://[::1]/','http://example.com/','https://user:secret@example.com/'])
def test_research_does_not_access_private_or_plaintext_endpoints(url):
    with pytest.raises(Fault):public_http(url)




def test_git_reconciliation_mismatch_never_changes_ref(full,full_project):
    c=full;p,r,q,root=full_project
    original=c.sn.capture(c.owner,p);first=c.sn.commit_snapshot(original,r,'initial')
    (root/'calc.py').write_text('different = True\n');second=c.sn.capture(c.owner,p)
    with pytest.raises(Fault):c.sn.commit_snapshot(second,r,'must not mutate',expected=first['commit'],reconcile_only=True)
    assert git(Path(first['git_dir']),'rev-parse',first['ref']).stdout.decode().strip()==first['commit']

def test_global_maintenance_backup_is_a_real_job(full,full_project):
    c=full;j=c.jobs.submit(c.owner,'ops.backup',{})
    result=c.jobs.run_one(c.s.one('SELECT * FROM jobs WHERE id=?',(j['id'],)))
    assert result['status']=='succeeded',result
    assert Path(result['result']['path']).is_file()

def test_late_cancelled_review_evidence_is_never_adopted(full,full_project,monkeypatch):
    c=full;p,r,q,root=full_project
    j=c.jobs.submit(c.owner,'review',{'subject':q,'role':'requirements','adapter':'fixture'})
    c.s.execute("UPDATE jobs SET cancelled=1,status='running' WHERE id=?",(j['id'],))
    c.rt.job_context.id=j['id']
    try:
        review=c.rt.review(c.owner,q,'requirements','fixture')
        assert c.g.receipt(review['receipt'])['cancelled']
        with pytest.raises(Fault):c.g.require_review(review['receipt'],q,c.k.artifact(c.owner,q)['digest'],{'requirements'})
    finally:c.rt.job_context.id=None

def test_audit_fences_completed_task_with_disappeared_receipts(full,full_project):
    c=full;p=full_project[0];t=make_task(c,full_project);finish_task(c,p,t)
    # Simulate administrative corruption, outside worker permissions. Immutable production triggers stay intact.
    c.s.execute("UPDATE runs SET status='unknown' WHERE task=? AND role='spec'",(t,))
    report=c.ops.audit(c.owner)
    assert not report['integrity_verified'] and c.w.task(c.owner,t)['validity']=='needs_review'
