"""Portable schema-13 history checks; archives never become live authority."""
from __future__ import annotations

import copy

import pytest

from daikibo.common import Fault, digest
from daikibo.execution_control_history import SECTIONS, validate_execution_controls
from daikibo.knowledge import Knowledge, artifact_body_contract


PROJECT = "PRJ-history"
TASK = "TASK-history"


def _row_history(*, attempts=1, implementer=True, project=PROJECT):
    task_body = {"title": "History task", "statement": "Retained task material"}
    task = {
        "id": TASK, "project": project, "body": task_body, "revision": 2,
        "status": "submitted", "validity": "current", "epoch": 1,
        "lease_owner": None, "lease_until": None, "candidate": None,
        "attempts": attempts, "no_progress_count": 0,
        "paused": 0, "created": 1.0, "updated": 2.0,
    }
    attempt_body = {
        "format": "daikibo.execution-attempt.v1", "task": TASK,
        "project": project, "epoch": 1, "ordinal": attempts, "revision": 2,
        "binding": "b" * 64, "legacy_history": False,
    }
    attempt = {
        "id": "EATT-1", "task": TASK, "project": project,
        "attempt_epoch": 1, "attempt_ordinal": attempts, "task_revision": 2,
        "task_binding": "b" * 64,
        "status": "finished" if implementer else "claimed",
        "implementer_run": "RUN-impl" if implementer else None,
        "implementer_receipt": "REC-impl" if implementer else None,
        "body": attempt_body, "digest": digest(attempt_body),
        "created": 1.0, "updated": 2.0,
    }
    rows = {section: [] for section in SECTIONS}
    rows["tasks"] = [task]
    rows["execution_attempts"] = [attempt]
    return rows


