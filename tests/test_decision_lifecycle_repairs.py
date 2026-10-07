"""Decision lifecycle regressions at public RPC, native and review boundaries."""
from __future__ import annotations

import copy
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from daikibo.common import Actor, Fault, canonical, digest, parse_json, timestamp
from daikibo.db import SCHEMA_VERSION, Store
from daikibo.knowledge_history import validate_specifications
from conftest import make_task


def _proposal(ref, **extra):
    return {'title':'Clarify behavior','reason':'Choose the supported behavior',
            'options':['approve','keep_existing'],'recommendation':'approve',
            'refs':[ref],'requirement_affecting':True,**extra}


def _accepted_requirement(c, project, title, statement, **extra):
    source=c.k.source(c.owner,project,'Source for '+title)
    proposed=c.k.propose(c.owner,project,'requirement',{'title':title,'statement':statement,
        'acceptance':['AC-'+title.upper().replace(' ','-')],'source_refs':[source['id']],**extra})
    c.k.accept(c.owner,proposed['id'],1)
    return proposed['id']


def _user_decision(c, project, artifact, *, statement=None, constraints=None,
                   source_answer='I approve this exact change', auto_answer=False):
    source=c.k.source(c.owner,project,'The user requests this exact change.')
    old=c.k.artifact(c.owner,artifact)
    after=dict(old['body'])
    if statement is not None:after['statement']=statement
    if constraints is not None:after['constraints']=constraints
    change=c.p.change(c.owner,project,{'title':'User change','origin':'user',
        'reason':'Apply the requested requirement change','source':source['id'],
        'affected':[artifact],'evidence':[source['id']],
        'deltas':[{'artifact':artifact,'expected_revision':old['revision'],'body':after}]})
    decision=c.p.propose_decision(c.owner,project,_proposal(artifact,change=change['id']))
    if auto_answer:
        c.p.respond(c.owner,decision['id'],decision['digest'],'approve',source_answer)
    else:
        answer=c.k.source(c.owner,project,source_answer,'answer:'+decision['id'])
        c.p.respond(c.owner,decision['id'],decision['digest'],'approve',source_answer,source=answer['id'])
    return change['id'],decision['id']


def _provisional_decision(c, project, artifact, *, expires=None):
    body=_proposal(artifact,requirement_affecting=False,provisional=True,reversible=True,
                   expires=expires if expires is not None else timestamp()+600)
    review=c.rt.review(c.owner,project,'decision_proposal','fixture',proposal=body)
    return c.p.propose_decision(c.owner,project,{**body,'consistency_receipt':review['receipt']})


def _linked_change(c, project, artifact, statement):
    source=c.k.source(c.owner,project,'The user requests: '+statement)
    current=c.k.artifact(c.owner,artifact)
    change=c.p.change(c.owner,project,{'title':'User-requested revision','origin':'user',
        'reason':'Apply the exact user request','source':source['id'],'affected':[artifact],
        'evidence':[source['id']],'deltas':[{'artifact':artifact,
            'expected_revision':current['revision'],'body':{**current['body'],'statement':statement}}]})
    return change['id'],current['body']


def _register_coverage_reviewer(c, tmp_path, name='lifecycle-reviewer'):
    script=tmp_path/(name+'.py')
    script.write_text('''import json,sys\np=json.load(sys.stdin)\nc=p.get("context",{})\nrefs=set()\ndef walk(v):\n if isinstance(v,dict):\n  if isinstance(v.get("artifact"),str): refs.add(v["artifact"])\n  if isinstance(v.get("affected"),list): refs.update(x for x in v["affected"] if isinstance(x,str))\n  if isinstance(v.get("refs"),list): refs.update(x for x in v["refs"] if isinstance(x,str))\n  if isinstance(v.get("id"),str) and v["id"].startswith(("REQUIREMENT-","ARTIFACT-","INTERFACE-","DESIGN-")): refs.add(v["id"])\n  for x in v.values(): walk(x)\n elif isinstance(v,list):\n  for x in v: walk(x)\nwalk(c)\nprint(json.dumps({"verdict":"pass","rationale":"Deterministic test double only.","covered":c.get("required_coverage",[]),"findings":[],"observations":[{"ref":r,"detail":"Fixture observed this exact retained artifact."} for r in sorted(refs)],"dispositions":[]}))\n''')
    c.rt.adapters.register(c.owner,name,'fixture',sys.executable,[str(script)])
    return name


def _register_verdict_reviewer(c, tmp_path, verdict, name):
    script=tmp_path/(name+'.py')
    script.write_text('''import json,sys
p=json.load(sys.stdin);c=p.get("context",{});refs=c.get("proposal",{}).get("refs",[])
print(json.dumps({"verdict":'''+repr(verdict)+''',"rationale":"Deterministic regression fixture.","covered":c.get("required_coverage",[]),"findings":[],"observations":[{"ref":x,"detail":"Fixture reviewed the exact bound context."} for x in refs] or [{"ref":p.get("subject","review"),"detail":"Fixture reviewed the exact bound context."}],"dispositions":[]}))
''')
    c.rt.adapters.register(c.owner,name,'fixture',sys.executable,[str(script)])
    return name


def _request(c, method, params, ident):
    return c.request(None,{'id':ident,'method':method,'params':params})


def test_decision_batch_is_reviewed_once_and_applied_atomically_through_public_routes(full,full_project,tmp_path):
    c=full;project,_,first,_=full_project
    second=_accepted_requirement(c,project,'Independent','Independent behavior')
    first_change,first_decision=_user_decision(c,project,first,statement='Returns the exact integer sum')
    second_change,second_decision=_user_decision(c,project,second,statement='Preserves independent behavior',
                                                 auto_answer=True,source_answer='Yes, preserve it.')
    # Native action admission exposes the same batch preparation route used by
    # the public controller, while the answer and consistency review remain
    # explicit human/evidence gates.
    c.native.attach(c.owner,'decision-batch-session',str(full_project[3]),project=project,
                    register_repository=False)
    actions=c.native.actions(c.owner,'decision-batch-session',[{'method':'decision.batch_prepare',
        'params':{'project':project,'decisions':[first_decision,second_decision]}}])
    assert actions['all_applied']
    batch=actions['actions'][0]['result']
    metadata=_request(c,'decision.batch_get',{'batch':batch['id']},'batch-metadata')
    # The bounded reader uses character offsets and a byte budget; reconstruct
    # Unicode across page boundaries and prove it matches the exact digest.
    pages=[];offset=0
    while True:
        page=_request(c,'decision.batch_read',{'batch':batch['id'],'expected_digest':metadata['read_digest'],
            'offset':offset,'byte_budget':256},'batch-page-'+str(offset))
        pages.append(page['content']);offset=page['end']
        if page['next_offset'] is None:break
    raw=''.join(pages)
    assert digest(raw.encode('utf-8'))==metadata['read_digest']
    packet=parse_json(raw)
    assert packet['baseline']['artifacts'] and packet['final']['artifacts']
    assert set(packet['required_coverage']) >= {'decision-member:'+first_decision,
                                                'decision-member:'+second_decision}
    reviewer=_register_coverage_reviewer(c,tmp_path)
    review=c.rt.review(c.owner,batch['id'],'consistency',reviewer)
    result=_request(c,'decision.batch_apply',{'batch':batch['id'],'review_receipt':review['receipt']},'batch-apply')
    assert result['atomic'] is True
    assert result['changed_artifacts']==sorted([first,second])
    assert c.k.artifact(c.owner,first)['revision']==2
    assert c.k.artifact(c.owner,second)['revision']==2
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(first_decision,))['status']=='applied'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(second_decision,))['status']=='applied'
    assert c.p.decision_batch_get(c.owner,batch['id'])['status']=='applied'
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        c.s.execute('UPDATE decision_batches SET body=? WHERE id=?',(raw,batch['id']))
    exported=c.history.export_current(c.owner,project)
    assert len(exported['decision_batches'])==1
    assert validate_specifications(exported)['decision_batches']==1
    corrupted=copy.deepcopy(exported)
    archived=corrupted['decision_batches'][0]
    archived['body']['members'][0]['answer_evidence']['body']['quote']='unrelated quotation'
    archived['digest']=digest(archived['body'])
    archived['result']['batch_digest']=archived['digest']
    with pytest.raises(Fault,match='exact trusted archived source'):
        validate_specifications(corrupted)
    malformed=copy.deepcopy(exported)
    archived=malformed['decision_batches'][0]
    archived['body']['members'][0]='not-a-member'
    archived['digest']=digest(archived['body'])
    archived['result']['batch_digest']=archived['digest']
    with pytest.raises(Fault,match='members or coverage are malformed'):
        validate_specifications(malformed)
    # A new Control opens the same on-disk packet with its immutable body and
    # applied result intact, independently of the in-memory controller.
    from daikibo.control import Control
    home=c.s.home
    c.close()
    reopened=Control(home,mode='validation',start_workers=False)
    try:
        loaded=reopened.p.decision_batch_get(reopened.sec.authenticate(None),batch['id'])
        assert loaded['digest']==batch['digest'] and loaded['status']=='applied'
        assert loaded['result']['members']==[first_decision,second_decision]
    finally:
        reopened.close()


