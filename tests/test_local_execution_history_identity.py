"""Rehashed local-history identity regressions.

These checks exercise the portable validator directly.  The mutations retain
their enclosing digests, so a checksum-only history reader cannot hide the
contradiction.
"""
from __future__ import annotations

import copy
import json

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.local_execution_history import validate_local_executions
from daikibo.local_executions import FORMAT, PACKET_FORMAT, ROLES, STAGES


PROJECT = "PRJ"
PROGRAM = "PROGRAM"
SUBPLAN = "SUBPLAN"
PROPOSAL = "LEX"
PACKET = "LEXPACK"
TASK = "TASK"


def _history():
    stage_evidence = {TASK: {stage: {} for stage in STAGES}}
    value = {
        "id": TASK,
        "revision": 1,
        "body": {"task_kind": "production"},
        "reads": [],
        "dependencies": [],
        "test_plan": None,
    }
    task = {
        "id": "task:" + TASK,
        "kind": "task",
        "task": TASK,
        "digest": digest(value),
        "project": PROJECT,
        "value": value,
    }
    material = {
        "format": FORMAT,
        "project": PROJECT,
        "program": PROGRAM,
        "subplan": SUBPLAN,
        "program_source": {},
        "sources": [],
        "inventory": [],
        "tasks": [task],
        "external_tasks": [],
        "task_inventory": [],
        "task_obligations": {TASK: []},
        "subplans": [],
        "stage_evidence": stage_evidence,
        "dispositions": [],
        "obligations": [],
        "policy": "policy-digest",
        "instructions": "historical fixture",
    }
    material_digest = digest(material)
    coverage = {
        "stages": [
            "LEXSTAGE-" + digest([TASK, stage, digest(stage_evidence[TASK][stage])])
            for stage in STAGES
        ],
        "obligations": [],
        "impact": [],
    }
    serialized = canonical(material).decode()
    packet_body = {
        "format": PACKET_FORMAT,
        "id": PACKET,
        "proposal": PROPOSAL,
        "program": PROGRAM,
        "subplan": SUBPLAN,
        "ordinal": 0,
        "material_digest": material_digest,
        "start": 0,
        "end": len(serialized),
        "total_characters": len(serialized),
        "serialized_fragment": serialized,
        "required_coverage": [
            "LEXPART-" + digest([PROPOSAL, material_digest, 0, len(serialized)]),
            *coverage["stages"],
        ],
        "coverage_manifest": coverage,
        "complete_manifest": {"packet_count": 1, "markers": coverage},
    }
    packet_digest = digest(packet_body)
    stable = {
        "format": FORMAT,
        "program": PROGRAM,
        "subplan": SUBPLAN,
        "project": PROJECT,
        "tasks": [TASK],
        "rationale": "fixture",
        "stage_evidence": stage_evidence,
        "dispositions": [],
        "material_digest": material_digest,
        "coverage_manifest": coverage,
        "task_obligations": {TASK: []},
    }
    proposal_body = {
        **stable,
        "packet_manifest": [{"id": PACKET, "digest": packet_digest}],
        "packet_count": 1,
        "material_characters": len(serialized),
        "created_by": "owner",
        "payload_digest": digest(stable),
    }
    reviews = [
        {"packet": PACKET, "role": role, "receipt": "RECEIPT-" + role, "run": "RUN-" + role,
         "binding": packet_digest}
        for role in ROLES
    ]
    certification_body = {
        "format": "daikibo.local-execution-certification.v1",
        "proposal": PROPOSAL,
        "kind": "certified",
        "task": None,
        "epoch": None,
        "proposal_digest": digest(proposal_body),
        "material_digest": material_digest,
        "reviews": reviews,
        "review_digest": digest(reviews),
        "request_id": None,
        "packet_manifest": [{"id": PACKET, "digest": packet_digest}],
        "tasks": [TASK],
        "current": True,
        "deploy_ready": False,
    }
    return {
        "programs": [{"id": PROGRAM, "project": PROJECT}],
        "subplans": [{"id": SUBPLAN, "project": PROJECT, "program": PROGRAM}],
        "local_execution_proposals": [{
            "id": PROPOSAL, "project": PROJECT, "program": PROGRAM, "subplan": SUBPLAN,
            "digest": digest(proposal_body), "body": proposal_body, "created": 1.0,
        }],
        "local_execution_packets": [{
            "id": PACKET, "project": PROJECT, "proposal": PROPOSAL, "ordinal": 0,
            "digest": packet_digest, "body": packet_body,
        }],
        "local_execution_records": [{
            "id": "CERT", "project": PROJECT, "proposal": PROPOSAL, "task": None,
            "epoch": None, "kind": "certified", "digest": digest(certification_body),
            "body": certification_body, "created": 2.0,
        }],
    }


