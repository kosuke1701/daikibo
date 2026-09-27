from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

from daikibo.common import Fault


def _artifact_ref(project, row):
    return {"kind": "artifact", "project": project, "artifact": row["id"],
            "revision": row["revision"], "body_digest": row["digest"]}


def _source_ref(project, row):
    return {"kind": "source", "project": project, "source": row["id"],
            "blob_digest": row["digest"]}


def _fixture(full):
    project = full.k.create_project(full.owner, "E3 canonical selection")['id']
    source = full.k.source(full.owner, project, "The requirement is source grounded.")
    requirement = full.k.propose(
        full.owner, project, "requirement",
        {"title": "Requirement", "statement": "The requirement is explicit.",
         "acceptance": ["AC-E3"], "source_refs": [source["id"]]},
    )
    requirement = full.k.accept(full.owner, requirement["id"], 1)
    program = full.p.begin(full.owner, project, source["id"])["program"]
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [_artifact_ref(project, requirement)], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    return project, source, requirement, program, scope


def _profile_body(project, program, scope, *, previous=None, mode="mandatory",
                  authority_refs=None, reason="initial profile"):
    relation_set = {"relation": "realizes", "direction": "outgoing", "centers": ["requirements"]}
    stages = {
        "plan": {"denominator": "program_plan", "relation_sets": [relation_set],
                 "node_rules": ["requirements"], "execution_results": "none"},
        "task": {"denominator": "assigned_task_contributors", "relation_sets": [relation_set],
                 "node_rules": ["requirements"], "execution_results": "assigned_checks"},
        "integration": {"denominator": "program_integration", "relation_sets": [relation_set],
                         "node_rules": ["requirements"], "execution_results": "integration_checks"},
        "delivery": {"denominator": "actual_delivery", "relation_sets": [relation_set],
                      "node_rules": ["requirements"],
                      "execution_results": "certified_integration_and_actual_outputs"},
    }
    return {
        "format": "assurance.profile.v2", "project": project, "program": program,
        "scope_ref": scope["scope_ref"], "obligations_ref": scope["obligations_ref"],
        "previous_selection_ref": previous, "application_mode": mode,
        "stage_rules": stages,
        "node_review_rules": [{"id": "requirements", "selector": "requirement",
                                "roles": ["requirements"]}],
        "relation_selectors": ["realizes"], "test_definition_bindings": [],
        "change_reason": reason, "authority_refs": authority_refs or [],
    }


def _register_fixture_review(full):
    full.rt.adapters.register(
        full.owner, "e3-assurance-fixture", "fixture", sys.executable,
        [str(Path(__file__).with_name("assurance_reviewer_fixture.py"))],
    )


def _review_refs(full, project, root):
    refs = []
    for dependency in full.assurance._adoption_roots(project, root):
        for packet in full.assurance.review_subject(full.owner, project, dependency["id"])["packets"]:
            for role in packet["body"]["required_roles"]:
                review = full.rt.review(full.owner, packet["id"], role, "e3-assurance-fixture")
                refs.append({"packet": packet["id"], "role": role,
                             "id": review["receipt"]})
    return refs


def _adopt(full, project, proposal, expected_head):
    refs = _review_refs(full, project, proposal["profile"])
    return full.assurance.adopt(full.owner, project, proposal["profile"]["id"],
                                proposal["profile"]["digest"], expected_head, refs)


def test_profile_v2_catalog_and_bootstrap_selection(full):
    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    catalog = full.assurance.catalog(full.owner)
    assert catalog["profile"]["format"] == "assurance.profile.v2"
    assert catalog["profile"]["logical_id"] == "profile:program:<program>"
    descriptor = full.invoke(full.owner, "api.describe", {"method": "assurance.profile_propose"})
    assert descriptor["methods"]["assurance.profile_propose"]["body_contract"]["format"] == "assurance.profile.v2"
    body = _profile_body(project, program, scope)
    proposed = full.invoke(full.owner, "assurance.profile_propose",
                           {"project": project, "program": program,
                            "body": body, "expected_head": None})
    assert proposed["profile"]["logical_id"] == "profile:program:" + program
    assert proposed["selection"]["state"] == "not_enabled"
    adopted = _adopt(full, project, proposed, None)
    assert adopted["event"]["expected_head"] is None
    selection = full.assurance.selected_profile(full.owner, project, program)
    assert selection["profile_ref"] == proposed["profile_ref"]
    assert selection["state"] == "selected"
    assert selection["strong_complete"] is False
    assert selection["stage_evaluator"] is False


