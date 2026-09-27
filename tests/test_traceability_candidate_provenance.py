"""Finite archive regressions for candidate implementation provenance."""
from __future__ import annotations

import copy

import pytest

from daikibo.common import Fault, digest
from daikibo.candidate_provenance import (
    resolve_candidate_identity,
    resolve_candidate_pin,
    validate_execution_record_consistency,
)
from daikibo.execution_errors import failure as execution_failure
from daikibo.task_revisions import validate_history_record
from daikibo.traceability import inspect_archive
from daikibo.traceability_refs import TraceabilityRefResolver, _LivePinnedContext
from test_traceability_refs import refs_fixture
from test_traceability_unit_b_contract_repair import (
    _decisions,
    _leaf_ids,
    _mapping,
    _write_traceability_archive,
)


@pytest.mark.parametrize("section", ["runs", "receipts", "repos"])
def test_candidate_archive_requires_saved_provenance_context(refs_fixture, tmp_path, section):
    fixture = refs_fixture
    control = fixture["control"]
    decision = _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])])
    _mapping(control, fixture, decision, _leaf_ids(fixture)[0], fixture["candidate"])
    payload, blobs = control.traceability._archive_payload(control.owner, fixture["project"])
    good = tmp_path / "candidate-provenance-good.zip"
    _write_traceability_archive(payload, blobs, good)
    assert inspect_archive(good)["verified"]

    payload["context"][section] = []
    bad = tmp_path / f"candidate-provenance-missing-{section}.zip"
    _write_traceability_archive(payload, blobs, bad)
    with pytest.raises(Fault) as error:
        inspect_archive(bad)
    assert error.value.code == "invalid_archive"


@pytest.mark.parametrize(
    "mutation",
    [
        "run_missing", "run_task", "run_role", "run_epoch", "run_status", "run_body",
        "receipt_missing", "receipt_binding", "receipt_body", "repository_missing",
        "repository_name", "snapshot_size", "receipt_cas_missing",
    ],
)
def test_candidate_archive_rejects_provenance_cross_field_mismatch(refs_fixture, tmp_path, mutation):
    """Recomputed archive envelopes cannot erase a candidate's typed provenance."""
    fixture = refs_fixture
    control = fixture["control"]
    decision = _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])])
    _mapping(control, fixture, decision, _leaf_ids(fixture)[0], fixture["candidate"])
    payload, blobs = control.traceability._archive_payload(control.owner, fixture["project"])

    candidate_id = fixture["candidate"]["candidate"]
    candidate = next(row for row in payload["context"]["candidates"] if row["id"] == candidate_id)
    run_id = candidate["implementation_run"]
    receipt_id = candidate["body"]["implementation_receipt"]
    run = next(row for row in payload["context"]["runs"] if row["id"] == run_id)
    receipt = next(row for row in payload["context"]["receipts"] if row["id"] == receipt_id)
    repository = fixture["candidate"]["repository"]

    if mutation == "run_missing":
        payload["context"]["runs"] = [row for row in payload["context"]["runs"] if row["id"] != run_id]
    elif mutation == "run_task":
        run["task"] = "TASK-FOREIGN"
    elif mutation == "run_role":
        run["role"] = "reviewer"
    elif mutation == "run_epoch":
        run["epoch"] += 1
    elif mutation == "run_status":
        run["status"] = "running"
    elif mutation == "run_body":
        run["body"]["tampered"] = True
    elif mutation == "receipt_missing":
        payload["context"]["receipts"] = [row for row in payload["context"]["receipts"] if row["id"] != receipt_id]
    elif mutation == "receipt_binding":
        receipt["binding"] = "BINDING-TAMPERED"
    elif mutation == "receipt_body":
        receipt["body"]["binding"] = "BINDING-TAMPERED"
    elif mutation == "repository_missing":
        payload["context"]["repos"] = [row for row in payload["context"]["repos"] if row["id"] != repository]
    elif mutation == "repository_name":
        repo = next(row for row in payload["context"]["repos"] if row["id"] == repository)
        repo["name"] = "renamed-after-capture"
    elif mutation == "snapshot_size":
        entry = candidate["body"]["snapshot"]["repos"][repository]["files"]["calc.py"]
        entry["size"] += 1
    else:
        receipt_blob = receipt["body"].get("stdout_blob")
        assert receipt_blob
        blobs.pop(receipt_blob)

    bad = tmp_path / f"candidate-provenance-{mutation}.zip"
    _write_traceability_archive(payload, blobs, bad)
    with pytest.raises(Fault) as error:
        inspect_archive(bad)
    assert error.value.code == "invalid_archive"


