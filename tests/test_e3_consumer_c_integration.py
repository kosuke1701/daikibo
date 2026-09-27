from __future__ import annotations

import pytest

from daikibo.assurance import SET_UNIVERSAL_CRITERIA
from daikibo.assurance_criteria import build_relation_request, evaluate_criteria
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_node_reviews import build_node_requests, select_node_reviews
from daikibo.assurance_relations import REGISTRY_DIGEST, REGISTRY_V2_DIGEST, registry_entry
from daikibo.common import Fault
from conftest import finish_task
from test_e3_consumer_c_profile_v3 import (
    _prepare_reviewed_program,
    _replace_fixture_profile,
)
from test_e3_unit2b_delivery_criteria import _receipt_delivery_snapshot
from test_delivery_git_and_recovery import profile


def _case(full, full_project, tmp_path):
    fixture = _prepare_reviewed_program(full, full_project, tmp_path)
    project = fixture["project"]
    body = profile(project, fixture["repository"], fixture["requirement"], fixture["task"])
    body["build_outputs"] = [
        {"id": output_id, "repo": fixture["repository"], "path": f".daikibo-build/{output_id}"}
        for output_id in ("a", "b")
    ]
    body["checks"][0].update(
        argv=[
            "python", "-c",
            "from pathlib import Path; Path('.daikibo-build').mkdir(exist_ok=True); "
            "Path('.daikibo-build/a').write_text('a')",
        ],
        produces=["a"],
    )
    body["checks"].insert(1, {
        "id": "build-b", "category": "build", "repo": fixture["repository"],
        "kind": "command", "argv": ["python", "-c", "raise SystemExit(1)"],
        "purpose": "actual failed producer", "produces": ["b"],
    })
    body["checks"][2]["uses"] = ["a", "b"]
    full.d.configure(full.owner, project, body)
    finish_task(full, project, fixture["task"])
    full.breakdowns.activate(full.owner, fixture["breakdown"])
    delivery_id = full.d.prepare(full.owner, project)["id"]
    verification = full.d.verify(full.owner, delivery_id)
    _row, delivery_body = full.d.current(delivery_id)
    build_receipt = next(item["receipt"] for item in verification["results"]
                         if item["check"] == "build")
    snapshot = _receipt_delivery_snapshot(full, full.g.receipt(build_receipt))

    proposal = _replace_fixture_profile(
        full, fixture, relation_selectors=["contains", "produced_by", "realizes"],
    )
    context = collect_stage_context(
        full, full.owner, project=project, program=fixture["program"],
        stage="delivery", proposed_breakdown=fixture["breakdown"], delivery=snapshot,
    )
    denominator = derive_denominator(context)
    declarations = [item for item in denominator["obligations"]
                    if item["category"] == "delivery_declared_output"]
    output_ref = full.assurance.pin(
        full.owner, project,
        {"kind": "output_artifact", "delivery": snapshot, "check_id": "build",
         "receipt": build_receipt, "output_id": "a"},
    )["ref"]
    producer = next(item["producer_ref"] for item in context["delivery_material"]["declared_outputs"]["items"]
                    if item["definition"]["id"] == "a")
    return {
        "fixture": fixture, "project": project, "snapshot": snapshot,
        "context": context, "denominator": denominator, "scope": proposal["profile_ref"],
        "declarations": declarations, "output_ref": output_ref,
        "producer": producer, "build_receipt": build_receipt,
        "verification": verification, "delivery_body": delivery_body,
    }


def _edge(full, case, *, relation, source, target, obligation_ids, name):
    body = {
        "format": "assurance.edge.v1", "project": case["project"],
        "source_ref": source, "target_ref": target, "relation": relation,
        "relation_contract_digest": REGISTRY_V2_DIGEST,
        "scope_ref": case["scope"], "claim": "controller edge fixture",
        "obligation_ids": sorted(obligation_ids), "required_evidence_refs": [],
        "authority_refs": [],
    }
    return full.assurance.store_object(
        full.owner, case["project"], "edge", name, 1, body,
    )


