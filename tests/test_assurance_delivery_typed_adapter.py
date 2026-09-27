import copy
import json
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.gitops import git
from daikibo.assurance import validate_assurance_rows, validate_execution_material_relation
from conftest import finish_task, make_task
from test_delivery_git_and_recovery import profile


def _prepared_verified_delivery(full, full_project):
    project, repository, requirement, _root = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, profile(project, repository, requirement, task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]
    verified = full.d.verify(full.owner, delivery)
    assert all(item["passed"] for item in verified["results"]), verified
    row, body = full.d.current(delivery)
    return project, repository, delivery, row, body


def _delivery_observed_ref(project, receipt):
    return {
        "kind": "observed_result", "project": project,
        "receipt": receipt["id"], "run": receipt["run"],
        "receipt_digest": digest(receipt), "run_binding": receipt["binding"],
        "snapshot_digest": receipt["snapshot"],
        "result_digest": digest(receipt["result"]),
    }


def _delivery_material_parts(full, receipt):
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    run_body = json.loads(run["body"])
    pin = receipt["verification_material"]
    material_row = full.s.one("SELECT * FROM assurance_objects WHERE id=?",
                              (pin["id"],), True)
    material_body = json.loads(material_row["body"])
    payload = json.loads(full.s.blob_get(material_body["payload_blob"]))
    return run, run_body, material_row, material_body, payload


def test_delivery_observed_result_binds_definition_to_delivery_subject(full, full_project):
    project, repository, delivery, row, body = _prepared_verified_delivery(full, full_project)
    receipt = full.g.receipt(body["results"][0]["receipt"])
    ref = _delivery_observed_ref(project, receipt)
    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["resolution"]["current"] is True
    assert resolved["resolution"]["payload"]["execution_subject"] == {
        "kind": "delivery", "id": delivery, "binding": row["digest"]}

    other_delivery = full.d.prepare(full.owner, project)["id"]
    other_row, other_body = full.d.current(other_delivery)
    other_snapshot, _ = full.rt.verification_materials.pin_delivery_snapshot(
        full.owner, project, other_row, other_body,
        captured_from={"controller": "delivery", "operation": "other-delivery"})
    other_check = full.rt.verification_materials.delivery_check_ref(
        project, other_snapshot, other_body["checks"][0])
    run, run_body, material_row, material_body, payload = _delivery_material_parts(full, receipt)
    payload["definition_ref"] = other_check
    with pytest.raises(Fault) as rejected:
        copied_body = copy.deepcopy(material_body)
        copied_payload = copy.deepcopy(payload)
        copied_body["payload_blob"] = full.s.blob_put(canonical(copied_payload))
        copied_body["semantic_digest"] = digest(copied_payload)
        copied_body["dependency_refs"] = [copied_payload["definition_ref"]]
        copied_row = copy.deepcopy(material_row)
        copied_row["body"] = copied_body
        copied_row["digest"] = digest(copied_body)
        copied_receipt = copy.deepcopy(receipt)
        copied_pin = {"id": copied_row["id"], "digest": copied_row["digest"]}
        copied_receipt["verification_material"] = copied_pin
        copied_run_body = copy.deepcopy(run_body)
        copied_run_body["verification_material"] = copied_pin
        validate_execution_material_relation(
            project=project, ref=_delivery_observed_ref(project, copied_receipt),
            run_row=run, run_body=copied_run_body, observed=copied_receipt,
            material_row=copied_row, material_body=copied_body, blob_store=full.s,
            resolve_definition=lambda definition: full.assurance._resolve_locator(
                full.owner, definition, current=False),
            resolve_candidate=lambda candidate: None,
            resolve_artifact=lambda artifact: full.assurance._artifact_body(project, artifact),
        )
    assert rejected.value.code == "integrity_error"


def test_delivery_snapshot_and_check_resolve_from_real_material(full, full_project):
    project, repository, delivery, row, body = _prepared_verified_delivery(full, full_project)
    coordinator = full.rt.verification_materials
    snapshot_ref, _ = coordinator.pin_delivery_snapshot(
        full.owner, project, row, body,
        captured_from={"controller": "delivery", "operation": "test.delivery.snapshot"})
    public_pin = full.assurance.pin(full.owner, project, {
        "kind": "delivery_snapshot", "delivery": delivery,
        "binding_digest": row["digest"], "snapshot_digest": body["snapshot"]["digest"],
    })
    assert full.assurance.resolve_pinned(full.owner, public_pin["ref"])["resolution"]["payload"]["delivery"] == delivery
    resolved = full.assurance.resolve_pinned(full.owner, snapshot_ref)
    assert resolved["resolution"]["payload"]["snapshot"]["digest"] == snapshot_ref["snapshot_digest"]
    assert resolved["dependency_refs"] == []

    check = body["checks"][0]
    check_ref = coordinator.delivery_check_ref(project, snapshot_ref, check)
    check_resolved = full.assurance.resolve_pinned(full.owner, check_ref)
    assert check_resolved["resolution"]["content"] == check
    assert full.assurance.contains(full.owner, project, snapshot_ref, check_ref)["contains"] is True
    nonmember = dict(check_ref, check_id="missing-check")
    # The exact reference remains schema-valid; membership proves the pinned
    # check body rather than accepting an ID-only label.
    with pytest.raises(Fault):
        full.assurance.resolve_pinned(full.owner, nonmember)
    assert full.assurance.contains(full.owner, project, snapshot_ref, nonmember)["contains"] is False


