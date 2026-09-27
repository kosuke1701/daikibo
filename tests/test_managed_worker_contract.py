"""Managed worker context and self-wait diagnostics use local protocol fixtures only."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from daikibo.common import Fault, digest, parse_json
from daikibo.runtime import FIXED_TEST_CONTRACT, MANAGED_OUTPUT_CONTRACT, SNAPSHOT_IDENTITY_CONTEXT
from conftest import make_task


MANAGED_KEYS = (
    'DAIKIBO_MANAGED_RUN',
    'DAIKIBO_RUN_ID',
    'DAIKIBO_RUN_ROLE',
    'DAIKIBO_TASK_ID',
    'DAIKIBO_JOB_ID',
)


def _register_script(c, tmp_path, name, source):
    path = tmp_path / (name + '.py')
    path.write_text(source)
    c.rt.adapters.register(c.owner, name, 'fixture', sys.executable, [str(path)])
    return name


def _job_row(c, job):
    return c.s.one('SELECT * FROM jobs WHERE id=?', (job['id'],), True)


def _receipt(c, receipt_id):
    return parse_json(c.s.one('SELECT body FROM receipts WHERE id=?', (receipt_id,), True)['body'])


def test_managed_execute_returns_then_collector_seals_candidate_and_records_context(full, full_project, tmp_path, monkeypatch):
    c = full
    task = make_task(c, full_project)
    script = _register_script(c, tmp_path, 'managed-implementer', """
import json
import hashlib
import os
from pathlib import Path

