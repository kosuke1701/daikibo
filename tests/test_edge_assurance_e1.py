from __future__ import annotations

import json
import sqlite3

import pytest

from daikibo.assurance import validate_assurance_rows
from daikibo.assurance_relations import REGISTRY_DIGEST, RELATION_REGISTRY, validate_typed_ref
from daikibo.common import Fault, canonical, digest
from daikibo.db import SCHEMA_VERSION, Store


def _profile_ref(project, row, body, semantic):
    return {"kind": "artifact", "project": project, "artifact": row["id"],
            "revision": 1, "body_digest": digest(body)}


def _edge_fixture(full):
    actor = full.owner
    project = full.k.create_project(actor, "edge E1")['id']
    first = {"format": "identity.v1", "project": project, "name": "left"}
    second = {"format": "identity.v1", "project": project, "name": "right"}
    left = full.assurance.store_object(actor, project, "profile", "left", 1, first)
    right = full.assurance.store_object(actor, project, "profile", "right", 1, second)
    left_ref = _profile_ref(project, left, first, "artifact")
    right_ref = _profile_ref(project, right, second, "artifact")
    edge = {
        "format": "edge.v1", "project": project, "source_ref": left_ref,
        "target_ref": right_ref, "relation": "realizes",
        "relation_contract_digest": REGISTRY_DIGEST, "scope_ref": left_ref,
        "claim": "mechanical identity fixture", "obligation_ids": [],
        "required_evidence_refs": [], "authority_refs": [],
    }
    return project, left, right, edge


def test_registry_has_exactly_thirteen_contracts_and_catalog_is_detached(full):
    assert len(RELATION_REGISTRY) == 13
    catalog = full.assurance.catalog(full.owner)
    assert catalog["registry_digest"] == REGISTRY_DIGEST
    catalog["relations"][0]["relation"] = "tampered"
    assert full.assurance.catalog(full.owner)["relations"][0]["relation"] != "tampered"
    assert catalog["schema"] == SCHEMA_VERSION == 17


def test_immutable_object_refs_and_head_compare_swap(full):
    project, left, right, edge = _edge_fixture(full)
    actor = full.owner
    stored = full.assurance.store_object(actor, project, "edge", "realizes:left:right", 1, edge)
    assert len(stored["refs"]) == 3
    event = full.assurance._append_storage_event(actor, project, "realizes:left:right", stored["id"], stored["digest"], "adopt", None, {"mechanical": True})
    assert full.assurance.history(actor, project)["heads"][0]["head_event"] == event["id"]
    with pytest.raises(Fault) as stale:
        full.assurance._append_storage_event(actor, project, "realizes:left:right", stored["id"], stored["digest"], "withdraw", None, {})
    assert stale.value.code == "stale_head"
    with pytest.raises(sqlite3.IntegrityError):
        full.s.execute("UPDATE assurance_objects SET body=? WHERE id=?", (canonical({"changed": True}).decode(), stored["id"]))
    with pytest.raises(Fault) as bad:
        full.assurance.resolve_pinned(actor, {**edge["source_ref"], "body_digest": "0" * 64})
    assert bad.value.code in {"integrity_error", "unresolved_reference"}


def test_typed_refs_reject_cross_project_bool_revision_and_unknown_locator(full):
    actor = full.owner
    project = full.k.create_project(actor, "ref negative")['id']
    with pytest.raises(Fault) as cross:
        validate_typed_ref({"kind": "artifact", "project": "other", "artifact": "A", "revision": 1, "body_digest": "0" * 64}, project=project)
    assert cross.value.code == "cross_project"
    with pytest.raises(Fault):
        validate_typed_ref({"kind": "artifact", "project": project, "artifact": "A", "revision": True, "body_digest": "0" * 64}, project=project)
    with pytest.raises(Fault):
        validate_typed_ref({"kind": "artifact", "project": project, "artifact": "A", "revision": 1, "body_digest": "0" * 64, "table": "artifacts"}, project=project)
    with pytest.raises(Fault):
        validate_typed_ref({"kind": "traceability_ref", "project": project, "locator": {"ref_type": "source_span"}}, project=project)


def test_bounded_object_pages_and_index_projection(full):
    actor = full.owner
    project = full.k.create_project(actor, "bounded")['id']
    for index in range(1001):
        body = {"format": "identity.v1", "project": project, "ordinal": index}
        full.assurance.store_object(actor, project, "profile", f"p-{index:04d}", 1, body)
    page = full.assurance.object_list(actor, project, kind="profile", limit=500)
    assert page["total"] == 1001 and len(page["objects"]) == 500 and page["next_offset"] == 500
    page2 = full.assurance.object_list(actor, project, kind="profile", limit=500, offset=500)
    assert len(page2["objects"]) == 500 and page2["next_offset"] == 1000
    page3 = full.assurance.object_list(actor, project, kind="profile", limit=500, offset=1000)
    assert len(page3["objects"]) == 1 and page3["next_offset"] is None


