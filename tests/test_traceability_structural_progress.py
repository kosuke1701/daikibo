"""Finite public checks for Traceability Unit T1's readonly projection."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from daikibo.common import Fault
from test_traceability_unit_b_contract_repair import _decisions, _leaf_ids, _mapping
from test_unit5_durable_delivery import _public_traceability_delivery_fixture

pytest_plugins = ["test_traceability_refs"]


def _db_blob_state(control):
    tables = [row["name"] for row in control.s.all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    rows = {
        table: [tuple(row.values()) for row in control.s.all(f'SELECT * FROM "{table}" ORDER BY rowid')]
        for table in tables
    }
    blobs = [
        (str(path.relative_to(control.s.blobs)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(control.s.blobs.rglob("*"))
        if path.is_file()
    ]
    return rows, blobs


def test_duplicate_population_proposals_are_one_logical_entry(full):
    project = full.k.create_project(full.owner, "T1 duplicate proposal")['id']
    source = full.k.source(full.owner, project, "one\ntwo", "T1 source")['id']
    first = full.traceability.propose(full.owner, project, kind="document", name="docs", scope={"source": source})
    first_state = full.supervisor.state_digest(project)
    duplicate = full.traceability.propose(full.owner, project, kind="document", name="docs", scope={"source": source})
    assert full.supervisor.state_digest(project) == first_state
    changed = full.traceability.propose(
        full.owner, project, kind="document", name="docs", scope={"source": source}, label="changed",
        payload={"proposal_id": first["id"], "item": first["id"]},
    )
    other_set = full.traceability.propose(full.owner, project, kind="document", name="other", scope={"source": source})

    projection = full.traceability.structural_progress_projection(project)
    proposals = projection["proposals"]
    assert len(proposals) == 3
    same = [entry for entry in proposals if entry["body"].get("id") == "$proposal" and entry["set"]["name"] == "docs"]
    assert len(same) == 2
    assert all(entry["statuses"] == ["proposed"] for entry in same)
    assert first["id"] != duplicate["id"]
    assert changed["id"] not in {first["id"], duplicate["id"]}
    assert other_set["set_id"] != first["set_id"]
    assert {entry["body"].get("extra", {}).get("label") for entry in same} == {None, "changed"}
    changed_entry = next(entry for entry in same if entry["body"].get("extra", {}).get("label") == "changed")
    assert changed_entry["body"]["extra"]["payload"] == {"proposal_id": first["id"], "item": first["id"]}


def test_ready_duplicate_proposal_does_not_change_logical_state(full):
    project = full.k.create_project(full.owner, "T1 ready duplicate")['id']
    source = full.k.source(full.owner, project, "one\ntwo", "T1 ready source")['id']
    first = full.traceability.propose(full.owner, project, kind="document", name="same", scope={"source": source})
    full.traceability.extract(full.owner, first["id"])
    before = full.traceability.structural_progress_projection(project)
    before_digest = full.supervisor.state_digest(project)

    duplicate = full.traceability.propose(
        full.owner, project, kind="document", name="same", scope={"source": source},
    )
    after = full.traceability.structural_progress_projection(project)

    assert full.supervisor.state_digest(project) == before_digest
    assert after == before
    entry = next(value for value in after["proposals"] if value["set"]["name"] == "same")
    assert entry["status"] == "ready"
    assert entry["statuses"] == ["ready"]
    assert duplicate["id"] != first["id"]


def test_extra_blob_named_value_remains_user_meaning(full):
    project = full.k.create_project(full.owner, "T1 opaque extra")['id']
    source = full.k.source(full.owner, project, "opaque", "T1 opaque source")['id']
    proposal = full.traceability.propose(
        full.owner, project, kind="document", scope={"source": source},
        payload={"blob": "user meaning", "ref_type": "user-owned"},
    )
    state = full.supervisor.state_digest(project)
    projection = full.traceability.structural_progress_projection(project)
    entry = next(value for value in projection["proposals"] if value["body"]["extra"].get("payload"))
    assert entry["body"]["extra"]["payload"] == {"blob": "user meaning", "ref_type": "user-owned"}
    assert state == full.supervisor.state_digest(project)
    assert proposal["id"]


def test_extract_projection_contains_complete_population_and_is_readonly(full, tmp_path):
    project = full.k.create_project(full.owner, "T1 population")['id']
    source = full.k.source(full.owner, project, "alpha\nβeta\n", "T1 unicode")['id']
    proposal = full.traceability.propose(full.owner, project, kind="document", scope={"source": source})
    revision = full.traceability.extract(full.owner, proposal["id"])["revision"]
    before = _db_blob_state(full)
    first = full.traceability.structural_progress_projection(project)
    after = _db_blob_state(full)
    second = full.traceability.structural_progress_projection(project)

    assert before == after
    assert first == second
    revision_row = full.s.one("SELECT * FROM traceability_revisions WHERE id=?", (revision,), True)
    item_rows = full.s.all("SELECT * FROM traceability_items WHERE revision=? ORDER BY ordinal", (revision,))
    projected = [entry for entry in first["items"] if entry["identity"]["revision"]["revision"] == int(revision_row["revision"])]
    assert len(projected) == len(item_rows)
    assert first["revisions"][0]["body"]["inventory"]
    assert first["format"] == "daikibo.traceability-structural-progress.v1"
    assert full.supervisor.state_digest(project)


def test_decision_and_mapping_duplicates_normalize_only_generated_links(refs_fixture):
    fixture = refs_fixture
    control = fixture["control"]
    leaves = _leaf_ids(fixture)
    first = _decisions(fixture, [(leaves[0], fixture["task"])])
    duplicate = _decisions(fixture, [(leaves[0], fixture["task"])])
    mapping = _mapping(control, fixture, first, leaves[0], fixture["git_file"])
    mapping_duplicate = _mapping(control, fixture, duplicate, leaves[0], fixture["git_file"])
    changed = _mapping(control, fixture, first, leaves[0], fixture["git_symbol"])

    projection = control.traceability.structural_progress_projection(fixture["project"])
    assert len(projection["decisions"]) == 1
    assert len(projection["mappings"]) == 3
    assert projection["decisions"][0]["statuses"] == ["proposed"]
    targets = [entry["body"]["mappings"][0]["target_refs"][0] for entry in projection["mappings"]]
    assert {target["ref_type"] for target in targets} == {"git_file", "git_symbol"}
    assert fixture["git_file"] in targets and fixture["git_symbol"] in targets
    decision_refs = {
        entry["body"]["mappings"][0]["decision_ref"]["id"]
        for entry in projection["mappings"]
    }
    assert decision_refs == {first["id"], duplicate["id"]}
    symbols = [entry["body"] for entry in projection["items"] if entry["body"].get("type") == "symbol_group"]
    assert symbols and isinstance(symbols[0]["symbol_id"], dict)
    assert isinstance(symbols[0]["atom_ids"][0], dict)
    assert first["id"] != duplicate["id"]
    assert mapping["id"] != mapping_duplicate["id"]
    assert changed["id"] not in {mapping["id"], mapping_duplicate["id"]}


def test_scope_binding_duplicate_keeps_requirement_and_companion_role(refs_fixture):
    fixture = refs_fixture
    control = fixture["control"]
    program = control.p.begin(control.owner, fixture["project"], fixture["source"]["source_id"], compact=True)["program"]
    first = control.traceability.scope_propose(
        control.owner, fixture["project"], fixture["revision"], program, fixture["source"])
    duplicate = control.traceability.scope_propose(
        control.owner, fixture["project"], fixture["revision"], program, fixture["source"])
    projection = control.traceability.structural_progress_projection(fixture["project"])
    assert len(projection["bindings"]) == 1
    binding = projection["bindings"][0]["body"]
    assert binding["id"] == "$binding" and binding["proposal"] == "$proposal"
    assert binding["scope_requirement"] == fixture["source"]
    assert first["binding"] != duplicate["binding"]


def test_adopted_traceability_history_reads_without_rechecking_current_gate(full, full_project, tmp_path):
    fixture = _public_traceability_delivery_fixture(full, full_project, tmp_path)
    projection = full.traceability.structural_progress_projection(fixture["project"])
    kinds = {entry["kind"] for entry in projection["records"]}
    assert {"population_adopted", "decision_adopted", "mapping_adopted"} <= kinds
    assert projection["bindings"] and projection["bindings"][0]["status"] == "mandatory"


def test_mapping_reproposal_after_adoption_does_not_add_initial_state(full, full_project, tmp_path):
    fixture = _public_traceability_delivery_fixture(full, full_project, tmp_path)
    old = json.loads(full.s.one(
        "SELECT body FROM traceability_mappings WHERE id=?", (fixture["mapping"],), True,
    )["body"])
    before = full.traceability.structural_progress_projection(fixture["project"])
    full.traceability.map_propose(
        full.owner, fixture["project"], fixture["revision"], old["mappings"],
    )
    after = full.traceability.structural_progress_projection(fixture["project"])
    assert after == before
    mapping = next(entry for entry in after["mappings"] if entry["body"]["id"] == "$mapping")
    assert mapping["statuses"] == ["accepted"]


def test_closure_mapping_proposal_uses_closure_record_companion(full, full_project, tmp_path):
    from test_unit5_durable_delivery import _adopt_trace_subject, _public_traceability_delivery_fixture

    fixture = _public_traceability_delivery_fixture(full, full_project, tmp_path)
    old = json.loads(full.s.one(
        "SELECT body FROM traceability_mappings WHERE id=?", (fixture["mapping"],), True,
    )["body"])
    edge = dict(old["mappings"][0])
    decision = full.s.one(
        "SELECT body FROM traceability_decisions WHERE id=?", (edge["decision_ref"]["id"],), True,
    )
    edge["leaf_ids"] = [entry["item"] for entry in json.loads(decision["body"])["decisions"]]
    complete_mapping = full.traceability.map_propose(
        full.owner, fixture["project"], fixture["revision"], [edge],
    )
    _adopt_trace_subject(full, fixture["project"], complete_mapping["proposal"], fixture["revision"], fixture["adapter"])
    closure = full.traceability.closure_propose(
        full.owner, fixture["project"], fixture["revision"], "task", task=fixture["task"],
    )
    projection = full.traceability.structural_progress_projection(fixture["project"])
    closure_entries = [entry for entry in projection["records"] if entry["kind"] == "closure_proposed"]
    assert len(closure_entries) == 1
    assert any(entry["body"].get("adapter") == "traceability-closure-v1"
               for entry in projection["proposals"])
    assert closure["proposal"]


def test_missing_population_cas_is_an_explicit_read_failure(full, tmp_path):
    project = full.k.create_project(full.owner, "T1 missing CAS")['id']
    source = full.k.source(full.owner, project, "cas body", "T1 CAS")['id']
    proposal = full.traceability.propose(full.owner, project, kind="document", scope={"source": source})
    extracted = full.traceability.extract(full.owner, proposal["id"])
    pin = extracted["pins"][0]
    path = full.s.blob_path(pin)
    saved = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(Fault) as error:
            full.traceability.structural_progress_projection(project)
        assert error.value.code in {"missing_blob", "missing_evidence", "integrity_error", "unresolved_reference"}
    finally:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(saved)
    assert full.traceability.structural_progress_projection(project)["revisions"]
