"""Finite Unit 2b criteria checks against controller-owned evidence."""
from __future__ import annotations

import json
import sys

from daikibo.assurance import SET_UNIVERSAL_CRITERIA
from daikibo.assurance_criteria import evaluate_criteria
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_node_reviews import build_node_requests, select_node_reviews
from daikibo.assurance_relations import REGISTRY_DIGEST, registry_entry
from daikibo.common import digest
from test_e3_unit2a_denominators import _fixture, _task, _unit_b


def _artifact_ref(project, row):
    return {"kind": "artifact", "project": project, "artifact": row["id"],
            "revision": row["revision"], "body_digest": row["digest"]}


def _denominator(full, fixture):
    return derive_denominator(collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"],
    ))


def _review(full, fixture):
    return select_node_reviews(
        full, full.owner,
        node_requests=build_node_requests(
            full, full.owner, project=fixture["project"],
            selectors=[{"selector": "requirement",
                        "node_ref": _artifact_ref(fixture["project"], fixture["parent"])}],
        ),
    )


def _requirements(relation):
    return sorted(SET_UNIVERSAL_CRITERIA | set(registry_entry(relation)["set_checks"]))


def _scope(full, fixture, *roots):
    return full.assurance.scope_propose(
        full.owner, fixture["project"], {
            "roots": [_artifact_ref(fixture["project"], root) for root in roots],
            "selection_rules": {}, "exclusion_proposals": [],
            "authority_refs": [], "discovery_unknowns": [],
        },
    )


def _realizes_edge(full, fixture, scope, source, target, obligation_ids,
                   *, logical_id):
    body = {
        "format": "assurance.edge.v1", "project": fixture["project"],
        "source_ref": _artifact_ref(fixture["project"], source),
        "target_ref": _artifact_ref(fixture["project"], target),
        "relation": "realizes", "relation_contract_digest": REGISTRY_DIGEST,
        "scope_ref": scope["scope_ref"], "claim": "finite fixture edge",
        "obligation_ids": sorted(obligation_ids), "required_evidence_refs": [],
        "authority_refs": [],
    }
    return full.assurance.store_object(
        full.owner, fixture["project"], "edge", logical_id, 1, body,
    )


