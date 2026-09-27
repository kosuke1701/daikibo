"""Transport budgeting is not scope reduction, acknowledgement, or a judgment."""
import hashlib
import json
import sys
import pytest
from daikibo.common import Actor, Fault, canonical
from test_reviewed_breakdowns import setup, accepted


def call(c, name, **params):
    return c.invoke(c.owner,name,params)


def test_catalog_pages_preserve_all_items_and_require_cursor(setup):
    c,p,r,q,program,d,t,units=setup
    for n in range(7): c.k.propose(c.owner,p,'finding',{'title':str(n),'statement':'note '*10000})
    ids=[];offset=0;snapshot=None
    while True:
        page=call(c,'artifact.catalog',project=p,offset=offset,limit=3,expected_snapshot=snapshot)
        assert len(canonical(page))<12000 and not page['bodies_included']
        ids += [a['id'] for a in page['items']]
        snapshot=page['snapshot'];offset=page['next_offset']
        if offset is None:break
    assert set(ids)=={r['id'] for r in c.s.all('SELECT id FROM artifacts WHERE project=?',(p,))}
    assert len(ids)==page['total']
    with pytest.raises(Fault,match='Continue'):call(c,'artifact.catalog',project=p,offset=1)


def test_catalog_rejects_mixed_revision_pages(setup):
    c,p,r,q,program,d,t,units=setup
    old=call(c,'artifact.catalog',project=p,limit=1)
    c.k.propose(c.owner,p,'finding',{'title':'new','statement':'new fact'})
    with pytest.raises(Fault,match='collection changed'):
        call(c,'artifact.catalog',project=p,offset=1,expected_snapshot=old['snapshot'])


def test_exact_unicode_fragments_reconstruct_full_body_and_old_revision(setup):
    c,p,r,q,program,d,t,units=setup
    body={'title':'日本語仕様','statement':'仕様変更🧩を検討。'*80}
    a=c.k.propose(c.owner,p,'finding',body);out=[];offset=0
    while True:
        page=call(c,'artifact.read',artifact=a['id'],expected_digest=a['digest'],offset=offset,byte_budget=64)
        assert len(page['content'].encode())<=64 and not page['semantic_review']
        out.append(page['content']);offset=page['next_offset']
        if offset is None:break
    assert ''.join(out).encode()==canonical(body)
    c.k.revise(c.owner,a['id'],1,{**body,'statement':'new'},'new information')
    with pytest.raises(Fault,match='digest'):call(c,'artifact.read',artifact=a['id'],expected_digest=a['digest'])
    old=call(c,'artifact.read',artifact=a['id'],expected_digest=a['digest'],revision=1,byte_budget=65536)
    assert old['complete_body'] and json.loads(old['content'])==body


def test_inbox_pages_never_hide_counts_or_acknowledge(setup):
    c,p,r,q,program,d,t,units=setup
    for n in range(55):c.g.inbox(p,'needs_adjudication',str(n),{'content':'important '*5000},'critical' if n==54 else 'warning')
    first=call(c,'inbox.catalog',project=p,limit=2)
    assert first['total']==55 and first['next_offset']==2
    assert first['items'][0]['severity']=='critical'
    one=first['items'][0]
    part=call(c,'inbox.read',item=one['id'],expected_digest=one['digest'],byte_budget=64)
    assert not part['acknowledgement'] and not part['complete_body']
    assert c.s.one("SELECT count(*) AS n FROM inbox WHERE status='open'")['n']==55
    assert c.s.one('SELECT sum(displayed) AS n FROM inbox')['n']==0
    summary=call(c,'workflow.summary',project=p)
    assert sum(x['count'] for x in summary['notice_counts'])==55
    assert not summary['completion_evaluated']


def test_inbox_same_length_update_invalidates_cursor_and_body_digest(setup):
    c,p,r,q,program,d,t,units=setup
    c.g.inbox(p,'note','same',{'content':'one'})
    a=call(c,'inbox.catalog',project=p)
    c.g.inbox(p,'note','same',{'content':'two'})
    with pytest.raises(Fault,match='collection changed'):
        call(c,'inbox.catalog',project=p,expected_snapshot=a['snapshot'])
    with pytest.raises(Fault,match='digest'):
        call(c,'inbox.read',item=a['items'][0]['id'],expected_digest=a['items'][0]['digest'])


def test_phase_blocker_paging_does_not_relax_full_gate(setup):
    c,p,r,q,program,d,t,units=setup
    for n in range(10):accepted(c,p,'requirement',str(n),acceptance=[f'AC-{n}'])
    c.s.execute("UPDATE programs SET phase='design' WHERE id=?",(program,))
    first=call(c,'program.blockers',program=program,limit=2)
    assert first['total']==10 and len(first['items'])==2 and first['next_offset']==2
    last=call(c,'program.blockers',program=program,limit=2,offset=first['total'],expected_snapshot=first['snapshot'])
    assert not last['items'] and not last['phase_complete'] and not last['all_structural_blockers_clear']
    with pytest.raises(Fault,match='incomplete'):
        c.p.advance(c.owner,program,1,'invented-review')
    c.k.propose(c.owner,p,'finding',{'title':'new','statement':'Changes phase binding'})
    with pytest.raises(Fault,match='conditions changed'):
        call(c,'program.blockers',program=program,offset=2,expected_snapshot=first['snapshot'])


@pytest.mark.parametrize('name,extra',[
    ('artifact.catalog',{'offset':-1}),('inbox.catalog',{'limit':201}),('program.catalog',{'limit':True}),
    ('artifact.catalog',{'offset':100000}),('program.blockers',{'byte_budget':5})])
