"""REQ-006/036/051/079: applicability is a profile decision, not missing capability."""
import copy
import itertools
import pytest
from daikibo.common import Fault
from conftest import make_task,finish_task
from test_delivery_git_and_recovery import profile

CATEGORIES=('migration','security','performance','contract')

@pytest.mark.parametrize('flags', list(itertools.product((False,True),repeat=4)))
def test_every_conditional_profile_combination_is_explicit(full,full_project,flags):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);b=profile(p,r,q,t)
    for name,yes in zip(CATEGORIES,flags):
        b['applicability'][name]={'applicable':yes,'reason':'Fixture applicability for '+name}
        if yes:b['checks'].append({'id':name,'category':name,'repo':r,'kind':'pytest','argv':['python','-m','pytest','-q','test_calc.py'],'required_tests':['test_add']})
    out=c.d.configure(c.owner,p,b)
    assert out['scope']['requirements']==[q] and out['scope']['tasks']==[t]
    assert 'deploy_ready' not in out

@pytest.mark.parametrize('category',CATEGORIES)
def test_applicable_but_unplanned_category_is_rejected(full,full_project,category):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);b=profile(p,r,q,t)
    b['applicability'][category]['applicable']=True
    with pytest.raises(Fault) as exc:c.d.configure(c.owner,p,b)
    assert exc.value.code=='missing_required_check'
    assert not c.s.one('SELECT project FROM profiles WHERE project=?',(p,))

@pytest.mark.parametrize('reason',['',None,{},[]])
def test_nonapplicability_without_reason_cannot_be_frozen(full,full_project,reason):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);b=profile(p,r,q,t)
    b['applicability']['migration']['reason']=reason
    with pytest.raises(Fault):c.d.configure(c.owner,p,b)

@pytest.mark.parametrize('category',('build','start','smoke','integration','scenario'))
def test_nonapplicability_cannot_remove_unconditional_delivery_checks(full,full_project,category):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);b=profile(p,r,q,t)
    b['checks']=[x for x in b['checks'] if x['category']!=category]
    with pytest.raises(Fault) as exc:c.d.configure(c.owner,p,b)
    assert exc.value.code=='incomplete_profile'


def test_profile_weakened_after_review_cannot_be_replaced(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);b=profile(p,r,q,t)
    frozen=c.d.configure(c.owner,p,b);next=copy.deepcopy(b);next['reason']='Clarify target only'
    ev=c.rt.review(c.owner,p,'delivery_profile','fixture',proposal=next)
    next['rollback']='Skip previous rollback plan'
    with pytest.raises(Fault):c.d.configure(c.owner,p,next,frozen['digest'],ev['receipt'])


def test_all_categories_execute_but_fixture_is_not_deployment_acceptance(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);b=profile(p,r,q,t)
    for name in CATEGORIES:
        b['applicability'][name]={'applicable':True,'reason':'Exercise the conditional check path, not a real target'}
        b['checks'].append({'id':name,'category':name,'repo':r,'kind':'pytest',
                            'argv':['python','-m','pytest','-q','test_calc.py'],'required_tests':['test_add']})
    c.d.configure(c.owner,p,b);finish_task(c,p,t)
    candidate=c.d.prepare(c.owner,p)['id'];results=c.d.verify(c.owner,candidate)
    assert len(results['results'])==9 and all(x['passed'] for x in results['results'])
    for role in ('integration','goal_validation'):c.rt.review(c.owner,candidate,role,'fixture')
    with pytest.raises(Fault):c.d.certify(c.owner,candidate)
