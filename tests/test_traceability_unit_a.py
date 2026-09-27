"""Real I/O checks for the dev28 Unit A traceability foundation."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import subprocess
import time
from pathlib import Path

import pytest
from _child_import import child_env, child_import_guard

from daikibo.common import Fault
from daikibo.control import Control
from daikibo.operations import restore_backup
from daikibo.traceability import inspect_archive, partition_document, partition_python


def _child_env(**updates: str) -> dict[str, str]:
    return child_env(**updates)


def _commit(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "trace-test", "GIT_AUTHOR_EMAIL": "trace@test", "GIT_COMMITTER_NAME": "trace-test", "GIT_COMMITTER_EMAIL": "trace@test"}
    subprocess.run(["git", "-c", "user.name=trace-test", "-c", "user.email=trace@test", "commit", "-qm", "fixture"], cwd=root, check=True, env=env)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def test_python_partition_reassembles_bytes_and_separates_same_name_nested_defs():
    raw = b"# \xf0\x9f\x98\x80\n@overload\ndef f(x: int):\n    return x\n@overload\ndef f(x: str):\n    return x\nclass C:\n    def f(self):\n        return 1\n"
    result = partition_python(raw)
    atoms = [item for item in result["items"] if item["item_kind"] == "atom"]
    assert b"".join(raw[item["start_byte"]:item["end_byte"]] for item in sorted(atoms, key=lambda item: item["start_byte"])) == raw
    assert len({item["id"] for item in atoms}) == len(atoms)
    symbols = [item["body"] for item in result["items"] if item["item_kind"] == "symbol"]
    assert [item["qualified_name"] for item in symbols].count("f") == 2
    assert any(item["qualified_name"] == "C.f" for item in symbols)
    decorated = partition_python(b"@deco\ndef decorated():\n    return 1\n")
    decorated_symbol = next(item for item in decorated["items"] if item["item_kind"] == "symbol")
    assert decorated_symbol["start_byte"] == 0
    assert decorated["items"][1]["start_byte"] == 0


def test_python_partition_large_flat_definitions_reassembles_without_quadratic_scan():
    raw = "\n".join(f"def flat_{index}(x):\n    return x + {index}\n" for index in range(2000)).encode()
    result = partition_python(raw, "flat.py")
    atoms = sorted((item for item in result["items"] if item["item_kind"] == "atom"), key=lambda item: item["start_byte"])
    assert len(atoms) == 4000
    assert sum(item["end_byte"] - item["start_byte"] for item in atoms) == len(raw)
    assert all(left["end_byte"] == right["start_byte"] for left, right in zip(atoms, atoms[1:]))


def test_document_partition_preserves_bom_crlf_and_unicode_coordinates():
    raw = b"\xef\xbb\xbfA\xf0\x9f\x98\x80\r\n\r\nlast"
    result = partition_document(raw)
    lines = [item for item in result["items"] if item["item_kind"] == "line"]
    assert b"".join(raw[item["start_byte"]:item["end_byte"]] for item in lines) == raw
    # Knowledge.source/source.read count the leading U+FEFF as a character;
    # byte spans remain the lossless source of truth.
    assert [item["body"]["unicode_start"] for item in lines] == [0, 5, 7]
    assert [item["body"]["unicode_end"] for item in lines] == [5, 7, 11]


def test_document_partition_does_not_invent_a_line_after_terminal_newline():
    assert len([item for item in partition_document(b"one\n")["items"] if item["item_kind"] == "line"]) == 1
    empty = [item for item in partition_document(b"\n\n")["items"] if item["item_kind"] == "line"]
    assert len(empty) == 2
    assert b"".join(b"\n\n"[item["start_byte"]:item["end_byte"]] for item in empty) == b"\n\n"
    invalid = next(item for item in partition_document(b"\xff\n")["items"] if item["item_kind"] == "line")
    assert invalid["status"] == "unknown"
    assert invalid["body"]["unicode_start"] is None and invalid["body"]["unicode_end"] is None


def test_registered_source_coordinates_match_knowledge_source_read(full):
    project = full.k.create_project(full.owner, "registered unicode source")["id"]
    content = "\ufeffA😀\r\n\r\nlast"
    source = full.k.source(full.owner, project, content, "unicode-fixture")["id"]
    proposal = full.traceability.propose(full.owner, project, kind="document", scope={"source": source})
    revision = full.traceability.extract(full.owner, proposal["id"])["revision"]
    lines = [item for item in full.traceability.items(full.owner, revision, limit=100)["items"] if item["item_kind"] == "line"]
    source_read = full.k.source_read(full.owner, source, start=0, limit=100)
    assert source_read["content"] == content
    assert [item["body"]["unicode_start"] for item in lines] == [0, 5, 7]
    for item in lines:
        start, end = item["body"]["unicode_start"], item["body"]["unicode_end"]
        assert content[start:end] == source_read["content"][start:end]


def test_git_population_is_pinned_and_pages_are_stale_safe(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "code.py").write_text("def add(x):\n    return x + 1\n")
    (repo / "ignored.txt").write_text("unknown files remain visible\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "trace fixture")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code", scope={"repository": registered, "commit": commit, "roots": ["pkg"], "include": ["*.py"]})
    extracted = full.traceability.extract(full.owner, proposal["id"])
    revision = extracted["revision"]
    first = full.traceability.items(full.owner, revision, limit=1)
    assert first["next_cursor"] and first["total"] >= 3
    # A working-tree edit cannot replace the selected commit's CAS bytes.
    (repo / "pkg" / "code.py").write_text("def add(x):\n    return x + 999\n")
    read = full.traceability.read(full.owner, revision, path="pkg/code.py")
    assert "x + 1" in read["content"]
    full.s.execute("UPDATE traceability_revisions SET status='superseded' WHERE id=?", (revision,))
    with pytest.raises(Fault) as error:
        full.traceability.items(full.owner, revision, limit=1, cursor=first["next_cursor"])
    assert error.value.code == "stale_cursor"


def test_failed_or_unknown_inputs_are_retained_and_empty_scope_is_explicit(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bad.py").write_bytes(b"\xff\xfe")
    (repo / "empty.py").write_bytes(b"")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "unknown fixture")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code", scope={"repository": registered, "commit": commit})
    extracted = full.traceability.extract(full.owner, proposal["id"])
    assert extracted["counts"]["unknown"] == 1
    items = full.traceability.items(full.owner, extracted["revision"], limit=100)["items"]
    assert {item["body"].get("unknown_reason") for item in items if item["status"] == "unknown"} >= {"invalid_utf8"}
    assert any(item["body"].get("empty") and item["status"] == "known" for item in items)
    empty = full.traceability.propose(full.owner, project, kind="code", name="empty", scope={"repository": registered, "commit": commit, "roots": []})
    result = full.traceability.extract(full.owner, empty["id"])
    assert result["counts"]["empty_scope"] is True and result["counts"]["files"] == 0
    assert full.traceability.coverage(full.owner, project, result["revision"])["revisions"][0]["empty_scope_requires_adoption"]


def test_traceability_archive_restores_after_original_home_and_git_are_gone(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(); (repo / "doc.py").write_text("def hi():\n    return 'ok'\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "archive fixture")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code", scope={"repository": registered, "commit": commit})
    revision = full.traceability.extract(full.owner, proposal["id"])["revision"]
    archive = full.traceability.export(full.owner, project)
    saved = tmp_path / "traceability.zip"; shutil.copyfile(archive["path"], saved)
    assert inspect_archive(saved, archive["sha256"])["history_version"] == 10
    old_home = full.s.home
    full.close(); shutil.rmtree(old_home); shutil.rmtree(repo)
    restored = Control(tmp_path / "restored", mode="validation", start_workers=False)
    try:
        restored.owner = restored.sec.authenticate(None)
        imported = restored.traceability.import_archive(restored.owner, saved, archive["sha256"])
        assert imported["historical_only"] and not imported["fresh_review_or_test_evidence"]
        assert restored.traceability.read(restored.owner, revision, path="doc.py")["content"].startswith("def hi")
    finally:
        restored.close()


def test_public_extract_is_durable_job_and_abrupt_worker_resume(full, tmp_path):
    repo = tmp_path / "resume-repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    (repo / "b.py").write_text("def b():\n    return 2\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "resume fixture")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code", scope={"repository": registered, "commit": commit})
    queued = full.invoke(full.owner, "traceability.extract", {"proposal": proposal["id"]})
    assert queued["status"] == "queued"
    job = full.s.one("SELECT * FROM jobs WHERE id=?", (queued["id"],), True)
    full.jobs.run_one(job)
    assert full.jobs.get(full.owner, queued["id"])["status"] == "succeeded"

    proposal = full.traceability.propose(full.owner, project, name="abrupt", kind="code",
                                         scope={"repository": registered, "commit": commit})
    home = str(full.s.home)
    full.close()
    script = child_import_guard() + r'''
import os
from daikibo.control import Control
c = Control(os.environ["TRACE_HOME"], mode="validation", start_workers=False)
owner = c.sec.authenticate(None)
original = c.traceability._stage_checkpoint
seen = {"entry": False}
def crash(proposal, revision_id, revision_no, body, pins):
    original(proposal, revision_id, revision_no, body, pins)
    if body.get("stage") == "entry" and not seen["entry"]:
        seen["entry"] = True
        os._exit(73)
c.traceability._stage_checkpoint = crash
c.traceability.extract(owner, os.environ["TRACE_PROPOSAL"])
'''
    env = _child_env(TRACE_HOME=home, TRACE_PROPOSAL=proposal["id"])
    child = subprocess.run([sys.executable, "-c", script], env=env)
    assert child.returncode == 73
    resumed = Control(home, mode="validation", start_workers=False)
    try:
        resumed.owner = resumed.sec.authenticate(None)
        state = resumed.s.one("SELECT status,result FROM traceability_proposals WHERE id=?", (proposal["id"],), True)
        assert state["status"] == "staging"
        result = resumed.traceability.extract(resumed.owner, proposal["id"])
        assert result["status"] == "ready"
        assert resumed.s.one("SELECT count(*) AS n FROM traceability_records WHERE proposal=? AND kind='extraction_checkpoint'", (proposal["id"],))["n"] >= 2
    finally:
        resumed.close()


def test_schema13_migration_creates_traceability_tables_without_fabricating_rows(full, tmp_path):
    home = full.s.home
    full.close()
    db = sqlite3.connect(home / "state.sqlite3")
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TABLE program_origins")
    for table in ("traceability_records", "traceability_bindings", "traceability_mappings", "traceability_decisions", "traceability_proposals", "traceability_items", "traceability_revisions", "traceability_sets"):
        db.execute("DROP TABLE " + table)
    db.execute("PRAGMA user_version=13"); db.commit(); db.close()
    reopened = Control(home, mode="validation", start_workers=False)
    try:
        assert reopened.s.one("PRAGMA user_version")["user_version"] == 16
        assert reopened.s.one("SELECT count(*) AS n FROM traceability_revisions")["n"] == 0
    finally:
        reopened.close()


def test_gc_and_operational_backup_retain_historical_trace_pins(full, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(); (repo / "code.py").write_text("def keep():\n    return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "retention fixture")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code", scope={"repository": registered, "commit": commit})
    extracted = full.traceability.extract(full.owner, proposal["id"])
    live = set(extracted["pins"])
    orphan = full.s.blob_put(b"unrelated old blob")
    orphan_path = full.s.blob_path(orphan); old = time.time() - 90000; os.utime(orphan_path, (old, old))
    collected = full.ops.garbage_collect(full.owner, dry_run=False)
    assert orphan in {item["blob"] for item in collected["candidates"]}
    assert all(full.s.blob_path(value).is_file() for value in live)
    backup = full.ops.backup(full.owner)
    destination = tmp_path / "restored-control"
    full.close(); restore_backup(backup["path"], destination, backup["sha256"])
    restored = Control(destination, mode="validation", start_workers=False)
    try:
        restored.owner = restored.sec.authenticate(None)
        assert restored.traceability.get(restored.owner, extracted["revision"])["status"] == "ready"
        assert all(restored.s.blob_path(value).is_file() for value in live)
    finally:
        restored.close()