def test_actual_delivery_commit_pins_real_git_closure_and_rejects_identity_mixes(full, full_project):
    project, repository, delivery, row, body = _prepared_verified_delivery(full, full_project)
    result = full.sn.commit_snapshot(body["snapshot"], repository, "typed adapter fixture")
    updated = copy.deepcopy(body)
    updated["git"][repository] = result
    full.s.execute("UPDATE deliveries SET body=? WHERE id=?", (canonical(updated).decode(), delivery))
    saved_row = full.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery, project), True)
    actual_ref, _ = full.rt.verification_materials.pin_actual_delivery_commit(
        full.owner, project, saved_row, updated, repository, result,
        captured_from={"controller": "delivery", "operation": "test.delivery.actual"})
    public_actual = full.assurance.pin(full.owner, project, {
        "kind": "actual_delivery_commit", "delivery": delivery,
        "binding_digest": saved_row["digest"], "snapshot_digest": body["snapshot"]["digest"],
        "repository": repository, "commit": result["commit"], "tree": result["tree"],
    })
    assert public_actual["ref"]["kind"] == "actual_delivery_commit"
    resolved = full.assurance.resolve_pinned(full.owner, actual_ref)
    payload = resolved["resolution"]["payload"]
    assert payload["commit"] == result["commit"]
    assert payload["tree"] == result["tree"]
    assert payload["object_format"] in {"sha1", "sha256"}
    assert payload["delivery_snapshot_ref"]["kind"] == "delivery_snapshot"
    assert set(actual_ref) == {"kind", "project", "delivery", "repository", "object_format", "commit", "tree", "pin"}
    validate_assurance_rows(full.assurance.archive_rows(project), project)
    closure = full.assurance.cas_closure(project)
    assert payload["object_manifest_blob"] in closure and payload["commit_object_blob"] in closure

    wrong_project = dict(actual_ref, project=full.k.create_project(full.owner, "other")['id'])
    with pytest.raises(Fault):
        full.assurance.resolve_pinned(full.owner, wrong_project)

    original_commit = body["snapshot"]["repos"][repository]["head"]
    if original_commit and original_commit != result["commit"]:
        original_tree = git(Path(result["git_dir"]), "rev-parse", f"{original_commit}^{{tree}}").stdout.decode().strip()
        wrong_commit = dict(actual_ref, commit=original_commit, tree=original_tree)
        with pytest.raises(Fault):
            full.assurance.resolve_pinned(full.owner, wrong_commit)

    changed_snapshot = copy.deepcopy(body["snapshot"])
    path = next(iter(changed_snapshot["repos"][repository]["files"]))
    entry = changed_snapshot["repos"][repository]["files"][path]
    if entry["kind"] == "file":
        changed = b"different snapshot\n"
        changed_entry = {**entry, "blob": full.s.blob_put(changed), "size": len(changed)}
        changed_snapshot["repos"][repository]["files"][path] = changed_entry
        changed_snapshot["repos"][repository]["bytes"] += len(changed) - entry["size"]
        changed_snapshot["digest"] = digest({key: value for key, value in changed_snapshot.items() if key != "digest"})
        mixed = copy.deepcopy(actual_ref)
        mixed["delivery"]["snapshot_digest"] = changed_snapshot["digest"]
        with pytest.raises(Fault):
            full.assurance.resolve_pinned(full.owner, mixed)

    # Historical pin resolution uses the immutable material.  A later live
    # binding mutation makes currentness stale without replacing that history.
    mutated = copy.deepcopy(updated)
    mutated["binding"]["synthetic_mutation"] = "later"
    full.s.execute("UPDATE deliveries SET body=? WHERE id=?", (canonical(mutated).decode(), delivery))
    assert full.assurance.resolve_pinned(full.owner, actual_ref)["resolution"]["payload"]["commit"] == result["commit"]
    assert full.assurance.evaluate_current(full.owner, actual_ref)["current"]["state"] == "stale"

    material = full.assurance.object_get(full.owner, project, actual_ref["pin"]["id"])
    payload_blob = material["body"]["payload_blob"]
    payload = json.loads(full.s.blob_get(payload_blob))
    manifest_blob = payload["object_manifest_blob"]
    manifest_path = full.s.blob_path(manifest_blob)
    manifest_path.unlink()
    with pytest.raises(Fault) as missing:
        full.assurance.resolve_pinned(full.owner, actual_ref)
    assert missing.value.code in {"missing_evidence", "integrity_error"}
