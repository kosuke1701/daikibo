import copy
import pytest
from daikibo.common import Actor,Fault,canonical,digest,parse_json,timestamp
from conftest import make_task


def proposal(c,p,q,**extra):
    return {'title':'Clarify behavior','reason':'Important interpretation','options':['approve','keep_existing'],'recommendation':'approve','refs':[q],'requirement_affecting':True,**extra}


def test_new_user_change_authentic_adjudication_rechecks_then_replans(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project)
    source=c.k.source(c.owner,p,'Change accepted arithmetic precision.')
    old=c.k.artifact(c.owner,q)
    updated={**old['body'],'statement':'Returns the exact integer arithmetic sum'}
    change=c.p.change(c.owner,p,{'title':'Precision','origin':'user','reason':'Explicit revised product constraint','source':source['id'],'affected':[q],'evidence':[source['id']],
                                'deltas':[{'artifact':q,'expected_revision':1,'body':updated}]})
    assert change['stage']=='awaiting_product_decision'
    assert c.w.task(c.owner,t)['validity']=='needs_review'
    d=c.p.propose_decision(c.owner,p,proposal(c,p,q,change=change['id']))
    agent=Actor('test-agent','agent',p)
    with pytest.raises(Fault):c.p.respond(agent,d['id'],d['digest'],'approve','I am the user')
    response=c.p.respond(c.owner,d['id'],d['digest'],'approve','I approve the exact integer requirement')
    assert response['status']=='decision_received'
    assert c.k.artifact(c.owner,q)['revision']==1
    review=c.rt.review(c.owner,d['id'],'consistency','fixture')
    applied=c.p.apply_decision(agent,d['id'],review['receipt'])
    assert applied['status']=='applied'
    assert c.k.artifact(c.owner,q)['revision']==2
    assert c.w.task(c.owner,t)['validity']=='needs_review'
    c.w.replan(c.owner,t,1,'Reconcile explicit revised specification')
    assert c.w.task(c.owner,t)['validity']=='current'
    assert c.k.artifact(c.owner,q,1)['body']==old['body']


def test_agent_source_cannot_be_relabelled_user_change(full,full_project):
    c=full;p,r,q,_=full_project;agent=Actor('test-agent','agent',p)
    source=c.k.source(agent,p,'I approve a new requirement')
    with pytest.raises(Fault,match='Untrusted text'):
        c.p.change(agent,p,{'title':'Fake user','origin':'user','reason':'pretend','source':source['id'],'affected':[q],'evidence':[source['id']]})


def test_technical_attempt_escalates_layers_not_budget_exhaustion(full,full_project):
    c=full;p,r,q,_=full_project
    source=c.k.source(c.owner,p,'External design constraint')
    change=c.p.change(c.owner,p,{'title':'Investigate infeasibility','origin':'implementation','reason':'Observed limitation','affected':[q],'evidence':[source['id']]})
    with pytest.raises(Fault):c.p.propose_decision(c.owner,p,proposal(c,p,q,change=change['id']))
    first=c.rt.review(c.owner,change['id'],'feasibility','fixture')
    attempt={'hypothesis':'Try one implementation','alternatives':['alternative code'],'evidence':[first['receipt']],'outcome':'resource_exhausted','remaining_unknown':'Other approaches not investigated'}
    assert c.p.attempt(c.owner,change['id'],'local_repair',attempt)['stage']=='local_repair'
    for current,nextlevel in [('local_repair','module_replan'),('module_replan','system_replan'),('system_replan','awaiting_product_decision')]:
        review=c.rt.review(c.owner,change['id'],'feasibility','fixture')
        outcome=c.p.attempt(c.owner,change['id'],current,{**attempt,'outcome':'no_solution_found','review_receipt':review['receipt'],'evidence':[review['receipt']]})
        assert outcome['stage']==nextlevel
    d=c.p.propose_decision(c.owner,p,proposal(c,p,q,change=change['id']))
    assert d['id']


def test_revision_invalidates_pending_human_decision(full,full_project):
    c=full;p,r,q,_=full_project;d=c.p.propose_decision(c.owner,p,proposal(c,p,q))
    with c.s.transaction():
        art=c.k.artifact(c.owner,q)
        c.k._revise(c.owner,art,1,{**art['body'],'statement':'New meaning'},'Fixture simulates an independently accepted concurrent change','accepted')
    with pytest.raises(Fault,match='bound requirement changed'):c.p.respond(c.owner,d['id'],d['digest'],'approve','Approve old proposal')