def test_generic_candidate_identity_has_an_exact_wire_shape(refs_fixture):
    fixture = refs_fixture
    control = fixture["control"]
    ref = fixture["candidate"]
    generic = {
        "kind": "candidate", "project": fixture["project"],
        "candidate": ref["candidate"], "task": ref["task"],
        "task_revision": ref["task_revision"],
        "candidate_digest": ref["candidate_digest"],
        "snapshot_digest": ref["snapshot_digest"],
    }
    context = _LivePinnedContext(TraceabilityRefResolver(control))
    resolved = resolve_candidate_identity(generic, context)
    assert resolved["canonical_ref"] == generic
    assert resolved["kind"] == "candidate"
    assert {item["kind"] for item in resolved["dependency_refs"]} >= {
        "task", "candidate", "implementation_run", "implementation_receipt", "snapshot",
    }

    for field, value in (("snapshot_digest", "0" * 64), ("task_revision", 2),
                         ("project", "PROJECT-FOREIGN")):
        bad = dict(generic)
        bad[field] = value
        with pytest.raises(Fault):
            resolve_candidate_identity(bad, context)


def test_execution_record_helper_accepts_a_complete_failed_observation(refs_fixture):
    """Record identity is independent from the caller's success policy."""
    fixture = refs_fixture
    control = fixture["control"]
    context = _LivePinnedContext(TraceabilityRefResolver(control))
    candidate = context.row("candidate", fixture["candidate"]["candidate"])
    run = context.row("run", candidate["implementation_run"])
    receipt = context.receipt_body(candidate["body"]["implementation_receipt"])
    failed_receipt = copy.deepcopy(receipt)
    failed_result = dict(failed_receipt["result"])
    failed_result["error"] = {"code": "collector_error", "message": "fixture failure"}
    failed_receipt["result"] = failed_result
    failed_receipt["failure"] = execution_failure(
        "collector_error", source="collector", message="fixture failure")
    failed_receipt["exit_code"] = 1

    # A failed observation still has a meaningful run/receipt identity.  The
    # candidate resolver separately requires a successful implementation.
    validate_execution_record_consistency(run["body"], failed_result, failed_receipt)


def test_candidate_identity_does_not_invent_history_for_a_later_current_revision(refs_fixture):
    fixture = refs_fixture
    control = fixture["control"]
    with control.s.transaction():
        control.s.execute("UPDATE tasks SET revision=2 WHERE id=?", (fixture["task"],))
    generic = {
        "kind": "candidate", "project": fixture["project"],
        "candidate": fixture["candidate"]["candidate"], "task": fixture["task"],
        "task_revision": fixture["candidate"]["task_revision"],
        "candidate_digest": fixture["candidate"]["candidate_digest"],
        "snapshot_digest": fixture["candidate"]["snapshot_digest"],
    }
    context = _LivePinnedContext(TraceabilityRefResolver(control))
    with pytest.raises(Fault):
        resolve_candidate_identity(generic, context)


def test_candidate_identity_accepts_a_retained_history_migration_boundary(refs_fixture):
    fixture = refs_fixture
    control = fixture["control"]
    task = fixture["task"]
    candidate = fixture["candidate"]
    with control.s.transaction():
        control.s.execute("UPDATE tasks SET revision=4 WHERE id=?", (task,))
    control.task_revisions.replan(control.owner, task, 4, "retained post-migration candidate history")
    generic = {
        "kind": "candidate", "project": fixture["project"],
        "candidate": candidate["candidate"], "task": task,
        "task_revision": 4, "candidate_digest": candidate["candidate_digest"],
        "snapshot_digest": candidate["snapshot_digest"],
    }
    context = _LivePinnedContext(TraceabilityRefResolver(control))
    resolved = resolve_candidate_identity(generic, context)
    assert resolved["task_revision"] == 4


