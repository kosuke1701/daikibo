"""Observed fixture processes, not live LLM semantic approval."""
import copy
import json
import sys
import pytest
from daikibo.common import Actor, Fault, canonical, digest
from test_reviewed_breakdowns import setup, adopt, accepted, propose, review_all
from test_delegated_workstreams import activate_scope, second_unit


def begin(setup,budget=4096,reason='Return the entire responsibility without removing any requirement.'):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    proposal=c.scope_returns.propose(c.owner,w,reason,budget)
    return w,proposal


def review_level(c,pid):
    before=c.scope_returns.advance(c.owner,pid);offset=0
    # Keep one level's work list fixed before starting the reviews.
    todo=[]
    while True:
        page=c.scope_returns.advance(c.owner,pid,offset=offset,limit=2)
        assert page['level']==before['level']
        todo.extend(page['pending'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    for item in todo:c.rt.review(c.owner,item['packet'],'impact','markers')
    return before


def ready(c,pid):
    for _ in range(32):
        state=c.scope_returns.advance(c.owner,pid)
        if state['ready']:return state
        review_level(c,pid)
    raise AssertionError('Synthesis did not converge')


def apply_ready(c,proposal):
    state=ready(c,proposal['id'])
    return c.scope_returns.apply(c.owner,proposal['id'],proposal['digest'],state['root_packet'],state['root_review_receipt'])


def test_complete_fragments_synthesis_and_idempotent_return(setup):
    c,p,r,q,program,d,t,units=setup;w,proposal=begin(setup)
    assert proposal['leaf_count']>1
    original=c.w.task(c.owner,t)
    pending=c.scope_returns.advance(c.owner,proposal['id'])
    assert pending['level']==0 and not pending['ready']
    assert c.workstreams.get(c.owner,w)['status']=='active'
    review_level(c,proposal['id'])
    summary=c.scope_returns.advance(c.owner,proposal['id'])
    assert summary['level']==1 and not summary['ready']
    state=ready(c,proposal['id']);assert state['level']>=1
    result=c.scope_returns.apply(c.owner,proposal['id'],proposal['digest'],state['root_packet'],state['root_review_receipt'])
    assert result['status']=='withdrawn' and not result['deploy_ready'] and not result['root_scope_reduced']
    assert result['tasks_cancelled']==[] and c.w.task(c.owner,t)==original
    assert c.k.artifact(c.owner,q)['status']=='accepted'
    assert c.scope_returns.apply(c.owner,proposal['id'],proposal['digest'],state['root_packet'],state['root_review_receipt'])['replayed']
    with pytest.raises(Fault):c.scope_returns.apply(c.owner,proposal['id'],proposal['digest'],state['root_packet'],'WRONG')


def test_single_fragment_still_needs_distinct_root_review(setup):
    c=setup[0];w,p=begin(setup,budget=100000)
    assert p['leaf_count']==1
    ev=c.rt.review(c.owner,p['leaves'][0]['id'],'impact','markers')
    with pytest.raises(Fault) as e:c.scope_returns.apply(c.owner,p['id'],p['digest'],p['leaves'][0]['id'],ev['receipt'])
    assert e.value.code=='synthesis_required'
    state=c.scope_returns.advance(c.owner,p['id']);assert not state['ready'] and state['root_packet']
    ev2=c.rt.review(c.owner,state['root_packet'],'impact','markers')
    assert ev['run']!=ev2['run']
    assert apply_ready(c,p)['status']=='withdrawn'


@pytest.mark.parametrize('mutation',['task','artifact','policy','child','root','scope_closed'])
def test_changed_inputs_block_existing_return(setup,mutation):
    c,p,r,q,program,d,t,units=setup;w,pr=begin(setup,budget=100000);state=ready(c,pr['id'])
    if mutation=='task':c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'Task revised')
    elif mutation=='artifact':
        a=c.k.artifact(c.owner,q);b=copy.deepcopy(a['body']);b['statement']+=' changed'
        c.k._revise(c.owner,a,a['revision'],b,'Requirement changed','accepted')
    elif mutation=='policy':
        body=c.g.policy(p)['body'];body['max_run_seconds']+=1
        c.s.execute('UPDATE policies SET body=?,digest=? WHERE project=?',(canonical(body).decode(),digest(body),p))
    elif mutation=='child':activate_scope(setup,parent=w)
    elif mutation=='root':
        old=c.breakdowns.active(c.owner,program);new=propose(setup,previous=old['id']);review_all(c,new['id']);c.breakdowns.activate(c.owner,new['id'],old['id'])
    else:
        from test_delegated_workstreams import withdraw
        withdraw(c,w)
    with pytest.raises(Fault):c.scope_returns.apply(c.owner,pr['id'],pr['digest'],state['root_packet'],state['root_review_receipt'])
    assert c.scope_returns.get(c.owner,pr['id'])['status']=='proposed'


def test_unrelated_sibling_change_does_not_block_return(setup):
    c=setup[0];second,_=second_unit(setup);w=activate_scope(setup)
    p=c.scope_returns.propose(c.owner,w,'Return arithmetic',100000);state=ready(c,p['id'])
    c.w.replan(c.owner,second,c.w.task(c.owner,second)['revision'],'Only sibling changed')
    assert c.scope_returns.apply(c.owner,p['id'],p['digest'],state['root_packet'],state['root_review_receipt'])['status']=='withdrawn'


def failing_adapter(c,tmp_path,verdict='fail',covered=None,findings=None):
    file=tmp_path/('failed_'+verdict+'.py')
    file.write_text("import json,sys\np=json.load(sys.stdin)\nprint(json.dumps("+
       repr({'verdict':verdict,'rationale':'TEST ONLY deliberate changed review','covered':covered or [],'findings':findings or [],'observations':[{'ref':'fixture','detail':'Protocol fixture deliberately fails.'}],'dispositions':[]})+"))\n")
    c.rt.adapters.register(c.owner,'failed-'+verdict,'fixture',sys.executable,[str(file)])
    return 'failed-'+verdict


@pytest.mark.parametrize('damage',['later_failed_leaf','later_pass_leaf','missing_receipt','missing_blob','missing_run','wrong_receipt','wrong_root','wrong_proposal_digest'])
def test_root_cannot_hide_missing_or_changed_lower_evidence(setup,tmp_path,damage):
    c=setup[0];w,p=begin(setup,budget=100000);state=ready(c,p['id']);leaf=p['leaves'][0]['id']
    previous=c.g.evidence_for(leaf,p['leaves'][0]['digest'],'impact')[0]['id']
    packet=state['root_packet'];receipt=state['root_review_receipt'];ph=p['digest']
    if damage=='later_failed_leaf':c.rt.review(c.owner,leaf,'impact',failing_adapter(c,tmp_path))
    elif damage=='later_pass_leaf':c.rt.review(c.owner,leaf,'impact','markers')
    elif damage=='missing_receipt':
        c.s.execute('DROP TRIGGER receipts_no_delete');c.s.execute('DELETE FROM receipts WHERE id=?',(previous,))
    elif damage=='missing_blob':
        raw=c.g.receipt(previous)['stdout_blob'];c.s.blob_path(raw).unlink()
    elif damage=='missing_run':
        run=c.g.receipt(previous)['run'];c.s.execute("UPDATE runs SET status='unknown' WHERE id=?",(run,))
    elif damage=='wrong_receipt':receipt=previous
    elif damage=='wrong_root':packet=leaf
    else:ph='0'*64
    with pytest.raises(Fault):
        c.scope_returns.apply(c.owner,p['id'],ph,packet,receipt)
    assert c.workstreams.get(c.owner,w)['status']=='active'


def test_changed_leaf_review_generates_new_synthesis_not_rewrite(setup):
    c=setup[0];w,p=begin(setup,budget=100000);one=ready(c,p['id'])
    old=c.scope_returns.packet(c.owner,one['root_packet'],historical=True)
    c.rt.review(c.owner,p['leaves'][0]['id'],'impact','markers')
    two=c.scope_returns.advance(c.owner,p['id']);assert two['root_packet']!=one['root_packet']
    assert not two['ready']
    assert c.scope_returns.packet(c.owner,one['root_packet'],historical=True)==old
    with pytest.raises(Fault):c.rt.review(c.owner,one['root_packet'],'impact','markers')
    assert apply_ready(c,p)['status']=='withdrawn'


def test_repeated_advance_does_not_duplicate_packets_or_progress(setup):
    c=setup[0];w,p=begin(setup,budget=100000);review_level(c,p['id'])
    first=c.scope_returns.advance(c.owner,p['id']);stamp=c.supervisor.progress_digest(setup[1])
    count=c.s.one('SELECT count(*) n FROM scope_return_packets')['n']
    assert c.scope_returns.advance(c.owner,p['id'])==first
    assert c.s.one('SELECT count(*) n FROM scope_return_packets')['n']==count
    assert c.supervisor.progress_digest(setup[1])==stamp


@pytest.mark.parametrize('damage',['missing_leaf','modified_leaf','ordinal','wrong_coverage'])
def test_fragment_corruption_is_rejected(setup,damage):
    c=setup[0];w,p=begin(setup);leaf=p['leaves'][0]['id']
    c.s.execute('DROP TRIGGER scope_return_packets_immutable');c.s.execute('DROP TRIGGER scope_return_packets_no_delete')
    if damage=='missing_leaf':c.s.execute('DELETE FROM scope_return_packets WHERE id=?',(leaf,))
    elif damage=='ordinal':c.s.execute('UPDATE scope_return_packets SET ordinal=99 WHERE id=?',(leaf,))
    else:
        body=c.scope_returns.packet(c.owner,leaf)['body'];body['serialized_fragment']='lost' if damage=='modified_leaf' else body['serialized_fragment']
        if damage=='wrong_coverage':body['required_coverage']=[]
        c.s.execute('UPDATE scope_return_packets SET body=?,digest=? WHERE id=?',(canonical(body).decode(),digest(body),leaf))
    with pytest.raises(Fault):c.scope_returns.advance(c.owner,p['id'])


def test_abandon_preserves_scope_and_forbids_new_reviews(setup):
    c=setup[0];w,p=begin(setup);before=c.w.task(c.owner,setup[6])
    c.scope_returns.abandon(c.owner,p['id'],'Not needed, keep assignment')
    assert c.workstreams.get(c.owner,w)['status']=='active' and c.w.task(c.owner,setup[6])==before
    assert c.scope_returns.packet(c.owner,p['leaves'][0]['id'],historical=True)['historical']
    with pytest.raises(Fault):c.rt.review(c.owner,p['leaves'][0]['id'],'impact','markers')


@pytest.mark.parametrize('budget',[None,True,100,4095,100001])
def test_invalid_budget_does_not_record_partial_proposal(setup,budget):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    with pytest.raises(Fault):c.scope_returns.propose(c.owner,w,'return',budget)
    assert c.s.one('SELECT count(*) n FROM scope_returns')['n']==0


def test_legacy_small_return_also_binds_current_task_and_requirement(setup):
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup)
    ev=c.rt.review(c.owner,w,'impact','markers',proposal={'reason':'Return'})
    c.w.replan(c.owner,t,c.w.task(c.owner,t)['revision'],'Definition changed after review')
    with pytest.raises(Fault):c.workstreams.withdraw(c.owner,w,'Return',ev['receipt'])


def test_partial_synthesis_cannot_return_whole_scope(setup):
    c=setup[0];w,p=begin(setup,reason='large reason '*600)
    review_level(c,p['id']);state=c.scope_returns.advance(c.owner,p['id'])
    assert state['pending_total']>1
    partial=state['pending'][0]['packet'];ev=c.rt.review(c.owner,partial,'impact','markers')
    with pytest.raises(Fault) as e:c.scope_returns.apply(c.owner,p['id'],p['digest'],partial,ev['receipt'])
    assert e.value.code=='incomplete_return_review'
    assert apply_ready(c,p)['status']=='withdrawn'
