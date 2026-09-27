"""Deterministic Task-check recapture regressions for Unit 2b criteria."""
from __future__ import annotations

import copy
import json

import pytest

from conftest import make_task
from daikibo.assurance_criteria import _check_match
from daikibo.common import digest, parse_json
from test_e3_unit2b_definition_authority import _observed_ref


def _capture_pair(full, full_project):
    project, _repo, _requirement, _root = full_project
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    assert receipt["result"]["passed"] is True

    captures = {}
    for row in full.s.all(
            "SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
            (project,),
    ):
        envelope = parse_json(row["body"])
        if envelope.get("material_kind") != "test_plan":
            continue
        payload = parse_json(full.s.blob_get(envelope["payload_blob"]))
        if payload.get("task") != task:
            continue
        operation = envelope.get("captured_from", {}).get("operation")
        if operation not in {"task.plan_tests", "task.tests"}:
            continue
        check = payload["plan_body"]["checks"][0]
        captures[operation] = {
            "kind": "test_plan_check",
            "project": project,
            "plan": {
                "kind": "test_plan",
                "project": project,
                "task": task,
                "task_revision": payload["task_revision"],
                "plan_digest": payload["plan_digest"],
                "pin": {"id": row["id"], "digest": row["digest"]},
            },
            "check_id": check["id"],
            "check_digest": digest(check),
        }

    assert set(captures) == {"task.plan_tests", "task.tests"}
    assert captures["task.plan_tests"]["plan"]["pin"] != captures["task.tests"]["plan"]["pin"]
    return task, receipt, captures


def _match(full, full_project, receipt, target):
    project = full_project[0]
    return _check_match(
        full,
        full.owner,
        {"source_ref": target},
        _observed_ref(project, receipt),
        target,
        "execution_of",
    )


def test_both_valid_capture_pins_match_the_same_observation(full, full_project):
    _task, receipt, captures = _capture_pair(full, full_project)

    for target in captures.values():
        full.assurance.resolve_pinned(full.owner, target)
        match = _match(full, full_project, receipt, target)
        assert match[0] is True and match[1] is False, match


@pytest.mark.parametrize(
    "change",
    ["unknown_pin", "digest_only", "check", "revision", "foreign",
     "task_body", "plan_body", "observed_dependency"],
)
def test_exact_capture_authority_remains_required(full, full_project, change):
    task, receipt, captures = _capture_pair(full, full_project)
    project = full_project[0]
    target = copy.deepcopy(captures["task.plan_tests"])

    if change == "unknown_pin":
        target["plan"]["pin"]["id"] = "AOBJ-missing"
    elif change == "digest_only":
        target["plan"].pop("pin")
    elif change == "check":
        target["check_digest"] = "0" * 64
    elif change == "revision":
        target["plan"]["task_revision"] += 1
    elif change == "foreign":
        target["plan"]["task"] = make_task(full, full_project)
    elif change == "task_body":
        row = full.s.one("SELECT body FROM tasks WHERE id=?", (task,))
        body = parse_json(row["body"])
        body["title"] = "changed definition"
        full.s.execute("UPDATE tasks SET body=? WHERE id=?", (json.dumps(body), task))
    elif change == "plan_body":
        row = full.s.one("SELECT body FROM plans WHERE task=?", (task,))
        body = parse_json(row["body"])
        body["checks"][0]["purpose"] = "changed public plan"
        full.w.replan(full.owner, task, full.w.task(full.owner, task)["revision"],
                      "public plan change")
        full.w.plan_tests(full.owner, task, body)
    else:
        row = full.s.one("SELECT body FROM runs WHERE id=?", (receipt["run"],))
        body = parse_json(row["body"])
        body["verification_material"] = {"id": "AOBJ-missing", "digest": "0" * 64}
        full.s.execute("UPDATE runs SET body=? WHERE id=?", (json.dumps(body), receipt["run"]))

    match = _match(full, full_project, receipt, target)
    assert match[0] is False, {"change": change, "match": match, "target": target,
                               "project": project}
