"""Focused lease-keeper lifecycle tests using local execution fixtures only."""

from __future__ import annotations

from contextlib import contextmanager
import sys
import threading
import time
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest, parse_json
import daikibo.runtime as runtime_module
from daikibo.runtime import _LeaseKeeper
from conftest import make_task


def _set_lease_seconds(control, project, seconds):
    row = control.s.one("SELECT body FROM policies WHERE project=?", (project,), True)
    body = parse_json(row["body"])
    body["lease_seconds"] = seconds
    control.s.execute(
        "UPDATE policies SET body=?,digest=? WHERE project=?",
        (canonical(body).decode(), digest(body), project),
    )


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _register_slow_adapter(control, tmp_path, name="slow-implementer", delay=2.0):
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "payload=json.load(sys.stdin)\n"
        f"time.sleep({delay!r})\n"
        "goal=payload['task']['goal']\n"
        "if goal.startswith('WRITE:'):\n"
        "    for name,content in json.loads(goal[6:]).items():\n"
        "        path=Path(name); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(content)\n"
        "print(json.dumps({'message':'slow fixture implementation ran'}))\n"
    )
    control.rt.adapters.register(control.owner, name, "fixture", sys.executable, [str(script)])
    return name


def test_near_expiry_start_renews_synchronously_and_expiry_is_not_revived(full, full_project, monkeypatch):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    _set_lease_seconds(control, project, 2.0)
    claimed = control.w.claim(control.owner, project, task)

    monkeypatch.setattr(runtime_module, "timestamp", lambda: 100.0)
    near_expiry = 100.01
    control.s.execute("UPDATE tasks SET lease_until=? WHERE id=?", (near_expiry, task))
    context = control.rt._keep_task_lease(task, claimed["epoch"], control.owner.id)
    with context:
        renewed = control.s.one("SELECT lease_until FROM tasks WHERE id=?", (task,), True)["lease_until"]
        assert renewed > near_expiry + 1.0

    # A genuinely expired lease is fenced; start() cannot revive it.
    expired = 99.0
    control.s.execute("UPDATE tasks SET lease_until=? WHERE id=?", (expired, task))
    failed = _LeaseKeeper(control.rt, task, claimed["epoch"], control.owner.id, None)
    with pytest.raises(Fault) as error:
        failed.start()
    assert error.value.code == "lease_lost"
    assert control.s.one("SELECT lease_until FROM tasks WHERE id=?", (task,), True)["lease_until"] == expired


def test_renew_reads_clock_after_store_lock_and_never_revives_expiry(full, full_project, monkeypatch):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    claimed = control.w.claim(control.owner, project, task)
    control.s.execute("UPDATE tasks SET lease_until=101 WHERE id=?", (task,))

    clock = [100.0]
    lock_entered = threading.Event()
    observations = []
    renewal_thread = [None]
    original_transaction = control.s.transaction
    original_timestamp = runtime_module.timestamp

    @contextmanager
    def transaction_probe():
        with original_transaction():
            if threading.current_thread() is renewal_thread[0]:
                # This yield point is after Store acquired its writer lock and
                # issued BEGIN IMMEDIATE.  The fixed renewer reads the clock
                # only after this point.
                lock_entered.set()
                clock[0] = 102.0
            yield

    def controlled_timestamp():
        if threading.current_thread() is renewal_thread[0]:
            observations.append(lock_entered.is_set())
            return clock[0]
        return original_timestamp()

    monkeypatch.setattr(control.s, "transaction", transaction_probe)
    monkeypatch.setattr(runtime_module, "timestamp", controlled_timestamp)
    keeper = _LeaseKeeper(control.rt, task, claimed["epoch"], control.owner.id, None)
    result = []

    def renew():
        renewal_thread[0] = threading.current_thread()
        try:
            keeper._renew()
        except Fault as error:
            result.append(error)

    thread = threading.Thread(target=renew)
    renewal_thread[0] = thread
    thread.start()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert result and result[0].code == "lease_lost"
    assert observations == [True]
    assert control.s.one("SELECT lease_until FROM tasks WHERE id=?", (task,), True)["lease_until"] == 101


def test_leasekeeper_performs_multiple_short_interval_renewals(full, full_project):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    _set_lease_seconds(control, project, 0.5)
    claimed = control.w.claim(control.owner, project, task)
    context = control.rt._keep_task_lease(task, claimed["epoch"], control.owner.id)
    renewals = []
    original = context.keeper._renew

    def tracked_renewal():
        renewals.append(time.monotonic())
        return original()

    context.keeper._renew = tracked_renewal
    with context as keeper:
        assert 0 < keeper.interval <= 0.5 / 3.0
        # Keep this regression independent from host scheduling while still
        # exercising the background thread's repeated renew path.
        keeper.interval = 0.005
        assert _wait_until(lambda: len(renewals) >= 3)
        keeper.check()
    assert len(renewals) >= 3


