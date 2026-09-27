"""Local RPC, native conversation routing and bounded synthesis failure handling."""
import copy
import json
import sys
import tempfile
import threading
from pathlib import Path
import pytest
from daikibo.common import Actor, Fault, canonical, digest
from daikibo.rpc import Server, Client
from test_reviewed_breakdowns import setup, adopt
from test_delegated_workstreams import activate_scope
from test_scope_returns import begin, ready, review_level, apply_ready


def test_managed_job_reviews_leaf_and_root_without_new_scheduler(setup):
    c=setup[0];w,p=begin(setup,budget=100000)
    for _ in range(2):
        state=c.scope_returns.advance(c.owner,p['id']);assert state['pending_total']==1
        args={'subject':state['pending'][0]['packet'],'role':'impact','adapter':'markers'}
        assert c.jobs.subject_project('review',args)==setup[1]
        job=c.jobs.submit(c.owner,'review',args)
        outcome=c.jobs.run_one(c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],)))
        assert c.jobs.get(c.owner,job['id'])['status']=='succeeded'
        assert outcome['result']['result']['verdict']=='pass'
    assert c.scope_returns.advance(c.owner,p['id'])['ready']
    assert apply_ready(c,p)['status']=='withdrawn'


def test_native_conversation_can_propose_review_and_apply_without_owner_token(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup)
    workspace=c.s.one('SELECT path FROM repos WHERE id=?',(r,))['path']
    c.native.attach(c.owner,'returns-session',workspace,project=p)
    ans=c.native.actions(c.owner,'returns-session',[{'method':'workstream.return_propose',
        'params':{'scope':w,'reason':'Keep requirements, return responsibility','byte_budget':100000}}])
    assert ans['all_applied'];proposal=ans['actions'][0]['result']
    result=ready(c,proposal['id'])
    reply=c.native.actions(c.owner,'returns-session',[{'method':'workstream.return_apply','params':{
        'proposal':proposal['id'],'expected_digest':proposal['digest'],'root_packet':result['root_packet'],
        'review_receipt':result['root_review_receipt']}}])
    assert reply['all_applied'] and reply['actions'][0]['result']['deploy_ready'] is False


def test_real_unix_rpc_source_pages_and_apply(setup):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    with tempfile.TemporaryDirectory(prefix='dd-return-') as tmp:
        server=Server(c,Path(tmp)/'rpc');th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
        try:
            client=Client(Path(tmp)/'rpc')
            p=client.call('workstream.return_propose',{'scope':w,'reason':'Return normally','byte_budget':4096})
            page=client.call('workstream.return_get',{'proposal':p['id'],'limit':1})
            assert page['next_offset']==1
            leaf=client.call('workstream.return_packet',{'packet':page['leaves'][0]['id']})
            assert len(canonical(leaf['body']))<=4096
            for name in ('propose','get','list','packet','advance','apply','abandon'):
                assert 'workstream.return_'+name in c.routes
            result=ready(c,p['id'])
            answer=client.call('workstream.return_apply',{'proposal':p['id'],'expected_digest':p['digest'],
                'root_packet':result['root_packet'],'review_receipt':result['root_review_receipt']},request_id='return-one')
            again=client.call('workstream.return_apply',{'proposal':p['id'],'expected_digest':p['digest'],
                'root_packet':result['root_packet'],'review_receipt':result['root_review_receipt']},request_id='return-one')
            assert again==answer
        finally:server.shutdown();server.server_close();th.join(timeout=5)


def test_synthesis_result_that_cannot_fit_is_not_truncated(setup,tmp_path):
    c=setup[0];w,p=begin(setup)
    script=tmp_path/'verbose.py';script.write_text('''import json,sys
p=json.load(sys.stdin)
print(json.dumps({'verdict':'pass','rationale':'Detailed actual observation '*220,
'covered':p['context']['required_coverage'],'findings':[],'observations':[{'ref':p['subject'],'detail':'Protocol fixture only.'}],'dispositions':[]}))
''')
    c.rt.adapters.register(c.owner,'verbose','fixture',sys.executable,[str(script)])
    for item in p['leaves']:c.rt.review(c.owner,item['id'],'impact','verbose')
    count=c.s.one('SELECT count(*) n FROM scope_return_packets')['n']
    with pytest.raises(Fault) as e:c.scope_returns.advance(c.owner,p['id'])
    assert e.value.code=='review_result_too_large'
    assert c.s.one('SELECT count(*) n FROM scope_return_packets')['n']==count
    assert c.workstreams.get(c.owner,w)['status']=='active'


