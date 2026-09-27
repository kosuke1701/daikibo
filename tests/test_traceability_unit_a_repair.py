"""Bounded real-I/O acceptance for the Unit A repair contract."""
from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from _child_import import child_env, child_import_guard

from daikibo.common import Fault, timestamp
from daikibo.control import Control
from daikibo.knowledge_history import inspect_archive


def _child_env(**updates: str) -> dict[str, str]:
    return child_env(**updates)


def _commit(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "trace-repair", "GIT_AUTHOR_EMAIL": "trace@invalid",
           "GIT_COMMITTER_NAME": "trace-repair", "GIT_COMMITTER_EMAIL": "trace@invalid"}
    subprocess.run(["git", "-c", "user.name=trace-repair", "-c", "user.email=trace@invalid",
                    "commit", "-qm", "fixture"], cwd=root, check=True, env=env)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _crash_worker(home: Path, proposal: str, predicate: str, exit_code: int) -> int:
    script = child_import_guard() + f"""
import os
from daikibo.control import Control
c = Control(os.environ['TRACE_HOME'], mode='validation', start_workers=False)
owner = c.sec.authenticate(None)
original = c.traceability._stage_checkpoint
def checkpoint(*args):
    original(*args)
    body = args[3]
    if {predicate}:
        os._exit({exit_code})
c.traceability._stage_checkpoint = checkpoint
c.traceability.extract(owner, os.environ['TRACE_PROPOSAL'])
"""
    env = _child_env(TRACE_HOME=str(home), TRACE_PROPOSAL=proposal)
    return subprocess.run([sys.executable, "-c", script], env=env).returncode


def test_complete_git_pin_resumes_after_real_exit_and_git_removal(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "pin repair")['id']
    registered = full.sn.register(full.owner, project, "fixture", str(repo))['id']
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    home = full.s.home
    full.close()
    assert _crash_worker(home, proposal['id'], "body.get('stage') == 'entry' and body.get('complete')", 73) == 73
    shutil.rmtree(repo)
    resumed = Control(home, mode='validation', start_workers=False)
    try:
        owner = resumed.sec.authenticate(None)
        result = resumed.traceability.extract(owner, proposal['id'])
        assert result['status'] == 'ready'
        assert resumed.s.one("SELECT count(*) AS n FROM traceability_revisions WHERE id=?", (result['revision'],))['n'] == 1
        assert resumed.traceability.read(owner, result['revision'], path='a.py')['content'].startswith('def a')
    finally:
        resumed.close()


