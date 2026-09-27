from __future__ import annotations

import sys
from pathlib import Path

import pytest

from daikibo.common import Fault, digest


def _artifact_ref(project, row):
    return {"kind": "artifact", "project": project, "artifact": row["id"],
            "revision": row["revision"], "body_digest": row["digest"]}


def _setup(full):
    project = full.k.create_project(full.owner, "E2 semantic repair")['id']
    source = full.k.source(full.owner, project, "Repair test source")
    requirement = full.k.propose(
        full.owner, project, "requirement",
        {"title": "Requirement", "statement": "The requirement is explicit.",
         "acceptance": ["AC-REPAIR"], "source_refs": [source["id"]]},
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
        {"scope_ref": scope["scope_ref"], "stage_rules": {"plan": {}},
         "relation_selectors": ["realizes"], "test_definition_bindings": []},
    )
    full.rt.adapters.register(
        full.owner, "assurance-fixture", "fixture", sys.executable,
        [str(Path(__file__).with_name("assurance_reviewer_fixture.py"))],
    )
    return project, requirement, design, scope, profile


def _review_refs(full, project, root):
    result = []
    for dependency in full.assurance._adoption_roots(project, root):
        for packet in full.assurance.review_subject(full.owner, project, dependency["id"])["packets"]:
            for role in packet["body"]["required_roles"]:
                full.rt.review(full.owner, packet["id"], role, "assurance-fixture")
                receipt = full.s.one(
                    "SELECT id FROM receipts WHERE subject=? AND role=? ORDER BY created DESC",
                    (packet["id"], role), True,
                )
                result.append({"packet": packet["id"], "role": role, "id": receipt["id"]})
    return result


def _adopt(full, project, row):
    refs = _review_refs(full, project, row)
    return full.assurance.adopt(full.owner, project, row["id"], row["digest"], None, refs)


def _edge(full, project, requirement, design, scope, profile, claim="realizes"):
    obligation = scope["obligations"]["body"]["obligations"][0]["id"]
    return full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": _artifact_ref(project, design), "target_ref": _artifact_ref(project, requirement),
         "relation": "realizes", "scope_ref": profile["profile_ref"], "claim": claim,
         "obligation_ids": [obligation], "required_evidence_refs": [], "authority_refs": []},
    )


def _revise_artifact(full, project, artifact, body):
    source = full.k.source(full.owner, project, "Repair fixture revision")
    change = full.p.change(full.owner, project, {
        "title": "Repair revision", "origin": "user", "reason": "Repair fixture",
        "source": source["id"], "affected": [artifact["id"]], "evidence": [source["id"]],
        "deltas": [{"artifact": artifact["id"], "expected_revision": 1, "body": body}],
    })
    decision = full.p.propose_decision(full.owner, project, {
        "title": "Repair revision", "reason": "Repair fixture", "options": ["approve", "keep_existing"],
        "recommendation": "approve", "refs": [artifact["id"]], "requirement_affecting": True,
        "change": change["id"],
    })
    full.p.respond(full.owner, decision["id"], decision["digest"], "approve", "Repair fixture approval")
    receipt = full.rt.review(full.owner, decision["id"], "consistency", "assurance-fixture")
    full.p.apply_decision(full.owner, decision["id"], receipt["receipt"])


def test_currentness_rechecks_endpoint_and_set_membership(full):
    project, requirement, design, scope, profile = _setup(full)
    _adopt(full, project, profile["profile"])
    edge = _edge(full, project, requirement, design, scope, profile)
    _adopt(full, project, edge["edge"])
    relation_set = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": _artifact_ref(project, design), "relation": "realizes", "direction": "outgoing",
         "scope_ref": profile["profile_ref"], "criteria": {}, "required_evidence_refs": []},
    )
    _adopt(full, project, relation_set["set"])

    _revise_artifact(full, project, design, {**design["body"], "statement": "revision two"})
    current_edge_ref = {"kind": "assurance_object", "project": project, "object": edge["edge"]["id"],
                        "object_kind": "edge", "object_digest": edge["edge"]["digest"]}
    with pytest.raises(Fault) as rejected:
        full.assurance.resolve(full.owner, project, current_edge_ref,
                               {"profile": {"id": profile["profile"]["id"], "digest": profile["profile"]["digest"]},
                                "stage": "plan", "task": None, "delivery": None})
    assert rejected.value.code in {"stale_reference", "stale_set", "missing_evidence"}
    edge_report = next(item for item in full.assurance.report(full.owner, project)["items"]
                       if item["subject"]["object"] == edge["edge"]["id"])
    assert edge_report["status"] == "stale"

    _edge(full, project, requirement, design, scope, profile, claim="new edge")
    set_report = next(item for item in full.assurance.report(full.owner, project)["items"]
                      if item["subject"]["object"] == relation_set["set"]["id"])
    assert set_report["status"] == "stale"


def test_set_criteria_are_registry_bound_and_achieved_separately(full):
    project, requirement, design, scope, profile = _setup(full)
    with pytest.raises(Fault) as unknown:
        full.assurance.set_propose(
            full.owner, project,
            {"center_ref": _artifact_ref(project, design), "relation": "realizes", "direction": "outgoing",
             "scope_ref": profile["profile_ref"], "criteria": {"invented": True},
             "required_evidence_refs": []},
        )
    assert unknown.value.code == "invalid_criterion"
    _edge(full, project, requirement, design, scope, profile)
    result = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": _artifact_ref(project, design), "relation": "realizes", "direction": "outgoing",
         "scope_ref": profile["profile_ref"], "criteria": {}, "required_evidence_refs": []},
    )
    assert result["criteria"]["all_edges_current"] is False
    assert result["criteria_requirements_digest"] == digest(result["criteria_requirements"])


@pytest.mark.parametrize("value", [False, {"required": False}, 1, {"required": 1}])
def test_set_criterion_wire_value_type_is_strict(full, value):
    project, _requirement, design, _scope, profile = _setup(full)
    with pytest.raises(Fault) as rejected:
        full.assurance.set_propose(
            full.owner, project,
            {"center_ref": _artifact_ref(project, design), "relation": "realizes",
             "direction": "outgoing", "scope_ref": profile["profile_ref"],
             "criteria": {"meaning_review": value}, "required_evidence_refs": []},
        )
    assert rejected.value.code == "invalid_criterion"


@pytest.mark.parametrize("kind", ["profile", "scope", "obligations"])
def test_legacy_typed_dependency_cannot_make_verified_archive(full, kind):
    project = full.k.create_project(full.owner, "Legacy archive repair")['id']
    missing = {"kind": "artifact", "project": project, "artifact": "ABSENT",
               "revision": 1, "body_digest": digest({"missing": True})}
    full.assurance.store_object(
        full.owner, project, kind, "legacy-" + kind, 1,
        {"format": kind + ".v1", "project": project, "source": missing},
    )
    with pytest.raises(Fault) as rejected:
        full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    assert rejected.value.code == "invalid_archive"
