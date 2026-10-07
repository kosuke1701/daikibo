"""Finite Unit4-R Task admission reader and writer-boundary checks."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from daikibo.common import Fault
from daikibo.governance import Governance
from daikibo.workflow import Workflow
from daikibo.unit4_enforcement import inspect_task_admission
from test_delivery_git_and_recovery import profile
from test_e3_selection_contract import _adopt
from test_local_execution import (
    _dispositions,
    _adopt_current_artifact_produced_by,
    _claim_local,
    _local_review_script,
    _managed,
    _make_local_case,
    _pages,
    _review_local_packets,
)
from test_reviewed_breakdowns import review_all
from test_unit3_private_candidate_stage import _prepare_selected_stage


def _advance_mixed_owner_programs(full, case, programs):
    """Advance every active owner through the public planning prefix."""
    phase_profile = profile(
        case["project"], case["repo"], case["requirement"], case["task"],
    )
    phase_profile["required_requirements"] = case["requirements"]
    phase_profile["required_tasks"] = [case["task"], case["analysis"]]
    phase_profile["program"] = programs[0]
    full.d.configure(full.owner, case["project"], phase_profile)
    for program in programs:
        while full.p.next(full.owner, program)["phase"] != "implementation":
            current = full.p.next(full.owner, program)
            receipt = full.rt.review(
                full.owner, program, "phase", "fixture",
            )["receipt"]
            full.p.advance(
                full.owner, program, current["revision"], receipt,
            )


def _refresh_mixed_local_proposal(full, case, tmp_path, request_id):
    """Re-propose B after a public material change using fresh inventory."""
    pages = _pages(
        full, full.local_executions.inventory, case["program"], case["partial"],
        [case["task"]], limit=2,
    )
    items = [item for page in pages for item in page["items"]]
    assert items and len(items) == pages[0]["total"]
    dispositions = _dispositions(case["task"], items, case["requirement"])
    adapter_script = _local_review_script(
        tmp_path,
        [f"disposition:{case['task']}:{item['id']}" for item in items],
    )
    disposition_file = adapter_script.with_name("local_review_disposition_ids.json")
    full.rt.adapters.register(
        full.owner,
        "local-review-fixture",
        "fixture",
        sys.executable,
        [str(adapter_script), str(disposition_file)],
    )
    proposal = full.local_executions.propose(
        full.owner,
        case["program"],
        case["partial"],
        [case["task"]],
        "Refresh the current local B proposal after the retained root material change.",
        copy.deepcopy(case["stage"]),
        dispositions,
        byte_budget=24000,
        request_id=request_id,
    )
    refreshed = dict(case)
    refreshed.update(items=items, dispositions=dispositions, proposal=proposal)
    return refreshed


def test_task_admission_uses_active_membership_and_is_readonly(full, full_project, tmp_path):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, omit_workflow=True, mature=True,
    )
    task = flow["task"]
    ready_gate = full.s.one(
        "SELECT body FROM gate_results WHERE subject=? AND gate='ready' "
        "ORDER BY created DESC,id DESC LIMIT 1",
        (task,), True,
    )
    ready_body = json.loads(ready_gate["body"])
    assert ready_body["task_admission"]["checkpoint"] == "ready"
    assert ready_body["task_admission"]["allowed"] is True
    before_changes = full.s.conn.total_changes
    before_gates = full.s.one(
        "SELECT count(*) AS n FROM gate_results WHERE subject=?", (task,), True,
    )["n"]

    result = inspect_task_admission(
        full, full.owner, task=task, checkpoint="claim",
    )

    assert result["allowed"] is True
    assert result["task"]["kind"] == "task_revision"
    assert result["canonical_programs"] == [flow["program"]]
    branch = result["programs"][0]
    assert branch["program"] == flow["program"]
    assert branch["route"] == "root"
    assert branch["origin"]["program"] == flow["program"]
    assert branch["selection"]["application_mode"] == "mandatory"
    assert branch["evaluation"]["assurance_allow"] is True
    assert result["semantic_fingerprint"] and result["report_snapshot"]
    assert full.s.conn.total_changes == before_changes
    assert full.s.one(
        "SELECT count(*) AS n FROM gate_results WHERE subject=?", (task,), True,
    )["n"] == before_gates


def test_task_admission_rejects_immature_root_without_local_authority(
    full, full_project, tmp_path, monkeypatch,
):
    """An omitted workflow_id cannot bypass a preimplementation root owner."""
    class ReadyStop(Exception):
        pass

    identifiers = {}

    def stop_before_ready(actor, task):
        identifiers["task"] = task
        raise ReadyStop()

    with monkeypatch.context() as patched:
        patched.setattr(full.w, "ready", stop_before_ready)
        with pytest.raises(ReadyStop):
            _prepare_selected_stage(
                full, full_project, tmp_path, omit_workflow=True,
            )

    task = identifiers["task"]
    before = full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (task,), True,
    )
    before_changes = full.s.conn.total_changes
    result = inspect_task_admission(
        full, full.owner, task=task, checkpoint="claim",
    )
    assert result["allowed"] is False
    assert any(
        failure.get("code") == "root_execution_not_current"
        for failure in result["failures"]
    )
    assert full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (task,), True,
    ) == before
    assert full.s.conn.total_changes == before_changes


def test_task_workflow_id_does_not_create_private_membership(full, full_project):
    project, repository, requirement, _root = full_project
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,), True)["id"]
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    task = full.w.create(
        full.owner,
        project,
        {
            "title": "Unassigned workflow compatibility Task",
            "goal": "WRITE:" + json.dumps({"result.txt": "unassigned\n"}),
            "read_artifacts": [requirement],
            "write_paths": ["result.txt"],
            "acceptance": ["AC-ADD"],
            "dependencies": [],
            "repos": [repository],
            "non_goals": [],
            "workflow_id": program,
        },
    )

    result = inspect_task_admission(
        full, full.owner, task=task["id"], checkpoint="ready",
    )

    assert result["canonical_programs"] == []
    assert result["allowed"] is True
    assert result["reason"] == "task_admission_not_applicable"


def test_task_admission_rejects_invalidated_current_plan_without_writes(
    full, full_project, tmp_path,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, omit_workflow=True, mature=True,
    )
    task = flow["task"]
    # This is a real retained-review invalidation.  It is performed after the
    # normal public ready/claim path so the reader must recheck current
    # material instead of trusting the earlier gate result.
    full.s.execute("DROP TRIGGER receipts_no_delete")
    full.s.execute(
        "DELETE FROM receipts WHERE subject=? AND role='test_plan'", (task,),
    )
    before_changes = full.s.conn.total_changes
    before = full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (task,), True,
    )

    result = inspect_task_admission(
        full, full.owner, task=task, checkpoint="claim",
    )

    assert result["allowed"] is False
    assert result["failures"]
    assert any(
        item.get("code") == "no_current_receipt" or
        item.get("reason") == "no_current_receipt"
        for item in result["failures"]
    )
    after = full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (task,), True,
    )
    assert after == before
    assert full.s.conn.total_changes == before_changes


def test_task_admission_ands_all_active_memberships_without_workflow_population(
    full, full_project, tmp_path,
):
    """Every active root owner remains required when workflow_id is omitted."""
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, second_membership=True,
        omit_workflow=True, mature=True,
    )
    before_changes = full.s.conn.total_changes
    result = inspect_task_admission(
        full, full.owner, task=flow["task"], checkpoint="claim",
    )
    assert result["canonical_programs"] == sorted({
        row["program"]
        for row in full.s.all(
            "SELECT program FROM breakdowns WHERE project=? AND status='active'",
            (flow["project"],),
        )
    })
    assert len(result["canonical_programs"]) == 2
    assert all(branch["root"]["active"] for branch in result["programs"])
    assert all(branch["evaluation"] is not None for branch in result["programs"])
    assert result["allowed"] is False
    assert all(branch["failures"] for branch in result["programs"])
    assert full.s.conn.total_changes == before_changes


def _prepare_mixed_owner_admission_case(full, tmp_path):
    """Return a mature current-root-A/current-local-B admission fixture."""
    full.rt.adapters.register(
        full.owner,
        "fixture",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("fixture_agent.py"))],
    )
    marker_script = tmp_path / "unit4_r_marker_review.py"
    marker_script.write_text(
        "import json,sys\n"
        "packet=json.load(sys.stdin); context=packet.get('context',{})\n"
        "print(json.dumps({'verdict':'pass','rationale':'fixture marker',"
        "'covered':context.get('required_coverage') or context.get('task',{}).get('acceptance',[]),"
        "'findings':[],"
        "'observations':[{'ref':packet.get('subject','packet'),'detail':'fixture marker'}],"
        "'dispositions':[]}))\n",
        encoding="utf-8",
    )
    full.rt.adapters.register(
        full.owner, "markers", "fixture", sys.executable, [str(marker_script)],
    )
    case = _make_local_case(
        (full, None, None, None, None, None, None, []),
        tmp_path,
        activate_root=False,
        include_structural_output=True,
        mature_material=True,
    )
    root_audit = full.breakdowns.audit(full.owner, case["root"])
    assert root_audit["current"] is True, json.dumps(root_audit, default=str)

    project = case["project"]
    program_b = case["program"]
    source = full.s.one(
        "SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (project,), True,
    )["id"]
    program_a = full.p.begin(full.owner, project, source, compact=True)["program"]
    root_b = full.s.one(
        "SELECT id,body FROM breakdowns WHERE id=?", (case["root"],), True,
    )
    root_body = json.loads(root_b["body"])
    root_interface = full.k.propose(
        full.owner,
        project,
        "interface",
        {
            "title": "Root A arithmetic contract",
            "statement": "The root A owner preserves the selected arithmetic boundary.",
            "input": "two integers",
            "output": "one integer",
            "authentication": "none",
            "errors": "invalid values are rejected",
            "idempotency": "same inputs have the same result",
            "compatibility": "existing callers remain compatible",
            "consumers": [],
            "verification": "test_calc.py",
            "source_refs": [source],
        },
    )
    full.k.accept(full.owner, root_interface["id"], root_interface["revision"])
    # This input belongs only to the canonical A root.  B's local subplan does
    # not claim the interface, allowing the stale-root negative to refresh B's
    # public local proposal after the interface is revised.
    for unit in root_body["units"]:
        if unit["tasks"]:
            unit["interfaces"] = [root_interface["id"]]
            break
    case = _refresh_mixed_local_proposal(
        full, case, tmp_path, "local-mixed-root-interface-1",
    )
    proposed = full.breakdowns.propose(
        full.owner,
        program_a,
        "Second canonical root",
        "Public fixture for a root owner alongside a local owner.",
        root_body["units"],
    )

    # The scope is shared, while profile selection is per program.  Reuse the
    # selected B profile body through the public object reader and adopt a
    # separate A selection before reviewing the new root.
    selected_b = full.assurance.selected_profile(full.owner, project, program_b)
    profile_b = full.assurance.object_get(
        full.owner, project, selected_b["profile_ref"]["object"],
    )
    profile_body = copy.deepcopy(profile_b["body"])
    profile_body["program"] = program_a
    profile_body["change_reason"] = "Second canonical root public fixture"
    profile_a = full.assurance.profile_propose(
        full.owner, project, program_a, profile_body, None,
    )
    _adopt(full, project, profile_a, None)
    review_all(full, proposed["id"])
    full.breakdowns.activate(full.owner, proposed["id"])

    _advance_mixed_owner_programs(full, case, [program_a])
    _review_local_packets(case)
    full.local_executions.certify(
        full.owner, case["proposal"]["id"], case["proposal"]["digest"],
    )
    # Keep the Task at the public ready boundary.  The admission reader must
    # prove both owners without creating a claim or changing Task counters.
    full.w.ready(full.owner, case["task"])

    case["root_interface"] = root_interface["id"]
    return {
        "case": case,
        "program_a": program_a,
        "program_b": program_b,
        "task": case["task"],
        "root_interface": root_interface["id"],
    }


def _revise_root_interface_through_public_change(full, case):
    """Revise A-only contract material through the normal change workflow."""
    current = full.k.artifact(full.owner, case["root_interface"])
    source = full.s.one(
        "SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1",
        (case["project"],), True,
    )["id"]
    verification = full.s.one(
        "SELECT id FROM artifacts WHERE project=? AND kind='test' AND status='accepted' ORDER BY id LIMIT 1",
        (case["project"],), True,
    )["id"]
    changed = copy.deepcopy(current["body"])
    changed["statement"] = "The root A owner preserves the revised arithmetic boundary."
    changed["change_control"] = {
        "consumer_impact": {},
        "verification_ids": [verification],
        "repository_order": [case["repo"]],
        "migration": "No data migration; retain the arithmetic result.",
    }
    change = full.p.change(
        full.owner,
        case["project"],
        {
            "title": "Revise root A arithmetic contract",
            "origin": "design",
            "reason": "The root A contract received a reviewed clarification.",
            "affected": [case["root_interface"]],
            "evidence": [source],
            "deltas": [{
                "artifact": case["root_interface"],
                "expected_revision": current["revision"],
                "body": changed,
            }],
        },
    )
    feasibility = full.rt.review(
        full.owner, change["id"], "feasibility", "fixture",
    )
    full.p.attempt(
        full.owner,
        change["id"],
        "local_repair",
        {
            "hypothesis": "Apply the reviewed root A contract clarification.",
            "alternatives": ["Retain the previous root A contract."],
            "evidence": [feasibility["receipt"]],
            "outcome": "solution",
            "remaining_unknown": "",
        },
    )
    consistency = full.rt.review(
        full.owner, change["id"], "consistency", "fixture",
    )
    return full.p.apply_technical_change(
        full.owner, change["id"], consistency["receipt"],
    )


def test_task_admission_reads_active_root_a_and_local_b_owners(
    full, tmp_path,
):
    """A public local proposal cannot hide a second active root owner."""
    prepared = _prepare_mixed_owner_admission_case(full, tmp_path)
    case = prepared["case"]
    program_a = prepared["program_a"]
    program_b = prepared["program_b"]
    result = inspect_task_admission(
        full, full.owner, task=case["task"], checkpoint="claim",
    )
    assert result["allowed"] is True, json.dumps(result, default=str)
    assert result["canonical_programs"] == sorted([program_a, program_b])
    routes = {branch["program"]: branch["route"] for branch in result["programs"]}
    assert routes == {program_a: "root", program_b: "local"}
    roots = {branch["program"]: branch["root"] for branch in result["programs"]}
    assert roots[program_a]["active"] and roots[program_a]["current"]
    assert roots[program_b]["active"] is False
    assert result["local_selection"]["program"] == program_b


def test_task_admission_ands_active_root_with_withdrawn_local_owner(
    full, tmp_path,
):
    """A withdrawn local B cannot disappear while current root A remains."""
    prepared = _prepare_mixed_owner_admission_case(full, tmp_path)
    case = prepared["case"]
    program_a = prepared["program_a"]
    program_b = prepared["program_b"]
    before = full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (case["task"],), True,
    )
    withdrawn = full.local_executions.withdraw(
        full.owner,
        case["proposal"]["id"],
        case["proposal"]["digest"],
        "Withdraw the local B authority for the stale-owner negative fixture.",
    )
    assert withdrawn["withdrawn"] is True

    result = inspect_task_admission(
        full, full.owner, task=case["task"], checkpoint="claim",
    )
    assert result["allowed"] is False, json.dumps(result, default=str)
    branches = {branch["program"]: branch for branch in result["programs"]}
    assert branches[program_a]["root"]["active"] is True
    assert branches[program_a]["root"]["current"] is True
    assert branches[program_a]["route"] == "root"
    assert branches[program_b]["root"]["active"] is False
    assert branches[program_b]["route"] == "local"
    assert branches[program_b]["failures"]
    assert result["local_selection"]["authorization"]["allowed"] is False
    assert "local_execution_withdrawn" in result["local_selection"]["authorization"]["failures"]
    assert full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (case["task"],), True,
    ) == before


def test_task_admission_ands_stale_root_a_with_current_local_b(
    full, tmp_path,
):
    """A stale A root remains mandatory after B is refreshed and current."""
    prepared = _prepare_mixed_owner_admission_case(full, tmp_path)
    case = prepared["case"]
    program_a = prepared["program_a"]
    program_b = prepared["program_b"]
    before = full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (case["task"],), True,
    )
    applied = _revise_root_interface_through_public_change(full, case)
    assert applied["reassessment_required"] is True
    refreshed = _refresh_mixed_local_proposal(
        full, case, tmp_path, "local-mixed-after-root-stale-1",
    )
    _review_local_packets(refreshed)
    certified = full.local_executions.certify(
        full.owner, refreshed["proposal"]["id"], refreshed["proposal"]["digest"],
    )
    assert certified["current"] is True

    result = inspect_task_admission(
        full, full.owner, task=case["task"], checkpoint="claim",
    )
    assert result["allowed"] is False, json.dumps(result, default=str)
    branches = {branch["program"]: branch for branch in result["programs"]}
    assert branches[program_a]["root"]["active"] is True
    assert branches[program_a]["root"]["current"] is False
    assert branches[program_a]["route"] == "root"
    assert branches[program_a]["failures"]
    assert branches[program_b]["root"]["active"] is False
    assert branches[program_b]["route"] == "local"
    assert branches[program_b]["local"]["authorization"]["allowed"] is True
    assert branches[program_b]["allowed"] is True
    assert full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (case["task"],), True,
    ) == before


def _claim_writer_snapshot(full, task):
    return {
        "task": full.s.one(
            "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
            (task,), True,
        ),
        "local_claims": full.s.all(
            "SELECT id,proposal,task,epoch,digest,body FROM local_execution_records "
            "WHERE task=? AND kind='claimed' ORDER BY id", (task,),
        ),
        "attempts": full.s.all(
            "SELECT id,attempt_epoch,attempt_ordinal,status,body,digest FROM execution_attempts "
            "WHERE task=? ORDER BY attempt_epoch,id", (task,),
        ),
        "execution_control_events": full.s.one(
            "SELECT count(*) AS n FROM execution_control_events", (), True,
        )["n"],
        "events": full.s.one("SELECT count(*) AS n FROM events", (), True)["n"],
        "outbox": full.s.one("SELECT count(*) AS n FROM outbox", (), True)["n"],
    }


def test_mixed_owner_workflow_claim_persists_local_responsibility(full, tmp_path):
    """Workflow.claim binds local B even while the ready route is root A."""
    prepared = _prepare_mixed_owner_admission_case(full, tmp_path)
    case = prepared["case"]
    task = prepared["task"]
    admission = inspect_task_admission(
        full, full.owner, task=task, checkpoint="claim",
    )
    assert admission["allowed"] is True, json.dumps(admission, default=str)
    branches = {branch["program"]: branch for branch in admission["programs"]}
    assert branches[prepared["program_a"]]["route"] == "root"
    assert branches[prepared["program_b"]]["route"] == "local"
    local_authorization = admission["local_selection"]["authorization"]
    assert local_authorization["allowed"] is True

    running = full.w.claim(full.owner, case["project"], task=task)

    claim = full.local_executions.claimed(task, running["epoch"])
    assert running["status"] == "running"
    assert running["epoch"] == 1 and running["attempts"] == 1
    assert claim is not None, "Workflow.claim must persist the selected local responsibility itself"
    body = json.loads(claim["body"])
    assert claim["proposal"] == case["proposal"]["id"]
    assert body["proposal"] == admission["local_selection"]["selector"]
    assert body["certified_event"] == local_authorization["certification"]
    assert body["material_digest"] == local_authorization["material_digest"]
    assert body["task"] == task and body["epoch"] == running["epoch"]
    assert full.s.one(
        "SELECT count(*) AS n FROM local_execution_records "
        "WHERE task=? AND epoch=? AND kind='claimed'", (task, running["epoch"]), True,
    )["n"] == 1


def test_local_only_workflow_claim_persists_local_responsibility(full, tmp_path):
    """A current local-only Task is bound in the same Workflow.claim transaction."""
    full.rt.adapters.register(
        full.owner,
        "fixture",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("fixture_agent.py"))],
    )
    marker_script = tmp_path / "local_only_marker_review.py"
    marker_script.write_text(
        "import json,sys\n"
        "packet=json.load(sys.stdin); context=packet.get('context',{})\n"
        "print(json.dumps({'verdict':'pass','rationale':'fixture marker',"
        "'covered':context.get('required_coverage') or context.get('task',{}).get('acceptance',[]),"
        "'findings':[],"
        "'observations':[{'ref':packet.get('subject','packet'),'detail':'fixture marker'}],"
        "'dispositions':[]}))\n",
        encoding="utf-8",
    )
    full.rt.adapters.register(
        full.owner, "markers", "fixture", sys.executable, [str(marker_script)],
    )
    case = _make_local_case(
        (full, None, None, None, None, None, None, []),
        tmp_path,
        activate_root=False,
        include_structural_output=True,
        mature_material=True,
    )
    _review_local_packets(case)
    full.local_executions.certify(
        full.owner, case["proposal"]["id"], case["proposal"]["digest"],
    )
    full.w.ready(full.owner, case["task"])
    admission = inspect_task_admission(
        full, full.owner, task=case["task"], checkpoint="claim",
    )
    assert admission["allowed"] is True, json.dumps(admission, default=str)
    assert admission["canonical_programs"] == [case["program"]]
    assert admission["programs"][0]["route"] == "local"
    assert admission["programs"][0]["root"]["active"] is False

    running = full.w.claim(full.owner, case["project"], task=case["task"])

    claim = full.local_executions.claimed(case["task"], running["epoch"])
    assert running["status"] == "running" and running["epoch"] == 1
    assert claim is not None
    claim_body = json.loads(claim["body"])
    assert claim["proposal"] == admission["local_selection"]["selector"]
    assert claim_body["certified_event"] == admission["local_selection"]["authorization"]["certification"]
    assert claim_body["epoch"] == running["epoch"]


def test_root_only_workflow_claim_does_not_create_local_claim(
    full, full_project, tmp_path, monkeypatch,
):
    class StopBeforeClaim(Exception):
        pass

    captured = {}

    def stop_before_claim(actor, project, task=None):
        captured.update(project=project, task=task)
        raise StopBeforeClaim()

    with monkeypatch.context() as patched:
        patched.setattr(full.w, "claim", stop_before_claim)
        with pytest.raises(StopBeforeClaim):
            _prepare_selected_stage(
                full, full_project, tmp_path, omit_workflow=True, mature=True,
            )

    task = captured["task"]
    admission = inspect_task_admission(
        full, full.owner, task=task, checkpoint="claim",
    )
    assert admission["allowed"] is True, json.dumps(admission, default=str)
    assert len(admission["programs"]) == 1
    assert admission["programs"][0]["route"] == "root"
    assert admission["local_selection"]["state"] == "absent"

    running = full.w.claim(full.owner, captured["project"], task=task)

    assert running["status"] == "running" and running["epoch"] == 1
    assert full.local_executions.claimed(task, running["epoch"]) is None
    assert full.s.one(
        "SELECT count(*) AS n FROM execution_attempts WHERE task=? AND attempt_epoch=?",
        (task, running["epoch"]), True,
    )["n"] == 1


@pytest.mark.parametrize("failure_point", ["local_claim_write", "task_claimed_event"])
def test_workflow_claim_rolls_back_local_and_task_writes_on_storage_failure(
    full, tmp_path, failure_point,
):
    prepared = _prepare_mixed_owner_admission_case(full, tmp_path)
    task = prepared["task"]
    before = _claim_writer_snapshot(full, task)
    if failure_point == "local_claim_write":
        full.s.execute(
            "CREATE TEMP TRIGGER fail_local_claim_insert BEFORE INSERT ON local_execution_records "
            "WHEN NEW.kind='claimed' BEGIN SELECT RAISE(ABORT,'fixture local claim write failed'); END",
        )
    else:
        full.s.execute(
            "CREATE TEMP TRIGGER fail_task_claim_event BEFORE INSERT ON events "
            "WHEN NEW.kind='task_claimed' BEGIN SELECT RAISE(ABORT,'fixture task event write failed'); END",
        )

    with pytest.raises(sqlite3.IntegrityError):
        full.w.claim(full.owner, prepared["case"]["project"], task=task)

    assert _claim_writer_snapshot(full, task) == before


def test_schema15_migration_runs_a_real_task_after_origin_backfill(tmp_path):
    """Migration preserves the legacy origin while a new Task runs normally."""
    from daikibo.control import Control
    from daikibo.db import SCHEMA_VERSION
    from daikibo.program_origins import resolve_program_origin
    from portable_origin_fixture import materialize_fixture

    home = materialize_fixture("legacy", tmp_path / "schema15-real-task")
    control = Control(home, mode="validation", start_workers=False)
    try:
        control.owner = control.sec.authenticate(None)
        assert control.s.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION == 17
        assert control.s.one("SELECT count(*) AS n FROM tasks")["n"] == 0
        project = control.s.one("SELECT id FROM projects", (), True)["id"]
        program = control.s.one("SELECT id FROM programs WHERE project=?", (project,), True)["id"]
        requirement = control.s.one(
            "SELECT id FROM artifacts WHERE project=? AND kind='requirement' AND status='accepted'",
            (project,), True,
        )["id"]
        origin = resolve_program_origin(
            control.s, project=project, program=program,
        )
        assert origin["policy"] == "legacy-preserved"

        repo = tmp_path / "schema15-real-task-repo"
        repo.mkdir()
        (repo / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8",
        )
        (repo / "test_calc.py").write_text(
            "from calc import add\n"
            "def test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        repository = control.sn.register(
            control.owner, project, "schema15-real-task", str(repo),
        )["id"]
        control.rt.adapters.register(
            control.owner,
            "fixture",
            "fixture",
            sys.executable,
            [str(Path(__file__).with_name("fixture_agent.py"))],
        )
        task = control.w.create(
            control.owner,
            project,
            {
                "title": "Run a real migrated Task",
                "goal": "WRITE:" + json.dumps({
                    "calc.py": "def add(a, b):\n    return a + b\n",
                }),
                "read_artifacts": [requirement],
                "write_paths": ["calc.py"],
                "acceptance": ["AC-ORIGIN"],
                "dependencies": [],
                "repos": [repository],
                "non_goals": [],
                "workflow_id": program,
            },
        )["id"]
        control.w.plan_tests(
            control.owner,
            task,
            {"checks": [{
                "id": "unit",
                "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                "kind": "pytest",
                "required_tests": ["test_add"],
            }]},
        )
        ready_gate = control.g.evaluate_task(control.owner, task, "ready")
        assert ready_gate["task_admission"]["canonical_programs"] == []
        control.w.ready(control.owner, task)
        claimed = control.w.claim(control.owner, project, task)
        control.rt.execute(control.owner, task, "fixture")
        control.rt.tests(control.owner, task)
        for role in ("spec", "quality", "test_adequacy"):
            control.rt.review(control.owner, task, role, "fixture")
        completed = control.w.complete(
            control.owner, task, control.w.task(control.owner, task)["revision"],
        )
        assert completed["status"] == "completed"
        assert completed["epoch"] == claimed["epoch"] == 1
        assert completed["attempts"] == 1
        assert control.s.one("SELECT count(*) AS n FROM tasks")["n"] == 1
        assert control.s.one(
            "SELECT count(*) AS n FROM program_origins WHERE project=?", (project,),
        )["n"] == 1
    finally:
        control.close()


def test_schema15_existing_task_and_canonical_root_survive_migration_and_admission(
    tmp_path,
):
    """An old public Task/root keeps identity and remains admissible after migration."""
    from daikibo.control import Control
    from daikibo.db import SCHEMA_VERSION
    from daikibo.program_origins import resolve_program_origin
    from portable_origin_fixture import fixture_manifest, materialize_fixture

    manifest = fixture_manifest("closure")
    assert manifest["source_commit"] == "ac013df0653abe312e1e49f75a62bd9a64ef4518"
    home = materialize_fixture("closure", tmp_path / "schema15-existing-task-root")
    task_id = manifest["closed"]["task"]
    program_id = manifest["closed"]["program"]
    project_id = manifest["closed"]["project"]

    def snapshot(connection):
        connection.row_factory = sqlite3.Row
        sources = [dict(row) for row in connection.execute(
            "SELECT id,project,blob,locator,characters,actor,trust FROM sources "
            "WHERE project=? ORDER BY id",
            (project_id,),
        ).fetchall()]
        task = dict(connection.execute(
            "SELECT id,project,body,revision,status,validity,epoch,lease_owner,"
            "lease_until,candidate,attempts,no_progress_count,paused FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone())
        program = dict(connection.execute(
            "SELECT id,project,phase,revision,body FROM programs WHERE id=?",
            (program_id,),
        ).fetchone())
        root = dict(connection.execute(
            "SELECT id,program,project,body,digest,status,previous FROM breakdowns "
            "WHERE program=? AND status='active'",
            (program_id,),
        ).fetchone())
        encoded = json.dumps(
            {"sources": sources, "task": task, "program": program, "root": root},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return {"sources": sources, "task": task, "program": program, "root": root,
                "sha256": hashlib.sha256(encoded).hexdigest()}

    before_connection = sqlite3.connect(home / "state.sqlite3")
    try:
        assert before_connection.execute("PRAGMA user_version").fetchone()[0] == 15
        assert before_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='program_origins'"
        ).fetchone() is None
        before = snapshot(before_connection)
        assert before["task"]["status"] == "completed"
        assert before["task"]["candidate"]
        assert before["root"]["status"] == "active"
        root_body = json.loads(before["root"]["body"])
        assert task_id in {
            task for unit in root_body["units"] for task in unit["tasks"]
        }
    finally:
        before_connection.close()

    control = Control(home, mode="validation", start_workers=False)
    try:
        control.owner = control.sec.authenticate(None)
        assert control.s.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION == 17
        after = snapshot(control.s.conn)
        assert after == before
        origin = resolve_program_origin(
            control.s, project=project_id, program=program_id,
        )
        assert origin["policy"] == "legacy-preserved"
        assert origin["legacy_program_digest"]
        admission = inspect_task_admission(
            control, control.owner, task=task_id, checkpoint="complete",
        )
        assert admission["allowed"] is True, json.dumps(admission, default=str)
        assert admission["canonical_programs"] == [program_id]
        assert admission["programs"][0]["route"] == "root"
        assert admission["programs"][0]["root"]["active"] is True
        assert admission["programs"][0]["root"]["current"] is True
        assert snapshot(control.s.conn) == before
    finally:
        control.close()


def test_local_admission_rejects_partial_multiple_required_output_producers(
    full, tmp_path,
):
    """One collected producer cannot satisfy a two-output local declaration."""
    full.rt.adapters.register(
        full.owner,
        "fixture",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("fixture_agent.py"))],
    )
    marker_script = tmp_path / "multi_output_marker_review.py"
    marker_script.write_text(
        "import json,sys\n"
        "packet=json.load(sys.stdin); context=packet.get('context',{})\n"
        "print(json.dumps({'verdict':'pass','rationale':'fixture marker',"
        "'covered':context.get('required_coverage') or context.get('task',{}).get('acceptance',[]),"
        "'findings':[],"
        "'observations':[{'ref':packet.get('subject','packet'),'detail':'fixture marker'}],"
        "'dispositions':[]}))\n",
        encoding="utf-8",
    )
    full.rt.adapters.register(
        full.owner, "markers", "fixture", sys.executable, [str(marker_script)],
    )
    case = _make_local_case(
        (full, None, None, None, None, None, None, []),
        tmp_path,
        include_structural_output=True,
        mature_material=True,
        required_output_ids=["local-result", "second-result"],
        produced_output_ids=["local-result"],
    )
    _claim_local(case)
    c = case["c"]
    task = case["task"]
    _managed(c, "execute", {"task": task, "adapter": "fixture"})
    _managed(c, "tests", {"task": task})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": task, "role": role, "adapter": "fixture"})
    _adopt_current_artifact_produced_by(c, case, task)
    declared = c.w.task(c.owner, task)["body"]["structural_obligations"]["required_outputs"]
    assert {item["id"] for item in declared} == {"local-result", "second-result"}
    assert c.s.one(
        "SELECT count(*) AS n FROM assurance_objects "
        "WHERE project=? AND kind='material' "
        "AND json_extract(body,'$.material_kind')='artifact_production'",
        (case["project"],),
    )["n"] == 1
    before = c.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (task,), True,
    )
    result = inspect_task_admission(c, c.owner, task=task, checkpoint="complete")
    assert result["allowed"] is False, json.dumps(result, default=str)
    assert any(
        failure.get("code") == "relation_criterion_all_declared_outputs"
        for failure in result["failures"]
    )
    assert c.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate FROM tasks WHERE id=?",
        (task,), True,
    ) == before


def test_standalone_governance_reads_admission_without_unbound_allow(
    full, full_project, tmp_path,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, omit_workflow=True, mature=True,
    )
    standalone = Governance(full.s, full.sec, full.k, mode="validation")
    for name in ("assurance", "breakdowns", "g", "local_executions", "traceability"):
        setattr(standalone, name, getattr(full, name))

    gate = standalone.evaluate_task(full.owner, flow["task"], "ready")
    admission = gate["task_admission"]
    assert admission["canonical_programs"] == [flow["program"]]
    assert admission["reason"] != "unit4_r_unbound_standalone"
    assert admission["programs"][0]["program"] == flow["program"]


def test_standalone_bind_helper_reuses_the_canonical_readers(
    full, full_project, tmp_path,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, omit_workflow=True, mature=True,
    )
    standalone = Governance(full.s, full.sec, full.k, mode="validation")
    workflow = Workflow(full.s, full.sec, full.k, standalone)
    standalone._bind_composition(
        assurance=full.assurance,
        breakdowns=full.breakdowns,
        local_executions=full.local_executions,
        traceability=full.traceability,
        workflow=workflow,
        review_materials=full.rt.review_materials,
    )
    assert standalone.review_materials is full.rt.review_materials
    before = full.s.conn.total_changes
    result = inspect_task_admission(
        standalone, full.owner, task=flow["task"], checkpoint="claim",
    )
    assert result["allowed"] is True
    assert result["canonical_programs"] == [flow["program"]]
    assert full.s.conn.total_changes == before


def test_standalone_admission_fails_closed_without_review_material_provider(
    full, full_project, tmp_path,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, omit_workflow=True, mature=True,
    )
    standalone = Governance(full.s, full.sec, full.k, mode="validation")
    workflow = Workflow(full.s, full.sec, full.k, standalone)
    standalone._bind_composition(
        assurance=full.assurance,
        breakdowns=full.breakdowns,
        local_executions=full.local_executions,
        traceability=full.traceability,
        workflow=workflow,
    )

    result = inspect_task_admission(
        standalone, full.owner, task=flow["task"], checkpoint="claim",
    )
    assert result["allowed"] is False
    details = json.dumps(result, sort_keys=True, default=str)
    assert "unverified_node_review" in details


def test_standalone_workflow_claim_rechecks_invalid_current_plan(
    full, full_project, tmp_path, monkeypatch,
):
    class ReadyStop(Exception):
        pass

    identifiers = {}

    def stop_before_claim(actor, project, task=None):
        identifiers.update(project=project, task=task)
        raise ReadyStop()

    with monkeypatch.context() as patched:
        patched.setattr(full.w, "claim", stop_before_claim)
        with pytest.raises(ReadyStop):
            _prepare_selected_stage(
                full, full_project, tmp_path, omit_workflow=True, mature=True,
            )

    task = identifiers["task"]
    before = full.w.task(full.owner, task)
    assert before["status"] == "ready"
    full.s.execute("DROP TRIGGER receipts_no_delete")
    full.s.execute(
        "DELETE FROM receipts WHERE subject=? AND role='test_plan'", (task,),
    )
    standalone = Governance(full.s, full.sec, full.k, mode="validation")
    workflow = Workflow(full.s, full.sec, full.k, standalone)

    with pytest.raises(Fault) as rejected:
        workflow.claim(full.owner, identifiers["project"], task)

    assert rejected.value.code == "no_work"
    assert any(
        item.get("stage") == "task_admission"
        for item in rejected.value.details
        if isinstance(item, dict)
    )
    after = full.w.task(full.owner, task)
    assert (after["status"], after["epoch"], after["attempts"]) == (
        before["status"], before["epoch"], before["attempts"],
    )
