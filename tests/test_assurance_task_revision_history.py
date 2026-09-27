from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import make_task
from daikibo.common import Fault, canonical, digest


def _task_ref(project, task, revision, definition_digest):
    return {
        "kind": "task_revision",
        "project": project,
        "task": task,
        "revision": revision,
        "definition_digest": definition_digest,
    }


def _register_history_reviewer(full, tmp_path):
    script = Path(tmp_path) / "history_reviewer.py"
    script.write_text(
        "import json,sys\n"
        "payload=json.load(sys.stdin)\n"
        "print(json.dumps({'verdict':'pass','rationale':'deterministic history protocol fixture',"
        "'covered':payload['context']['required_coverage'],'findings':[],"
        "'observations':[{'ref':payload['subject'],'detail':'history fixture'}],"
        "'dispositions':[]}))\n"
    )
    full.rt.adapters.register(
        full.owner, "history-reviewer", "fixture", sys.executable, [str(script)]
    )


def _apply_revision(full, task, title, reviewer="history-reviewer"):
    current = full.w.task(full.owner, task)
    body = {key: value for key, value in current["body"].items() if key != "task_kind"}
    body["title"] = title
    proposal = full.task_revisions.propose(
        full.owner, task, current["revision"], body, "retain an actual historical definition"
    )
    review = full.rt.review(full.owner, proposal["id"], "impact", reviewer)
    result = full.task_revisions.apply(
        full.owner, proposal["id"], proposal["digest"], review["receipt"]
    )
    return current, result


def test_task_revision_history_uses_definition_body_and_currentness(full, full_project, monkeypatch):
    project = full_project[0]
    task = make_task(full, full_project)
    before = full.w.task(full.owner, task)

    # This is the real revision writer.  It records an immutable
    # before/after task_revision_history row through TaskRevisions._apply.
    full.w.replan(full.owner, task, before["revision"], "retain the prior definition for history")
    after = full.w.task(full.owner, task)
    assert after["revision"] == before["revision"] + 1

    historical = _task_ref(project, task, before["revision"], digest(before["body"]))
    resolved = full.assurance.resolve_pinned(full.owner, historical)
    assert resolved["resolution"]["mode"] == "task_revision"
    assert resolved["resolution"]["current"] is False
    assert resolved["resolution"]["content"]["body"] == before["body"]

    # The public currentness report preserves the historical read while
    # exposing that the same identity is stale for current use.  The strict
    # resolver path must reject it instead of silently downgrading it.
    report = full.assurance.evaluate_current(full.owner, historical)
    assert report["current"]["state"] == "stale"
    with pytest.raises(Fault) as stale:
        full.assurance._resolve_locator(full.owner, historical, current=True)
    assert stale.value.code == "stale_reference"

    current = _task_ref(project, task, after["revision"], digest(after["body"]))
    current_result = full.assurance.resolve_pinned(full.owner, current)
    assert current_result["resolution"]["current"] is True

    bad_digest = {**historical, "definition_digest": "0" * 64}
    with pytest.raises(Fault) as missing:
        full.assurance.resolve_pinned(full.owner, bad_digest)
    assert missing.value.code == "unresolved_reference"

    # A duplicated target revision is invalid even when both rows carry valid
    # checksums.  The live schema prevents this write; the read fixture keeps
    # the corruption bounded without mutating the database.
    original_all = full.s.all

    def duplicate_history(sql, args=()):
        rows = original_all(sql, args)
        if "FROM task_revision_history" in sql and rows:
            duplicate = dict(rows[0])
            duplicate["id"] = duplicate["id"] + "-duplicate"
            return rows + [duplicate]
        return rows

    monkeypatch.setattr(full.s, "all", duplicate_history)
    with pytest.raises(Fault) as duplicate:
        full.assurance.resolve_pinned(full.owner, historical)
    assert duplicate.value.code == "integrity_error"


def test_two_actual_applies_merge_equal_adjacent_snapshot_identities(full, full_project, tmp_path):
    project = full_project[0]
    task = make_task(full, full_project)
    _register_history_reviewer(full, tmp_path)

    first_before, first_result = _apply_revision(full, task, "revision two")
    second_before, second_result = _apply_revision(full, task, "revision three")
    assert first_result["revision"] == 2 and second_result["revision"] == 3

    history = [
        full.task_revisions.history_record(full.owner, first_result["history"]),
        full.task_revisions.history_record(full.owner, second_result["history"]),
    ]
    rev2_after = history[0]["body"]["after"]["task"]
    rev2_before = history[1]["body"]["before"]["task"]
    assert rev2_after["revision"] == rev2_before["revision"] == 2
    assert rev2_after["body"] == rev2_before["body"] == second_before["body"]

    ref = _task_ref(project, task, 2, digest(second_before["body"]))
    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["resolution"]["current"] is False
    assert resolved["resolution"]["content"]["body"] == second_before["body"]
    with pytest.raises(Fault) as stale:
        full.assurance._resolve_locator(full.owner, ref, current=True)
    assert stale.value.code == "stale_reference"

    current = full.w.task(full.owner, task)
    current_ref = _task_ref(project, task, 3, digest(current["body"]))
    assert full.assurance.resolve_pinned(full.owner, current_ref)["resolution"]["current"] is True


def test_history_definition_conflict_is_rejected_before_requested_digest_filter(full, full_project, tmp_path, monkeypatch):
    project = full_project[0]
    task = make_task(full, full_project)
    _register_history_reviewer(full, tmp_path)
    first_before, first_result = _apply_revision(full, task, "revision two")
    second_before, second_result = _apply_revision(full, task, "revision three")
    requested = _task_ref(project, task, 2, digest(second_before["body"]))

    original_all = full.s.all

    def contradictory_history(sql, args=()):
        rows = original_all(sql, args)
        if "FROM task_revision_history" not in sql:
            return rows
        result = []
        for raw in rows:
            row = dict(raw)
            if row["from_revision"] == 2:
                body = json.loads(row["body"])
                body["before"]["task"]["body"]["goal"] += "\ncontradictory retained definition"
                row["body"] = canonical(body).decode()
                row["digest"] = digest(body)
            result.append(row)
        return result

    monkeypatch.setattr(full.s, "all", contradictory_history)
    with pytest.raises(Fault) as conflict:
        full.assurance.resolve_pinned(full.owner, requested)
    assert conflict.value.code == "integrity_error"
    assert first_result["revision"] == 2 and second_result["revision"] == 3
