from __future__ import annotations

import json

import pytest

from test_e3_selection_contract import (
    _adopt,
    _fixture,
    _profile_body,
    _register_fixture_review,
)
from test_e3_replay_authority import _apply_withdrawal_change
from conftest import make_task
from daikibo.common import Fault, digest, parse_json


def _artifact_ref(project, row):
    return {"kind": "artifact", "project": project, "artifact": row["id"],
            "revision": row["revision"], "body_digest": row["digest"]}


def test_assurance_proposals_are_a_stable_supervisor_input(full):
    project, _source, _requirement, program, scope = _fixture(full)

    before = full.assurance.structural_progress_projection(project)
    before_state = full.supervisor.state_digest(project)
    before_progress = full.supervisor.progress_digest(project)
    body = _profile_body(project, program, scope)
    proposed = full.assurance.profile_propose(
        full.owner, project, program, body, None,
    )

    after = full.assurance.structural_progress_projection(project)
    assert after["format"] == "supervisor.assurance-structure.v1"
    profile = next(item for item in after["objects"] if item["kind"] == "profile")
    assert profile["logical_id"] == proposed["profile"]["logical_id"]
    assert profile["body"] == body
    assert full.supervisor.state_digest(project) != before_state
    assert full.supervisor.progress_digest(project) != before_progress
    assert after != before

    # _store_e2_object reuses the exact body digest.  Repeating the proposal
    # is therefore not a new structural input, even though the caller gets a
    # fresh response object.
    full.assurance.profile_propose(full.owner, project, program, body, None)
    assert full.assurance.structural_progress_projection(project) == after


def test_assurance_head_withdrawal_and_review_packets_keep_meaning_boundaries(full):
    project, source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposal = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )

    proposed = full.assurance.structural_progress_projection(project)
    proposed_state = full.supervisor.state_digest(project)
    reviewed = full.assurance.review_subject(full.owner, project, proposal["profile"]["id"])
    # Ordinary review packets are bookkeeping and must not enter the
    # structural projection.
    assert full.assurance.structural_progress_projection(project) == proposed
    assert full.supervisor.state_digest(project) == proposed_state
    assert reviewed["packets"]

    adopted = _adopt(full, project, proposal, None)
    with_head = full.assurance.structural_progress_projection(project)
    head = next(item for item in with_head["heads"]
                if item["logical_id"] == proposal["profile"]["logical_id"])
    assert head["event_kind"] == "adopt"
    assert head["subject"]["body"] == proposal["profile"]["body"]
    assert "id" not in head and "created" not in head and "previous" not in head
    assert head["body"] == parse_json(adopted["event"]["body"])

    withdrawal = full.assurance.withdraw_propose(
        full.owner, project, proposal["profile"]["id"],
        "replace profile after a reviewed structural change",
        [{"kind": "source", "project": project, "source": source["id"],
          "blob_digest": source["digest"]}],
        expected_head=adopted["event"]["id"],
    )
    final = full.assurance.structural_progress_projection(project)
    assert withdrawal["withdrawal"] in final["withdrawal_proposals"]
    assert all("partition" not in item for item in final["withdrawal_proposals"])


def test_assurance_edge_and_set_proposals_are_structural_inputs(full):
    project = full.k.create_project(full.owner, "structural edge set")['id']
    source = full.k.source(full.owner, project, "The design realizes the requirement.")
    requirement = full.k.propose(
        full.owner, project, "requirement",
        {"title": "Requirement", "statement": "The requirement is explicit.",
         "acceptance": ["AC-STRUCTURE"], "source_refs": [source["id"]]},
    )
    requirement = full.k.accept(full.owner, requirement["id"], 1)
    design = full.k.propose(
        full.owner, project, "design",
        {"title": "Design", "statement": "The design realizes the requirement.",
         "source_refs": [source["id"]]},
    )
    design = full.k.accept(full.owner, design["id"], 1)
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [_artifact_ref(project, requirement)], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    profile = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope["scope_ref"],
         "stage_rules": {"plan": {}, "task": {}, "integration": {}, "delivery": {}},
         "relation_selectors": ["realizes"], "test_definition_bindings": []},
    )
    before = full.assurance.structural_progress_projection(project)
    edge = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": _artifact_ref(project, design),
         "target_ref": _artifact_ref(project, requirement), "relation": "realizes",
         "scope_ref": profile["profile_ref"], "claim": "Design realizes the requirement.",
         "obligation_ids": [scope["obligations"]["body"]["obligations"][0]["id"]],
         "required_evidence_refs": [], "authority_refs": []},
    )
    with_edge = full.assurance.structural_progress_projection(project)
    assert with_edge != before
    assert any(item["kind"] == "edge" and item["body"] == edge["edge"]["body"]
               for item in with_edge["objects"])

    relation_set = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": _artifact_ref(project, design), "relation": "realizes",
         "direction": "outgoing", "scope_ref": profile["profile_ref"],
         "criteria": {}, "required_evidence_refs": []},
    )
    with_set = full.assurance.structural_progress_projection(project)
    assert with_set != with_edge
    assert any(item["kind"] == "set" and item["body"] == relation_set["set"]["body"]
               for item in with_set["objects"])
    full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": _artifact_ref(project, design),
         "target_ref": _artifact_ref(project, requirement), "relation": "realizes",
         "scope_ref": profile["profile_ref"], "claim": "Design realizes the requirement.",
         "obligation_ids": [scope["obligations"]["body"]["obligations"][0]["id"]],
         "required_evidence_refs": [], "authority_refs": []},
    )
    assert full.assurance.structural_progress_projection(project) == with_set


