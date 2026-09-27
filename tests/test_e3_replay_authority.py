from __future__ import annotations

import copy

import pytest

from daikibo.common import Fault, digest, parse_json

from test_e3_selection_contract import (
    _adopt,
    _fixture,
    _profile_body,
    _register_fixture_review,
    _review_refs,
)


def _apply_withdrawal_change(full, project, source, artifact):
    body = {**artifact["body"], "statement": "The explicitly revised requirement."}
    change = full.p.change(
        full.owner,
        project,
        {
            "title": "Revision",
            "origin": "user",
            "reason": "approved requirement update",
            "source": source["id"],
            "affected": [artifact["id"]],
            "evidence": [source["id"]],
            "deltas": [{"artifact": artifact["id"], "expected_revision": 1,
                        "body": body, "withdraw": True}],
        },
    )
    decision = full.p.propose_decision(
        full.owner,
        project,
        {
            "title": "Approve revision",
            "reason": "source grounded revision",
            "options": ["approve", "keep_existing"],
            "recommendation": "approve",
            "refs": [artifact["id"]],
            "requirement_affecting": True,
            "change": change["id"],
        },
    )
    full.p.respond(full.owner, decision["id"], decision["digest"],
                   "approve", "Approved requirement revision")
    review = full.rt.review(
        full.owner, decision["id"], "consistency", "e3-assurance-fixture"
    )
    full.p.apply_decision(full.owner, decision["id"], review["receipt"])
    row = full.s.one("SELECT * FROM changes WHERE id=?", (change["id"],))
    pinned = full.assurance.pin(
        full.owner,
        project,
        {"kind": "change", "change": change["id"], "revision": row["revision"],
         "body_digest": digest(parse_json(row["body"]))},
    )
    return change, pinned


def _withdrawal_transition(full):
    project, source, requirement, program, old_scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, old_scope), None,
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    change, pinned = _apply_withdrawal_change(full, project, source, requirement)
    scope = full.assurance.scope_propose(
        full.owner,
        project,
        {"roots": [], "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []},
    )
    body = _profile_body(
        project, program, scope, previous=selected["profile_ref"],
        authority_refs=[pinned["ref"]], reason="withdrawal replacement",
    )
    proposal = full.assurance.profile_propose(
        full.owner, project, program, body, selected["head_event"],
    )
    return {
        "project": project, "source": source, "requirement": requirement,
        "program": program, "old_scope": old_scope, "first": first,
        "selected": selected, "change": change, "pinned": pinned,
        "scope": scope, "body": body, "proposal": proposal,
    }


def test_withdrawal_replay_uses_exact_predecessor_scope_and_receipt(full):
    state = _withdrawal_transition(full)
    project = state["project"]
    proposal = state["proposal"]
    selected = state["selected"]
    for key in ("scope", "obligations"):
        dependency = state["scope"][key]
        full.assurance.adopt(
            full.owner, project, dependency["id"], dependency["digest"], None,
            _review_refs(full, project, dependency),
        )
    review_refs = _review_refs(full, project, proposal["profile"])
    event = full.assurance.adopt(
        full.owner, project, proposal["profile"]["id"], proposal["profile"]["digest"],
        selected["head_event"], review_refs,
    )
    replay = full.assurance.adopt(
        full.owner, project, proposal["profile"]["id"], proposal["profile"]["digest"],
        selected["head_event"], review_refs,
    )
    assert replay["id"] == event["id"]
    assert replay["idempotent"] is True
    assert full.assurance.resolve_pinned(full.owner, state["first"]["profile_ref"])
    assert full.assurance.resolve_pinned(full.owner, state["pinned"]["ref"])
    report = full.assurance.report(full.owner, project, program=state["program"])
    current = [item for item in report["items"] if item["kind"] == "profile"]
    assert len(current) == 1 and current[0]["status"] == "current"


def test_withdrawal_change_authority_rejects_unrelated_transition_target(full):
    project, source, requirement, program, old_scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, old_scope), None,
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    unrelated = full.k.propose(
        full.owner, project, "requirement",
        {"title": "Unrelated", "statement": "Outside the selected transition.",
         "acceptance": ["AC-E3-OTHER"], "source_refs": [source["id"]]},
    )
    unrelated = full.k.accept(full.owner, unrelated["id"], 1)
    _change, pinned = _apply_withdrawal_change(full, project, source, unrelated)
    empty_scope = full.assurance.scope_propose(
        full.owner,
        project,
        {"roots": [], "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []},
    )
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(
            full.owner, project, program,
            _profile_body(project, program, empty_scope,
                          previous=selected["profile_ref"],
                          authority_refs=[pinned["ref"]],
                          reason="unrelated withdrawal must not authorize transition"),
            selected["head_event"],
        )
    assert rejected.value.code == "unsupported_authority"
    assert full.assurance.resolve_pinned(full.owner, pinned["ref"])


def test_withdrawal_transition_rejects_modified_predecessor_reference(full):
    state = _withdrawal_transition(full)
    altered = copy.deepcopy(state["body"])
    altered["previous_selection_ref"]["object_digest"] = "0" * 64
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(
            full.owner, state["project"], state["program"], altered,
            state["selected"]["head_event"],
        )
    assert rejected.value.code == "stale_head"
    assert full.assurance.resolve_pinned(full.owner, state["first"]["profile_ref"])
    assert full.assurance.resolve_pinned(full.owner, state["pinned"]["ref"])
