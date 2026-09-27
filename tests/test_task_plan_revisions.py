"""Focused mechanics for immutable test-plan-only Task revisions."""
from __future__ import annotations

import sqlite3
import sys
import json

import pytest

from conftest import make_task
from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.supervisor import ALLOWED
from daikibo.control import Control
from daikibo.knowledge_history import inspect_archive
from daikibo.operations import restore_backup


def _old_plan():
    return {'checks': [{
        'id': 'unit', 'argv': ['python', '-m', 'pytest', '-q', 'test_calc.py'],
        'kind': 'pytest', 'required_tests': ['test_missing'],
    }]}


def _new_plan():
    return {'checks': [{
        'id': 'unit', 'argv': ['python', '-m', 'pytest', '-q', 'test_calc.py'],
        'kind': 'pytest', 'required_tests': ['test_add'],
    }]}


def _failed_runtime(c, project, task):
    c.w.plan_tests(c.owner, task, _old_plan())
    c.w.ready(c.owner, task)
    c.w.claim(c.owner, project[0], task)
    c.rt.execute(c.owner, task, 'fixture')
    observed = c.rt.tests(c.owner, task)
    receipt = observed['checks'][0]['receipt']
    assert c.g.receipt(receipt)['result']['missing'] == ['test_missing']
    return receipt


def _propose(c, task, receipt):
    row = c.w.task(c.owner, task)
    plan = c.s.one('SELECT digest FROM plans WHERE task=?', (task,))
    return c.task_revisions.propose_plan_revision(
        c.owner, task, row['revision'], plan['digest'], _new_plan(),
        'Replace the stale required test identity while preserving the Task definition',
        [receipt],
    )


def _review(c, proposal):
    script = c.s.home / 'plan_revision_impact_fixture.py'
    script.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "print(json.dumps({'verdict':'pass','rationale':'mechanical protocol fixture; not semantic judgment',"
        "'covered':p['context']['required_coverage'],'findings':[],"
        "'observations':[{'ref':p['subject'],'detail':'exact proposal context observed'}],"
        "'dispositions':[]}))\n"
    )
    if not c.s.one("SELECT name FROM adapters WHERE name=?", ('plan-impact-fixture',)):
        c.rt.adapters.register(c.owner, 'plan-impact-fixture', 'fixture', sys.executable, [str(script)])
    return c.rt.review(c.owner, proposal['id'], 'impact', 'plan-impact-fixture')


def test_plan_revision_proposal_apply_preserves_old_runtime_and_freezes_true_after(full, full_project):
    c = full
    task = make_task(c, full_project)
    old_receipt = _failed_runtime(c, full_project, task)
    before = c.task_revisions.snapshot(c.owner, task)
    old_candidate = before['task']['candidate']
    old_attempts = before['task']['attempts']
    proposal = _propose(c, task, old_receipt)

    material = c.task_revisions.get(c.owner, proposal['id'])['body']['material']
    assert material['proposed_task'] == before['task']['body']
    assert material['proposed_plan'] == _new_plan()
    assert material['candidate']['id'] == old_candidate
    assert material['evidence_refs'][0]['id'] == old_receipt
    assert c.w.task(c.owner, task)['body'] == before['task']['body']
    assert c.w.task(c.owner, task)['candidate'] == old_candidate

    review = _review(c, proposal)
    result = c.task_revisions.apply(c.owner, proposal['id'], proposal['digest'], review['receipt'])
    after = c.task_revisions.snapshot(c.owner, task)
    assert result['new_test_plan_required'] is False
    assert result['plan_revised'] is True
    assert after['task']['revision'] == before['task']['revision'] + 1
    assert after['task']['epoch'] == before['task']['epoch'] + 1
    assert after['task']['body'] == before['task']['body']
    assert after['task']['candidate'] is None
    assert after['task']['attempts'] == old_attempts
    assert after['test_plan']['body'] == _new_plan()
    assert c.s.one('SELECT id FROM candidates WHERE id=?', (old_candidate,)) is not None
    assert c.g.receipt(old_receipt)['result']['missing'] == ['test_missing']
    history = c.task_revisions.history_record(c.owner, result['history'])
    assert history['body']['format'] == 'daikibo.task-plan-revision.v1'
    assert history['body']['before'] == before
    assert history['body']['after'] == after
    assert history['body']['proposed_task'] == before['task']['body']
    assert history['body']['proposed_plan'] == _new_plan()
    assert 'no_approved_test_plan' not in [f for f in c.g.evaluate_task(c.owner, task, 'ready')['failures']
                                           if f == 'no_approved_test_plan']
    # A frozen plan is not a semantic pass; the Task is deliberately planned.
    assert c.w.task(c.owner, task)['status'] == 'planned'


