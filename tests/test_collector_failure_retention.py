"""Focused coverage for non-adoptable collector-failure retention."""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import sqlite3
import shutil
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from daikibo.common import Actor, Fault
from daikibo.control import Control
from daikibo.operations import restore_backup
from daikibo.knowledge_history import inspect_archive


def _observe(c, project, snapshot, command):
    def argv_factory(work, home, cwd):
        return [sys.executable, "-c", command], None
    return c.rt.observe(project, None, "retention-test", "implementer", None,
                        "retention-binding", snapshot, argv_factory, timeout=60)


def _force_collection_failure(monkeypatch, c, code="snapshot_too_large"):
    def fail(*args, **kwargs):
        raise Fault(code, "the ordinary snapshot collector intentionally failed for this test")
    monkeypatch.setattr(c.sn, "collect", fail)


def _entries(c, owner, run, offset=0, limit=100):
    return c.invoke(owner, "run.recovery", {"run": run, "offset": offset, "limit": limit})


def test_large_and_small_files_are_retained_without_snapshot_or_candidate(full, full_project):
    c = full
    project, rid, _, _ = full_project
    snapshot = c.sn.capture(c.owner, project)
    observed, after, _ = _observe(
        c, project, snapshot,
        "from pathlib import Path; Path('large.json').write_bytes(b'x'*(33*1024*1024)); Path('small.txt').write_text('retained')",
    )
    assert after is None and observed["failure"]["code"] == "collector_error"
    recovery = observed["recovery_artifacts"]
    assert recovery["status"] == "complete" and recovery["adoptable"] is False
    page = _entries(c, c.owner, observed["run"], limit=10)
    by_path = {entry["path"]: entry for entry in page["entries"]}
    assert page["total"] == 4
    assert by_path["large.json"]["classification"] == "oversized_unclassified"
    assert by_path["large.json"]["observed_bytes"] == 33 * 1024 * 1024
    assert by_path["small.txt"]["classification"] == "bounded_source_candidate"
    assert by_path["small.txt"]["retention_status"] == "stored"
    assert not (c.rt.workroot / observed["run"]).exists()
    assert c.s.one("SELECT id FROM candidates WHERE implementation_run=?", (observed["run"],)) is None
    with pytest.raises(Fault) as denied:
        c.invoke(Actor("other", "agent", project="other-project"), "run.recovery",
                 {"run": observed["run"]})
    assert denied.value.code == "forbidden"
    large = c.invoke(c.owner, "run.recovery_read", {
        "run": observed["run"], "repo": rid, "path": "large.json",
        "expected_digest": by_path["large.json"]["blob"], "offset": 0, "limit": 7,
    })
    assert base64.b64decode(large["base64"]) == b"x" * 7
    assert large["total_bytes"] == 33 * 1024 * 1024


