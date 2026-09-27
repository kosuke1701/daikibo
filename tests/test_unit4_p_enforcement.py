"""Finite Unit4-P writer-boundary checks.

The negative fixture uses the ordinary public breakdown proposal/review path.
The positive fixture is the existing Runtime P/M/R stage fixture, so the
writer helper consumes a real canonical plan evaluation rather than a caller
shaped ``allow`` value.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from daikibo.common import Fault
from daikibo.control import Control
from daikibo.program_origins import resolve_program_origin
from daikibo.unit4_enforcement import inspect_plan_gate
from portable_origin_fixture import materialize_fixture, fixture_manifest


def _new_program_breakdown(full, full_project):
    project, repository, requirement, _root = full_project
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    requirement_row = full.s.one(
        "SELECT * FROM artifacts WHERE id=? AND project=?", (requirement, project), True,
    )
    # A current canonical plan also needs a complete source partition.  This
    # is the same public traceability evidence used by the real planning
    # fixture; omitting it would make the positive gate fail at context
    # validation before Unit4-P is exercised.
    partition = full.traceability.propose(
        full.owner, project, kind="document", scope={"source": source},
    )
    full.traceability.extract(full.owner, partition["id"])
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    domain = full.k.propose(
        full.owner,
        project,
        "domain",
        {
            "title": "Unit4-P domain",
            "statement": "The bounded plan owns arithmetic output.",
            "responsibilities": ["arithmetic"],
            "non_responsibilities": ["billing"],
            "owned_data": [],
            "interfaces": [],
            "source_refs": [source],
        },
    )
    full.k.accept(full.owner, domain["id"], domain["revision"])
    task = full.w.create(
        full.owner,
        project,
        {
            "title": "Unit4-P production task",
            "goal": "WRITE:" + json.dumps({"result.txt": "plan output\n"}),
            "read_artifacts": [requirement, domain["id"]],
            "write_paths": ["result.txt"],
            "acceptance": ["AC-ADD"],
            "dependencies": [],
            "repos": [repository],
            "non_goals": [],
            "workflow_id": program,
            "structural_obligations": {
                "format": "daikibo.task-structural-obligations.v1",
                "required_outputs": [{
                    "id": "unit4-p-result",
                    "statement": "The Unit4-P Task emits its bounded result.",
                    "artifact_refs": [{
                        "kind": "artifact", "project": project,
                        "artifact": requirement_row["id"],
                        "revision": requirement_row["revision"],
                        "body_digest": requirement_row["digest"],
                    }],
                    "realization_kind": "artifact",
                }],
                "required_exercises": [],
            },
        },
    )
    full.w.plan_tests(
        full.owner,
        task["id"],
        {
            "checks": [
                {
                    "id": "unit",
                    "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest",
                    "required_tests": ["test_add"],
                }
            ]
        },
    )
    units = [
        {
            "id": "unit4-p-unit",
            "title": "Unit4-P bounded unit",
            "parent": None,
            "domain": domain["id"],
            "rationale": "Keep the accepted requirement and production Task in one unit.",
            "obligations": [{"requirement": requirement, "acceptance": "AC-ADD"}],
            "tasks": [task["id"]],
            "interfaces": [],
            "dependencies": [],
        }
    ]
    breakdown = full.breakdowns.propose(
        full.owner,
        program,
        "Unit4-P plan",
        "Exercise the new-program plan adoption boundary.",
        units,
    )
    page = full.breakdowns.get(full.owner, breakdown["id"])
    full.rt.adapters.register(
        full.owner,
        "unit4-p-markers",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("assurance_reviewer_fixture.py"))],
    )
    for packet in page["packets"]:
        for role in ("design", "trace"):
            full.rt.review(full.owner, packet["id"], role, "unit4-p-markers")
    return project, program, breakdown["id"]


def test_new_program_without_profile_blocks_breakdown_audit_and_activation(full, full_project):
    project, program, breakdown = _new_program_breakdown(full, full_project)
    report = full.breakdowns.audit(full.owner, breakdown)
    assert report["current"] is False
    assert report["plan_gate"]["allowed"] is False
    assert report["plan_gate"]["reason"] == "canonical_profile_required"
    assert any(
        item.get("code") == "stage_assurance_blocked"
        and item.get("plan_gate", {}).get("reason") == "canonical_profile_required"
        for item in report["failures"]
    )
    with pytest.raises(Fault) as rejected:
        full.breakdowns.activate(full.owner, breakdown)
    assert rejected.value.code == "breakdown_gate_denied"
    assert full.s.one(
        "SELECT status FROM breakdowns WHERE id=?", (breakdown,), True,
    )["status"] == "proposed"
    assert full.s.one(
        "SELECT id FROM breakdowns WHERE program=? AND status='active'", (program,)
    ) is None

    # The plan-phase reader exposes the same denial before advance reaches its
    # transaction boundary.  The fixture is deliberately moved to that phase
    # only to exercise the public diagnostic; no phase output is fabricated.
    row = full.s.one("SELECT * FROM programs WHERE id=?", (program,), True)
    full.s.execute("UPDATE programs SET phase='plan' WHERE id=?", (program,))
    blockers = full.p.phase_blockers({**row, "phase": "plan"})
    assert "assurance_plan_blocked:canonical_profile_required" in blockers
    with pytest.raises(Fault) as phase_rejected:
        full.p.advance(full.owner, program, row["revision"], "missing-review")
    assert phase_rejected.value.code == "phase_blocked"


def test_existing_migrated_legacy_program_keeps_unselected_route(tmp_path):
    home = materialize_fixture("legacy", tmp_path / "legacy-u4p")
    expected = fixture_manifest("legacy")
    control = Control(home, mode="validation", start_workers=False)
    try:
        control.owner = control.sec.authenticate(None)
        project = expected["project"]
        program = expected["program"]
        origin = resolve_program_origin(control.s, project=project, program=program)
        assert origin["policy"] == "legacy-preserved"
        gate = inspect_plan_gate(
            control,
            control.owner,
            project=project,
            program=program,
        )
        assert gate["allowed"] is True
        assert gate["required"] is False
        assert gate["reason"] == "legacy_unselected_profile_route"
    finally:
        control.close()


def test_selected_disabled_profile_does_not_fallback_to_legacy(full):
    from test_e3_selection_contract import (
        _adopt,
        _fixture,
        _profile_body,
        _register_fixture_review,
        _source_ref,
    )

    project, source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    disabled = full.assurance.profile_propose(
        full.owner,
        project,
        program,
        _profile_body(
            project,
            program,
            scope,
            previous=selected["profile_ref"],
            mode="disabled",
            authority_refs=[_source_ref(project, source)],
            reason="finite Unit4-P disabled-selection contrast",
        ),
        selected["head_event"],
    )
    _adopt(full, project, disabled, selected["head_event"])
    gate = inspect_plan_gate(full, full.owner, project=project, program=program)
    assert gate["allowed"] is False
    assert gate["reason"] == "mandatory_profile_required"
    assert gate["selection"]["state"] == "disabled"


def test_new_program_local_certification_requires_the_same_plan_gate(full, tmp_path):
    # Reuse the repository's public local-proposal fixture builder, but leave
    # its composed root unadopted.  Unit4-P must permit that proposal to exist
    # while refusing certification until the canonical mandatory plan proof is
    # available.
    from test_local_execution import _make_local_case, _review_local_packets

    full.rt.adapters.register(
        full.owner,
        "fixture",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("fixture_agent.py"))],
    )
    marker_script = tmp_path / "unit4_p_marker_review.py"
    marker_script.write_text(
        "import json,sys\n"
        "packet=json.load(sys.stdin)\n"
        "context=packet.get('context',{})\n"
        "print(json.dumps({'verdict':'pass','rationale':'mechanical marker fixture',"
        "'covered':context.get('required_coverage',[]),'findings':[],"
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
    )
    _review_local_packets(case)
    report = full.local_executions.audit(full.owner, case["proposal"]["id"])
    assert report["current"] is False
    assert report["plan_gate"]["reason"] == "stage_assurance_blocked"
    with pytest.raises(Fault) as rejected:
        full.local_executions.certify(
            full.owner,
            case["proposal"]["id"],
            case["proposal"]["digest"],
        )
    assert rejected.value.code == "local_execution_gate_denied"
    assert full.s.one(
        "SELECT count(*) AS n FROM local_execution_records WHERE proposal=? AND kind='certified'",
        (case["proposal"]["id"],),
    )["n"] == 0


def test_real_runtime_mandatory_plan_evaluation_is_consumed(full, full_project, tmp_path):
    # This fixture performs actual Runtime execution, output collection, E3
    # profile adoption, relation reviews, and N review before the shared
    # Unit4 reader is called.
    from test_stage_profile_registry_connection import _stage_flow

    flow = _stage_flow(
        full,
        full_project,
        tmp_path,
        profile_format="assurance.profile.v3",
    )
    gate = inspect_plan_gate(
        full,
        full.owner,
        project=flow["project"],
        program=flow["program"],
        proposed_breakdown=flow["breakdown"],
    )
    assert gate["allowed"] is True
    assert gate["required"] is True
    assert gate["selection"]["application_mode"] == "mandatory"
    assert gate["evaluation"]["assurance_allow"] is True
    assert gate["evaluation"]["strong_complete"] is True
    assert gate["proof_digest"]


def test_public_new_program_accepts_current_plan_with_deferred_output(full, full_project, tmp_path):
    """A real plan may admit while its declared output remains future material."""
    from test_consumer_p_mr_integration import _actual_review_adapter
    from test_private_selected_local_stage import _review_task_plan, _select_local_profile

    project, program, breakdown = _new_program_breakdown(full, full_project)
    task = full.s.one("SELECT id FROM tasks WHERE project=? ORDER BY id DESC LIMIT 1", (project,), True)["id"]
    case = {"c": full, "project": project, "program": program,
            "requirements": [full_project[2]], "task": task}
    _select_local_profile(case)
    _actual_review_adapter(full, tmp_path)
    _review_task_plan(case)

    gate = inspect_plan_gate(
        full, full.owner, project=project, program=program,
        proposed_breakdown=breakdown,
    )
    assert gate["allowed"] is True
    assert gate["stage"] == "plan" and gate["checkpoint"] == "plan"
    assert gate["evaluation"]["assurance_allow"] is True
    assert gate["evaluation"]["strong_complete"] is False
    assert gate["evaluation"]["deferred_future"]
    adopted = full.breakdowns.activate(full.owner, breakdown)
    assert adopted["status"] == "active"
    assert full.breakdowns.activate(full.owner, breakdown)["replayed"] is True


def test_public_new_program_missing_current_plan_review_blocks_activation(full, full_project):
    """The same public fixture rejects a plan with no current test-plan receipt."""
    from test_private_selected_local_stage import _select_local_profile

    project, program, breakdown = _new_program_breakdown(full, full_project)
    task = full.s.one("SELECT id FROM tasks WHERE project=? ORDER BY id DESC LIMIT 1", (project,), True)["id"]
    _select_local_profile({"c": full, "project": project, "program": program,
                           "requirements": [full_project[2]], "task": task})

    gate = inspect_plan_gate(
        full, full.owner, project=project, program=program,
        proposed_breakdown=breakdown,
    )
    assert gate["allowed"] is False
    assert any(
        item.get("kind") == "node" and item.get("reason") == "no_current_receipt"
        for item in gate["evaluation"]["failures"]
    )
    with pytest.raises(Fault) as rejected:
        full.breakdowns.activate(full.owner, breakdown)
    assert rejected.value.code == "breakdown_gate_denied"
    assert full.s.one(
        "SELECT status FROM breakdowns WHERE id=?", (breakdown,), True,
    )["status"] == "proposed"


def test_local_selected_task_uses_current_plan_and_rejects_revision(full, tmp_path):
    """Local certification evaluates the selected Task projection and rechecks plan currentness."""
    from test_consumer_p_mr_integration import _actual_review_adapter
    from test_local_execution import _make_local_case, _review_local_packets
    from test_private_selected_local_stage import _review_task_plan, _select_local_profile

    full.rt.adapters.register(
        full.owner,
        "fixture",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("fixture_agent.py"))],
    )
    full.rt.adapters.register(
        full.owner,
        "markers",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("assurance_reviewer_fixture.py"))],
    )

    case = _make_local_case(
        (full, None, None, None, None, None, None, []), tmp_path,
        activate_root=False, include_structural_output=True,
    )
    _select_local_profile(case)
    _actual_review_adapter(full, tmp_path)
    _review_task_plan(case)
    _review_local_packets(case)

    report = full.local_executions.audit(full.owner, case["proposal"]["id"])
    assert report["current"] is True
    assert report["plan_gate"]["allowed"] is True
    assert report["plan_gate"]["stage"] == "task"
    assert report["plan_gate"]["checkpoint"] == "ready"
    certified = full.local_executions.certify(
        full.owner, case["proposal"]["id"], case["proposal"]["digest"],
    )
    assert certified["current"] is True
    assert full.local_executions.certify(
        full.owner, case["proposal"]["id"], case["proposal"]["digest"],
    )["replayed"] is True

    full.w.plan_tests(
        full.owner, case["task"],
        {"checks": [{"id": "changed-unit", "argv": ["python", "-c", "print('changed')"],
                      "kind": "pytest", "required_tests": ["test_changed"],
                      "purpose": "Current plan revision invalidates the old review."}]},
    )
    invalidated = full.local_executions.audit(full.owner, case["proposal"]["id"])
    assert invalidated["current"] is False
    assert any(
        item.get("code") == "integrity_error"
        and "scope" in item.get("reason", "").lower()
        for item in invalidated["plan_gate"].get("evaluation", {}).get("failures", [])
    )
    with pytest.raises(Fault) as rejected:
        full.local_executions.certify(
            full.owner, case["proposal"]["id"], case["proposal"]["digest"],
        )
    assert rejected.value.code == "local_execution_gate_denied"