def _add_applied_control(rows, *, control_type="timeout", judgment="progress"):
    """Append a core-shaped proposal, packet, event, and optional assessment."""
    attempt = rows["execution_attempts"][0]
    material = {
        "format": "daikibo.execution-control-material.v1",
        "task": {
            "id": TASK, "project": PROJECT, "revision": 2,
            "body": rows["tasks"][0]["body"], "body_digest": digest(rows["tasks"][0]["body"]),
        },
        "target_attempt": {
            "id": attempt["id"], "epoch": attempt["attempt_epoch"],
            "ordinal": attempt["attempt_ordinal"], "revision": attempt["task_revision"],
            "binding": attempt["task_binding"], "implementer_run": attempt["implementer_run"],
            "implementer_receipt": attempt["implementer_receipt"],
        },
        "semantic": {"task": TASK, "revision": 2, "inputs": []},
        "semantic_digest": digest({"task": TASK, "revision": 2, "inputs": []}),
    }
    request = {
        "target_attempt_epoch": attempt["attempt_epoch"],
        "target_attempt_ordinal": attempt["attempt_ordinal"],
        "control_type": control_type,
        "requested_seconds": 90000 if control_type == "timeout" else None,
        "old_effective_seconds": 14400,
        "cause_analysis": "The observed run needed a longer finite window.",
        "experiment_estimate": {"seconds": 90000},
        "evidence": [{"id": "REC-impl", "kind": "receipt"}],
        "intended_next_action": "Run the revised attempt after authorization.",
        "scope": {"task": TASK},
    }
    if control_type == "recovery":
        request["recovery_action"] = "Reassess the durable claim through the normal gates."
    proposal_body = {
        "format": "daikibo.execution-control-proposal.v1", "task": TASK,
        "project": PROJECT, "task_revision": 2, "request": request,
        "material": material, "material_digest": digest(material),
        "target_attempt": {
            "epoch": attempt["attempt_epoch"], "ordinal": attempt["attempt_ordinal"],
            "run": attempt["implementer_run"], "receipt": attempt["implementer_receipt"],
            "legacy": False,
        },
        "control_type": control_type,
    }
    proposal = {
        "id": "ECPROP-1", "task": TASK, "project": PROJECT, "task_revision": 2,
        "body": proposal_body, "digest": digest(proposal_body),
        "binding": digest({"proposal": "ECPROP-1", "body": proposal_body}),
        "status": "applied", "result": None, "created": 4.0,
    }
    packet_body = {
        "format": "daikibo.execution-control-packet.v1", "proposal": proposal["id"],
        "proposal_digest": proposal["digest"], "ordinal": 0, "start": 0, "end": 1,
        "total": 1, "material_digest": digest(material),
        "required_coverage": [f"attempt:{attempt['attempt_epoch']}"],
        "material": copy.deepcopy(material),
    }
    packet = {
        "id": "ECPKT-1", "proposal": proposal["id"], "project": PROJECT,
        "ordinal": 0, "body": packet_body, "digest": digest(packet_body), "created": 4.5,
    }
    rows["execution_control_proposals"] = [proposal]
    rows["execution_control_packets"] = [packet]

    assessment_id = None
    if judgment is not None:
        assessment_id = "EASM-1"
        assessment_body = {
            "format": "daikibo.attempt-assessment.v1", "task": TASK,
            "project": PROJECT, "attempt_epoch": attempt["attempt_epoch"],
            "attempt_ordinal": attempt["attempt_ordinal"], "task_revision": 2,
            "task_binding": attempt["task_binding"], "proposal": proposal["id"],
            "proposal_digest": proposal["digest"], "implementer_run": "RUN-impl",
            "implementer_receipt": "REC-impl", "reviewer_run": "RUN-review",
            "reviewer_receipt": "REC-review", "judgment": judgment,
            "rationale": "Observed retained evidence", "evidence": [{"ref": "REC-impl"}],
        }
        rows["attempt_assessments"] = [{
            "id": assessment_id, "task": TASK, "project": PROJECT,
            "attempt_epoch": attempt["attempt_epoch"], "attempt_ordinal": attempt["attempt_ordinal"],
            "task_revision": 2, "task_binding": attempt["task_binding"],
            "proposal": proposal["id"], "proposal_digest": proposal["digest"],
            "implementer_run": "RUN-impl", "implementer_receipt": "REC-impl",
            "reviewer_run": "RUN-review", "reviewer_receipt": "REC-review",
            "judgment": judgment, "rationale": assessment_body["rationale"],
            "evidence": assessment_body["evidence"], "body": assessment_body,
            "digest": digest(assessment_body), "created": 5.0,
        }]
        if judgment == "no_progress":
            rows["tasks"][0]["no_progress_count"] = 1

    auth_id = None
    if control_type in {"timeout", "recovery"}:
        auth_id = "ECAUTH-1"
        observed = bool(attempt["implementer_run"] and attempt["implementer_receipt"])
        auth_assessment = judgment if observed and judgment in {"progress", "no_progress"} else None
        auth_body = {
            "format": "daikibo.execution-control-authorization.v1", "proposal": proposal["id"],
            "proposal_digest": proposal["digest"], "task": TASK, "task_revision": 2,
            "control_revision": 1, "requested_seconds": 90000 if control_type == "timeout" else None,
            "effective_seconds": 90000 if control_type == "timeout" else None,
            "assessment": auth_assessment, "control_type": control_type,
            "reviewer_run": "RUN-review", "reviewer_receipt": "REC-review",
            "assessment_id": assessment_id,
        }
        rows["execution_control_authorizations"] = [{
            "id": auth_id, "proposal": proposal["id"], "project": PROJECT, "task": TASK,
            "task_revision": 2, "control_revision": 1,
            "proposal_digest": proposal["digest"],
            "requested_seconds": auth_body["requested_seconds"],
            "effective_seconds": auth_body["effective_seconds"], "assessment": auth_assessment,
            "reviewer_run": "RUN-review", "reviewer_receipt": "REC-review",
            "body": auth_body, "digest": digest(auth_body), "created": 6.0,
        }]

    event_body = {
        "format": "daikibo.execution-control-event.v1", "proposal": proposal["id"],
        "proposal_digest": proposal["digest"], "kind": "applied",
        "result": {"authorization": auth_id, "assessment": assessment_id},
        "reviewer_receipt": "REC-review",
    }
    rows["execution_control_events"] = [{
        "id": "ECEVT-1", "proposal": proposal["id"], "project": PROJECT,
        "kind": "applied", "body": event_body, "digest": digest(event_body), "created": 7.0,
    }]


