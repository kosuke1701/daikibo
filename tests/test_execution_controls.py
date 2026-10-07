"""Focused dev18 execution-control invariants."""
from __future__ import annotations

import json
import math
import sys

import pytest

from daikibo.common import Fault, finite_duration
from conftest import make_task


def test_schema13_has_separate_progress_counter(full):
    columns = {row["name"] for row in full.s.all("PRAGMA table_info(tasks)")}
    assert "no_progress_count" in columns
    assert full.s.one("SELECT no_progress_count FROM tasks LIMIT 1") is None
    for table in (
        "execution_attempts", "attempt_assessments", "execution_control_proposals",
        "execution_control_packets", "execution_control_events", "execution_control_authorizations",
    ):
        assert full.s.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))


def test_claim_records_attempt_and_budget_history_is_telemetry(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    row = full.s.one("SELECT * FROM execution_attempts WHERE task=?", (task,), True)
    assert row["attempt_epoch"] == 1 and row["attempt_ordinal"] == 1
    full.s.execute("INSERT INTO blocks VALUES(?,?,?,?)", (task, "budget", "attempts", "legacy telemetry"))
    admission = full.execution_controls.admission(full.owner, task)
    assert admission["allowed"] is True
    assert admission["legacy_attempt_limit_blocking"] is False


def test_implementer_run_reservation_is_one_per_claim(full, full_project):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    run = full.execution_controls.reserve_implementer(full.owner, task, claimed["epoch"])
    assert run.startswith("RUN-")
    with pytest.raises(Fault) as error:
        full.execution_controls.reserve_implementer(full.owner, task, claimed["epoch"])
    assert error.value.code == "attempt_already_executing"


def test_semantic_authorization_material_ignores_own_claim_epoch(full, full_project):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    target = {"attempt_epoch": claimed["epoch"], "attempt_ordinal": 1, "task_revision": claimed["revision"]}
    before = full.execution_controls._current_material(full.owner, task, target)
    full.s.execute("UPDATE tasks SET epoch=epoch+1,candidate=NULL,updated=updated+1 WHERE id=?", (task,))
    after = full.execution_controls._current_material(full.owner, task, target)
    assert before["semantic_digest"] == after["semantic_digest"]


@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, True])
def test_duration_rejects_nonfinite_or_nonpositive(value):
    with pytest.raises(Fault):
        finite_duration(value, "duration")


def test_duration_accepts_explicit_long_experiment():
    assert finite_duration(172800, "duration") == 172800.0


def test_policy_adoption_requires_source_backed_requirement_review(full, full_project):
    project, _, requirement, _ = full_project
    source = full.s.one(
        "SELECT id,blob FROM sources WHERE project=? ORDER BY created LIMIT 1", (project,), True
    )
    artifact = full.s.one("SELECT revision,digest FROM artifacts WHERE id=?", (requirement,), True)
    policy = full.g.policy(project)
    body = {
        "source": {"id": source["id"], "digest": source["blob"]},
        "requirement": {"id": requirement, "revision": artifact["revision"], "digest": artifact["digest"]},
        "expected_policy": {"revision": policy["revision"], "digest": policy["digest"]},
        "supersedes": [], "reason": "Adopt the reviewed execution-control requirement.",
    }
    with pytest.raises(Fault) as error:
        full.execution_controls.policy_propose(full.owner, project, body)
    assert error.value.code == "review_required"


def test_policy_requirement_review_retains_accepted_material_after_context_changes(full, full_project):
    from test_execution_control_lifecycle import _reviewed_requirement

    project = full_project[0]
    source = full.s.one("SELECT id FROM sources WHERE project=? LIMIT 1", (project,), True)
    requirement = _reviewed_requirement(full, project, source["id"])
    row = full.s.one("SELECT * FROM artifacts WHERE id=?", (requirement["id"],), True)
    receipt = full.execution_controls._requirement_review(row)
    assert full.g.receipt(receipt)["binding"] != row["digest"]
    invariant = full.k.propose(full.owner, project, "design", {
        "title": "New independent constraint", "statement": "Retain the reviewed policy source.",
        "critical": True,
    })
    full.k.accept(full.owner, invariant["id"], 1)
    # Historical acceptance remains proof of this exact requirement; the
    # policy proposal has its own separate review of the current context.
    assert full.execution_controls._requirement_review(row) == receipt


