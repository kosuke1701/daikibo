"""Finite Unit4-O origin storage, migration, and portable retention checks."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.db import SCHEMA_VERSION, Store
from daikibo.knowledge_history import validate_specifications
from daikibo.program_origins import legacy_program_digest, resolve_program_origin
from daikibo.operations import restore_backup
from portable_origin_fixture import fixture_manifest, materialize_fixture


def _program_fixture(full, name="origin"):
    project = full.k.create_project(full.owner, name)["id"]
    source = full.k.source(full.owner, project, "Retain this source for the origin fixture.")
    return project, source, full.p.begin(full.owner, project, source["id"], compact=True)["program"]


def test_begin_stamps_immutable_e3_origin_for_all_public_modes(full, tmp_path):
    cases = []
    for mode in ("auto", "greenfield"):
        project, source, program = _program_fixture(full, f"origin-{mode}")
        result = full.p.begin(full.owner, project, source["id"], mode=mode, compact=True)
        cases.append((project, result["program"]))
    project, source, _ = _program_fixture(full, "origin-brownfield")
    repo = tmp_path / "repo"
    repo.mkdir()
    full.sn.register(full.owner, project, "fixture", str(repo))
    result = full.p.begin(full.owner, project, source["id"], mode="brownfield", compact=True)
    cases.append((project, result["program"]))
    for project, program in cases:
        origin = resolve_program_origin(full.s, project=project, program=program)
        assert origin["policy"] == "e3-required"
        assert origin["origin"] == "program.begin"
        assert origin["legacy_program_digest"] is None
        assert set(origin["body"]) == {
            "format", "program", "project", "policy", "origin",
            "introduced_schema", "legacy_program_digest",
        }
        assert not {"allow", "gate", "strong"} & set(origin)


def test_begin_origin_and_program_event_rollback_together(full, monkeypatch):
    project = full.k.create_project(full.owner, "origin-rollback")["id"]
    source = full.k.source(full.owner, project, "Retain this source for rollback.")
    import daikibo.program_origins as origins

    def fail(*args, **kwargs):
        raise Fault("origin_write_failed", "fixture origin write failure")

    monkeypatch.setattr(origins, "insert_origin", fail)
    with pytest.raises(Fault) as rejected:
        full.p.begin(full.owner, project, source["id"], compact=True)
    assert rejected.value.code == "origin_write_failed"
    assert full.s.one("SELECT count(*) AS n FROM programs WHERE project=?", (project,))["n"] == 0
    assert full.s.one(
        "SELECT count(*) AS n FROM events WHERE project=? AND kind='program_started'", (project,)
    )["n"] == 0


@pytest.mark.parametrize("damage", ["missing", "unknown", "bad_digest", "foreign", "illegal"])
def test_origin_resolver_rejects_missing_unknown_corrupt_and_foreign_records(full, damage):
    project, source, program = _program_fixture(full, f"origin-negative-{damage}")
    row = full.s.one("SELECT * FROM program_origins WHERE program=?", (program,), True)
    if damage == "missing":
        full.s.execute("DROP TRIGGER program_origins_no_delete")
        full.s.execute("DELETE FROM program_origins WHERE program=?", (program,))
    else:
        full.s.execute("DROP TRIGGER program_origins_no_update")
        body = json.loads(row["body"])
        if damage == "unknown":
            body["origin"] = "schema-guess"
        elif damage == "bad_digest":
            full.s.execute("UPDATE program_origins SET digest=? WHERE program=?", ("0" * 64, program))
        elif damage == "foreign":
            other = full.k.create_project(full.owner, "foreign-origin-project")["id"]
            full.s.execute("UPDATE program_origins SET project=? WHERE program=?", (other, program))
        elif damage == "illegal":
            body["policy"] = "legacy-preserved"
        if damage in {"unknown", "illegal"}:
            full.s.execute(
                "UPDATE program_origins SET digest=?,body=? WHERE program=?",
                (digest(body), canonical(body).decode(), program),
            )
    with pytest.raises(Fault) as rejected:
        resolve_program_origin(full.s, project=project, program=program)
    assert rejected.value.code in {"origin_missing", "origin_invalid", "origin_cross_project"}


def test_origin_rows_cannot_be_updated_or_deleted(full):
    project, source, program = _program_fixture(full, "origin-immutable")
    with pytest.raises(sqlite3.IntegrityError):
        full.s.execute("UPDATE program_origins SET body=? WHERE program=?", ("{}", program))
    with pytest.raises(sqlite3.IntegrityError):
        full.s.execute("DELETE FROM program_origins WHERE program=?", (program,))


@pytest.mark.parametrize("kind", ["export", "chunked"])
def test_schema16_missing_origin_table_cannot_downgrade_output(full, kind):
    project, source, _ = _program_fixture(full, f"origin-missing-table-{kind}")
    full.s.execute("DROP TABLE program_origins")
    with pytest.raises(Fault) as rejected:
        if kind == "export":
            full.k.export(full.owner, project)
        else:
            full.k.baseline(full.owner, project, layout="chunked")
    assert rejected.value.code in {"origin_schema_missing", "origin_schema_invalid"}


def test_backup_rejects_missing_origin_row(full):
    project, source, program = _program_fixture(full, "origin-backup-missing")
    full.s.execute("DROP TRIGGER program_origins_no_delete")
    full.s.execute("DELETE FROM program_origins WHERE program=?", (program,))
    with pytest.raises(Fault) as rejected:
        full.ops.backup(full.owner)
    assert rejected.value.code == "origin_integrity"


@pytest.mark.parametrize(("field", "value"), [("policy", []), ("origin", {})])
def test_origin_resolver_rejects_malformed_types_as_fault(full, field, value):
    project, source, program = _program_fixture(full, f"origin-type-{field}")
    row = full.s.one("SELECT * FROM program_origins WHERE program=?", (program,), True)
    body = json.loads(row["body"])
    body[field] = value
    full.s.execute("DROP TRIGGER program_origins_no_update")
    full.s.execute(
        "UPDATE program_origins SET body=?,digest=? WHERE program=?",
        (canonical(body).decode(), digest(body), program),
    )
    with pytest.raises(Fault) as rejected:
        resolve_program_origin(full.s, project=project, program=program)
    assert rejected.value.code == "origin_invalid"


def test_migration_rejects_v16_origin_table_in_pre_v16_database(tmp_path):
    home = tmp_path / "downgrade-with-origin"
    store = Store(home)
    store.close()
    db = sqlite3.connect(home / "state.sqlite3")
    db.execute("PRAGMA user_version=15")
    db.commit()
    db.close()
    with pytest.raises(Fault) as rejected:
        Store(home)
    assert rejected.value.code == "origin_schema_invalid"


def test_real_schema15_fixture_migrates_twice_without_changing_old_rows(tmp_path):
    before = fixture_manifest("legacy")
    home = materialize_fixture("legacy", tmp_path / "migrated")
    old_db = sqlite3.connect(home / "state.sqlite3")
    assert old_db.execute("PRAGMA user_version").fetchone()[0] == 15
    assert old_db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='program_origins'"
    ).fetchone() is None
    old_db.close()
    control = __import__("daikibo.control", fromlist=["Control"]).Control(
        home, mode="validation", start_workers=False,
    )
    try:
        control.owner = control.sec.authenticate(None)
        assert control.s.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION == 16
        assert (home / "pre-migration-v15.sqlite3").is_file()
        assert dict(control.s.one("SELECT * FROM programs WHERE id=?", (before["program"],))) == before["program_row"]
        assert control.s.one("SELECT count(*) AS n FROM sources WHERE project=?", (before["project"],))["n"] == before["source_count"]
        assert control.s.one("SELECT count(*) AS n FROM events WHERE project=?", (before["project"],))["n"] == before["event_count"]
        first = resolve_program_origin(control.s, project=before["project"], program=before["program"])
        assert first["policy"] == "legacy-preserved"
        assert first["legacy_program_digest"] == legacy_program_digest(before["program_row"])
    finally:
        control.close()
    reopened = __import__("daikibo.control", fromlist=["Control"]).Control(
        home, mode="validation", start_workers=False,
    )
    try:
        reopened.owner = reopened.sec.authenticate(None)
        second = resolve_program_origin(reopened.s, project=before["project"], program=before["program"])
        assert second == first
        assert reopened.s.one("SELECT count(*) AS n FROM program_origins")["n"] == 1
    finally:
        reopened.close()


def test_schema15_migration_keeps_legacy_origin_when_a_new_program_begins(tmp_path):
    """Migration history stays legacy while later begin() rows use E3 origin."""
    before = fixture_manifest("legacy")
    home = materialize_fixture("legacy", tmp_path / "migration-then-begin")
    control = __import__("daikibo.control", fromlist=["Control"]).Control(
        home, mode="validation", start_workers=False,
    )
    try:
        control.owner = control.sec.authenticate(None)
        old_origin = resolve_program_origin(
            control.s, project=before["project"], program=before["program"],
        )
        source = control.k.source(
            control.owner, before["project"],
            "A source added after schema-15 origin migration.",
        )
        begun = control.p.begin(
            control.owner, before["project"], source["id"], compact=True,
        )["program"]
        assert resolve_program_origin(
            control.s, project=before["project"], program=before["program"],
        ) == old_origin
        new_origin = resolve_program_origin(
            control.s, project=before["project"], program=begun,
        )
        assert new_origin["policy"] == "e3-required"
        assert new_origin["origin"] == "program.begin"
        assert new_origin["legacy_program_digest"] is None
        assert control.s.one(
            "SELECT count(*) AS n FROM program_origins WHERE project=?",
            (before["project"],),
        )["n"] == 2
    finally:
        control.close()


def test_schema16_backfill_failure_rolls_back_table_and_user_version(tmp_path, monkeypatch):
    before = fixture_manifest("legacy")
    home = materialize_fixture("legacy", tmp_path / "failed-migration")
    import daikibo.db as db

    original = db._backfill_program_origins

    def fail(connection):
        original(connection)
        raise Fault("origin_backfill_failed", "fixture backfill failure")

    monkeypatch.setattr(db, "_backfill_program_origins", fail)
    with pytest.raises(Fault) as rejected:
        Store(home)
    assert rejected.value.code == "origin_backfill_failed"
    check = sqlite3.connect(home / "state.sqlite3")
    assert check.execute("PRAGMA user_version").fetchone()[0] == 15
    assert check.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='program_origins'"
    ).fetchone() is None
    assert check.execute("SELECT * FROM programs WHERE id=?", (before["program"],)).fetchone() is not None
    check.close()


def test_monolithic_spec_v5_and_chunked_v12_keep_origins_and_reject_tamper(full, tmp_path):
    project, source, program = _program_fixture(full, "origin-archives")
    specification = full.k.export(full.owner, project)
    assert specification["format"] == "daikibo.spec.v5"
    history = specification["planning_history"]
    assert history["format"] == "daikibo.planning-history.v2"
    assert history["program_origins"][0]["program"] == program
    validate_specifications(specification)
    damaged = copy.deepcopy(specification)
    damaged["planning_history"]["program_origins"][0]["body"]["origin"] = "unknown"
    damaged["planning_history"]["program_origins"][0]["digest"] = digest(
        damaged["planning_history"]["program_origins"][0]["body"]
    )
    with pytest.raises(Fault):
        validate_specifications(damaged)
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    assert exported["format"] == "daikibo.knowledge-archive.v12"
    inspected = full.history.inspect_archive(full.owner, exported["path"], exported["sha256"])
    assert inspected["verified"] is True
    assert inspected["format"] == "daikibo.knowledge-archive.v12"
    assert inspected["counts"]["program_origins"] == 1
    # The old v2 archive remains a historical read-only fixture.
    old_archive = Path(__file__).parent / "fixtures/dev5-v2-history.dkarchive"
    assert full.history.inspect_archive(full.owner, old_archive, digest(old_archive.read_bytes()))["verified"]
    legacy_archive = Path(__file__).parent / "fixtures/unit4_origin_legacy/legacy_v3_origin_fixture.dkarchive"
    assert full.history.inspect_archive(
        full.owner, legacy_archive, digest(legacy_archive.read_bytes()),
    )["verified"]


def test_backup_restore_and_gc_retain_origin_without_treating_it_as_a_cas_leaf(full, tmp_path):
    project, source, program = _program_fixture(full, "origin-retention")
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    backup = full.ops.backup(full.owner)
    orphan = b"orphan retained only to exercise the GC boundary"
    orphan_digest = hashlib.sha256(orphan).hexdigest()
    orphan_path = full.s.blobs / orphan_digest[:2] / orphan_digest[2:]
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(orphan)
    os.utime(orphan_path, (0, 0))
    dry = full.ops.garbage_collect(full.owner, dry_run=True, minimum_age=86400)
    assert any(item["blob"] == orphan_digest for item in dry["candidates"])
    full.ops.garbage_collect(full.owner, dry_run=False, minimum_age=86400)
    assert not orphan_path.exists()
    assert resolve_program_origin(full.s, project=project, program=program)["origin"] == "program.begin"
    restored_home = tmp_path / "restored"
    restore_backup(backup["path"], restored_home, backup["sha256"])
    restored = __import__("daikibo.control", fromlist=["Control"]).Control(
        restored_home, mode="validation", start_workers=False,
    )
    try:
        restored.owner = restored.sec.authenticate(None)
        assert resolve_program_origin(restored.s, project=project, program=program)["origin"] == "program.begin"
        assert restored.history.inspect_archive(
            restored.owner, exported["path"], exported["sha256"],
        )["verified"]
    finally:
        restored.close()
