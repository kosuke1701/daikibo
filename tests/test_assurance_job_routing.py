"""Public durable-job routing for immutable assurance review packets.

The adapter used here is the repository's deterministic protocol fixture.  It
checks the job boundary and retained receipt shape; it is not a semantic LLM
reviewer.
"""

import sys
from pathlib import Path

import pytest

from daikibo.common import Actor, Fault
from test_e3_selection_contract import (
    _fixture,
    _profile_body,
    _register_fixture_review,
)


def _packet_job_fixture(full):
    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposal = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None
    )
    packet = full.assurance.review_subject(
        full.owner, project, proposal["profile"]["id"]
    )["packets"][0]
    role = packet["body"]["required_roles"][0]
    return project, packet, role


def _run_queued_job(full, job):
    return full.jobs.run_one(full.s.one("SELECT * FROM jobs WHERE id=?", (job["id"],), True))


def test_public_assurance_packet_job_runs_and_retains_fixture_receipt(full):
    project, packet, role = _packet_job_fixture(full)
    args = {
        "subject": packet["id"],
        "role": role,
        "adapter": "e3-assurance-fixture",
    }
    assert full.jobs.subject_project("review", args) == project

    job = full.invoke(full.owner, "job.submit", {"kind": "review", "args": args})
    outcome = _run_queued_job(full, job)

    assert outcome["status"] == "succeeded", outcome
    state = full.jobs.get(full.owner, job["id"])
    assert state["status"] == "succeeded"
    receipt_id = outcome["result"]["receipt"]
    receipt = full.g.receipt(receipt_id)
    assert receipt["subject"] == packet["id"]
    assert receipt["role"] == role
    assert receipt["result"]["verdict"] == "pass"
    assert receipt["result"]["covered"] == packet["body"]["required_coverage"]


@pytest.mark.parametrize("subject_factory", [
    lambda project, packet, full: full.s.one(
        "SELECT id FROM assurance_objects WHERE project=? AND kind='profile' LIMIT 1",
        (project,),
    )["id"],
    lambda project, packet, full: "AOBJ-unknown-assurance-subject",
])
def test_review_job_rejects_nonpacket_or_unknown_assurance_subject(full, subject_factory):
    project, packet, role = _packet_job_fixture(full)
    subject = subject_factory(project, packet, full)
    args = {"subject": subject, "role": role, "adapter": "e3-assurance-fixture"}

    with pytest.raises(Fault) as rejected:
        full.invoke(full.owner, "job.submit", {"kind": "review", "args": args})
    assert rejected.value.code == "not_found"


def test_review_job_rejects_actor_scoped_to_another_project(full):
    project, packet, role = _packet_job_fixture(full)
    other = full.k.create_project(full.owner, "Foreign review actor")['id']
    actor = Actor("foreign-agent", "agent", other)
    args = {
        "subject": packet["id"],
        "role": role,
        "adapter": "e3-assurance-fixture",
    }

    with pytest.raises(Fault) as rejected:
        full.invoke(actor, "job.submit", {"kind": "review", "args": args})
    assert rejected.value.code == "forbidden"


def test_review_job_keeps_existing_stale_material_fence(full, full_project):
    """A queued ordinary review still becomes stale after its input revision."""
    project, _repo, _requirement, _root = full_project
    draft = full.k.propose(
        full.owner,
        project,
        "design",
        {"title": "Pending design", "statement": "Initial design material"},
    )
    full.rt.adapters.register(
        full.owner,
        "routing-stale-fixture",
        "fixture",
        sys.executable,
        [str(Path(__file__).with_name("assurance_reviewer_fixture.py"))],
    )
    job = full.jobs.submit(
        full.owner,
        "review",
        {
            "subject": draft["id"],
            "role": "requirements",
            "adapter": "routing-stale-fixture",
        },
    )
    artifact = full.k.artifact(full.owner, draft["id"])
    changed = {**artifact["body"], "statement": "The revised material is different."}
    full.k.revise(
        full.owner,
        draft["id"],
        artifact["revision"],
        changed,
        "Queue stale-material regression",
    )

    outcome = _run_queued_job(full, job)
    assert outcome["status"] == "failed"
    assert outcome["error"]["code"] == "retry_stale"
    assert full.jobs.get(full.owner, job["id"])["status"] == "failed"
