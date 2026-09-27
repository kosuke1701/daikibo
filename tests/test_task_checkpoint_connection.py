"""Finite Unit 1/A checks for transactional Task test-plan definition pins."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from daikibo.common import Fault, parse_json


def _make_task(full, full_project):
    project, repository, requirement, _root = full_project
    return full.w.create(full.owner, project, {
        "title": "Definition pin task",
        "goal": "WRITE:" + json.dumps({"calc.py": "def add(a, b):\n    return a + b\n"}),
        "read_artifacts": [requirement], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [],
    })["id"]


def _plan_body(*, rationale=None):
    body = {"checks": [{
        "id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
        "kind": "pytest", "required_tests": ["test_add"],
    }]}
    if rationale is not None:
        body["rationale"] = rationale
    return body


def _plan_materials(full, project, task):
    found = []
    rows = full.s.all(
        "SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY created,id",
        (project,),
    )
    for row in rows:
        envelope = parse_json(row["body"])
        if envelope.get("material_kind") != "test_plan":
            continue
        payload = parse_json(full.s.blob_get(envelope["payload_blob"]))
        if payload.get("task") == task:
            found.append((row, envelope, payload))
    return found


def _plan_ref(full, project, task, material):
    row, _envelope, payload = material
    return {
        "kind": "test_plan", "project": project, "task": task,
        "task_revision": payload["task_revision"],
        "plan_digest": payload["plan_digest"],
        "pin": {"id": row["id"], "digest": row["digest"]},
    }


def _execution_counts(full, task):
    return {
        "candidates": full.s.one("SELECT count(*) AS n FROM candidates WHERE task=?", (task,))["n"],
        "runs": full.s.one("SELECT count(*) AS n FROM runs WHERE subject=?", (task,))["n"],
        "receipts": full.s.one("SELECT count(*) AS n FROM receipts WHERE subject=?", (task,))["n"],
    }


def _freeze_events(full, project, task):
    return [
        (row["id"], parse_json(row["body"]))
        for row in full.s.all(
            "SELECT id,body FROM events WHERE project=? AND kind='test_plan_frozen' ORDER BY seq",
            (project,),
        )
        if parse_json(row["body"]).get("task") == task
    ]


def test_plan_tests_pins_definition_before_execution_and_stays_reviewable(full, full_project):
    project, _repository, _requirement, _root = full_project
    task = _make_task(full, full_project)
    body = _plan_body()

    result = full.w.plan_tests(full.owner, task, body)
    plan = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    materials = _plan_materials(full, project, task)

    assert result == {"task": task, "digest": plan["digest"]}
    assert len(materials) == 1
    material, envelope, payload = materials[0]
    assert envelope["captured_from"]["operation"] == "task.plan_tests"
    assert payload == {
        "task": task, "task_revision": 1,
        "plan_body": body, "plan_digest": plan["digest"],
    }
    assert _execution_counts(full, task) == {"candidates": 0, "runs": 0, "receipts": 0}
    assert len(_freeze_events(full, project, task)) == 1
    assert _freeze_events(full, project, task)[0][1]["digest"] == plan["digest"]

    ref = _plan_ref(full, project, task, materials[0])
    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["resolution"]["current"] is True
    review = full.rt.review(full.owner, task, "test_plan", "fixture")
    assert full.g.receipt(review["receipt"])["role"] == "test_plan"


def test_recapture_changes_exact_pin_but_not_definition_semantics(full, full_project):
    project, _repository, _requirement, _root = full_project
    task = _make_task(full, full_project)
    full.w.plan_tests(full.owner, task, _plan_body())
    first = _plan_materials(full, project, task)[0]
    task_row = full.w.task(full.owner, task)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)

    second_ref, second_pin = full.rt.verification_materials.pin_test_plan(
        full.owner, project, task_row, plan_row,
        captured_from={"controller": "runtime", "operation": "task.plan_tests.recapture",
                       "capture_id": "VMAT-RECAPTURE-A"},
    )
    materials = _plan_materials(full, project, task)
    assert len(materials) == 2
    assert second_pin == second_ref["pin"]
    assert materials[0][2] == materials[1][2]
    assert materials[0][1]["semantic_digest"] == materials[1][1]["semantic_digest"]
    assert materials[0][0]["id"] != materials[1][0]["id"]
    assert materials[0][0]["digest"] != materials[1][0]["digest"]

    before = full.supervisor.state_digest(project)
    assert full.assurance.resolve_pinned(full.owner, _plan_ref(full, project, task, materials[0]))["resolution"]["current"] is True
    assert full.supervisor.state_digest(project) == before


def test_plan_change_invalidates_old_pin_but_retains_historical_material(full, full_project):
    project, _repository, _requirement, _root = full_project
    task = _make_task(full, full_project)
    full.w.plan_tests(full.owner, task, _plan_body())
    old = _plan_materials(full, project, task)[0]
    old_ref = _plan_ref(full, project, task, old)

    full.w.plan_tests(full.owner, task, _plan_body(rationale="A changed plan identity"))
    historical = full.assurance.resolve_pinned(full.owner, old_ref)
    assert historical["resolution"]["payload"]["plan_digest"] == old_ref["plan_digest"]
    assert historical["resolution"]["current"] is False
    with pytest.raises(Fault) as exc:
        full.assurance._resolve_locator(full.owner, old_ref, current=True)
    assert exc.value.code == "stale_reference"
    assert len(_plan_materials(full, project, task)) == 2


def test_task_revision_invalidates_plan_pin_without_erasing_history(full, full_project, tmp_path):
    project, _repository, _requirement, _root = full_project
    task = _make_task(full, full_project)
    full.w.plan_tests(full.owner, task, _plan_body())
    old = _plan_materials(full, project, task)[0]
    old_ref = _plan_ref(full, project, task, old)

    current = full.w.task(full.owner, task)
    revised_body = {key: copy.deepcopy(value) for key, value in current["body"].items()
                    if key != "task_kind"}
    revised_body["title"] = "Definition pin task revised"
    proposal = full.task_revisions.propose(
        full.owner, task, current["revision"], revised_body,
        "Change the implementation definition while preserving the requirement",
    )
    reviewer = Path(tmp_path) / "revision_reviewer.py"
    reviewer.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "print(json.dumps({'verdict':'pass','rationale':'bounded revision protocol fixture',"
        "'covered':p['context']['required_coverage'],'findings':[],"
        "'observations':[{'ref':p['subject'],'detail':'fixture received the revision'}],"
        "'dispositions':[]}))\n"
    )
    full.rt.adapters.register(full.owner, "revision-reviewer", "fixture", sys.executable, [str(reviewer)])
    review = full.rt.review(full.owner, proposal["id"], "impact", "revision-reviewer")
    full.task_revisions.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])

    historical = full.assurance.resolve_pinned(full.owner, old_ref)
    assert historical["resolution"]["payload"]["task_revision"] == 1
    assert historical["resolution"]["current"] is False
    with pytest.raises(Fault) as exc:
        full.assurance._resolve_locator(full.owner, old_ref, current=True)
    assert exc.value.code == "stale_reference"
    history = full.task_revisions.history(full.owner, task)
    assert history["total"] == 1
    assert history["complete_since_creation"] is True


def test_pin_failure_rolls_back_plan_event_and_material_object(full, full_project, monkeypatch):
    project, _repository, _requirement, _root = full_project
    task = _make_task(full, full_project)
    before_state = full.supervisor.state_digest(project)
    before_material_ids = [
        row["id"] for row in full.s.all(
            "SELECT id FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
            (project,),
        )
    ]
    before_events = _freeze_events(full, project, task)
    original = full.w.verification_materials.pin_test_plan

    def pin_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise Fault("test_pin_failure", "Definition pin failure fixture")

    monkeypatch.setattr(full.w.verification_materials, "pin_test_plan", pin_then_fail)
    with pytest.raises(Fault) as exc:
        full.w.plan_tests(full.owner, task, _plan_body())
    assert exc.value.code == "test_pin_failure"
    assert full.s.one("SELECT * FROM plans WHERE task=?", (task,)) is None
    assert _freeze_events(full, project, task) == before_events
    after_material_ids = [
        row["id"] for row in full.s.all(
            "SELECT id FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
            (project,),
        )
    ]
    assert after_material_ids == before_material_ids
    assert full.supervisor.state_digest(project) == before_state
    assert _execution_counts(full, task) == {"candidates": 0, "runs": 0, "receipts": 0}