@pytest.mark.parametrize('mutation',['wrong_role','wrong_project','bad_page','bad_historical','wrong_proposal'])
def test_routing_keeps_role_project_and_input_contract(setup,mutation):
    c=setup[0];w,p=begin(setup);leaf=p['leaves'][0]['id']
    with pytest.raises(Fault):
        if mutation=='wrong_role':c.rt.review(c.owner,leaf,'design','markers')
        elif mutation=='wrong_project':c.scope_returns.get(Actor('other','agent','wrong'),p['id'])
        elif mutation=='bad_page':c.scope_returns.advance(c.owner,p['id'],limit=1000)
        elif mutation=='bad_historical':c.scope_returns.packet(c.owner,leaf,historical='true')
        else:
            other=c.scope_returns.propose(c.owner,w,'Another return')
            state=ready(c,other['id'])
            c.scope_returns.apply(c.owner,p['id'],p['digest'],state['root_packet'],state['root_review_receipt'])


def test_active_child_cannot_be_implicitly_returned(setup):
    c=setup[0];adopt(setup);w=activate_scope(setup);child=activate_scope(setup,parent=w)
    with pytest.raises(Fault) as e:c.scope_returns.propose(c.owner,w,'Return parent')
    assert e.value.code=='active_child_scopes'
    proposal=c.scope_returns.propose(c.owner,child,'Return child first',100000);apply_ready(c,proposal)
    parent=c.scope_returns.propose(c.owner,w,'Now return parent',100000);apply_ready(c,parent)
    assert c.workstreams.get(c.owner,w)['status']=='withdrawn'


def test_competing_proposals_cannot_withdraw_twice(setup):
    c=setup[0];w,p=begin(setup,budget=100000);other=c.scope_returns.propose(c.owner,w,'Competing return',100000)
    first=ready(c,p['id']);second=ready(c,other['id'])
    c.scope_returns.apply(c.owner,p['id'],p['digest'],first['root_packet'],first['root_review_receipt'])
    with pytest.raises(Fault):c.scope_returns.apply(c.owner,other['id'],other['digest'],second['root_packet'],second['root_review_receipt'])
    assert c.s.one("SELECT count(*) n FROM workstream_records WHERE scope=? AND kind='withdraw'",(w,))['n']==1


def test_large_material_over_old_700k_bound_can_complete_without_dropping_content(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup)
    a=c.k.artifact(c.owner,q);body=copy.deepcopy(a['body']);body['statement']='🌏'*190000  # 760000 UTF-8 bytes, within the 200000-character artifact bound
    c.k._revise(c.owner,a,a['revision'],body,'Larger current input; old assignment still needs an explicit return','accepted')
    material=c.scope_returns.material(c.owner,w,'Return stale assignment for replanning')
    assert len(canonical(material))>700000
    with pytest.raises(Fault) as e:c.workstreams.withdrawal_subject(c.owner,w,{'reason':'Return stale assignment for replanning'})
    assert e.value.code=='context_insufficient'
    proposal=c.scope_returns.propose(c.owner,w,'Return stale assignment for replanning',70000)
    assert proposal['leaf_count']>10
    collected=[];offset=0
    while True:
        page=c.scope_returns.get(c.owner,proposal['id'],offset,5)
        for item in page['leaves']:
            packet=c.scope_returns.packet(c.owner,item['id'])['body'];collected.append(packet['serialized_fragment'])
            assert len(canonical(packet))<=70000
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert ''.join(collected).encode()==canonical(material)
    original=c.w.task(c.owner,t)
    state=ready(c,proposal['id']);assert state['level']>=2
    outcome=c.scope_returns.apply(c.owner,proposal['id'],proposal['digest'],state['root_packet'],state['root_review_receipt'])
    assert not outcome['deploy_ready'] and c.w.task(c.owner,t)==original
    assert c.k.artifact(c.owner,q)['body']['statement']==body['statement']