def test_historical_candidate_uses_the_selected_task_definition_scope(refs_fixture, tmp_path):
    fixture = refs_fixture
    control = fixture["control"]
    context = _LivePinnedContext(TraceabilityRefResolver(control))
    generic = {
        "kind": "candidate", "project": fixture["project"],
        "candidate": fixture["candidate"]["candidate"], "task": fixture["task"],
        "task_revision": fixture["candidate"]["task_revision"],
        "candidate_digest": fixture["candidate"]["candidate_digest"],
        "snapshot_digest": fixture["candidate"]["snapshot_digest"],
    }
    second_root = tmp_path / "second-repository"
    second_root.mkdir()
    (second_root / "future.txt").write_text("future definition\n", encoding="utf-8")
    second_repository = control.sn.register(
        control.owner, fixture["project"], "second", str(second_root))["id"]
    before = control.task_revisions.snapshot(control.owner, fixture["task"])
    future_body = copy.deepcopy(before["task"]["body"])
    future_body["repos"].append(second_repository)
    future_body.pop("task_kind", None)
    future_body = control.task_revisions.validate(control.owner, before["task"], future_body)
    with control.s.transaction():
        control.task_revisions._apply(
            control.owner, before, future_body, "future task repository definition")

    # The current Task is the chain anchor at revision 2, while the candidate
    # remains pinned to revision 1 and its original repository scope.
    assert resolve_candidate_identity(generic, context)["task_revision"] == generic["task_revision"]
    assert resolve_candidate_pin(fixture["project"], fixture["candidate"], context)


def test_candidate_history_checks_all_definition_sides_before_candidate_filter(refs_fixture):
    fixture = refs_fixture
    control = fixture["control"]
    context = _LivePinnedContext(TraceabilityRefResolver(control))
    generic = {
        "kind": "candidate", "project": fixture["project"],
        "candidate": fixture["candidate"]["candidate"], "task": fixture["task"],
        "task_revision": fixture["candidate"]["task_revision"],
        "candidate_digest": fixture["candidate"]["candidate_digest"],
        "snapshot_digest": fixture["candidate"]["snapshot_digest"],
    }

    for title in ("Second definition", "Third definition"):
        before = control.task_revisions.snapshot(control.owner, fixture["task"])
        body = copy.deepcopy(before["task"]["body"])
        body["title"] = title
        body.pop("task_kind", None)
        body = control.task_revisions.validate(control.owner, before["task"], body)
        with control.s.transaction():
            control.task_revisions._apply(
                control.owner, before, body, "all-definition consistency fixture")

    history = copy.deepcopy(list(context.task_history(fixture["task"])))
    task = copy.deepcopy(context.row("task", fixture["task"]))
    for record in history:
        validate_history_record(record, fixture["project"])
    clean_history = copy.deepcopy(history)
    clean_task = copy.deepcopy(task)

    # The later history sides and current row have candidate=None, but their
    # definition body still proves the same revision and must be consistent.
    history[1]["body"]["before"]["task"]["body"]["goal"] = "conflicting middle definition"
    history[1]["digest"] = digest(history[1]["body"])
    validate_history_record(history[1], fixture["project"])

    class ChangedContext:
        def __getattr__(self, name):
            return getattr(context, name)

        def task_history(self, ident):
            return history

        def row(self, kind, ident):
            return task if kind == "task" else context.row(kind, ident)

    with pytest.raises(Fault):
        resolve_candidate_identity(generic, ChangedContext())

    history = copy.deepcopy(clean_history)
    task = copy.deepcopy(clean_task)
    task["body"]["goal"] = "conflicting current definition"
    if "body_digest" in task:
        task["body_digest"] = digest(task["body"])
    with pytest.raises(Fault):
        resolve_candidate_identity(generic, ChangedContext())