def test_batch_rejects_overlapping_deltas_and_inconsistent_common_final_state(full,full_project):
    c=full;project,_,first,_=full_project
    same_a,same_d1=_user_decision(c,project,first,statement='One meaning')
    # Create an independently answered proposal for the same exact revision.
    same_b,same_d2=_user_decision(c,project,first,statement='Another meaning')
    with pytest.raises(Fault) as overlap:
        c.p.decision_batch_prepare(c.owner,project,[same_d1,same_d2])
    assert overlap.value.code=='overlapping_batch_delta'
    assert c.k.artifact(c.owner,first)['revision']==1
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(same_d1,))['status']=='decision_received'

    # Two disjoint changes still fail when their projected accepted
    # requirements contradict one another.
    second=_accepted_requirement(c,project,'Mode','Select a mode')
    first_change,first_decision=_user_decision(c,project,first,constraints={'mode':'sync'})
    second_change,second_decision=_user_decision(c,project,second,constraints={'mode':'async'})
    with pytest.raises(Fault) as conflict:
        c.p.decision_batch_prepare(c.owner,project,[first_decision,second_decision])
    assert conflict.value.code=='secondary_conflict'
    assert c.k.artifact(c.owner,first)['revision']==1
    assert c.k.artifact(c.owner,second)['revision']==1


def test_batch_does_not_hide_new_conflict_key_on_an_already_conflicting_pair(full,full_project):
    c=full;project,_,first,_=full_project
    second=_accepted_requirement(c,project,'Competing','Competing mode')
    for artifact,body in ((first,{'x':'old-a','y':'same'}),(second,{'x':'old-b','y':'same'})):
        current=c.k.artifact(c.owner,artifact)
        c.k._revise(c.owner,current,current['revision'],{**current['body'],'constraints':body},
                    'Create a historical unrelated conflict','accepted')
    first_change,first_decision=_user_decision(c,project,first,constraints={'x':'new-a','y':'left'})
    second_change,second_decision=_user_decision(c,project,second,constraints={'x':'new-b','y':'right'})
    with pytest.raises(Fault) as conflict:
        c.p.decision_batch_prepare(c.owner,project,[first_decision,second_decision])
    assert conflict.value.code=='secondary_conflict'
    assert c.k.artifact(c.owner,first)['revision']==2
    assert c.k.artifact(c.owner,second)['revision']==2


def test_batch_cannot_leave_a_preexisting_conflict_on_an_affected_artifact(full,full_project):
    c=full;project,_,first,_=full_project
    second=_accepted_requirement(c,project,'Existing conflict peer','Conflicting mode')
    third=_accepted_requirement(c,project,'Independent peer','Independent behavior')
    for artifact,value in ((first,'old-a'),(second,'old-b')):
        current=c.k.artifact(c.owner,artifact)
        c.k._revise(c.owner,current,current['revision'],{**current['body'],'constraints':{'mode':value}},
                    'Record a preexisting contradiction','accepted')
    _,first_decision=_user_decision(c,project,first,constraints={'mode':'new-a'})
    _,third_decision=_user_decision(c,project,third,statement='Independent edit')
    with pytest.raises(Fault) as conflict:
        c.p.decision_batch_prepare(c.owner,project,[first_decision,third_decision])
    assert conflict.value.code=='secondary_conflict'
    assert c.k.artifact(c.owner,first)['revision']==2
    assert c.k.artifact(c.owner,third)['revision']==1


def test_batch_review_requires_every_member_and_rejects_a_stale_baseline(full,full_project,tmp_path):
    c=full;project,_,first,_=full_project
    second=_accepted_requirement(c,project,'Other','Other behavior')
    _,d1=_user_decision(c,project,first,statement='Updated first')
    _,d2=_user_decision(c,project,second,statement='Updated second')
    prepared=c.p.decision_batch_prepare(c.owner,project,[d1,d2])
    ordinary=c.rt.review(c.owner,prepared['id'],'consistency','fixture')
    with pytest.raises(Fault) as uncovered:
        c.p.decision_batch_apply(c.owner,prepared['id'],ordinary['receipt'])
    assert uncovered.value.code=='incomplete_review_coverage'
    reviewer=_register_coverage_reviewer(c,tmp_path)
    review=c.rt.review(c.owner,prepared['id'],'consistency',reviewer)
    # Even an exact pass is stale after any member of the frozen common
    # baseline changes; no first member may be partially committed.
    current=c.k.artifact(c.owner,first)
    c.k._revise(c.owner,current,current['revision'],{**current['body'],'statement':'Concurrent update'},
                'Concurrent baseline change','accepted')
    with pytest.raises(Fault) as stale:
        c.p.decision_batch_apply(c.owner,prepared['id'],review['receipt'])
    assert stale.value.code=='stale_decision_batch'
    assert c.k.artifact(c.owner,second)['revision']==1
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(d1,))['status']=='decision_received'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(d2,))['status']=='decision_received'


def test_external_batch_material_is_read_through_the_advertised_subprocess_route(full,full_project,tmp_path,monkeypatch):
    c=full;project,_,first,root=full_project
    decisions=[]
    # A bounded source quotation makes the immutable batch exceed the inline
    # prompt threshold without relying on generated packet internals.
    for index in range(9):
        artifact=first if index==0 else _accepted_requirement(c,project,f'Large {index}',f'Independent behavior {index}')
        answer=(f'Approved exact member {index}. '+('x'*79000))
        _,decision=_user_decision(c,project,artifact,statement=f'Independent update {index}',source_answer=answer)
        decisions.append(decision)
    batch=c.p.decision_batch_prepare(c.owner,project,decisions)
    packet=parse_json(c.s.one('SELECT body FROM decision_batches WHERE id=?',(batch['id'],))['body'])
    external='decision-batch-material:'+batch['id']
    assert external in packet['required_coverage']
    script=tmp_path/'batch_reader.py'
    script.write_text('''import hashlib,json,subprocess,sys
p=json.load(sys.stdin);c=p['context'];a=c['read_access'];r=c['decision_batch_ref']
parts=[];offset=0
while True:
 q={'batch':r['id'],'expected_digest':r['digest'],'offset':offset,'byte_budget':16000}
 page=json.loads(subprocess.check_output(a['command']+['call','decision.batch_read','--json',json.dumps(q)],cwd='/tmp'))
 parts.append(page['content']);offset=page['end']
 if page['next_offset'] is None:break
raw=''.join(parts)
actual=hashlib.sha256(raw.encode('utf-8')).hexdigest()
assert actual==r['digest']
print(json.dumps({'verdict':'pass','rationale':'Protocol fixture read the exact bounded packet.','covered':c['required_coverage'],'findings':[],'observations':[{'ref':r['id'],'detail':'Read '+str(len(raw))+' exact canonical bytes: '+actual}],'dispositions':[]}))
''')
    import daikibo
    monkeypatch.setenv('PYTHONPATH',str(Path(daikibo.__file__).resolve().parent.parent))
    c.rt.adapters.register(c.owner,'batch-reader','fixture',sys.executable,[str(script)])
    from daikibo.rpc import Server
    with tempfile.TemporaryDirectory(prefix='batch-review-rpc-') as folder:
        server=Server(c,Path(folder)/'socket')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            reviewed=c.rt.review(c.owner,batch['id'],'consistency','batch-reader')
            assert external in reviewed['result']['covered']
            assert str(len(canonical(packet))) in reviewed['result']['observations'][0]['detail']
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close()


def test_legacy_exact_response_quote_can_be_bound_into_batch_without_reasking(full,full_project,tmp_path,monkeypatch):
    c=full;project,_,first,_=full_project
    second=_accepted_requirement(c,project,'Legacy proof peer','Another independent behavior')
    _,d1=_user_decision(c,project,first,source_answer='Exact retained answer for first')
    _,d2=_user_decision(c,project,second,source_answer='Exact retained answer for second')
    original=c.p.response_evidence
    def old_shape(decision):
        result=original(decision)
        if result and decision==d1:
            body=dict(result['body']);body.pop('source_digest',None)
            return {**result,'body':body}
        return result
    monkeypatch.setattr(c.p,'response_evidence',old_shape)
    batch=c.p.decision_batch_prepare(c.owner,project,[d1,d2])
    frozen=parse_json(c.s.one('SELECT body FROM decision_batches WHERE id=?',(batch['id'],))['body'])
    member=next(m for m in frozen['members'] if m['decision']==d1)
    assert member['answer_evidence']['legacy_source_digest_derived'] is True
    assert member['answer_evidence']['body']['source_digest']==member['source']['digest']
    reviewer=_register_coverage_reviewer(c,tmp_path)
    review=c.rt.review(c.owner,batch['id'],'consistency',reviewer)
    result=c.p.decision_batch_apply(c.owner,batch['id'],review['receipt'])
    assert result['atomic'] and set(result['members'])=={d1,d2}


def test_failed_member_rolls_back_the_entire_batch_transaction(full,full_project,tmp_path,monkeypatch):
    c=full;project,_,first,_=full_project
    second=_accepted_requirement(c,project,'Rollback peer','Peer behavior')
    _,d1=_user_decision(c,project,first,statement='First replacement')
    _,d2=_user_decision(c,project,second,statement='Second replacement')
    batch=c.p.decision_batch_prepare(c.owner,project,[d1,d2])
    reviewer=_register_coverage_reviewer(c,tmp_path)
    review=c.rt.review(c.owner,batch['id'],'consistency',reviewer)
    original=c.p._apply_change;calls=[]
    def fail_second(*args,**kwargs):
        calls.append(args[1])
        if len(calls)==2:raise Fault('injected_batch_failure','Fail after the first member mutation')
        return original(*args,**kwargs)
    monkeypatch.setattr(c.p,'_apply_change',fail_second)
    with pytest.raises(Fault,match='Fail after the first member mutation'):
        c.p.decision_batch_apply(c.owner,batch['id'],review['receipt'])
    assert c.k.artifact(c.owner,first)['revision']==1
    assert c.k.artifact(c.owner,second)['revision']==1
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(d1,))['status']=='decision_received'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(d2,))['status']=='decision_received'
    assert c.p.decision_batch_get(c.owner,batch['id'])['status']=='prepared'
    assert not c.s.one("SELECT id FROM events WHERE kind='decision_batch_applied' AND json_extract(body,'$.id')=?",(batch['id'],))