def test_historical_artifact_ref_survives_public_withdrawal_but_current_gate_stays_strict(full):
    project, source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    historical_ref = _artifact_ref(project, requirement)
    before = full.supervisor.state_digest(project)

    # This is the normal planning/change/decision/review/apply path.  It
    # advances the canonical artifact and marks the old requirement history
    # withdrawn; the assurance proposal still retains revision 1 exactly.
    _apply_withdrawal_change(full, project, source, requirement)

    after = full.supervisor.state_digest(project)
    assert after != before
    assert any(item["body"].get("roots") for item in
               full.assurance.structural_progress_projection(project)["objects"]
               if item["kind"] == "scope")
    historical = full.assurance.resolve_pinned(full.owner, historical_ref)
    assert historical["resolution"]["current"] is False

    withdrawn = full.k.artifact(full.owner, requirement["id"])
    historical_ac = {
        "kind": "traceability_ref", "project": project,
        "locator": {
            "ref_type": "artifact_ac", "artifact": requirement["id"],
            "revision": withdrawn["revision"], "body_digest": withdrawn["digest"],
            "ac_pointer": "/acceptance/0", "ac_id": "AC-E3",
            "ac_digest": digest("AC-E3"),
        },
    }
    ac_resolution = full.assurance.resolve_pinned(full.owner, historical_ac)
    assert ac_resolution["resolution"]["current"] is False

    current = full.assurance.evaluate_current(full.owner, historical_ref)
    assert current["current"] == {"state": "stale", "reasons": ["stale_reference"]}
    with pytest.raises(Fault) as strict:
        full.assurance._resolve_locator(full.owner, historical_ref, current=True)
    assert strict.value.code == "stale_reference"


def test_structural_projection_keeps_a_nonartifact_history_pin(full, full_project):
    project, _repository, requirement, _root = full_project
    task = make_task(full, full_project)
    task_row = full.s.one("SELECT * FROM tasks WHERE id=?", (task,), True)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    old_plan_ref, _old_pin = full.rt.verification_materials.pin_test_plan(
        full.owner, project, task_row, plan_row,
        captured_from={"controller": "runtime", "operation": "history-plan"},
    )
    check = parse_json(plan_row["body"])["checks"][0]
    check_ref = full.rt.verification_materials.test_plan_check_ref(project, old_plan_ref, check)

    source = full.k.source(full.owner, project, "The test artifact is retained as review evidence.")
    test_artifact = full.k.propose(
        full.owner, project, "test",
        {"kind": "test", "title": "Historical plan test",
         "statement": "Runs the retained plan check.", "source_refs": [source["id"]]},
    )
    test_artifact = full.k.accept(full.owner, test_artifact["id"], 1)
    requirement_ref = {"kind": "artifact", "project": project, "artifact": requirement,
                       "revision": 1,
                       "body_digest": full.s.one(
                           "SELECT digest FROM revisions WHERE artifact=? AND revision=1",
                           (requirement,), True)["digest"]}
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [requirement_ref], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    profile = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope["scope_ref"], "stage_rules": {"task": {}},
         "relation_selectors": [],
         "test_definition_bindings": [{
             "artifact_ref": _artifact_ref(project, test_artifact),
             "check_ref": check_ref,
         }]},
    )
    before = full.assurance.structural_progress_projection(project)
    assert any(item["logical_id"] == profile["profile"]["logical_id"] for item in before["objects"])

    # Rewrite the mutable plan through Workflow.plan_tests.  The retained
    # material pin is now historical, but its task/plan body and CAS remain a
    # valid typed structural input to the profile proposal.
    copy_plan = json.loads(plan_row["body"])
    copy_plan["rationale"] = "A later plan capture changes currentness only"
    full.w.plan_tests(full.owner, task, copy_plan)

    after = full.assurance.structural_progress_projection(project)
    assert any(item["logical_id"] == profile["profile"]["logical_id"] for item in after["objects"])
    resolved = full.assurance.resolve_pinned(full.owner, old_plan_ref)
    assert resolved["resolution"]["current"] is False


@pytest.mark.parametrize("artifact_id", ["ART-MISSING-HISTORY", "existing"])
def test_structural_projection_rejects_missing_or_changed_typed_artifact_ref(full, artifact_id):
    project, _source, requirement, _program, _scope = _fixture(full)
    target = requirement["id"] if artifact_id == "existing" else artifact_id
    body_digest = "0" * 64
    body = {
        "format": "assurance.scope.v1", "project": project,
        "roots": [{"kind": "artifact", "project": project, "artifact": target,
                    "revision": 1, "body_digest": body_digest}],
        "selection_rules": {}, "exclusion_proposals": [],
        "authority_refs": [], "discovery_unknowns": [],
    }
    # E1 storage accepts the immutable shape/index; the read projection must
    # then reject a missing endpoint or a body/digest mismatch without
    # treating either as an empty historical population.
    full.assurance.store_object(full.owner, project, "scope",
                                "negative-history-ref:" + artifact_id, 1, body)
    with pytest.raises(Fault) as rejected:
        full.assurance.structural_progress_projection(project)
    assert rejected.value.code in {"unresolved_reference", "integrity_error"}
