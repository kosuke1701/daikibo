"""Regression for optional workflow input at the private candidate boundary."""
from __future__ import annotations

import json

import pytest

from daikibo.common import Fault

from test_unit3_private_candidate_stage import _prepare_selected_stage


def test_active_breakdown_membership_without_workflow_requires_stage(
    full, full_project, tmp_path,
):
    flow = _prepare_selected_stage(
        full, full_project, tmp_path, review_plan=False, omit_workflow=True,
        mature=True,
    )
    task_body = json.loads(full.s.one(
        "SELECT body FROM tasks WHERE id=?", (flow["task"],), True,
    )["body"])
    assert "workflow_id" not in task_body

    # The public view and selected mandatory profile both come from the real
    # active Breakdown/profile records.  The missing test-plan review is the
    # only current proof removed from this fixture.
    import daikibo.assurance_stage as stage
    assert stage._task_programs(full, flow["project"], flow["task"]) == [flow["program"]]
    assert stage._active_task_programs(full, flow["project"], flow["task"]) == [flow["program"]]
    assert full.assurance.selected_profile(
        full.owner, flow["project"], flow["program"],
    )["application_mode"] == "mandatory"

    with pytest.raises(Fault) as failure:
        full.rt.execute(full.owner, flow["task"], "fixture")
    assert failure.value.code == "stage_assurance_blocked"
    assert full.s.one(
        "SELECT status,candidate FROM tasks WHERE id=?", (flow["task"],), True,
    ) == {"status": "running", "candidate": None}
    assert full.s.one("SELECT id FROM candidates WHERE task=?", (flow["task"],)) is None
    receipt = full.s.one(
        "SELECT body FROM receipts WHERE subject=? ORDER BY created DESC,id DESC LIMIT 1",
        (flow["task"],), True,
    )
    assert receipt is not None
    assert json.loads(receipt["body"])["work_product"] is not None
