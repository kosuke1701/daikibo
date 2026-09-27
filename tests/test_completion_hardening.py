import json,os,socket,sys,threading,time
from pathlib import Path
import pytest
from daikibo.common import Fault,canonical,digest,parse_json
from daikibo.rpc import Server,Client
from daikibo.qualification import catalog
from conftest import make_task,finish_task


def test_discovery_observations_include_empty_queries_and_exact_source(full,full_project):
    c=full;p,r,q,root=full_project;c.idx.index(c.owner,r)
    empty=c.idx.search(c.owner,p,'there_is_no_such_symbol')
    assert not empty['results'] and not empty['complete']
    event=c.s.one('SELECT * FROM events WHERE id=?',(empty['observation'],))
    assert event['kind']=='discovery_observed'
    assert parse_json(event['body'])['inputs']['query']=='there_is_no_such_symbol'
    read=c.idx.read(c.owner,r,'calc.py',line_count=1)
    assert read['content']=='def add(a,b):' and read['next_line']==2
    consumers=c.idx.consumers(c.owner,p,'add')
    assert consumers['observation'] and consumers['confidence']=='inferred'
    assert c.sec.audit()['verified']


def test_streamed_read_handles_multibyte_chunk_boundary(full,full_project):
    c=full;p,r,q,root=full_project
    content='x'*65534+'漢字\r\n'+'次の行\n'+'last'
    path=root/'large.txt';path.write_bytes(content.encode())
    result=c.idx.read(c.owner,r,'large.txt',start_line=2,line_count=2)
    assert result['content']=='次の行\nlast' and result['end_line']==3 and result['next_line'] is None
    assert result['digest']==digest(content.encode())


def test_oversize_single_line_cannot_overflow_context(full,full_project):
    c=full;p,r,q,root=full_project
    (root/'one-line.txt').write_bytes(b'x'*(2*1024*1024))
    with pytest.raises(Fault) as exc:c.idx.read(c.owner,r,'one-line.txt')
    assert exc.value.code=='context_insufficient'


def test_brownfield_boundaries_require_observed_discovery(full,full_project):
    c=full;p,r,q,root=full_project
    blockers=c.p.phase_blockers({'project':p,'phase':'boundaries'})
    assert 'discovery_index_missing:'+r in blockers
    c.idx.index(c.owner,r)
    assert 'discovery_not_observed' in c.p.phase_blockers({'project':p,'phase':'boundaries'})
    c.idx.search(c.owner,p,'calc')
    assert 'discovery_not_observed' not in c.p.phase_blockers({'project':p,'phase':'boundaries'})


def test_preparation_failure_releases_os_identity_and_working_directory(full,full_project):
    c=full;p,r,q,root=full_project;snap=c.sn.capture(c.owner,p)
    def bad(*args):raise Fault('fixture_failure','Fail before Popen')
    with pytest.raises(Fault):c.rt.observe(p,None,q,'test',None,'b',snap,bad)
    assert not c.rt.active
    assert not list(c.rt.workroot.iterdir())


def test_exit_zero_with_invalid_agent_result_cannot_submit_candidate(full,full_project,monkeypatch):
    c=full;p=full_project[0];task=make_task(c,full_project);c.w.claim(c.owner,p,task)
    def broken(*args):raise Fault('invalid_agent_output','No valid terminal Agent result')
    monkeypatch.setattr(c.rt.adapters,'normalize',broken)
    with pytest.raises(Fault) as exc:c.rt.execute(c.owner,task,'fixture')
    assert exc.value.code=='implementation_failed'
    assert not c.w.task(c.owner,task)['candidate']
    assert c.s.one('SELECT count(*) AS n FROM receipts')['n']==1


def test_report_blob_loss_invalidates_completion_in_periodic_sweep(full,full_project):
    c=full;p=full_project[0];task=make_task(c,full_project);finish_task(c,p,task)
    evidence=c.s.one("SELECT id FROM receipts WHERE subject=? AND role='test:unit'",(task,))
    report=c.g.receipt(evidence['id'])['result']['report_blob']
    (c.s.blobs/report[:2]/report[2:]).unlink()
    sweep=c.ops.reconcile_evidence(batch_size=100)
    assert sweep['problems'] and not sweep['whole_database_certified']
    assert c.w.task(c.owner,task)['validity']=='needs_review'
    assert c.s.one("SELECT id FROM inbox WHERE kind='integrity_mismatch' AND ref=?",(task,))


def test_periodic_sweep_has_bounded_resumable_cursors(full,full_project):
    c=full;p=full_project[0];task=make_task(c,full_project);finish_task(c,p,task)
    first=c.ops.reconcile_evidence(batch_size=1);assert first['scanned']['receipts']==1
    position=c.s.one("SELECT value FROM meta WHERE key='reconcile.cursor.receipts'")['value']
    second=c.ops.reconcile_evidence(batch_size=1)
    assert int(c.s.one("SELECT value FROM meta WHERE key='reconcile.cursor.receipts'")['value'])>int(position)
    assert not second['problems']


def test_judgment_qualification_catalog_has_positive_negative_and_ambiguous_cases():
    q=catalog();cases=q['cases']
    assert len(cases)==8 and len({c['id'] for c in cases})==8
    assert {c['expect'] for c in cases}=={'pass','fail','blocked'}
    assert q['digest']==digest(cases)
    assert {'weak-assertion','repository-injection','contradictory-requirements','ambiguous-acceptance'}<={c['id'] for c in cases}
    assert all(c['files'] and c['requirement'] and c['purpose'] for c in cases)


def test_rpc_rejects_capacity_without_spawning_unbounded_threads(full,tmp_path):
    path=tmp_path/'rpc'/'sock';server=Server(full,path)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    for _ in range(64):assert server.slots.acquire(blocking=False)
    try:
        token=Path(full.sec.bootstrap()).read_text()
        with pytest.raises(Fault) as exc:Client(path,token).call('api.describe')
        assert exc.value.code=='capacity'
    finally:
        for _ in range(64):server.slots.release()
        server.shutdown();server.server_close();thread.join()


def test_recursive_sql_impact_terminates_cycles_and_preserves_task_fanout(full,full_project):
    c=full;p,r,q,root=full_project;t=make_task(c,full_project)
    a=c.k.propose(c.owner,p,'design',{'title':'A','statement':'A'})['id']
    b=c.k.propose(c.owner,p,'design',{'title':'B','statement':'B'})['id']
    c.k.link(c.owner,a,q,'realizes','inferred','candidate mapping')
    c.k.link(c.owner,b,a,'depends_on','inferred','dependency candidate')
    c.k.link(c.owner,a,b,'depends_on','inferred','cycle allowed outside hierarchy')
    impact=c.k.impact(c.owner,p,[q])
    assert set(impact['artifacts'])=={q,a,b} and impact['tasks']==[t]
    assert impact['reachable_sets_complete'] and impact['unknown']