def test_raw_edge_claim_without_controller_row_is_unverified(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    denominator = _denominator(full, fixture)
    design = full.k.accept(full.owner, full.k.propose(
        full.owner, fixture["project"], "design", {
            "title": "Design", "statement": "Parent only",
            "source_refs": [fixture["source"]["id"]],
        })["id"], 1)
    target = _artifact_ref(fixture["project"], fixture["parent"])
    source = _artifact_ref(fixture["project"], design)
    scope = _scope(full, fixture, fixture["parent"])
    body = {
        "format": "assurance.edge.v1", "project": fixture["project"],
        "source_ref": source, "target_ref": target, "relation": "realizes",
        "relation_contract_digest": REGISTRY_DIGEST, "scope_ref": scope["scope_ref"],
        "claim": "caller self claim", "obligation_ids": sorted(
            item["id"] for item in denominator["obligations"]
            if item["category"] == "acceptance_condition"
        ), "required_evidence_refs": [], "authority_refs": [],
    }
    edge = {"id": "EDGE-unstored-self-claim", "revision": 1,
            "digest": digest(body), "body": body}
    result = evaluate_criteria(
        relation="realizes", requirements=_requirements("realizes"),
        denominator=denominator, edges=[edge], validated_reviews=_review(full, fixture),
    )
    assert result["criteria"]["all_acceptance_conditions"]["status"] == "unverified"
    assert result["criteria"]["all_obligations_covered"]["status"] == "unverified"
    assert {item["code"] for item in result["capabilities"]["adapter_diagnostics"]} == {
        "edge_not_controller_stored"
    }


def test_controller_stored_edges_map_each_artifact_acceptance_exactly(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    denominator = _denominator(full, fixture)
    design = full.k.accept(full.owner, full.k.propose(
        full.owner, fixture["project"], "design", {
            "title": "Parent design", "statement": "Parent",
            "source_refs": [fixture["source"]["id"]],
        })["id"], 1)
    child_design = full.k.accept(full.owner, full.k.propose(
        full.owner, fixture["project"], "design", {
            "title": "Child design", "statement": "Child",
            "source_refs": [fixture["source"]["id"]],
        })["id"], 1)
    scope = _scope(full, fixture, fixture["parent"], fixture["child"])
    parent_ids = {
        item["id"] for item in denominator["obligations"]
        if item["category"] == "acceptance_condition"
        and item["source_ref"]["locator"]["artifact"] == fixture["parent"]["id"]
    }
    child_ids = {
        item["id"] for item in denominator["obligations"]
        if item["category"] == "acceptance_condition"
        and item["source_ref"]["locator"]["artifact"] == fixture["child"]["id"]
    }
    parent_edge = _realizes_edge(
        full, fixture, scope, design, fixture["parent"], parent_ids,
        logical_id="edge-unit2b-parent",
    )
    child_edge = _realizes_edge(
        full, fixture, scope, child_design, fixture["child"], child_ids,
        logical_id="edge-unit2b-child",
    )
    result = evaluate_criteria(
        relation="realizes", requirements=_requirements("realizes"),
        denominator=denominator, edges=[parent_edge, child_edge],
        validated_reviews=_review(full, fixture),
    )
    acceptance = result["criteria"]["all_acceptance_conditions"]
    assert acceptance["status"] == "satisfied"
    assert acceptance["observed_ids"] == sorted(parent_ids | child_ids)
    assert result["criteria"]["all_obligations_covered"]["status"] == "missing"
    # The rows are immutable but have not gone through the adoption gate, so
    # a matching body/digest is not enough to claim currentness.
    assert result["criteria"]["all_edges_current"]["status"] == "stale"


def test_changed_body_with_new_digest_cannot_replace_controller_edge(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    denominator = _denominator(full, fixture)
    design = full.k.accept(full.owner, full.k.propose(
        full.owner, fixture["project"], "design", {
            "title": "Design", "statement": "Parent",
            "source_refs": [fixture["source"]["id"]],
        })["id"], 1)
    scope = _scope(full, fixture, fixture["parent"])
    ids = {
        item["id"] for item in denominator["obligations"]
        if item["category"] == "acceptance_condition"
        and item["source_ref"]["locator"]["artifact"] == fixture["parent"]["id"]
    }
    stored = _realizes_edge(
        full, fixture, scope, design, fixture["parent"], ids,
        logical_id="edge-unit2b-tamper",
    )
    tampered = json.loads(json.dumps(stored["body"]))
    tampered["claim"] = "changed after controller storage"
    supplied = {"id": stored["id"], "revision": stored["revision"],
                "digest": digest(tampered), "body": tampered}
    result = evaluate_criteria(
        relation="realizes", requirements=_requirements("realizes"),
        denominator=denominator, edges=[supplied], validated_reviews=_review(full, fixture),
    )
    assert result["criteria"]["all_acceptance_conditions"]["status"] == "unverified"
    assert {item["code"] for item in result["capabilities"]["adapter_diagnostics"]} == {
        "edge_identity_differs"
    }


def test_unmaterialized_failed_observation_is_unverified_after_record_consistency(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    task = fixture["task_a"]
    full.w.ready(full.owner, task["id"])
    full.w.claim(full.owner, fixture["project"], task["id"])
    plan = json.loads(full.s.one(
        "SELECT body FROM plans WHERE task=?", (task["id"],), True,
    )["body"])
    check = plan["checks"][0]
    snapshot = {"format": "snapshot.v1", "repos": {}}
    snapshot["digest"] = digest(snapshot)
    observed = full.rt.observe(
        fixture["project"], task["id"], task["id"], "test:" + check["id"], None,
        full.g.task_binding(task["id"]), snapshot,
        lambda _worktree, _home, _cwd: ([sys.executable, "-c", "raise SystemExit(1)"], None),
        epoch=full.w.task(full.owner, task["id"])["epoch"], check=check,
    )[0]
    denominator = _denominator(full, fixture)
    obligation = next(item for item in denominator["obligations"]
                      if item["category"] == "required_check"
                      and item["source_ref"]["check_id"] == observed["check_id"])
    source = {
        "kind": "observed_result", "project": fixture["project"],
        "receipt": observed["id"], "run": observed["run"],
        "receipt_digest": digest(observed), "run_binding": observed["binding"],
        "snapshot_digest": observed["snapshot"],
        "result_digest": digest(observed["result"]),
    }
    scope = _scope(full, fixture, fixture["parent"])
    body = {
        "format": "assurance.edge.v1", "project": fixture["project"],
        "source_ref": source, "target_ref": obligation["source_ref"],
        "relation": "execution_of", "relation_contract_digest": REGISTRY_DIGEST,
        "scope_ref": scope["scope_ref"], "claim": "observed failing check",
        "obligation_ids": [obligation["id"]], "required_evidence_refs": [],
        "authority_refs": [],
    }
    edge = full.assurance.store_object(
        full.owner, fixture["project"], "edge", "edge-unit2b-failed-check", 1, body,
    )
    result = evaluate_criteria(
        relation="execution_of", requirements=_requirements("execution_of"),
        denominator=denominator, edges=[edge], validated_reviews=_review(full, fixture),
    )
    checks = result["criteria"]["all_required_checks"]
    # A raw observe result has no controller-pinned definition_ref.  Its
    # failure is retained as telemetry, but it cannot cover a denominator
    # check until Runtime.tests/Delivery.verify supplies immutable material.
    assert checks["status"] == "unverified"
    assert obligation["id"] not in checks["observed_ids"]


def test_population_checker_requires_all_port_contributors_and_keeps_exclusion(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    task_b = _task(full, fixture["project"], fixture["parent"]["id"], "Task B", [
        {"id": "b", "argv": ["python", "-c", "print(3)"],
         "kind": "command", "purpose": "b"},
    ])
    _unit_b(full, fixture, task_b)
    task_ref = {
        "kind": "task_revision", "project": fixture["project"],
        "task": fixture["task_a"]["id"], "revision": 1,
        "definition_digest": digest(fixture["task_a"]["body"]),
    }
    context = collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="task", proposed_breakdown=fixture["breakdown"], task=task_ref,
    )
    denominator = derive_denominator(context)
    population = {
        item["source_ref"]["item"]: item for item in denominator["obligations"]
        if item["category"] == "population_leaf"
    }
    scope = _scope(full, fixture, fixture["parent"])
    target = {
        "kind": "traceability_ref", "project": fixture["project"],
        "locator": {
            "ref_type": "artifact_ac", "artifact": fixture["parent"]["id"],
            "revision": fixture["parent"]["revision"],
            "body_digest": fixture["parent"]["digest"],
            "ac_pointer": "/acceptance/0", "ac_digest": digest("AC-PARENT-0"),
            "ac_id": "AC-PARENT-0",
        },
    }
    edges = []
    for item_id in ("ITEM-A", "ITEM-B"):
        obligation = population[item_id]
        body = {
            "format": "assurance.edge.v1", "project": fixture["project"],
            "source_ref": obligation["source_ref"], "target_ref": target,
            "relation": "migrated_to", "relation_contract_digest": REGISTRY_DIGEST,
            "scope_ref": scope["scope_ref"], "claim": item_id,
            "obligation_ids": [obligation["id"]],
            "required_evidence_refs": [item["task_ref"] for item in obligation["contributors"]],
            "authority_refs": [],
        }
        edges.append(full.assurance.store_object(
            full.owner, fixture["project"], "edge", "edge-unit2b-pop-" + item_id, 1, body,
        ))
    result = evaluate_criteria(
        relation="migrated_to", requirements=_requirements("migrated_to"),
        denominator=denominator, edges=edges, validated_reviews=_review(full, fixture),
    )
    leaves = result["criteria"]["all_population_leaves"]
    assert population["ITEM-A"]["id"] in leaves["observed_ids"]
    assert population["ITEM-B"]["id"] in leaves["observed_ids"]
    assert population["ITEM-C"]["id"] in leaves["missing_ids"]
