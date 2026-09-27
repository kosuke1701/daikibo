import copy
import json
from pathlib import Path

import pytest

from daikibo.assurance_outputs import (resolve_output_artifact,
                                       validate_output_material,
                                       validate_output_reference)
from daikibo.assurance_relations import REGISTRY_V2_DIGEST, validate_relation
from daikibo.common import Fault, digest
from conftest import finish_task, make_task


def output_profile(project, repository, requirement, task):
    output = {"id": "artifact", "repo": repository, "path": ".daikibo-build/result.bin"}
    checks = [
        {"id": "producer", "category": "build", "repo": repository, "kind": "command",
         "argv": ["python", "-c", "from pathlib import Path; Path('.daikibo-build').mkdir(exist_ok=True); Path('.daikibo-build/result.bin').write_bytes(b'unit2c-output\\n')"],
         "purpose": "Produce a real non-Git Delivery output", "produces": ["artifact"]},
        {"id": "consumer", "category": "smoke", "repo": repository, "kind": "command",
         "argv": ["python", "-c", "from pathlib import Path; assert Path('.daikibo-build/result.bin').read_bytes() == b'unit2c-output\\n'"],
         "purpose": "Consume the producer output", "uses": ["artifact"]},
        {"id": "start", "category": "start", "repo": repository, "kind": "command",
         "argv": ["python", "-c", "print('ok')"], "purpose": "Start fixture"},
        {"id": "integration", "category": "integration", "repo": repository, "kind": "command",
         "argv": ["python", "-c", "print('ok')"], "purpose": "Integrate fixture"},
        {"id": "scenario", "category": "scenario", "repo": repository, "kind": "pytest",
         "argv": ["python", "-m", "pytest", "-q", "test_calc.py"], "required_tests": ["test_add"]},
    ]
    return {"target_environment": "CPython 3.13 isolated output adapter fixture",
            "required_requirements": [requirement], "required_tasks": [task],
            "repo_order": [repository], "rollback": "Restore the previous fixture snapshot",
            "applicability": {name: {"applicable": False, "reason": "Not applicable in fixture"}
                              for name in ("migration", "security", "performance", "contract")},
            "build_outputs": [output], "checks": checks}


def _output_ref(full, project, delivery, body, output_id="artifact"):
    snap_ref, _ = full.rt.verification_materials.pin_delivery_snapshot(
        full.owner, project, full.s.one("SELECT * FROM deliveries WHERE id=?", (delivery,), True), body,
        captured_from={"controller": "delivery", "operation": "test.output.snapshot"})
    check = next(item for item in body["checks"] if item["id"] == "producer")
    check_ref = full.rt.verification_materials.delivery_check_ref(project, snap_ref, check)
    result = next(item for item in body["results"] if item["check"] == "producer")
    receipt = full.g.receipt(result["receipt"])
    observed = {"kind": "observed_result", "project": project, "receipt": receipt["id"],
                "run": receipt["run"], "receipt_digest": digest(receipt),
                "run_binding": receipt["binding"], "snapshot_digest": receipt["snapshot"],
                "result_digest": digest(receipt["result"])}
    output = next(item for item in receipt["result"]["build_outputs"] if item["id"] == output_id)
    return {"kind": "output_artifact", "project": project, "delivery": snap_ref,
            "check": check_ref, "observed": observed, "output_id": output_id,
            "output_digest": digest(output)}


