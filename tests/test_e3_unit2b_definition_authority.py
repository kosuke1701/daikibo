"""Unit 2b execution edges use the producer's exact definition identity."""
from __future__ import annotations

from conftest import make_task
from daikibo.assurance_criteria import _observed_definition_ref, evaluate_criteria
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_relations import REGISTRY_DIGEST
from daikibo.common import digest, parse_json
from test_delivery_git_and_recovery import finish_task, profile
from test_e3_unit2b_criteria_authority import _requirements, _review, _scope


def _observed_ref(project: str, receipt: dict) -> dict:
    return {
        "kind": "observed_result", "project": project,
        "receipt": receipt["id"], "run": receipt["run"],
        "receipt_digest": digest(receipt), "run_binding": receipt["binding"],
        "snapshot_digest": receipt["snapshot"],
        "result_digest": digest(receipt["result"]),
    }


def _edge(full, project: str, requirement: dict, scope: dict, source: dict,
          target: dict, obligation: dict, logical_id: str) -> dict:
    body = {
        "format": "assurance.edge.v1", "project": project,
        "source_ref": source, "target_ref": target,
        "relation": "execution_of", "relation_contract_digest": REGISTRY_DIGEST,
        "scope_ref": scope["scope_ref"], "claim": "material identity fixture",
        "obligation_ids": [obligation["id"]],
        "required_evidence_refs": [], "authority_refs": [],
    }
    return full.assurance.store_object(full.owner, project, "edge", logical_id, 1, body)


def _evaluate(full, project: str, program: str, requirement: dict,
              denominator: dict, edge: dict) -> dict:
    fixture = {"project": project, "parent": requirement}
    return evaluate_criteria(
        relation="execution_of", requirements=_requirements("execution_of"),
        denominator=denominator, edges=[edge],
        validated_reviews=_review(full, fixture),
    )


def test_actual_task_material_definition_covers_only_same_task(full, full_project):
    project, _repo, requirement_id, _root = full_project
    source = full.k.source(full.owner, project, "program source")
    program = full.p.begin(full.owner, project, source["id"], compact=True)["program"]
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    assert receipt["result"]["passed"] is True

    denominator = derive_denominator(collect_stage_context(
        full, full.owner, project=project, program=program, stage="plan",
    ))
    obligation = next(item for item in denominator["obligations"]
                      if item["category"] == "required_check"
                      and item["source_ref"]["plan"]["task"] == task)
    scope = _scope(full, {"project": project, "parent": full.k.artifact(full.owner, requirement_id)},
                   full.k.artifact(full.owner, requirement_id))
    edge = _edge(full, project, full.k.artifact(full.owner, requirement_id), scope,
                 _observed_ref(project, receipt), obligation["source_ref"],
                 obligation, "unit2b-definition-same-task")
    result = _evaluate(full, project, program, full.k.artifact(full.owner, requirement_id),
                       denominator, edge)
    checks = result["criteria"]["all_required_checks"]
    assert checks["status"] == "satisfied"
    assert checks["observed_ids"] == [obligation["id"]]


def test_actual_task_material_definition_cannot_cover_same_shape_foreign_task(full, full_project):
    project, _repo, requirement_id, _root = full_project
    source = full.k.source(full.owner, project, "program source")
    program = full.p.begin(full.owner, project, source["id"], compact=True)["program"]
    task_a = make_task(full, full_project)
    full.w.claim(full.owner, project, task_a)
    full.rt.execute(full.owner, task_a, "fixture")
    observed = full.rt.tests(full.owner, task_a)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    assert receipt["result"]["passed"] is True

    task_b = make_task(full, full_project)
    task_b_row = full.w.task(full.owner, task_b)
    task_b_plan = full.s.one("SELECT * FROM plans WHERE task=?", (task_b,), True)
    full.rt.verification_materials.pin_test_plan(
        full.owner, project, task_b_row, task_b_plan,
        captured_from={"controller": "runtime", "operation": "pin", "capture_id": "foreign"},
    )
    denominator = derive_denominator(collect_stage_context(
        full, full.owner, project=project, program=program, stage="plan",
    ))
    obligation = next(item for item in denominator["obligations"]
                      if item["category"] == "required_check"
                      and item["source_ref"]["plan"]["task"] == task_b)
    requirement = full.k.artifact(full.owner, requirement_id)
    scope = _scope(full, {"project": project, "parent": requirement}, requirement)
    edge = _edge(full, project, requirement, scope, _observed_ref(project, receipt),
                 obligation["source_ref"], obligation, "unit2b-definition-foreign-task")
    result = _evaluate(full, project, program, requirement, denominator, edge)
    assert obligation["id"] not in result["criteria"]["all_required_checks"]["observed_ids"]


def test_actual_delivery_material_retains_definition_and_adjusted_check_separately(full, full_project):
    project, repository, requirement, _root = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, profile(project, repository, requirement, task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]
    verification = full.d.verify(full.owner, delivery)
    item = verification["results"][0]
    observed = full.g.receipt(item["receipt"])
    source = _observed_ref(project, observed)
    definition_ref, runtime_check, reason = _observed_definition_ref(
        full, full.owner, source, observed,
    )
    assert reason is None
    assert definition_ref["kind"] == "delivery_check"
    assert definition_ref["check_id"] == observed["check_id"] == runtime_check["id"]
    assert digest(runtime_check) == observed["check_digest"]
    snapshot = parse_json(full.s.one(
        "SELECT body FROM deliveries WHERE id=?", (delivery,), True,
    )["body"])
    frozen = next(check for check in snapshot["checks"] if check["id"] == runtime_check["id"])
    assert definition_ref["check_digest"] == digest(frozen)
    assert digest(runtime_check) != definition_ref["check_digest"]