def test_delta_terminalizes_only_its_pending_decisions_and_refreshes_exact_notice(full,full_project):
    c=full;project,_,artifact,_=full_project;task=make_task(c,full_project)
    change,decision=_user_decision(c,project,artifact,statement='First proposed meaning')
    answer=c.s.one('SELECT source FROM decisions WHERE id=?',(decision,))['source']
    conflict=c.p.conflict(c.owner,project,[artifact],'Keep a separate conflict blocker',['keep_existing'])
    c.s.execute("INSERT INTO blocks VALUES(?,?,?,?)",(task,'changed_input','input-1','Input changed'))
    old_body=parse_json(c.s.one('SELECT body FROM changes WHERE id=?',(change,))['body'])
    old_delta=old_body['deltas'][0]
    revised={**old_delta['body'],'statement':'A later, exact replacement proposal'}
    updated=c.p.set_delta(c.owner,change,1,[{**old_delta,'body':revised}],'Revise the proposal')
    decision_row=c.s.one('SELECT status,source,response FROM decisions WHERE id=?',(decision,))
    assert decision_row=={'status':'superseded','source':answer,'response':'approve'}
    blocks={(r['kind'],r['ref']) for r in c.s.all('SELECT kind,ref FROM blocks WHERE task=?',(task,))}
    assert ('decision',decision) not in blocks
    assert ('change',change) in blocks and ('conflict',conflict['id']) in blocks
    assert ('changed_input','input-1') in blocks
    notice=c.s.one("SELECT body,status FROM inbox WHERE project=? AND kind='product_decision' AND ref=?",
                   (project,change))
    published=parse_json(notice['body'])
    assert notice['status']=='open' and published['revision']==2
    assert published['binding']==updated['binding'] and published['body']!=old_body
    assert published['body']['deltas'][0]['body']==revised
    assert c.s.one("SELECT status FROM inbox WHERE ref=? AND kind='product_decision'",(decision,))['status']=='acknowledged'
    assert c.p.response_evidence(decision)['body']['source']==answer
    # A second terminalization pass cleans an orphan decision block and notice
    # without retargeting its status, source or historical response evidence.
    c.s.execute('INSERT INTO blocks VALUES(?,?,?,?)',(task,'decision',decision,'Orphaned historical block'))
    c.g.inbox(project,'product_decision',decision,parse_json(c.s.one('SELECT body FROM decisions WHERE id=?',(decision,))['body']))
    c.p.withdraw(c.owner,change,'Superseded by a later input',None)
    assert c.s.one('SELECT status,source FROM decisions WHERE id=?',(decision,))=={'status':'superseded','source':answer}
    assert not c.s.one("SELECT 1 FROM blocks WHERE kind='decision' AND ref=?",(decision,))
    assert not c.s.one("SELECT 1 FROM inbox WHERE ref=? AND kind='product_decision' AND status='open'",(decision,))
    assert c.s.one("SELECT 1 FROM blocks WHERE kind='conflict' AND ref=?",(conflict['id'],))
    assert c.s.one("SELECT 1 FROM blocks WHERE kind='changed_input' AND ref='input-1'")


def test_change_revision_does_not_retroactively_terminalize_an_applied_decision(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    reviewer=_register_coverage_reviewer(c,tmp_path)
    change,decision=_user_decision(c,project,artifact,statement='Approved meaning')
    review=c.rt.review(c.owner,decision,'consistency',reviewer)
    c.p.apply_decision(c.owner,decision,review['receipt'])
    current=c.k.artifact(c.owner,artifact)
    delta={'artifact':artifact,'expected_revision':current['revision'],
           'body':{**current['body'],'statement':'New proposal after application'}}
    row=c.s.one('SELECT revision FROM changes WHERE id=?',(change,))
    c.p.set_delta(c.owner,change,row['revision'],[delta],'Later design revision')
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision,))['status']=='applied'


def test_transitive_supersession_binds_and_terminalizes_every_ancestor(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    reviewer=_register_coverage_reviewer(c,tmp_path)
    first=c.p.propose_decision(c.owner,project,_proposal(artifact))
    a_source=c.k.source(c.owner,project,'Approve the first proposal')
    c.p.respond(c.owner,first['id'],first['digest'],'approve','Approve the first proposal',source=a_source['id'])
    first_review=c.rt.review(c.owner,first['id'],'consistency',reviewer)
    c.p.apply_decision(c.owner,first['id'],first_review['receipt'])
    second=c.p.propose_decision(c.owner,project,_proposal(artifact,supersedes=first['id']))
    b_source=c.k.source(c.owner,project,'Approve the second proposal')
    c.p.respond(c.owner,second['id'],second['digest'],'approve','Approve the second proposal',source=b_source['id'])
    second_review=c.rt.review(c.owner,second['id'],'consistency',reviewer)
    c.p.apply_decision(c.owner,second['id'],second_review['receipt'])
    third=c.p.propose_decision(c.owner,project,_proposal(artifact,supersedes=second['id']))
    assert [row['id'] for row in third['body']['supersession_closure']]==[second['id'],first['id']]
    c_source=c.k.source(c.owner,project,'Approve the third proposal')
    c.p.respond(c.owner,third['id'],third['digest'],'approve','Approve the third proposal',source=c_source['id'])
    material_project,material=c.p.decision_review_material(third['id'])
    assert material_project==project
    assert [row['id'] for row in material['supersession_closure']]==[second['id'],first['id']]
    review=c.rt.review(c.owner,third['id'],'consistency',reviewer)
    c.p.apply_decision(c.owner,third['id'],review['receipt'])
    for old in (first,second):
        row=c.s.one('SELECT status,source FROM decisions WHERE id=?',(old['id'],))
        assert row['status']=='superseded' and row['source'] is not None
        assert c.s.one("SELECT status FROM inbox WHERE ref=? AND kind='product_decision'",(old['id'],))['status']=='acknowledged'


def test_long_supersession_closure_stays_flat_and_applies_every_ancestor(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    chain=[];previous=None
    for index in range(14):
        proposal=_proposal(artifact,title=f'Proposal {index}',reason=f'Chain member {index}',
                          **({'supersedes':previous} if previous else {}))
        decision=c.p.propose_decision(c.owner,project,proposal)
        chain.append(decision);previous=decision['id']
    final=chain[-1]
    closure=final['body']['supersession_closure']
    assert [entry['id'] for entry in closure]==[decision['id'] for decision in reversed(chain[:-1])]
    assert all('supersession_closure' not in entry['body'] for entry in closure)
    assert len(canonical(final['body']))<50000
    answer=c.k.source(c.owner,project,'Approve the complete replacement chain.')
    c.p.respond(c.owner,final['id'],final['digest'],'approve','Approve the complete replacement chain.',source=answer['id'])
    reviewer=_register_coverage_reviewer(c,tmp_path,'long-chain-review')
    review=c.rt.review(c.owner,final['id'],'consistency',reviewer)
    assert c.p.apply_decision(c.owner,final['id'],review['receipt'])['status']=='applied'
    assert all(c.s.one('SELECT status FROM decisions WHERE id=?',(item['id'],))['status']=='superseded'
               for item in chain[:-1])


def test_feasibility_escalation_requires_latest_same_role_review(full,full_project):
    c=full;project,_,artifact,_=full_project
    source=c.k.source(c.owner,project,'Evidence to assess this local design option.')
    change=c.p.change(c.owner,project,{'title':'Explore feasibility','origin':'implementation',
        'reason':'Test a local design option','affected':[artifact],'evidence':[source['id']]})
    old=c.rt.review(c.owner,change['id'],'feasibility','fixture')
    latest=c.rt.review(c.owner,change['id'],'feasibility','fixture')
    attempt={'hypothesis':'Test a local option','alternatives':['alternate design'],
             'evidence':[old['receipt']],'outcome':'no_solution_found','remaining_unknown':'None'}
    with pytest.raises(Fault) as stale:
        c.p.attempt(c.owner,change['id'],'local_repair',{**attempt,'review_receipt':old['receipt']})
    assert stale.value.code=='stale_evidence'
    result=c.p.attempt(c.owner,change['id'],'local_repair',{**attempt,'review_receipt':latest['receipt']})
    assert result['stage']=='module_replan'


def test_decision_apply_rejects_an_older_pass_after_a_newer_fail(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    _,decision=_user_decision(c,project,artifact,statement='Exact reviewed change')
    passing=_register_coverage_reviewer(c,tmp_path,'apply-pass')
    failing=_register_verdict_reviewer(c,tmp_path,'fail','apply-fail')
    old=c.rt.review(c.owner,decision,'consistency',passing)
    newer=c.rt.review(c.owner,decision,'consistency',failing)
    assert newer['result']['verdict']=='fail'
    with pytest.raises(Fault) as stale:
        c.p.apply_decision(c.owner,decision,old['receipt'])
    assert stale.value.code=='stale_evidence'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision,))['status']=='decision_received'


def test_deferred_decision_resumes_after_a_new_native_presentation(full,full_project,tmp_path):
    c=full;project,_,artifact,root=full_project
    c.native.attach(c.owner,'deferred-session',str(root),project=project,register_repository=False)
    decision=c.p.propose_decision(c.owner,project,_proposal(artifact))
    first=c.native.present_decision(c.owner,'deferred-session',decision['id'])
    pause=c.native.input(c.owner,'deferred-session','I need more time to decide')['source']
    result=c.native.respond(c.owner,'deferred-session',decision['id'],first['expected_digest'],pause,
                            'defer','I need more time to decide')
    assert result['status']=='deferred'
    second=c.native.present_decision(c.owner,'deferred-session',decision['id'])
    with pytest.raises(Fault) as stale:
        c.native.respond(c.owner,'deferred-session',decision['id'],second['expected_digest'],pause,
                         'approve','I need more time to decide')
    assert stale.value.code=='stale_user_input'
    answer=c.native.input(c.owner,'deferred-session','I approve the exact proposal now')['source']
    result=c.native.respond(c.owner,'deferred-session',decision['id'],second['expected_digest'],answer,
                            'approve','I approve the exact proposal now')
    assert result['status']=='decision_received'
    assert c.p.response_evidence(decision['id'])['body']['source']==answer
    assert c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='human_response_observed' AND json_extract(body,'$.decision')=?",
                   (decision['id'],))['n']==2
    reviewer=_register_coverage_reviewer(c,tmp_path,'deferred-apply')
    review=c.rt.review(c.owner,decision['id'],'consistency',reviewer)
    assert c.p.apply_decision(c.owner,decision['id'],review['receipt'])['status']=='applied'


