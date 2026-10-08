"""Typed layer scope, source-backed typo repair, and narrow task recertification."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from daikibo.common import Fault


def _accept_requirement(c,project,title,statement,source=None):
    source=source or c.k.source(c.owner,project,'Source for '+title)['id']
    proposal=c.k.propose(c.owner,project,'requirement',{'title':title,'statement':statement,
        'acceptance':['AC-'+title.upper().replace(' ','-')],'source_refs':[source]})
    c.k.accept(c.owner,proposal['id'],1)
    return proposal['id']


def _typed_reviewer(c,tmp_path,*,scope='within_scope',effect='preserves_meaning',target=None,name='typed-scope'):
    script=tmp_path/(name+'.py')
    script.write_text('''import json,sys
p=json.load(sys.stdin);context=p.get("context",{});packets=[]
def walk(value):
 if isinstance(value,dict):
  review=value.get("scope_review")
  if isinstance(review,dict) and review.get("format")=="change-scope-review.v1": packets.append(review)
  for child in value.values(): walk(child)
 elif isinstance(value,list):
  for child in value: walk(child)
walk(context)
dispositions=[];observations=[]
for review in packets:
 layer=review.get("layer","local_repair")
 for item in review.get("required_dispositions",[]):
  kind=item.get("kind");marker=item["id"]
  if kind=="layer_scope": resolution=sys.argv[1]
  elif kind=="layer_target": resolution=(sys.argv[2] if sys.argv[2]!="auto" else layer)
  elif kind=="delta_effect":
   resolution=sys.argv[3];observations.append({"ref":item["subject"],"detail":"Compared the exact source and before/after bodies for this protocol regression."})
  elif kind=="review_carry_and_task_fence": resolution="unaffected"
  elif kind=="interface_consumer": resolution="addressed"
  elif kind=="declared_unknown_consumer": resolution="unresolved"
  else: continue
  dispositions.append({"id":marker,"resolution":resolution,"reason":"Explicit deterministic test protocol result."})
print(json.dumps({"verdict":"pass","rationale":"Deterministic regression protocol fixture.",
 "covered":context.get("required_coverage",[]),"findings":[],"observations":observations,
 "dispositions":dispositions}))
''')
    c.rt.adapters.register(c.owner,name,'fixture',sys.executable,
                          [str(script),scope,target or 'auto',effect])
    return name


def _task_reading(c,project,repo,artifact):
    return c.w.create(c.owner,project,{'title':'Unstarted reader','goal':'Inspect the accepted requirement',
        'read_artifacts':[artifact],'write_paths':['calc.py'],'acceptance':['AC-UNSTARTED'],
        'dependencies':[],'repos':[repo],'non_goals':[]})['id']


def test_source_backed_statement_typo_applies_without_human_reanswer_and_recarries_only_unstarted_task(
        full,full_project,tmp_path):
    c=full;project,repo,artifact,_=full_project
    current=c.k.artifact(c.owner,artifact)
    misspelled={**current['body'],'statement':'Returns the exact arithmatic sum'}
    c.k._revise(c.owner,current,current['revision'],misspelled,'Fixture seeds an accepted statement typo','accepted')
    task=_task_reading(c,project,repo,artifact)
    before_task=c.w.task(c.owner,task)
    before_pin=c.s.one('SELECT revision,digest FROM task_reads WHERE task=? AND artifact=?',(task,artifact),True)
    source=c.k.source(c.owner,project,'Correct “arithmatic” to “arithmetic” in the requirement statement.')
    corrected={**misspelled,'statement':'Returns the exact arithmetic sum'}
    change=c.p.change(c.owner,project,{'title':'Correct requirement typo','origin':'user',
        'reason':'Apply the exact source-backed spelling correction','source':source['id'],
        'affected':[artifact],'evidence':[source['id']],
        'deltas':[{'artifact':artifact,'expected_revision':2,'body':corrected}]})
    assert change['stage']=='local_repair'
    reviewer=_typed_reviewer(c,tmp_path)
    review=c.rt.review(c.owner,change['id'],'consistency',reviewer)
    result=c.p.apply_technical_change(c.owner,change['id'],review['receipt'])
    assert result['stage']=='ready_for_reimplementation'
    assert c.k.artifact(c.owner,artifact)['body']['statement']=='Returns the exact arithmetic sum'
    assert c.s.one('SELECT count(*) AS n FROM decisions WHERE json_extract(body,'"'"'$.change'"'"')=?',
                    (change['id'],))['n']==0
    after_task=c.w.task(c.owner,task)
    after_pin=c.s.one('SELECT revision,digest FROM task_reads WHERE task=? AND artifact=?',(task,artifact),True)
    assert after_task['validity']=='current' and after_task['epoch']>before_task['epoch']
    assert after_pin['revision']==3 and after_pin['digest']!=before_pin['digest']
    proofs=c.s.all("SELECT body FROM events WHERE kind='task_revalidated_after_unaffected_change' "
                   "AND json_extract(body,'$.task')=?",(task,))
    assert len(proofs)==1
    proof=__import__('json').loads(proofs[0]['body'])
    assert proof['receipt']==review['receipt'] and proof['old_pins'][0]['revision']==2


def test_asserted_parent_contract_allows_child_requirement_adjustment_without_kind_gate(
        full,full_project,tmp_path):
    c=full;project,_,seed,_=full_project
    parent=_accept_requirement(c,project,'Parent contract','For integer inputs, return their exact mathematical sum.')
    child=_accept_requirement(c,project,'Child detail','Return the exact sum for two integer inputs.')
    c.k.link(c.owner,parent,child,'decomposes','asserted','This child refines the accepted parent contract.')
    source=c.k.source(c.owner,project,'Clarify the child wording without changing the accepted parent contract.')
    current=c.k.artifact(c.owner,child)
    after={**current['body'],'statement':'For two integer inputs, return their exact mathematical sum.'}
    change=c.p.change(c.owner,project,{'title':'Clarify child requirement','origin':'design',
        'reason':'Make a child requirement precise within its accepted parent','affected':[child],
        'evidence':[source['id']],'deltas':[{'artifact':child,'expected_revision':1,'body':after}]})
    _,material=c.p.change_review_material(change['id'])
    path=next(item for item in material['upper_contracts']['upper_paths'] if item['root']==child)
    assert path['upper']==parent and path['path'][0]['relation']=='decomposes'
    reviewer=_typed_reviewer(c,tmp_path,scope='within_scope',effect='within_current_contract')
    review=c.rt.review(c.owner,change['id'],'consistency',reviewer)
    applied=c.p.apply_technical_change(c.owner,change['id'],review['receipt'])
    assert applied['stage']=='ready_for_reimplementation'
    assert c.k.artifact(c.owner,child)['revision']==2
    assert c.k.artifact(c.owner,parent)['revision']==1


def test_unrelated_or_reverse_requirement_link_cannot_authorize_child_scope(
        full,full_project,tmp_path):
    c=full;project,_,_,_=full_project
    unrelated=_accept_requirement(c,project,'Unrelated parent','This governs a separate behavior.')
    child=_accept_requirement(c,project,'Unlinked child','The child requirement is independent.')
    # The inverse direction is not a child-to-parent authority edge.
    c.k.link(c.owner,child,unrelated,'decomposes','asserted','Deliberately reversed for this negative test.')
    source=c.k.source(c.owner,project,'Clarify a distinct child behavior.')
    current=c.k.artifact(c.owner,child)
    change=c.p.change(c.owner,project,{'title':'Adjust unrelated child','origin':'design',
        'reason':'Attempt to borrow an unrelated accepted requirement as authority','affected':[child],
        'evidence':[source['id']],'deltas':[{'artifact':child,'expected_revision':1,
            'body':{**current['body'],'statement':'A changed meaning for the independent child.'}}]})
    _,material=c.p.change_review_material(change['id'])
    assert not any(path['root']==child and path['upper']==unrelated
                   for path in material['upper_contracts']['upper_paths'])
    reviewer=_typed_reviewer(c,tmp_path,scope='within_scope',effect='within_current_contract',
                             name='unsupported-parent')
    review=c.rt.review(c.owner,change['id'],'consistency',reviewer)
    with pytest.raises(Fault) as rejected:
        c.p.apply_technical_change(c.owner,change['id'],review['receipt'])
    assert rejected.value.code=='upper_contract_required'
    assert c.k.artifact(c.owner,child)['revision']==1
    assert c.k.artifact(c.owner,unrelated)['revision']==1


def test_scope_exceeded_routes_directly_to_the_reviewed_module(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    source=c.k.source(c.owner,project,'A product meaning change needs a module decision.')
    current=c.k.artifact(c.owner,artifact)
    change=c.p.change(c.owner,project,{'title':'Escalate to module','origin':'implementation',
        'reason':'The local layer cannot authorize this adjustment','affected':[artifact],
        'evidence':[source['id']],'deltas':[{'artifact':artifact,'expected_revision':1,
            'body':{**current['body'],'statement':'A changed meaning.'}}]})
    reviewer=_typed_reviewer(c,tmp_path,scope='upper_scope_required',effect='changes_upper_contract',
                             target='module_replan',name='module-target-review')
    review=c.rt.review(c.owner,change['id'],'consistency',reviewer)
    result=c.p.attempt(c.owner,change['id'],'local_repair',{'hypothesis':'This layer lacks authority.',
        'alternatives':['Submit to the identified module layer.'],'evidence':[review['receipt']],
        'outcome':'scope_exceeded','remaining_unknown':'None for the authority boundary.',
        'review_receipt':review['receipt']})
    assert result['stage']=='module_replan'
    _,material=c.p.change_review_material(change['id'])
    assert material['scope_review']['layer']=='module_replan'
    assert c.s.one('SELECT count(*) AS n FROM attempts WHERE change_id=?',(change['id'],))['n']==1


def test_product_decision_path_uses_latest_system_attempt_event_order(full,full_project,tmp_path):
    c=full;project,_,artifact,_=full_project
    source=c.k.source(c.owner,project,'Review bounded options before requesting a product decision.')
    current=c.k.artifact(c.owner,artifact)
    proposed={**current['body'],'statement':'Candidate text for the feasibility-history regression.'}
    change=c.p.change(c.owner,project,{'title':'System feasibility history','origin':'implementation',
        'reason':'Verify that event order, rather than wall-clock timestamps, selects the final attempt',
        'affected':[artifact],'evidence':[source['id']],
        'deltas':[{'artifact':artifact,'expected_revision':current['revision'],'body':proposed}]})
    reviewer=_typed_reviewer(c,tmp_path,name='system-attempt-order')

    def reviewed_attempt(level,outcome):
        review=c.rt.review(c.owner,change['id'],'feasibility',reviewer)
        assert review['result']['verdict']=='pass', review['result']
        result=c.p.attempt(c.owner,change['id'],level,{
            'hypothesis':'Assess the current layer with independent scope evidence.',
            'alternatives':['A documented alternative at this layer.'],
            'evidence':[review['receipt']],'outcome':outcome,
            'remaining_unknown':'No unresolved scope evidence in this fixture.',
            'review_receipt':review['receipt']})
        return review,result

    _,at_module=reviewed_attempt('local_repair','no_solution_found')
    assert at_module['stage']=='module_replan'
    _,at_system=reviewed_attempt('module_replan','no_solution_found')
    assert at_system['stage']=='system_replan'
    first_system,_=reviewed_attempt('system_replan','resource_exhausted')
    final_system,at_product=reviewed_attempt('system_replan','no_solution_found')
    assert at_product['stage']=='awaiting_product_decision'

    attempts=c.s.all("SELECT id,body FROM attempts WHERE change_id=? AND level='system_replan'",(change['id'],))
    resource=next(row for row in attempts if __import__('json').loads(row['body'])['outcome']=='resource_exhausted')
    terminal=next(row for row in attempts if __import__('json').loads(row['body'])['outcome']=='no_solution_found')
    # Simulate a wall-clock rollback/skew: the earlier event now has the later
    # created timestamp, but must not hide the final system no-solution event.
    c.s.execute('UPDATE attempts SET created=? WHERE id=?',(9999999999.0,resource['id']))
    c.s.execute('UPDATE attempts SET created=? WHERE id=?',(0.0,terminal['id']))
    _,material=c.p.change_review_material(change['id'])
    scope=c.p._validate_change_scope_result(material,final_system['result'],allow_upper=True)
    assert c.p._has_current_system_no_solution_path(change['id'],scope) is True


def test_exact_no_effect_stays_open_without_revision_fence_or_apply(full,full_project,tmp_path):
    c=full;project,repo,artifact,_=full_project
    task=_task_reading(c,project,repo,artifact)
    original=c.k.artifact(c.owner,artifact)
    source=c.k.source(c.owner,project,'Clarify the requirement without changing its accepted text.')
    change=c.p.change(c.owner,project,{'title':'No-op request','origin':'user',
        'reason':'Check whether this exact proposal changes the accepted artifact','source':source['id'],
        'affected':[artifact],'evidence':[source['id']],
        'deltas':[{'artifact':artifact,'expected_revision':original['revision'],
                   'body':original['body']}]})
    assert change['no_effect'] is True
    assert change['stage']=='local_repair'
    before_task=c.w.task(c.owner,task)
    before_row=c.s.one('SELECT validity,epoch,revision FROM tasks WHERE id=?',(task,),True)
    assert before_row['validity']=='current' and before_row['epoch']==before_task['epoch']
    assert c.s.one('SELECT count(*) AS n FROM blocks WHERE task=? AND kind=\'change\' AND ref=?',
                   (task,change['id']))['n']==0
    assert c.k.artifact(c.owner,artifact)['revision']==original['revision']
    no_effect_events=c.s.all("SELECT body FROM events WHERE kind='change_no_effect_observed' "
                             "AND json_extract(body,'$.change')=?",(change['id'],))
    assert len(no_effect_events)==1
    assert __import__('json').loads(no_effect_events[0]['body'])['request_fulfilled'] is False

    reviewer=_typed_reviewer(c,tmp_path,name='no-effect-apply')
    review=c.rt.review(c.owner,change['id'],'consistency',reviewer)
    with pytest.raises(Fault) as rejected:
        c.p.apply_technical_change(c.owner,change['id'],review['receipt'])
    assert rejected.value.code=='change_has_no_effect'
    after_row=c.s.one('SELECT validity,epoch,revision FROM tasks WHERE id=?',(task,),True)
    assert after_row==before_row
    assert c.k.artifact(c.owner,artifact)['revision']==original['revision']
    current_change=c.p.change_get(c.owner,change['id'])
    assert current_change['stage']=='local_repair' and current_change['no_effect'] is True


def test_substantive_change_revised_to_no_effect_restores_only_its_clean_task(full,full_project):
    c=full;project,repo,artifact,_=full_project
    task=_task_reading(c,project,repo,artifact)
    before_task=c.w.task(c.owner,task)
    original=c.k.artifact(c.owner,artifact)
    source=c.k.source(c.owner,project,'The accepted requirement already satisfies this request.')
    changed={**original['body'],'statement':'A substantive proposed change.'}
    change=c.p.change(c.owner,project,{'title':'Proposed behavior change','origin':'design',
        'reason':'Exercise safe no-effect recertification','affected':[artifact],
        'evidence':[source['id']],'deltas':[{'artifact':artifact,
            'expected_revision':original['revision'],'body':changed}]})
    fenced=c.w.task(c.owner,task)
    assert fenced['validity']=='needs_review' and fenced['epoch']==before_task['epoch']+1
    assert c.s.one('SELECT 1 FROM blocks WHERE task=? AND kind=\'change\' AND ref=?',
                   (task,change['id'])) is not None

    result=c.p.set_delta(c.owner,change['id'],1,[{'artifact':artifact,
        'expected_revision':original['revision'],'body':original['body']}],
        'The actual proposal has no artifact effect')
    assert result['no_effect'] is True
    restored=c.w.task(c.owner,task)
    assert restored['validity']=='current' and restored['epoch']==before_task['epoch']+1
    assert c.s.one('SELECT 1 FROM blocks WHERE task=? AND kind=\'change\' AND ref=?',
                   (task,change['id'])) is None
    proof=c.s.one("SELECT body FROM events WHERE kind='task_revalidated_after_no_effect_change' "
                  "AND json_extract(body,'$.task')=?",(task,))
    assert proof is not None


def test_no_effect_to_substantive_delta_stops_impact_tasks_again(full,full_project):
    c=full;project,repo,artifact,_=full_project
    task=_task_reading(c,project,repo,artifact)
    original=c.k.artifact(c.owner,artifact)
    source=c.k.source(c.owner,project,'Update the requirement behavior.')
    change=c.p.change(c.owner,project,{'title':'Initially unchanged proposal','origin':'design',
        'reason':'Exercise no-effect to substantive transition','affected':[artifact],
        'evidence':[source['id']],'deltas':[{'artifact':artifact,
            'expected_revision':original['revision'],'body':original['body']}]})
    assert c.w.task(c.owner,task)['validity']=='current'
    changed={**original['body'],'statement':'A substantive proposed change.'}
    result=c.p.set_delta(c.owner,change['id'],1,[{'artifact':artifact,
        'expected_revision':original['revision'],'body':changed}],
        'Replace the no-op with the substantive proposal')
    assert result['no_effect'] is False
    after=c.w.task(c.owner,task)
    assert after['validity']=='needs_review'
    assert c.s.one('SELECT 1 FROM blocks WHERE task=? AND kind=\'change\' AND ref=?',
                   (task,change['id'])) is not None