def test_plan_revision_route_is_idempotent_and_old_format_stays_strict(full, full_project):
    c = full
    task = make_task(c, full_project)
    receipt = _failed_runtime(c, full_project, task)
    row = c.w.task(c.owner, task)
    plan = c.s.one('SELECT digest FROM plans WHERE task=?', (task,))['digest']
    params = {'task': task, 'expected_revision': row['revision'], 'expected_plan_digest': plan,
              'body': _new_plan(), 'reason': 'Correct the test identity with exact failure evidence',
              'evidence_refs': [receipt]}
    assert 'task.propose_plan_revision' in c.routes
    first = c.request('ignored', {'id': 'req-plan-1', 'method': 'task.propose_plan_revision', 'params': params})
    replay = c.request('ignored', {'id': 'req-plan-1', 'method': 'task.propose_plan_revision', 'params': params})
    assert replay == first
    assert 'task.propose_plan_revision' in ALLOWED
    with pytest.raises(Fault) as exc:
        c.request('ignored', {'id': 'req-plan-1', 'method': 'task.propose_plan_revision',
                              'params': {**params, 'reason': 'changed'}})
    assert exc.value.code == 'idempotency_conflict'
    with pytest.raises(Fault) as exc:
        c.task_revisions.propose_plan_revision(c.owner, task, row['revision'], plan, _old_plan(), 'same', [receipt])
    assert exc.value.code == 'empty_revision'


def test_plan_revision_impact_review_uses_existing_jobs_subject_routing(full, full_project):
    c = full
    task = make_task(c, full_project)
    proposal = _propose(c, task, _failed_runtime(c, full_project, task))
    script = c.s.home / 'plan_revision_job_fixture.py'
    script.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "print(json.dumps({'verdict':'pass','rationale':'mechanical protocol fixture',"
        "'covered':p['context']['required_coverage'],'findings':[],"
        "'observations':[{'ref':p['subject'],'detail':'job context observed'}],"
        "'dispositions':[]}))\n"
    )
    c.rt.adapters.register(c.owner, 'plan-job-fixture', 'fixture', sys.executable, [str(script)])
    args = {'subject': proposal['id'], 'role': 'impact', 'adapter': 'plan-job-fixture'}
    assert c.jobs.subject_project('review', args) == full_project[0]
    job = c.jobs.submit(c.owner, 'review', args)
    outcome = c.jobs.run_one(c.s.one('SELECT * FROM jobs WHERE id=?', (job['id'],)))
    assert outcome['status'] == 'succeeded'
    result = outcome['result']
    assert result['result']['verdict'] == 'pass'
    applied = c.task_revisions.apply(c.owner, proposal['id'], proposal['digest'], result['receipt'])
    assert applied['plan_revised'] is True


def test_plan_revision_without_candidate_has_optional_review_context(full, full_project):
    c = full
    project, repo, requirement, _ = full_project
    task = c.w.create(c.owner, project, {
        'title': 'Plan before execution',
        'goal': 'WRITE:' + json.dumps({'calc.py': 'def add(a,b):\n    return a+b\n'}),
        'read_artifacts': [requirement],
        'write_paths': ['calc.py'],
        'acceptance': ['AC-ADD'],
        'dependencies': [],
        'repos': [repo],
        'non_goals': [],
    })['id']
    c.w.plan_tests(c.owner, task, _old_plan())
    row = c.w.task(c.owner, task)
    old_plan = c.s.one('SELECT digest FROM plans WHERE task=?', (task,))['digest']
    proposal = c.task_revisions.propose_plan_revision(
        c.owner, task, row['revision'], old_plan,
        {**_new_plan(), 'rationale': 'pre-execution correction'},
        'Correct the plan before the first execution', [],
    )
    review = _review(c, proposal)
    result = c.task_revisions.apply(c.owner, proposal['id'], proposal['digest'], review['receipt'])
    assert result['plan_revised'] is True
    assert c.w.task(c.owner, task)['candidate'] is None