def test_real_delivery_output_pin_resolve_contains_archive_and_gc(full, full_project, tmp_path):
    project, repository, requirement, _root = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, output_profile(project, repository, requirement, task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]
    verified = full.d.verify(full.owner, delivery)
    assert all(item["passed"] for item in verified["results"]), [(item, full.g.receipt(item["receipt"]) if item.get("receipt") else None) for item in verified["results"]]
    row, body = full.d.current(delivery)
    producer_receipt = next(item["receipt"] for item in body["results"] if item["check"] == "producer")
    receipt = full.g.receipt(producer_receipt)
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    run_body = json.loads(run["body"])
    execution_pin = run_body["verification_material"]
    execution_material = full.assurance.object_get(full.owner, project, execution_pin["id"])
    execution_payload = json.loads(full.s.blob_get(execution_material["body"]["payload_blob"]))
    snapshot_ref = execution_payload["definition_ref"]["delivery"]
    pinned = full.assurance.pin(full.owner, project, {
        "kind": "output_artifact", "delivery": snapshot_ref,
        "check_id": "producer", "receipt": producer_receipt,
        "output_id": "artifact"})
    ref = pinned["ref"]
    assert set(ref) == {"kind", "project", "delivery", "check", "observed", "output_id", "output_digest"}
    assert ref["output_digest"] != full.g.receipt(producer_receipt)["result"]["build_outputs"][0]["blob"]
    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["resolution"]["content"]["id"] == "artifact"
    assert resolved["resolution"]["membership"][0]["relation"] == "delivery_build_output"
    catalog = full.assurance.catalog(full.owner, contract_digest=REGISTRY_V2_DIGEST)
    assert catalog["registry_digest"] == REGISTRY_V2_DIGEST and catalog["version"] == 2
    assert validate_relation("produced_by", ref, ref["check"], project=project,
                             contract_digest=REGISTRY_V2_DIGEST)[0]["kind"] == "output_artifact"
    assert validate_relation("contains", ref["delivery"], ref, project=project,
                             contract_digest=REGISTRY_V2_DIGEST)[1]["kind"] == "output_artifact"
    with pytest.raises(Fault):
        validate_relation("produced_by", ref, {
            "kind": "task_revision", "project": project, "task": task,
            "revision": 1, "definition_digest": "0" * 64}, project=project,
                         contract_digest=REGISTRY_V2_DIGEST)
    assert full.assurance.contains(full.owner, project, ref["delivery"], ref)["contains"] is True

    # The public edge/set APIs dispatch on the explicit v2 registry digest;
    # their endpoint resolvers still use the same retained output material.
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [], "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []})
    profile = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope["scope_ref"],
         "stage_rules": {"plan": {}, "delivery": {}},
         "relation_selectors": ["produced_by", "contains"],
         "test_definition_bindings": []})
    output_edge = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": ref, "target_ref": ref["check"], "relation": "produced_by",
         "relation_contract_digest": REGISTRY_V2_DIGEST,
         "scope_ref": profile["profile_ref"], "claim": "Producer check emits this output.",
         "obligation_ids": [], "required_evidence_refs": [], "authority_refs": []})
    assert output_edge["edge"]["body"]["relation_contract_digest"] == REGISTRY_V2_DIGEST
    membership_edge = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": ref["delivery"], "target_ref": ref, "relation": "contains",
         "relation_contract_digest": REGISTRY_V2_DIGEST,
         "scope_ref": profile["profile_ref"], "claim": "Delivery bundles this output.",
         "obligation_ids": [], "required_evidence_refs": [], "authority_refs": []})
    assert membership_edge["edge"]["body"]["relation_contract_digest"] == REGISTRY_V2_DIGEST
    output_set = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": ref, "relation": "produced_by", "direction": "outgoing",
         "relation_contract_digest": REGISTRY_V2_DIGEST,
         "scope_ref": profile["profile_ref"], "criteria": {},
         "required_evidence_refs": []})
    assert output_set["set"]["body"]["relation_contract_digest"] == REGISTRY_V2_DIGEST
    manifest_payload = json.loads(full.s.blob_get(output_set["manifest"]["body"]["payload_blob"]))
    assert manifest_payload["count"] == 1
    delivery_set = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": ref["delivery"], "relation": "produced_by", "direction": "outgoing",
         "relation_contract_digest": REGISTRY_V2_DIGEST,
         "scope_ref": profile["profile_ref"], "criteria": {},
         "required_evidence_refs": []})
    delivery_manifest = json.loads(full.s.blob_get(delivery_set["manifest"]["body"]["payload_blob"]))
    assert delivery_manifest["count"] == 1

    repinned_snapshot, _ = full.rt.verification_materials.pin_delivery_snapshot(
        full.owner, project, row, body,
        captured_from={"controller": "delivery", "operation": "test.unit2c.output.repin"})
    assert full.assurance.contains(full.owner, project, repinned_snapshot, ref)["contains"] is True

    # A real Git commit is a separate Delivery bundle member.  The output
    # remains a non-Git delivery_build_output even when the same Delivery also
    # has an actual Git commit pin.
    commit_result = full.sn.commit_snapshot(body["snapshot"], repository, "unit2c output actual commit")
    committed_body = copy.deepcopy(body)
    committed_body["git"][repository] = commit_result
    full.s.execute("UPDATE deliveries SET body=? WHERE id=?",
                   (json.dumps(committed_body, sort_keys=True, separators=(",", ":")), delivery))
    committed_row = full.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery, project), True)
    actual_ref, _ = full.rt.verification_materials.pin_actual_delivery_commit(
        full.owner, project, committed_row, committed_body, repository, commit_result,
        captured_from={"controller": "delivery", "operation": "test.unit2c.output.actual"})
    actual_membership = full.assurance.contains(full.owner, project, actual_ref, ref)
    assert actual_membership["contains"] is True, actual_membership
    assert actual_membership["membership"] == "delivery_build_output"
    assert actual_membership["git_tree_inclusion"] is False

    closure = full.assurance.cas_closure(project)
    assert resolved["resolution"]["content"]["blob"] in closure
    gc = full.ops.garbage_collect(full.owner, dry_run=True)
    assert resolved["resolution"]["content"]["blob"] not in {item["blob"] for item in gc["candidates"]}

    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    assert full.history.inspect_archive(full.owner, exported["path"], exported["sha256"])["verified"]

    # Historical output material survives a later Delivery row mutation and is
    # reported as stale rather than being silently replaced.
    changed = copy.deepcopy(body)
    changed["binding"]["later"] = True
    full.s.execute("UPDATE deliveries SET body=? WHERE id=?", (json.dumps(changed, sort_keys=True, separators=(",", ":")), delivery))
    historical = full.assurance.resolve_pinned(full.owner, ref)
    assert historical["resolution"]["content"]["id"] == "artifact"
    assert full.assurance.evaluate_current(full.owner, ref)["current"]["state"] in {"stale", "unknown"}


