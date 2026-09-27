"""Fixed candidate verification keeps saved inputs immutable across check kinds."""

import json
import sys

from conftest import make_task
from daikibo.gitops import git
from daikibo.runtime import FIXED_TEST_CONTRACT, SNAPSHOT_IDENTITY_CONTEXT


def _junit_report(name):
    return f"Path('results.xml').write_text('<testsuite><testcase classname=\"fixture\" name=\"{name}\"/></testsuite>')"


def test_junit_report_exception_is_narrow_and_input_mutation_still_fails(full, full_project):
    task = make_task(full, full_project)
    fresh_scratch = (
        "from pathlib import Path\n"
        "import tempfile\n"
        "with tempfile.TemporaryDirectory() as directory:\n"
        "    result = Path(directory) / 'fresh.result'\n"
        "    result.write_text(str(2 + 3))\n"
        "    assert result.read_text() == '5'\n"
        + _junit_report('fresh_scratch')
    )
    junit_mutation = (
        "from pathlib import Path\n"
        "Path('calc.py').write_text('mutated by junit\\n')\n"
        + _junit_report('junit_mutation')
    )
    command_mutation = "from pathlib import Path; Path('calc.py').write_text('mutated by command\\n')"
    full.w.plan_tests(full.owner, task, {
        'checks': [
            {'id': 'fresh-scratch', 'argv': [sys.executable, '-c', fresh_scratch],
             'kind': 'junit', 'report': 'results.xml', 'required_tests': ['fresh_scratch']},
            {'id': 'junit-mutation', 'argv': [sys.executable, '-c', junit_mutation],
             'kind': 'junit', 'report': 'results.xml', 'required_tests': ['junit_mutation']},
            {'id': 'command-mutation', 'argv': [sys.executable, '-c', command_mutation],
             'kind': 'command', 'purpose': 'Reject a command that changes sealed candidate input.'},
        ],
        'rationale': 'Record implementation artifacts once, then verify from a sealed candidate.',
    })

    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, 'fixture')
    sealed = full.s.one('SELECT body,digest FROM candidates WHERE task=?', (task,), True)
    report = full.rt.tests(full.owner, task)
    results = {item['check']: item['result'] for item in report['checks']}
    receipts = {item['check']: full.g.receipt(item['receipt']) for item in report['checks']}
    retained = full.s.one('SELECT body,digest FROM candidates WHERE task=?', (task,), True)

    assert retained == sealed
    assert results['fresh-scratch']['passed'] is True
    assert receipts['fresh-scratch']['input_mutated'] is False
    assert results['junit-mutation']['passed'] is False
    assert receipts['junit-mutation']['input_mutated'] is True
    assert results['junit-mutation']['count'] == 1
    assert results['junit-mutation']['failed'] == 0
    assert results['command-mutation']['passed'] is False
    assert receipts['command-mutation']['input_mutated'] is True


def test_runtime_separates_snapshot_content_from_recorded_git_provenance(full, full_project):
    project, repository, _, source_root = full_project
    git(source_root, 'init')
    git(source_root, 'add', 'calc.py', 'test_calc.py')
    git(source_root, 'commit', '-m', 'sealed source fixture')
    recorded_head = git(source_root, 'rev-parse', 'HEAD').stdout.decode().strip()
    snapshot = full.sn.capture(full.owner, project, [repository])
    source_entry = snapshot['repos'][repository]['files']['calc.py']
    assert snapshot['repos'][repository]['head'] == recorded_head

    probe = (
        "import json, subprocess\n"
        "from pathlib import Path\n"
        "result = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)\n"
        "print(json.dumps({'git_exists': (Path('.git').exists()),\n"
        "                  'git_head': result.stdout.strip() if result.returncode == 0 else None,\n"
        "                  'git_returncode': result.returncode}))\n"
    )
    command_check = {'kind': 'command', 'purpose': 'Compare materialized content with recorded Git provenance.'}
    observed, after, changes = full.rt.observe(
        project, None, 'snapshot-identity-probe', 'test:materialize', None, 'snapshot-binding',
        snapshot, lambda work, home, cwd: ([sys.executable, '-c', probe], None),
        check=command_check,
    )
    probe_result = json.loads(full.s.blob_get(observed['stdout_blob']).decode())

    assert SNAPSHOT_IDENTITY_CONTEXT in FIXED_TEST_CONTRACT
    assert observed['result']['passed'] is True
    assert observed['input_mutated'] is False
    assert probe_result['git_exists'] is False
    assert probe_result['git_head'] != recorded_head
    assert not changes
    assert after['repos'][repository]['head'] == recorded_head
    assert after['repos'][repository]['files']['calc.py'] == source_entry

    mutation = "from pathlib import Path; Path('calc.py').write_text('def add(a, b):\\n    return a * b\\n')"
    changed, changed_after, changed_paths = full.rt.observe(
        project, None, 'snapshot-identity-mutation', 'test:materialize', None, 'snapshot-binding',
        snapshot, lambda work, home, cwd: ([sys.executable, '-c', mutation], None),
        check=command_check,
    )

    assert changed['result']['passed'] is False
    assert changed['input_mutated'] is True
    assert any(item['path'] == 'calc.py' for item in changed_paths)
    assert changed_after['repos'][repository]['files']['calc.py'] != source_entry
    assert snapshot['repos'][repository]['files']['calc.py'] == source_entry