def test_parallel_direct_decision_apply_has_one_effect(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    _,decision=_user_decision(c,project,artifact,statement='Apply once')
    reviewer=_register_coverage_reviewer(c,tmp_path,'parallel-apply')
    review=c.rt.review(c.owner,decision,'consistency',reviewer)
    barrier=threading.Barrier(2)
    def invoke():
        barrier.wait()
        try:return c.p.apply_decision(c.owner,decision,review['receipt'])['status']
        except Fault as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:invoke(),range(2)))
    assert results.count('applied')==1
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision,))['status']=='applied'
    assert c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='decision_applied' AND json_extract(body,'$.decision')=?",
                   (decision,))['n']==1


def test_title_only_user_repair_requires_independent_equivalence_review_but_no_second_human_decision(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    reviewer=_register_coverage_reviewer(c,tmp_path)
    source=c.k.source(c.owner,project,'Please rename the requirement for display only.')
    current=c.k.artifact(c.owner,artifact)
    renamed={**current['body'],'title':'A clearer display title'}
    assert c.p._is_display_metadata_repair(current,{'artifact':artifact,'expected_revision':1,'body':renamed})
    change=c.p.change(c.owner,project,{'title':'Display title cleanup','origin':'user',
        'reason':'The user clarified the label','source':source['id'],'affected':[artifact],
        'evidence':[source['id']],'deltas':[{'artifact':artifact,'expected_revision':1,'body':renamed}]})
    assert change['stage']=='local_repair'
    feasibility=c.rt.review(c.owner,change['id'],'feasibility','fixture')
    c.p.attempt(c.owner,change['id'],'local_repair',{'hypothesis':'Apply the requested display title',
        'alternatives':['Retain the existing title'],'evidence':[feasibility['receipt']],
        'outcome':'solution','remaining_unknown':''})
    _,material=c.p.change_review_material(change['id'])
    assert 'display-metadata-equivalence:'+artifact in material['required_coverage']
    _,_,_,review_context,_=c.rt._subject(c.owner,change['id'],'consistency')
    assert 'display-metadata-equivalence:'+artifact in review_context['required_coverage']
    # The fixture assesses only the narrow test contract. The production prompt
    # explicitly says a title may carry product meaning and requires the exact
    # before/after, source and marker citation.
    review=c.rt.review(c.owner,change['id'],'consistency',reviewer)
    assert 'display-metadata-equivalence:'+artifact in review['result']['covered']
    c.p.apply_technical_change(c.owner,change['id'],review['receipt'])
    assert c.k.artifact(c.owner,artifact)['body']['title']=='A clearer display title'
    assert not c.s.one("SELECT id FROM decisions WHERE json_extract(body,'$.change')=?",(change['id'],))


def test_title_plus_withdraw_does_not_enter_technical_repair_route(full,full_project):
    c=full;project,_,artifact,_=full_project
    source=c.k.source(c.owner,project,'The product requirement should be withdrawn.')
    current=c.k.artifact(c.owner,artifact)
    changed={**current['body'],'title':'Renamed while withdrawing'}
    change=c.p.change(c.owner,project,{'title':'Withdraw the product requirement','origin':'implementation',
        'reason':'Attempt a combined title and withdrawal','affected':[artifact],'evidence':[source['id']],
        'deltas':[{'artifact':artifact,'expected_revision':1,'body':changed,'withdraw':True}]})
    feasibility=c.rt.review(c.owner,change['id'],'feasibility','fixture')
    c.p.attempt(c.owner,change['id'],'local_repair',{'hypothesis':'Withdraw the requirement',
        'alternatives':['Keep it'],'evidence':[feasibility['receipt']],'outcome':'solution','remaining_unknown':''})
    consistency=c.rt.review(c.owner,change['id'],'consistency','fixture')
    with pytest.raises(Fault) as rejected:
        c.p.apply_technical_change(c.owner,change['id'],consistency['receipt'])
    assert rejected.value.code=='product_decision_required'
    assert c.k.artifact(c.owner,artifact)['status']=='accepted'
    assert c.k.artifact(c.owner,artifact)['revision']==1


def test_native_decision_source_order_uses_events_across_equal_or_rolled_back_clock(full,full_project,monkeypatch):
    c=full;project,_,artifact,root=full_project
    c.native.attach(c.owner,'sequence-session',str(root),project=project,register_repository=False)
    wall_clock=[100]
    for module in ('daikibo.knowledge','daikibo.planning','daikibo.native'):
        monkeypatch.setattr(module+'.timestamp',lambda:wall_clock[0])
    monkeypatch.setattr(c.sec,'clock',lambda:wall_clock[0])
    old=c.native.input(c.owner,'sequence-session','A preproposal source')['source']
    decision=c.p.propose_decision(c.owner,project,_proposal(artifact))
    shown=c.native.present_decision(c.owner,'sequence-session',decision['id'])
    with pytest.raises(Fault) as stale:
        c.native.respond(c.owner,'sequence-session',decision['id'],shown['expected_digest'],old,
                         'approve','A preproposal source')
    assert stale.value.code=='stale_user_input'
    # Roll the wall clock backward after presentation. Sequence numbers still
    # prove that this exact recorded turn followed the proposal and display.
    wall_clock[0]=-100
    new=c.native.input(c.owner,'sequence-session','A fresh approval')['source']
    response=c.native.respond(c.owner,'sequence-session',decision['id'],shown['expected_digest'],new,
                              'approve','A fresh approval')
    assert response['status']=='decision_received'


def test_native_provisional_answer_allows_exact_quote_correction_but_not_answer_reuse(full,full_project):
    c=full;project,_,artifact,root=full_project
    c.native.attach(c.owner,'provisional-session',str(root),project=project,register_repository=False)
    body=_proposal(artifact,requirement_affecting=False,provisional=True,reversible=True,
                   expires=timestamp()+600)
    review=c.rt.review(c.owner,project,'decision_proposal','fixture',proposal=body)
    decision=c.p.propose_decision(c.owner,project,{**body,'consistency_receipt':review['receipt']})
    shown=c.native.present_decision(c.owner,'provisional-session',decision['id'])
    source=c.native.input(c.owner,'provisional-session','Keep this reversible assumption')['source']
    result=c.native.respond(c.owner,'provisional-session',decision['id'],shown['expected_digest'],source,
                            'approve','Keep this reversible assumption')
    assert result['status']=='decision_received'
    count=lambda:c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='human_response_observed' AND json_extract(body,'$.decision')=?",
                         (decision['id'],))['n']
    assert count()==1
    # An exact retry is a no-op; a narrower exact quote from that same
    # retained source corrects evidence without requiring another answer.
    binding=c.p.decision_binding(decision['id'])
    repeated=c.native.respond(c.owner,'provisional-session',decision['id'],shown['expected_digest'],source,
                              'approve','Keep this reversible assumption')
    assert repeated['status']=='decision_received' and count()==1
    corrected=c.native.respond(c.owner,'provisional-session',decision['id'],shown['expected_digest'],source,
                                'approve','reversible assumption')
    assert corrected['status']=='decision_received' and count()==2
    assert c.p.response_evidence(decision['id'])['body']['quote']=='reversible assumption'
    assert c.p.decision_binding(decision['id'])!=binding
    # A new display invalidates the old source for a different answer. A new
    # source recorded after that display can change the choice.
    next_shown=c.native.present_decision(c.owner,'provisional-session',decision['id'])
    with pytest.raises(Fault) as stale:
        c.native.respond(c.owner,'provisional-session',decision['id'],next_shown['expected_digest'],source,
                         'keep_existing','Keep this reversible assumption')
    assert stale.value.code=='stale_user_input'
    fresh=c.native.input(c.owner,'provisional-session','Keep the current assumption as drafted')['source']
    changed=c.native.respond(c.owner,'provisional-session',decision['id'],next_shown['expected_digest'],fresh,
                              'keep_existing','Keep the current assumption as drafted')
    assert changed['status']=='decision_received' and count()==3


def test_public_answer_consumes_matching_native_source_across_routes(full,full_project):
    c=full;project,_,artifact,root=full_project
    c.native.attach(c.owner,'cross-route-session',str(root),project=project,register_repository=False)
    decision=c.p.propose_decision(c.owner,project,_proposal(artifact))
    shown=c.native.present_decision(c.owner,'cross-route-session',decision['id'])
    source=c.native.input(c.owner,'cross-route-session',
                          'I approve this proposal; keep_existing remains available')['source']
    c.p.respond(c.owner,decision['id'],shown['expected_digest'],'approve',
                'I approve this proposal',source=source)

    # The public decision.respond path has already consumed this same observed
    # answer even though native.presented.consumed was not updated by that RPC.
    with pytest.raises(Fault) as reused:
        c.native.respond(c.owner,'cross-route-session',decision['id'],shown['expected_digest'],source,
                         'keep_existing','I approve this proposal')
    assert reused.value.code=='stale_user_input'
    corrected=c.native.respond(c.owner,'cross-route-session',decision['id'],shown['expected_digest'],source,
                               'approve','approve')
    assert corrected['status']=='decision_received'
    assert c.p.response_evidence(decision['id'])['body']['quote']=='approve'

    # After an explicit fresh presentation, a newly recorded source may change
    # the choice as a new human answer.
    next_shown=c.native.present_decision(c.owner,'cross-route-session',decision['id'])
    fresh=c.native.input(c.owner,'cross-route-session','I choose keep_existing for this proposal')['source']
    changed=c.native.respond(c.owner,'cross-route-session',decision['id'],next_shown['expected_digest'],fresh,
                             'keep_existing','I choose keep_existing')
    assert changed['status']=='decision_received'
    assert c.decision_get(c.owner,decision['id'])['response']=='keep_existing'


