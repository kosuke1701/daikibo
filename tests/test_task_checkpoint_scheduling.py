"""Finite Unit 2/B checkpoint routing checks."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from daikibo.assurance_stage import (
    CHECKPOINT_RELATION_RULES,
    classify_relation_obligation,
)
from daikibo.assurance_denominators import (
    _validate_projection,
    collect_stage_context,
    derive_denominator,
    project_global_checkpoint,
)
from daikibo.assurance_relations import RELATION_REGISTRY
from daikibo.common import Fault

from test_assurance_candidate_observation import _candidate_ref
from test_consumer_p_mr_integration import (
    _actual_review_adapter,
    _task_ref,
)
from test_e3_selection_contract import (
    _adopt,
    _fixture,
    _profile_body,
    _register_fixture_review,
)


def test_checkpoint_table_covers_exactly_the_thirteen_registry_relations():
    assert set(CHECKPOINT_RELATION_RULES) == {
        entry["relation"] for entry in RELATION_REGISTRY
    }


@pytest.mark.parametrize(
    ("relation", "category"),
    [
        ("extracted_from", "source_span"),
        ("decomposes", "child_obligation"),
        ("realizes", "acceptance_condition"),
        ("implements", "artifact_responsibility"),
        ("verifies", "acceptance_condition"),
        ("exercises", "required_exercise"),
        ("assigned_to", "requirement"),
        ("affects", "impacted_target"),
    ],
)
def test_fixed_relation_obligations_are_current_without_observation_inference(relation, category):
    value = classify_relation_obligation(
        relation, stage="task", checkpoint="ready",
        obligation={"id": "obligation:fixed", "category": category},
        owner_refs=[{"kind": "artifact", "project": "p", "artifact": "a",
                     "revision": 1, "body_digest": "0" * 64}],
        context={"task_definitions": []},
    )
    assert value["classification"] == "required_now"


def test_task_artifact_output_is_future_before_candidate_completion():
    value = classify_relation_obligation(
        "produced_by", stage="task", checkpoint="ready",
        obligation={
            "id": "obligation:task-output",
            "category": "required_output",
            "pointer": "/tasks/TASK-fixed/structural_obligations/required_outputs/0",
        },
        owner_refs=[{"kind": "task_revision", "project": "p", "task": "TASK-fixed",
                     "revision": 1, "definition_digest": "0" * 64}],
        context={"task_definitions": [{
            "id": "TASK-fixed",
            "structural_obligations": {
                "required_outputs": [{"realization_kind": "artifact"}],
                "required_exercises": [],
            },
        }]},
    )
    assert value["classification"] == "deferred_future"
    assert value["first_required_checkpoint"] == "complete"
    assert value["producer_kind"] == "task_artifact"


def test_fixed_artifact_input_remains_current_when_owner_is_saved_artifact():
    value = classify_relation_obligation(
        "produced_by", stage="task", checkpoint="ready",
        obligation={
            "id": "obligation:fixed-input",
            "category": "required_output",
            "pointer": "/tasks/TASK-fixed/structural_obligations/required_outputs/0",
        },
        owner_refs=[{"kind": "artifact", "project": "p", "artifact": "input",
                     "revision": 1, "body_digest": "0" * 64}],
        context={"task_definitions": [{
            "id": "TASK-fixed",
            "structural_obligations": {
                "required_outputs": [{"realization_kind": "artifact"}],
                "required_exercises": [],
            },
        }]},
    )
    assert value["classification"] == "required_now"
    assert value["producer_kind"] == "fixed_artifact_input"


def test_unknown_checkpoint_is_unverified_instead_of_future():
    value = classify_relation_obligation(
        "assigned_to", stage="task", checkpoint="not-a-checkpoint",
        obligation={"id": "obligation:unknown", "category": "requirement"},
        owner_refs=[], context={"task_definitions": []},
    )
    assert value["classification"] == "unknown"


def test_global_checkpoint_projection_rejects_caller_partition(full):
    project, _source, _requirement, program, _scope = _fixture(full)
    context = collect_stage_context(
        full, full.owner, project=project, program=program, stage="plan",
    )
    denominator = derive_denominator(context)
    center_ref = context["artifacts"][0]["ref"]
    with pytest.raises(Fault):
        project_global_checkpoint(
            denominator, checkpoint="plan", relation="assigned_to", direction="incoming",
            center_ref=center_ref, population_ids=[], required_now_ids=[],
            deferred_future_ids=[], schedule=[],
        )


def test_task_candidate_output_is_deferred_by_sealed_projection(full, tmp_path):
    project, source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    body = _profile_body(project, program, scope)
    relation_set = {"relation": "produced_by", "direction": "incoming",
                    "centers": ["assigned_tasks"]}
    for stage_rule in body["stage_rules"].values():
        stage_rule["relation_sets"] = [copy.deepcopy(relation_set)]
    body["relation_selectors"] = ["produced_by"]
    proposed = full.assurance.profile_propose(full.owner, project, program, body, None)
    _adopt(full, project, proposed, None)
    domain = full.k.propose(
        full.owner, project, "domain", {
            "title": "Checkpoint domain", "statement": "Owns the checkpoint task",
            "responsibilities": ["checkpoint"], "non_responsibilities": [],
            "owned_data": [], "interfaces": [], "source_refs": [source["id"]],
        },
    )
    domain = full.k.accept(full.owner, domain["id"], 1)
    repository_root = tmp_path / "candidate-output-repo"
    repository_root.mkdir()
    repository = full.sn.register(full.owner, project, "candidate-output", str(repository_root))["id"]
    task = full.w.create(full.owner, project, {
        "title": "Checkpoint output",
        "goal": "WRITE:" + json.dumps({"result.txt": "candidate"}),
        "read_artifacts": [requirement["id"], domain["id"]], "write_paths": ["result.txt"],
        "acceptance": ["AC-E3"], "dependencies": [], "repos": [repository],
        "non_goals": [], "workflow_id": program,
        "structural_obligations": {
            "format": "daikibo.task-structural-obligations.v1",
            "required_outputs": [{
                "id": "candidate-output", "statement": "candidate output",
                "artifact_refs": [{
                    "kind": "artifact", "project": project,
                    "artifact": requirement["id"], "revision": 1,
                    "body_digest": full.s.one(
                        "SELECT digest FROM artifacts WHERE id=?", (requirement["id"],), True,
                    )["digest"],
                }], "realization_kind": "candidate_member",
            }],
            "required_exercises": [],
        },
    })
    full.w.plan_tests(full.owner, task["id"], {
        "checks": [{"id": "unit", "argv": [sys.executable, "-m", "pytest", "-q", "-k", "none"],
                    "kind": "pytest", "purpose": "checkpoint fixture",
                    "required_tests": ["none"]}],
    })
    full.rt.adapters.register(
        full.owner, "fixture", "fixture", sys.executable,
        [str(Path(__file__).with_name("fixture_agent.py"))],
    )
    task_ref = _task_ref(full, project, task["id"])
    full.w.ready(full.owner, task["id"])
    full.w.claim(full.owner, project, task["id"])
    full.rt.execute(full.owner, task["id"], "fixture")
    candidate_ref = _candidate_ref(full, project, task["id"])
    review_adopt = _actual_review_adapter(full, tmp_path)
    obligation_ids = [item["id"] for item in scope["obligations"]["body"]["obligations"]]
    edge = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": candidate_ref, "target_ref": task_ref,
         "relation": "produced_by", "scope_ref": proposed["profile_ref"],
         "claim": "the retained candidate is future output material",
         "obligation_ids": obligation_ids, "required_evidence_refs": [],
         "authority_refs": []},
    )
    review_adopt(project, edge["edge"])
    relation_set = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": task_ref, "relation": "produced_by", "direction": "incoming",
         "scope_ref": proposed["profile_ref"], "criteria": {},
         "required_evidence_refs": []},
    )
    review_adopt(project, relation_set["set"])
    breakdown = full.breakdowns.propose(
        full.owner, program, "Checkpoint task assignment", "future output checkpoint", [{
            "id": "unit-checkpoint", "title": "checkpoint", "parent": None,
            "domain": domain["id"],
            "rationale": "task assignment", "obligations": [
                {"requirement": requirement["id"], "acceptance": "AC-E3"},
            ], "tasks": [task["id"]], "interfaces": [], "dependencies": [],
        }],
    )["id"]
    result = full.assurance.evaluate_stage(
        full.owner, project, program, "task", task=task_ref, checkpoint="ready",
        proposed_breakdown=breakdown,
    )
    produced = [item for item in result["relations"]["items"]
                if item.get("relation") == "produced_by"]
    assert produced
    assert any(item.get("status") == "deferred" for item in produced)
    scheduled = [entry for item in produced for entry in item.get("schedule", {}).get("schedule", [])]
    assert scheduled
    assert all(entry["classification"] == "deferred_future" for entry in scheduled)
    assert result["strong_complete"] is False

    global_result = full.assurance.evaluate_stage(
        full.owner, project, program, "plan", checkpoint="plan",
        proposed_breakdown=breakdown,
    )
    global_produced = [item for item in global_result["relations"]["items"]
                       if item.get("relation") == "produced_by"]
    assert global_produced
    assert all(item.get("status") == "deferred" for item in global_produced)
    global_request = global_produced[0]["request"]
    assert global_request["local_projection"]["format"] == "assurance.stage-checkpoint-projection.v1"
    assert global_request["local_projection"]["required_now_ids"] == []
    assert global_request["local_projection"]["deferred_future_ids"]