def _reviews(full, case):
    return select_node_reviews(
        full, full.owner,
        node_requests=build_node_requests(
            full, full.owner, project=case["project"], selectors=[],
        ),
    )


def _requirements(relation):
    return sorted(SET_UNIVERSAL_CRITERIA | set(
        registry_entry(relation, contract_digest=REGISTRY_V2_DIGEST)["set_checks"]
    ))


def test_v3_relation_request_uses_exact_registry_and_delivery_owner(full, full_project, tmp_path):
    case = _case(full, full_project, tmp_path)
    declaration = next(item for item in case["declarations"]
                       if item["pointer"].endswith("/0"))
    request = build_relation_request(
        full, full.owner, context=case["context"], denominator=case["denominator"],
        relation="produced_by", center_ref=case["output_ref"], direction="outgoing",
        scope_ref=case["scope"], registry_digest=REGISTRY_V2_DIGEST,
    )
    assert request["required_obligation_ids"] == [declaration["id"]]
    assert request["capabilities"]["categories"] == ["delivery_declared_output"]
    assert request["owner_mapping"] == [{"obligation_id": declaration["id"],
                                          "owners": [case["producer"]]}]
    with pytest.raises(Fault) as rejected:
        build_relation_request(
            full, full.owner, context=case["context"], denominator=case["denominator"],
            relation="produced_by", center_ref=case["output_ref"], direction="outgoing",
            scope_ref=case["scope"], registry_digest=REGISTRY_DIGEST,
        )
    assert rejected.value.code == "invalid_registry"


def test_v2_produced_by_criteria_keeps_delivery_failure_separate_from_missing_edge(
    full, full_project, tmp_path,
):
    case = _case(full, full_project, tmp_path)
    declaration = next(item for item in case["declarations"]
                       if item["pointer"].endswith("/0"))
    request = build_relation_request(
        full, full.owner, context=case["context"], denominator=case["denominator"],
        relation="produced_by", center_ref=case["snapshot"], direction="outgoing",
        scope_ref=case["scope"], registry_digest=REGISTRY_V2_DIGEST,
    )
    edge = _edge(
        full, case, relation="produced_by", source=case["output_ref"],
        target=case["producer"], obligation_ids=[declaration["id"]],
        name="consumer-c-produced-by-a",
    )
    result = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=case["denominator"], edges=[edge], validated_reviews=_reviews(full, case),
        relation_request=request,
    )
    criterion = result["criteria"]["all_declared_outputs"]
    failed = next(item for item in case["declarations"] if item["pointer"].endswith("/1"))
    assert criterion["observed_ids"] == [declaration["id"]]
    assert criterion["missing_ids"] == [failed["id"]], result
    assert criterion["status"] == "failed"


def test_v2_contains_keeps_two_declarations_when_only_one_output_exists(
    full, full_project, tmp_path,
):
    case = _case(full, full_project, tmp_path)
    request = build_relation_request(
        full, full.owner, context=case["context"], denominator=case["denominator"],
        relation="contains", center_ref=case["snapshot"], direction="outgoing",
        scope_ref=case["scope"], registry_digest=REGISTRY_V2_DIGEST,
    )
    declaration = next(item for item in case["declarations"]
                       if item["pointer"].endswith("/0"))
    edge = _edge(
        full, case, relation="contains", source=case["snapshot"],
        target=case["output_ref"], obligation_ids=[declaration["id"]],
        name="consumer-c-contains-a",
    )
    result = evaluate_criteria(
        relation="contains", requirements=_requirements("contains"),
        denominator=case["denominator"], edges=[edge], validated_reviews=_reviews(full, case),
        relation_request=request,
    )
    criterion = result["criteria"]["all_required_outputs"]
    assert len(criterion["required_ids"]) == 2
    assert criterion["observed_ids"] == [declaration["id"]], result
    assert criterion["status"] == "failed"
