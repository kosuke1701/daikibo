"""The shared, read-only formal-test selection contract for Task reviews."""
from __future__ import annotations

import json
import sys

import pytest

from conftest import make_task
from daikibo.common import Actor, Fault, canonical, digest, parse_json


def _candidate_snapshot(control, task):
    row = control.s.one("SELECT candidate FROM tasks WHERE id=?", (task,), True)
    candidate = control.s.one("SELECT body FROM candidates WHERE id=?", (row["candidate"],), True)
    body = parse_json(candidate["body"])
    return body["snapshot"]


def _check(control, task, check_id="unit"):
    plan = control.s.one("SELECT body FROM plans WHERE task=?", (task,), True)
    return next(check for check in parse_json(plan["body"])["checks"] if check["id"] == check_id)


def _observe_failed_check(control, project, task, check):
    snapshot = _candidate_snapshot(control, task)
    binding = control.g.task_binding(task)
    return control.rt.observe(
        project,
        task,
        task,
        "test:" + check["id"],
        None,
        binding,
        snapshot,
        lambda work, home, cwd: ([sys.executable, "-c", "raise SystemExit(1)"], None),
        timeout=30,
        epoch=control.w.task(control.owner, task)["epoch"],
        check=check,
    )[0]


def test_formal_test_receipt_is_in_public_selection_and_review_prompt(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    run_result = full.rt.execute(full.owner, task, "fixture")
    assert run_result["status"] == "submitted"
    tests = full.rt.tests(full.owner, task)
    snapshot = _candidate_snapshot(full, task)
    binding = full.g.task_binding(task)

    selected = full.g.task_test_evidence(
        full.owner, task, binding=binding, snapshot_digest=snapshot["digest"])
    item = selected["checks"][0]
    assert item["status"] == "executed"
    assert item["selected_receipt"] == tests["checks"][0]["receipt"]
    assert item["check_digest"] == digest(_check(full, task))
    assert item["observed_summary"]["passed"] is True
    assert item["observed_summary"]["input_mutated"] is False
    assert item["history_read_ref"]["operation"] == "evidence.get"
    assert item["observed_summary"]["report_blob_read_ref"]["operation"] == "blob.read"

    public = full.invoke(full.owner, "task.test_evidence", {"task": task})
    assert public["selection_digest"] == selected["selection_digest"]
    evidence = full.invoke(full.owner, "evidence.get", {"evidence": item["selected_receipt"]})
    assert evidence["id"] == item["selected_receipt"]
    report_blob = evidence["result"]["report_blob"]
    report = full.invoke(full.owner, "blob.read", {"blob": report_blob, "project": full_project[0], "limit": 256})
    assert report["sha256"] == report_blob
    reviewer = Actor("test-reviewer", "agent", full_project[0])
    reviewer_report = full.invoke(reviewer, "blob.read", {"blob": report_blob, "project": full_project[0], "limit": 256})
    assert reviewer_report["sha256"] == report_blob

    review = full.rt.review(full.owner, task, "test_adequacy", "fixture")
    review_run = full.s.one("SELECT body FROM runs WHERE id=?", (review["run"],), True)
    prompt = parse_json(full.s.blob_get(parse_json(review_run["body"])["input_digest"]))
    assert prompt["context"]["test_evidence"]["selection_digest"] == selected["selection_digest"]
    prompt_item = prompt["context"]["test_evidence"]["checks"][0]
    assert prompt_item["selected_receipt"] == item["selected_receipt"]
    review_receipt = full.g.receipt(review["receipt"])
    assert review_receipt["review_test_evidence"]["selection_digest"] == selected["selection_digest"]
    # Adding the review's own receipt cannot change the test-only selection.
    after_review = full.g.task_test_evidence(
        full.owner, task, binding=binding, snapshot_digest=snapshot["digest"])
    assert after_review["selection_digest"] == selected["selection_digest"]
    full.g.require_review(review["receipt"], task, binding, {"test_adequacy"})


def test_new_failed_observation_wins_and_stales_old_review(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    full.rt.tests(full.owner, task)
    check = _check(full, task)
    snapshot = _candidate_snapshot(full, task)
    binding = full.g.task_binding(task)
    before = full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=snapshot["digest"])
    old_receipt = before["checks"][0]["selected_receipt"]
    review = full.rt.review(full.owner, task, "quality", "fixture")

    failed = _observe_failed_check(full, full_project[0], task, check)
    after = full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=snapshot["digest"])
    item = after["checks"][0]
    assert item["selected_receipt"] == failed["id"]
    assert item["selected_receipt"] != old_receipt
    assert item["status"] == "failed"
    assert item["observed_summary"]["passed"] is False
    assert after["selection_digest"] != before["selection_digest"]
    with pytest.raises(Fault) as error:
        full.g.require_review(review["receipt"], task, binding, {"quality"})
    assert error.value.code == "stale_evidence"


