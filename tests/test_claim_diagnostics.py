"""Focused public diagnostics for the existing Task claim gates."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import make_task
from daikibo.common import Actor, Fault


def _no_work(control, project, task=None):
    with pytest.raises(Fault) as error:
        control.w.claim(control.owner, project, task)
    assert error.value.code == "no_work"
    return error.value


def _rejected_reviews(control, task, tmp_path):
    adapter = tmp_path / "claim-diagnostics-rejected-review.py"
    adapter.write_text(
        "import json\n"
        "payload=json.load(__import__('sys').stdin)\n"
        "markers=list(payload.get('context',{}).get('task',{}).get('acceptance',[]))\n"
        "print(json.dumps({'verdict':'fail','rationale':'The retained candidate needs correction.',"
        "'covered':markers,'findings':[],"
        "'observations':[{'ref':payload['subject'],'detail':'Observed rejection.'}],"
        "'dispositions':[]}))\n",
    )
    adapter.chmod(0o755)
    control.rt.adapters.register(control.owner, "claim-diagnostics-rejected", "fixture",
                                 sys.executable, [str(adapter)])
    for role in ("spec", "quality", "test_adequacy"):
        result = control.rt.review(control.owner, task, role, "claim-diagnostics-rejected")
        assert result["result"]["verdict"] == "fail"


def _replace_test_plan(control, task):
    control.w.plan_tests(
        control.owner,
        task,
        {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                     "kind": "pytest", "required_tests": ["test_add"]}]},
    )


def test_recovery_denial_is_reported_after_ready_without_mutation(full, full_project, tmp_path):
    task = make_task(full, full_project)
    project = full_project[0]
    first_claim = full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    _rejected_reviews(full, task, tmp_path)
    row = full.w.task(full.owner, task)
    full.w.replan(full.owner, task, row["revision"], "Reassess the rejected candidate.")
    _replace_test_plan(full, task)
    ready = full.w.ready(full.owner, task)
    assert ready["status"] == "ready"

    before = full.s.one("SELECT status,epoch,attempts,lease_owner,lease_until FROM tasks WHERE id=?",
                        (task,), True)
    attempt_count = full.s.one("SELECT count(*) AS n FROM execution_attempts WHERE task=?", (task,))["n"]
    error = _no_work(full, project, task)

    assert error.details == [{
        "task": task,
        "stage": "execution_admission",
        "failures": [f"recovery_required:{first_claim['epoch']}"],
    }]
    after = full.s.one("SELECT status,epoch,attempts,lease_owner,lease_until FROM tasks WHERE id=?",
                       (task,), True)
    assert after == before
    assert full.s.one("SELECT count(*) AS n FROM execution_attempts WHERE task=?", (task,))["n"] == attempt_count


def test_no_progress_threshold_is_reported_but_attempt_telemetry_alone_is_not(full, full_project):
    blocked = make_task(full, full_project)
    project = full_project[0]
    full.s.execute("UPDATE tasks SET attempts=?,no_progress_count=? WHERE id=?", (17, 3, blocked))
    error = _no_work(full, project, blocked)
    assert error.details == [{
        "task": blocked,
        "stage": "execution_admission",
        "failures": ["no_progress_limit"],
    }]
    row = full.s.one("SELECT attempts,no_progress_count,epoch,status FROM tasks WHERE id=?", (blocked,), True)
    assert row == {"attempts": 17, "no_progress_count": 3, "epoch": 0, "status": "ready"}

    telemetry_only = make_task(full, full_project)
    full.s.execute("UPDATE tasks SET attempts=? WHERE id=?", (99, telemetry_only))
    claimed = full.w.claim(full.owner, project, telemetry_only)
    assert claimed["id"] == telemetry_only
    assert claimed["attempts"] == 100


def test_currentness_failure_keeps_the_existing_code(full, full_project):
    task = make_task(full, full_project)
    project = full_project[0]
    full.s.execute("UPDATE tasks SET validity='needs_review' WHERE id=?", (task,))
    error = _no_work(full, project, task)
    assert error.details == [{
        "task": task,
        "stage": "execution_admission",
        "failures": ["inputs_require_reassessment"],
    }]


def test_existing_local_failure_shape_is_retained_with_a_stage(full, full_project, monkeypatch):
    task = make_task(full, full_project)
    project = full_project[0]

    def rejected_readiness(actor, selected, gate):
        assert selected == task and gate == "ready"
        return {"verdict": "fail", "failures": ["local_execution_not_current"]}

    monkeypatch.setattr(full.g, "_evaluate_task_readonly", rejected_readiness)
    error = _no_work(full, project, task)
    assert error.details == [{
        "task": task,
        "stage": "ready_gate",
        "failures": ["local_execution_not_current"],
    }]
    assert set(error.details[0]) >= {"task", "failures", "stage"}


@pytest.mark.parametrize(
    ("mutation", "stage", "failure"),
    [("paused", "task_state", "task_paused"), ("planned", "task_state", "task_not_ready")],
)
def test_explicit_task_state_skip_is_candidate_scoped(full, full_project, mutation, stage, failure):
    task = make_task(full, full_project)
    project = full_project[0]
    if mutation == "paused":
        full.w.pause(full.owner, project, task, True)
    else:
        full.s.execute("UPDATE tasks SET status='planned' WHERE id=?", (task,))
    error = _no_work(full, project, task)
    assert error.details == [{"task": task, "stage": stage, "failures": [failure]}]


def test_dependency_and_resource_skips_do_not_expose_counterpart_ids(full, full_project):
    project = full_project[0]
    dependency = make_task(full, full_project)
    dependent = make_task(full, full_project)
    full.s.execute("INSERT INTO task_deps(task,dependency) VALUES(?,?)", (dependent, dependency))
    dependency_error = _no_work(full, project, dependent)
    assert dependency_error.details == [{
        "task": dependent, "stage": "dependency", "failures": ["dependency_not_current"]
    }]
    assert dependency not in json.dumps(dependency_error.details)

    running = make_task(full, full_project)
    competing = make_task(full, full_project)
    full.w.claim(full.owner, project, running)
    conflict_error = _no_work(full, project, competing)
    assert conflict_error.details == [{
        "task": competing, "stage": "resource_conflict", "failures": ["write_conflict"]
    }]
    assert running not in json.dumps(conflict_error.details)


def test_automatic_scan_skips_blocked_candidate_and_claims_next_candidate(full, full_project):
    project = full_project[0]
    blocked = make_task(full, full_project)
    eligible = make_task(full, full_project)
    full.s.execute("UPDATE tasks SET no_progress_count=3 WHERE id=?", (blocked,))
    claimed = full.w.claim(full.owner, project)
    assert claimed["id"] == eligible
    assert full.s.one("SELECT attempts,status FROM tasks WHERE id=?", (blocked,), True) == {
        "attempts": 0, "status": "ready"
    }


def test_automatic_scan_skips_unit4_r_denial_and_claims_next_candidate(
    full, full_project, monkeypatch,
):
    """A readonly R denial remains candidate-scoped during automatic claim."""
    project = full_project[0]
    blocked = make_task(full, full_project)
    eligible = make_task(full, full_project)
    before = full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate "
        "FROM tasks WHERE id=?", (blocked,), True,
    )
    import daikibo.unit4_enforcement as enforcement

    original = enforcement.inspect_task_admission
    seen = []

    def deny_one(control, actor, *, task, checkpoint):
        seen.append((task, checkpoint))
        result = original(control, actor, task=task, checkpoint=checkpoint)
        if task == blocked:
            return {
                **result,
                "allowed": False,
                "failures": [{
                    "code": "canonical_profile_required",
                    "reason": "The first automatic candidate is not admissible.",
                }],
            }
        return result

    monkeypatch.setattr(enforcement, "inspect_task_admission", deny_one)
    claimed = full.w.claim(full.owner, project)
    assert claimed["id"] == eligible
    assert seen[:2] == [(blocked, "claim"), (eligible, "claim")]
    assert seen[2:] == [(eligible, "ready")]
    assert full.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate "
        "FROM tasks WHERE id=?", (blocked,), True,
    ) == before


def test_scope_and_early_rejections_do_not_leak_diagnostics(full, full_project):
    project = full_project[0]
    blocked = make_task(full, full_project)
    full.s.execute("UPDATE tasks SET no_progress_count=3 WHERE id=?", (blocked,))
    other = full.k.create_project(full.owner, "foreign")['id']
    scoped = Actor("foreign-agent", "agent", other)
    with pytest.raises(Fault) as error:
        full.w.claim(scoped, project, blocked)
    assert error.value.code == "forbidden"
    assert blocked not in json.dumps(error.value.details)

    running = [make_task(full, full_project, paths=[f"disjoint-{index}.py"]) for index in range(5)]
    for task in running[:4]:
        full.w.claim(full.owner, project, task)
    with pytest.raises(Fault) as capacity:
        full.w.claim(full.owner, project, running[4])
    assert capacity.value.code == "capacity"
    assert capacity.value.details is None


def test_empty_automatic_scan_preserves_the_existing_details_list(full, full_project):
    error = _no_work(full, full_project[0])
    assert error.details == []


def test_ambiguous_admission_exception_is_not_converted_to_no_work(full, full_project, monkeypatch):
    task = make_task(full, full_project)

    def ambiguous(*args, **kwargs):
        raise Fault("ambiguous_attempt", "Retained attempt identity is ambiguous")

    monkeypatch.setattr(full.execution_controls, "admission", ambiguous)
    with pytest.raises(Fault) as error:
        full.w.claim(full.owner, full_project[0], task)
    assert error.value.code == "ambiguous_attempt"


def test_claim_diagnostics_are_exposed_by_api_describe_and_bundled_skill(full):
    descriptor = full.invoke(full.owner, "api.describe", {"method": "task.claim"})
    contract = descriptor["methods"]["task.claim"]["body_contract"]["no_work"]
    assert contract["code"] == "no_work"
    assert contract["details"][0] == {
        "task": "authorized candidate ID", "stage": "check stage",
        "failures": ["canonical failure code"],
    }
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/daikibo/assets/skill/SKILL.md").is_file()
    assert (root / "src/daikibo/assets/skill/references/operations.md").is_file()
