"""Direct boundaries for the shared execution-record identity helper."""
from __future__ import annotations

import copy
import json

import pytest

from conftest import make_task
from daikibo.common import Fault, parse_json
from daikibo.execution_record import execution_record_consistency
from test_task_test_evidence import _check, _observe_failed_check


def _records(control, receipt):
    run = control.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],), True)
    row = control.s.one("SELECT * FROM receipts WHERE id=?", (receipt["id"],), True)
    return run, parse_json(run["body"]), parse_json(run["result"]), row, copy.deepcopy(receipt)


def _successful_task_records(control, project):
    task = make_task(control, project)
    control.w.claim(control.owner, project[0], task)
    control.rt.execute(control.owner, task, "fixture")
    receipt_id = control.rt.tests(control.owner, task)["checks"][0]["receipt"]
    return task, _records(control, control.g.receipt(receipt_id))


@pytest.mark.parametrize("field", ["project", "subject", "role", "binding", "task", "epoch", "id"])
def test_execution_record_rejects_missing_identity_keys(full, full_project, field):
    _task, values = _successful_task_records(full, full_project)
    execution_record_consistency(*values)
    run, body, result, row, observed = values
    if field == "id":
        run.pop("id")
        row.pop("run")
        observed.pop("run")
    else:
        run.pop(field, None)
        row.pop(field, None)
        observed.pop(field, None)
    row["body"] = json.dumps(observed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(Fault):
        execution_record_consistency(run, body, result, row, observed)


@pytest.mark.parametrize(
    ("field", "value"),
    [("project", ""), ("role", 7), ("binding", None), ("task", ""),
     ("id", 7), ("epoch", True), ("epoch", -1)],
)
def test_execution_record_rejects_malformed_identity_values(full, full_project, field, value):
    _task, values = _successful_task_records(full, full_project)
    run, body, result, row, observed = values
    run[field] = value
    with pytest.raises(Fault):
        execution_record_consistency(run, body, result, row, observed)


def test_execution_record_preserves_nullable_task_for_taskless_producer(full, full_project):
    review = full.rt.review(full.owner, full_project[2], "requirements", "fixture")
    values = _records(full, full.g.receipt(review["receipt"]))
    assert values[0]["task"] is None and values[0]["epoch"] is None
    assert values[4]["task"] is None and values[4]["epoch"] is None
    execution_record_consistency(*values)


def test_execution_record_accepts_a_real_failed_observation(full, full_project):
    task, _values = _successful_task_records(full, full_project)
    failed = _observe_failed_check(full, full_project[0], task, _check(full, task))
    observed = execution_record_consistency(*_records(full, failed))
    assert observed.receipt["exit_code"] != 0
    assert observed.receipt["failure"] is not None