def test_output_pure_resolver_rejects_missing_and_corrupt_cas(full, full_project):
    project, repository, requirement, _root = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, output_profile(project, repository, requirement, task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]
    verified = full.d.verify(full.owner, delivery)
    row, body = full.d.current(delivery)
    producer_receipt = next(item["receipt"] for item in body["results"] if item["check"] == "producer")
    receipt = full.g.receipt(producer_receipt)
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    execution_material = full.assurance.object_get(
        full.owner, project, json.loads(run["body"])["verification_material"]["id"])
    execution_payload = json.loads(full.s.blob_get(execution_material["body"]["payload_blob"]))
    snapshot_ref = execution_payload["definition_ref"]["delivery"]
    pinned = full.assurance.pin(full.owner, project, {
        "kind": "output_artifact", "delivery": snapshot_ref,
        "check_id": "producer", "receipt": producer_receipt,
        "output_id": "artifact"})
    ref = pinned["ref"]
    material = next(item for item in full.assurance.object_list(full.owner, project, kind="material")["objects"]
                    if item["body"].get("material_kind") == "delivery_output")
    payload = json.loads(full.s.blob_get(material["body"]["payload_blob"]))
    resolver = lambda nested: full.assurance._resolve_locator(full.owner, nested, current=False)
    with pytest.raises(Fault):
        resolve_output_artifact(
            ref, resolve_ref=resolver, load_blob={},
            load_delivery_record=lambda _ref: payload)
    corrupt = copy.deepcopy(payload)
    corrupt["output"]["bytes"] += 1
    with pytest.raises(Fault):
        validate_output_material(ref, output_payload=corrupt,
                                 resolve_ref=resolver, load_blob=full.s)


