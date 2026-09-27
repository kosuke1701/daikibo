"""Dev19 read-only execution progress reporting boundaries."""
from __future__ import annotations

import json
import sys

import pytest

from conftest import finish_task, make_task
from daikibo.common import Fault, canonical, digest, uid


def _register_review(control, tmp_path, name, *, verdict="pass", resolution="acceptable", exit_code=0):
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, sys\n"
        f"VERDICT = {verdict!r}\n"
        f"RESOLUTION = {resolution!r}\n"
        f"EXIT = {exit_code!r}\n"
        "payload = json.load(sys.stdin)\n"
        "context = payload.get('context', {})\n"
        "markers = list(context.get('required_coverage', context.get('task', {}).get('acceptance', [])))\n"
        "if payload.get('role') == 'execution_control':\n"
        "    dispositions = [{'id': marker, 'resolution': ('inconclusive' if marker.startswith('attempt:') and RESOLUTION == 'inconclusive' else ('approved' if marker.startswith(('recovery:', 'timeout:')) else RESOLUTION)), 'reason': 'progress reporting fixture'} for marker in markers]\n"
        "else:\n"
        "    dispositions = []\n"
        "print(json.dumps({'verdict': VERDICT, 'rationale': 'progress reporting fixture', 'covered': markers,\n"
        "                  'findings': [], 'observations': [{'ref': payload.get('subject', 'unknown'), 'detail': 'fixture observation'}],\n"
        "                  'dispositions': dispositions}))\n"
        "sys.exit(EXIT)\n"
    )
    script.chmod(0o755)
    control.rt.adapters.register(control.owner, name, "fixture", sys.executable, [str(script)])
    return name


def _completion_review(control, task, tmp_path, name="completion-pass", **kwargs):
    adapter = _register_review(control, tmp_path, name, **kwargs)
    return [control.rt.review(control.owner, task, role, adapter) for role in ("spec", "quality", "test_adequacy")]


def _assessment_proposal(control, task, claimed):
    attempt = control.s.one("SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?",
                            (task, claimed["epoch"]), True)
    body = {
        "target_attempt_epoch": claimed["epoch"],
        "target_attempt_ordinal": attempt["attempt_ordinal"],
        "target_implementer_run": attempt["implementer_run"],
        "control_type": "assessment",
        "requested_seconds": None,
        "old_effective_seconds": None,
        "cause_analysis": "Observe the retained implementation outcome.",
        "experiment_estimate": {"seconds": 1},
        "evidence": [attempt["implementer_receipt"]],
        "intended_next_action": "Keep the observed result in the progress report.",
        "scope": {"task": task},
    }
    return control.execution_controls.propose(control.owner, task,
                                              control.w.task(control.owner, task)["revision"], body)


def _clone_implementer_run(control, run_id, *, epoch, receipt=False):
    clone_id = uid("RUN")
    control.s.execute(
        "INSERT INTO runs(id,project,task,subject,role,adapter,status,binding,epoch,worker_uid,pid,start,end,body,result) "
        "SELECT ?,project,task,subject,role,adapter,status,binding,?,?,pid,start,end,body,result FROM runs WHERE id=?",
        (clone_id, epoch, None, run_id),
    )
    receipt_id = None
    if receipt:
        receipt_id = uid("EVD")
        control.s.execute(
            "INSERT INTO receipts(id,run,project,subject,role,binding,body,key_id,mac,created) "
            "SELECT ?,?,project,subject,role,binding,body,key_id,mac,created FROM receipts WHERE run=?",
            (receipt_id, clone_id, run_id),
        )
    return clone_id, receipt_id


def test_submitted_latest_observation_is_separate_from_prior_admission(full, full_project, tmp_path):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    _completion_review(full, task, tmp_path, "reject-all", verdict="fail")
    before = full.execution_controls.admission(full.owner, task)
    counts_before = full.s.one("SELECT count(*) AS n FROM receipts WHERE project=?", (full_project[0],))["n"]
    report = full.execution_controls.progress(full.owner, task)
    latest = report["reporting"]["latest_attempt"]
    assert latest["epoch"] == claimed["epoch"]
    assert latest["implementer"]["outcome"] == "succeeded"
    assert latest["reviews"]["state"] == "rejected"
    assert report["reporting"]["next_claim"] == {
        "state": "reassess_current_result", "reference_attempt_epoch": claimed["epoch"],
        "projected_authorization": False, "explanation_code": "submitted_result_before_next_claim",
    }
    assert report["admission"] == before
    assert full.s.one("SELECT count(*) AS n FROM receipts WHERE project=?", (full_project[0],))["n"] == counts_before