def test_policy_requirement_review_rejects_other_artifact_in_acceptance_event(full, full_project):
    project = full_project[0]
    source = full.s.one("SELECT id FROM sources WHERE project=? LIMIT 1", (project,), True)
    body = {"title": "Policy requirement", "statement": "Source-backed policy.",
            "acceptance": ["AC-POLICY"], "source_refs": [source["id"]]}
    reviewed = full.k.propose(full.owner, project, "requirement", body)
    receipt = full.rt.review(full.owner, reviewed["id"], "requirements", "fixture")["receipt"]
    other = full.k.propose(full.owner, project, "requirement", {**body, "title": "Other policy"})
    full.k.accept(full.owner, other["id"], 1, receipt)
    row = full.s.one("SELECT * FROM artifacts WHERE id=?", (other["id"],), True)
    with pytest.raises(Fault) as error:
        full.execution_controls._requirement_review(row)
    assert error.value.code == "review_required"


def test_inconclusive_is_nonfinal_and_markers_are_typed(full):
    body = {
        "target_attempt_epoch": 1,
        "control_type": "timeout",
        "requested_seconds": 90000,
        "old_effective_seconds": None,
        "cause_analysis": "Observed finite experiment needs a longer window.",
        "experiment_estimate": {"seconds": 90000},
        "evidence": ["missing"],
        "intended_next_action": "Run one reviewed experiment.",
        "scope": {"task": "TASK"},
    }
    normalized = full.execution_controls._normalize_proposal(body)
    assert normalized["control_type"] == "timeout"
    with pytest.raises(Fault):
        full.execution_controls._dispositions(
            {"result": {"verdict": "pass", "findings": [], "covered": ["timeout:X"],
                        "dispositions": [{"id": "timeout:X", "reason": "x", "resolution": "approved"}]}},
            {"id": "X", "body": {"control_type": "assessment", "target_attempt": {"epoch": 1}}},
        )


def test_replan_preserves_unresolved_claim_recovery_gate(full, full_project):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.s.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
    full.w.reconcile(full.owner, full_project[0])
    before = full.execution_controls.admission(full.owner, task)
    assert before["allowed"] is False
    assert any(value.startswith("recovery_required:") for value in before["failures"])

    row = full.w.task(full.owner, task)
    full.w.replan(full.owner, task, row["revision"], "Review the same definition after the expired claim.")
    after = full.execution_controls.admission(full.owner, task)
    assert after["allowed"] is False
    assert any(value.startswith("recovery_required:") for value in after["failures"])
    assert full.s.one("SELECT kind FROM blocks WHERE task=? AND kind='run_unknown'", (task,))


def test_recovery_keeps_conclusive_no_progress_when_permission_is_inconclusive(full, full_project, tmp_path):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    attempt = full.s.one("SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?",
                         (task, claimed["epoch"]), True)
    body = {
        "target_attempt_epoch": claimed["epoch"],
        "target_attempt_ordinal": attempt["attempt_ordinal"],
        "target_implementer_run": attempt["implementer_run"],
        "control_type": "recovery",
        "requested_seconds": None,
        "old_effective_seconds": None,
        "cause_analysis": "The observed run needs a separate recovery decision.",
        "experiment_estimate": {"seconds": 1},
        "evidence": [attempt["implementer_receipt"]],
        "intended_next_action": "Wait for an independently reviewed recovery decision.",
        "scope": {"task": task},
        "recovery_action": "Reassess the observed run through the normal gate.",
    }
    proposal = full.execution_controls.propose(
        full.owner, task, full.w.task(full.owner, task)["revision"], body
    )
    reviewer = tmp_path / "recovery-review.py"
    reviewer.write_text(
        "import json, sys\n"
        "p=json.load(sys.stdin); markers=p['context']['required_coverage']\n"
        "d=[]\n"
        "for marker in markers:\n"
        "    resolution='no_progress' if marker.startswith('attempt:') else 'inconclusive'\n"
        "    d.append({'id':marker,'resolution':resolution,'reason':'Separate typed fixture disposition'})\n"
        "print(json.dumps({'verdict':'pass','rationale':'fixture','covered':markers,'findings':[],"
        "'observations':[{'ref':p['subject'],'detail':'fixture observation'}],'dispositions':d}))\n"
    )
    reviewer.chmod(0o755)
    full.rt.adapters.register(full.owner, "recovery-inconclusive", "fixture", sys.executable, [str(reviewer)])
    review = full.rt.review(full.owner, proposal["id"], "execution_control", "recovery-inconclusive")
    result = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert result["status"] == "proposed" and result["inconclusive"] is True
    assert result["judgment"] == "no_progress" and result["recovery"] == "inconclusive"
    assert result["assessment"] is not None and result["authorization"] is None
    assert full.w.task(full.owner, task)["no_progress_count"] == 1


