from __future__ import annotations

import copy
import json

import pytest

from daikibo.assurance import SET_UNIVERSAL_CRITERIA
from daikibo.assurance_criteria import build_relation_request, evaluate_criteria
from daikibo.assurance_denominators import collect_stage_context, derive_denominator, project_task
from daikibo.assurance_node_reviews import build_node_requests, select_node_reviews
from daikibo.assurance_relations import REGISTRY_DIGEST, registry_entry
from daikibo.common import Fault, canonical, digest, timestamp

from test_e3_unit2a_denominators import _fixture


def _artifact_ref(project, row):
    return {"kind": "artifact", "project": project, "artifact": row["id"],
            "revision": row["revision"], "body_digest": row["digest"]}


def _requirements(relation):
    return sorted(SET_UNIVERSAL_CRITERIA | set(registry_entry(relation)["set_checks"]))


def _scope(full, project, requirement):
    return full.assurance.scope_propose(
        full.owner, project,
        {"roots": [_artifact_ref(project, requirement)], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )


def test_relation_request_requires_controller_seals_and_keeps_new_family_unverified(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    context = collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"],
    )
    denominator = derive_denominator(context)
    scope = _scope(full, fixture["project"], fixture["parent"])
    center = _artifact_ref(fixture["project"], fixture["parent"])
    request = build_relation_request(
        full, full.owner, context=context, denominator=denominator,
        relation="assigned_to", center_ref=center, direction="outgoing",
        scope_ref=scope["scope_ref"], registry_digest=REGISTRY_DIGEST,
    )
    reviews = select_node_reviews(
        full, full.owner,
        node_requests=build_node_requests(
            full, full.owner, project=fixture["project"],
            selectors=[{"selector": "requirement", "node_ref": center}],
        ),
    )
    result = evaluate_criteria(
        relation="assigned_to", requirements=_requirements("assigned_to"),
        denominator=denominator, edges=[], validated_reviews=reviews,
        relation_request=request,
    )
    assert result["criteria"]["all_requirements"]["status"] in {"missing", "unverified"}
    assert result["criteria"]["all_requirements"]["status"] != "satisfied"
    with pytest.raises(Fault) as copied:
        evaluate_criteria(
            relation="assigned_to", requirements=_requirements("assigned_to"),
            denominator=denominator, edges=[], validated_reviews=reviews,
            relation_request=copy.deepcopy(dict(request)),
        )
    assert copied.value.code == "invalid_relation_request"


def test_legacy_call_cannot_promote_new_denominator_family(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    denominator = derive_denominator(collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"],
    ))
    center = _artifact_ref(fixture["project"], fixture["parent"])
    scope = _scope(full, fixture["project"], fixture["parent"])
    reviews = select_node_reviews(
        full, full.owner,
        node_requests=build_node_requests(
            full, full.owner, project=fixture["project"],
            selectors=[{"selector": "requirement", "node_ref": center}],
        ),
    )
    result = evaluate_criteria(
        relation="assigned_to", requirements=_requirements("assigned_to"),
        denominator=denominator, edges=[], validated_reviews=reviews,
    )
    assert result["criteria"]["all_requirements"]["status"] == "unverified"


def test_task_projection_is_local_but_keeps_global_owner_identity(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    task_ref = {
        "kind": "task_revision", "project": fixture["project"],
        "task": fixture["task_a"]["id"], "revision": fixture["task_a"]["revision"],
        "definition_digest": digest(fixture["task_a"]["body"]),
    }
    context = collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="task", proposed_breakdown=fixture["breakdown"], task=task_ref,
    )
    denominator = derive_denominator(context)
    projection = project_task(denominator, task_ref)
    scope = _scope(full, fixture["project"], fixture["parent"])
    request = build_relation_request(
        full, full.owner, context=context, denominator=denominator,
        relation="assigned_to", center_ref=_artifact_ref(fixture["project"], fixture["parent"]),
        direction="outgoing", scope_ref=scope["scope_ref"],
        registry_digest=REGISTRY_DIGEST, projection=projection,
    )
    assert request["local_projection"]["global_digest"] == denominator["digest"]
    assert request["capabilities"]["projection_scope"] == "task"
    assert all(owner["task"] == fixture["task_a"]["id"]
               for item in request["owner_mapping"] for owner in item["owners"]
               if owner.get("kind") == "task_revision")


def test_child_obligation_uses_stored_child_to_parent_link(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    full.k.link(full.owner, fixture["child"]["id"], fixture["parent"]["id"],
                "decomposes", "asserted", "child requirement decomposition")
    body = json.loads(full.s.one("SELECT body FROM breakdowns WHERE id=?",
                                 (fixture["breakdown"],))["body"])
    body["scope"] = full.breakdowns._scope(full.owner, fixture["project"])
    breakdown = "BREAKDOWN-consumer-child"
    with full.s.transaction():
        full.s.execute(
            "INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)",
            (breakdown, fixture["program"], fixture["project"], canonical(body).decode(),
             digest(body), "proposed", None, timestamp()),
        )
    context = collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=breakdown,
    )
    denominator = derive_denominator(context)
    obligation = next(item for item in denominator["obligations"]
                      if item["category"] == "child_obligation")
    scope = full.assurance.scope_propose(
        full.owner, fixture["project"],
        {"roots": [_artifact_ref(fixture["project"], fixture["parent"]),
                   _artifact_ref(fixture["project"], fixture["child"])],
         "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []},
    )
    edge_body = {
        "format": "assurance.edge.v1", "project": fixture["project"],
        "source_ref": _artifact_ref(fixture["project"], fixture["child"]),
        "target_ref": _artifact_ref(fixture["project"], fixture["parent"]),
        "relation": "decomposes", "relation_contract_digest": REGISTRY_DIGEST,
        "scope_ref": scope["scope_ref"], "claim": "stored child decomposition",
        "obligation_ids": [obligation["id"]], "required_evidence_refs": [],
        "authority_refs": [],
    }
    edge = full.assurance.store_object(
        full.owner, fixture["project"], "edge", "edge-consumer-child", 1, edge_body,
    )
    request = build_relation_request(
        full, full.owner, context=context, denominator=denominator,
        relation="decomposes", center_ref=_artifact_ref(fixture["project"], fixture["parent"]),
        direction="incoming", scope_ref=scope["scope_ref"], registry_digest=REGISTRY_DIGEST,
    )
    reviews = select_node_reviews(
        full, full.owner,
        node_requests=build_node_requests(
            full, full.owner, project=fixture["project"], selectors=[],
        ),
    )
    result = evaluate_criteria(
        relation="decomposes", requirements=_requirements("decomposes"),
        denominator=denominator, edges=[edge], validated_reviews=reviews,
        relation_request=request,
    )
    child_result = result["criteria"]["all_child_obligations"]
    assert child_result["observed_ids"] == [obligation["id"]]
    assert child_result["status"] == "missing"
