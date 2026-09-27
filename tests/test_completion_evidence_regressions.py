"""Completed labels cannot replace retained execution/test evidence after loss."""
import copy
import pytest
from conftest import make_task,finish_task
from daikibo.common import canonical,digest,parse_json,Fault


def candidate(c,t):
    row=c.s.one('SELECT * FROM candidates WHERE task=?',(t,),True)
    return row,parse_json(row['body'])


def test_successful_retained_implementation_remains_valid(full,full_project):
    c=full;t=make_task(c,full_project);finish_task(c,full_project[0],t)
    assert c.g.implementation_evidence(t)['process_started']
    assert c.g.evaluate_task(c.owner,t,'recheck')['verdict']=='pass'


@pytest.mark.parametrize('damage',['implementation_stdout','candidate_blob','candidate_digest','candidate_snapshot','different_run','epoch'])
def test_core_gate_rechecks_implementation_and_changed_output(full,full_project,damage):
    c=full;t=make_task(c,full_project);finish_task(c,full_project[0],t)
    row,body=candidate(c,t)
    if damage=='implementation_stdout':
        receipt=c.g.receipt(body['implementation_receipt']);c.s.blob_path(receipt['stdout_blob']).unlink()
    elif damage=='candidate_blob':
        changed=next(ch['after'] for ch in body['changes'] if ch['after'] and ch['after']['kind']=='file')
        c.s.blob_path(changed['blob']).unlink()
    else:
        if damage=='candidate_digest':body['unexpected']='corrupt'
        elif damage=='candidate_snapshot':body['snapshot']['digest']='0'*64
        elif damage=='different_run':
            r=c.s.one("SELECT id FROM receipts WHERE subject=? AND role='spec'",(t,),True)
            body['implementation_receipt']=r['id']
        elif damage=='epoch':c.s.execute('UPDATE candidates SET epoch=epoch+1 WHERE id=?',(row['id'],))
        if damage!='epoch':
            h=row['digest'] if damage=='candidate_digest' else digest(body)
            c.s.execute('UPDATE candidates SET body=?,digest=? WHERE id=?',(canonical(body).decode(),h,row['id']))
    report=c.g.evaluate_task(c.owner,t,'recheck')
    assert report['verdict']=='fail' and any(f.startswith('implementation:') for f in report['failures'])


def test_manual_integrity_audit_cannot_ignore_test_failure(full,full_project):
    c=full;t=make_task(c,full_project);finish_task(c,full_project[0],t)
    c.s.execute("UPDATE runs SET status='unknown' WHERE task=? AND role LIKE 'test:%'",(t,))
    report=c.ops.audit(c.owner)
    assert not report['integrity_verified']
    assert any(p.get('task')==t for p in report['problems'])
    assert c.w.task(c.owner,t)['validity']=='needs_review'


def test_periodic_reconciliation_does_not_accept_a_wrong_candidate_body(full,full_project):
    c=full;t=make_task(c,full_project);finish_task(c,full_project[0],t)
    c.s.execute("UPDATE candidates SET digest=? WHERE task=?",('0'*64,t))
    report=c.ops.reconcile_evidence()
    assert any(p['id']==t for p in report['problems'])
    assert c.w.task(c.owner,t)['validity']=='needs_review'