def test_invalid_ranges_are_structured_faults(setup,name,extra):
    c,p,r,q,program,d,t,units=setup
    params={'program':program} if name=='program.blockers' else {'project':p}
    with pytest.raises(Fault):call(c,name,**params,**extra)


def test_readonly_routes_enforce_project_and_expose_signatures(setup):
    c,p,r,q,program,d,t,units=setup
    other=c.k.create_project(c.owner,'different')['id'];agent=Actor('other-agent','agent',other)
    for method,params in [('artifact.catalog',{'project':p}),('program.blockers',{'program':program}),
                          ('artifact.read',{'artifact':q,'expected_digest':c.k.artifact(c.owner,q)['digest']})]:
        assert method in c.read_routes
        with pytest.raises(Fault):c.invoke(agent,method,params)


def test_catalog_poll_only_counts_as_progress_when_content_changes(setup):
    c,p,r,q,program,d,t,units=setup
    ev=c.rt.review(c.owner,q,'requirements','markers')['receipt'];params={'project':p}
    view=call(c,'artifact.catalog',**params)
    c.supervisor.record_view(p,'artifact.catalog',params,view,ev);before=c.supervisor.progress_digest(p)
    c.supervisor.record_view(p,'artifact.catalog',params,call(c,'artifact.catalog',**params),ev)
    assert before==c.supervisor.progress_digest(p)
    c.k.propose(c.owner,p,'finding',{'title':'new','statement':'new'})
    c.supervisor.record_view(p,'artifact.catalog',params,call(c,'artifact.catalog',**params),ev)
    assert before!=c.supervisor.progress_digest(p)


def test_real_fixture_planner_uses_small_indexes_for_large_specs_and_notices(setup,tmp_path):
    c,p,r,q,program,d,t,units=setup
    for n in range(20):
        c.k.propose(c.owner,p,'finding',{'title':str(n),'statement':'仕様検討 '*18000})
        c.g.inbox(p,'note',str(n),{'content':'判断内容 '*18000},'critical' if n==19 else 'warning')
    script=tmp_path/'catalog_planner.py'
    script.write_text('''import json,sys
p=json.load(sys.stdin)
a=p['context']['artifacts'];i=p['context']['inbox'];w=p['context']['workflows']
assert 'body' not in a['items'][0] and i['total']==20
assert w['total'] >= 1
print(json.dumps({'message':'FIXTURE only: bounded prompt bytes='+str(len(json.dumps(p).encode())),
'actions':[{'method':'artifact.catalog','params':{'project':p['context']['project'],'limit':2}}], 'questions':[]}))
''')
    c.rt.adapters.register(c.owner,'bounded-planner','fixture',sys.executable,[str(script)])
    # Legacy eager context is over budget; do not weaken the actual prompt cap.
    assert len(canonical(c.k.list_artifacts(c.owner,p)))>900000
    result=c.supervisor.turn(c.owner,p,'bounded-planner')
    assert 'bounded prompt bytes=' in result['message']
    assert len(result['actions'][0]['result']['items'])==2
    assert c.s.one('SELECT sum(displayed) AS n FROM inbox')['n']==0


def test_oversized_mandatory_constraints_still_fail_instead_of_being_dropped(setup):
    c,p,r,q,program,d,t,units=setup
    for n in range(20):accepted(c,p,'finding',str(n),statement_override='unused')
    # Synthetic DB setup isolates the invariant budget safeguard: not evidence
    # that these drafted constraints have passed a real semantic review.
    for row in c.s.all("SELECT id,body FROM artifacts WHERE project=? AND kind='finding'",(p,)):
        body=json.loads(row['body']);body['constraints']={'text':'x'*100000}
        c.s.execute('UPDATE artifacts SET body=? WHERE id=?',(canonical(body).decode(),row['id']))
    with pytest.raises(Fault,match='Bound the planning domain'):
        c.supervisor.turn(c.owner,p,'markers')
    assert not c.s.one("SELECT id FROM receipts WHERE project=? AND role='supervisor'",(p,))


def test_native_input_and_context_do_not_prefetch_large_pending_notices(setup,tmp_path):
    c,p,r,q,program,d,t,units=setup
    for n in range(40):c.g.inbox(p,'important',str(n),{'content':'pending '*20000},'critical' if n==39 else 'warning')
    session='bounded-conversation'
    connected=c.native.attach(c.owner,session,str(tmp_path),project=p,register_repository=False)
    assert len(canonical(connected))<65536 and connected['notification_count']==40
    assert connected['notifications'][0]['severity']=='critical'
    assert connected['notifications_are_index'] and connected['more_notifications_operation']=='inbox.catalog'
    result=c.native.input(c.owner,session,'同じ要件のまま、未確認事項について議論してください。',turn_id='first')
    assert len(canonical(result))<65536 and result['notification_count']==40
    assert result['notification_catalog']['next_offset']==30
    assert c.s.one("SELECT count(*) AS n FROM inbox WHERE status='open'")['n']==40
    assert c.s.one('SELECT sum(displayed) AS n FROM inbox')['n']==0


def test_compact_intake_retains_source_and_first_phase_without_full_notices(full,tmp_path):
    c=full;p=c.k.create_project(c.owner,'bootstrap')['id']
    for n in range(30):c.g.inbox(p,'bootstrap-note',str(n),{'content':'long '*30000})
    result=c.i.intake(c.owner,'初期仕様です。',project=p,bounded=True)
    assert len(canonical(result))<65536 and result['workflow']['phase']=='requirements'
    assert c.k.source_read(c.owner,result['source']['id'])['content']=='初期仕様です。'
    assert result['mandatory_notifications']['total']==30
    assert c.s.one('SELECT sum(displayed) AS n FROM inbox')['n']==0