def test_schema16_database_migrates_to_17_with_immutable_batch_table(tmp_path):
    home=tmp_path/'db'
    from daikibo.control import Control
    control=Control(home,mode='validation',start_workers=False)
    owner=control.sec.authenticate(None)
    project=control.k.create_project(owner,'Migration project')['id']
    source=control.k.source(owner,project,'Source retained across schema migration.')
    program=control.p.begin(owner,project,source['id'])['program']
    origin_before=control.s.one('SELECT * FROM program_origins WHERE program=?',(program,))
    events_before=control.s.all('SELECT id,seq,kind,body,mac FROM events ORDER BY seq')
    store=control.s
    assert store.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION==17
    store.execute('DROP TRIGGER decision_batches_no_delete')
    store.execute('DROP TRIGGER decision_batches_immutable')
    store.execute('DROP INDEX decision_batches_project')
    store.execute('DROP TABLE decision_batches')
    store.execute('PRAGMA user_version=16')
    control.close()
    migrated=Store(home)
    try:
        assert migrated.one('PRAGMA user_version')['user_version']==17
        assert migrated.one("SELECT name FROM sqlite_master WHERE type='table' AND name='decision_batches'")
        assert migrated.one("SELECT name FROM sqlite_master WHERE type='trigger' AND name='decision_batches_immutable'")
        assert migrated.one("SELECT name FROM sqlite_master WHERE type='trigger' AND name='decision_batches_no_delete'")
        assert migrated.one('SELECT * FROM program_origins WHERE program=?',(program,))==origin_before
        assert migrated.all('SELECT id,seq,kind,body,mac FROM events ORDER BY seq')==events_before
        from daikibo.program_origins import validate_origin_store
        assert validate_origin_store(migrated)['program_origins']==1
    finally:
        migrated.close()


def test_keep_existing_declines_linked_change_without_superseding_ancestor(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project;task=make_task(c,full_project)
    reviewer=_register_coverage_reviewer(c,tmp_path,'keep-existing-review')
    ancestor=c.p.propose_decision(c.owner,project,_proposal(artifact))
    first_source=c.k.source(c.owner,project,'Approve the already adopted interpretation')
    c.p.respond(c.owner,ancestor['id'],ancestor['digest'],'approve',
                'Approve the already adopted interpretation',source=first_source['id'])
    first_review=c.rt.review(c.owner,ancestor['id'],'consistency',reviewer)
    c.p.apply_decision(c.owner,ancestor['id'],first_review['receipt'])

    change,original_body=_linked_change(c,project,artifact,'A different interpretation requested later')
    sibling=c.p.propose_decision(c.owner,project,_proposal(artifact,change=change))
    selected=c.p.propose_decision(c.owner,project,
        _proposal(artifact,change=change,supersedes=ancestor['id']))
    answer=c.k.source(c.owner,project,'Keep the existing approved interpretation.',
                      'answer:'+selected['id'])
    c.p.respond(c.owner,selected['id'],selected['digest'],'keep_existing',
                'Keep the existing approved interpretation.',source=answer['id'])
    _,material=c.p.decision_review_material(selected['id'])
    assert material['selected_effect']=='keep_existing'
    review=c.rt.review(c.owner,selected['id'],'consistency',reviewer)
    task_epoch=c.w.task(c.owner,task)['epoch']
    assert c.p.apply_decision(c.owner,selected['id'],review['receipt'])=={
        'id':selected['id'],'status':'applied','task_revalidations':[]}

    assert c.k.artifact(c.owner,artifact)['revision']==1
    assert c.k.artifact(c.owner,artifact)['body']==original_body
    declined=parse_json(c.s.one('SELECT body FROM changes WHERE id=?',(change,))['body'])
    assert c.s.one('SELECT stage FROM changes WHERE id=?',(change,))['stage']=='withdrawn'
    assert declined['withdrawal']['decision']==selected['id']
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(ancestor['id'],))['status']=='applied'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(sibling['id'],))['status']=='superseded'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(selected['id'],))['status']=='applied'
    assert c.s.one('SELECT epoch FROM tasks WHERE id=?',(task,))['epoch']==task_epoch
    assert not c.s.one("SELECT 1 FROM blocks WHERE kind IN ('change','decision') AND ref IN (?, ?, ?)",
                       (change,sibling['id'],selected['id']))
    for ref in (change,sibling['id'],selected['id']):
        assert c.s.one("SELECT status FROM inbox WHERE ref=? AND kind='product_decision' ORDER BY id LIMIT 1",
                       (ref,))['status']=='acknowledged'


def test_keep_existing_batch_projection_matches_apply_and_archive(full,full_project,tmp_path):
    c=full;project,_,keep_artifact,_=full_project
    apply_artifact=_accepted_requirement(c,project,'Batch peer','An independent behavior')
    keep_change,_=_linked_change(c,project,keep_artifact,'Retain the existing independent behavior')
    keep_decision=c.p.propose_decision(c.owner,project,
        _proposal(keep_artifact,change=keep_change))
    keep_source=c.k.source(c.owner,project,'Keep the current behavior for this item.',
                           'answer:'+keep_decision['id'])
    c.p.respond(c.owner,keep_decision['id'],keep_decision['digest'],'keep_existing',
                'Keep the current behavior for this item.',source=keep_source['id'])
    apply_change,apply_decision=_user_decision(c,project,apply_artifact,
        statement='Apply the independently approved behavior')

    prepared=c.p.decision_batch_prepare(c.owner,project,[keep_decision['id'],apply_decision])
    packet=c.p.decision_batch_get(c.owner,prepared['id'])
    pages=[];offset=0
    while True:
        page=c.p.decision_batch_read(c.owner,prepared['id'],packet['read_digest'],
                                     offset=offset,byte_budget=12000)
        pages.append(page['content']);offset=page['end']
        if page['next_offset'] is None:break
    raw=''.join(pages)
    material=parse_json(raw)
    member=next(row for row in material['members'] if row['decision']==keep_decision['id'])
    assert member['selected_effect']=='keep_existing'
    projected={row['id']:row for row in material['final']['artifacts']}
    assert projected[keep_artifact]['revision']==1
    assert projected[apply_artifact]['revision']==2
    assert material['final']['changes'][keep_change]['stage']=='withdrawn'
    assert material['effects']['changed_artifacts']==[apply_artifact]

    reviewer=_register_coverage_reviewer(c,tmp_path,'keep-batch-review')
    review=c.rt.review(c.owner,prepared['id'],'consistency',reviewer)
    result=c.p.decision_batch_apply(c.owner,prepared['id'],review['receipt'])
    assert result['changed_artifacts']==[apply_artifact]
    assert c.k.artifact(c.owner,keep_artifact)['revision']==1
    assert c.k.artifact(c.owner,apply_artifact)['revision']==2
    assert c.s.one('SELECT stage FROM changes WHERE id=?',(keep_change,))['stage']=='withdrawn'
    assert c.s.one('SELECT stage FROM changes WHERE id=?',(apply_change,))['stage']=='ready_for_reimplementation'
    archived=c.history.export_current(c.owner,project)
    assert validate_specifications(archived)['decision_batches']==1


