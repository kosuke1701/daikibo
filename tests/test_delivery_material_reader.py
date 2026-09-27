from __future__ import annotations

import copy
import json

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.delivery_material_reader import read_delivery_material, validate_delivery_material
from conftest import finish_task, make_task
from test_delivery_git_and_recovery import profile


def _prepared_two_repository_delivery(full, full_project, tmp_path, *, pin_repositories=None):
    project, first_repository, requirement, _root = full_project
    second_root = tmp_path / "second-repository"
    second_root.mkdir()
    (second_root / "secondary.py").write_text("value = 2\n")
    second_repository = full.sn.register(
        full.owner, project, "secondary", str(second_root),
    )["id"]
    task = make_task(full, full_project)
    delivery_profile = profile(project, first_repository, requirement, task)
    delivery_profile["repo_order"] = [first_repository, second_repository]
    full.d.configure(full.owner, project, delivery_profile)
    finish_task(full, project, task)
    delivery_id = full.d.prepare(full.owner, project)["id"]
    row, body = full.d.current(delivery_id)
    snapshot_ref, _ = full.rt.verification_materials.pin_delivery_snapshot(
        full.owner, project, row, body,
        captured_from={"controller": "delivery", "operation": "reader-test.snapshot"},
    )
    repositories = [first_repository, second_repository]
    updated = copy.deepcopy(body)
    results = {}
    for repository in repositories:
        results[repository] = full.sn.commit_snapshot(
            body["snapshot"], repository, "reader fixture",
        )
        updated["git"][repository] = results[repository]
    full.s.execute(
        "UPDATE deliveries SET body=? WHERE id=?",
        (canonical(updated).decode(), delivery_id),
    )
    row = full.s.one(
        "SELECT * FROM deliveries WHERE id=? AND project=?", (delivery_id, project), True,
    )
    pins = {}
    for repository in repositories:
        if pin_repositories is not None and repository not in pin_repositories:
            continue
        pins[repository], _ = full.rt.verification_materials.pin_actual_delivery_commit(
            full.owner, project, row, updated, repository, results[repository],
            snapshot_ref=snapshot_ref,
            captured_from={"controller": "delivery", "operation": "reader-test.actual"},
        )
    return {
        "project": project, "delivery": delivery_id, "row": row, "body": updated,
        "repositories": repositories, "snapshot": snapshot_ref, "actual": pins,
        "results": results,
    }


def _public_two_repository_delivery(full, full_project, tmp_path):
    """Build the two-repository public prepare/verify boundary.

    Validation mode intentionally cannot certify or commit a release.  This
    helper therefore stops at the public verify result and lets the boundary
    test assert that commit remains gated, instead of manufacturing a delivered
    row for a reader fixture.
    """
    project, first_repository, requirement, _root = full_project
    second_root = tmp_path / "public-second-repository"
    second_root.mkdir()
    (second_root / "secondary.py").write_text("value = 2\n")
    second_repository = full.sn.register(
        full.owner, project, "secondary", str(second_root),
    )["id"]
    task = make_task(full, full_project)
    delivery_profile = profile(project, first_repository, requirement, task)
    delivery_profile["repo_order"] = [first_repository, second_repository]
    full.d.configure(full.owner, project, delivery_profile)
    finish_task(full, project, task)
    delivery_id = full.d.prepare(full.owner, project)["id"]
    verification = full.d.verify(full.owner, delivery_id)
    return {
        "project": project, "delivery": delivery_id,
        "repositories": [first_repository, second_repository],
        "verification": verification,
    }


def _repository(material, repository):
    return next(item for item in material["repositories"] if item["repository"] == repository)


def test_profile_v2_plan_and_task_keep_the_legacy_selection_wire(full):
    """A v2 Delivery-less context must not acquire the v3 registry capability."""
    from test_e3_selection_contract import (
        _adopt, _fixture, _profile_body, _register_fixture_review,
    )

    project, _source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposal = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, proposal, None)

    plan_context = collect_stage_context(
        full, full.owner, project=project, program=program, stage="plan",
    )
    task = full.w.create(full.owner, project, {
        "title": "legacy v2 task", "goal": "retain the old wire",
        "read_artifacts": [requirement["id"]], "write_paths": [],
        "acceptance": ["AC-E3"], "dependencies": [], "repos": [],
        "non_goals": [],
    })
    task_ref = {
        "kind": "task_revision", "project": project, "task": task["id"],
        "revision": task["revision"], "definition_digest": digest(task["body"]),
    }
    task_context = collect_stage_context(
        full, full.owner, project=project, program=program, stage="task",
        task=task_ref,
    )

    for context in (plan_context, task_context):
        assert context["format"] == "assurance.stage-context.v2"
        selection = context["capabilities"]["selection"]
        assert selection["profile_format"] == "assurance.profile.v2"
        assert "effective_relation_contract_digest" not in selection
        denominator = derive_denominator(context)
        assert denominator["format"] == "assurance.denominator.v2"
        assert "effective_relation_contract_digest" not in denominator["capabilities"]["selection"]


