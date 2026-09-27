from __future__ import annotations

import copy

import pytest

from daikibo.assurance import validate_assurance_rows
from daikibo.common import Fault, canonical, digest, parse_json

from test_e3_selection_contract import (
    _adopt,
    _artifact_ref,
    _fixture,
    _profile_body,
    _register_fixture_review,
    _source_ref,
)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("application_mode",), []),
        (("node_review_rules", 0, "selector"), {}),
        (("node_review_rules", 0, "roles"), [[]]),
        (("stage_rules", "plan", "relation_sets", 0, "direction"), {}),
        (("application_mode",), True),
        (("stage_rules", "plan", "relation_sets", 0, "direction"), 1),
    ],
)
def test_profile_wire_nested_bad_types_are_structured_faults(full, path, value):
    project, _source, _requirement, program, scope = _fixture(full)
    body = _profile_body(project, program, scope)
    body = copy.deepcopy(body)
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(Fault) as rejected:
        full.invoke(
            full.owner,
            "assurance.profile_propose",
            {"project": project, "program": program, "body": body, "expected_head": None},
        )
    assert rejected.value.code == "invalid_profile"


def test_scope_reference_cannot_authorize_disabled_profile(full):
    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    body = _profile_body(
        project,
        program,
        scope,
        previous=selected["profile_ref"],
        mode="disabled",
        authority_refs=[scope["scope_ref"]],
    )
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(full.owner, project, program, body, selected["head_event"])
    assert rejected.value.code in {"invalid_profile", "unsupported_authority"}


def test_decision_authority_uses_canonical_row_kind_and_links(full):
    project, source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    decision = full.k.propose(
        full.owner,
        project,
        "decision",
        {
            "title": "Keep selected requirement",
            "statement": "The selected requirement remains authoritative.",
            "source_refs": [source["id"]],
            "target_refs": [requirement["id"]],
        },
    )
    decision = full.k.accept(full.owner, decision["id"], 1)
    body = _profile_body(
        project,
        program,
        scope,
        previous=selected["profile_ref"],
        mode="disabled",
        authority_refs=[{
            "kind": "artifact", "project": project, "artifact": decision["id"],
            "revision": decision["revision"], "body_digest": decision["digest"],
        }],
        reason="retain reviewed decision linkage",
    )
    proposal = full.assurance.profile_propose(
        full.owner, project, program, body, selected["head_event"]
    )
    assert proposal["profile"]["body"]["authority_refs"] == body["authority_refs"]


def test_pinned_change_authority_requires_material_and_archive_endpoint(full):
    project, source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    change = full.p.change(
        full.owner,
        project,
        {
            "title": "Retain requirement scope",
            "origin": "implementation",
            "reason": "The implementation needs a reviewed adjustment.",
            "affected": [requirement["id"]],
            "evidence": [source["id"]],
        },
    )
    feasibility = full.rt.review(
        full.owner, change["id"], "feasibility", "e3-assurance-fixture"
    )
    full.p.attempt(
        full.owner,
        change["id"],
        "local_repair",
        {
            "hypothesis": "The reviewed implementation adjustment is sufficient.",
            "alternatives": ["retain the current implementation"],
            "evidence": [feasibility["receipt"]],
            "outcome": "solution",
            "remaining_unknown": "",
        },
    )
    row = full.s.one("SELECT * FROM changes WHERE id=?", (change["id"],))
    pinned = full.assurance.pin(
        full.owner,
        project,
        {"kind": "change", "change": change["id"], "revision": row["revision"],
         "body_digest": digest(parse_json(row["body"]))},
    )
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    body = _profile_body(
        project,
        program,
        scope,
        previous=selected["profile_ref"],
        mode="disabled",
        authority_refs=[pinned["ref"]],
        reason="retain reviewed change linkage",
    )
    proposed = full.assurance.profile_propose(
        full.owner, project, program, body, selected["head_event"]
    )
    assert proposed["profile"]["body"]["authority_refs"] == [pinned["ref"]]

    tables = full.assurance.archive_rows(project)
    def decoded(rows):
        result = []
        for row in rows:
            value = dict(row)
            if isinstance(value.get("body"), str):
                value["body"] = parse_json(value["body"])
            result.append(value)
        return result

    external_rows = {
        "artifacts": decoded(full.s.all("SELECT * FROM artifacts WHERE project=?", (project,))),
        "revisions": decoded(full.s.all(
            "SELECT r.* FROM revisions r JOIN artifacts a ON a.id=r.artifact WHERE a.project=?",
            (project,),
        )),
        "sources": decoded(full.s.all("SELECT * FROM sources WHERE project=?", (project,))),
        "changes": decoded(full.s.all("SELECT * FROM changes WHERE project=?", (project,))),
    }
    external = {
        "artifacts": {row["id"]: row for row in external_rows["artifacts"]},
        "revisions": {
            canonical([row["artifact"], row["revision"]]).decode(): row
            for row in external_rows["revisions"]
        },
        "sources": {row["id"]: row for row in external_rows["sources"]},
        "changes": {row["id"]: row for row in external_rows["changes"]},
    }
    validate_assurance_rows(tables, project, external)
    external["changes"].pop(change["id"])
    with pytest.raises(Fault) as missing_change:
        validate_assurance_rows(tables, project, external)
    assert missing_change.value.code == "invalid_archive"


