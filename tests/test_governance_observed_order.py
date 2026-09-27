"""Finite regressions for the shared Governance receipt selector."""

from __future__ import annotations

import pytest

from daikibo.common import Fault
from daikibo.governance import Governance
from test_e3_unit2b_node_reviews import _failing_adapter, _review_adapter


def _binding(control, receipt):
    return control.s.one("SELECT binding FROM receipts WHERE id=?", (receipt,), True)["binding"]


def test_evidence_for_returns_durable_latest_first_without_control(full, full_project, tmp_path, monkeypatch):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "governance-order")
    import daikibo.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "timestamp", lambda: 1234567890.0)
    first = control.rt.review(control.owner, requirement, "requirements", adapter)
    second = control.rt.review(control.owner, requirement, "requirements", adapter)
    binding = _binding(control, first["receipt"])

    refs = control.g.evidence_for(requirement, binding, "requirements")
    assert [item["id"] for item in refs] == [second["receipt"], first["receipt"]]

    # The public helper remains usable with the standalone s/sec/k composition;
    # it does not acquire a Control or infer a project from an arbitrary row.
    standalone = Governance(control.s, control.sec, control.k, mode="validation")
    standalone_refs = standalone.evidence_for(requirement, binding, "requirements")
    assert [item["id"] for item in standalone_refs] == [second["receipt"], first["receipt"]]


def test_evidence_for_keeps_latest_failure_ahead_of_old_pass(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    passing = _review_adapter(control, tmp_path, "governance-pass")
    failing = _failing_adapter(control, tmp_path, "governance-fail")
    first = control.rt.review(control.owner, requirement, "requirements", passing)
    second = control.rt.review(control.owner, requirement, "requirements", failing)
    refs = control.g.evidence_for(requirement, _binding(control, first["receipt"]), "requirements")
    assert refs[0]["id"] == second["receipt"]
    assert control.g.receipt(refs[0]["id"])["result"]["verdict"] == "fail"


def test_evidence_for_rejects_ambiguous_projects(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "governance-projects")
    first = control.rt.review(control.owner, requirement, "requirements", adapter)
    second = control.rt.review(control.owner, requirement, "requirements", adapter)
    other_project = control.k.create_project(control.owner, "Other evidence project")["id"]
    with control.s.transaction():
        control.s.execute("DROP TRIGGER receipts_no_update")
        control.s.execute("UPDATE receipts SET project=? WHERE id=?", (other_project, second["receipt"]))
    with pytest.raises(Fault) as error:
        control.g.evidence_for(requirement, _binding(control, first["receipt"]), "requirements")
    assert error.value.code == "observed_order_invalid"


@pytest.mark.parametrize("invalid_project", ["", 123])
def test_evidence_for_rejects_empty_or_non_string_project(invalid_project):
    from types import SimpleNamespace

    class CandidateStore:
        def all(self, sql, args=()):
            return [{"id": "receipt", "project": invalid_project}]

    standalone = Governance(CandidateStore(), object(), SimpleNamespace(), mode="validation")
    with pytest.raises(Fault) as error:
        standalone.evidence_for("subject", "binding", "role")
    assert error.value.code == "observed_order_invalid"


def test_evidence_for_rejects_missing_observation_event(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "governance-missing-event")
    reviewed = control.rt.review(control.owner, requirement, "requirements", adapter)
    event = control.s.one(
        "SELECT seq FROM events WHERE kind='run_observed' AND project=? "
        "AND json_extract(body,'$.receipt')=?",
        (project, reviewed["receipt"]),
        True,
    )
    with control.s.transaction():
        control.s.execute("DROP TRIGGER events_no_delete")
        control.s.execute("DELETE FROM events WHERE seq=?", (event["seq"],))
    with pytest.raises(Fault) as error:
        control.g.evidence_for(requirement, _binding(control, reviewed["receipt"]), "requirements")
    assert error.value.code == "observed_order_invalid"


def test_evidence_for_empty_family_is_still_empty(full):
    assert full.g.evidence_for("missing-subject", "missing-binding", "missing-role") == []