def _validate(rows):
    def get(section, key):
        values = [row for row in rows[section] if row.get("id") == key]
        assert len(values) == 1
        return values[0]

    def each(section, ref=None):
        values = rows[section]
        if ref is not None:
            field = "proposal" if section in {"execution_control_packets", "execution_control_events"} else "task"
            values = [row for row in values if row.get(field) == ref]
        return iter(values)

    return validate_execution_controls(get, each, PROJECT)


def test_valid_history_preserves_unknown_legacy_attempts_and_no_live_authority():
    rows = _row_history(attempts=3)
    _add_applied_control(rows)
    result = _validate(rows)
    assert result["legacy_unknown_attempts"] == 2
    assert result["fresh_live_authorization"] is False


@pytest.mark.parametrize("mutation", ["cross_project", "wrong_epoch", "cached_count", "claim_only"])
def test_history_rejects_dangling_or_unobserved_control_records(mutation):
    rows = _row_history(attempts=1)
    _add_applied_control(rows)
    if mutation == "cross_project":
        rows["attempt_assessments"][0]["project"] = "PRJ-other"
        rows["attempt_assessments"][0]["body"]["project"] = "PRJ-other"
        rows["attempt_assessments"][0]["digest"] = digest(rows["attempt_assessments"][0]["body"])
    elif mutation == "wrong_epoch":
        rows["attempt_assessments"][0]["attempt_epoch"] = 99
        rows["attempt_assessments"][0]["body"]["attempt_epoch"] = 99
        rows["attempt_assessments"][0]["digest"] = digest(rows["attempt_assessments"][0]["body"])
    elif mutation == "cached_count":
        rows["tasks"][0]["no_progress_count"] = 1
    else:
        rows["execution_attempts"][0]["implementer_run"] = None
        rows["execution_attempts"][0]["implementer_receipt"] = None
    with pytest.raises(Fault):
        _validate(rows)


def test_inconclusive_receipt_has_no_finalized_assessment_or_counter():
    # An inconclusive review is retained in ordinary receipt/proposal history;
    # it is deliberately absent from attempt_assessments and consumes no slot.
    rows = _row_history()
    result = _validate(rows)
    assert result["attempt_assessments"] == 0
    assert result["tasks"] == 1


def test_assessment_only_applies_without_authorization():
    rows = _row_history()
    _add_applied_control(rows, control_type="assessment")
    result = _validate(rows)
    assert result["attempt_assessments"] == 1
    assert result["execution_control_authorizations"] == 0


def test_claim_only_recovery_keeps_nullable_duration_and_assessment():
    rows = _row_history(implementer=False)
    _add_applied_control(rows, control_type="recovery", judgment=None)
    result = _validate(rows)
    assert result["execution_control_authorizations"] == 1


def test_reserved_attempt_state_round_trips_with_nullable_run_and_receipt():
    rows = _row_history(implementer=False)
    rows["execution_attempts"][0]["status"] = "reserved"
    result = _validate(rows)
    assert result["execution_attempts"] == 1
    assert result["fresh_live_authorization"] is False


def test_applied_proposal_without_event_or_authorization_is_rejected():
    rows = _row_history()
    _add_applied_control(rows)
    rows["execution_control_events"].clear()
    with pytest.raises(Fault):
        _validate(rows)


@pytest.mark.parametrize("source_refs", [None, {}, [""], ["SRC-1", 4], ["SRC-1", "SRC-1"]])
def test_requirement_source_refs_are_canonical_string_ids(source_refs):
    body = {"title": "Requirement", "statement": "Retained source", "acceptance": ["AC-1"]}
    body["source_refs"] = source_refs
    with pytest.raises(Fault):
        Knowledge.validate_body("requirement", body)
    contract = artifact_body_contract()["kinds"]["requirement"]
    assert contract["fields"]["source_refs"]["type"] == "array"


def test_requirement_source_refs_accept_valid_ids():
    Knowledge.validate_body("requirement", {
        "title": "Requirement", "statement": "Retained source", "acceptance": ["AC-1"],
        "source_refs": ["SRC-1", "SRC-2"],
    })