def test_profile_replay_requires_exact_event_predecessor_live_and_archive(full, monkeypatch):
    project, source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    second = full.assurance.profile_propose(
        full.owner,
        project,
        program,
        _profile_body(
            project,
            program,
            scope,
            previous=selected["profile_ref"],
            authority_refs=[_source_ref(project, source)],
            reason="second",
        ),
        selected["head_event"],
    )
    _adopt(full, project, second, selected["head_event"])

    original = full.assurance._selection_event

    def altered_event(current_project, current_program):
        event = copy.deepcopy(original(current_project, current_program))
        event_body = parse_json(event["body"]) if isinstance(event["body"], str) else event["body"]
        event_body["selection"]["previous_selection_ref"] = second["profile_ref"]
        event["body"] = canonical(event_body).decode()
        return event

    monkeypatch.setattr(full.assurance, "_selection_event", altered_event)
    with pytest.raises(Fault):
        full.assurance._validate_profile_v2_records(
            full.owner, project, second["profile"]["body"], for_adoption=True
        )

    tables = full.assurance.archive_rows(project)
    event = next(item for item in tables["assurance_events"]
                 if item["subject_id"] == second["profile"]["id"])
    event_body = parse_json(event["body"])
    event_body["selection"]["previous_selection_ref"] = second["profile_ref"]
    event["body"] = canonical(event_body).decode()
    with pytest.raises(Fault):
        validate_assurance_rows(tables, project)


def test_test_binding_uses_canonical_artifact_row_kind(full, tmp_path):
    project, _source, requirement, program, scope = _fixture(full)
    test_artifact = full.k.propose(
        full.owner,
        project,
        "test",
        {"title": "test artifact", "statement": "checks the selected requirement"},
    )
    test_artifact = full.k.accept(full.owner, test_artifact["id"], 1)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "check.py").write_text("value = 1\n")
    repository = full.sn.register(full.owner, project, "app", str(repo_root))["id"]
    task = full.w.create(
        full.owner,
        project,
        {
            "title": "binding task",
            "goal": "verify",
            "read_artifacts": [requirement["id"]],
            "write_paths": ["check.py"],
            "acceptance": ["AC-E3"],
            "dependencies": [],
            "repos": [repository],
            "non_goals": [],
        },
    )
    plan = full.w.plan_tests(
        full.owner,
        task["id"],
        {"checks": [{"id": "unit", "argv": ["true"], "kind": "pytest", "required_tests": ["unit"]}]},
    )
    pinned = full.assurance.pin(
        full.owner,
        project,
        {"kind": "test_plan", "task": task["id"], "task_revision": 1, "plan_digest": plan["digest"]},
    )
    plan_body = parse_json(full.s.one("SELECT body FROM plans WHERE task=?", (task["id"],))["body"])
    check = plan_body["checks"][0]
    body = _profile_body(project, program, scope)
    body["test_definition_bindings"] = [{
        "artifact_ref": {
            "kind": "artifact", "project": project, "artifact": test_artifact["id"],
            "revision": test_artifact["revision"], "body_digest": test_artifact["digest"],
        },
        "check_ref": {
            "kind": "test_plan_check", "project": project, "plan": pinned["ref"],
            "check_id": check["id"], "check_digest": digest(check),
        },
    }]
    proposed = full.assurance.profile_propose(full.owner, project, program, body, None)
    assert proposed["profile"]["body"]["test_definition_bindings"] == body["test_definition_bindings"]