def test_secondary_conflict_prevents_applying_even_approved_delta(full,full_project):
    c=full;p,r,q,_=full_project
    a=c.k.propose(c.owner,p,'requirement',{'title':'Sync','statement':'Synchronous mode','acceptance':['SYNC'],'constraints':{'mode':'sync'}})
    c.k.accept(c.owner,a['id'],1)
    src=c.k.source(c.owner,p,'Use async mode for addition')
    current=c.k.artifact(c.owner,q)
    change=c.p.change(c.owner,p,{'title':'Async','origin':'user','reason':'New request','source':src['id'],'affected':[q],'evidence':[src['id']],
                              'deltas':[{'artifact':q,'expected_revision':1,'body':{**current['body'],'constraints':{'mode':'async'}}}]})
    d=c.p.propose_decision(c.owner,p,proposal(c,p,q,change=change['id']))
    c.p.respond(c.owner,d['id'],d['digest'],'approve','Approved without silently removing other constraints')
    review=c.rt.review(c.owner,d['id'],'consistency','fixture')
    with pytest.raises(Fault,match='other accepted requirements'):c.p.apply_decision(c.owner,d['id'],review['receipt'])
    assert c.k.artifact(c.owner,q)['revision']==1
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(d['id'],))['status']=='decision_received'


def test_provisional_decision_review_binds_actual_proposal_and_expires(full,full_project):
    c=full;p,r,q,_=full_project
    body=proposal(c,p,q,requirement_affecting=False,provisional=True,reversible=True,expires=timestamp()+600)
    review=c.rt.review(c.owner,p,'decision_proposal','fixture',proposal=body)
    wrong={**body,'reason':'Changed after review','consistency_receipt':review['receipt']}
    with pytest.raises(Fault):c.p.propose_decision(c.owner,p,wrong)
    d=c.p.propose_decision(c.owner,p,{**body,'consistency_receipt':review['receipt']})
    first=c.i.inbox(c.owner,p);second=c.i.inbox(c.owner,p)
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(d['id'],))['status']=='provisional'
    c.s.execute("UPDATE timers SET due=0 WHERE ref=?",(d['id'],));c.w.reconcile(c.owner,p)
    assert c.s.one('SELECT status FROM decisions WHERE id=?',(d['id'],))['status']=='expired'
    assert c.s.one("SELECT id FROM inbox WHERE ref=? AND status='open'",(d['id'],))


def test_policy_changes_need_human_and_old_policy_consistency(full,full_project):
    c=full;p=full_project[0];old=c.g.policy(p);new=copy.deepcopy(old['body']);new['max_parallel']=8
    d=c.g.policy_propose(c.owner,p,new)
    assert c.g.policy(p)['body']['max_parallel']==4
    with pytest.raises(Fault):c.p.apply_decision(c.owner,d['id'],'fake')
    c.p.respond(c.owner,d['id'],d['digest'],'approve','Allow up to eight independently governed workers')
    review=c.rt.review(c.owner,d['id'],'consistency','fixture');c.p.apply_decision(c.owner,d['id'],review['receipt'])
    assert c.g.policy(p)['body']['max_parallel']==8
    weak=copy.deepcopy(new);weak['review_roles']=[]
    with pytest.raises(Fault):c.g.policy_propose(c.owner,p,weak)


def test_unrelated_task_continues_while_conflict_is_pending(full,full_project):
    c=full;p,r,q,_=full_project;t1=make_task(c,full_project)
    src=c.k.source(c.owner,p,'Independent other feature')
    other=c.k.propose(c.owner,p,'requirement',{'title':'Other','statement':'Independent behavior','acceptance':['AC-OTHER'],'source_refs':[src['id']]})
    c.k.accept(c.owner,other['id'],1)
    t2=make_task(c,(p,r,other['id'],full_project[3]),paths=['other.py'])
    c.p.conflict(c.owner,p,[q],'Incompatible new request',['keep_existing','change_request'])
    assert c.w.task(c.owner,t1)['validity']=='needs_review'
    claimed=c.w.claim(c.owner,p,t2)
    assert claimed['id']==t2
