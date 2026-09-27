"""Finite regressions for the shared readonly local-authorization primitive."""

from __future__ import annotations

import json

import pytest

from daikibo import candidate_provenance
from daikibo.common import canonical, digest
from test_local_execution import _claim_local, local_case
from test_reviewed_breakdowns import setup


def _db_projection(control):
    names = [row["name"] for row in control.s.all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {
        name: digest(canonical(control.s.all(f'SELECT * FROM "{name}" ORDER BY rowid')))
        for name in names
    }


def test_readonly_local_primitive_preserves_current_authorization_and_db(local_case, monkeypatch):
    case = local_case
    control = case["c"]
    _, running, claim = _claim_local(case)
    claim_body = json.loads(control.s.one(
        "SELECT body FROM local_execution_records WHERE id=?", (claim["id"],), True)["body"])

    policy = control.g.policy

    def readonly_policy(project, create=True):
        assert create is False, "readonly local authorization attempted policy creation"
        return policy(project, create=False)

    monkeypatch.setattr(control.g, "policy", readonly_policy)
    before = _db_projection(control)
    changes_before = control.s.conn.total_changes
    authorization = control.g.execution_readiness_readonly(
        control.owner, case["task"], "candidate", claim_body["certified_event"])
    after = _db_projection(control)

    assert authorization["allowed"] is True
    assert authorization["route"] == "local"
    assert authorization["proposal"] == case["proposal"]["id"]
    assert authorization["certification"]["id"] == claim_body["certified_event"]["id"]
    assert after == before
    assert control.s.conn.total_changes == changes_before
    assert control.s.one("SELECT status,epoch FROM tasks WHERE id=?",
                         (case["task"],)) == {"status": "running", "epoch": running["epoch"]}


def test_workflow_claim_local_ready_fallback_does_not_write_a_ready_gate(local_case):
    """Claim's local route rereads ready state without a second gate/event."""
    case = local_case
    control = case["c"]
    from test_local_execution import _review_local_packets

    _review_local_packets(case)
    control.local_executions.certify(
        control.owner, case["proposal"]["id"], case["proposal"]["digest"],
    )
    control.w.ready(control.owner, case["task"])
    before_gates = control.s.one(
        "SELECT count(*) AS n FROM gate_results WHERE subject=?",
        (case["task"],),
    )["n"]
    running = control.w.claim(control.owner, case["project"], task=case["task"])
    after_gates = control.s.one(
        "SELECT count(*) AS n FROM gate_results WHERE subject=?",
        (case["task"],),
    )["n"]
    assert running["status"] == "running"
    assert after_gates == before_gates


def test_candidate_provenance_local_route_does_not_reenter_readiness_wrapper(
    local_case, monkeypatch
):
    case = local_case
    control = case["c"]
    _claim_local(case)
    original_resolver = candidate_provenance.resolve_preadoption_observation

    def observe_boundary(controller, actor, observation):
        before = _db_projection(controller)

        def forbidden(*args, **kwargs):
            pytest.fail("candidate provenance re-entered stage readiness wrapper")

        monkeypatch.setattr(controller.g, "execution_readiness", forbidden)
        monkeypatch.setattr(controller.g.local_executions, "current_authorization", forbidden)
        identity = original_resolver(controller, actor, observation)
        assert _db_projection(controller) == before
        return identity

    monkeypatch.setattr(
        candidate_provenance,
        "resolve_preadoption_observation",
        observe_boundary,
    )
    result = control.rt.execute(control.owner, case["task"], "fixture")
    assert result["status"] == "submitted"
    assert control.s.one("SELECT candidate FROM tasks WHERE id=?",
                         (case["task"],))["candidate"] is not None
