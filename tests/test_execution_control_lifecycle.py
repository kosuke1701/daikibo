"""End-to-end execution-control lifecycle coverage.

The adapters in this module are deterministic subprocesses.  They exercise the
managed Runtime review/receipt path and deliberately do not represent live
semantic or independent acceptance.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import make_task
from daikibo.common import Fault
from daikibo.knowledge_history import inspect_archive


def _register_review_adapter(control, tmp_path, name, *, attempt="progress", timeout="rejected", recovery="rejected"):
    """Register a subprocess reviewer which echoes exact required markers."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, sys\n"
        f"ATTEMPT = {attempt!r}\n"
        f"TIMEOUT = {timeout!r}\n"
        f"RECOVERY = {recovery!r}\n"
        "payload = json.load(sys.stdin)\n"
        "context = payload.get('context', {})\n"
        "markers = list(context.get('required_coverage', []))\n"
        "dispositions = []\n"
        "for marker in markers:\n"
        "    if marker.startswith('attempt:'):\n"
        "        resolution = ATTEMPT\n"
        "    elif marker.startswith('timeout:'):\n"
        "        resolution = TIMEOUT\n"
        "    elif marker.startswith('recovery:'):\n"
        "        resolution = RECOVERY\n"
        "    else:\n"
        "        resolution = 'inconclusive'\n"
        "    dispositions.append({'id': marker, 'resolution': resolution, 'reason': 'lifecycle protocol fixture'})\n"
        "print(json.dumps({'verdict': 'pass', 'rationale': 'Deterministic lifecycle protocol fixture; not semantic review.',\n"
        "                  'covered': markers, 'findings': [],\n"
        "                  'observations': [{'ref': payload.get('subject', 'unknown'), 'detail': 'Observed fixture input.'}],\n"
        "                  'dispositions': dispositions}))\n"
    )
    control.rt.adapters.register(control.owner, name, "fixture", sys.executable, [str(script)])
    return name