def test_candidate_seal_stops_keeper_before_sweep_visible_transition(full, full_project, monkeypatch):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    states = []
    checks = []

    class ProbeKeeper:
        failure = None

        def check(self):
            checks.append(control.s.one("SELECT status FROM tasks WHERE id=?", (task,), True)["status"])

        def stop(self):
            state = control.s.one("SELECT status FROM tasks WHERE id=?", (task,), True)["status"]
            states.append(state)
            # A sweep concurrent with the handoff sees the still-running claim
            # before the seal transaction, and therefore must not fence it.
            control.w.reconcile(control.owner, project)

    probe = ProbeKeeper()

    class ProbeContext:
        keeper = probe

        def __enter__(self):
            return self.keeper

        def __exit__(self, exc_type, exc, tb):
            self.keeper.stop()
            return False

    def replacement(task_id, epoch, owner, job_id=None):
        assert task_id == task and owner == control.owner.id
        return ProbeContext()

    monkeypatch.setattr(control.rt, "_keep_task_lease", replacement)
    control.w.claim(control.owner, project, task)
    result = control.rt.execute(control.owner, task, "fixture")

    assert result["status"] == "submitted"
    assert states[0] == "running"
    assert states[-1] == "submitted"
    assert all(state == "running" for state in checks)
    assert control.w.task(control.owner, task)["status"] == "submitted"


def test_keeper_renews_during_delayed_snapshot_collection(full, full_project, monkeypatch):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    _set_lease_seconds(control, project, 2.0)
    control.w.claim(control.owner, project, task)
    original_keep_task_lease = control.rt._keep_task_lease

    def fast_keeper(task_id, epoch, owner, job_id=None):
        context = original_keep_task_lease(task_id, epoch, owner, job_id)
        original_start = context.keeper.start

        def start_with_test_interval():
            original_start()
            context.keeper.interval = 0.01

        context.keeper.start = start_with_test_interval
        return context

    monkeypatch.setattr(control.rt, "_keep_task_lease", fast_keeper)
    original = control.sn.collect
    collection_started = threading.Event()

    def delayed_collection(snapshot, directory, ignored=()):
        collection_started.set()
        time.sleep(0.35)
        return original(snapshot, directory, ignored=ignored)

    monkeypatch.setattr(control.sn, "collect", delayed_collection)
    result = control.rt.execute(control.owner, task, "fixture")

    assert collection_started.is_set()
    assert result["status"] == "submitted"
    assert control.w.task(control.owner, task)["lease_until"] is None


def test_job_cancellation_fences_managed_execute_and_stops_renewal(full, full_project, tmp_path):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    _set_lease_seconds(control, project, 0.8)
    adapter = _register_slow_adapter(control, tmp_path)
    control.w.claim(control.owner, project, task)
    job = control.jobs.submit(control.owner, "execute", {"task": task, "adapter": adapter})
    row = control.s.one("SELECT * FROM jobs WHERE id=?", (job["id"],), True)
    outcome = []
    runner = threading.Thread(target=lambda: outcome.append(control.jobs.run_one(row)), daemon=True)
    runner.start()

    assert _wait_until(
        lambda: control.s.one(
            "SELECT id FROM runs WHERE task=? AND role='implementer' AND status IN ('registered','running')",
            (task,),
        )
        is not None
    )
    control.jobs.cancel(control.owner, job["id"], "Stop the managed run")
    runner.join(timeout=5.0)

    assert not runner.is_alive()
    assert outcome and outcome[0]["status"] == "cancelled"
    current = control.w.task(control.owner, task)
    assert current["candidate"] is None
    assert current["lease_until"] is None


def test_candidate_seal_rechecks_lease_after_findings_scan(full, full_project, monkeypatch):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    expired = threading.Event()
    original_stub_findings = runtime_module.stub_findings

    def expire_before_adoption(path, data):
        if not expired.is_set():
            control.s.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task,))
            expired.set()
        return original_stub_findings(path, data)

    monkeypatch.setattr(runtime_module, "stub_findings", expire_before_adoption)
    control.w.claim(control.owner, project, task)
    with pytest.raises(Fault) as error:
        control.rt.execute(control.owner, task, "fixture")

    assert expired.is_set()
    assert error.value.code == "stale_run"
    current = control.w.task(control.owner, task)
    assert current["candidate"] is None
    assert current["status"] == "running"


def test_successful_seal_does_not_check_quiesced_keeper_after_lease_clear(full, full_project, monkeypatch):
    control = full
    project = full_project[0]
    task = make_task(control, full_project)
    checks = []

    class SealKeeper:
        failure = None

        def check(self):
            state = control.s.one("SELECT status FROM tasks WHERE id=?", (task,), True)["status"]
            checks.append(state)
            if state == "submitted":
                raise Fault("lease_lost", "A post-seal keeper check would be invalid")

        def stop(self):
            return None

    keeper = SealKeeper()

    class SealContext:
        def __enter__(self):
            return keeper

        def __exit__(self, exc_type, exc, tb):
            keeper.stop()
            return False

    monkeypatch.setattr(
        control.rt,
        "_keep_task_lease",
        lambda task_id, epoch, owner, job_id=None: SealContext(),
    )
    control.w.claim(control.owner, project, task)
    result = control.rt.execute(control.owner, task, "fixture")

    assert result["status"] == "submitted"
    assert checks and all(state == "running" for state in checks)