def test_output_material_rejects_foreign_failure_and_nested_identity(full, full_project):
    project, repository, requirement, _root = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, output_profile(project, repository, requirement, task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]
    verified = full.d.verify(full.owner, delivery)
    assert all(item["passed"] for item in verified["results"]), verified
    _row, body = full.d.current(delivery)
    producer_receipt = next(item["receipt"] for item in body["results"] if item["check"] == "producer")
    receipt = full.g.receipt(producer_receipt)
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    execution = full.assurance.object_get(
        full.owner, project, json.loads(run["body"])["verification_material"]["id"])
    execution_payload = json.loads(full.s.blob_get(execution["body"]["payload_blob"]))
    snapshot_ref = execution_payload["definition_ref"]["delivery"]
    pinned = full.assurance.pin(full.owner, project, {
        "kind": "output_artifact", "delivery": snapshot_ref,
        "check_id": "producer", "receipt": producer_receipt, "output_id": "artifact"})
    ref = pinned["ref"]
    material = next(item for item in full.assurance.object_list(
        full.owner, project, kind="material")["objects"]
                    if item["body"].get("material_kind") == "delivery_output")
    payload = json.loads(full.s.blob_get(material["body"]["payload_blob"]))
    resolver = lambda nested: full.assurance._resolve_locator(full.owner, nested, current=False)

    cases = []
    foreign = copy.deepcopy(payload)
    foreign["output"]["repo"] = "foreign-repository"
    cases.append(("foreign-repository", foreign))
    failed = copy.deepcopy(payload)
    failed["delivery_result"]["passed"] = False
    cases.append(("failed-delivery-result", failed))
    other_delivery = copy.deepcopy(payload)
    other_delivery["delivery_ref"]["binding_digest"] = "0" * 64
    cases.append(("foreign-delivery-binding", other_delivery))
    wrong_check = copy.deepcopy(payload)
    wrong_check["check_ref"]["check_digest"] = "0" * 64
    cases.append(("foreign-check-identity", wrong_check))
    for label, candidate in cases:
        with pytest.raises(Fault) as rejected:
            validate_output_material(ref, output_payload=candidate,
                                     resolve_ref=resolver, load_blob=full.s)
        assert rejected.value.code in {"invalid_reference", "integrity_error", "failed_output", "missing_evidence",
                                      "unresolved_reference", "stale_reference"}, label

    with pytest.raises(Fault) as missing:
        resolve_output_artifact(ref, resolve_ref=resolver, load_blob=full.s,
                                load_delivery_record=lambda _ref: None)
    assert missing.value.code == "missing_evidence"


def test_output_material_rejects_coherent_wrong_output_id(full, full_project):
    project, repository, requirement, _root = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, output_profile(project, repository, requirement, task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]
    verified = full.d.verify(full.owner, delivery)
    assert all(item["passed"] for item in verified["results"]), verified
    _row, body = full.d.current(delivery)
    producer_receipt = next(item["receipt"] for item in body["results"] if item["check"] == "producer")
    receipt = full.g.receipt(producer_receipt)
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    execution = full.assurance.object_get(
        full.owner, project, json.loads(run["body"])["verification_material"]["id"])
    execution_payload = json.loads(full.s.blob_get(execution["body"]["payload_blob"]))
    ref = full.assurance.pin(full.owner, project, {
        "kind": "output_artifact", "delivery": execution_payload["definition_ref"]["delivery"],
        "check_id": "producer", "receipt": producer_receipt, "output_id": "artifact"})["ref"]
    material = next(item for item in full.assurance.object_list(
        full.owner, project, kind="material")["objects"]
                    if item["body"].get("material_kind") == "delivery_output")
    payload = json.loads(full.s.blob_get(material["body"]["payload_blob"]))
    wrong_ref = copy.deepcopy(ref)
    wrong_ref["output_id"] = "nonexistent-output"
    wrong_payload = copy.deepcopy(payload)
    wrong_payload["output_identity_digest"] = digest(wrong_ref)
    resolver = lambda nested: full.assurance._resolve_locator(full.owner, nested, current=False)
    with pytest.raises(Fault) as rejected:
        validate_output_material(wrong_ref, output_payload=wrong_payload,
                                 resolve_ref=resolver, load_blob=full.s)
    assert rejected.value.code == "integrity_error"
    full.assurance.store_material(
        full.owner, project, "delivery_output", wrong_payload,
        [wrong_ref["delivery"], wrong_ref["check"], wrong_ref["observed"]],
        {"test": "coherent wrong output identity"},
        {"test": "independent negative"},
    )
    with pytest.raises(Fault) as public_rejected:
        full.assurance.resolve_pinned(full.owner, wrong_ref)
    assert public_rejected.value.code == "integrity_error"