def test_profile_v2_competing_proposals_use_head_cas_and_replay(full):
    project, source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(full.owner, project, program,
                                           _profile_body(project, program, scope), None)
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    old_head = selected["head_event"]
    authority = [_source_ref(project, source)]
    second_body = _profile_body(project, program, scope,
                                previous=selected["profile_ref"], authority_refs=authority,
                                reason="replace profile one")
    third_body = copy.deepcopy(second_body)
    third_body["change_reason"] = "replace profile two"
    second = full.assurance.profile_propose(full.owner, project, program, second_body, old_head)
    third = full.assurance.profile_propose(full.owner, project, program, third_body, old_head)
    assert full.assurance.selected_profile(full.owner, project, program)["head_event"] == old_head
    adopted = _adopt(full, project, second, old_head)
    replay = _adopt(full, project, second, old_head)
    assert replay["id"] == adopted["id"]
    assert replay["idempotent"] is True
    assert full.assurance.selected_profile(full.owner, project, program)["profile_ref"] == second["profile_ref"]
    with pytest.raises(Fault) as stale:
        _adopt(full, project, third, old_head)
    assert stale.value.code == "stale_head"


def test_profile_v2_disabled_requires_authority_and_never_completes(full):
    project, source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(full.owner, project, program,
                                           _profile_body(project, program, scope), None)
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    with pytest.raises(Fault) as missing_authority:
        full.assurance.profile_propose(
            full.owner, project, program,
            _profile_body(project, program, scope, previous=selected["profile_ref"],
                          mode="disabled", reason="disable without source"),
            selected["head_event"],
        )
    assert missing_authority.value.code == "invalid_profile"
    disabled = full.assurance.profile_propose(
        full.owner, project, program,
        _profile_body(project, program, scope, previous=selected["profile_ref"],
                      mode="disabled", authority_refs=[_source_ref(project, source)],
                      reason="source-backed disable"),
        selected["head_event"],
    )
    _adopt(full, project, disabled, selected["head_event"])
    result = full.assurance.selected_profile(full.owner, project, program)
    assert result["state"] == "disabled"
    assert result["application_mode"] == "disabled"
    assert result["strong_complete"] is False


@pytest.mark.parametrize("mutate", [
    lambda body: body["stage_rules"].pop("task"),
    lambda body: body["stage_rules"]["plan"].update({"unknown": "x"}),
    lambda body: body["stage_rules"]["plan"].update({"denominator": False}),
    lambda body: body["stage_rules"]["plan"].update({"denominator": 1}),
    lambda body: body["node_review_rules"][0].update({"roles": []}),
    lambda body: body["node_review_rules"][0].update({"selector": "owner"}),
    lambda body: body["relation_selectors"].clear(),
])
def test_profile_v2_rule01_rejects_weak_or_unknown_contract(full, mutate):
    project, _source, _requirement, program, scope = _fixture(full)
    body = _profile_body(project, program, scope)
    mutate(body)
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(full.owner, project, program, body, None)
    assert rejected.value.code == "invalid_profile"


def test_legacy_v1_profile_is_migration_pending_and_not_canonical(full):
    project, _source, _requirement, program, scope = _fixture(full)
    legacy = full.assurance.profile_propose(
        full.owner, project, program,
        {"scope_ref": scope["scope_ref"], "stage_rules": {"plan": {}},
         "relation_selectors": [], "test_definition_bindings": []},
    )
    assert legacy["profile"]["body"]["format"] == "assurance.profile.v1"
    selection = full.assurance.selected_profile(full.owner, project, program)
    assert selection["state"] == "migration_pending"
    assert selection["profile_ref"] is None
    assert full.assurance.report(full.owner, project, program=program)["selection"]["state"] == "migration_pending"