def _register_writer(control, tmp_path, name, content):
    """Register a real implementer subprocess with deterministic changed output."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.load(sys.stdin)\n"
        "if payload.get('task'):\n"
        f"    Path('calc.py').write_text({content!r})\n"
        "print(json.dumps({'message': 'lifecycle implementation fixture ran'}))\n"
    )
    control.rt.adapters.register(control.owner, name, "fixture", sys.executable, [str(script)])
    return name


def _reviewed_requirement(control, project, source, adapter="fixture"):
    requirement = control.k.propose(
        control.owner,
        project,
        "requirement",
        {
            "title": "Reviewed execution control requirement",
            "statement": "The execution-control policy is source grounded.",
            "acceptance": ["AC-POLICY"],
            "source_refs": [source],
        },
    )
    review = control.rt.review(control.owner, requirement["id"], "requirements", adapter)
    control.k.accept(control.owner, requirement["id"], 1, review["receipt"])
    return control.k.artifact(control.owner, requirement["id"])


def _execution_policy_proposal(control, full_project, tmp_path):
    project = full_project[0]
    source = control.s.one("SELECT id,blob FROM sources WHERE project=? ORDER BY created LIMIT 1", (project,), True)
    requirement = _reviewed_requirement(control, project, source["id"])
    old_policy = control.g.policy(project)
    pending = control.g.policy_propose(control.owner, project, old_policy["body"])
    body = {
        "source": {"id": source["id"], "digest": source["blob"]},
        "requirement": {"id": requirement["id"], "revision": requirement["revision"], "digest": requirement["digest"]},
        "expected_policy": {"revision": old_policy["revision"], "digest": old_policy["digest"]},
        "supersedes": [{"id": pending["id"], "digest": pending["digest"]}],
        "reason": "Adopt the reviewed execution-control policy and supersede the older pending instruction.",
    }
    proposal = control.execution_controls.policy_propose(control.owner, project, body)
    review_adapter = _register_review_adapter(control, tmp_path, "policy_consistency", attempt="progress")
    review = control.rt.review(control.owner, proposal["id"], "consistency", review_adapter)
    return project, pending, proposal, review


def _proposal_body(control, task, claimed, control_type, *, requested_seconds=None, evidence=None):
    attempt = control.s.one(
        "SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?",
        (task, claimed["epoch"]),
        True,
    )
    if evidence is None:
        evidence = [attempt["implementer_receipt"]] if attempt["implementer_receipt"] else []
    body = {
        "target_attempt_epoch": claimed["epoch"],
        "target_attempt_ordinal": attempt["attempt_ordinal"],
        "target_implementer_run": attempt["implementer_run"],
        "control_type": control_type,
        "requested_seconds": requested_seconds,
        "old_effective_seconds": None,
        "cause_analysis": "The observed attempt needs a bounded reviewed lifecycle decision.",
        "experiment_estimate": {"seconds": requested_seconds or 1},
        "evidence": evidence,
        "intended_next_action": "Continue through the ordinary reviewed execution gate.",
        "scope": {"task": task, "epoch": claimed["epoch"]},
    }
    if control_type == "recovery":
        body["target_implementer_run"] = None
        body["recovery_action"] = "Reassess the durable claim and return through normal currentness gates."
    return body


def _claim_and_execute(control, project, task, adapter):
    claimed = control.w.claim(control.owner, project, task)
    result = control.rt.execute(control.owner, task, adapter)
    attempt = control.s.one(
        "SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?",
        (task, claimed["epoch"]),
        True,
    )
    receipt = control.g.receipt(result["receipt"])
    assert attempt["implementer_run"] == receipt["run"]
    assert attempt["implementer_receipt"] == result["receipt"]
    return claimed, result, attempt


def _assess(control, project, task, claimed, tmp_path, *, judgment="progress", name="assessment"):
    proposal = control.execution_controls.propose(
        control.owner,
        task,
        control.w.task(control.owner, task)["revision"],
        _proposal_body(control, task, claimed, "assessment"),
    )
    adapter = _register_review_adapter(control, tmp_path, name, attempt=judgment)
    review = control.rt.review(control.owner, proposal["id"], "execution_control", adapter)
    result = control.execution_controls.apply(control.owner, proposal["id"], proposal["digest"], review["receipt"])
    return proposal, review, result


def _reset_for_next_attempt(control, project, task, *, ready=True):
    """Replan the same definition through the public route before another claim."""
    row = control.w.task(control.owner, task)
    control.w.replan(control.owner, task, row["revision"], "Reassess the same definition for the next lifecycle attempt.")
    control.w.plan_tests(
        control.owner,
        task,
        {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"], "kind": "pytest", "required_tests": ["test_add"]}]},
    )
    if ready:
        control.w.ready(control.owner, task)


def test_policy_propose_review_apply_supersedes_pending_instruction(full, full_project, tmp_path):
    project, pending, proposal, review = _execution_policy_proposal(full, full_project, tmp_path)

    applied = full.execution_controls.policy_apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert applied["status"] == "applied"
    assert full.s.one("SELECT response FROM decisions WHERE id=?", (proposal["id"],))["response"] is None
    assert full.s.one("SELECT status FROM decisions WHERE id=?", (pending["id"],))["status"] == "superseded"
    assert full.s.one("SELECT status FROM inbox WHERE ref=?", (pending["id"],))["status"] == "resolved"
    event = full.s.one("SELECT body FROM events WHERE project=? AND kind='execution_policy_applied' ORDER BY seq DESC", (project,), True)
    assert any(value["decision"] == pending["id"] for value in json.loads(event["body"])["supersedes"])


@pytest.mark.parametrize("judgment", ["progress", "no_progress"])
def test_three_total_attempts_remain_admissible(full, full_project, tmp_path, judgment):
    task = make_task(full, full_project)
    project = full_project[0]
    for ordinal in range(1, 4):
        adapter = _register_writer(full, tmp_path, f"writer_{judgment}_{ordinal}", f"def add(a,b):\n    return a+b+{ordinal}\n")
        claimed, _result, _attempt = _claim_and_execute(full, project, task, adapter)
        _assess(full, project, task, claimed, tmp_path, judgment=judgment, name=f"assessment_{judgment}_{ordinal}")
        if ordinal < 3:
            _reset_for_next_attempt(full, project, task)

    row = full.w.task(full.owner, task)
    assert row["attempts"] == 3
    if judgment == "progress":
        assert row["no_progress_count"] == 0
        assert full.execution_controls.admission(full.owner, task)["allowed"] is True
    else:
        assert row["no_progress_count"] == 3
        assert full.execution_controls.admission(full.owner, task)["allowed"] is False
        assert full.s.one("SELECT kind FROM blocks WHERE task=? AND kind='no_progress'", (task,))


def test_inconclusive_then_conclusive_assessment_is_exactly_once(full, full_project, tmp_path):
    task = make_task(full, full_project)
    claimed, _result, _attempt = _claim_and_execute(full, full_project[0], task, "fixture")
    proposal = full.execution_controls.propose(
        full.owner,
        task,
        full.w.task(full.owner, task)["revision"],
        _proposal_body(full, task, claimed, "assessment"),
    )
    inconclusive_adapter = _register_review_adapter(full, tmp_path, "inconclusive", attempt="inconclusive")
    first_review = full.rt.review(full.owner, proposal["id"], "execution_control", inconclusive_adapter)
    first = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], first_review["receipt"])
    assert first["status"] == "proposed" and first["inconclusive"] is True
    assert full.s.one("SELECT count(*) AS n FROM attempt_assessments WHERE task=?", (task,))["n"] == 0

    conclusive_adapter = _register_review_adapter(full, tmp_path, "conclusive", attempt="progress")
    second_review = full.rt.review(full.owner, proposal["id"], "execution_control", conclusive_adapter)
    second = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], second_review["receipt"])
    replay = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], second_review["receipt"])
    assert second["assessment"] is not None and second["judgment"] == "progress"
    assert replay["replayed"] is True
    assert full.s.one("SELECT count(*) AS n FROM attempt_assessments WHERE task=?", (task,))["n"] == 1
    assert full.w.task(full.owner, task)["no_progress_count"] == 0


def test_claim_only_recovery_authorization_has_no_fabricated_assessment(full, full_project, tmp_path):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.s.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
    full.w.reconcile(full.owner, full_project[0])
    event_id = next(
        row["id"]
        for row in full.s.all("SELECT id,body FROM events WHERE project=? AND kind='task_claimed' ORDER BY seq", (full_project[0],))
        if json.loads(row["body"]).get("task") == task and json.loads(row["body"]).get("epoch") == claimed["epoch"]
    )
    proposal = full.execution_controls.propose(
        full.owner,
        task,
        full.w.task(full.owner, task)["revision"],
        _proposal_body(full, task, claimed, "recovery", evidence=[event_id]),
    )
    adapter = _register_review_adapter(full, tmp_path, "claim_recovery", recovery="approved")
    review = full.rt.review(full.owner, proposal["id"], "execution_control", adapter)
    result = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert result["authorization"] is not None
    assert result["assessment"] is None
    auth = full.s.one("SELECT requested_seconds,effective_seconds,assessment FROM execution_control_authorizations WHERE id=?", (result["authorization"],), True)
    assert auth["requested_seconds"] is None and auth["effective_seconds"] is None and auth["assessment"] is None
    assert full.s.one("SELECT count(*) AS n FROM attempt_assessments WHERE task=?", (task,))["n"] == 0


def test_core_claim_only_attempt_is_retained_by_archive_roundtrip(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    baseline = full.k.baseline(full.owner, full_project[0])
    archive = full.history.export_archive(full.owner, baseline["id"])
    inspected = inspect_archive(archive["path"], archive["sha256"])
    assert inspected["verified"] is True
    assert inspected["counts"]["execution_attempts"] == 1
    spec = full.k.export(full.owner, full_project[0])
    attempt = spec["execution_control_history"]["execution_attempts"][0]
    assert attempt["status"] == "claimed"
    assert attempt["implementer_run"] is None and attempt["implementer_receipt"] is None
    assert spec["runtime_restore_supported"] is False


def test_core_reserved_attempt_is_retained_by_archive_roundtrip(full, full_project):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.execution_controls.reserve_implementer(full.owner, task, claimed["epoch"])
    attempt = full.s.one("SELECT status,implementer_run,implementer_receipt FROM execution_attempts WHERE task=?", (task,), True)
    assert attempt["status"] == "reserved"
    assert attempt["implementer_run"] is None and attempt["implementer_receipt"] is None
    baseline = full.k.baseline(full.owner, full_project[0])
    archive = full.history.export_archive(full.owner, baseline["id"])
    inspected = inspect_archive(archive["path"], archive["sha256"])
    assert inspected["verified"] is True
    assert inspected["counts"]["execution_attempts"] == 1


def test_execution_control_policy_apply_requires_latest_review(full,full_project,tmp_path):
    project,_,proposal,first=_execution_policy_proposal(full,full_project,tmp_path)
    script=tmp_path/'policy-fail-review.py'
    script.write_text('''import json,sys
p=json.load(sys.stdin);c=p.get('context',{});markers=c.get('required_coverage',[])
print(json.dumps({'verdict':'fail','rationale':'The later fixture review fails.','covered':markers,
 'findings':[],'observations':[{'ref':p.get('subject','policy'),'detail':'Observed the reviewed policy packet.'}],
 'dispositions':[]}))
''')
    full.rt.adapters.register(full.owner,'policy_fail_review','fixture',sys.executable,[str(script)])
    failed=full.rt.review(full.owner,proposal['id'],'consistency','policy_fail_review')
    assert failed['result']['verdict']=='fail'
    with pytest.raises(Fault) as stale:
        full.execution_controls.policy_apply(full.owner,proposal['id'],proposal['digest'],first['receipt'])
    assert stale.value.code=='stale_evidence'
    assert full.g.policy(project)['revision']==1

    fresh=full.rt.review(full.owner,proposal['id'],'consistency','policy_consistency')
    assert fresh['result']['verdict']=='pass'
    result=full.execution_controls.policy_apply(full.owner,proposal['id'],proposal['digest'],fresh['receipt'])
    assert result['status']=='applied'
    assert full.g.policy(project)['revision']==2


def test_core_claim_event_survives_schema12_migration_and_legacy_archive_roundtrip(full, full_project, tmp_path):
    """A real core claim remains selectable after migration without backfill."""
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    legacy_home = tmp_path / "legacy-control"
    legacy_home.mkdir()
    full.s.backup_database(legacy_home / "state.sqlite3")
    shutil.copytree(full.s.home / "blobs", legacy_home / "blobs")

    # Model the supported schema-12 migration boundary using a database
    # produced by the current workflow.  The claim event, task telemetry and
    # source material remain core-produced; only schema-13 tables/column are
    # removed to represent an older retained database.
    old_db = sqlite3.connect(legacy_home / "state.sqlite3")
    old_db.executescript(
        """
        PRAGMA foreign_keys=OFF;
        DROP TRIGGER IF EXISTS execution_attempts_identity_immutable;
        DROP TRIGGER IF EXISTS execution_attempts_no_delete;
        DROP TRIGGER IF EXISTS attempt_assessments_immutable;
        DROP TRIGGER IF EXISTS attempt_assessments_no_delete;
        DROP TRIGGER IF EXISTS execution_control_proposals_identity_immutable;
        DROP TRIGGER IF EXISTS execution_control_proposals_no_delete;
        DROP TRIGGER IF EXISTS execution_control_packets_immutable;
        DROP TRIGGER IF EXISTS execution_control_packets_no_delete;
        DROP TRIGGER IF EXISTS execution_control_events_immutable;
        DROP TRIGGER IF EXISTS execution_control_events_no_delete;
        DROP TRIGGER IF EXISTS execution_control_authorizations_immutable;
        DROP TRIGGER IF EXISTS execution_control_authorizations_no_delete;
        DROP TABLE execution_control_authorizations;
        DROP TABLE program_origins;
        DROP TABLE execution_control_events;
        DROP TABLE execution_control_packets;
        DROP TABLE execution_control_proposals;
        DROP TABLE attempt_assessments;
        DROP TABLE execution_attempts;
        ALTER TABLE tasks DROP COLUMN no_progress_count;
        PRAGMA user_version=12;
        """,
    )
    # Set the deterministic lease expiry separately while preserving the same
    # migration boundary.
    old_db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
    old_db.execute("PRAGMA user_version=12")
    old_db.commit()
    old_db.close()

    reopened = __import__("daikibo.control", fromlist=["Control"]).Control(legacy_home, mode="validation", start_workers=False)
    try:
        owner = reopened.sec.authenticate(None)
        if reopened.w.task(owner, task)["status"] == "running":
            reopened.w.reconcile(owner, full_project[0])
        assert reopened.w.task(owner, task)["validity"] == "needs_review"
        event_row = next(
            row for row in reopened.s.all("SELECT id,body FROM events WHERE project=? AND kind='task_claimed' ORDER BY seq", (full_project[0],))
            if json.loads(row["body"]).get("task") == task and json.loads(row["body"]).get("epoch") == claimed["epoch"]
        )
        proposal_body = {
            "target_attempt_epoch": claimed["epoch"],
            "target_attempt_ordinal": claimed["attempts"],
            "target_implementer_run": None,
            "control_type": "recovery",
            "requested_seconds": None,
            "old_effective_seconds": None,
            "cause_analysis": "The migrated durable claim has no observed implementer run.",
            "experiment_estimate": {"seconds": 1},
            "evidence": [event_row["id"]],
            "intended_next_action": "Review the retained claim before another normal claim.",
            "scope": {"task": task},
            "recovery_action": "Reassess the durable claim and preserve its unknown history.",
        }
        proposal = reopened.execution_controls.propose(owner, task, reopened.w.task(owner, task)["revision"], proposal_body)
        spec = reopened.k.export(owner, full_project[0])
        proposal_row = spec["execution_control_history"]["execution_control_proposals"][0]
        target = proposal_row["body"]["target_attempt"]
        assert target["legacy"] is True and target["claim_only"] is True
        assert target["run"] is None and target["receipt"] is None
        baseline = reopened.k.baseline(owner, full_project[0])
        archive = reopened.history.export_archive(owner, baseline["id"])
        inspected = inspect_archive(archive["path"], archive["sha256"])
        assert inspected["verified"] is True
        assert inspected["counts"]["execution_control_proposals"] == 1
        assert proposal["id"] == proposal_row["id"]
    finally:
        reopened.close()


def test_reviewed_timeout_over_24h_survives_next_claim_and_expires_on_input_change(full, full_project, tmp_path):
    task = make_task(full, full_project)
    project = full_project[0]
    claimed, _result, _attempt = _claim_and_execute(full, project, task, "fixture")
    # A completed candidate is re-planned through the ordinary public route.
    # The new revision retains the historical attempt while requiring a fresh
    # frozen test plan; the timeout proposal is then bound to this revision.
    _reset_for_next_attempt(full, project, task, ready=False)
    proposal = full.execution_controls.propose(
        full.owner,
        task,
        full.w.task(full.owner, task)["revision"],
        _proposal_body(full, task, claimed, "timeout", requested_seconds=172800),
    )
    adapter = _register_review_adapter(full, tmp_path, "long_timeout", attempt="progress", timeout="approved")
    review = full.rt.review(full.owner, proposal["id"], "execution_control", adapter)
    result = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert result["effective_seconds"] == 172800.0
    assert full.execution_controls.resolve_timeout(full.owner, task, 172800)["seconds"] == 172800.0

    # A subsequent claim keeps the exact reviewed Task revision and semantic
    # inputs, so the duration remains current through normal admission.
    full.w.ready(full.owner, task)
    next_claim = full.w.claim(full.owner, project, task)
    # Replanning fences the completed lease once; the new claim fences it
    # again.  The reviewed timeout still targets the observed epoch.
    assert next_claim["epoch"] == claimed["epoch"] + 2
    assert full.execution_controls.resolve_timeout(full.owner, task, 172800)["authorization"] is not None

    source = full.s.one("SELECT id FROM sources WHERE project=? ORDER BY created LIMIT 1", (project,), True)
    new_requirement = full.k.propose(
        full.owner,
        project,
        "requirement",
        {"title": "Changed input", "statement": "A changed accepted input", "acceptance": ["AC-NEW"], "source_refs": [source["id"]]},
    )
    full.k.accept(full.owner, new_requirement["id"], 1)
    old_body = dict(full.w.task(full.owner, task)["body"])
    old_body.pop("task_kind", None)
    old_body["read_artifacts"] = [new_requirement["id"]]
    revision = full.task_revisions.propose(full.owner, task, full.w.task(full.owner, task)["revision"], old_body, "Change the reviewed input artifact.")
    impact_adapter = _register_review_adapter(full, tmp_path, "input_change", attempt="progress")
    impact = full.rt.review(full.owner, revision["id"], "impact", impact_adapter)
    full.task_revisions.apply(full.owner, revision["id"], revision["digest"], impact["receipt"])
    assert full.execution_controls.current_authorization(full.owner, task, 172800) is None
    with pytest.raises(Fault) as error:
        full.execution_controls.resolve_timeout(full.owner, task, 172800)
    assert error.value.code == "timeout_authorization_required"