def test_replanned_epoch_keeps_historical_latest_and_existing_admission(full, full_project):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    before = full.execution_controls.progress(full.owner, task)
    old_revision = full.w.task(full.owner, task)["revision"]
    full.w.replan(full.owner, task, old_revision, "Prepare a reviewed current revision.")
    full.w.plan_tests(full.owner, task, {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"], "kind": "pytest", "required_tests": ["test_add"]}]})
    after = full.execution_controls.progress(full.owner, task)
    assert after["reporting"]["context"]["task_epoch"] > claimed["epoch"]
    assert after["reporting"]["latest_attempt"]["epoch"] == before["reporting"]["latest_attempt"]["epoch"] == claimed["epoch"]
    assert after["reporting"]["next_claim"]["state"] == "prepare_ready_state"
    assert set(after["admission"]) == set(before["admission"])


def test_running_claim_waits_for_current_execution_and_does_not_self_recover(full, full_project):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    report = full.execution_controls.progress(full.owner, task)
    assert report["reporting"]["current_claim"]["epoch"] == claimed["epoch"]
    assert report["reporting"]["current_claim"]["observation"] == "unobserved"
    assert report["reporting"]["next_claim"]["state"] == "wait_for_current_execution"
    assert report["reporting"]["next_claim"]["reference_attempt_epoch"] == claimed["epoch"]
    assert not any(value.startswith("recovery_required:") for value in report["admission"]["failures"])


def test_missing_reviews_then_all_pass_stays_noncompletion_report(full, full_project, tmp_path):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    _register_review(full, tmp_path, "partial-review", verdict="pass")
    full.rt.review(full.owner, task, "spec", "partial-review")
    partial = full.execution_controls.progress(full.owner, task)
    assert partial["reporting"]["latest_attempt"]["reviews"]["state"] == "pending_or_incomplete"
    _completion_review(full, task, tmp_path)
    complete_reviews = full.execution_controls.progress(full.owner, task)
    assert complete_reviews["reporting"]["latest_attempt"]["reviews"]["state"] == "observed_passes"
    assert complete_reviews["reporting"]["latest_attempt"]["implementation_success_is_completion"] is False
    assert complete_reviews["reporting"]["next_claim"]["state"] == "reassess_current_result"
    assert full.w.task(full.owner, task)["status"] == "submitted"
    assert claimed["epoch"] == complete_reviews["reporting"]["latest_attempt"]["epoch"]


def test_failed_review_and_inconclusive_assessment_are_distinct(full, full_project, tmp_path):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    _completion_review(full, task, tmp_path, "review-process-failure", exit_code=1)
    failed = full.execution_controls.progress(full.owner, task)
    assert failed["reporting"]["latest_attempt"]["reviews"]["state"] == "pending_or_incomplete"
    assert any(item["observation"] == "execution_failed" for item in failed["reporting"]["latest_attempt"]["reviews"]["latest_by_role"])

    proposal = _assessment_proposal(full, task, claimed)
    adapter = _register_review(full, tmp_path, "assessment-inconclusive", resolution="inconclusive")
    review = full.rt.review(full.owner, proposal["id"], "execution_control", adapter)
    applied = full.execution_controls.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert applied["assessment"] is None and applied["inconclusive"] is True
    after = full.execution_controls.progress(full.owner, task)
    assert after["reporting"]["latest_attempt"]["assessment"] is None
    assert after["no_progress_count"] == 0


def test_corrected_review_replaces_display_without_erasing_failed_receipt(full, full_project, tmp_path):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    _completion_review(full, task, tmp_path, "quality-fail", verdict="fail")
    failed_count = full.s.one("SELECT count(*) AS n FROM receipts WHERE subject=? AND role='quality'", (task,))["n"]
    candidate = full.s.one("SELECT * FROM candidates WHERE id=(SELECT candidate FROM tasks WHERE id=?)", (task,), True)
    alternate_id = uid("CAND")
    alternate_body = json.loads(candidate["body"])
    alternate_body["alternate_binding_marker"] = "different-candidate-input"
    full.s.execute(
        "INSERT INTO candidates(id,task,epoch,body,digest,implementation_run,created) VALUES(?,?,?,?,?,?,?)",
        (alternate_id, task, candidate["epoch"], canonical(alternate_body).decode(), digest(alternate_body),
         candidate["implementation_run"], candidate["created"] + 0.001),
    )
    full.s.execute("UPDATE tasks SET candidate=? WHERE id=?", (alternate_id, task))
    alternate = full.execution_controls.progress(full.owner, task)
    assert alternate["reporting"]["latest_attempt"]["reviews"]["state"] == "pending_or_incomplete"
    assert alternate["reporting"]["latest_attempt"]["reviews"]["latest_by_role"] == []
    full.s.execute("UPDATE tasks SET candidate=? WHERE id=?", (candidate["id"], task))
    _completion_review(full, task, tmp_path, "quality-pass")
    report = full.execution_controls.progress(full.owner, task)
    reviews = report["reporting"]["latest_attempt"]["reviews"]
    assert reviews["state"] == "observed_passes"
    quality = next(item for item in reviews["latest_by_role"] if item["role"] == "quality")
    assert quality["observation"] == "pass"
    assert full.s.one("SELECT count(*) AS n FROM receipts WHERE subject=? AND role='quality'", (task,))["n"] == failed_count + 1


def test_legacy_unknown_and_ambiguous_runs_are_explicit(full, full_project):
    task = make_task(full, full_project)
    full.s.execute("UPDATE tasks SET attempts=3 WHERE id=?", (task,))
    policies_before = full.s.one("SELECT count(*) AS n FROM policies WHERE project=?", (full_project[0],))["n"]
    unknown = full.execution_controls.progress(full.owner, task)
    assert full.s.one("SELECT count(*) AS n FROM policies WHERE project=?", (full_project[0],))["n"] == policies_before
    assert unknown["reporting"]["latest_attempt"] is None
    assert unknown["reporting"]["next_claim"]["explanation_code"] == "legacy_attempt_details_unavailable"
    assert unknown["attempts"] == 3 and unknown["no_progress_count"] == 0

    row = full.w.task(full.owner, task)
    for suffix in ("a", "b"):
        run = uid("RUN")
        full.s.execute(
            "INSERT INTO runs(id,project,task,subject,role,adapter,status,binding,epoch,worker_uid,start,end,body) "
            "VALUES(?,?,?,?,?,'fixture','finished',?,?,?, ?, ?,?)",
            (run, row["project"], task, task, "implementer", f"binding-{suffix}", row["epoch"] + 1,
             None, 1.0, 2.0, "{}"),
        )
    ambiguous = full.execution_controls.progress(full.owner, task)
    latest = ambiguous["reporting"]["latest_attempt"]
    assert latest["observation"] == "ambiguous"
    assert latest["implementer"]["outcome"] == "ambiguous"
    assert len(latest["implementer"]["run_ids"]) == 2


def test_prior_epoch_ambiguous_history_is_reported_bounded_without_gate_relaxation(full, full_project):
    task = make_task(full, full_project)
    claimed = full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    run = full.s.one("SELECT id FROM runs WHERE task=? AND role='implementer'", (task,), True)
    full.s.execute("UPDATE runs SET epoch=2 WHERE id=?", (run["id"],))
    for index in range(17):
        _clone_implementer_run(full, run["id"], epoch=2, receipt=True)
    full.sec.event(full_project[0], "task_claimed", full.owner.id,
                   {"task": task, "epoch": 2, "attempt_ordinal": 99, "task_revision": 1})
    full.sec.event(full_project[0], "task_claimed", full.owner.id,
                   {"task": task, "epoch": 2, "attempt_ordinal": 100, "task_revision": 1})
    full.s.execute("UPDATE tasks SET epoch=3,status='ready',candidate=NULL WHERE id=?", (task,))

    report = full.execution_controls.progress(full.owner, task)
    latest = report["reporting"]["latest_attempt"]
    assert latest["epoch"] == 2
    assert latest["observation"] == "ambiguous"
    assert latest["ordinal"] is None
    assert latest["implementer"]["run_total"] == 18
    assert len(latest["implementer"]["run_ids"]) == 16
    assert latest["implementer"]["run_truncated"] is True
    assert latest["implementer"]["run_detail"]["attempt_epoch"] == 2
    assert latest["implementer"]["receipt_total"] == 18
    assert len(latest["implementer"]["receipt_ids"]) == 16
    assert latest["implementer"]["receipt_truncated"] is True
    assert report["admission"]["allowed"] is False
    assert "ambiguous_attempt" in report["admission"]["failures"]
    assert report["admission"]["diagnostic"]["non_authorizing"] is True

    def follow(pointer, **overrides):
        params = {key: value for key, value in pointer.items() if key != "route"}
        params.update(overrides)
        return full.invoke(full.owner, pointer["route"], params)

    run_pointer = latest["implementer"]["run_detail"]
    run_page = follow(run_pointer)
    assert run_page["total"] == 18
    assert run_page["next_offset"] == 16
    assert run_page["records"] == sorted(run_page["records"], key=lambda value: value["id"])
    all_run_ids = [value["id"] for value in full.s.all(
        "SELECT id FROM runs WHERE task=? AND role='implementer' AND epoch=? ORDER BY id", (task, 2))]
    full.s.execute("UPDATE runs SET status='unknown' WHERE id=?", (run_page["records"][0]["id"],))
    with pytest.raises(Fault) as error:
        follow(run_pointer, offset=run_page["next_offset"], expected_snapshot=run_page["snapshot"])
    assert error.value.code == "stale_history_detail"
    refreshed = follow(run_pointer)
    omitted = follow(run_pointer, offset=refreshed["next_offset"], expected_snapshot=refreshed["snapshot"])
    assert {record["id"] for record in refreshed["records"] + omitted["records"]} == set(all_run_ids)
    assert len(omitted["records"]) == 2

    receipt_pointer = latest["implementer"]["receipt_detail"]
    receipt_page = follow(receipt_pointer)
    receipt_tail = follow(receipt_pointer, offset=receipt_page["next_offset"],
                          expected_snapshot=receipt_page["snapshot"])
    assert {record["id"] for record in receipt_page["records"] + receipt_tail["records"]} == set(
        latest["implementer"]["receipt_ids"] + [
            value["id"] for value in full.s.all(
                "SELECT q.id FROM receipts q JOIN runs r ON r.id=q.run "
                "WHERE r.task=? AND r.role='implementer' AND r.epoch=? ORDER BY q.id",
                (task, 2),
            ) if value["id"] not in latest["implementer"]["receipt_ids"]
        ])
    assert len(receipt_tail["records"]) == 2

    project_report = full.execution_controls.project_progress(full.owner, full_project[0])
    assert project_report["items"][0]["admission"]["allowed"] is False
    assert project_report["items"][0]["reporting"]["latest_attempt"]["observation"] == "ambiguous"
    with pytest.raises(Fault) as error:
        full.execution_controls.admission(full.owner, task)
    assert error.value.code == "ambiguous_attempt"
    with pytest.raises(Fault) as error:
        full.w.claim(full.owner, full_project[0], task)
    assert error.value.code == "ambiguous_attempt"
    assert claimed["epoch"] == 1


def test_project_snapshot_tracks_new_review_without_task_row_change(full, full_project, tmp_path):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    first = full.execution_controls.project_progress(full.owner, full_project[0], limit=1)
    repeat = full.execution_controls.project_progress(full.owner, full_project[0], limit=1,
                                                      expected_snapshot=first["snapshot"])
    assert repeat["snapshot"] == first["snapshot"]
    task_row = full.s.one("SELECT revision,epoch,status,updated FROM tasks WHERE id=?", (task,), True)
    _register_review(full, tmp_path, "new-review", verdict="pass")
    full.rt.review(full.owner, task, "quality", "new-review")
    assert full.s.one("SELECT revision,epoch,status,updated FROM tasks WHERE id=?", (task,), True) == task_row
    with pytest.raises(Fault) as error:
        full.execution_controls.project_progress(full.owner, full_project[0], limit=1,
                                                  expected_snapshot=first["snapshot"])
    assert error.value.code == "stale_progress"


def test_project_progress_pages_only_requested_detail_and_indexes_claims_once(full, full_project, monkeypatch):
    tasks = [make_task(full, full_project) for _ in range(3)]
    for task in tasks[:2]:
        full.w.claim(full.owner, full_project[0], task)
        full.rt.execute(full.owner, task, "fixture")
    observed_tasks = []
    observed_indexes = []
    original_epoch = full.execution_controls._progress_epoch
    original_index = full.execution_controls._progress_claim_index

    def count_epoch(*args, **kwargs):
        observed_tasks.append(args[1])
        return original_epoch(*args, **kwargs)

    def count_index(*args, **kwargs):
        observed_indexes.append(args[0])
        return original_index(*args, **kwargs)

    monkeypatch.setattr(full.execution_controls, "_progress_epoch", count_epoch)
    monkeypatch.setattr(full.execution_controls, "_progress_claim_index", count_index)
    page = full.execution_controls.project_progress(full.owner, full_project[0], limit=1)
    assert page["total"] == 3
    assert page["next_offset"] == 1
    assert observed_tasks == [tasks[0]]
    assert observed_indexes == [full_project[0]]


def test_project_stamp_streams_compact_rows_without_evidence_body_reads(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    statements = []
    full.s.conn.set_trace_callback(statements.append)
    try:
        with full.s.transaction():
            stamp = full.execution_controls._progress_evidence_stamp(full_project[0])
    finally:
        full.s.conn.set_trace_callback(None)
    assert len(stamp) == 64
    evidence_queries = [statement.lower() for statement in statements
                        if any(f" from {table} " in statement.lower()
                               for table in ("tasks", "runs", "receipts"))]
    assert evidence_queries
    assert all("body" not in statement and "result" not in statement for statement in evidence_queries)


def test_completed_and_cancelled_tasks_are_not_work_recommendations(full, full_project):
    completed = make_task(full, full_project)
    finish_task(full, full_project[0], completed)
    completed_report = full.execution_controls.progress(full.owner, completed)
    assert completed_report["reporting"]["next_claim"]["state"] == "not_applicable"
    assert completed_report["reporting"]["next_claim"]["explanation_code"] == "completed"

    cancelled = make_task(full, full_project)
    full.w.cancel(full.owner, cancelled, "Retain cancellation as terminal history.")
    cancelled_report = full.execution_controls.progress(full.owner, cancelled)
    assert cancelled_report["reporting"]["next_claim"]["state"] == "not_applicable"
    assert cancelled_report["reporting"]["next_claim"]["explanation_code"] == "cancelled"


def test_running_lifecycle_with_terminal_evidence_and_pause_preserves_explanation(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    full.s.execute("UPDATE tasks SET status='running',paused=1 WHERE id=?", (task,))
    report = full.execution_controls.progress(full.owner, task)
    next_claim = report["reporting"]["next_claim"]
    assert report["reporting"]["latest_attempt"]["observation"] == "terminal"
    assert next_claim["state"] == "reassess_current_result"
    assert next_claim["explanation_code"] == "paused"
    assert next_claim["lifecycle_explanation_code"] == "current_execution_observed_terminal"


def test_no_profile_snapshot_read_is_digest_equivalent_without_blob_writes(full, full_project, monkeypatch):
    task = make_task(full, full_project)
    row = full.w.task(full.owner, task)
    calls = []
    original_blob_put = full.s.blob_put

    def record_blob_put(data):
        calls.append(len(data))
        return original_blob_put(data)

    monkeypatch.setattr(full.s, "blob_put", record_blob_put)
    stored = full.rt.task_snapshot(full.owner, row, store_blobs=True)
    assert calls
    calls.clear()
    before_files = {path for path in full.s.blobs.rglob("*") if path.is_file()}
    readonly = full.rt.task_snapshot(full.owner, row, store_blobs=False)
    after_files = {path for path in full.s.blobs.rglob("*") if path.is_file()}
    assert readonly["digest"] == stored["digest"]
    assert calls == []
    assert after_files == before_files

    dependency = make_task(full, full_project)
    finish_task(full, full_project[0], dependency)
    dependent = make_task(full, full_project, deps=[dependency])
    dependent_row = full.w.task(full.owner, dependent)
    calls.clear()
    stored_with_dependency = full.rt.task_snapshot(full.owner, dependent_row, store_blobs=True)
    calls.clear()
    before_files = {path for path in full.s.blobs.rglob("*") if path.is_file()}
    readonly_with_dependency = full.rt.task_snapshot(full.owner, dependent_row, store_blobs=False)
    after_files = {path for path in full.s.blobs.rglob("*") if path.is_file()}
    assert readonly_with_dependency["digest"] == stored_with_dependency["digest"]
    assert calls == []
    assert after_files == before_files
