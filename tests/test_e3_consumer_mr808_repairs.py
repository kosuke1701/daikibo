"""MR808 typed center ownership regressions.

These cases use a real Workflow/Runtime candidate and the controller-derived
Breakdown assignment.  A valid candidate is only a center for the selected
scope after its retained Task revision is resolved and its canonical
assignment is found; a candidate or Task reference copied into the scope is
not an owner proof.
"""
from __future__ import annotations

import json

import pytest

from conftest import finish_task
from daikibo.assurance_additive import TASK_STRUCTURAL_FORMAT
from daikibo.assurance_criteria import (
    _center_matches_obligation,
    build_relation_request,
)
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_relations import REGISTRY_DIGEST
from daikibo.common import Fault, canonical, digest, timestamp
from test_assurance_candidate_observation import _candidate_ref


def _artifact_ref(full, project: str, artifact: str) -> dict:
    row = full.s.one("SELECT * FROM artifacts WHERE id=?", (artifact,), True)
    return {"kind": "artifact", "project": project, "artifact": artifact,
            "revision": row["revision"], "body_digest": row["digest"]}


def _task_with_output(full, project: str, repo: str, artifact_ref: dict, *, title: str) -> tuple[str, dict]:
    task = full.w.create(
        full.owner, project,
        {
            "title": title,
            "goal": "WRITE:" + json.dumps({"calc.py": "def add(a,b):\n    return a+b\n"}),
            "read_artifacts": [artifact_ref["artifact"]],
            "write_paths": ["calc.py"],
            "acceptance": ["AC-ADD"],
            "dependencies": [],
            "repos": [repo],
            "non_goals": [],
            "structural_obligations": {
                "format": TASK_STRUCTURAL_FORMAT,
                "required_outputs": [{
                    "id": "candidate-member", "statement": "Deliver implemented arithmetic",
                    "artifact_refs": [artifact_ref], "realization_kind": "candidate_member",
                }],
                "required_exercises": [],
            },
        },
    )["id"]
    full.w.plan_tests(full.owner, task, {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    full.w.ready(full.owner, task)
    finish_task(full, project, task)
    task_row = full.w.task(full.owner, task)
    return task, {
        "kind": "task_revision", "project": project, "task": task,
        "revision": task_row["revision"], "definition_digest": digest(task_row["body"]),
    }


def _case(full, full_project, *, extra_task: bool = False):
    project, repo, requirement, _ = full_project
    requirement_ref = _artifact_ref(full, project, requirement)
    task, task_ref = _task_with_output(
        full, project, repo, requirement_ref, title="Produce actual candidate A",
    )
    candidate = _candidate_ref(full, project, task)
    second_task = second_ref = second_candidate = None
    if extra_task:
        second_task, second_ref = _task_with_output(
            full, project, repo, requirement_ref, title="Produce unassigned candidate B",
        )
        second_candidate = _candidate_ref(full, project, second_task)

    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    body = {
        "format": "daikibo.breakdown.v1", "program": program, "title": "MR808 actual",
        "rationale": "typed center assignment fixture",
        "units": [{"id": "unit-a", "title": "arithmetic", "parent": None, "domain": None,
                   "rationale": "actual", "obligations": [{"requirement": requirement,
                   "acceptance": "AC-ADD"}], "tasks": [task], "interfaces": [],
                   "dependencies": []}],
        "scope": full.breakdowns._scope(full.owner, project),
        "structure": {}, "material_bindings": {},
    }
    breakdown = "BREAKDOWN-mr808-" + ("two" if extra_task else "one")
    full.s.execute(
        "INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)",
        (breakdown, program, project, canonical(body).decode(), digest(body), "proposed", None, timestamp()),
    )
    context = collect_stage_context(
        full, full.owner, project=project, program=program, stage="plan",
        proposed_breakdown=breakdown,
    )
    denominator = derive_denominator(context)
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [requirement_ref], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    return {
        "project": project, "repo": repo, "requirement": requirement,
        "requirement_ref": requirement_ref, "task": task, "task_ref": task_ref,
        "candidate": candidate, "second_task": second_task, "second_ref": second_ref,
        "second_candidate": second_candidate, "context": context,
        "denominator": denominator, "scope": scope,
    }


@pytest.mark.parametrize("boundary", ["request", "contributor", "task_incoming"])
def test_mr808_actual_candidate_and_task_centers_use_assignment(full, full_project, boundary):
    case = _case(full, full_project)
    project = case["project"]
    output_ids = [item["id"] for item in case["denominator"]["obligations"]
                  if item["category"] == "required_output"]
    assert len(output_ids) == 1
    if boundary == "contributor":
        obligation = next(item for item in case["denominator"]["obligations"]
                           if item["id"] == output_ids[0])
        assert _center_matches_obligation(
            full, full.owner, project, "produced_by", "outgoing",
            case["candidate"], obligation,
        ) is True
        return
    if boundary == "task_incoming":
        request = build_relation_request(
            full, full.owner, context=case["context"], denominator=case["denominator"],
            relation="assigned_to", center_ref=case["task_ref"], direction="incoming",
            scope_ref=case["scope"]["scope_ref"], registry_digest=REGISTRY_DIGEST,
        )
    else:
        request = build_relation_request(
            full, full.owner, context=case["context"], denominator=case["denominator"],
            relation="produced_by", center_ref=case["candidate"], direction="outgoing",
            scope_ref=case["scope"]["scope_ref"], registry_digest=REGISTRY_DIGEST,
        )
    if boundary == "request":
        assert request["required_obligation_ids"] == output_ids
    else:
        assert request["required_obligation_ids"]
    assert request["capabilities"]["center_current"] is True


def test_mr808_unassigned_real_candidate_cannot_use_structural_output(full, full_project):
    case = _case(full, full_project, extra_task=True)
    with pytest.raises(Fault) as failure:
        build_relation_request(
            full, full.owner, context=case["context"], denominator=case["denominator"],
            relation="produced_by", center_ref=case["second_candidate"], direction="outgoing",
            scope_ref=case["scope"]["scope_ref"], registry_digest=REGISTRY_DIGEST,
        )
    assert failure.value.code == "invalid_relation_request"


def test_mr808_assigned_foreign_task_is_outside_selected_scope(full, full_project):
    project, repo, requirement_a, _ = full_project
    source_b = full.k.source(full.owner, project, "Foreign requirement source")
    requirement_b = full.k.propose(
        full.owner, project, "requirement",
        {"title": "Foreign", "statement": "Foreign output", "acceptance": ["AC-FOREIGN"],
         "source_refs": [source_b["id"]]},
    )
    full.k.accept(full.owner, requirement_b["id"], 1)
    full.k.classify(full.owner, source_b["id"], 0, source_b["characters"],
                    "requirement", [requirement_b["id"]], "Foreign source")
    ref_a = _artifact_ref(full, project, requirement_a)
    ref_b = _artifact_ref(full, project, requirement_b["id"])
    task_a, _task_ref_a = _task_with_output(full, project, repo, ref_a, title="Scope A")
    task_b, task_ref_b = _task_with_output(full, project, repo, ref_b, title="Foreign B")
    candidate_b = _candidate_ref(full, project, task_b)
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    body = {
        "format": "daikibo.breakdown.v1", "program": program,
        "title": "MR808 foreign assignment", "rationale": "foreign scope fixture",
        "units": [
            {"id": "unit-a", "title": "A", "parent": None, "domain": None,
             "rationale": "A", "obligations": [{"requirement": requirement_a, "acceptance": "AC-ADD"}],
             "tasks": [task_a], "interfaces": [], "dependencies": []},
            {"id": "unit-b", "title": "B", "parent": None, "domain": None,
             "rationale": "B", "obligations": [{"requirement": requirement_b["id"], "acceptance": "AC-FOREIGN"}],
             "tasks": [task_b], "interfaces": [], "dependencies": []},
        ],
        "scope": full.breakdowns._scope(full.owner, project),
        "structure": {}, "material_bindings": {},
    }
    breakdown = "BREAKDOWN-mr808-foreign"
    full.s.execute(
        "INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)",
        (breakdown, program, project, canonical(body).decode(), digest(body), "proposed", None, timestamp()),
    )
    context = collect_stage_context(
        full, full.owner, project=project, program=program, stage="plan",
        proposed_breakdown=breakdown,
    )
    denominator = derive_denominator(context)
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [ref_a], "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []},
    )
    # The foreign Task and candidate are both real/current and assigned in the
    # canonical Breakdown, but their exact output anchor is outside this scope.
    assert task_ref_b["task"] == task_b
    with pytest.raises(Fault) as failure:
        build_relation_request(
            full, full.owner, context=context, denominator=denominator,
            relation="produced_by", center_ref=candidate_b, direction="outgoing",
            scope_ref=scope["scope_ref"], registry_digest=REGISTRY_DIGEST,
        )
    assert failure.value.code == "invalid_relation_request"


def test_mr808_foreign_project_and_forged_owner_are_rejected(full, full_project):
    case = _case(full, full_project, extra_task=True)
    foreign_project = full.k.create_project(full.owner, "MR808 foreign project")["id"]
    foreign_ref = {**case["candidate"], "project": foreign_project}
    with pytest.raises(Fault) as foreign:
        build_relation_request(
            full, full.owner, context=case["context"], denominator=case["denominator"],
            relation="produced_by", center_ref=foreign_ref, direction="outgoing",
            scope_ref=case["scope"]["scope_ref"], registry_digest=REGISTRY_DIGEST,
        )
    assert foreign.value.code == "cross_project"

    forged = {**case["candidate"], "task": case["second_task"]}
    with pytest.raises(Fault) as owner:
        build_relation_request(
            full, full.owner, context=case["context"], denominator=case["denominator"],
            relation="produced_by", center_ref=forged, direction="outgoing",
            scope_ref=case["scope"]["scope_ref"], registry_digest=REGISTRY_DIGEST,
        )
    assert owner.value.code in {"invalid_relation_request", "unresolved_reference", "stale_reference"}


def test_mr808_stale_candidate_definition_does_not_admit_request(full, full_project):
    case = _case(full, full_project)
    task = full.w.task(full.owner, case["task"])
    full.w.replan(full.owner, case["task"], task["revision"], "MR808 stale definition negative")
    with pytest.raises(Fault) as failure:
        build_relation_request(
            full, full.owner, context=case["context"], denominator=case["denominator"],
            relation="produced_by", center_ref=case["candidate"], direction="outgoing",
            scope_ref=case["scope"]["scope_ref"], registry_digest=REGISTRY_DIGEST,
        )
    assert failure.value.code in {"invalid_relation_request", "stale_reference", "unresolved_reference"}
