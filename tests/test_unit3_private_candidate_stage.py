"""Finite Runtime-to-Unit3 private candidate checkpoint checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from daikibo import candidate_provenance
from daikibo.common import Fault
from daikibo.control import Control

from test_consumer_p_mr_integration import _actual_review_adapter
from test_delivery_git_and_recovery import profile
from test_e3_selection_contract import _adopt, _artifact_ref, _register_fixture_review


def _prepare_selected_stage(full, full_project, tmp_path, *, review_plan: bool = True,
                            second_membership: bool = False,
                            omit_workflow: bool = False,
                            mature: bool = False):
    """Prepare an active profile/root before the real Runtime subprocess."""
    project, repository, requirement, root = full_project
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    partition = full.traceability.propose(
        full.owner, project, kind="document", scope={"source": source},
    )
    full.traceability.extract(full.owner, partition["id"])
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    domain = full.k.propose(
        full.owner, project, "domain",
        {"title": "Runtime domain", "statement": "The Runtime owns arithmetic.",
         "responsibilities": ["arithmetic"], "non_responsibilities": [],
         "owned_data": [], "interfaces": [], "source_refs": [source]},
    )
    domain = full.k.accept(full.owner, domain["id"], 1)
    requirement_ref = _artifact_ref(
        project, full.s.one("SELECT * FROM artifacts WHERE id=?", (requirement,), True),
    )
    task_body = {
        "title": "Candidate stage task",
        "goal": "WRITE:{\"calc.py\":\"def add(a,b):\\n    return a+b\\n\"}",
        "read_artifacts": [requirement, domain["id"]], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [],
        "structural_obligations": {
            "format": "daikibo.task-structural-obligations.v1",
            "required_outputs": [{"id": "artifact-result", "statement": "one finding",
                                   "artifact_refs": [requirement_ref],
                                   "realization_kind": "artifact"}],
            "required_exercises": [],
        },
    }
    # workflow_id is an optional public Task field.  The private candidate
    # gate must derive its authority from the active Breakdown membership.
    if not omit_workflow:
        task_body["workflow_id"] = program
    task = full.w.create(full.owner, project, task_body)
    full.w.plan_tests(full.owner, task["id"], {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    units = [{"id": "runtime-unit", "title": "runtime", "parent": None,
              "domain": domain["id"], "rationale": "runtime owner",
              "obligations": [{"requirement": requirement, "acceptance": "AC-ADD"}],
              "tasks": [task["id"]], "interfaces": [], "dependencies": []}]
    breakdown = full.breakdowns.propose(
        full.owner, program, "Candidate stage root", "candidate stage fixture", units,
    )
    _actual_review_adapter(full, tmp_path)
    page = full.breakdowns.get(full.owner, breakdown["id"])
    for packet in page["packets"]:
        for role in ("design", "trace"):
            full.rt.review(full.owner, packet["id"], role, "p-mr-markers")

    requirement_row = full.s.one(
        "SELECT * FROM artifacts WHERE id=? AND project=?", (requirement, project), True,
    )
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [_artifact_ref(project, requirement_row)], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    relation_set = {"relation": "produced_by", "direction": "incoming",
                    "centers": ["assigned_tasks"]}
    stages = {
        stage: {"denominator": denominator, "relation_sets": [relation_set],
                "node_rules": ["test-plan"], "execution_results": execution}
        for stage, denominator, execution in (
            ("plan", "program_plan", "none"),
            ("task", "assigned_task_contributors", "assigned_checks"),
            ("integration", "program_integration", "integration_checks"),
            ("delivery", "actual_delivery", "certified_integration_and_actual_outputs"),
        )
    }
    body = {
        "format": "assurance.profile.v2", "project": project, "program": program,
        "scope_ref": scope["scope_ref"], "obligations_ref": scope["obligations_ref"],
        "previous_selection_ref": None, "application_mode": "mandatory",
        "stage_rules": stages,
        "node_review_rules": [{"id": "test-plan", "selector": "test_plan", "roles": ["test_plan"]}],
        "relation_selectors": ["produced_by"], "test_definition_bindings": [],
        "change_reason": "private candidate stage fixture", "authority_refs": [],
    }
    _register_fixture_review(full)
    proposed = full.assurance.profile_propose(full.owner, project, program, body, None)
    _adopt(full, project, proposed, None)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task["id"],), True)
    # Unit4-P is now consumed at the public breakdown writer.  Seed the
    # current plan observation so the canonical profile can admit the root;
    # the review_plan=False branch removes this observation after activation
    # to retain the missing-current-proof candidate regression.
    full.rt.review(
        full.owner, task["id"], "test_plan", "p-mr-markers",
        proposal=json.loads(plan_row["body"]),
    )
    full.breakdowns.activate(full.owner, breakdown["id"])
    if second_membership:
        second_program = full.p.begin(full.owner, project, source, compact=True)["program"]
        second_breakdown = full.breakdowns.propose(
            full.owner, second_program, "Second candidate root", "multi-program fixture", units,
        )
        second_page = full.breakdowns.get(full.owner, second_breakdown["id"])
        for packet in second_page["packets"]:
            for role in ("design", "trace"):
                full.rt.review(full.owner, packet["id"], role, "p-mr-markers")
        second_body = dict(body)
        second_body["program"] = second_program
        second_body["change_reason"] = "second private candidate stage fixture"
        second_proposed = full.assurance.profile_propose(
            full.owner, project, second_program, second_body, None,
        )
        _adopt(full, project, second_proposed, None)
        full.breakdowns.activate(full.owner, second_breakdown["id"])
    if mature:
        # Unit4-R's current-root contract is phase-bound.  Keep the historical
        # Unit3 candidate fixture preimplementation by default, while letting
        # admission tests opt into the public phase progression that supplies
        # the exact requirements-through-plan history before ready/claim.
        scenario = full.k.propose(
            full.owner, project, "scenario",
            {"title": "Runtime scenario", "statement": "An observed input pair produces its sum.",
             "source_refs": [source]},
        )
        full.k.accept(full.owner, scenario["id"], scenario["revision"])
        interface = full.k.propose(
            full.owner, project, "interface",
            {"title": "Runtime interface", "statement": "The Runtime exposes the selected arithmetic result.",
             "input": "two integers", "output": "one integer", "authentication": "none",
             "errors": "invalid values are rejected", "idempotency": "same inputs have the same result",
             "compatibility": "existing callers remain compatible", "consumers": [],
             "verification": "test_calc.py", "source_refs": [source]},
        )
        full.k.accept(full.owner, interface["id"], interface["revision"])
        finding = full.k.propose(
            full.owner, project, "finding",
            {"title": "Runtime feasibility", "statement": "The bounded arithmetic experiment completed.",
             "source_refs": [source]},
        )
        full.k.accept(full.owner, finding["id"], finding["revision"])
        design = full.k.propose(
            full.owner, project, "design",
            {"title": "Runtime design", "statement": "The selected Runtime Task preserves the accepted result.",
             "source_refs": [source]},
        )
        full.k.accept(full.owner, design["id"], design["revision"])
        verification = full.k.propose(
            full.owner, project, "test",
            {"title": "Runtime verification", "statement": "The measured test checks the accepted arithmetic result.",
             "source_refs": [source]},
        )
        full.k.accept(full.owner, verification["id"], verification["revision"])
        full.k.link(
            full.owner, design["id"], requirement, "realizes", "asserted",
            "The Runtime design preserves the accepted requirement.",
        )
        full.k.link(
            full.owner, verification["id"], requirement, "verifies", "asserted",
            "The Runtime test covers the accepted requirement.",
        )
        full.idx.index(full.owner, repository)
        full.idx.search(full.owner, project, "add")
        mature_programs = [program]
        if second_membership:
            mature_programs.append(second_program)
        phase_profile = profile(project, repository, requirement, task["id"])
        phase_profile["program"] = program
        full.d.configure(full.owner, project, phase_profile)
        for mature_program in mature_programs:
            while full.p.next(full.owner, mature_program)["phase"] != "implementation":
                current = full.p.next(full.owner, mature_program)
                receipt = full.rt.review(
                    full.owner, mature_program, "phase", "fixture",
                )["receipt"]
                full.p.advance(
                    full.owner, mature_program, current["revision"], receipt,
                )
    full.w.ready(full.owner, task["id"])
    claimed = full.w.claim(full.owner, project, task["id"])
    if not review_plan or second_membership:
        # Unit4-R checks the current task checkpoint before ready/claim, so
        # retain the normal admission proof through those writer boundaries.
        # The negative candidate tests then remove the durable test-plan
        # observation before Runtime's later candidate checkpoint.  Runtime
        # observations remain intact and the stage reader must report the
        # missing current test-plan receipt; for the multi-membership case
        # the AND reader still evaluates both roots before rejecting.
        full.s.execute("DROP TRIGGER receipts_no_delete")
        full.s.execute(
            "DELETE FROM receipts WHERE subject=? AND role='test_plan'",
            (task["id"],),
        )
    return {"project": project, "program": program, "task": task["id"],
            "repository": repository, "claimed": claimed}


def test_runtime_private_stage_allows_deferred_future_and_precedes_insert(
    full, full_project, tmp_path, monkeypatch,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, omit_workflow=True, mature=True,
    )
    captured = {}
    original = candidate_provenance.resolve_preadoption_observation
    import daikibo.assurance_stage as stage_module
    original_stage = stage_module._evaluate_pre_adoption

    def capture(control, actor, observation):
        identity = original(control, actor, observation)
        captured["identity"] = identity
        return identity

    monkeypatch.setattr(candidate_provenance, "resolve_preadoption_observation", capture)

    def inspect_stage(control, actor, identity):
        assert control.s.one("SELECT count(*) AS n FROM candidates")["n"] == 0
        assert control.s.one("SELECT status FROM tasks WHERE id=?", (flow["task"],))["status"] == "running"
        captured["stage"] = original_stage(control, actor, identity)
        return captured["stage"]

    monkeypatch.setattr(stage_module, "_evaluate_pre_adoption", inspect_stage)
    result = full.rt.execute(full.owner, flow["task"], "fixture")
    assert result["status"] == "submitted"
    assert captured["identity"]["task"] == flow["task"]
    assert captured["stage"]["assurance_allow"] is True
    assert captured["stage"]["strong_complete"] is False
    task_body = json.loads(full.s.one("SELECT body FROM tasks WHERE id=?", (flow["task"],))["body"])
    assert "workflow_id" not in task_body
    import daikibo.assurance_stage as stage_module
    assert stage_module._active_task_programs(full, flow["project"], flow["task"]) == [flow["program"]]
    assert full.s.one("SELECT status FROM tasks WHERE id=?", (flow["task"],))["status"] == "submitted"
    assert full.s.one("SELECT count(*) AS n FROM candidates WHERE task=?", (flow["task"],))["n"] == 1


def test_runtime_private_stage_rejects_current_missing_and_retains_observation(
    full, full_project, tmp_path, monkeypatch,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, review_plan=False,
        omit_workflow=True, mature=True,
    )
    original = candidate_provenance.resolve_preadoption_observation
    captured = {}

    def capture(control, actor, observation):
        identity = original(control, actor, observation)
        captured["identity"] = identity
        return identity

    monkeypatch.setattr(candidate_provenance, "resolve_preadoption_observation", capture)
    with pytest.raises(Fault) as failure:
        full.rt.execute(full.owner, flow["task"], "fixture")
    assert failure.value.code == "stage_assurance_blocked"
    assert full.s.one("SELECT candidate FROM tasks WHERE id=?", (flow["task"],))["candidate"] is None
    assert full.s.one("SELECT status FROM tasks WHERE id=?", (flow["task"],))["status"] == "running"
    receipt = full.s.one(
        "SELECT id,body FROM receipts WHERE subject=? ORDER BY created DESC,id DESC LIMIT 1",
        (flow["task"],), True,
    )
    assert receipt is not None
    assert json.loads(receipt["body"])["work_product"] is not None
    assert captured["identity"]["task"] == flow["task"]

    # The stage rejection is inside the adoption transaction, while the
    # Runtime observation was already committed.  Reopen the same control
    # home to prove a fresh composition root can read that retained material.
    home = full.s.home
    full.close()
    reopened = Control(home, mode="validation", start_workers=False)
    try:
        retained = reopened.s.one(
            "SELECT id,body FROM receipts WHERE subject=? ORDER BY created DESC,id DESC LIMIT 1",
            (flow["task"],), True,
        )
        assert retained is not None
        assert json.loads(retained["body"])["work_product"] is not None
    finally:
        reopened.close()


def test_runtime_private_stage_ands_all_active_program_memberships(
    full, full_project, tmp_path, monkeypatch,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, second_membership=True,
        omit_workflow=True, mature=True,
    )
    import daikibo.assurance_stage as stage_module

    original = stage_module._evaluate_pre_adoption
    captured = {}

    def inspect(control, actor, identity):
        try:
            return original(control, actor, identity)
        except Fault as exc:
            captured["details"] = exc.details
            raise

    monkeypatch.setattr(stage_module, "_evaluate_pre_adoption", inspect)
    with pytest.raises(Fault) as rejected:
        full.rt.execute(full.owner, flow["task"], "fixture")
    assert rejected.value.code == "stage_assurance_blocked"
    result = captured["details"]["result"]
    assert flow["program"] in result["membership"]["all_programs_evaluated"]
    assert len(set(result["membership"]["all_programs_evaluated"])) == 2
    assert len(result["program_evaluations"]) == 2
    assert result["assurance_allow"] is False
    assert full.s.one("SELECT candidate FROM tasks WHERE id=?", (flow["task"],))["candidate"] is None
    assert full.s.one("SELECT status FROM tasks WHERE id=?", (flow["task"],))["status"] == "running"


def test_private_stage_identity_rejects_a_different_control(full, full_project, tmp_path, monkeypatch):
    flow = _prepare_selected_stage(full, full_project, tmp_path, mature=True)
    captured = {}
    original = candidate_provenance.resolve_preadoption_observation

    def capture(control, actor, observation):
        identity = original(control, actor, observation)
        captured["identity"] = identity
        return identity

    monkeypatch.setattr(candidate_provenance, "resolve_preadoption_observation", capture)
    full.rt.execute(full.owner, flow["task"], "fixture")

    other = Control(tmp_path / "other-control", mode="validation", start_workers=False)
    other.owner = other.sec.authenticate(Path(other.sec.bootstrap()).read_text())
    try:
        import daikibo.assurance_stage as stage_module
        with pytest.raises(Fault) as rejected:
            stage_module._evaluate_pre_adoption(other, other.owner, captured["identity"])
        assert rejected.value.code == "invalid_preadoption"
    finally:
        other.close()