def test_side_effecting_custom_choices_require_fixed_explicit_effects(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    change,_=_linked_change(c,project,artifact,'Keep the accepted meaning unchanged')
    base={'title':'Choose linked change outcome','reason':'Select the user-requested effect',
          'options':['ship'],'recommendation':'ship','refs':[artifact],
          'requirement_affecting':True,'change':change}
    with pytest.raises(Fault) as missing:
        c.p.propose_decision(c.owner,project,base)
    assert missing.value.code=='choice_effect_required'
    with pytest.raises(Fault) as reserved:
        c.p.propose_decision(c.owner,project,{**base,'options':['approve'],
            'choice_effects':{'approve':'keep_existing'}})
    assert reserved.value.code=='invalid_choice_effect'
    with pytest.raises(Fault) as record_only:
        c.p.propose_decision(c.owner,project,{**base,'options':['ship','retain'],
            'choice_effects':{'ship':'record_only','retain':'keep_existing'}})
    assert record_only.value.code=='invalid_choice_effect'

    proposal=c.p.propose_decision(c.owner,project,{**base,'options':['ship','retain'],
        'choice_effects':{'ship':'accept','retain':'keep_existing'}})
    answer=c.k.source(c.owner,project,'Retain the current requirement.',
                      'answer:'+proposal['id'])
    c.p.respond(c.owner,proposal['id'],proposal['digest'],'retain',
                'Retain the current requirement.',source=answer['id'])
    assert c.p.decision_review_material(proposal['id'])[1]['selected_effect']=='keep_existing'
    reviewer=_register_coverage_reviewer(c,tmp_path,'custom-choice-review')
    review=c.rt.review(c.owner,proposal['id'],'consistency',reviewer)
    c.p.apply_decision(c.owner,proposal['id'],review['receipt'])
    assert c.k.artifact(c.owner,artifact)['revision']==1
    assert c.s.one('SELECT stage FROM changes WHERE id=?',(change,))['stage']=='withdrawn'

    record=c.p.propose_decision(c.owner,project,_proposal(artifact,
        options=['note_only'],recommendation='note_only',requirement_affecting=False))
    record_source=c.k.source(c.owner,project,'Record the selected option for the audit.')
    c.p.respond(c.owner,record['id'],record['digest'],'note_only',
                'Record the selected option for the audit.',source=record_source['id'])
    assert c.p.decision_review_material(record['id'])[1]['selected_effect']=='record_only'
    record_review=c.rt.review(c.owner,record['id'],'consistency',reviewer)
    c.p.apply_decision(c.owner,record['id'],record_review['receipt'])
    assert c.k.artifact(c.owner,artifact)['revision']==1


def test_exact_public_response_retry_is_noop_but_reused_source_cannot_change_choice(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    decision=c.p.propose_decision(c.owner,project,_proposal(artifact))
    source=c.k.source(c.owner,project,'I approve the exact proposal.',
                      'answer:'+decision['id'])
    params={'decision':decision['id'],'expected_digest':decision['digest'],'choice':'approve',
            'utterance':'I approve the exact proposal.','source':source['id']}
    _request(c,'decision.respond',params,'public-response-request')
    events_before=c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='human_response_observed' "
                          "AND json_extract(body,'$.decision')=?",(decision['id'],))['n']
    binding=c.p.decision_binding(decision['id'])
    reviewer=_register_coverage_reviewer(c,tmp_path,'response-retry-review')
    review=c.rt.review(c.owner,decision['id'],'consistency',reviewer)
    _request(c,'decision.respond',params,'public-response-request')
    _request(c,'decision.respond',params,'public-response-new-id')
    assert c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='human_response_observed' "
                   "AND json_extract(body,'$.decision')=?",(decision['id'],))['n']==events_before
    assert c.p.decision_binding(decision['id'])==binding
    assert c.p.apply_decision(c.owner,decision['id'],review['receipt'])['status']=='applied'

    revised=c.p.propose_decision(c.owner,project,_proposal(artifact))
    reused=c.k.source(c.owner,project,'Approve this proposal, then retain the current meaning.',
                      'answer:'+revised['id'])
    c.p.respond(c.owner,revised['id'],revised['digest'],'approve',
                'Approve this proposal',source=reused['id'])
    with pytest.raises(Fault) as stale:
        c.p.respond(c.owner,revised['id'],revised['digest'],'keep_existing',
                    'retain the current meaning',source=reused['id'])
    assert stale.value.code=='stale_user_input'
    fresh=c.k.source(c.owner,project,'I now choose to keep the existing meaning.',
                     'answer:'+revised['id']+':revision')
    c.p.respond(c.owner,revised['id'],revised['digest'],'keep_existing',
                'I now choose to keep the existing meaning.',source=fresh['id'])
    assert c.p.response_evidence(revised['id'])['body']['choice']=='keep_existing'


def test_native_response_cannot_change_choice_across_public_route_and_defer_needs_new_source(full,full_project):
    c=full;project,_,artifact,root=full_project
    c.native.attach(c.owner,'cross-route-choice',str(root),project=project,register_repository=False)
    decision=c.p.propose_decision(c.owner,project,_proposal(artifact))
    presented=c.native.present_decision(c.owner,'cross-route-choice',decision['id'])
    native_input=c.native.input(c.owner,'cross-route-choice','Approve this exact proposal.',turn_id='approve')
    c.native.respond(c.owner,'cross-route-choice',decision['id'],presented['expected_digest'],
                     native_input['source'],'approve','Approve this exact proposal.')
    with pytest.raises(Fault) as stale:
        c.p.respond(c.owner,decision['id'],decision['digest'],'keep_existing',
                    'Approve this exact proposal.',source=native_input['source'])
    assert stale.value.code=='stale_user_input'
    fresh=c.k.source(c.owner,project,'I changed my answer: keep the existing meaning.',
                     'answer:'+decision['id']+':public-revision')
    c.p.respond(c.owner,decision['id'],decision['digest'],'keep_existing',
                'I changed my answer: keep the existing meaning.',source=fresh['id'])
    assert c.p.response_evidence(decision['id'])['body']['source']==fresh['id']

    deferred=c.p.propose_decision(c.owner,project,_proposal(artifact))
    pause=c.k.source(c.owner,project,'I need more time.', 'answer:'+deferred['id'])
    c.p.respond(c.owner,deferred['id'],deferred['digest'],'defer','I need more time.',source=pause['id'])
    with pytest.raises(Fault) as reused:
        c.p.respond(c.owner,deferred['id'],deferred['digest'],'approve',
                    'I need more time.',source=pause['id'])
    assert reused.value.code=='stale_user_input'
    resumed=c.k.source(c.owner,project,'I have decided to approve the proposal.',
                       'answer:'+deferred['id']+':resumed')
    c.p.respond(c.owner,deferred['id'],deferred['digest'],'approve',
                'I have decided to approve the proposal.',source=resumed['id'])
    assert c.p.response_evidence(deferred['id'])['body']['source']==resumed['id']


def test_same_choice_quote_correction_is_allowed_but_source_none_is_a_new_observation(full,full_project):
    c=full;project,_,artifact,_=full_project
    decision=c.p.propose_decision(c.owner,project,_proposal(artifact))
    source=c.k.source(c.owner,project,
        'The user says approve this exact proposal and keep the current behavior.',
        'answer:'+decision['id'])
    c.p.respond(c.owner,decision['id'],decision['digest'],'approve',
                'approve this exact proposal',source=source['id'])
    c.p.respond(c.owner,decision['id'],decision['digest'],'approve',
                'keep the current behavior',source=source['id'])
    corrected=c.p.response_evidence(decision['id'])
    assert corrected['body']['source']==source['id']
    assert corrected['body']['quote']=='keep the current behavior'
    assert c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='human_response_observed' "
                   "AND json_extract(body,'$.decision')=?",(decision['id'],))['n']==2

    observed=c.p.propose_decision(c.owner,project,_proposal(artifact))
    utterance='I approve now.'
    first=c.p.respond(c.owner,observed['id'],observed['digest'],'approve',utterance)
    first_source=c.s.one('SELECT source FROM decisions WHERE id=?',(observed['id'],))['source']
    second=c.p.respond(c.owner,observed['id'],observed['digest'],'keep_existing',utterance)
    second_source=c.s.one('SELECT source FROM decisions WHERE id=?',(observed['id'],))['source']
    assert first['status']=='decision_received' and second['status']=='decision_received'
    assert second_source!=first_source
    assert c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='human_response_observed' "
                   "AND json_extract(body,'$.decision')=?",(observed['id'],))['n']==2


def test_provisional_expiry_auto_closes_old_notice_and_keeps_other_scoped_blocks(full,full_project):
    c=full;project,_,artifact,_=full_project;task=make_task(c,full_project)
    c.s.execute('INSERT INTO blocks VALUES(?,?,?,?)',(task,'changed_input','independent-input','Still needs review'))
    body=_proposal(artifact,requirement_affecting=False,provisional=True,reversible=True,
                   expires=timestamp()+600)
    review=c.rt.review(c.owner,project,'decision_proposal','fixture',proposal=body)
    decision=c.p.propose_decision(c.owner,project,{**body,'consistency_receipt':review['receipt']})
    notice=c.s.one("SELECT * FROM inbox WHERE project=? AND kind='provisional_decision' AND ref=?",
                   (project,decision['id']))
    with pytest.raises(Fault) as blocked:
        c.i.acknowledge(c.owner,notice['id'],'I have seen this provisional notice.',
                        expected_digest=digest(notice['body'].encode()))
    assert blocked.value.code=='adjudication_required'
    c.s.execute("UPDATE timers SET due=0 WHERE ref=? AND kind='decision_expiry'",(decision['id'],))
    c.w.reconcile(c.owner,project)
    rows=c.s.all('SELECT id,kind,severity,status FROM inbox WHERE project=? AND ref=? ORDER BY id',
                 (project,decision['id']))
    assert next(row for row in rows if row['kind']=='provisional_decision')['status']=='acknowledged'
    assert [(row['kind'],row['severity'],row['status']) for row in rows if row['status']=='open']==[
        ('decision','critical','open')]
    closed=c.s.one("SELECT body FROM events WHERE kind='notification_closed' AND json_extract(body,'$.item')=?",
                   (notice['id'],))
    assert parse_json(closed['body'])['user_acknowledgement'] is False
    assert c.s.one("SELECT 1 FROM blocks WHERE task=? AND kind='changed_input' AND ref='independent-input'",
                   (task,))


def test_public_request_commits_expiry_instead_of_rolling_it_back(full,full_project):
    c=full;project,_,artifact,_=full_project
    decision=_provisional_decision(c,project,artifact)
    timer=c.s.one("SELECT * FROM timers WHERE kind='decision_expiry' AND ref=?",(decision['id'],),True)
    source=c.k.source(c.owner,project,'I approve after the provisional deadline.')
    c.s.execute('UPDATE timers SET due=0 WHERE id=?',(timer['id'],))
    args={'decision':decision['id'],'expected_digest':decision['digest'],'choice':'approve',
          'utterance':'I approve after the provisional deadline.','source':source['id']}
    result=_request(c,'decision.respond',args,'late-provisional-response')
    assert result=={'id':decision['id'],'status':'expired','expired':True,
                    'answered':False,'must_reconcile':True}
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision['id'],))['status']=='expired'
    assert c.s.one("SELECT count(*) AS n FROM inbox WHERE ref=? AND kind='decision' AND severity='critical' AND status='open'",
                   (decision['id'],))['n']==1
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='decision_expired' AND json_extract(body,'$.decision')=?",
                   (decision['id'],))['n']==1
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='timer_fired' AND json_extract(body,'$.timer')=?",
                   (timer['id'],))['n']==1
    again=_request(c,'decision.respond',args,'late-provisional-retry')
    assert again['status']=='expired' and again['answered'] is False
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='decision_expired' AND json_extract(body,'$.decision')=?",
                   (decision['id'],))['n']==1