def test_aggregate_failure_is_paged_and_keeps_exact_inventory(full, full_project, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    snapshot = c.sn.capture(c.owner, project)
    command = "from pathlib import Path; [Path(f'part-{i:03d}.txt').write_text(str(i)) for i in range(305)]"
    observed, after, _ = _observe(c, project, snapshot, command)
    assert after is None and observed["recovery_artifacts"]["complete"]
    first = _entries(c, c.owner, observed["run"], limit=17)
    second = _entries(c, c.owner, observed["run"], offset=first["next_offset"], limit=17,
                     )
    assert first["total"] == 307 and first["next_offset"] == 17
    assert second["entries"][0]["path"] == "part-016.txt"
    assert len(first["entries"]) == len(second["entries"]) == 17
    all_paths = []
    offset = 0
    while True:
        page = _entries(c, c.owner, observed["run"], offset=offset, limit=100)
        all_paths.extend(item["path"] for item in page["entries"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    part_paths = [path for path in all_paths if path.startswith("part-")]
    assert len(part_paths) == 305 and len(set(part_paths)) == 305


def test_symlink_and_fifo_are_metadata_only_and_never_followed(full, full_project):
    c = full
    project, _, _, _ = full_project
    snapshot = c.sn.capture(c.owner, project)
    command = (
        "from pathlib import Path; import os; "
        "Path('outside-target').write_text('outside'); "
        "Path('unsafe-link').symlink_to('/tmp/collector-retention-outside'); "
        "os.mkfifo('worker.pipe')"
    )
    observed, after, _ = _observe(c, project, snapshot, command)
    assert after is None
    page = _entries(c, c.owner, observed["run"], limit=20)
    values = {item["path"]: item for item in page["entries"]}
    assert values["unsafe-link"]["kind"] == "symlink"
    assert values["unsafe-link"]["retention_status"] == "metadata_only"
    assert values["worker.pipe"]["kind"] == "fifo"
    assert values["worker.pipe"]["retention_status"] == "metadata_only"
    assert values["worker.pipe"]["blob"] is None


def test_disk_full_keeps_pending_staging_and_reconcile_does_not_duplicate_receipt(full, full_project, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    def no_space(source, *args, **kwargs):
        raise OSError(errno.ENOSPC, "test retention disk full")
    monkeypatch.setattr(c.s, "blob_put_stream", no_space)
    snapshot = c.sn.capture(c.owner, project)
    observed, _, _ = _observe(c, project, snapshot, "from pathlib import Path; Path('pending.txt').write_text('pending')")
    assert observed["recovery_artifacts"]["status"] == "partial"
    markers = c.rt.retention.marker_rows()
    assert len(markers) == 1 and markers[0]["state"] == "pending_staging"
    staged = Path(markers[0]["staged_root"])
    assert staged.is_dir() and not (c.rt.workroot / observed["run"]).exists()
    receipts_before = c.s.one("SELECT count(*) AS n FROM receipts WHERE run=?", (observed["run"],))["n"]
    monkeypatch.setattr(c.s, "blob_put_stream", c.s.__class__.blob_put_stream.__get__(c.s, c.s.__class__))
    result = c.rt.reconcile_retention()
    assert result["scanned"] == 1
    assert c.s.one("SELECT count(*) AS n FROM receipts WHERE run=?", (observed["run"],))["n"] == receipts_before == 1
    summary = c.rt.retention.summary(observed["run"])
    assert summary["status"] == "complete"
    assert _entries(c, c.owner, observed["run"])["total"] == 3


def test_cleanup_crash_leaves_marker_and_is_reconciled_without_adoption(full, full_project, monkeypatch):
    c = full
    project, _, _, _ = full_project
    original = shutil.rmtree
    crashed = {"value": False}
    def fail_once(path, *args, **kwargs):
        if not crashed["value"] and str(path).startswith(str(c.rt.workroot)):
            crashed["value"] = True
            raise OSError("simulated cleanup crash")
        return original(path, *args, **kwargs)
    monkeypatch.setattr("daikibo.runtime.shutil.rmtree", fail_once)
    snapshot = c.sn.capture(c.owner, project)
    observed, _, _ = _observe(c, project, snapshot, "from pathlib import Path; Path('cleanup.txt').write_text('x')")
    assert crashed["value"]
    assert c.rt.retention.has_pending()
    assert c.rt.retention.marker_rows()[0]["state"] == "pending_staging"
    # Reconciliation only adds a retention event and keeps the original
    # receipt/run; it cannot turn the collector failure into a success.
    result = c.rt.reconcile_retention()
    assert result["scanned"] == 1
    assert c.s.one("SELECT count(*) AS n FROM receipts WHERE run=?", (observed["run"],))["n"] == 1
    assert c.s.one("SELECT id FROM candidates WHERE implementation_run=?", (observed["run"],)) is None


def test_receipt_commit_failure_retains_stopped_tree_for_reconciliation(full, full_project, monkeypatch):
    c = full
    project, _, _, _ = full_project
    original = c.s.execute
    def fail_receipt(sql, args=()):
        if sql.lstrip().upper().startswith("INSERT INTO RECEIPTS"):
            raise sqlite3.OperationalError("simulated receipt commit failure")
        return original(sql, args)
    monkeypatch.setattr(c.s, "execute", fail_receipt)
    snapshot = c.sn.capture(c.owner, project)
    with pytest.raises(sqlite3.OperationalError):
        _observe(c, project, snapshot, "from pathlib import Path; Path('uncommitted.txt').write_text('keep')")
    markers = c.rt.retention.marker_rows()
    assert len(markers) == 1 and markers[0]["state"] == "pending_staging"
    assert Path(markers[0]["staged_root"]).is_dir()
    assert c.s.one("SELECT count(*) AS n FROM receipts") ["n"] == 0


def test_backup_and_gc_retain_manifest_and_transitive_entry_blobs(full, full_project, tmp_path, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    snapshot = c.sn.capture(c.owner, project)
    observed, _, _ = _observe(c, project, snapshot, "from pathlib import Path; Path('backup.txt').write_text('backup evidence')")
    summary = observed["recovery_artifacts"]
    detail = _entries(c, c.owner, observed["run"])
    entry_blob = detail["entries"][0]["blob"]
    orphan = c.s.blob_put(b"orphan")
    orphan_path = c.s.blobs / orphan[:2] / orphan[2:]
    old = 1.0
    os.utime(orphan_path, (old, old))
    gc = c.ops.garbage_collect(c.owner, dry_run=True)
    assert orphan in {item["blob"] for item in gc["candidates"]}
    retained = {item["blob"] for item in gc["candidates"]}
    assert summary["manifest_blob"] not in retained and entry_blob not in retained
    backup = c.ops.backup(c.owner)
    restored_home = tmp_path / "restored"
    restore_backup(backup["path"], restored_home, backup["sha256"])
    from daikibo.control import Control
    restored = Control(restored_home, mode="validation", start_workers=False)
    try:
        owner = restored.sec.authenticate(Path(restored.sec.bootstrap()).read_text())
        page = restored.invoke(owner, "run.recovery", {"run": observed["run"], "limit": 10})
        assert page["total"] == 3 and any(item["path"] == "backup.txt" for item in page["entries"])
    finally:
        restored.close()


def test_receipt_without_recovery_keeps_legacy_read_contract(full, full_project):
    c = full
    project, _, _, _ = full_project
    snapshot = c.sn.capture(c.owner, project)
    observed, after, changes = _observe(c, project, snapshot, "pass")
    assert after is not None and changes == []
    run = c.rt.run_status(c.owner, observed["run"])
    assert "recovery" not in run
    with pytest.raises(Fault) as exc:
        c.invoke(c.owner, "run.recovery", {"run": observed["run"]})
    assert exc.value.code == "recovery_unavailable"


def test_reconcile_retries_partial_storage_and_deduplicates_same_manifest(full, full_project, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    original = c.s.blob_put_stream
    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "storage unavailable")
    monkeypatch.setattr(c.s, "blob_put_stream", no_space)
    snapshot = c.sn.capture(c.owner, project)
    observed, _, _ = _observe(c, project, snapshot, "from pathlib import Path; Path('retry.txt').write_text('retry')")
    first = c.rt.reconcile_retention()
    assert first["scanned"] == 1
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='retention_reconciled' AND json_extract(body,'$.run')=?", (observed["run"],))["n"] == 1
    monkeypatch.setattr(c.s, "blob_put_stream", original)
    second = c.rt.reconcile_retention()
    assert second["scanned"] == 1 and second["items"][0]["recovery_artifacts"]["status"] == "complete"
    assert c.s.one("SELECT count(*) AS n FROM events WHERE kind='retention_reconciled' AND json_extract(body,'$.run')=?", (observed["run"],))["n"] == 2
    assert not c.rt.retention.has_pending()
    assert c.rt.reconcile_retention()["scanned"] == 0
    assert c.s.one("SELECT count(*) AS n FROM receipts WHERE run=?", (observed["run"],))["n"] == 1


def test_pending_backup_carries_raw_bytes_and_rebinds_old_home(full, full_project, tmp_path, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "storage unavailable")
    monkeypatch.setattr(c.s, "blob_put_stream", no_space)
    snapshot = c.sn.capture(c.owner, project)
    observed, _, _ = _observe(c, project, snapshot, "from pathlib import Path; Path('pending-raw.txt').write_bytes(b'raw-pending')")
    marker = c.rt.retention.marker_rows()[0]
    old_home = str(c.s.home)
    monkeypatch.setattr(c.s, "blob_put_stream", c.s.__class__.blob_put_stream.__get__(c.s, c.s.__class__))
    backup = c.ops.backup(c.owner)
    with zipfile.ZipFile(backup["path"]) as archive:
        names = set(archive.namelist())
        raw_members = [name for name in names if name.endswith("/pending-raw.txt")]
        assert raw_members
        assert "recovery/pending/" + observed["run"] + ".json" in names
    restored_home = tmp_path / "fresh-home"
    restore_backup(backup["path"], restored_home, backup["sha256"])
    restored_marker = json.loads((restored_home / "recovery" / "pending" / (observed["run"] + ".json")).read_text())
    assert old_home not in json.dumps(restored_marker)
    restored_root = Path(restored_marker["staged_root"])
    assert restored_root.is_dir()
    assert (restored_root / "work" / "app" / "pending-raw.txt").read_bytes() == b"raw-pending"
    assert marker["state"] == "pending_staging"


def test_pending_backup_rejects_missing_and_unreadable_subtrees(full, full_project, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "storage unavailable")
    monkeypatch.setattr(c.s, "blob_put_stream", no_space)
    snapshot = c.sn.capture(c.owner, project)
    observed, _, _ = _observe(c, project, snapshot, "from pathlib import Path; Path('pending.txt').write_text('pending')")
    marker = c.rt.retention.marker_rows()[0]
    staged = Path(marker["staged_root"])
    shutil.rmtree(staged)
    with pytest.raises(Fault) as missing:
        c.ops.backup(c.owner)
    assert missing.value.code == "pending_backup_incomplete"

    # Recreate an independent pending tree so a subtree scan failure is tested
    # after the missing-root assertion without weakening the first marker.
    marker["staged_root"] = str(staged)
    staged.mkdir(parents=True)
    (staged / "work" / "app").mkdir(parents=True)
    (staged / "work" / "app" / "pending.txt").write_text("pending")
    c.rt.retention._write_marker(marker)
    import daikibo.operations as operations
    original_scandir = operations.os.scandir
    denied = staged / "work"
    def deny_subtree(path):
        if not isinstance(path, int) and Path(path) == denied:
            raise PermissionError("pending subtree unreadable")
        return original_scandir(path)
    monkeypatch.setattr(operations.os, "scandir", deny_subtree)
    with pytest.raises(PermissionError, match="pending subtree unreadable"):
        c.ops.backup(c.owner)


def test_pending_backup_preserves_special_metadata_after_fresh_restore(full, full_project, tmp_path, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "storage unavailable")
    monkeypatch.setattr(c.s, "blob_put_stream", no_space)
    snapshot = c.sn.capture(c.owner, project)
    command = (
        "from pathlib import Path; import os; "
        "Path('pending.txt').write_text('pending'); "
        "Path('unsafe-link').symlink_to('/outside/retention-target'); "
        "os.mkfifo('worker.pipe')"
    )
    observed, _, _ = _observe(c, project, snapshot, command)
    assert observed["recovery_artifacts"]["status"] == "partial"
    marker = c.rt.retention.marker_rows()[0]
    old_home = str(c.s.home)
    monkeypatch.setattr(c.s, "blob_put_stream", c.s.__class__.blob_put_stream.__get__(c.s, c.s.__class__))
    backup = c.ops.backup(c.owner)
    staged = Path(marker["staged_root"])
    shutil.rmtree(staged)
    restored_home = tmp_path / "fresh-special-home"
    restore_backup(backup["path"], restored_home, backup["sha256"])
    restored_marker_path = restored_home / "recovery" / "pending" / f"{observed['run']}.json"
    restored_inventory_path = restored_home / "recovery" / "pending" / f"{observed['run']}.inventory"
    restored_marker = json.loads(restored_marker_path.read_text())
    inventory = json.loads(restored_inventory_path.read_text())
    assert old_home not in json.dumps(restored_marker)
    assert inventory["format"] == "daikibo.pending-recovery-inventory.v1"
    assert not (Path(restored_marker["staged_root"]) / "work" / "app" / "unsafe-link").exists()

    from daikibo.control import Control
    restored = Control(restored_home, mode="validation", start_workers=False)
    try:
        owner = restored.sec.authenticate(Path(restored.sec.bootstrap()).read_text())
        page = restored.invoke(owner, "run.recovery", {"run": observed["run"], "limit": 20})
        entries = {item["path"]: item for item in page["entries"]}
        assert entries["unsafe-link"]["kind"] == "symlink"
        assert entries["unsafe-link"]["target"] == "/outside/retention-target"
        assert entries["worker.pipe"]["kind"] == "fifo"
        assert entries["worker.pipe"]["retention_status"] == "metadata_only"
    finally:
        restored.close()


def test_pending_backup_binds_home_relative_roots_when_ancestor_is_named_recovery(tmp_path):
    home = tmp_path / "recovery" / "control"
    source = tmp_path / "recovery" / "source"
    source.mkdir(parents=True)
    (source / "calc.py").write_text("def add(a,b):\n    return a-b\n")
    c = Control(home, mode="validation", start_workers=False)
    c.owner = c.sec.authenticate(Path(c.sec.bootstrap()).read_text())
    try:
        project = c.k.create_project(c.owner, "recovery ancestor")['id']
        rid = c.sn.register(c.owner, project, "app", str(source))['id']
        src = c.k.source(c.owner, project, "Addition returns the arithmetic sum.")
        req = c.k.propose(c.owner, project, "requirement", {"title":"Addition", "statement":"Returns arithmetic sum",
            "acceptance":["AC-ADD"], "source_refs":[src['id']]})
        c.k.accept(c.owner, req['id'], 1)
        c.k.classify(c.owner, src['id'], 0, src['characters'], "requirement", [req['id']], "Original source")
        c.rt.retention.begin(project=project, run="RUN-root-binding", task=None, epoch=None, role="implementer",
                             root=c.rt.workroot / "RUN-root-binding", snapshot=c.sn.capture(c.owner, project))
        root = c.rt.workroot / "RUN-root-binding"
        (root / "work" / "app").mkdir(parents=True)
        (root / "work" / "app" / "worker.pipe").parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(root / "work" / "app" / "worker.pipe")
        c.rt.retention.stage("RUN-root-binding")
        marker = c.rt.retention.marker_rows()[0]
        backup = c.ops.backup(c.owner)
        shutil.rmtree(Path(marker["staged_root"]))
        restored_home = tmp_path / "recovery" / "restored"
        restore_backup(backup["path"], restored_home, backup["sha256"])
        restored_marker = json.loads((restored_home / "recovery" / "pending" / "RUN-root-binding.json").read_text())
        assert Path(restored_marker["staged_root"]) == restored_home / "recovery" / "staging" / "RUN-root-binding"
        assert Path(restored_marker["work_root"]) == restored_home / "recovery" / "staging" / "RUN-root-binding" / "work"
        assert str(home) not in json.dumps(restored_marker)
        assert Path(restored_marker["staged_root"]).is_dir()
    finally:
        c.close()


def _pending_generation_fixture(c, full_project, run="RUN-rebackup-generations"):
    project = full_project[0]
    root = c.rt.workroot / run
    snapshot = c.sn.capture(c.owner, project)
    c.rt.retention.begin(project=project, run=run, task=None, epoch=None, role="implementer",
                         root=root, snapshot=snapshot)
    (root / "work" / "app").mkdir(parents=True)
    (root / "work" / "app" / "payload.txt").write_text("payload-generation")
    os.mkfifo(root / "work" / "app" / "pending.pipe")
    c.rt.retention.stage(run)
    return project, run


def _open_without_startup_reconcile(monkeypatch, home):
    from daikibo.operations import Operations
    monkeypatch.setattr(Operations, "reconcile_startup", lambda self: None)
    restored = Control(home, mode="validation", start_workers=False)
    restored.owner = restored.sec.authenticate(Path(restored.sec.bootstrap()).read_text())
    return restored


def test_pending_backup_is_idempotent_across_multiple_restore_generations(full, full_project, tmp_path, monkeypatch):
    c = full
    _, run = _pending_generation_fixture(c, full_project)
    first = c.ops.backup(c.owner)
    first_home = tmp_path / "generation-one"
    restore_backup(first["path"], first_home, first["sha256"])
    first_marker = json.loads((first_home / "recovery" / "pending" / f"{run}.json").read_text())
    first_root = Path(first_marker["staged_root"])
    assert (first_root / "work" / "app" / "payload.txt").read_bytes() == b"payload-generation"
    assert not (first_root / "work" / "app" / "pending.pipe").exists()

    restored_one = _open_without_startup_reconcile(monkeypatch, first_home)
    try:
        second = restored_one.ops.backup(restored_one.owner)
    finally:
        restored_one.close()
    second_home = tmp_path / "generation-two"
    restore_backup(second["path"], second_home, second["sha256"])
    second_marker = json.loads((second_home / "recovery" / "pending" / f"{run}.json").read_text())
    second_inventory = json.loads((second_home / "recovery" / "pending" / f"{run}.inventory").read_text())
    assert Path(second_marker["staged_root"]) == second_home / "recovery" / "staging" / run
    assert {item["kind"] for item in second_inventory["entries"] if item["path"].endswith("pending.pipe")} == {"fifo"}
    regular = next(item for item in second_inventory["entries"] if item["path"].endswith("payload.txt"))
    assert regular["sha256"] == hashlib.sha256(b"payload-generation").hexdigest()
    shutil.rmtree(first_root)

    restored_two = _open_without_startup_reconcile(monkeypatch, second_home)
    try:
        third = restored_two.ops.backup(restored_two.owner)
    finally:
        restored_two.close()
    third_home = tmp_path / "generation-three"
    restore_backup(third["path"], third_home, third["sha256"])
    third_inventory = json.loads((third_home / "recovery" / "pending" / f"{run}.inventory").read_text())
    assert any(item["path"].endswith("pending.pipe") and item["kind"] == "fifo"
               for item in third_inventory["entries"])
    assert any(item["path"].endswith("payload.txt") and item["bytes"] == len(b"payload-generation")
               for item in third_inventory["entries"])
    assert third_inventory["provenance"]["generation"] >= second_inventory["provenance"]["generation"]


def test_pending_restore_reconciles_on_unpatched_control_startup(full, full_project, tmp_path):
    c = full
    _, run = _pending_generation_fixture(c, full_project, run="RUN-normal-startup")
    backup = c.ops.backup(c.owner)
    restored_home = tmp_path / "normal-startup"
    restore_backup(backup["path"], restored_home, backup["sha256"])

    restored = Control(restored_home, mode="validation", start_workers=False)
    try:
        summary = restored.rt.retention.summary(run)
        assert summary is not None and summary["status"] == "complete"
        manifest = restored.rt.retention._manifest(summary)
        values = {}
        for page in restored.rt.retention._page_descriptors(manifest):
            page_body = json.loads(restored.s.blob_get(page["blob"]))
            for chunk in page_body["chunks"]:
                chunk_body = json.loads(restored.s.blob_get(chunk["blob"]))
                values.update({entry["path"]: entry for entry in chunk_body["entries"]})
        assert values["payload.txt"]["retention_status"] == "stored"
        assert values["payload.txt"]["observed_bytes"] == len(b"payload-generation")
        assert values["pending.pipe"]["kind"] == "fifo"
        assert values["pending.pipe"]["retention_status"] == "metadata_only"
        assert not restored.rt.retention.has_pending()
    finally:
        restored.close()


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_pending_restore_keeps_regular_integrity_failure_pending(full, full_project, tmp_path, mutation):
    c = full
    _, run = _pending_generation_fixture(c, full_project, run=f"RUN-startup-{mutation}")
    backup = c.ops.backup(c.owner)
    restored_home = tmp_path / mutation
    restore_backup(backup["path"], restored_home, backup["sha256"])
    marker = json.loads((restored_home / "recovery" / "pending" / f"{run}.json").read_text())
    payload = Path(marker["staged_root"]) / "work" / "app" / "payload.txt"
    if mutation == "missing":
        payload.unlink()
    else:
        payload.write_text("tampered after restore")

    restored = Control(restored_home, mode="validation", start_workers=False)
    try:
        summary = restored.rt.retention.summary(run)
        assert summary is None or summary.get("status") != "complete"
        rows = restored.rt.retention.marker_rows()
        assert len(rows) == 1 and rows[0]["state"] == "pending"
        assert rows[0]["retention_error"]["code"] == "integrity_error"
        assert restored.rt.retention.has_pending()
    finally:
        restored.close()


def test_source_too_large_survives_scoped_restart_backup_restore_startup(full, full_project, tmp_path):
    """Exercise the complete diagnostic path with the real runtime collector.

    This is a bounded fixture for retention/restart behavior.  It does not
    certify a candidate, admission, or any model-driven scientific result.
    """
    c = full
    project, repo_id, _, _ = full_project
    snapshot = c.sn.capture(c.owner, project)
    observed, after, _ = _observe(
        c, project, snapshot,
        "from pathlib import Path; Path('oversized.bin').write_bytes(b'x'*(33*1024*1024))",
    )
    assert after is None
    assert observed["failure"]["code"] == "collector_error"
    assert observed["result"]["collector_error"]["code"] == "source_too_large"
    assert observed["recovery_artifacts"]["status"] == "complete"

    scoped = Actor("scoped-owner", "owner", project=project)
    page = c.invoke(scoped, "run.recovery", {"run": observed["run"], "limit": 20})
    values = {entry["path"]: entry for entry in page["entries"]}
    assert values["oversized.bin"]["classification"] == "oversized_unclassified"
    old_home = c.s.home
    c.close()
    restarted = Control(old_home, mode="validation", start_workers=False)
    try:
        restarted_owner = restarted.sec.authenticate(Path(restarted.sec.bootstrap()).read_text())
        restarted_page = restarted.invoke(restarted_owner, "run.recovery", {"run": observed["run"], "limit": 20})
        assert restarted_page["total"] == page["total"]
        backup = restarted.ops.backup(restarted_owner)
        portable_backup = tmp_path / "retention-backup.zip"
        shutil.copyfile(backup["path"], portable_backup)
    finally:
        restarted.close()
    shutil.rmtree(old_home)

    restored_home = tmp_path / "fresh-after-restart"
    restore_backup(portable_backup, restored_home, backup["sha256"])
    restored = Control(restored_home, mode="validation", start_workers=False)
    try:
        owner = restored.sec.authenticate(Path(restored.sec.bootstrap()).read_text())
        recovered = restored.invoke(owner, "run.recovery", {"run": observed["run"], "limit": 20})
        assert recovered["total"] == page["total"]
        large = next(item for item in recovered["entries"] if item["path"] == "oversized.bin")
        assert large["repo"] == repo_id
        assert large["observed_bytes"] == 33 * 1024 * 1024
        assert large["retention_status"] == "stored"
    finally:
        restored.close()


def test_pending_backup_rejects_sidecar_conflict_missing_hash_and_malformed_inventory(full, full_project, tmp_path):
    c = full
    _, run = _pending_generation_fixture(c, full_project, run="RUN-rebackup-invalid")
    first = c.ops.backup(c.owner)
    from daikibo.operations import _pending_recovery_inventory

    conflict_home = tmp_path / "conflict"
    restore_backup(first["path"], conflict_home, first["sha256"])
    conflict_marker_path = conflict_home / "recovery" / "pending" / f"{run}.json"
    conflict_marker = json.loads(conflict_marker_path.read_text())
    payload = Path(conflict_marker["staged_root"]) / "work" / "app" / "payload.txt"
    payload.write_text("changed-payload")
    with pytest.raises(Fault) as conflict:
        _pending_recovery_inventory(conflict_home, conflict_marker)
    assert conflict.value.code == "pending_backup_conflict"

    missing_hash_home = tmp_path / "missing-hash"
    restore_backup(first["path"], missing_hash_home, first["sha256"])
    missing_hash_path = missing_hash_home / "recovery" / "pending" / f"{run}.inventory"
    missing_hash = json.loads(missing_hash_path.read_text())
    next(item for item in missing_hash["entries"] if item["kind"] == "file").pop("sha256")
    missing_hash_path.write_text(json.dumps(missing_hash))
    missing_hash_marker = json.loads((missing_hash_home / "recovery" / "pending" / f"{run}.json").read_text())
    with pytest.raises(Fault) as missing:
        _pending_recovery_inventory(missing_hash_home, missing_hash_marker)
    assert missing.value.code == "pending_backup_incomplete"

    malformed_home = tmp_path / "malformed"
    restore_backup(first["path"], malformed_home, first["sha256"])
    malformed_path = malformed_home / "recovery" / "pending" / f"{run}.inventory"
    malformed_path.write_text("{not-json")
    malformed_marker = json.loads((malformed_home / "recovery" / "pending" / f"{run}.json").read_text())
    with pytest.raises(Exception):
        _pending_recovery_inventory(malformed_home, malformed_marker)

    missing_sidecar_home = tmp_path / "missing-sidecar"
    restore_backup(first["path"], missing_sidecar_home, first["sha256"])
    missing_sidecar_path = missing_sidecar_home / "recovery" / "pending" / f"{run}.inventory"
    missing_sidecar_path.unlink()
    missing_sidecar_marker = json.loads((missing_sidecar_home / "recovery" / "pending" / f"{run}.json").read_text())
    with pytest.raises(Fault) as missing_sidecar:
        _pending_recovery_inventory(missing_sidecar_home, missing_sidecar_marker)
    assert missing_sidecar.value.code == "pending_backup_incomplete"

    special_conflict_home = tmp_path / "special-conflict"
    restore_backup(first["path"], special_conflict_home, first["sha256"])
    special_marker = json.loads((special_conflict_home / "recovery" / "pending" / f"{run}.json").read_text())
    (Path(special_marker["staged_root"]) / "work" / "app" / "pending.pipe").write_text("regular collision")
    with pytest.raises(Fault) as special_conflict:
        _pending_recovery_inventory(special_conflict_home, special_marker)
    assert special_conflict.value.code == "pending_backup_conflict"


def test_error_summary_is_bounded_while_manifest_keeps_exact_count(full, full_project, tmp_path, monkeypatch):
    c = full
    project, rid, _, _ = full_project
    snapshot = c.sn.capture(c.owner, project)
    run = "RUN-bounded-retention"
    root = tmp_path / "worker-root"
    work = root / "work" / "repo"
    work.mkdir(parents=True)
    for index in range(2200):
        (work / f"failure-{index:04d}.txt").write_text(str(index))
    c.rt.retention.begin(project=project, run=run, task=None, epoch=None, role="implementer",
                        root=root, snapshot=snapshot)
    def fail_file(*args, **kwargs):
        raise OSError(errno.ENOSPC, "simulated per-file failure")
    monkeypatch.setattr(c.s, "blob_put_file", fail_file)
    summary = c.rt.retention.retain(project=project, run=run, task=None, epoch=None, role="implementer",
                                    snapshot_digest=snapshot["digest"], collector_failure={"code": "collector"},
                                    work=root / "work", repos=[{"id": rid, "name": "repo"}])
    marker_path = c.rt.retention._marker_path(run)
    marker_bytes = marker_path.read_bytes()
    assert len(marker_bytes) < 2 * 1024 * 1024
    marker = json.loads(marker_bytes)
    assert summary["status"] == "partial"
    assert summary["retention_error_count"] == 2200
    assert len(summary["retention_error_sample"]) <= 32
    assert marker["summary"]["retention_error_count"] == 2200
    assert len(marker["summary"]["retention_error"]) <= 32


def test_recovery_reader_honors_task_scoped_actor(full, full_project, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _, rid, req, _ = full_project
    task = c.w.create(c.owner, project, {"title": "retention task", "goal": "inspect",
        "read_artifacts": [req], "write_paths": [], "acceptance": ["AC-ADD"], "dependencies": [], "repos": [rid], "non_goals": []})["id"]
    _force_collection_failure(monkeypatch, c)
    snapshot = c.sn.capture(c.owner, project)
    def argv_factory(work, home, cwd):
        return [sys.executable, "-c", "from pathlib import Path; Path('task.txt').write_text('task')"], None
    observed, _, _ = c.rt.observe(project, task, "retention-task", "implementer", None,
                                  "retention-binding", snapshot, argv_factory, timeout=60)
    scoped = Actor("task-agent", "agent", project=project, task=task)
    assert c.invoke(scoped, "run.recovery", {"run": observed["run"], "limit": 10})["total"] >= 2
    with pytest.raises(Fault) as other_task:
        c.invoke(Actor("other-task", "agent", project=project, task="TASK-other"),
                 "run.recovery", {"run": observed["run"]})
    assert other_task.value.code == "forbidden"
    with pytest.raises(Fault) as other_project:
        c.invoke(Actor("other-project", "agent", project="PRJ-other", task=task),
                 "run.recovery", {"run": observed["run"]})
    assert other_project.value.code == "forbidden"


def test_chunked_history_exports_recovery_transitive_blob_closure(full, full_project, tmp_path, monkeypatch):
    c = full
    project, _, _, _ = full_project
    _force_collection_failure(monkeypatch, c)
    snapshot = c.sn.capture(c.owner, project)
    observed, _, _ = _observe(c, project, snapshot, "from pathlib import Path; Path('history.txt').write_text('history')")
    baseline = c.k.baseline(c.owner, project, layout="chunked", chunk_bytes=1024)
    exported = c.history.export_archive(c.owner, baseline["id"])
    portable = tmp_path / "portable-recovery.zip"
    shutil.copyfile(exported["path"], portable)
    with zipfile.ZipFile(portable) as archive:
        payload = json.loads(archive.read("snapshot.json"))
        assert payload["format"] == "daikibo.knowledge-snapshot.v12"
        records_blob = b"".join(archive.read("objects/" + part["sha256"])
                                  for part in payload["records"]["chunks"])
        records = [json.loads(line) for line in records_blob.splitlines()]
        recovery = [record for record in records if record["section"] == "collector_failure_history"]
        assert recovery
        durable = next(record for record in recovery if record["row"].get("run") == observed["run"])
        kinds = {raw["kind"] for raw in durable.get("raw_objects", [])}
        assert {"manifest", "entry_page", "entry_chunk", "artifact"} <= kinds
    c.close()
    report = inspect_archive(portable, hashlib.file_digest(portable.open("rb"), "sha256").hexdigest())
    assert report["verified"] and report["format"] == "daikibo.knowledge-archive.v12"