prompt = json.load(__import__('sys').stdin)
execution = prompt['managed_execution']
assert execution['role'] == 'implementer'
assert execution['task_id'] == os.environ['DAIKIBO_TASK_ID']
assert execution['job_id'] == os.environ['DAIKIBO_JOB_ID']
Path('calc.py').write_text('def add(a,b):\\n    return a+b\\n')
print(json.dumps({'message':'returned to collector',
                  'verification':['local fixture check'],
                  'env':{key:os.environ.get(key) for key in %r},
                  'test_plan':prompt['test_plan'],
                  'instructions':prompt['instructions'],
                  'prompt_digest':hashlib.sha256(json.dumps(prompt,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
                  'without_plan_digest':hashlib.sha256(json.dumps({key:value for key,value in prompt.items() if key != 'test_plan'},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()}))
""" % (MANAGED_KEYS,))

    # Stale context from a parent managed run must not leak into the new one.
    for key in MANAGED_KEYS:
        monkeypatch.setenv(key, 'STALE-' + key)
    c.w.claim(c.owner, full_project[0], task)
    job = c.jobs.submit(c.owner, 'execute', {'task': task, 'adapter': script})
    outcome = c.jobs.run_one(_job_row(c, job))

    assert outcome['status'] == 'succeeded'
    result = outcome['result']
    assert result['status'] == 'submitted'
    assert c.w.task(c.owner, task)['status'] == 'submitted'
    candidate = c.s.one('SELECT id,body,implementation_run FROM candidates WHERE task=?', (task,), True)
    assert candidate is not None
    candidate_body = parse_json(candidate['body'])

    receipt = _receipt(c, result['receipt'])
    run = parse_json(c.s.one('SELECT body FROM runs WHERE id=?', (receipt['run'],), True)['body'])
    expected = receipt['environment']['managed_context']
    assert set(expected) == set(MANAGED_KEYS)
    assert expected['DAIKIBO_MANAGED_RUN'] == '1'
    assert expected['DAIKIBO_JOB_ID'] == job['id']
    assert expected['DAIKIBO_TASK_ID'] == task
    assert expected['DAIKIBO_RUN_ROLE'] == 'implementer'
    assert result['receipt'] == receipt['id']
    assert result['receipt'] == candidate_body['implementation_receipt']
    assert receipt['run'] == candidate['implementation_run']
    assert run['job'] == job['id']
    assert run['environment']['managed_context'] == expected
    assert receipt['result']['env'] == expected
    assert receipt['result']['message'] == 'returned to collector'
    saved_plan = c.s.one('SELECT body,digest FROM plans WHERE task=?', (task,), True)
    assert receipt['result']['test_plan'] == {'body':parse_json(saved_plan['body']), 'digest':saved_plan['digest']}
    assert FIXED_TEST_CONTRACT in receipt['result']['instructions']
    assert receipt['result']['instructions'].count(MANAGED_OUTPUT_CONTRACT) == 1
    assert SNAPSHOT_IDENTITY_CONTEXT in receipt['result']['instructions']
    assert receipt['result']['prompt_digest'] == receipt['input_digest']
    assert receipt['result']['without_plan_digest'] != receipt['input_digest']


def test_context_package_carries_frozen_plan_and_goes_stale_when_plan_changes(full, full_project):
    c = full
    task = make_task(c, full_project)
    first = c.ctx.task_context(c.owner, task)
    saved = c.s.one('SELECT body,digest FROM plans WHERE task=?', (task,), True)
    assert first['package']['mandatory']['test_plan'] == {'body':parse_json(saved['body']), 'digest':saved['digest']}
    assert first['package']['mandatory']['binding'] == c.g.task_binding(task)
    assert c.ctx.fresh(c.owner, first['id'])['fresh']

    changed = parse_json(saved['body'])
    changed['rationale'] = 'Updated fixture plan detail'
    c.w.plan_tests(c.owner, task, changed)
    assert not c.ctx.fresh(c.owner, first['id'])['fresh']
    second = c.ctx.task_context(c.owner, task)
    current = c.s.one('SELECT body,digest FROM plans WHERE task=?', (task,), True)
    assert second['package']['mandatory']['test_plan'] == {'body':parse_json(current['body']), 'digest':current['digest']}
    assert second['digest'] != first['digest']


def test_direct_command_observation_removes_stale_ids_and_keeps_empty_prompt(full, full_project, tmp_path, monkeypatch):
    c = full
    snapshot = c.sn.capture(c.owner, full_project[0])
    for key in MANAGED_KEYS:
        monkeypatch.setenv(key, 'STALE-' + key)
    script = tmp_path / 'command.py'
    script.write_text("""
import json, os, sys
assert sys.stdin.buffer.read() == b''
print(json.dumps({key:os.environ.get(key) for key in %r}))
""" % (MANAGED_KEYS,))
    receipt, _, _ = c.rt.observe(
        full_project[0], None, 'command-probe', 'test:command', None,
        digest({'command': True}), snapshot,
        lambda work, home, cwd: ([sys.executable, str(script)], None),
        prompt=b'', timeout=10,
        extra_env={key: 'STALE-extra-' + key for key in MANAGED_KEYS},
    )

    output = json.loads(c.s.blob_get(receipt['stdout_blob']))
    assert output['DAIKIBO_MANAGED_RUN'] == '1'
    assert output['DAIKIBO_RUN_ID'] == receipt['run']
    assert output['DAIKIBO_RUN_ROLE'] == 'test:command'
    assert output['DAIKIBO_TASK_ID'] is None
    assert output['DAIKIBO_JOB_ID'] is None
    context = receipt['environment']['managed_context']
    assert context == {'DAIKIBO_MANAGED_RUN':'1','DAIKIBO_RUN_ID':receipt['run'],'DAIKIBO_RUN_ROLE':'test:command'}
    run = parse_json(c.s.one('SELECT body FROM runs WHERE id=?', (receipt['run'],), True)['body'])
    assert run['environment']['managed_context'] == context
    assert receipt['result'] == {'exit_code': 0}


def test_managed_review_and_supervisor_roles_return_existing_protocols(full, full_project, tmp_path):
    c = full
    review = _register_script(c, tmp_path, 'managed-reviewer', """
import json, os, sys
p=json.load(sys.stdin); managed=p['context']['managed_execution']
assert managed['role']=='requirements' and managed['job_id']==os.environ['DAIKIBO_JOB_ID']
print(json.dumps({'verdict':'pass','rationale':'fixture role contract', 'covered':[], 'findings':[],
                  'observations':[{'ref':p['subject'],'detail':'managed reviewer returned'}], 'dispositions':[]}))
""")
    review_job = c.jobs.submit(c.owner, 'review',
                               {'subject': full_project[2], 'role': 'requirements', 'adapter': review})
    review_outcome = c.jobs.run_one(_job_row(c, review_job))
    assert review_outcome['status'] == 'succeeded'
    review_result = review_outcome['result']
    review_receipt = _receipt(c, review_result['receipt'])
    assert review_receipt['role'] == 'requirements'
    assert review_receipt['environment']['managed_context']['DAIKIBO_JOB_ID'] == review_job['id']
    assert 'DAIKIBO_TASK_ID' not in review_receipt['environment']['managed_context']

    supervisor = _register_script(c, tmp_path, 'managed-supervisor', """
import json, os, sys
p=json.load(sys.stdin); managed=p['managed_execution']
assert managed['role']=='supervisor' and managed['job_id']==os.environ['DAIKIBO_JOB_ID']
print(json.dumps({'message':'supervisor returned','actions':[],'questions':[]}))
""")
    supervisor_job = c.jobs.submit(c.owner, 'supervisor.turn',
                                   {'project': full_project[0], 'adapter': supervisor, 'message':'fixture'})
    supervisor_outcome = c.jobs.run_one(_job_row(c, supervisor_job))
    assert supervisor_outcome['status'] == 'succeeded'
    assert supervisor_outcome['result']['message'] == 'supervisor returned'
    supervisor_receipt = _receipt(c, supervisor_outcome['result']['receipt'])
    assert supervisor_receipt['role'] == 'supervisor'
    assert supervisor_receipt['environment']['managed_context']['DAIKIBO_JOB_ID'] == supervisor_job['id']
    assert supervisor_receipt['environment']['managed_context']['DAIKIBO_RUN_ROLE'] == 'supervisor'


def test_cli_self_wait_fails_before_poll_and_other_job_wait_is_unchanged(monkeypatch, tmp_path):
    from daikibo import cli

    monkeypatch.setenv('DAIKIBO_MANAGED_RUN', '1')
    monkeypatch.setenv('DAIKIBO_JOB_ID', 'JOB-self')
    monkeypatch.setenv('DAIKIBO_RUN_ID', 'RUN-self')
    monkeypatch.setenv('DAIKIBO_TASK_ID', 'TASK-self')
    monkeypatch.setenv('DAIKIBO_RUN_ROLE', 'implementer')

    class NoPoll:
        calls = 0

        def call(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('self wait entered job.get')

    client = NoPoll()
    with pytest.raises(Fault) as exc:
        cli.wait(client, 'JOB-self')
    assert exc.value.code == 'self_wait'
    assert client.calls == 0

    states = iter([{'status':'running'}, {'status':'succeeded', 'result':{'ok':True}}])
    pauses = []
    monkeypatch.setattr(cli.time, 'sleep', pauses.append)

    class OtherJob:
        def call(self, *args, **kwargs):
            return next(states)

    assert cli.wait(OtherJob(), 'JOB-other')['status'] == 'succeeded'
    assert pauses == [.5]

    monkeypatch.delenv('DAIKIBO_MANAGED_RUN')
    monkeypatch.delenv('DAIKIBO_JOB_ID')
    ordinary_states = iter([{'status':'queued'}, {'status':'running'}, {'status':'retry_wait'},
                            {'status':'succeeded', 'result':{'ok':True}}])
    ordinary_pauses = []
    monkeypatch.setattr(cli.time, 'sleep', ordinary_pauses.append)

    class OrdinaryJob:
        def call(self, *args, **kwargs):
            return next(ordinary_states)

    assert cli.wait(OrdinaryJob(), 'JOB-ordinary')['status'] == 'succeeded'
    assert ordinary_pauses == [.5, .5, .5]

    env = dict(os.environ)
    if env.get('DAIKIBO_TEST_INSTALLED') != '1':
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    env['DAIKIBO_MANAGED_RUN'] = '1'
    env['DAIKIBO_JOB_ID'] = 'JOB-cli-self'
    env['DAIKIBO_RUN_ID'] = 'RUN-cli-self'
    env['DAIKIBO_TASK_ID'] = 'TASK-cli-self'
    env['DAIKIBO_RUN_ROLE'] = 'implementer'
    command = [sys.executable, '-m', 'daikibo', '--home', str(tmp_path / 'home'),
               '--socket', str(tmp_path / 'missing.sock'), 'wait', 'JOB-cli-self']
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=3)
    assert completed.returncode == 2
    assert json.loads(completed.stdout)['error']['code'] == 'self_wait'


def test_bundled_managed_skill_branches_before_connection():
    source = Path('src/daikibo/assets/skill/SKILL.md').read_text()
    operations = Path('src/daikibo/assets/skill/references/operations.md').read_text()
    assert operations
    assert source.index('## Managed worker role') < source.index('## 接続')
    assert 'DAIKIBO_RUN_ROLE' in source
    assert 'candidate' in source and '自己jobのwait' in source
