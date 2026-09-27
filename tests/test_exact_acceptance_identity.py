"""Mechanical identity tests; fixture reviewer outputs are not semantic evidence."""
from __future__ import annotations
import copy
import json
import sys
from pathlib import Path

import pytest
from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.obligations import from_store, marker, resolve, review_task
from conftest import make_task, finish_task
from test_reviewed_breakdowns import accepted, setup, propose


def add_same_name(c, p):
    return accepted(c, p, 'requirement', 'Distinct outcome with the same label', acceptance=['AC-ADD'])


def body_for(project, refs=None):
    p, repo, req, _ = project
    result = {'title':'Identity test','goal':'WRITE:'+json.dumps({'calc.py':'def add(a,b):\n    return a+b\n'}),
              'read_artifacts':[req], 'write_paths':['calc.py'], 'acceptance':['AC-ADD'],
              'dependencies':[], 'repos':[repo], 'non_goals':[]}
    if refs is not None: result['acceptance_refs'] = refs
    return result


def test_ambiguous_new_task_requires_qualified_identity(full, full_project):
    c=full; second=add_same_name(c,full_project[0]); body=body_for(full_project)
    body['read_artifacts'].append(second)
    with pytest.raises(Fault) as exc: c.w.create(c.owner,full_project[0],body)
    assert exc.value.code=='ambiguous_acceptance'
    assert not c.s.all('SELECT id FROM tasks')


def test_a_read_reference_does_not_mean_it_is_implemented(full, full_project):
    c=full;p,_,q,_=full_project;second=add_same_name(c,p)
    body=body_for(full_project,[{'requirement':q,'acceptance':'AC-ADD'}]);body['read_artifacts'].append(second)
    t=c.w.create(c.owner,p,body)['id'];pairs=from_store(c.s,c.w.task(c.owner,t)['body'])['pairs']
    assert pairs=={(q,'AC-ADD')}
    report=c.k.trace(c.owner,p)
    other=next(x for x in report['missing'] if x['requirement']==second)
    assert 'acceptance_task:AC-ADD' in other['missing']
    original=next(x for x in report['missing'] if x['requirement']==q)
    assert 'acceptance_task:AC-ADD' not in original['missing']


def test_two_requirements_keep_two_distinct_review_markers(full, full_project):
    c=full;p,_,q,_=full_project;second=add_same_name(c,p)
    body=body_for(full_project,[{'requirement':q,'acceptance':'AC-ADD'},
                               {'requirement':second,'acceptance':'AC-ADD'}]);body['read_artifacts'].append(second)
    t=c.w.create(c.owner,p,body)['id'];view=review_task(c.s,c.w.task(c.owner,t)['body'])
    assert set(view['acceptance'])=={'AC-ADD',marker(q,'AC-ADD'),marker(second,'AC-ADD')}
    assert len(view['acceptance_identity'])==2
    assert c.w.task(c.owner,t)['body']['acceptance']==['AC-ADD']


@pytest.mark.parametrize('fault',['outside_reads','unknown_condition','duplicate','missing_mapping','wrong_kind','unknown_key','not_list'])
def test_explicit_reference_validation(full, full_project,fault):
    c=full;p,_,q,_=full_project;body=body_for(full_project,[{'requirement':q,'acceptance':'AC-ADD'}])
    if fault=='outside_reads':body['acceptance_refs'][0]['requirement']=add_same_name(c,p)
    elif fault=='unknown_condition':body['acceptance_refs'][0]['acceptance']='ABSENT'
    elif fault=='duplicate':body['acceptance_refs']*=2
    elif fault=='missing_mapping':body['acceptance_refs']=[]
    elif fault=='wrong_kind':
        other=accepted(c,p,'design','Not a requirement');body['read_artifacts'].append(other);body['acceptance_refs'][0]['requirement']=other
    elif fault=='unknown_key':body['acceptance_refs'][0]['extra']=True
    elif fault=='not_list':body['acceptance_refs']='REQ/AC'
    with pytest.raises(Fault):c.w.create(c.owner,p,body)


def test_legacy_unambiguous_review_labels_remain_compatible(full,full_project):
    c=full;task=make_task(c,full_project)
    material=from_store(c.s,c.w.task(c.owner,task)['body'])
    assert not material['explicit'] and material['required_coverage']==['AC-ADD']
    assert finish_task(c,full_project[0],task)['status']=='completed'


def test_explicit_pairs_require_actual_markers_through_complete_gate(full,full_project,tmp_path):
    c=full;p,_,q,_=full_project
    task=c.w.create(c.owner,p,body_for(full_project,[{'requirement':q,'acceptance':'AC-ADD'}]))['id']
    c.w.plan_tests(c.owner,task,{'checks':[{'id':'unit','argv':['python','-m','pytest','-q','test_calc.py'],
                                        'kind':'pytest','required_tests':['test_add']}]})
    c.w.ready(c.owner,task);c.w.claim(c.owner,p,task);c.rt.execute(c.owner,task,'fixture');c.rt.tests(c.owner,task)
    for role in ('spec','quality','test_adequacy'):c.rt.review(c.owner,task,role,'fixture')
    assert c.g.evaluate_task(c.owner,task)['verdict']=='pass'
    # A later protocol-shaped review which lists only the ambiguous human label
    # cannot replace the exact pair's coverage. It is an actually launched fixture.
    script=tmp_path/'labels_only.py'
    script.write_text("import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({'verdict':'pass','rationale':'fixture only','covered':['AC-ADD'],'findings':[], 'observations':[{'ref':p['subject'],'detail':'fixture input received'}],'dispositions':[]}))\n")
    c.rt.adapters.register(c.owner,'labels-only','fixture',sys.executable,[str(script)])
    c.rt.review(c.owner,task,'spec','labels-only')
    failed=c.g.evaluate_task(c.owner,task)
    assert 'review_coverage:spec' in failed['failures']


def test_legacy_ambiguity_is_detected_at_ready_trace_and_breakdown(setup):
    c,p,r,q,program,d,t,units=setup;second=add_same_name(c,p)
    # Simulate a pre-upgrade ambiguous definition; do not assert it is acceptable.
    body=c.w.task(c.owner,t)['body'];body['read_artifacts'].append(second)
    art=c.k.artifact(c.owner,second)
    with c.s.transaction():
        c.s.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(body).decode(),t))
        c.s.execute('INSERT INTO task_reads VALUES(?,?,?,?)',(t,second,art['revision'],art['digest']))
    assert 'acceptance_identity:ambiguous_acceptance' in c.g.evaluate_task(c.owner,t,'ready')['failures']
    assert any(any('ambiguous_acceptance' in code for code in x['missing']) for x in c.k.trace(c.owner,p)['missing'])
    changed=copy.deepcopy(units);changed[1]['obligations'].append({'requirement':second,'acceptance':'AC-ADD'})
    with pytest.raises(Fault) as exc:propose(setup,changed)
    assert exc.value.code=='ambiguous_acceptance'


def test_supplemental_check_does_not_create_an_invented_requirement():
    body={'acceptance':['AC-A','CHECK-DIAGNOSTICS'],'acceptance_refs':[{'requirement':'R-A','acceptance':'AC-A'}]}
    result=resolve(body,{'R-A':{'acceptance':['AC-A']}})
    assert result['pairs']=={('R-A','AC-A')} and result['supplemental_labels']==['CHECK-DIAGNOSTICS']


def test_pair_identity_is_not_simple_string_concatenation():
    assert marker('A:B','C')!=marker('A','B:C')
    assert marker('要件一','受入条件')==marker('要件一','受入条件')