def test_incomplete_git_pin_is_missing_input_and_never_ready(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    (repo / "b.py").write_text("def b():\n    return 2\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "pin incomplete")['id']
    registered = full.sn.register(full.owner, project, "fixture", str(repo))['id']
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    home = full.s.home
    full.close()
    assert _crash_worker(home, proposal['id'], "body.get('stage') == 'git_blob'", 74) == 74
    shutil.rmtree(repo)
    resumed = Control(home, mode='validation', start_workers=False)
    try:
        owner = resumed.sec.authenticate(None)
        with pytest.raises(Fault) as error:
            resumed.traceability.extract(owner, proposal['id'])
        assert error.value.code == 'missing_input'
        assert resumed.s.one("SELECT status FROM traceability_proposals WHERE id=?", (proposal['id'],))['status'] == 'failed'
        assert resumed.s.one("SELECT count(*) AS n FROM traceability_revisions WHERE project=?", (project,))['n'] == 0
    finally:
        resumed.close()


def test_ready_public_extract_job_reconciles_after_terminal_publication(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "job repair")['id']
    registered = full.sn.register(full.owner, project, "fixture", str(repo))['id']
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    queued = full.invoke(full.owner, "traceability.extract", {"proposal": proposal['id']})
    published = full.traceability.extract(full.owner, proposal['id'])
    now = timestamp()
    full.s.execute("UPDATE jobs SET status='running',attempt_count=1,started=? WHERE id=?", (now, queued['id']))
    full.s.execute("INSERT INTO job_attempts(job,attempt,status,started) VALUES(?,1,'running',?)", (queued['id'], now))
    home = full.s.home
    full.close()
    resumed = Control(home, mode='validation', start_workers=False)
    try:
        owner = resumed.sec.authenticate(None)
        job = resumed.jobs.get(owner, queued['id'])
        assert job['status'] == 'succeeded'
        assert job['attempts'][0]['status'] == 'succeeded'
        assert job['result']['revision'] == published['revision']
        assert resumed.s.one("SELECT count(*) AS n FROM traceability_revisions WHERE id=?", (published['revision'],))['n'] == 1
    finally:
        resumed.close()


def test_public_extract_job_real_process_exit_after_ready_publication_reconciles(full, tmp_path):
    """A worker can disappear after the revision transaction and before Jobs.run_one commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "job process exit")['id']
    registered = full.sn.register(full.owner, project, "fixture", str(repo))['id']
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    queued = full.invoke(full.owner, "traceability.extract", {"proposal": proposal['id']})
    home = full.s.home
    full.close()
    script = child_import_guard() + """
import os
from daikibo.control import Control
c = Control(os.environ['TRACE_HOME'], mode='validation', start_workers=False)
owner = c.sec.authenticate(None)
original = c.traceability.extract
def publish_then_exit(actor, proposal, **options):
    result = original(actor, proposal, **options)
    assert result['status'] == 'ready'
    os._exit(76)
c.traceability.extract = publish_then_exit
row = c.s.one('SELECT * FROM jobs WHERE id=?', (os.environ['TRACE_JOB'],), True)
c.jobs.run_one(row)
"""
    env = _child_env(TRACE_HOME=str(home), TRACE_JOB=queued['id'])
    assert subprocess.run([sys.executable, "-c", script], env=env).returncode == 76
    shutil.rmtree(repo)
    resumed = Control(home, mode='validation', start_workers=False)
    try:
        owner = resumed.sec.authenticate(None)
        job = resumed.jobs.get(owner, queued['id'])
        assert job['status'] == 'succeeded'
        assert job['result']['status'] == 'ready'
        assert resumed.s.one("SELECT count(*) AS n FROM traceability_revisions WHERE project=?", (project,))['n'] == 1
        # Startup reconciliation is idempotent; a second control open cannot
        # create another revision or alter the already terminal job.
        resumed.close()
        reopened = Control(home, mode='validation', start_workers=False)
        try:
            owner = reopened.sec.authenticate(None)
            again = reopened.jobs.get(owner, queued['id'])
            assert again['status'] == 'succeeded'
            assert again['result']['revision'] == job['result']['revision']
            assert reopened.s.one("SELECT count(*) AS n FROM traceability_revisions WHERE project=?", (project,))['n'] == 1
        finally:
            reopened.close()
    finally:
        # The nested reopen owns the handle when the first instance is closed.
        if not resumed.closed:
            resumed.close()


def test_standard_chunked_archive_v10_retains_trace_rows_and_cas_closure(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "history repair")['id']
    registered = full.sn.register(full.owner, project, "fixture", str(repo))['id']
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    revision = full.traceability.extract(full.owner, proposal['id'])['revision']
    baseline = full.history.create(full.owner, project, layout='chunked')
    exported = full.history.export_archive(full.owner, baseline['id'])
    report = inspect_archive(exported['path'], exported['sha256'])
    assert exported['format'] == 'daikibo.knowledge-archive.v12'
    assert report['counts']['traceability_revisions'] == 1
    assert report['counts']['traceability_items'] > 0
    with zipfile.ZipFile(exported['path']) as archive:
        members = [name for name in archive.namelist() if name.startswith('objects/')]
        assert any(revision.encode() in archive.read(name) for name in members)
    # The standard archive is self-contained historical input.  Its validator
    # must still read the trace rows and CAS closure after the originating home
    # and Git repository have been removed.
    portable = tmp_path / "portable-standard.zip"
    shutil.copy2(exported['path'], portable)
    archive_hash = hashlib.sha256(portable.read_bytes()).hexdigest()
    full.close()
    shutil.rmtree(full.s.home)
    restored_report = inspect_archive(portable, archive_hash)
    assert restored_report['verified'] is True
    assert restored_report['counts']['traceability_revisions'] == 1


def test_standard_chunked_archive_retains_failed_staging_pin_history(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    (repo / "b.py").write_text("def b():\n    return 2\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "failed history")['id']
    registered = full.sn.register(full.owner, project, "fixture", str(repo))['id']
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    home = full.s.home
    full.close()
    assert _crash_worker(home, proposal['id'], "body.get('stage') == 'git_blob'", 75) == 75
    shutil.rmtree(repo)
    resumed = Control(home, mode='validation', start_workers=False)
    try:
        owner = resumed.sec.authenticate(None)
        with pytest.raises(Fault) as error:
            resumed.traceability.extract(owner, proposal['id'])
        assert error.value.code == 'missing_input'
        baseline = resumed.history.create(owner, project, layout='chunked')
        exported = resumed.history.export_archive(owner, baseline['id'])
        report = inspect_archive(exported['path'], exported['sha256'])
        assert exported['format'] == 'daikibo.knowledge-archive.v12'
        assert report['counts']['traceability_proposals'] == 1
        assert report['counts']['traceability_records'] >= 3
    finally:
        resumed.close()
