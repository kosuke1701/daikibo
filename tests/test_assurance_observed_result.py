from __future__ import annotations

import copy
import json
import sys
import zipfile
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.archive_chunks import file_digest
from daikibo.assurance import (_material_identity, validate_assurance_rows,
                               validate_execution_material_relation)
from daikibo.candidate_provenance import resolve_candidate_identity
from conftest import make_task
from test_edge_assurance_e2_repairs import _review_refs


def _observed_ref(project, receipt):
    return {
        "kind": "observed_result",
        "project": project,
        "receipt": receipt["id"],
        "run": receipt["run"],
        "receipt_digest": digest(receipt),
        "run_binding": receipt["binding"],
        "snapshot_digest": receipt["snapshot"],
        "result_digest": digest(receipt["result"]),
    }


def _validate_relation_copy(full, project, receipt, payload, material_body,
                            material_row, run, run_body):
    """Run the shared relation validator against an in-memory material copy."""
    copied_body = copy.deepcopy(material_body)
    copied_payload = copy.deepcopy(payload)
    copied_body["payload_blob"] = full.s.blob_put(canonical(copied_payload))
    copied_body["semantic_digest"] = digest(copied_payload)
    copied_body["dependency_refs"] = [copied_payload["definition_ref"]]
    if copied_payload.get("candidate_ref") is not None:
        copied_body["dependency_refs"].append(copied_payload["candidate_ref"])
    copied_body["dependency_refs"].extend(copied_payload.get("test_artifact_refs", []))
    copied_row = copy.deepcopy(material_row)
    copied_row["body"] = copied_body
    copied_row["digest"] = digest(copied_body)
    copied_receipt = copy.deepcopy(receipt)
    copied_pin = {"id": copied_row["id"], "digest": copied_row["digest"]}
    copied_receipt["verification_material"] = copied_pin
    copied_run_body = copy.deepcopy(run_body)
    copied_run_body["verification_material"] = copied_pin
    ref = _observed_ref(project, copied_receipt)
    return validate_execution_material_relation(
        project=project, ref=ref, run_row=run,
        run_body=copied_run_body, observed=copied_receipt,
        material_row=copied_row, material_body=copied_body,
        blob_store=full.s,
        resolve_definition=lambda definition: full.assurance._resolve_locator(
            full.owner, definition, current=False),
        resolve_candidate=lambda candidate: resolve_candidate_identity(
            _material_identity(candidate), full.assurance._candidate_context),
        resolve_artifact=lambda artifact: full.assurance._artifact_body(
            project, artifact),
    )


def _observed_material_parts(full, receipt):
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    run_body = json.loads(run["body"])
    pin = receipt["verification_material"]
    material_row = full.s.one("SELECT * FROM assurance_objects WHERE id=?",
                              (pin["id"],), True)
    material_body = json.loads(material_row["body"])
    payload = json.loads(full.s.blob_get(material_body["payload_blob"]))
    return run, run_body, material_row, material_body, payload