def test_plan_revision_preserves_cancelled_task_state_contract(full, full_project):
    c = full
    task = make_task(c, full_project)
    row = c.w.task(c.owner, task)
    old_plan = c.s.one('SELECT digest FROM plans WHERE task=?', (task,))['digest']
    c.w.cancel(c.owner, task, 'Cancel before plan correction')
    with pytest.raises(Fault) as exc:
        c.task_revisions.propose_plan_revision(
            c.owner, task, row['revision'], old_plan, _new_plan(), 'cancelled', [],
        )
    assert exc.value.code == 'invalid_state'

    task = make_task(c, full_project)
    row = c.w.task(c.owner, task)
    old_plan = c.s.one('SELECT digest FROM plans WHERE task=?', (task,))['digest']
    proposal = c.task_revisions.propose_plan_revision(
        c.owner, task, row['revision'], old_plan,
        {**_new_plan(), 'rationale': 'pending correction'}, 'Cancel after proposal', [],
    )
    c.w.cancel(c.owner, task, 'Cancel pending plan correction')
    with pytest.raises(Fault) as exc:
        c.task_revisions.current(c.owner, proposal['id'])
    assert exc.value.code == 'stale_task_proposal'
    with pytest.raises(Fault) as exc:
        c.task_revisions.apply(c.owner, proposal['id'], proposal['digest'], 'no-review')
    assert exc.value.code == 'stale_task_proposal'


@pytest.mark.parametrize('mutation', ['task', 'plan', 'candidate', 'policy'])
def test_plan_revision_currentness_rejects_each_identity_change(full, full_project, mutation):
    c = full
    task = make_task(c, full_project)
    receipt = _failed_runtime(c, full_project, task)
    proposal = _propose(c, task, receipt)
    if mutation == 'task':
        c.s.execute('UPDATE tasks SET revision=revision+1 WHERE id=?', (task,))
    elif mutation == 'plan':
        c.s.execute('UPDATE plans SET body=?,digest=? WHERE task=?',
                    (canonical({**_new_plan(), 'rationale': 'unrelated current plan'}).decode(),
                     digest({**_new_plan(), 'rationale': 'unrelated current plan'}), task))
    elif mutation == 'candidate':
        c.s.execute('UPDATE tasks SET candidate=NULL WHERE id=?', (task,))
    elif mutation == 'policy':
        project = full_project[0]
        body = c.g.policy(project)['body']; body['max_parallel'] += 1
        c.s.execute('UPDATE policies SET body=?,digest=? WHERE project=?',
                    (canonical(body).decode(), digest(body), project))
    with pytest.raises(Fault) as exc:
        c.task_revisions.current(c.owner, proposal['id'])
    assert exc.value.code in {'stale_task_proposal', 'stale_plan', 'integrity_error', 'invalid_evidence'}


def test_plan_revision_rejects_foreign_receipt_and_caller_candidate_values(full, full_project):
    c = full
    task = make_task(c, full_project)
    receipt = _failed_runtime(c, full_project, task)
    row = c.w.task(c.owner, task)
    plan = c.s.one('SELECT digest FROM plans WHERE task=?', (task,))['digest']
    with pytest.raises(Fault) as exc:
        c.task_revisions.propose_plan_revision(
            c.owner, task, row['revision'], plan, _new_plan(), 'foreign receipt', ['RUN-unknown'])
    assert exc.value.code in {'invalid_evidence', 'not_found'}
    c.s.execute('UPDATE tasks SET candidate=? WHERE id=?', ('CANDIDATE-caller', task))
    with pytest.raises(Fault) as exc:
        c.task_revisions.propose_plan_revision(
            c.owner, task, row['revision'], plan, _new_plan(), 'caller candidate', [receipt])
    assert exc.value.code in {'stale_task_proposal', 'not_found'}