def test_deferred_provisional_expires_and_legacy_fired_timer_survives_clock_rollback(
        full,full_project,monkeypatch):
    c=full;project,_,artifact,_=full_project
    decision=_provisional_decision(c,project,artifact)
    timer=c.s.one("SELECT * FROM timers WHERE kind='decision_expiry' AND ref=?",(decision['id'],),True)
    pause=c.k.source(c.owner,project,'I need more time to decide.')
    c.p.respond(c.owner,decision['id'],decision['digest'],'defer','I need more time to decide.',source=pause['id'])
    # Simulate the legacy state in which the scheduler fired the timer but
    # left a deferred decision row. Its durable sequence event must prevent a
    # clock rollback from admitting a new answer or writing a duplicate event.
    c.s.execute('UPDATE timers SET due=10,fired=11 WHERE id=?',(timer['id'],))
    c.sec.event(project,'timer_fired','system',{'timer':timer['id'],'decision':decision['id'],
        'reason':'legacy_scheduler_observation'})
    monkeypatch.setattr('daikibo.planning.timestamp',lambda:1)
    answer=c.k.source(c.owner,project,'I approve the same provisional assumption now.')
    result=c.p.respond(c.owner,decision['id'],decision['digest'],'approve',
                       'I approve the same provisional assumption now.',source=answer['id'])
    assert result['status']=='expired' and result['answered'] is False
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision['id'],))['status']=='expired'
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='timer_fired' AND json_extract(body,'$.timer')=?",
                   (timer['id'],))['n']==1
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='decision_expired' AND json_extract(body,'$.decision')=?",
                   (decision['id'],))['n']==1


def test_timed_final_answer_can_be_reviewed_and_applied_after_expiry(full,full_project,monkeypatch,tmp_path):
    c=full;project,_,artifact,_=full_project
    fake_now=[1000.0]
    monkeypatch.setattr('daikibo.planning.timestamp',lambda:fake_now[0])
    decision=_provisional_decision(c,project,artifact,expires=1900.0)
    answer=c.k.source(c.owner,project,'I approve this exact provisional assumption.')
    c.p.respond(c.owner,decision['id'],decision['digest'],'approve',
                'I approve this exact provisional assumption.',source=answer['id'])
    response=c.p.response_evidence(decision['id'])
    deadline=response['body']['response_observed_at']+1
    timer=c.s.one("SELECT id FROM timers WHERE kind='decision_expiry' AND ref=?",(decision['id'],),True)
    c.s.execute('UPDATE timers SET due=? WHERE id=?',(deadline,timer['id']))
    fake_now[0]=deadline+10
    reviewer=_register_coverage_reviewer(c,tmp_path,'provisional-after-expiry')
    review=c.rt.review(c.owner,decision['id'],'consistency',reviewer)
    result=c.p.apply_decision(c.owner,decision['id'],review['receipt'])
    assert result['status']=='applied'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision['id'],))['status']=='applied'
    assert c.s.one('SELECT fired FROM timers WHERE id=?',(timer['id'],))['fired'] is not None


def test_native_expiry_does_not_present_or_consume_a_late_answer(full,full_project):
    c=full;project,_,artifact,root=full_project
    c.native.attach(c.owner,'expired-native-session',str(root),project=project,register_repository=False)
    first=_provisional_decision(c,project,artifact)
    shown=c.native.present_decision(c.owner,'expired-native-session',first['id'])
    source=c.native.input(c.owner,'expired-native-session','I approve the old provisional assumption')['source']
    timer=c.s.one("SELECT id FROM timers WHERE kind='decision_expiry' AND ref=?",(first['id'],),True)
    c.s.execute('UPDATE timers SET due=0 WHERE id=?',(timer['id'],))
    result=c.native.respond(c.owner,'expired-native-session',first['id'],shown['expected_digest'],source,
                            'approve','I approve the old provisional assumption')
    assert result['status']=='expired' and result['answered'] is False
    session=c.s.one('SELECT body FROM native_sessions WHERE id=?',('expired-native-session',),True)
    presentation=parse_json(session['body'])['presented'][first['id']]
    assert presentation['consumed'] is False
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='native_decision_response' AND json_extract(body,'$.decision')=?",
                   (first['id'],))['n']==0

    second=_provisional_decision(c,project,artifact)
    second_timer=c.s.one("SELECT id FROM timers WHERE kind='decision_expiry' AND ref=?",(second['id'],),True)
    c.s.execute('UPDATE timers SET due=0 WHERE id=?',(second_timer['id'],))
    result=c.native.present_decision(c.owner,'expired-native-session',second['id'])
    assert result['status']=='expired'
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='native_decision_presented' AND json_extract(body,'$.decision')=?",
                   (second['id'],))['n']==0


def _planned_unstarted_task(c, full_project, title='Unstarted decision task'):
    project,repo,artifact,_=full_project
    return c.w.create(c.owner,project,{'title':title,'goal':'Inspect the retained requirement',
        'read_artifacts':[artifact],'write_paths':['calc.py'],'acceptance':['AC-UNSTARTED'],
        'dependencies':[],'repos':[repo],'non_goals':[]})['id']


def _keep_existing_decision(c, project, artifact, source_text='The user asks for a change.'):
    source=c.k.source(c.owner,project,source_text)
    current=c.k.artifact(c.owner,artifact)
    change=c.p.change(c.owner,project,{'title':'Requested requirement update','origin':'user',
        'reason':'Apply the user request if approved','source':source['id'],'affected':[artifact],
        'evidence':[source['id']],'deltas':[{'artifact':artifact,'expected_revision':current['revision'],
            'body':{**current['body'],'statement':'Requested behavior'}}]})
    proposal=c.p.propose_decision(c.owner,project,_proposal(artifact,change=change['id']))
    answer=c.k.source(c.owner,project,'Keep the existing requirement as-is.')
    c.p.respond(c.owner,proposal['id'],proposal['digest'],'keep_existing',
                'Keep the existing requirement as-is.',source=answer['id'])
    return change['id'],proposal['id']


def test_keep_existing_revalidates_only_controller_proven_unstarted_task(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    task=_planned_unstarted_task(c,full_project)
    before=c.w.task(c.owner,task)
    change,decision=_keep_existing_decision(c,project,artifact)
    fenced=c.w.task(c.owner,task)
    assert fenced['validity']=='needs_review' and fenced['epoch']==before['epoch']+2
    material=c.p.decision_review_material(decision)[1]
    assert [item['task'] for item in material['task_revalidations']]==[task]
    reviewer=_register_coverage_reviewer(c,tmp_path,'keep-existing-task')
    review=c.rt.review(c.owner,decision,'consistency',reviewer)
    result=c.p.apply_decision(c.owner,decision,review['receipt'])
    current=c.w.task(c.owner,task)
    assert current['validity']=='current' and current['status']=='planned'
    assert current['epoch']==before['epoch']+2
    assert result['task_revalidations']==material['task_revalidations']
    audit=c.s.one("SELECT body FROM events WHERE kind='task_revalidated_after_keep_existing' "
                   "AND json_extract(body,'$.task')=?",(task,),True)
    assert parse_json(audit['body'])['change']==change


def test_keep_existing_does_not_clear_preexisting_task_blocks(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    task=_planned_unstarted_task(c,full_project,'Already fenced task')
    c.s.execute('INSERT INTO blocks VALUES(?,?,?,?)',(task,'changed_input','prior-change','Still needs reassessment'))
    change,decision=_keep_existing_decision(c,project,artifact,'A separate user request.')
    material=c.p.decision_review_material(decision)[1]
    assert material['task_revalidations']==[]
    reviewer=_register_coverage_reviewer(c,tmp_path,'keep-existing-blocked-task')
    review=c.rt.review(c.owner,decision,'consistency',reviewer)
    c.p.apply_decision(c.owner,decision,review['receipt'])
    current=c.w.task(c.owner,task)
    assert current['validity']=='needs_review'
    assert current['epoch']==0+2
    assert any(block['kind']=='changed_input' and block['ref']=='prior-change' for block in current['blocks'])


def _answered_decision(c,project,artifact,title):
    proposal=c.p.propose_decision(c.owner,project,_proposal(artifact,title=title))
    utterance='Approve '+title+'.'
    source=c.k.source(c.owner,project,utterance,'answer:'+proposal['id'])
    c.p.respond(c.owner,proposal['id'],proposal['digest'],'approve',utterance,source=source['id'])
    return proposal['id']


def test_keep_existing_batch_keeps_task_fenced_when_another_member_changes_its_input(full,full_project,tmp_path):
    c=full;project,repo,artifact,_=full_project
    second=_accepted_requirement(c,project,'Second task input','Another behavior read by the same task')
    task=_planned_unstarted_task(c,full_project,'Task with two accepted inputs')
    task_row=c.s.one('SELECT body FROM tasks WHERE id=?',(task,),True)
    task_body=parse_json(task_row['body']);task_body['read_artifacts'].append(second)
    second_row=c.k.artifact(c.owner,second)
    c.s.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(task_body).decode(),task))
    c.s.execute('INSERT INTO task_reads VALUES(?,?,?,?)',(task,second,second_row['revision'],second_row['digest']))

    keep_change,keep_decision=_keep_existing_decision(c,project,artifact,'Keep the first input as-is.')
    update_change,update_decision=_user_decision(c,project,second,statement='Adopt the requested second behavior')
    packet=c.p.decision_batch_prepare(c.owner,project,[keep_decision,update_decision])
    packet_body=parse_json(c.s.one('SELECT body FROM decision_batches WHERE id=?',(packet['id'],),True)['body'])
    assert packet_body['effects']['task_revalidations']==[]
    reviewer=_register_coverage_reviewer(c,tmp_path,'keep-existing-batch-overlap')
    review=c.rt.review(c.owner,packet['id'],'consistency',reviewer)
    result=c.p.decision_batch_apply(c.owner,packet['id'],review['receipt'])
    current=c.w.task(c.owner,task)
    assert result['task_revalidations']==[]
    assert current['validity']=='needs_review'
    assert any(block['kind']=='changed_input' and block['ref']==second for block in current['blocks'])
    assert c.s.one('SELECT stage FROM changes WHERE id=?',(keep_change,))['stage']=='withdrawn'
    assert c.s.one('SELECT stage FROM changes WHERE id=?',(update_change,))['stage']=='ready_for_reimplementation'


