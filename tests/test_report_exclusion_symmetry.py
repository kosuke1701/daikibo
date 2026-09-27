"""Declared JUnit report exclusions are exact and symmetric in Runtime.observe."""

import sys
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest, parse_json, timestamp, uid


REPORT = '<testsuite tests="1"><testcase classname="fixture" name="case"/></testsuite>'


def _run_junit(full, full_project, script, *, existing=False, original_report=None):
    project, repository, _requirement, source_root = full_project
    if existing:
        (source_root / 'verification.xml').write_text(REPORT)
    if original_report is not None:
        (source_root / original_report).write_text(REPORT)
    snapshot = full.sn.capture(full.owner, project, [repository])
    check = {'kind': 'junit', 'report': 'verification.xml', 'required_tests': ['case']}
    if original_report is not None:
        check['original_report'] = original_report
    return full.rt.observe(
        project, None, 'report-exclusion', 'test:report', None, 'report-binding', snapshot,
        lambda work, home, cwd: ([sys.executable, '-c', script], None),
        check=check,
    )


@pytest.mark.parametrize(
    ('name', 'script', 'existing', 'expected_pass', 'expected_mutated'),
    [
        ('new-report', f"from pathlib import Path; Path('verification.xml').write_text({REPORT!r})", False, True, False),
        ('existing-same', 'pass', True, True, False),
        ('existing-rewrite', f"from pathlib import Path; Path('verification.xml').write_text({REPORT.replace('fixture', 'rewritten')!r})", True, True, False),
        ('existing-delete', "from pathlib import Path; Path('verification.xml').unlink()", True, False, False),
        ('adjacent-report', "from pathlib import Path; Path('verification.xml.bak').write_text('adjacent')", True, False, True),
        ('source-change', "from pathlib import Path; Path('calc.py').write_text('changed source')", True, False, True),
        ('source-delete', "from pathlib import Path; Path('calc.py').unlink()", True, False, True),
    ],
)
def test_declared_junit_report_is_excluded_symmetrically_but_other_changes_remain_visible(
    full, full_project, name, script, existing, expected_pass, expected_mutated,
):
    observed, _after, changes = _run_junit(full, full_project, script, existing=existing)

    assert observed['result']['passed'] is expected_pass, name
    assert observed['input_mutated'] is expected_mutated, name
    if expected_mutated:
        assert changes
        assert all(change['path'] != 'verification.xml' for change in changes)
    else:
        assert changes == []


def test_original_report_alias_is_an_exact_second_exclusion(full, full_project):
    script = "from pathlib import Path; Path('legacy.xml').write_text('<testsuite tests=\\\"1\\\"><testcase classname=\\\"fixture\\\" name=\\\"legacy\\\"/></testsuite>')"
    observed, _after, changes = _run_junit(
        full, full_project, script, existing=True, original_report='legacy.xml',
    )

    assert observed['result']['passed'] is True
    assert observed['input_mutated'] is False
    assert changes == []


def test_command_checks_have_no_report_exclusion(full, full_project):
    project, repository, _requirement, source_root = full_project
    (source_root / 'verification.xml').write_text(REPORT)
    snapshot = full.sn.capture(full.owner, project, [repository])
    observed, _after, changes = full.rt.observe(
        project, None, 'command-without-report', 'test:command', None, 'command-binding', snapshot,
        lambda work, home, cwd: ([sys.executable, '-c', "from pathlib import Path; Path('verification.xml').unlink()"], None),
        check={'kind': 'command', 'purpose': 'Declared report exclusions do not apply to command checks.'},
    )

    assert observed['result']['passed'] is False
    assert observed['input_mutated'] is True
    assert [change['path'] for change in changes] == ['verification.xml']


def test_symlink_report_is_rejected_without_becoming_a_source_mutation(full, full_project):
    project, repository, _requirement, source_root = full_project
    (source_root / 'report-payload.xml').write_text(REPORT)
    (source_root / 'verification.xml').symlink_to('report-payload.xml')
    snapshot = full.sn.capture(full.owner, project, [repository])

    observed, _after, changes = full.rt.observe(
        project, None, 'symlink-report', 'test:report', None, 'report-binding', snapshot,
        lambda work, home, cwd: ([sys.executable, '-c', 'pass'], None),
        check={'kind': 'junit', 'report': 'verification.xml', 'required_tests': ['case']},
    )

    assert observed['result']['passed'] is False
    assert observed['result']['error']['code'] == 'missing_test_report'
    assert observed['input_mutated'] is False
    assert changes == []