def test_apply_requires_governance_review_before_any_recovery_effect(full, full_project, monkeypatch):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.s.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
    full.w.reconcile(full.owner, full_project[0])
    claim_event = full.s.one(
        "SELECT id FROM events WHERE project=? AND kind='task_claimed' "
        "AND json_extract(body,'$.task')=? ORDER BY seq DESC LIMIT 1",
        (full_project[0], task), True,
    )["id"]
    body = {
        "target_attempt_epoch": claimed["epoch"],
        "target_attempt_ordinal": 1,
        "control_type": "recovery",
        "requested_seconds": None,
        "old_effective_seconds": None,
        "cause_analysis": "A claim expired before an implementer run was observed.",
        "experiment_estimate": {"seconds": 1},
        "evidence": [claim_event],
        "intended_next_action": "Return through the normal reviewed admission gate.",
        "scope": {"task": task},
        "recovery_action": "Reconcile the durable claim and lease evidence.",
    }
    proposal = full.execution_controls.propose(
        full.owner, task, full.w.task(full.owner, task)["revision"], body
    )
    monkeypatch.setattr(full.g, "evidence_for", lambda *args: [{"id": "failed-review"}])
    observed = {}

    def reject_review(*args, **kwargs):
        observed["args"] = args
        raise Fault("review_failed", "The observed reviewer process failed")

    monkeypatch.setattr(full.g, "require_review", reject_review)
    with pytest.raises(Fault) as error:
        full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], "failed-review")
    assert error.value.code == "review_failed"
    assert observed["args"][0] == "failed-review"
    assert full.s.one("SELECT id FROM execution_control_authorizations WHERE proposal=?", (proposal["id"],)) is None
    assert full.w.task(full.owner, task)["no_progress_count"] == 0


def test_reviewed_recovery_consumption_allows_ready_claim_and_execute(full, full_project, tmp_path):
    """Exercise the producer path after recovery approval, including auth reuse checks."""
    task = make_task(full, full_project)
    project = full_project[0]
    claimed = full.w.claim(full.owner, project, task)
    full.s.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
    full.w.reconcile(full.owner, project)

    claim_event = full.s.one(
        "SELECT id FROM events WHERE project=? AND kind='task_claimed' "
        "AND json_extract(body,'$.task')=? ORDER BY seq DESC LIMIT 1",
        (project, task),
        True,
    )["id"]
    row = full.w.task(full.owner, task)
    full.w.replan(full.owner, task, row["revision"], "Reassess the expired claim before the next reviewed attempt.")
    full.w.plan_tests(
        full.owner,
        task,
        {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                     "kind": "pytest", "required_tests": ["test_add"]}]},
    )

    proposal_body = {
        "target_attempt_epoch": claimed["epoch"],
        "target_attempt_ordinal": claimed["attempts"],
        "target_implementer_run": None,
        "control_type": "recovery",
        "requested_seconds": None,
        "old_effective_seconds": None,
        "cause_analysis": "The durable claim expired before an implementer run was observed.",
        "experiment_estimate": {"seconds": 1},
        "evidence": [claim_event],
        "intended_next_action": "Return through the ordinary reviewed ready, claim and execute gates.",
        "scope": {"task": task},
        "recovery_action": "Admit one reassessment while preserving the expired claim history.",
    }
    proposal = full.execution_controls.propose(full.owner, task, full.w.task(full.owner, task)["revision"], proposal_body)
    reviewer = tmp_path / "recovery-approved.py"
    reviewer.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "markers = list(payload['context']['required_coverage'])\n"
        "print(json.dumps({'verdict': 'pass', 'rationale': 'Observed claim and lease evidence support one reassessment.',\n"
        "                  'covered': markers, 'findings': [],\n"
        "                  'observations': [{'ref': payload['subject'], 'detail': 'Reviewed durable claim evidence.'}],\n"
        "                  'dispositions': [{'id': marker, 'resolution': 'approved', 'reason': 'The retained claim is expired and no run was observed.'} for marker in markers]}))\n"
    )
    reviewer.chmod(0o755)
    full.rt.adapters.register(full.owner, "recovery-approved", "fixture", sys.executable, [str(reviewer)])
    review = full.rt.review(full.owner, proposal["id"], "execution_control", "recovery-approved")
    applied = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert applied["authorization"] is not None

    ready = full.w.ready(full.owner, task)
    assert ready["status"] == "ready"
    next_claim = full.w.claim(full.owner, project, task)
    assert next_claim["epoch"] > claimed["epoch"]
    attempt = full.s.one(
        "SELECT body,digest FROM execution_attempts WHERE task=? AND attempt_epoch=?",
        (task, next_claim["epoch"]),
        True,
    )
    assert json.loads(attempt["body"])["recovery_authorization"] == applied["authorization"]
    executed = full.rt.execute(full.owner, task, "fixture")
    assert executed["status"] == "submitted"


