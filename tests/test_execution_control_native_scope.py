"""Native and capability-scoped execution-control mutation boundaries."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import make_task
from daikibo.common import Actor, Fault


def _claim_only_recovery(control, project, task, requirement):
    claimed = control.w.claim(control.owner, project, task)
    control.s.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
    control.w.reconcile(control.owner, project)
    control.w.replan(control.owner, task, 1, "Retain the reviewed scope after the expired claim.")
    control.w.plan_tests(control.owner, task, {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}]
    })
    return claimed, {
        "target_attempt_epoch": claimed["epoch"],
        "control_type": "recovery",
        "cause_analysis": "The claim lease expired before an observed run.",
        "experiment_estimate": {},
        "evidence": [requirement],
        "intended_next_action": "Retry only after reviewed recovery and normal gates.",
        "scope": {"task": task},
        "recovery_action": "Observe the next claim under the retained task plan.",
    }


def _control_review_adapter(control, tmp_path, name):
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, sys\n"
        "payload=json.load(sys.stdin)\n"
        "markers=list(payload.get('context',{}).get('required_coverage',[]))\n"
        "dispositions=[]\n"
        "for marker in markers:\n"
        "    resolution='approved' if marker.startswith('recovery:') else 'progress'\n"
        "    dispositions.append({'id':marker,'resolution':resolution,'reason':'scoped fixture evidence'})\n"
        "print(json.dumps({'verdict':'pass','rationale':'scoped fixture review',\n"
        " 'covered':markers,'findings':[],'observations':[{'ref':payload.get('subject','proposal'),'detail':'fixture'}],\n"
        " 'dispositions':dispositions}))\n"
    )
    script.chmod(0o755)
    control.rt.adapters.register(control.owner, name, "fixture", sys.executable, [str(script)])
    return name


def _make_second_project(control, tmp_path):
    project = control.k.create_project(control.owner, "Second execution-control project")["id"]
    root = tmp_path / "second-repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a,b):\n    return a-b\n")
    (root / "test_calc.py").write_text("from calc import add\ndef test_add():\n    assert add(2,3) == 5\n")
    repo = control.sn.register(control.owner, project, "app", str(root))["id"]
    source = control.k.source(control.owner, project, "The second project retains the same arithmetic requirement.")
    requirement = control.k.propose(control.owner, project, "requirement", {
        "title": "Second addition", "statement": "Returns arithmetic sum", "acceptance": ["AC-ADD"],
        "source_refs": [source["id"]],
    })
    control.k.accept(control.owner, requirement["id"], 1)
    control.k.classify(control.owner, source["id"], 0, source["characters"], "requirement",
                       [requirement["id"]], "Original second-project source")
    return project, repo, requirement["id"], root


def test_native_scoped_propose_apply_replay_and_withdraw(full, full_project, tmp_path):
    project, _, requirement, root = full_project
    task = make_task(full, full_project)
    _, body = _claim_only_recovery(full, project, task, requirement)
    revision = full.w.task(full.owner, task)["revision"]
    session = "native-execution-control"
    full.native.attach(full.owner, session, str(root), project=project)

    proposed = full.native.actions(full.owner, session, [{
        "method": "execution_control.propose",
        "params": {"task": task, "expected_revision": revision, "body": body},
    }])
    assert proposed["all_applied"]
    proposal = proposed["actions"][0]["result"]
    event = full.s.one(
        "SELECT actor FROM events WHERE kind='execution_control_proposed' "
        "AND json_extract(body,'$.proposal')=?", (proposal["id"],), True)
    assert event["actor"] == "native-agent:" + session

    adapter = _control_review_adapter(full, tmp_path, "native-recovery-review")
    review = full.rt.review(full.owner, proposal["id"], "execution_control", adapter)
    applied = full.native.actions(full.owner, session, [{
        "method": "execution_control.apply",
        "params": {"proposal": proposal["id"], "expected_digest": proposal["digest"],
                    "review_receipt": review["receipt"]},
    }])
    assert applied["all_applied"]
    result = applied["actions"][0]["result"]
    assert result["authorization"]
    replay = full.native.actions(full.owner, session, [{
        "method": "execution_control.apply",
        "params": {"proposal": proposal["id"], "expected_digest": proposal["digest"],
                    "review_receipt": review["receipt"]},
    }])
    assert replay["all_applied"] and replay["actions"][0]["result"]["replayed"]
    assert full.s.one("SELECT count(*) AS n FROM execution_control_authorizations WHERE proposal=?",
                      (proposal["id"],))["n"] == 1

    second = full.native.actions(full.owner, session, [{
        "method": "execution_control.propose",
        "params": {"task": task, "expected_revision": revision, "body": body},
    }])
    assert second["all_applied"]
    pending = second["actions"][0]["result"]
    withdrawn = full.native.actions(full.owner, session, [{
        "method": "execution_control.withdraw",
        "params": {"proposal": pending["id"], "expected_digest": pending["digest"], "reason": "Keep the reviewed proposal history."},
    }])
    assert withdrawn["all_applied"] and withdrawn["actions"][0]["result"]["status"] == "withdrawn"
    withdrawn_replay = full.native.actions(full.owner, session, [{
        "method": "execution_control.withdraw",
        "params": {"proposal": pending["id"], "expected_digest": pending["digest"], "reason": "Keep the reviewed proposal history."},
    }])
    assert withdrawn_replay["all_applied"] and withdrawn_replay["actions"][0]["result"]["replayed"]


def test_execution_control_mutations_require_actual_project_and_task_scope(full, full_project, tmp_path):
    project, _, requirement, root = full_project
    other = _make_second_project(full, tmp_path)
    foreign_task = make_task(full, other)
    _, foreign_body = _claim_only_recovery(full, other[0], foreign_task, other[2])
    foreign_revision = full.w.task(full.owner, foreign_task)["revision"]
    foreign_proposal = full.execution_controls.propose(full.owner, foreign_task, foreign_revision, foreign_body)
    session = "project-a-session"
    full.native.attach(full.owner, session, str(root), project=project)
    proposals_before = full.s.one("SELECT count(*) AS n FROM execution_control_proposals")["n"]
    native_propose = full.native.actions(full.owner, session, [{
        "method": "execution_control.propose",
        "params": {"task": foreign_task, "expected_revision": foreign_revision, "body": foreign_body},
    }])
    assert not native_propose["all_applied"]
    assert native_propose["actions"][0]["error"]["code"] == "forbidden"
    assert full.s.one("SELECT count(*) AS n FROM execution_control_proposals")["n"] == proposals_before
    for method, params in (
        ("execution_control.apply", {"proposal": foreign_proposal["id"], "expected_digest": foreign_proposal["digest"], "review_receipt": "missing"}),
        ("execution_control.withdraw", {"proposal": foreign_proposal["id"], "expected_digest": foreign_proposal["digest"], "reason": "cross-project"}),
    ):
        result = full.native.actions(full.owner, session, [{"method": method, "params": params}])
        assert not result["all_applied"]
        assert result["actions"][0]["error"]["code"] == "forbidden"
    assert full.s.one("SELECT status FROM execution_control_proposals WHERE id=?", (foreign_proposal["id"],))["status"] == "proposed"

    same_task = make_task(full, full_project)
    _, same_body = _claim_only_recovery(full, project, same_task, requirement)
    same_revision = full.w.task(full.owner, same_task)["revision"]
    same_proposal = full.execution_controls.propose(full.owner, same_task, same_revision, same_body)
    scoped_other_task = Actor("task-scoped-agent", "agent", project, task=foreign_task)
    for method, params in (
        ("propose", {"task": same_task, "expected_revision": same_revision, "body": same_body}),
        ("apply", {"proposal": same_proposal["id"], "expected_digest": same_proposal["digest"], "review_receipt": "missing"}),
        ("withdraw", {"proposal": same_proposal["id"], "expected_digest": same_proposal["digest"], "reason": "cross-task"}),
    ):
        with pytest.raises(Fault) as error:
            getattr(full.execution_controls, method)(scoped_other_task, **params)
        assert error.value.code == "forbidden"
    for role in ("observer", "reviewer", "worker"):
        restricted = Actor(role + "-execution-control", role, project, task=same_task)
        for method, params in (
            ("propose", {"task": same_task, "expected_revision": same_revision, "body": same_body}),
            ("apply", {"proposal": same_proposal["id"], "expected_digest": same_proposal["digest"], "review_receipt": "missing"}),
            ("withdraw", {"proposal": same_proposal["id"], "expected_digest": same_proposal["digest"], "reason": "role"}),
        ):
            with pytest.raises(Fault) as error:
                getattr(full.execution_controls, method)(restricted, **params)
            assert error.value.code == "forbidden"
    assert full.s.one("SELECT status FROM execution_control_proposals WHERE id=?", (same_proposal["id"],))["status"] == "proposed"


def test_recovery_review_subject_uses_run_and_claim_only_marker_contract(full, full_project):
    project, _, requirement, _ = full_project
    claim_only_task = make_task(full, full_project)
    _, claim_body = _claim_only_recovery(full, project, claim_only_task, requirement)
    claim_revision = full.w.task(full.owner, claim_only_task)["revision"]
    claim_proposal = full.execution_controls.propose(full.owner, claim_only_task, claim_revision, claim_body)
    _, _, _, claim_context, _ = full.execution_controls.review_subject(
        full.owner, claim_proposal["id"], "execution_control")
    assert claim_context["required_coverage"] == [f"recovery:{claim_proposal['id']}"]
    assert "Return only the typed recovery disposition" in claim_context["instructions"]
    assert "attempt disposition" not in claim_context["instructions"]

    observed_task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, project, observed_task)
    full.rt.execute(full.owner, observed_task, "fixture")
    run = full.s.one("SELECT id FROM runs WHERE task=? AND role='implementer' AND epoch=?",
                     (observed_task, claimed["epoch"]), True)
    receipt = full.s.one("SELECT id FROM receipts WHERE run=?", (run["id"],), True)
    attempt = full.s.one("SELECT attempt_ordinal FROM execution_attempts WHERE task=? AND attempt_epoch=?",
                         (observed_task, claimed["epoch"]), True)
    observed_body = {
        "target_attempt_epoch": claimed["epoch"], "target_attempt_ordinal": attempt["attempt_ordinal"],
        "target_implementer_run": run["id"], "control_type": "recovery", "requested_seconds": None,
        "old_effective_seconds": None, "cause_analysis": "Review the retained run independently.",
        "experiment_estimate": {}, "evidence": [receipt["id"]],
        "intended_next_action": "Use only a separately reviewed recovery.", "scope": {"task": observed_task},
        "recovery_action": "Reassess the retained run before retrying.",
    }
    observed_proposal = full.execution_controls.propose(full.owner, observed_task, 1, observed_body)
    _, _, _, observed_context, _ = full.execution_controls.review_subject(
        full.owner, observed_proposal["id"], "execution_control")
    assert observed_context["required_coverage"] == [f"recovery:{observed_proposal['id']}", f"attempt:{claimed['epoch']}"]
    assert "one typed recovery disposition" in observed_context["instructions"]
    assert "one typed attempt disposition" in observed_context["instructions"]
    assert "Return only the typed recovery disposition" not in observed_context["instructions"]