def test_observed_result_rejects_definition_from_another_task(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    receipt = full.g.receipt(full.rt.tests(full.owner, task)["checks"][0]["receipt"])
    run, run_body, material_row, material_body, payload = _observed_material_parts(full, receipt)

    other = make_task(full, full_project)
    other_task = full.w.task(full.owner, other)
    other_plan = full.s.one("SELECT digest FROM plans WHERE task=?", (other,), True)
    other_ref = full.assurance.pin(
        full.owner, project,
        {"kind": "test_plan", "task": other,
         "task_revision": other_task["revision"],
         "plan_digest": other_plan["digest"]},
    )["ref"]
    payload["definition_ref"] = {
        **payload["definition_ref"], "plan": other_ref,
    }
    with pytest.raises(Fault) as rejected:
        _validate_relation_copy(full, project, receipt, payload, material_body,
                                material_row, run, run_body)
    assert rejected.value.code == "integrity_error"


def test_observed_result_keeps_valid_historical_plan_subject_after_replan(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    ref = _observed_ref(project, receipt)
    for role in ("spec", "quality", "test_adequacy"):
        full.rt.review(full.owner, task, role, "fixture")
    full.w.complete(full.owner, task, full.w.task(full.owner, task)["revision"])
    before = full.w.task(full.owner, task)
    after = full.w.replan(full.owner, task, before["revision"],
                          "Retain the historical execution identity")
    assert after["revision"] == before["revision"] + 1
    full.w.plan_tests(full.owner, task, {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["resolution"]["current"] is False
    assert resolved["resolution"]["content"]["result"]["passed"] is True

    current_task = full.w.task(full.owner, task)
    current_plan = full.s.one("SELECT digest FROM plans WHERE task=?", (task,), True)
    current_plan_ref = full.assurance.pin(
        full.owner, project,
        {"kind": "test_plan", "task": task,
         "task_revision": current_task["revision"],
         "plan_digest": current_plan["digest"]},
    )["ref"]
    run, run_body, material_row, material_body, payload = _observed_material_parts(full, receipt)
    payload["definition_ref"] = {
        **payload["definition_ref"], "plan": current_plan_ref,
    }
    with pytest.raises(Fault) as rejected:
        _validate_relation_copy(full, project, receipt, payload, material_body,
                                material_row, run, run_body)
    assert rejected.value.code == "integrity_error"


def test_runtime_observed_result_resolves_material_and_failure_is_not_success(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    ref = _observed_ref(project, receipt)

    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["semantic_kind"] == "observed_result"
    assert resolved["resolution"]["payload"]["definition_ref"]["kind"] == "test_plan_check"
    assert resolved["resolution"]["material_pin"] == observed["verification_material"]
    assert resolved["resolution"]["current"] is True
    assert resolved["resolution"]["content"]["result"]["passed"] is True

    current = full.assurance.evaluate_current(full.owner, ref)
    assert current["current"]["state"] == "current"

    wrong_result = {**ref, "result_digest": "0" * 64}
    with pytest.raises(Fault) as mismatch:
        full.assurance.resolve_pinned(full.owner, wrong_result)
    assert mismatch.value.code == "integrity_error"

    # A failed command remains a valid historical observation, but the
    # resolver never converts it into a successful test result.
    snapshot = full.sn.capture(full.owner, project)
    failed, _, _ = full.rt.observe(
        project, None, "legacy-subject", "command", None, "legacy-binding", snapshot,
        lambda work, home, cwd: ([sys.executable, "-c", "raise SystemExit(3)"], None),
        run_id="RUN-observed-failure", check=None,
    )
    failed_ref = _observed_ref(project, failed)
    with pytest.raises(Fault) as legacy:
        full.assurance.resolve_pinned(full.owner, failed_ref)
    assert legacy.value.code == "legacy_unverified"


def test_old_run_without_material_is_unknown_and_not_retrofitted(full, full_project):
    project = full_project[0]
    snapshot = full.sn.capture(full.owner, project)
    observed, _, _ = full.rt.observe(
        project, None, "old-subject", "test:old", None, "old-binding", snapshot,
        lambda work, home, cwd: ([sys.executable, "-c", "pass"], None),
        run_id="RUN-observed-old", check=None,
    )
    ref = _observed_ref(project, observed)
    with pytest.raises(Fault) as unknown:
        full.assurance.resolve_pinned(full.owner, ref)
    assert unknown.value.code == "legacy_unverified"


def test_failed_materialized_check_is_historical_observation_not_success(full, full_project):
    project = full_project[0]
    task = make_task(
        full, full_project,
        goal="WRITE:" + json.dumps({"calc.py": "def add(a,b):\n    return a*b\n"}),
    )
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    assert receipt["result"]["passed"] is False
    resolved = full.assurance.resolve_pinned(full.owner, _observed_ref(project, receipt))
    assert resolved["resolution"]["current"] is True
    assert resolved["resolution"]["content"]["result"]["passed"] is False
    assert isinstance(resolved["resolution"]["content"]["failure"], dict)


def test_adopted_profile_derives_test_artifacts_into_runtime_material(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    task_row = full.s.one("SELECT * FROM tasks WHERE id=?", (task,), True)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    plan = full.rt.verification_materials.pin_test_plan(
        full.owner, project, task_row, plan_row,
        captured_from={"controller": "runtime", "operation": "test-artifact-fixture"},
    )[0]
    check = __import__("json").loads(plan_row["body"])["checks"][0]
    check_ref = full.rt.verification_materials.test_plan_check_ref(project, plan, check)

    source = full.k.source(full.owner, project, "Test artifact source")
    artifact = full.k.propose(
        full.owner, project, "test",
        {"kind": "test", "title": "Formal test", "statement": "Runs the unit check", "source_refs": [source["id"]]},
    )
    artifact = full.k.accept(full.owner, artifact["id"], 1)
    artifact_ref = {"kind": "artifact", "project": project, "artifact": artifact["id"],
                    "revision": artifact["revision"], "body_digest": artifact["digest"]}

    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [{"kind": "artifact", "project": project, "artifact": full_project[2],
                     "revision": 1,
                     "body_digest": full.s.one("SELECT digest FROM revisions WHERE artifact=? AND revision=1",
                                               (full_project[2],))["digest"]}],
         "selection_rules": {}, "exclusion_proposals": [], "authority_refs": [],
         "discovery_unknowns": []},
    )
    profile = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope["scope_ref"], "stage_rules": {"task": {}},
         "relation_selectors": [],
         "test_definition_bindings": [{"artifact_ref": artifact_ref, "check_ref": check_ref}]},
    )
    full.rt.adapters.register(
        full.owner, "assurance-fixture", "fixture", sys.executable,
        [str(Path(__file__).with_name("assurance_reviewer_fixture.py"))],
    )
    reviews = _review_refs(full, project, profile["profile"])
    full.assurance.adopt(full.owner, project, profile["profile"]["id"],
                         profile["profile"]["digest"], None, reviews)

    derived = full.rt.execution_test_artifact_refs(full.owner, project, check_ref)
    assert derived == [artifact_ref]

    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    material = full.assurance.resolve_pinned(full.owner, _observed_ref(project, receipt))["resolution"]["payload"]
    assert material["test_artifact_refs"] == [artifact_ref]


def test_canonical_test_artifact_kind_does_not_require_body_kind(full, full_project, monkeypatch):
    original = full.k.propose

    def propose(actor, project, kind, body, *args, **kwargs):
        if kind == "test":
            body = {key: value for key, value in body.items() if key != "kind"}
        return original(actor, project, kind, body, *args, **kwargs)

    monkeypatch.setattr(full.k, "propose", propose)
    test_adopted_profile_derives_test_artifacts_into_runtime_material(full, full_project)


def test_body_kind_cannot_override_canonical_requirement_kind(full, full_project, monkeypatch):
    original = full.k.propose

    def propose(actor, project, kind, body, *args, **kwargs):
        if kind == "test":
            kind = "requirement"
            body = {**body, "acceptance": ["AC-TYPE"]}
        return original(actor, project, kind, body, *args, **kwargs)

    monkeypatch.setattr(full.k, "propose", propose)
    with pytest.raises(Fault):
        test_adopted_profile_derives_test_artifacts_into_runtime_material(full, full_project)


def test_shared_archive_execution_material_relation_rejects_run_pin_swap(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    first = full.rt.tests(full.owner, task)["checks"][0]
    first_receipt = full.g.receipt(first["receipt"])
    full.rt.tests(full.owner, task)
    second_receipt = full.g.receipt(full.rt.tests(full.owner, task)["checks"][0]["receipt"])
    assert first_receipt["verification_material"] != second_receipt["verification_material"]
    ref = _observed_ref(project, first_receipt)
    full.assurance.store_object(
        full.owner, project, "profile", "archive-run-pin-swap", 1,
        {"format": "profile.v1", "project": project, "observed": ref},
    )
    tables = full.assurance.archive_rows(project)
    lookup = {
        name: {row["id"]: row for row in full.s.all("SELECT * FROM " + name)}
        for name in ("tasks", "candidates", "runs", "receipts", "repos",
                     "task_revision_history", "assurance_objects")
    }

    def external(section, key):
        return lookup.get(section, {}).get(key)
    external.context_rows = {
        section: list(values.values())
        for section, values in lookup.items()
        if section != "assurance_objects"
    }

    validate_assurance_rows(tables, project, external, full.s.blob_get)
    run = lookup["runs"][first_receipt["run"]]
    run_body = json.loads(run["body"])
    run_body["verification_material"] = second_receipt["verification_material"]
    run["body"] = canonical(run_body).decode()
    with pytest.raises(Fault) as rejected:
        validate_assurance_rows(tables, project, external, full.s.blob_get)
    assert rejected.value.code == "invalid_archive"


def test_observed_result_archive_requires_run_receipt_material_closure(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    ref = _observed_ref(project, receipt)
    full.assurance.store_object(
        full.owner, project, "profile", "archive-observed-result", 1,
        {"format": "profile.v1", "project": project, "observed": ref},
    )
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    assert full.history.inspect_archive(full.owner, exported["path"], exported["sha256"])["format"] == \
        "daikibo.knowledge-archive.v12"


def test_standalone_observed_archive_validation_requires_saved_context(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    full.assurance.store_object(
        full.owner, project, "profile", "archive-observed-context-required", 1,
        {"format": "profile.v1", "project": project,
         "observed": _observed_ref(project, receipt)},
    )
    with pytest.raises(Fault) as rejected:
        validate_assurance_rows(full.assurance.archive_rows(project), project)
    assert rejected.value.code == "invalid_archive"


def test_observed_result_archive_rejects_missing_material_child(full, full_project, tmp_path):
    project = full_project[0]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    material_pin = observed["verification_material"]
    material_row = full.s.one("SELECT body FROM assurance_objects WHERE id=?", (material_pin["id"],), True)
    material_body = json.loads(material_row["body"])
    payload_blob = material_body["payload_blob"]
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])

    damaged = tmp_path / "missing-material-child.dkarchive"
    with zipfile.ZipFile(exported["path"]) as source:
        members = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(members["snapshot.json"])
    record_stream = b"".join(
        members["objects/" + part["sha256"]]
        for part in manifest["records"]["chunks"]
    )
    material_record = next(
        json.loads(line)
        for line in record_stream.splitlines()
        if json.loads(line).get("section") == "assurance_objects"
        and json.loads(line)["row"]["id"] == material_pin["id"]
    )
    payload_descriptor = next(
        raw for raw in material_record["raw_objects"]
        if raw["sha256"] == payload_blob
    )
    # The archive stores each original CAS leaf as bounded chunk objects.
    # Remove one such chunk from the portable copy; the live store remains
    # untouched and inspection must reject the incomplete material closure.
    members.pop("objects/" + payload_descriptor["chunks"][0]["sha256"])
    with zipfile.ZipFile(damaged, "w") as target:
        for name, content in members.items():
            target.writestr(name, content)

    with pytest.raises(Fault) as rejected:
        full.history.inspect_archive(full.owner, damaged, file_digest(damaged))
    assert rejected.value.code == "invalid_archive"