def _applied_change_authority_fixture(full, *, withdraw=False, adopt_initial=False):
    project, source, requirement, program, _old_scope = _fixture(full)
    _register_fixture_review(full)
    selected = None
    if adopt_initial:
        first = full.assurance.profile_propose(
            full.owner, project, program, _profile_body(project, program, _old_scope), None,
        )
        _adopt(full, project, first, None)
        selected = full.assurance.selected_profile(full.owner, project, program)
    revised_body = {**requirement["body"], "statement": "The explicitly revised requirement."}
    delta = {"artifact": requirement["id"], "expected_revision": 1, "body": revised_body}
    if withdraw:
        delta["withdraw"] = True
    change = full.p.change(
        full.owner,
        project,
        {
            "title": "Revision",
            "origin": "user",
            "reason": "approved requirement update",
            "source": source["id"],
            "affected": [requirement["id"]],
            "evidence": [source["id"]],
            "deltas": [delta],
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
            "refs": [requirement["id"]],
            "requirement_affecting": True,
            "change": change["id"],
        },
    )
    full.p.respond(full.owner, decision["id"], decision["digest"],
                   "approve", "Approved requirement revision")
    review = full.rt.review(full.owner, decision["id"], "consistency", "e3-assurance-fixture")
    full.p.apply_decision(full.owner, decision["id"], review["receipt"])
    change_row = full.s.one("SELECT * FROM changes WHERE id=?", (change["id"],))
    assert change_row["stage"] == "ready_for_reimplementation"
    pinned = full.assurance.pin(
        full.owner,
        project,
        {"kind": "change", "change": change["id"], "revision": change_row["revision"],
         "body_digest": digest(parse_json(change_row["body"]))},
    )
    current = full.s.one("SELECT * FROM artifacts WHERE id=?", (requirement["id"],))
    if adopt_initial:
        scope = full.assurance.scope_propose(
            full.owner,
            project,
            {"roots": [], "selection_rules": {}, "exclusion_proposals": [],
             "authority_refs": [], "discovery_unknowns": []},
        )
        return project, program, requirement, change, pinned, scope, selected
    scope = full.assurance.scope_propose(
        full.owner,
        project,
        {"roots": [_artifact_ref(project, current)], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    return project, program, requirement, change, pinned, scope


def test_applied_change_authority_uses_history_and_applied_result(full):
    project, program, _requirement, _change, pinned, scope = _applied_change_authority_fixture(full)
    proposal = full.assurance.profile_propose(
        full.owner, project, program,
        _profile_body(project, program, scope, authority_refs=[pinned["ref"]]), None,
    )
    assert proposal["profile"]["body"]["authority_refs"] == [pinned["ref"]]


def test_applied_change_authority_rejects_forged_baseline_history(full):
    project, program, _requirement, change, _pinned, scope = _applied_change_authority_fixture(full)
    row = full.s.one("SELECT * FROM changes WHERE id=?", (change["id"],))
    tampered = parse_json(row["body"])
    tampered["baseline_refs"][0]["digest"] = "0" * 64
    # This models a retained but forged mutable change row.  Pinning it is
    # allowed to exercise the authority resolver's historical closure check;
    # the immutable revision row must still reject it.
    full.s.execute("UPDATE changes SET body=? WHERE id=?",
                   (canonical(tampered).decode(), change["id"]))
    pinned = full.assurance.pin(
        full.owner, project,
        {"kind": "change", "change": change["id"], "revision": row["revision"],
         "body_digest": digest(tampered)},
    )
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(
            full.owner, project, program,
            _profile_body(project, program, scope, authority_refs=[pinned["ref"]]), None,
        )
    assert rejected.value.code == "unsupported_authority"


def test_withdrawn_change_authority_matches_canonical_status(full):
    project, program, requirement, _change, pinned, scope, selected = _applied_change_authority_fixture(
        full, withdraw=True, adopt_initial=True,
    )
    current = full.s.one("SELECT revision,status FROM artifacts WHERE id=?", (requirement["id"],))
    assert (current["revision"], current["status"]) == (2, "withdrawn")
    assert full.assurance.resolve_pinned(full.owner, pinned["ref"])
    proposal = full.assurance.profile_propose(
        full.owner, project, program,
        _profile_body(project, program, scope, previous=selected["profile_ref"],
                      authority_refs=[pinned["ref"]]),
        selected["head_event"],
    )
    assert proposal["profile"]["body"]["authority_refs"] == [pinned["ref"]]


def test_withdrawn_change_authority_keeps_source_route(full):
    project, program, _requirement, _change, _pinned, scope, selected = _applied_change_authority_fixture(
        full, withdraw=True, adopt_initial=True,
    )
    source = full.s.one("SELECT * FROM sources WHERE project=? ORDER BY id LIMIT 1", (project,))
    source_ref = {"kind": "source", "project": project, "source": source["id"],
                  "blob_digest": source["blob"]}
    proposal = full.assurance.profile_propose(
        full.owner, project, program,
        _profile_body(project, program, scope, previous=selected["profile_ref"],
                      authority_refs=[source_ref]),
        selected["head_event"],
    )
    assert proposal["profile"]["body"]["authority_refs"] == [source_ref]


def test_withdrawn_change_authority_rejects_status_mismatch_and_keeps_pin(full):
    project, program, requirement, _change, pinned, scope, selected = _applied_change_authority_fixture(
        full, withdraw=True, adopt_initial=True,
    )
    # The mutable projection is forged to accepted while its immutable current
    # revision still records the canonical withdrawn result.
    full.s.execute("UPDATE artifacts SET status='accepted' WHERE id=?", (requirement["id"],))
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(
            full.owner, project, program,
            _profile_body(project, program, scope, previous=selected["profile_ref"],
                          authority_refs=[pinned["ref"]]),
            selected["head_event"],
        )
    assert rejected.value.code == "unsupported_authority"
    assert full.assurance.resolve_pinned(full.owner, pinned["ref"])


def test_withdrawn_change_authority_rejects_later_revision_and_forged_baseline(full):
    project, program, requirement, change, pinned, scope, selected = _applied_change_authority_fixture(
        full, withdraw=True, adopt_initial=True,
    )
    current = full.s.one("SELECT * FROM artifacts WHERE id=?", (requirement["id"],), True)
    rerevised = {**parse_json(current["body"]), "statement": "An unrelated later revision."}
    full.k._revise(full.owner, current, current["revision"], rerevised,
                   "unrelated later withdrawal revision", "accepted")
    assert full.assurance.resolve_pinned(full.owner, pinned["ref"])
    with pytest.raises(Fault) as rerevised_rejected:
        full.assurance.profile_propose(
            full.owner, project, program,
            _profile_body(project, program, scope, previous=selected["profile_ref"],
                          authority_refs=[pinned["ref"]]),
            selected["head_event"],
        )
    assert rerevised_rejected.value.code == "unsupported_authority"

    row = full.s.one("SELECT * FROM changes WHERE id=?", (change["id"],))
    tampered = parse_json(row["body"])
    tampered["baseline_refs"][0]["digest"] = "0" * 64
    full.s.execute("UPDATE changes SET body=? WHERE id=?",
                   (canonical(tampered).decode(), change["id"]))
    forged = full.assurance.pin(
        full.owner, project,
        {"kind": "change", "change": change["id"], "revision": row["revision"],
         "body_digest": digest(tampered)},
    )
    with pytest.raises(Fault) as baseline_rejected:
        full.assurance.profile_propose(
            full.owner, project, program,
            _profile_body(project, program, scope, previous=selected["profile_ref"],
                          authority_refs=[forged["ref"]]),
            selected["head_event"],
        )
    assert baseline_rejected.value.code == "unsupported_authority"


def test_applied_change_authority_rejects_unrelated_current_rerevision_but_keeps_pin(full):
    project, program, requirement, _change, pinned, scope = _applied_change_authority_fixture(full)
    current = full.s.one("SELECT * FROM artifacts WHERE id=?", (requirement["id"],), True)
    rerevised = {**parse_json(current["body"]), "statement": "An unrelated later revision."}
    full.k._revise(full.owner, current, current["revision"], rerevised,
                   "unrelated later revision", "accepted")
    assert full.assurance.resolve_pinned(full.owner, pinned["ref"])
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(
            full.owner, project, program,
            _profile_body(project, program, scope, authority_refs=[pinned["ref"]]), None,
        )
    assert rejected.value.code == "unsupported_authority"