def test_schema15_migration_and_existing_backup_barrier(tmp_path):
    home = tmp_path / "migration"
    store = Store(home)
    store.conn.executescript("DROP TABLE program_origins; DROP TABLE assurance_refs; DROP TABLE assurance_heads; DROP TABLE assurance_events; DROP TABLE assurance_objects; PRAGMA user_version=14;")
    store.close()
    migrated = Store(home)
    assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 17
    assert migrated.one("SELECT name FROM sqlite_master WHERE name='assurance_objects'")
    migrated.close()
    barrier = tmp_path / "barrier"
    old = Store(barrier)
    old.conn.executescript("DROP TABLE program_origins; DROP TABLE assurance_refs; DROP TABLE assurance_heads; DROP TABLE assurance_events; DROP TABLE assurance_objects; PRAGMA user_version=14;")
    old.close()
    (barrier / "pre-migration-v14.sqlite3").write_bytes(b"reserved")
    with pytest.raises(Fault) as blocked:
        Store(barrier)
    assert blocked.value.code == "migration_backup_exists"


def test_v11_archive_retains_assurance_history_and_rejects_index_drift(full, tmp_path):
    actor = full.owner
    project, left, right, edge = _edge_fixture(full)
    stored = full.assurance.store_object(actor, project, "edge", "archive-edge", 1, edge)
    full.assurance._append_storage_event(actor, project, "archive-edge", stored["id"], stored["digest"], "adopt", None, {"mechanical": True})
    baseline = full.k.baseline(actor, project, layout="chunked", chunk_bytes=1024)
    payload = json.loads(full.s.blob_get(baseline["snapshot_blob"]))
    assert payload["format"] == "daikibo.knowledge-snapshot.v12"
    assert payload["features"]["assurance"] is True
    exported = full.history.export_archive(actor, baseline["id"])
    inspected = full.history.inspect_archive(actor, exported["path"], exported["sha256"])
    assert inspected["format"] == "daikibo.knowledge-archive.v12"
    assert inspected["counts"]["assurance_events"] == 1

    rows = full.assurance.archive_rows(project)
    rows["assurance_refs"][0]["ref_digest"] = "0" * 64
    with pytest.raises(Fault) as drift:
        validate_assurance_rows(rows, project)
    assert drift.value.code == "invalid_archive"


def test_material_pin_has_cas_closure_and_old_plan_becomes_stale(full, tmp_path):
    actor = full.owner
    project = full.k.create_project(actor, "material")['id']
    source = full.k.source(actor, project, "material source")
    artifact = full.k.propose(actor, project, "requirement", {"title": "r", "statement": "r", "acceptance": ["A"], "source_refs": [source["id"]]})
    full.k.accept(actor, artifact["id"], 1)
    repo_root = tmp_path / "repo"; repo_root.mkdir(); (repo_root / "x.py").write_text("x\n")
    repository = full.sn.register(actor, project, "app", str(repo_root))["id"]
    task = full.w.create(actor, project, {"title": "t", "goal": "x", "read_artifacts": [artifact["id"]], "write_paths": ["x.py"], "acceptance": ["A"], "dependencies": [], "repos": [repository], "non_goals": []})
    plan = full.w.plan_tests(actor, task["id"], {"checks": [{"id": "unit", "argv": ["true"], "kind": "pytest", "report": "results.xml", "required_tests": ["unit"], "purpose": "unit"}]})
    pinned = full.assurance.pin(actor, project, {"kind": "test_plan", "task": task["id"], "task_revision": 1, "plan_digest": plan["digest"]})
    assert len(full.assurance.cas_closure(project)) == 1
    assert full.assurance.resolve(actor, project, pinned["ref"])["current"]["state"] == "not_evaluated"
    # A second mutable plan cannot rewrite the original material object.
    full.w.plan_tests(actor, task["id"], {"checks": [{"id": "changed", "argv": ["false"], "kind": "pytest", "report": "results.xml", "required_tests": ["changed"], "purpose": "changed"}]})
    assert full.assurance.evaluate_current(actor, pinned["ref"])["current"]["state"] == "stale"
    baseline = full.k.baseline(actor, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(actor, baseline["id"])
    assert full.history.inspect_archive(actor, exported["path"], exported["sha256"])["format"] == "daikibo.knowledge-archive.v12"


def test_wrapped_source_membership_is_resolved_and_index_revision_is_lossless(full):
    actor = full.owner
    project = full.k.create_project(actor, "source membership")['id']
    source = full.k.source(actor, project, "alpha😀beta")
    raw = full.s.blob_get(source["digest"])

    def span(start, end):
        selected = raw[start:end]
        return {"kind": "traceability_ref", "project": project, "locator": {
            "ref_type": "source_span", "source_id": source["id"], "blob_digest": source["digest"],
            "byte_start": start, "byte_end": end,
            "unicode_start": len(raw[:start].decode("utf-8")), "unicode_end": len(raw[:end].decode("utf-8")),
            "span_hash": digest(selected),
        }}

    outer = span(0, len(raw)); inner = span(1, len(raw) - 1)
    stored = full.assurance.store_object(actor, project, "profile", "source-profile", 1,
                                         {"format": "profile.v1", "project": project, "source": outer})
    refs = full.assurance.refs(actor, project, ref_kind="source_span")
    assert refs["refs"][0]["ref_revision"] == "0"
    membership = full.assurance.contains(actor, project, outer, inner)
    assert membership["contains"] is True and membership["state"] == "verified"
    assert stored["refs"][0]["ref_kind"] == "source_span"