def test_plan_revision_failed_pin_rolls_back_plan_task_history_and_proposal(full, full_project, monkeypatch):
    c = full
    task = make_task(c, full_project)
    receipt = _failed_runtime(c, full_project, task)
    proposal = _propose(c, task, receipt)
    review = _review(c, proposal)
    before = c.task_revisions.snapshot(c.owner, task)

    def broken(*args, **kwargs):
        raise RuntimeError('injected plan material failure')

    monkeypatch.setattr(c.w.verification_materials, 'pin_test_plan', broken)
    with pytest.raises(RuntimeError):
        c.task_revisions.apply(c.owner, proposal['id'], proposal['digest'], review['receipt'])
    assert c.task_revisions.snapshot(c.owner, task) == before
    assert c.task_revisions.get(c.owner, proposal['id'])['status'] == 'proposed'
    assert not c.s.all('SELECT id FROM task_revision_history')


def test_plan_revision_archive_backup_and_legacy_history_validator(full, full_project, tmp_path):
    c = full
    task = make_task(c, full_project)
    receipt = _failed_runtime(c, full_project, task)
    proposal = _propose(c, task, receipt)
    review = _review(c, proposal)
    result = c.task_revisions.apply(c.owner, proposal['id'], proposal['digest'], review['receipt'])
    baseline = c.k.baseline(c.owner, full_project[0])
    archive = c.history.export_archive(c.owner, baseline['id'])
    assert archive['baseline'] == baseline['id']
    assert archive['path']
    backup = c.ops.backup(c.owner)
    assert backup['path']
    c.task_revisions.history_record(c.owner, result['history'])
    # A plan-only history cannot be made to masquerade as the old after-plan-empty format.
    with pytest.raises(sqlite3.IntegrityError):
        c.s.execute('UPDATE task_revision_history SET body=? WHERE id=?', ('{}', result['history']))


def test_plan_revision_backup_restore_replay_and_archive_tamper_rejection(full, full_project, tmp_path):
    c = full
    task = make_task(c, full_project)
    receipt = _failed_runtime(c, full_project, task)
    proposal = _propose(c, task, receipt)
    review = _review(c, proposal)
    result = c.task_revisions.apply(c.owner, proposal['id'], proposal['digest'], review['receipt'])

    backup = c.ops.backup(c.owner)
    restored_home = tmp_path / 'restored-control'
    restore_backup(backup['path'], restored_home, backup['sha256'])
    restored = Control(restored_home, 'validation', start_workers=False)
    try:
        owner = restored.sec.authenticate(None)
        assert restored.task_revisions.get(owner, proposal['id'])['status'] == 'applied'
        history = restored.task_revisions.history_record(owner, result['history'])
        assert history['body']['format'] == 'daikibo.task-plan-revision.v1'
        replay = restored.task_revisions.apply(owner, proposal['id'], proposal['digest'], review['receipt'])
        assert replay['replayed'] is True and replay['history'] == result['history']
        restored_baseline = restored.k.baseline(owner, full_project[0])
        restored_archive = restored.history.export_archive(owner, restored_baseline['id'])
        roundtrip = inspect_archive(restored_archive['path'], restored_archive['sha256'])
        assert roundtrip['verified'] and roundtrip['counts']['task_revision_history'] >= 1
    finally:
        restored.close()

    baseline = c.k.baseline(c.owner, full_project[0])
    archive = c.history.export_archive(c.owner, baseline['id'])
    checked = inspect_archive(archive['path'], archive['sha256'])
    assert checked['verified'] and checked['counts']['task_revision_history'] >= 1
    from test_chunked_knowledge import rewrite
    tampered = tmp_path / 'tampered-plan-history.dkarchive'

    def mutate(manifest, rows, objects):
        history = next(item for item in rows if item['section'] == 'task_revision_history')['row']
        history['body']['after']['test_plan'] = None
        history['digest'] = digest(history['body'])

    tampered_sha = rewrite(archive['path'], tampered, mutate)
    with pytest.raises(Fault):
        inspect_archive(tampered, tampered_sha)