def _validate(tables):
    def get(section, ident):
        matches = [row for row in tables[section] if row.get("id") == ident]
        assert len(matches) == 1
        return matches[0]

    def each(section):
        return iter(tables[section])

    return validate_local_executions(get, each, PROJECT)


def _refresh_record(record):
    record["digest"] = digest(record["body"])


def _rebind_material(tables, mutate):
    """Apply a material mutation and recompute all enclosing references."""
    proposal = tables["local_execution_proposals"][0]
    packet = tables["local_execution_packets"][0]
    record = tables["local_execution_records"][0]
    material = copy.deepcopy(json.loads(packet["body"]["serialized_fragment"]))
    mutate(material)
    for item in material.get("tasks", []) + material.get("external_tasks", []):
        item["digest"] = digest(item["value"])
    material_digest = digest(material)
    serialized = canonical(material).decode()
    packet_body = packet["body"]
    packet_body.update({
        "material_digest": material_digest,
        "end": len(serialized),
        "total_characters": len(serialized),
        "serialized_fragment": serialized,
    })
    packet_body["required_coverage"][0] = "LEXPART-" + digest([PROPOSAL, material_digest, 0, len(serialized)])
    packet["digest"] = digest(packet_body)
    proposal_body = proposal["body"]
    proposal_body.update({
        "material_digest": material_digest,
        "material_characters": len(serialized),
        "packet_manifest": [{"id": PACKET, "digest": packet["digest"]}],
    })
    stable = {key: proposal_body[key] for key in (
        "format", "program", "subplan", "project", "tasks", "rationale", "stage_evidence",
        "dispositions", "material_digest", "coverage_manifest", "task_obligations",
    )}
    proposal_body["payload_digest"] = digest(stable)
    proposal["digest"] = digest(proposal_body)
    certification = record["body"]
    certification["proposal_digest"] = proposal["digest"]
    certification["material_digest"] = material_digest
    certification["packet_manifest"] = proposal_body["packet_manifest"]
    for review in certification["reviews"]:
        review["binding"] = packet["digest"]
    certification["review_digest"] = digest(certification["reviews"])
    _refresh_record(record)


def test_identity_fixture_is_valid_before_tampering():
    assert _validate(_history()) == {
        "local_execution_proposals": 1,
        "local_execution_packets": 1,
        "local_execution_records": 1,
    }


def test_rehashed_task_projection_id_mismatch_is_rejected():
    tables = _history()
    _rebind_material(tables, lambda material: material["tasks"][0]["value"].update({"id": "OTHER"}))

    with pytest.raises(Fault):
        _validate(tables)


def test_rehashed_certification_task_list_must_match_proposal():
    tables = _history()
    certification = tables["local_execution_records"][0]
    certification["body"]["tasks"] = ["OTHER"]
    _refresh_record(certification)

    with pytest.raises(Fault):
        _validate(tables)


def test_rehashed_certification_task_epoch_must_match_null_row():
    tables = _history()
    certification = tables["local_execution_records"][0]
    certification["body"]["task"] = TASK
    _refresh_record(certification)

    with pytest.raises(Fault):
        _validate(tables)


def test_rehashed_review_reference_requires_nonempty_identity():
    tables = _history()
    certification = tables["local_execution_records"][0]
    certification["body"]["reviews"][0]["receipt"] = ""
    certification["body"]["review_digest"] = digest(certification["body"]["reviews"])
    _refresh_record(certification)

    with pytest.raises(Fault):
        _validate(tables)