def test_successful_rejected_reviews_still_require_recovery(full, full_project, tmp_path):
    """A successful run rejected by completion reviewers remains recoverable evidence."""
    task = make_task(full, full_project)
    project = full_project[0]
    first_claim = full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    rejected = tmp_path / "rejected-review.py"
    rejected.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "markers = list(payload.get('context', {}).get('task', {}).get('acceptance', []))\n"
        "print(json.dumps({'verdict': 'fail', 'rationale': 'The candidate needs correction.',\n"
        "                  'covered': markers, 'findings': [],\n"
        "                  'observations': [{'ref': payload['subject'], 'detail': 'Observed review rejection.'}],\n"
        "                  'dispositions': []}))\n"
    )
    rejected.chmod(0o755)
    full.rt.adapters.register(full.owner, "rejected-review", "fixture", sys.executable, [str(rejected)])
    for role in ("spec", "quality", "test_adequacy"):
        result = full.rt.review(full.owner, task, role, "rejected-review")
        assert result["result"]["verdict"] == "fail"

    row = full.w.task(full.owner, task)
    full.w.replan(full.owner, task, row["revision"], "Reassess the rejected candidate before another attempt.")
    assert any(value.startswith("recovery_required:") for value in full.execution_controls.admission(full.owner, task)["failures"])
    full.w.plan_tests(
        full.owner,
        task,
        {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                     "kind": "pytest", "required_tests": ["test_add"]}]},
    )
    attempt = full.s.one(
        "SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?",
        (task, first_claim["epoch"]),
        True,
    )
    proposal_body = {
        "target_attempt_epoch": first_claim["epoch"],
        "target_attempt_ordinal": attempt["attempt_ordinal"],
        "target_implementer_run": attempt["implementer_run"],
        "control_type": "recovery",
        "requested_seconds": None,
        "old_effective_seconds": None,
        "cause_analysis": "The implementer completed but independent completion reviews rejected the candidate.",
        "experiment_estimate": {"seconds": 1},
        "evidence": [attempt["implementer_receipt"]],
        "intended_next_action": "Admit one reviewed reassessment through the ordinary gates.",
        "scope": {"task": task},
        "recovery_action": "Reassess the rejected implementation without classifying quality rejection as no-progress.",
    }
    proposal = full.execution_controls.propose(full.owner, task, full.w.task(full.owner, task)["revision"], proposal_body)
    recovery = tmp_path / "recovery-approved-observed.py"
    recovery.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "markers = list(payload['context']['required_coverage'])\n"
        "def resolution(marker):\n"
        "    return 'progress' if marker.startswith('attempt:') else 'approved'\n"
        "print(json.dumps({'verdict': 'pass', 'rationale': 'The rejected candidate is explicitly admitted for reassessment.',\n"
        "                  'covered': markers, 'findings': [],\n"
        "                  'observations': [{'ref': payload['subject'], 'detail': 'Reviewed the retained run and rejection evidence.'}],\n"
        "                  'dispositions': [{'id': marker, 'resolution': resolution(marker), 'reason': 'Independent typed recovery decision.'} for marker in markers]}))\n"
    )
    recovery.chmod(0o755)
    full.rt.adapters.register(full.owner, "recovery-approved-observed", "fixture", sys.executable, [str(recovery)])
    review = full.rt.review(full.owner, proposal["id"], "execution_control", "recovery-approved-observed")
    applied = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert applied["authorization"] is not None and applied["assessment"] is not None
    full.w.ready(full.owner, task)
    full.w.claim(full.owner, project, task)
    assert full.rt.execute(full.owner, task, "fixture")["status"] == "submitted"