def test_reader_resolves_two_repositories_and_restart_capture_pin(full, full_project, tmp_path):
    case = _prepared_two_repository_delivery(full, full_project, tmp_path)
    project = case["project"]
    snapshot = read_delivery_material(
        full, full.owner, project=project, delivery=case["snapshot"],
    )
    actual = read_delivery_material(
        full, full.owner, project=project,
        delivery=case["actual"][case["repositories"][0]],
    )
    assert snapshot["status"] == actual["status"] == "available"
    assert snapshot["delivery_id"] == actual["delivery_id"] == case["delivery"]
    assert [item["repository"] for item in snapshot["repositories"]] == sorted(case["repositories"])
    assert len(snapshot["actual_commit_refs"]) == 2
    assert {item["repository"] for item in snapshot["centers"]["actual_commits"]} == set(case["repositories"])
    assert snapshot["centers"]["delivery_snapshots"] == [snapshot["snapshot_ref"]]
    assert all(item["actual_ref"]["kind"] == "actual_delivery_commit" for item in snapshot["repositories"])
    assert actual["snapshot_payload"] == snapshot["snapshot_payload"]
    assert actual["check_refs"] == snapshot["check_refs"]
    assert actual["actual_commit_refs"] == snapshot["actual_commit_refs"]
    validate_delivery_material(snapshot, project=project)
    # A second capture after restart has a different nested snapshot pin but
    # is the same saved meaning.  The reader retains both exact candidates and
    # chooses deterministically by canonical reference order.
    repository = case["repositories"][0]
    second_ref, _ = full.rt.verification_materials.pin_actual_delivery_commit(
        full.owner, project, case["row"], case["body"], repository,
        case["results"][repository],
        captured_from={"controller": "delivery", "operation": "reader-test.restart"},
    )
    resumed = read_delivery_material(full, full.owner, project=project, delivery=second_ref)
    entry = _repository(resumed, repository)
    assert resumed["status"] == "available"
    assert len(entry["candidate_refs"]) == 2
    assert entry["actual_ref"] == min(entry["candidate_refs"], key=canonical)
    assert second_ref["delivery"]["pin"] != case["snapshot"]["pin"]


def test_reader_retains_full_population_when_one_saved_pin_is_missing(full, full_project, tmp_path):
    case = _prepared_two_repository_delivery(
        full, full_project, tmp_path, pin_repositories={full_project[1]},
    )
    before_delivery = full.s.one(
        "SELECT body,digest FROM deliveries WHERE id=?", (case["delivery"],), True,
    )
    before_material = full.s.all(
        "SELECT id,digest FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
        (case["project"],),
    )
    material = read_delivery_material(
        full, full.owner, project=case["project"], delivery=case["snapshot"],
    )
    missing_repository = case["repositories"][1]
    missing = _repository(material, missing_repository)
    assert material["status"] == "unresolved"
    assert missing["integrity_state"] == "missing_material"
    assert missing["actual_ref"] is None
    assert missing_repository in {item["repository"] for item in material["repositories"]}
    assert any(item["code"] == "delivery_actual_material_missing"
               for item in missing["diagnostics"])
    assert full.s.one(
        "SELECT body,digest FROM deliveries WHERE id=?", (case["delivery"],), True,
    ) == before_delivery
    assert full.s.all(
        "SELECT id,digest FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
        (case["project"],),
    ) == before_material


def test_reader_reports_corrupt_actual_cas_without_repair_or_population_shrink(full, full_project, tmp_path):
    case = _prepared_two_repository_delivery(full, full_project, tmp_path)
    corrupted_repository = case["repositories"][1]
    corrupt_ref = case["actual"][corrupted_repository]
    row = full.s.one("SELECT body FROM assurance_objects WHERE id=?", (corrupt_ref["pin"]["id"],), True)
    body = json.loads(row["body"])
    manifest = body["payload_blob"]
    full.s.blob_path(manifest).unlink()
    before_delivery = full.s.one(
        "SELECT body,digest FROM deliveries WHERE id=?", (case["delivery"],), True,
    )
    material = read_delivery_material(
        full, full.owner, project=case["project"], delivery=case["snapshot"],
    )
    corrupted = _repository(material, corrupted_repository)
    assert material["status"] == "unresolved"
    assert corrupted["actual_ref"] is None
    assert corrupted["integrity_state"] == "missing_material"
    assert any(item["code"] == "delivery_actual_material_invalid"
               for item in material["diagnostics"])
    assert full.s.one(
        "SELECT body,digest FROM deliveries WHERE id=?", (case["delivery"],), True,
    ) == before_delivery


