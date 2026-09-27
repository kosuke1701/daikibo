"""Finite regression for current Task-dependent plan validation."""
from __future__ import annotations

import copy
import json

import pytest

from daikibo.common import Fault, parse_json
from test_consumer_p_mr_integration import _actual_review_adapter
from test_task_checkpoint_connection import _execution_counts, _make_task, _plan_body


def _material_rows(full, project, task):
    rows = []
    for row in full.s.all(
        "SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY created,id",
        (project,),
    ):
        envelope = parse_json(row["body"])
        if envelope.get("material_kind") != "test_plan":
            continue
        payload = parse_json(full.s.blob_get(envelope["payload_blob"]))
        if payload.get("task") == task:
            rows.append((row["id"], row["digest"], payload))
    return rows


def _freeze_rows(full, project, task):
    return [
        (row["id"], row["body"])
        for row in full.s.all(
            "SELECT id,body FROM events WHERE project=? AND kind='test_plan_frozen' ORDER BY seq",
            (project,),
        )
        if parse_json(row["body"]).get("task") == task
    ]


def _state(full, project, task):
    return {
        "task": full.s.one("SELECT id,revision,body,status,validity,candidate FROM tasks WHERE id=?", (task,), True),
        "plan": full.s.one("SELECT task,body,digest,approved FROM plans WHERE task=?", (task,)),
        "materials": _material_rows(full, project, task),
        "events": _freeze_rows(full, project, task),
        "counts": _execution_counts(full, task),
    }


def test_current_production_rechecks_inventory_after_real_revision(
    full, full_project, tmp_path, monkeypatch,
):
    project, repository, requirement, _root = full_project
    task = full.w.create(full.owner, project, {
        "title": "analysis before revision",
        "goal": "Research",
        "read_artifacts": [requirement],
        "write_paths": [".daikibo-research/a.py"],
        "acceptance": ["AC-ADD"],
        "dependencies": [],
        "repos": [repository],
        "non_goals": [],
        "phase": "feasibility",
    })["id"]
    old = full.w.task(full.owner, task)
    revised = {
        key: copy.deepcopy(value)
        for key, value in old["body"].items()
        if key not in {"task_kind", "phase"}
    }
    revised["write_paths"] = ["calc.py"]
    revised["goal"] = "Implement"
    proposal = full.task_revisions.propose(
        full.owner, task, old["revision"], revised, "Real production revision",
    )
    _actual_review_adapter(full, tmp_path)
    review = full.rt.review(full.owner, proposal["id"], "impact", "p-mr-markers")

    original = full.w.task
    pending = [True]
    state_after_revision = []

    def interleave(actor, ident):
        row = original(actor, ident)
        if ident == task and pending[0]:
            pending[0] = False
            full.task_revisions.apply(
                full.owner, proposal["id"], proposal["digest"], review["receipt"],
            )
            state_after_revision.append(_state(full, project, task))
        return row

    monkeypatch.setattr(full.w, "task", interleave)
    with pytest.raises(Fault) as denied:
        full.w.plan_tests(full.owner, task, {
            "checks": [{
                "id": "command", "argv": ["python", "-c", "pass"],
                "kind": "command", "purpose": "not measured inventory",
            }],
        })
    assert denied.value.code == "test_inventory_required"
    assert state_after_revision and _state(full, project, task) == state_after_revision[0]
    current = full.w.task(full.owner, task)
    assert current["revision"] == 2
    assert current["body"]["task_kind"] == "production"


def test_production_measured_inventory_still_pins(full, full_project):
    project, _repository, _requirement, _root = full_project
    task = _make_task(full, full_project)
    result = full.w.plan_tests(full.owner, task, _plan_body())
    assert result["task"] == task
    assert full.s.one("SELECT task FROM plans WHERE task=?", (task,), True)["task"] == task
    assert len(_material_rows(full, project, task)) == 1


def test_analysis_command_plan_remains_valid(full, full_project):
    project, repository, requirement, _root = full_project
    task = full.w.create(full.owner, project, {
        "title": "bounded analysis",
        "goal": "Research",
        "read_artifacts": [requirement],
        "write_paths": [".daikibo-research/notes.txt"],
        "acceptance": ["AC-ADD"],
        "dependencies": [],
        "repos": [repository],
        "non_goals": [],
        "phase": "feasibility",
    })["id"]
    full.w.plan_tests(full.owner, task, {
        "checks": [{
            "id": "command", "argv": ["python", "-c", "pass"],
            "kind": "command", "purpose": "bounded analysis observation",
        }],
    })
    plan = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    assert json.loads(plan["body"])["checks"][0]["kind"] == "command"
    assert len(_material_rows(full, project, task)) == 1
