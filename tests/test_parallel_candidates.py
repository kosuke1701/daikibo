"""Read-only scheduling visibility for a main agent."""
from __future__ import annotations

from conftest import make_task


def _task(control, project, *, title, path, dependencies=(), reads=(), writes=()):
    pid, rid, requirement, _ = project
    row = control.w.create(control.owner, pid, {
        'title': title,
        'goal': f'Update {path}',
        'read_artifacts': [requirement],
        'write_paths': [path],
        'acceptance': ['AC-ADD'],
        'dependencies': list(dependencies),
        'repos': [rid],
        'non_goals': [],
        'resource_reads': list(reads),
        'resource_writes': list(writes),
    })
    control.w.plan_tests(control.owner, row['id'], {
        'checks': [{'id': 'unit', 'argv': ['python', '-m', 'pytest', '-q', 'test_calc.py'],
                    'kind': 'pytest', 'required_tests': ['test_add']}],
    })
    control.w.ready(control.owner, row['id'])
    return row['id']


def test_parallel_candidates_exposes_dependencies_and_both_conflict_kinds_without_mutation(
    full, full_project,
):
    project = full_project[0]
    running = make_task(full, full_project, paths=['shared.py'])
    full.w.claim(full.owner, project, running)
    independent = make_task(full, full_project, paths=['independent.py'])
    path_conflict = make_task(full, full_project, paths=['shared.py'])
    prerequisite = make_task(full, full_project, paths=['prerequisite.py'])
    full.s.execute("UPDATE tasks SET status='completed' WHERE id=?", (prerequisite,))
    dependent = make_task(full, full_project, paths=['dependent.py'], deps=[prerequisite])
    # A dependency can become stale after the dependent Task reached ready.
    full.s.execute("UPDATE tasks SET validity='needs_review' WHERE id=?", (prerequisite,))
    writer = _task(full, full_project, title='Write schema', path='writer.py', writes=['schema'])
    reader = _task(full, full_project, title='Read schema', path='reader.py', reads=['schema'])

    before = {
        'tasks': full.s.all('SELECT id,status,epoch,attempts FROM tasks ORDER BY id'),
        'attempts': full.s.one('SELECT count(*) AS n FROM execution_attempts')['n'],
        'events': full.s.one('SELECT count(*) AS n FROM events')['n'],
    }
    result = full.invoke(full.owner, 'task.parallel_candidates', {'project': project})
    after = {
        'tasks': full.s.all('SELECT id,status,epoch,attempts FROM tasks ORDER BY id'),
        'attempts': full.s.one('SELECT count(*) AS n FROM execution_attempts')['n'],
        'events': full.s.one('SELECT count(*) AS n FROM events')['n'],
    }
    assert after == before
    assert result['running_count'] == 1
    assert result['available_capacity'] == 3
    assert result['candidate_conflicts_scope'] == 'returned_page'
    assert result['claim_rechecks_current_state'] is True
    tasks = {item['task']: item for item in result['tasks']}
    assert tasks[independent]['can_claim_now'] is True
    assert tasks[path_conflict]['can_claim_now'] is False
    assert tasks[path_conflict]['running_conflicts'] == [
        {'task': running, 'reasons': ['write_path']},
    ]
    assert tasks[dependent]['can_claim_now'] is False
    assert tasks[dependent]['unmet_dependencies'] == [prerequisite]
    assert {item['task']: item['reasons'] for item in tasks[writer]['candidate_conflicts']}[reader] == ['resource_read_write']
    assert {item['task']: item['reasons'] for item in tasks[reader]['candidate_conflicts']}[writer] == ['resource_read_write']


def test_parallel_candidates_honors_capacity_pagination_and_read_contract(full, full_project):
    project = full_project[0]
    for index in range(4):
        task = make_task(full, full_project, paths=[f'running-{index}.py'])
        full.w.claim(full.owner, project, task)
    waiting = make_task(full, full_project, paths=['waiting.py'])
    first = full.invoke(full.owner, 'task.parallel_candidates', {
        'project': project, 'limit': 1, 'offset': 0,
    })
    assert first['available_capacity'] == 0
    assert first['tasks'][0]['task'] == waiting
    assert first['tasks'][0]['can_claim_now'] is False
    assert first['next_offset'] is None
    descriptor = full.invoke(full.owner, 'api.describe', {'method': 'task.parallel_candidates'})
    assert descriptor['methods']['task.parallel_candidates']['read_only'] is True
    assert descriptor['methods']['task.parallel_candidates']['body_contract']['writes'] is False