def test_reader_context_uses_v4_versions_and_typed_centers(full, full_project, tmp_path):
    from test_e3_unit2b_delivery_criteria import _run_delivery_fixture

    fixture = _run_delivery_fixture(full, full_project, tmp_path)
    project = fixture["project"]
    snapshot_ref = fixture["first"]["snapshot"]
    delivery_id = snapshot_ref["delivery"]
    row, body = full.d.current(delivery_id)
    repository = next(iter(body["snapshot"]["repos"]))
    result = full.sn.commit_snapshot(body["snapshot"], repository, "reader context")
    updated = copy.deepcopy(body)
    updated["git"][repository] = result
    full.s.execute("UPDATE deliveries SET body=? WHERE id=?", (canonical(updated).decode(), delivery_id))
    row = full.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery_id, project), True)
    actual_ref, _ = full.rt.verification_materials.pin_actual_delivery_commit(
        full.owner, project, row, updated, repository, result,
        captured_from={"controller": "delivery", "operation": "reader-test.context"},
    )
    context = collect_stage_context(
        full, full.owner, project=project, program=fixture["program"], stage="delivery",
        proposed_breakdown=fixture["breakdown"], delivery=actual_ref,
    )
    assert context["format"] == "assurance.stage-context.v4"
    material = context["delivery_material"]
    assert material["payload"] == material["reader"]["snapshot_payload"]
    assert material["centers"]["delivery_snapshots"] == [material["snapshot_ref"]]
    assert all(ref["kind"] == "actual_delivery_commit"
               for ref in material["centers"]["actual_commits"])
    denominator = derive_denominator(context)
    assert denominator["format"] == "assurance.denominator.v4"
    assert denominator["derivation_version"] == "controller-denominator.v5"
    assert denominator["extractor_versions"]["delivery_material"] == "delivery-material.v1"
    assert denominator["extractor_versions"]["delivery_repository"] == "delivery-repository.v1"
    snapshot_context = collect_stage_context(
        full, full.owner, project=project, program=fixture["program"], stage="delivery",
        proposed_breakdown=fixture["breakdown"], delivery=snapshot_ref,
    )
    assert snapshot_context["format"] == "assurance.stage-context.v4"
    assert derive_denominator(snapshot_context)["input_digest"] == denominator["input_digest"]


def test_public_two_repository_verify_stays_separate_from_release_commit(
    full, full_project, tmp_path,
):
    case = _public_two_repository_delivery(full, full_project, tmp_path)
    assert all(item["passed"] for item in case["verification"]["results"]), case["verification"]
    row = full.s.one(
        "SELECT status,body FROM deliveries WHERE id=?", (case["delivery"],), True,
    )
    assert row["status"] == "prepared"
    before = row["body"]
    with pytest.raises(Fault) as exc:
        full.d.commit(full.owner, case["delivery"], "reader public boundary")
    assert exc.value.code == "release_gate_denied"
    after = full.s.one(
        "SELECT status,body FROM deliveries WHERE id=?", (case["delivery"],), True,
    )
    assert after["status"] == "prepared"
    assert after["body"] == before


def test_reader_failure_does_not_reenter_legacy_delivery_projection(
    full, full_project, tmp_path,
):
    from test_e3_unit2b_delivery_criteria import _run_delivery_fixture

    fixture = _run_delivery_fixture(full, full_project, tmp_path)
    snapshot_ref = fixture["first"]["snapshot"]
    row = full.s.one(
        "SELECT body FROM assurance_objects WHERE id=?",
        (snapshot_ref["pin"]["id"],), True,
    )
    envelope = json.loads(row["body"])
    full.s.blob_path(envelope["payload_blob"]).unlink()
    context = collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="integration", proposed_breakdown=fixture["breakdown"],
        delivery=snapshot_ref,
    )
    assert context["format"] == "assurance.stage-context.v4"
    material = context["delivery_material"]
    assert material["reader_context_v4"] is True
    assert material["payload"] is None
    assert material["check_refs"] == []
    assert any(item["code"] == "delivery_material_unresolved"
               for item in context["unresolved"])
