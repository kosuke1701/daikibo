"""Finite Unit 3 profile/registry propagation checks.

These tests exercise the existing controller and Runtime material path.  The
stage evaluator remains a read-only consumer: relation objects are prepared
before the snapshot, and every evaluation is compared against the project
state digest.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from daikibo.assurance_relations import REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST
from daikibo.common import Fault

from test_consumer_p_artifact_provenance import _artifact_ref as _runtime_artifact_ref
from test_consumer_p_mr_integration import (
    _actual_review_adapter,
    _breakdown_for_collected_task,
    _task_ref,
)
from test_e3_selection_contract import _adopt, _artifact_ref, _profile_body, _register_fixture_review
from test_e3_unit2b_node_reviews import _review_adapter
from test_unit3_stage_evaluator import _state_digest


def _stage_flow(full, full_project, tmp_path, *, profile_format: str,
                relation_ready: bool = True, node_ready: bool = True,
                v3_task_output: bool = False) -> dict:
    """Prepare one actual Runtime P/M/R fixture for the stage reader."""
    if v3_task_output and profile_format not in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"}:
        raise ValueError("v3_task_output needs the v3 relation registry")
    project, repository, requirement, root = full_project
    Path(root, "test_calc.py").write_text(
        "def test_add():\n    assert 1 + 1 == 2\n",
    )
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    partition_proposal = full.traceability.propose(
        full.owner, project, kind="document", scope={"source": source},
    )
    full.traceability.extract(full.owner, partition_proposal["id"])
    program = full.p.begin(full.owner, project, source, compact=True)["program"]

    manifest = {"format": "daikibo.artifact-output.v1", "outputs": [{
        "declaration_id": "artifact-result", "kind": "finding",
        "body": {"title": "Observed result", "statement": "The managed subprocess emitted the finding."},
    }]}
    task_body = {
        "title": "Selected registry Runtime task",
        "goal": "WRITE:" + json.dumps({"artifact-output.json": json.dumps(manifest, sort_keys=True)}),
        "read_artifacts": [requirement], "write_paths": ["artifact-output.json"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [], "workflow_id": program,
        "structural_obligations": {
            "format": "daikibo.task-structural-obligations.v1",
            "required_outputs": [{
                "id": "artifact-result", "statement": "one finding",
                "artifact_refs": [_runtime_artifact_ref(full, project, requirement)],
                "realization_kind": "artifact",
            }],
            "required_exercises": [],
        },
    }
    task = full.w.create(full.owner, project, task_body)
    full.w.plan_tests(full.owner, task["id"], {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    full.w.ready(full.owner, task["id"])
    claimed = full.w.claim(full.owner, project, task["id"])
    executed = full.rt.execute(full.owner, task["id"], "fixture")
    collected = full.invoke(full.owner, "task.artifacts_collect", {
        "task": task["id"], "expected_revision": claimed["revision"],
        "candidate": executed["candidate"], "repository": repository,
        "path": "artifact-output.json",
    })
    task_row = full.w.task(full.owner, task["id"])
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task["id"],), True)
    artifact = collected["artifacts"][0]["artifact"]
    artifact_ref = {
        "kind": "artifact", "project": project, "artifact": artifact["id"],
        "revision": artifact["revision"], "body_digest": artifact["digest"],
    }
    task_ref = _task_ref(full, project, task["id"])
    breakdown = _breakdown_for_collected_task(full, project, program, requirement, task["id"])

    requirement_row = full.s.one(
        "SELECT * FROM artifacts WHERE id=? AND project=?", (requirement, project), True,
    )
    requirement_ref = _artifact_ref(project, requirement_row)
    design_ref = None
    design_id = None
    if profile_format in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"}:
        design = full.k.propose(
            full.owner, project, "design",
            {"title": "Runtime design", "statement": "The Runtime design realizes the requirement.",
             "source_refs": [source]},
        )
        design = full.k.accept(full.owner, design["id"], 1)
        design_id = design["id"]
        design_ref = _artifact_ref(
            project, full.s.one("SELECT * FROM artifacts WHERE id=?", (design["id"],), True),
        )
    scope = full.assurance.scope_propose(
        full.owner, project,
        {**({"format":"assurance.scope.v2"} if profile_format=="assurance.profile.v5" else {}), "roots": [requirement_ref], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    relation_name = (
        "produced_by" if v3_task_output else
        "realizes" if profile_format in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"} else
        "produced_by"
    )
    relation_direction = "incoming"
    relation_centers = (
        ["assigned_tasks"] if v3_task_output or profile_format not in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"}
        else ["requirements"]
    )
    relation_set = {"relation": relation_name, "direction": relation_direction,
                    "centers": relation_centers}
    stages = {
        stage: {"denominator": denominator, "relation_sets": [relation_set],
                "node_rules": ["test-plan"], "execution_results": execution}
        for stage, denominator, execution in (
            ("plan", "program_plan", "none"),
            ("task", "assigned_task_contributors", "assigned_checks"),
            ("integration", "program_integration", "integration_checks"),
            ("delivery", "actual_delivery", "certified_integration_and_actual_outputs"),
        )
    }
    profile_body = _profile_body(project, program, scope, reason="Unit3 selected registry fixture")
    profile_body["stage_rules"] = stages
    profile_body["node_review_rules"] = [
        {"id": "test-plan", "selector": "test_plan", "roles": ["test_plan"]},
    ]
    if profile_format in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"}:
        profile_body["node_review_rules"].append(
            {"id": "design-node", "selector": "design", "roles": ["design"]},
        )
        profile_body["node_review_rules"].append(
            {"id": "requirement-node", "selector": "requirement", "roles": ["requirements"]},
        )
        profile_body["node_review_rules"] = sorted(
            profile_body["node_review_rules"], key=lambda item: item["id"],
        )
        for stage_rule in profile_body["stage_rules"].values():
            stage_rule["node_rules"] = sorted(
                [*stage_rule["node_rules"], "design-node", "requirement-node"],
            )
    profile_body["relation_selectors"] = [relation_name]
    registry_digest = REGISTRY_V1_DIGEST
    if profile_format in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"}:
        profile_body["format"] = profile_format
        profile_body["required_relation_contract_digest"] = REGISTRY_V2_DIGEST
        registry_digest = REGISTRY_V2_DIGEST
    if profile_format=="assurance.profile.v5":
        profile_body.update(required_scope_contract="assurance.scope.v2", required_node_contract="assurance.node-contract.v2")
    proposed = full.assurance.profile_propose(
        full.owner, project, program, profile_body, None,
    )
    _register_fixture_review(full)
    _adopt(full, project, proposed, None)

    review_adopt = _actual_review_adapter(full, tmp_path)
    relation_artifacts = {}
    if relation_ready:
        if profile_format in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"} and not v3_task_output:
            obligation_ids = [
                item["id"] for item in scope["obligations"]["body"]["obligations"]
            ]
            edge_source, edge_target = design_ref, requirement_ref
            claim = "The Runtime design realizes the requirement"
            center_ref = requirement_ref
        else:
            obligation_ids = [scope["obligations"]["body"]["obligations"][0]["id"]]
            edge_source, edge_target = artifact_ref, task_ref
            claim = "The actual Runtime artifact belongs to this Task output"
            center_ref = task_ref
        edge_body = {
            "source_ref": edge_source, "target_ref": edge_target,
            "relation": relation_name, "scope_ref": proposed["profile_ref"],
            "claim": claim, "obligation_ids": obligation_ids, "required_evidence_refs": [],
            "authority_refs": [],
        }
        if registry_digest == REGISTRY_V2_DIGEST:
            edge_body["relation_contract_digest"] = registry_digest
        edge = full.assurance.edge_propose(full.owner, project, edge_body)
        review_adopt(project, edge["edge"])
        set_body = {
            "center_ref": center_ref, "relation": relation_name, "direction": relation_direction,
            "scope_ref": proposed["profile_ref"], "criteria": {},
            "required_evidence_refs": [],
        }
        if registry_digest == REGISTRY_V2_DIGEST:
            set_body["relation_contract_digest"] = registry_digest
        relation_set_result = full.assurance.set_propose(full.owner, project, set_body)
        review_adopt(project, relation_set_result["set"])
        relation_artifacts = {"edge": edge, "set": relation_set_result}

    if node_ready:
        if design_id is not None:
            full.rt.review(full.owner, design_id, "design", "p-mr-markers")
            requirement_adapter = _review_adapter(full, tmp_path, "v3-requirement")
            full.rt.review(full.owner, requirement, "requirements", requirement_adapter)
        full.rt.review(
            full.owner, task["id"], "test_plan", "p-mr-markers",
            proposal=json.loads(plan_row["body"]),
        )
        full.rt.tests(full.owner, task["id"])

    return {
        "project": project, "program": program, "task": task["id"],
        "task_ref": task_ref, "breakdown": breakdown,
        "profile": proposed, "profile_format": profile_format,
        "registry_digest": registry_digest, "relations": relation_artifacts,
        "executed": executed, "collected": collected,
    }


@pytest.mark.parametrize(
    ("profile_format", "registry_digest"),
    [("assurance.profile.v2", REGISTRY_V1_DIGEST),
     ("assurance.profile.v3", REGISTRY_V2_DIGEST),
     ("assurance.profile.v4", REGISTRY_V2_DIGEST),
     ("assurance.profile.v5", REGISTRY_V2_DIGEST)],
)
def test_selected_profile_registry_reaches_read_only_plan_and_task(
    full, full_project, tmp_path, profile_format, registry_digest,
):
    flow = _stage_flow(full, full_project, tmp_path, profile_format=profile_format)
    selected = full.assurance.selected_profile(full.owner, flow["project"], flow["program"])
    assert selected["profile_format"] == profile_format
    assert selected["effective_relation_contract_digest"] == registry_digest
    before = _state_digest(full, flow["project"])

    plan = full.assurance.evaluate_stage(
        full.owner, flow["project"], flow["program"], "plan",
        proposed_breakdown=flow["breakdown"],
    )
    task = full.assurance.evaluate_stage(
        full.owner, flow["project"], flow["program"], "task",
        task=flow["task_ref"], checkpoint="complete",
        proposed_breakdown=flow["breakdown"],
    )
    if profile_format in {"assurance.profile.v4", "assurance.profile.v5"}:
        for checkpoint in ("ready", "recheck"):
            checked = full.assurance.evaluate_stage(
                full.owner, flow["project"], flow["program"], "task",
                task=flow["task_ref"], checkpoint=checkpoint,
                proposed_breakdown=flow["breakdown"],
            )
            assert checked["assurance_allow"] is True, checked
            assert checked["relations"]["registry_digest"] == REGISTRY_V2_DIGEST

    assert _state_digest(full, flow["project"]) == before
    # v2's produced_by relation consumes a Task-owned artifact output.  It is
    # deferred at the plan checkpoint and becomes current at Task complete;
    # the plan may be admitted with that explicit future obligation while
    # strong completion remains false.  v3's realizes relation remains a
    # fixed acceptance input at both reads.
    if profile_format == "assurance.profile.v2":
        assert plan["assurance_allow"] is True
        assert plan["strong_complete"] is False
        assert plan["relations"]["status"] == "deferred", plan["relations"]
        assert task["assurance_allow"] is True
        assert task["relations"]["status"] == "satisfied", task["relations"]
    else:
        for result in (plan, task):
            assert result["assurance_allow"] is True, {
                "status": result["status"], "failures": result["failures"],
                "nodes": result["nodes"], "relations": result["relations"],
                "execution": result["execution"], "unit_b": result["unit_b"],
            }
            assert result["relations"]["status"] == "satisfied", result["relations"]
    for result in (plan, task):
        assert result["relations"]["registry_digest"] == registry_digest
        assert result["capabilities"]["relation_consumer"]["registry_digest"] == registry_digest
        item = result["relations"]["items"][0]
        assert item["registry_digest"] == registry_digest
        assert item["request"]["registry_digest"] == registry_digest
        assert result["global_denominator"]["format"] == (
            "assurance.denominator.v3" if profile_format in {"assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"}
            else "assurance.denominator.v2"
        )


def test_v3_registry_dispatches_task_produced_by_to_required_output_population(
    full, full_project, tmp_path,
):
    """A v3 Task endpoint retains the v1 structural output denominator."""
    flow = _stage_flow(
        full, full_project, tmp_path, profile_format="assurance.profile.v3",
        v3_task_output=True,
    )
    before = _state_digest(full, flow["project"])
    plan = full.assurance.evaluate_stage(
        full.owner, flow["project"], flow["program"], "plan",
        proposed_breakdown=flow["breakdown"],
    )
    task = full.assurance.evaluate_stage(
        full.owner, flow["project"], flow["program"], "task",
        task=flow["task_ref"], checkpoint="complete",
        proposed_breakdown=flow["breakdown"],
    )
    assert _state_digest(full, flow["project"]) == before
    assert plan["assurance_allow"] is True
    assert plan["strong_complete"] is False
    assert plan["relations"]["status"] == "deferred"
    assert task["assurance_allow"] is True, task
    assert task["relations"]["status"] == "satisfied", task["relations"]
    item = task["relations"]["items"][0]
    assert item["request"]["capabilities"]["categories"] == ["required_output"]
    assert item["criteria"]["all_declared_outputs"]["status"] == "satisfied"


@pytest.mark.parametrize("bad_registry", ["0" * 64, None, [], True, "unknown"])
def test_selected_profile_registry_mismatch_is_unverified_and_read_only(
    full, full_project, tmp_path, monkeypatch, bad_registry,
):
    flow = _stage_flow(full, full_project, tmp_path, profile_format="assurance.profile.v3")
    original = full.assurance.selected_profile

    def selected_with_bad_registry(actor, project, program):
        value = copy.deepcopy(original(actor, project, program))
        value["effective_relation_contract_digest"] = bad_registry
        return value

    monkeypatch.setattr(full.assurance, "selected_profile", selected_with_bad_registry)
    before = _state_digest(full, flow["project"])
    result = full.assurance.evaluate_stage(
        full.owner, flow["project"], flow["program"], "plan",
        proposed_breakdown=flow["breakdown"],
    )
    assert _state_digest(full, flow["project"]) == before
    assert result["assurance_allow"] is False
    assert result["strong_complete"] is False
    assert any(item.get("code") == "invalid_registry" for item in result["failures"])


def test_selected_profile_stale_dependency_is_stale_and_read_only(
    full, full_project, tmp_path, monkeypatch,
):
    flow = _stage_flow(full, full_project, tmp_path, profile_format="assurance.profile.v3")
    original = full.assurance._ensure_object_current

    def stale_profile(actor, project, row, *, require_self=True, **kwargs):
        if row.get("kind") == "profile":
            raise Fault("stale_reference", "Selected profile dependency is stale")
        return original(actor, project, row, require_self=require_self, **kwargs)

    monkeypatch.setattr(full.assurance, "_ensure_object_current", stale_profile)
    before = _state_digest(full, flow["project"])
    result = full.assurance.evaluate_stage(
        full.owner, flow["project"], flow["program"], "plan",
        proposed_breakdown=flow["breakdown"],
    )
    assert _state_digest(full, flow["project"]) == before
    assert result["assurance_allow"] is False
    assert result["status"] == "stale"
    assert any(item.get("code") in {"stale_reference", "stale_set", "stale_evidence"}
               for item in result["failures"])


def test_v3_missing_nes_remains_blocking_and_read_only(full, full_project, tmp_path):
    flow = _stage_flow(
        full, full_project, tmp_path, profile_format="assurance.profile.v3",
        relation_ready=False, node_ready=False,
    )
    before = _state_digest(full, flow["project"])
    result = full.assurance.evaluate_stage(
        full.owner, flow["project"], flow["program"], "plan",
        proposed_breakdown=flow["breakdown"],
    )
    assert _state_digest(full, flow["project"]) == before
    assert result["assurance_allow"] is False
    assert result["strong_complete"] is False
    assert result["relations"]["status"] == "missing"
    assert result["nodes"]["status"] != "satisfied"
    assert any(item.get("code") == "relation_set_missing" for item in result["failures"])