def test_delivery_center_output_membership_false_paths(full, full_project):
    project, repository, requirement, _root = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, output_profile(project, repository, requirement, task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]
    verified = full.d.verify(full.owner, delivery)
    assert all(item["passed"] for item in verified["results"]), verified
    _row, body = full.d.current(delivery)
    producer_receipt = next(item["receipt"] for item in body["results"] if item["check"] == "producer")
    receipt = full.g.receipt(producer_receipt)
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    execution = full.assurance.object_get(
        full.owner, project, json.loads(run["body"])["verification_material"]["id"])
    execution_payload = json.loads(full.s.blob_get(execution["body"]["payload_blob"]))
    ref = full.assurance.pin(full.owner, project, {
        "kind": "output_artifact", "delivery": execution_payload["definition_ref"]["delivery"],
        "check_id": "producer", "receipt": producer_receipt, "output_id": "artifact"})["ref"]

    second_delivery = full.d.prepare(full.owner, project)["id"]
    second_row, second_body = full.d.current(second_delivery)
    second_snapshot, _ = full.rt.verification_materials.pin_delivery_snapshot(
        full.owner, project, second_row, second_body,
        captured_from={"controller": "delivery", "operation": "test.unit2c.output.false-center"})
    assert second_snapshot["delivery"] != ref["delivery"]["delivery"]
    public_false = full.assurance.contains(full.owner, project, second_snapshot, ref)
    assert public_false["contains"] is False and public_false["state"] == "false"
    assert full.assurance._member_of(full.owner, project, second_snapshot, ref) is False

    malformed = copy.deepcopy(ref)
    malformed["output_id"] = "missing-output"
    assert full.assurance._member_of(full.owner, project, ref["delivery"], malformed) is False

    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [], "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []})
    profile = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope["scope_ref"], "stage_rules": {"plan": {}, "delivery": {}},
         "relation_selectors": ["produced_by"], "test_definition_bindings": []})
    full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": ref, "target_ref": ref["check"], "relation": "produced_by",
         "relation_contract_digest": REGISTRY_V2_DIGEST, "scope_ref": profile["profile_ref"],
         "claim": "Output belongs only to its producing Delivery.", "obligation_ids": [],
         "required_evidence_refs": [], "authority_refs": []})
    other_set = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": second_snapshot, "relation": "produced_by", "direction": "outgoing",
         "relation_contract_digest": REGISTRY_V2_DIGEST, "scope_ref": profile["profile_ref"],
         "criteria": {}, "required_evidence_refs": []})
    other_manifest = json.loads(full.s.blob_get(other_set["manifest"]["body"]["payload_blob"]))
    assert other_manifest["count"] == 0


@pytest.mark.parametrize("mutator", [
    lambda ref: {**ref, "extra": True},
    lambda ref: {**ref, "output_digest": "0" * 64},
])
def test_output_reference_rejects_shape_or_wrong_identity(full, full_project, mutator):
    project, repository, requirement, _root = full_project
    nested = {"kind": "delivery_snapshot", "project": project, "delivery": "D",
              "binding_digest": "0" * 64, "snapshot_digest": "0" * 64}
    check = {"kind": "delivery_check", "project": project, "delivery": nested,
             "check_id": "producer", "check_digest": "0" * 64}
    observed = {"kind": "observed_result", "project": project, "receipt": "R", "run": "RUN",
                "receipt_digest": "0" * 64, "run_binding": "B", "snapshot_digest": "0" * 64,
                "result_digest": "0" * 64}
    ref = {"kind": "output_artifact", "project": project, "delivery": nested, "check": check,
           "observed": observed, "output_id": "artifact", "output_digest": "0" * 64}
    with pytest.raises(Fault):
        validate_output_reference(mutator(ref), project=project)
