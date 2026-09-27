"""Real Runtime checks for the private Unit 3 pre-adoption boundary."""
from __future__ import annotations

import copy
import json
import time

import pytest

from conftest import make_task
from daikibo import candidate_provenance
from daikibo.common import Fault, canonical, digest
import daikibo.runtime as runtime_module


class _RuntimeBoundaryClock:
    """Test-only Runtime clock with an explicit lease-expiry boundary."""

    def __init__(self):
        self._now = time.time()

    def __call__(self):
        return self._now

    def advance_past(self, lease_until):
        assert lease_until > self._now
        self._now = lease_until + 1.0


def _db_projection(control):
    names = [row["name"] for row in control.s.all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    result = {}
    for name in names:
        rows = control.s.all(f'SELECT * FROM "{name}" ORDER BY rowid')
        result[name] = digest(canonical(rows))
    return result


def test_runtime_preadoption_is_readonly_and_precedes_candidate_update(full, full_project, monkeypatch):
    project, _repository, _requirement, _root = full_project
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    original = candidate_provenance.resolve_preadoption_observation
    captured = {}

    def observe_boundary(control, actor, observation):
        before = _db_projection(control)
        assert control.s.one("SELECT count(*) AS n FROM candidates")["n"] == 0
        assert control.s.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "running"
        identity = original(control, actor, observation)
        after = _db_projection(control)
        assert after == before
        captured["observation"] = observation
        captured["identity"] = dict(identity)
        return identity

    monkeypatch.setattr(candidate_provenance, "resolve_preadoption_observation", observe_boundary)
    result = full.rt.execute(full.owner, task, "fixture")
    assert result["status"] == "submitted"
    assert "candidate" not in captured["identity"]
    assert {"run", "receipt", "output_snapshot_digest", "changes_digest",
            "producer_actor", "cas_dependencies"} <= set(captured["identity"])
    assert full.s.one("SELECT count(*) AS n FROM candidates")["n"] == 1


def test_preadoption_origin_and_seal_reject_caller_copies(full, full_project, monkeypatch):
    project, _repository, _requirement, _root = full_project
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    original = candidate_provenance.resolve_preadoption_observation

    def reject_copies(control, actor, observation):
        tampered = copy.deepcopy(observation)
        tampered._payload["after"] = copy.deepcopy(tampered._payload["snapshot"])
        with pytest.raises(Fault):
            original(control, actor, tampered)
        wire = json.loads(canonical(observation._payload))
        with pytest.raises(Fault):
            original(control, actor, wire)
        with pytest.raises(Fault):
            original(object(), actor, observation)
        return original(control, actor, observation)

    monkeypatch.setattr(candidate_provenance, "resolve_preadoption_observation", reject_copies)
    assert full.rt.execute(full.owner, task, "fixture")["status"] == "submitted"


def test_final_conditional_update_fences_expiry_after_readonly_adapter(full, full_project, monkeypatch):
    project, _repository, _requirement, _root = full_project
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    original = candidate_provenance.resolve_preadoption_observation
    captured = {}

    def expire_after_validation(control, actor, observation):
        identity = original(control, actor, observation)
        captured["run"] = identity["run"]
        control.s.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
        return identity

    monkeypatch.setattr(candidate_provenance, "resolve_preadoption_observation", expire_after_validation)
    with pytest.raises(Fault) as rejected:
        full.rt.execute(full.owner, task, "fixture")
    assert rejected.value.code == "stale_run"
    assert full.s.one("SELECT candidate FROM tasks WHERE id=?", (task,))["candidate"] is None
    assert full.s.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "running"
    assert full.s.one("SELECT id FROM candidates WHERE task=?", (task,)) is None
    receipt = full.s.one("SELECT body FROM receipts WHERE run=?", (captured["run"],), True)
    body = json.loads(receipt["body"])
    assert body["work_product"] is not None


def test_elapsed_lease_after_readonly_adapter_is_fenced(full, full_project, monkeypatch):
    project, _repository, _requirement, _root = full_project
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    original = candidate_provenance.resolve_preadoption_observation
    captured = {}
    clock = _RuntimeBoundaryClock()
    monkeypatch.setattr(runtime_module, "timestamp", clock)
    # The real provenance adapter checks the same lease before returning.  It
    # must observe the live side of this fixture boundary; the final Runtime
    # fence is the only clock transition in this test.
    monkeypatch.setattr(candidate_provenance, "timestamp", clock)

    def expire_during_adapter(control, actor, observation):
        # The adapter enters while the lease is live.  The fixture advances the
        # Runtime clock only after the real read-only resolver returns, so the
        # final mutation fence observes a deterministically expired lease.
        lease_until = clock() + 0.12
        control.s.execute(
            "UPDATE tasks SET lease_until=? WHERE id=?",
            (lease_until, task),
        )
        assert lease_until > clock()
        identity = original(control, actor, observation)
        captured["run"] = identity["run"]
        clock.advance_past(lease_until)
        captured["lease_until"] = lease_until
        captured["expired_at"] = clock()
        assert lease_until < clock()
        return identity

    monkeypatch.setattr(
        candidate_provenance, "resolve_preadoption_observation", expire_during_adapter)
    with pytest.raises(Fault) as rejected:
        full.rt.execute(full.owner, task, "fixture")
    assert rejected.value.code == "stale_run"
    assert full.s.one("SELECT candidate FROM tasks WHERE id=?", (task,))["candidate"] is None
    assert full.s.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "running"
    assert full.s.one("SELECT id FROM candidates WHERE task=?", (task,)) is None
    receipt = full.s.one("SELECT body FROM receipts WHERE run=?", (captured["run"],), True)
    assert receipt is not None
    assert json.loads(receipt["body"])["work_product"] is not None
