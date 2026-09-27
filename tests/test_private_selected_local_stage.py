"""Runtime candidate enforcement for a selected local proposal without a root adoption."""
from __future__ import annotations

import json

import pytest

from daikibo.common import Fault
from daikibo import assurance_stage as stage

from test_consumer_p_mr_integration import _actual_review_adapter
from test_e3_selection_contract import _adopt, _artifact_ref, _register_fixture_review
from test_local_execution import _claim_local, local_unadopted_case
from test_reviewed_breakdowns import setup


def _select_local_profile(case):
    control = case["c"]
    project = case["project"]
    program = case["program"]
    selected = control.assurance.selected_profile(control.owner, project, program)
    # The shared local fixture now performs the same canonical profile
    # bootstrap as a new program before composing its root.  Keep this helper
    # idempotent so the focused private-route tests can still call it at their
    # historical point without proposing a second profile head.
    if selected.get("profile_ref") is not None:
        return {"profile": selected["profile_ref"], "scope": selected.get("scope_ref")}
    requirement_rows = [
        control.s.one("SELECT * FROM artifacts WHERE id=?", (ident,), True)
        for ident in case["requirements"]
    ]
    scope = control.assurance.scope_propose(
        control.owner,
        project,
        {
            "roots": [_artifact_ref(project, row) for row in requirement_rows],
            "selection_rules": {},
            "exclusion_proposals": [],
            "authority_refs": [],
            "discovery_unknowns": [],
        },
    )
    relation_set = {
        "relation": "produced_by",
        "direction": "incoming",
        "centers": ["assigned_tasks"],
    }
    stages = {
        name: {
            "denominator": denominator,
            "relation_sets": [relation_set],
            "node_rules": ["plan-review"],
            "execution_results": execution,
        }
        for name, denominator, execution in (
            ("plan", "program_plan", "none"),
            ("task", "assigned_task_contributors", "assigned_checks"),
            ("integration", "program_integration", "integration_checks"),
            ("delivery", "actual_delivery", "certified_integration_and_actual_outputs"),
        )
    }
    body = {
        "format": "assurance.profile.v2",
        "project": project,
        "program": program,
        "scope_ref": scope["scope_ref"],
        "obligations_ref": scope["obligations_ref"],
        "previous_selection_ref": None,
        "application_mode": "mandatory",
        "stage_rules": stages,
        "node_review_rules": [{"id": "plan-review", "selector": "test_plan", "roles": ["test_plan"]}],
        "relation_selectors": ["produced_by"],
        "test_definition_bindings": [],
        "change_reason": "selected local candidate stage fixture",
        "authority_refs": [],
    }
    _register_fixture_review(control)
    proposal = control.assurance.profile_propose(control.owner, project, program, body, None)
    _adopt(control, project, proposal, None)
    return {"profile": proposal, "scope": scope}


def _review_task_plan(case):
    control = case["c"]
    plan = control.s.one("SELECT body FROM plans WHERE task=?", (case["task"],), True)
    control.rt.review(
        control.owner,
        case["task"],
        "test_plan",
        "p-mr-markers",
        proposal=json.loads(plan["body"]),
    )


def test_selected_local_claim_enforces_missing_task_proof_without_active_root(
    local_unadopted_case, monkeypatch,
):
    case = local_unadopted_case
    control = case["c"]
    _claim_local(case)
    # The migrated local fixture now records a valid current plan receipt as
    # part of canonical root review.  Remove that durable observation here to
    # retain the original missing-proof negative case; the stage reader must
    # reject the claim without manufacturing a receipt.
    control.s.execute("DROP TRIGGER receipts_no_delete")
    control.s.execute(
        "DELETE FROM receipts WHERE subject=? AND role='test_plan'",
        (case["task"],),
    )
    _select_local_profile(case)

    assert stage._active_task_programs(control, case["project"], case["task"]) == []
    assert control.local_executions.claimed(case["task"]) is not None
    assert control.assurance.selected_profile(
        control.owner, case["project"], case["program"],
    )["application_mode"] == "mandatory"

    captured = {}
    original = stage._evaluate_pre_adoption

    def inspect(controller, actor, identity):
        try:
            captured["result"] = original(controller, actor, identity)
            return captured["result"]
        except Fault as exc:
            captured["result"] = exc.details["result"]
            raise

    monkeypatch.setattr(stage, "_evaluate_pre_adoption", inspect)
    with pytest.raises(Fault) as failure:
        control.rt.execute(control.owner, case["task"], "fixture")

    assert failure.value.code == "stage_assurance_blocked"
    result = captured["result"]
    assert result["membership"]["all_programs_evaluated"] == [case["program"]]
    assert result["local_execution"]["status"] == "satisfied"
    assert any(item.get("reason") == "no_current_receipt" for item in result["failures"])
    assert not any(item.get("reason") == "Unknown local authorization stage"
                   for item in result["failures"])
    assert control.s.one(
        "SELECT status,candidate FROM tasks WHERE id=?", (case["task"],), True,
    ) == {"status": "running", "candidate": None}


def test_selected_local_claim_accepts_current_task_proof_without_active_root(
    local_unadopted_case, monkeypatch, tmp_path,
):
    case = local_unadopted_case
    control = case["c"]
    _claim_local(case)
    _select_local_profile(case)
    _actual_review_adapter(control, tmp_path)
    _review_task_plan(case)

    assert stage._active_task_programs(control, case["project"], case["task"]) == []
    captured = {}
    original = stage._evaluate_pre_adoption

    def inspect(controller, actor, identity):
        try:
            captured["result"] = original(controller, actor, identity)
            return captured["result"]
        except Fault as exc:
            captured["result"] = exc.details["result"]
            raise

    monkeypatch.setattr(stage, "_evaluate_pre_adoption", inspect)
    result = control.rt.execute(control.owner, case["task"], "fixture")

    assert result["status"] == "submitted"
    assert captured["result"]["membership"]["all_programs_evaluated"] == [case["program"]]
    assert captured["result"]["local_execution"]["status"] == "satisfied"
    assert captured["result"]["relations"]["status"] == "deferred"
    assert captured["result"]["failures"] == []
    assert control.s.one(
        "SELECT status,candidate FROM tasks WHERE id=?", (case["task"],), True,
    )["status"] == "submitted"
    assert control.s.one(
        "SELECT candidate FROM tasks WHERE id=?", (case["task"],), True,
    )["candidate"] is not None