def test_incremental_single_review_rechecks_current_family_and_archives_both_receipts(full,full_project,tmp_path):
    c=full;project=full_project[0]
    original=_accepted_requirement(c,project,'Original review scope','The already reviewed behavior')
    decision=_answered_decision(c,project,original,'Original choice')
    reviewer=_register_coverage_reviewer(c,tmp_path,'incremental-single-pass')
    base=c.rt.review(c.owner,decision,'consistency',reviewer)['receipt']
    added=_accepted_requirement(c,project,'Independent addition','A source-backed independent behavior')
    preview=c.p.decision_review_subject(c.owner,decision,incremental_from=base)
    assert preview['incremental_review']['available'] is True
    supplemental=c.rt.review(c.owner,decision,'consistency',reviewer,
                              proposal={'incremental_from':base})['receipt']

    failing=_register_verdict_reviewer(c,tmp_path,'fail','incremental-single-fail')
    failed_full=c.rt.review(c.owner,decision,'consistency',failing)['receipt']
    with pytest.raises(Fault) as stale:
        c.p.apply_decision(c.owner,decision,supplemental)
    assert stale.value.code=='stale_evidence'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision,))['status']=='decision_received'

    # A fresh supplemental judgment in the same full-material family becomes
    # latest; it must still carry the unchanged original full PASS as its base.
    supplemental=c.rt.review(c.owner,decision,'consistency',reviewer,
                              proposal={'incremental_from':base})['receipt']
    result=c.p.apply_decision(c.owner,decision,supplemental)
    assert result['review_mode']=='incremental'
    assert result['base_review_receipt']==base and result['supplemental_review_receipt']==supplemental
    assert failed_full!=supplemental
    archive=c.history.export_current(c.owner,project)
    assert len(archive['decision_incremental_reviews'])==1
    proof=archive['decision_incremental_reviews'][0]['body']['proof']
    assert proof['added_artifact']['id']==added
    assert validate_specifications(archive)['decisions']>=1
    corrupted=copy.deepcopy(archive)
    corrupt_proof=corrupted['decision_incremental_reviews'][0]['body']['proof']
    corrupt_proof['added_artifact']['body']['statement']='Altered after the recorded review'
    corrupt_proof['added_artifact']['digest']=digest(corrupt_proof['added_artifact']['body'])
    with pytest.raises(Fault) as invalid_archive:
        validate_specifications(corrupted)
    assert invalid_archive.value.code=='invalid_snapshot'


def test_incremental_batch_review_keeps_frozen_packet_and_exports_reconstruction_proof(full,full_project,tmp_path):
    c=full;project=full_project[0]
    first=_accepted_requirement(c,project,'Batch first','First selected behavior')
    second=_accepted_requirement(c,project,'Batch second','Second selected behavior')
    decisions=[_answered_decision(c,project,first,'Batch first choice'),
               _answered_decision(c,project,second,'Batch second choice')]
    batch=c.p.decision_batch_prepare(c.owner,project,decisions)['id']
    frozen=c.s.one('SELECT digest,body FROM decision_batches WHERE id=?',(batch,),True)
    reviewer=_register_coverage_reviewer(c,tmp_path,'incremental-batch-pass')
    base=c.rt.review(c.owner,batch,'consistency',reviewer)['receipt']
    added=_accepted_requirement(c,project,'Batch addition','An independent appended requirement')
    preview=c.p.decision_batch_get(c.owner,batch,incremental_from=base)
    assert preview['incremental_review']['available'] is True
    supplemental=c.rt.review(c.owner,batch,'consistency',reviewer,
                              proposal={'incremental_from':base})['receipt']
    result=c.p.decision_batch_apply(c.owner,batch,supplemental)
    stored=c.s.one('SELECT digest,body,result FROM decision_batches WHERE id=?',(batch,),True)
    assert stored['digest']==frozen['digest'] and stored['body']==frozen['body']
    assert result['review_mode']=='incremental'
    assert result['base_review_receipt']==base and result['supplemental_review_receipt']==supplemental
    assert result['incremental_proof']['added_artifact']['id']==added
    exported=c.history.export_current(c.owner,project)
    archived=next(item for item in exported['decision_batches'] if item['id']==batch)
    assert archived['result']['incremental_proof']['base_review_receipt']==base
    assert archived['result']['incremental_proof']['added_artifact']['id']==added
    assert validate_specifications(exported)['decision_batches']>=1


def test_incremental_delta_failure_cannot_be_applied_and_later_addition_stales_pass(full,full_project,tmp_path):
    c=full;project=full_project[0]
    original=_accepted_requirement(c,project,'Incremental base','Existing behavior')
    decision=_answered_decision(c,project,original,'Delta choice')
    passing=_register_coverage_reviewer(c,tmp_path,'incremental-stale-pass')
    failing=_register_verdict_reviewer(c,tmp_path,'fail','incremental-delta-fail')
    base=c.rt.review(c.owner,decision,'consistency',passing)['receipt']
    _accepted_requirement(c,project,'One addition','Independent addition')
    failed=c.rt.review(c.owner,decision,'consistency',failing,
                       proposal={'incremental_from':base})['receipt']
    with pytest.raises(Fault) as rejected:
        c.p.apply_decision(c.owner,decision,failed)
    assert rejected.value.code=='review_failed'

    supplemental=c.rt.review(c.owner,decision,'consistency',passing,
                             proposal={'incremental_from':base})['receipt']
    _accepted_requirement(c,project,'Later addition','A new post-review requirement')
    with pytest.raises(Fault) as stale:
        c.p.apply_decision(c.owner,decision,supplemental)
    assert stale.value.code=='stale_evidence'
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(decision,))['status']=='decision_received'


def test_incremental_review_requires_full_review_for_multiple_or_constrained_additions(full,full_project,tmp_path):
    c=full;project=full_project[0]
    original=_accepted_requirement(c,project,'Full review base','Existing behavior')
    decision=_answered_decision(c,project,original,'Full review choice')
    reviewer=_register_coverage_reviewer(c,tmp_path,'incremental-fallback')
    base=c.rt.review(c.owner,decision,'consistency',reviewer)['receipt']
    _accepted_requirement(c,project,'First independent addition','New independent behavior')
    _accepted_requirement(c,project,'Second independent addition','Another new behavior')
    preview=c.p.decision_review_subject(c.owner,decision,incremental_from=base)
    assert preview['incremental_review']['available'] is False
    constrained_project=c.k.create_project(c.owner,'Constrained delta')['id']
    constrained=_accepted_requirement(c,constrained_project,'Constrained base','Old behavior')
    constrained_decision=_answered_decision(c,constrained_project,constrained,'Constrained choice')
    constrained_base=c.rt.review(c.owner,constrained_decision,'consistency',reviewer)['receipt']
    _accepted_requirement(c,constrained_project,'Constrained addition','Needs whole-set review',
                          constraints={'latency_ms':200})
    constrained_preview=c.p.decision_review_subject(c.owner,constrained_decision,
                                                     incremental_from=constrained_base)
    assert constrained_preview['incremental_review']['available'] is False


def test_closed_notice_rejects_late_ack_and_old_ack_retry_after_auto_close(full,full_project):
    c=full;project=full_project[0]
    c.g.inbox(project,'warning','auto-closed-no-human',{'message':'Automatic closure'},'warning')
    automatic=c.s.one("SELECT * FROM inbox WHERE project=? AND ref='auto-closed-no-human'",(project,))
    c.p._close_notices(project,'auto-closed-no-human','system','finished_elsewhere')
    with pytest.raises(Fault) as late:
        c.i.acknowledge(c.owner,automatic['id'],'I saw it after it closed.',
                        expected_digest=digest(automatic['body'].encode()))
    assert late.value.code=='already_closed'
    assert c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='inbox_acknowledged' "
                   "AND json_extract(body,'$.id')=?",(automatic['id'],))['n']==0

    body={'message':'Same-version notice'}
    c.g.inbox(project,'warning','ack-then-close',body,'warning')
    notice=c.s.one("SELECT * FROM inbox WHERE project=? AND ref='ack-then-close'",(project,))
    source=c.k.source(c.owner,project,'I acknowledged the same-version notice.')
    expected=digest(notice['body'].encode())
    c.i.acknowledge(c.owner,notice['id'],'I acknowledged the same-version notice.',
                    source=source['id'],expected_digest=expected)
    c.g.inbox(project,'warning','ack-then-close',body,'warning')
    assert c.s.one('SELECT status FROM inbox WHERE id=?',(notice['id'],))['status']=='open'
    c.p._close_notices(project,'ack-then-close','system','finished_elsewhere')
    with pytest.raises(Fault) as closed:
        c.i.acknowledge(c.owner,notice['id'],'I acknowledged the same-version notice.',
                        source=source['id'],expected_digest=expected)
    assert closed.value.code=='already_closed'
    assert c.s.one("SELECT COUNT(*) AS n FROM events WHERE kind='inbox_acknowledged' "
                   "AND json_extract(body,'$.id')=?",(notice['id'],))['n']==1