def test_multi_repository_report_path_is_bound_to_named_repository(full, tmp_path):
    project = full.k.create_project(full.owner, 'Multi-repository report scope')['id']
    first = tmp_path / 'first'; second = tmp_path / 'second'
    first.mkdir(); second.mkdir()
    (first / 'source.py').write_text('first')
    (second / 'source.py').write_text('second')
    first_id = full.sn.register(full.owner, project, 'first', str(first))['id']
    second_id = full.sn.register(full.owner, project, 'second', str(second))['id']
    snapshot = full.sn.capture(full.owner, project, [first_id, second_id])

    mapped = full.sn._ignored_by_repo(snapshot, ['first/verification.xml'])
    assert mapped[first_id] == ['verification.xml']
    assert mapped[second_id] == []
    with pytest.raises(Fault):
        full.sn._ignored_by_repo(snapshot, ['../verification.xml'])

    script = f"from pathlib import Path; Path('first/verification.xml').write_text({REPORT!r})"
    observed, _after, changes = full.rt.observe(
        project, None, 'multi-repository-report', 'test:report', None, 'multi-binding', snapshot,
        lambda work, home, cwd: ([sys.executable, '-c', script], None),
        check={'kind': 'junit', 'report': 'first/verification.xml', 'required_tests': ['case']},
    )

    assert observed['result']['passed'] is True
    assert observed['input_mutated'] is False
    assert changes == []


def _delivery_with_nested_report(full, tmp_path, sibling_change):
    project = full.k.create_project(full.owner, 'Delivery report scope')['id']
    first = tmp_path / 'first'; second = tmp_path / 'second'
    first.mkdir(); second.mkdir(); (first / 'second').mkdir()
    (first / 'second' / 'report.xml').write_text(REPORT)
    (second / 'report.xml').write_text(REPORT)
    first_id = full.sn.register(full.owner, project, 'first', str(first))['id']
    second_id = full.sn.register(full.owner, project, 'second', str(second))['id']
    snapshot = full.sn.capture(full.owner, project, [first_id, second_id])
    policy = full.g.policy(project)
    profile_body = {'baseline_snapshot': snapshot}
    scope = {'requirements': [], 'tasks': [], 'policy': policy['digest']}
    profile_digest = digest(profile_body)
    full.s.execute(
        'INSERT INTO profiles VALUES(?,?,?,?,?)',
        (project, canonical(profile_body).decode(), profile_digest, canonical(scope).decode(), timestamp()),
    )
    check = {
        'id': 'nested-report', 'category': 'integration', 'repo': first_id, 'kind': 'junit',
        'argv': [sys.executable, '-c', 'from pathlib import Path; '
                 f'Path("second/report.xml").write_text({REPORT!r}); '
                 + ("Path(\"../second/report.xml\").write_text(\"changed sibling source\")" if sibling_change else 'pass')],
        'report': 'second/report.xml', 'required_tests': ['case'],
    }
    binding = {
        'program': None, 'breakdown': None, 'profile': profile_digest, 'scope': scope,
        'snapshot': snapshot['digest'], 'tasks': [], 'requirements': [], 'policy': policy['digest'],
    }
    record = {
        'binding': binding, 'snapshot': snapshot, 'build_definitions': [], 'build_outputs': {},
        'checks': [check], 'target_environment': 'fixture', 'applicability': {},
        'rollback': 'fixture', 'results': [], 'git': {}, 'limitations': [],
    }
    delivery = uid('DELIVERY')
    full.s.execute(
        'INSERT INTO deliveries VALUES(?,?,?,?,?,?)',
        (delivery, project, canonical(record).decode(), digest(binding), 'prepared', timestamp()),
    )
    return full.d.verify(full.owner, delivery), delivery


@pytest.mark.parametrize('sibling_change', [False, True])
def test_delivery_multirepo_report_alias_stays_bound_to_target_repository(full, tmp_path, sibling_change):
    result, _delivery = _delivery_with_nested_report(full, tmp_path, sibling_change)
    assert result['results'][0]['passed'] is (not sibling_change)
    receipt_id = result['results'][0]['receipt']
    observed = parse_json(full.s.one('SELECT body FROM receipts WHERE id=?', (receipt_id,), True)['body'])
    assert observed['input_mutated'] is sibling_change