def test_invalid_new_receipt_is_exposed_without_falling_back_to_pass(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    tests = full.rt.tests(full.owner, task)
    check = _check(full, task)
    snapshot = _candidate_snapshot(full, task)
    binding = full.g.task_binding(task)
    old = full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=snapshot["digest"])
    old_id = old["checks"][0]["selected_receipt"]
    failed = _observe_failed_check(full, full_project[0], task, check)
    stdout = full.g.receipt(failed["id"])["stdout_blob"]
    full.s.blob_path(stdout).write_bytes(b"corrupted formal test log")

    current = full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=snapshot["digest"])
    item = current["checks"][0]
    assert item["selected_receipt"] == failed["id"]
    assert item["selected_receipt"] != old_id
    assert item["status"] == "invalid"
    assert item["reason"] in {"integrity_error", "invalid_evidence"}
    assert tests["checks"][0]["receipt"] == old_id


@pytest.mark.parametrize("field", ["snapshot", "result"])
def test_run_receipt_crossfield_mismatch_is_not_executed(full, full_project, field):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    receipt_id = full.rt.tests(full.owner, task)["checks"][0]["receipt"]
    receipt = full.g.receipt(receipt_id)
    run = full.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    if field == "snapshot":
        body = parse_json(run["body"])
        assert body["snapshot"] == receipt["snapshot"]
        body["snapshot"] = "0" * 64
        full.s.execute("UPDATE runs SET body=? WHERE id=?", (canonical(body).decode(), run["id"]))
    else:
        assert parse_json(run["result"]) == receipt["result"]
        full.s.execute("UPDATE runs SET result=? WHERE id=?",
                       (canonical({"passed": False, "error": "corrupted execution result"}).decode(), run["id"]))
    selected = full.invoke(full.owner, "task.test_evidence", {"task": task})["checks"][0]
    assert selected["selected_receipt"] == receipt_id
    assert selected["status"] in {"invalid", "unknown"}


def test_current_row_epoch_conflict_cannot_hide_newest_failed_test(full, full_project):
    task = make_task(full, full_project)
    full.w.claim(full.owner, full_project[0], task)
    full.rt.execute(full.owner, task, "fixture")
    old = full.rt.tests(full.owner, task)["checks"][0]["receipt"]
    failed = _observe_failed_check(full, full_project[0], task, _check(full, task))
    before = full.invoke(full.owner, "task.test_evidence", {"task": task})["checks"][0]
    assert before["selected_receipt"] == failed["id"] and before["status"] == "failed"
    full.s.execute("UPDATE runs SET epoch=epoch+1 WHERE id=?", (failed["run"],))
    selected = full.invoke(full.owner, "task.test_evidence", {"task": task})["checks"][0]
    assert selected["selected_receipt"] == failed["id"]
    assert selected["selected_receipt"] != old
    assert selected["status"] in {"invalid", "unknown"}


def test_test_evidence_pages_are_exact_and_digest_bound(full, full_project):
    pid, rid, req, _ = full_project
    task = full.w.create(full.owner, pid, {
        "title": "two measured checks", "goal": "WRITE:" + json.dumps({"calc.py": "def add(a,b):\n    return a+b\n"}),
        "read_artifacts": [req], "write_paths": ["calc.py"], "acceptance": ["AC-ADD"],
        "dependencies": [], "repos": [rid], "non_goals": [],
    })["id"]
    full.w.plan_tests(full.owner, task, {"checks": [
        {"id": "unit-a", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"], "kind": "pytest", "report": "a.xml"},
        {"id": "unit-b", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"], "kind": "pytest", "report": "b.xml"},
    ]})
    full.w.ready(full.owner, task)
    full.w.claim(full.owner, pid, task)
    full.rt.execute(full.owner, task, "fixture")
    full.rt.tests(full.owner, task)
    snapshot = _candidate_snapshot(full, task)
    binding = full.g.task_binding(task)
    first = full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=snapshot["digest"], limit=1)
    assert first["total"] == 2
    assert first["next_offset"] == 1
    second = full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=snapshot["digest"],
                                       offset=first["next_offset"], limit=1,
                                       expected_selection_digest=first["selection_digest"])
    assert len(second["checks"]) == 1
    assert second["next_offset"] is None
    assert second["selection_digest"] == first["selection_digest"]

    _observe_failed_check(full, pid, task, _check(full, task, "unit-a"))
    with pytest.raises(Fault) as error:
        full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=snapshot["digest"],
                                  limit=1, expected_selection_digest=first["selection_digest"])
    assert error.value.code == "stale_evidence"


def test_unobserved_checks_are_explicit_and_test_plan_review_stays_available(full, full_project):
    task = make_task(full, full_project)
    binding = full.g.task_binding(task)
    current = full.g.task_test_evidence(full.owner, task, binding=binding, snapshot_digest=None)
    assert current["checks"][0]["status"] == "unobserved"
    assert current["selection_current"] is True
    proposal_review = full.rt.review(full.owner, task, "test_plan", "fixture")
    receipt = full.g.receipt(proposal_review["receipt"])
    assert receipt["role"] == "test_plan"
    assert "review_test_evidence" not in receipt or receipt["review_test_evidence"] is None
