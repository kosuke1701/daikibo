"""Portable direct specification closure and compatibility checks."""
from __future__ import annotations

import base64
import copy
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.execution_control_history import SECTIONS as EXECUTION_SECTIONS
from daikibo.knowledge_history import validate_specifications
from conftest import make_task
from test_consumer_p_artifact_provenance import _run_collect


def _refresh_context_digest(specification):
    context = specification["observed_context"]
    unsigned = {key: value for key, value in context.items() if key != "digest"}
    context["digest"] = digest(unsigned)


def _empty_legacy_history(specification, version):
    value = copy.deepcopy(specification)
    value.pop("observed_context", None)
    value["format"] = f"daikibo.spec.v{version}"
    if version < 5:
        value["planning_history"]["format"] = "daikibo.planning-history.v1"
        value["planning_history"].pop("program_origins", None)
        value.pop("traceability_history", None)
    if version >= 3:
        value["subplan_history"] = {
            "subplans": [], "subplan_packets": [], "subplan_compositions": [],
        }
        value["local_execution_history"] = {
            "local_execution_proposals": [], "local_execution_packets": [],
            "local_execution_records": [],
        }
    if version >= 4:
        value["execution_control_history"] = {section: [] for section in EXECUTION_SECTIONS}
    return value


def test_direct_export_is_v6_and_valid_in_fresh_process_without_original_home(
        full, full_project, tmp_path, monkeypatch):
    project, _repository, _task, _executed, result = _run_collect(full, full_project)
    monkeypatch.setenv("OPENAI_API_KEY", "DIRECT-EXPORT-CREDENTIAL-SENTINEL")
    before = {
        "events": full.s.one("SELECT count(*) AS n FROM events")["n"],
        "tasks": full.s.all("SELECT id,revision,status FROM tasks WHERE project=? ORDER BY id", (project,)),
        "assurance": full.s.one("SELECT count(*) AS n FROM assurance_objects WHERE project=?", (project,))["n"],
    }
    specification = full.k.export(full.owner, project)
    encoded = canonical(specification)
    assert b"DIRECT-EXPORT-CREDENTIAL-SENTINEL" not in encoded
    assert specification["format"] == "daikibo.spec.v6"
    assurance_history = specification["assurance_history"]
    assert len(assurance_history["assurance_objects"]) == before["assurance"]
    assert any(row["id"] == result["artifacts"][0]["material"]["id"]
               for row in assurance_history["assurance_objects"])
    context = specification["observed_context"]
    assert set(context["tables"]) == {"tasks", "candidates", "runs", "receipts", "repos"}
    assert context["historical_only"] is True
    assert context["runtime_restore_supported"] is False
    assert context["fresh_review_or_test_evidence"] is False
    assert context["blobs"]
    assert validate_specifications(specification)["artifacts"] >= 1
    assert before == {
        "events": full.s.one("SELECT count(*) AS n FROM events")["n"],
        "tasks": full.s.all("SELECT id,revision,status FROM tasks WHERE project=? ORDER BY id", (project,)),
        "assurance": full.s.one("SELECT count(*) AS n FROM assurance_objects WHERE project=?", (project,))["n"],
    }

    moved_home = tmp_path / "original-home-unavailable"
    shutil.move(full.s.home, moved_home)
    serialized = tmp_path / "portable-spec.json"
    serialized.write_bytes(encoded)
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    script = (
        "import json,sys; from daikibo.knowledge_history import validate_specifications; "
        "print(json.dumps(validate_specifications(json.load(open(sys.argv[1]))),sort_keys=True))"
    )
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "HOME": str(empty_home), "PATH": os.environ.get("PATH", "")}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(serialized)], cwd=unrelated_cwd,
        env=env, check=True, capture_output=True, text=True, timeout=30,
    )
    assert json.loads(completed.stdout)["artifacts"] >= 1


def test_direct_and_chunked_share_rows_reader_and_cas_enumerator(
        full, full_project, monkeypatch):
    import daikibo.archive_chunks as archive_chunks
    import daikibo.portable_context as portable_context

    project, _repository, _task, _executed, _result = _run_collect(full, full_project)
    calls = []
    original = portable_context.row_cas_refs

    def capture(section, row, blob_reader):
        calls.append((section, row.get("id")))
        return original(section, row, blob_reader)

    monkeypatch.setattr(portable_context, "row_cas_refs", capture)
    monkeypatch.setattr(archive_chunks, "row_cas_refs", capture)
    direct = full.k.export(full.owner, project)
    direct_rows = set(calls)
    calls.clear()
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    chunked_rows = set(calls)
    exported = full.history.export_archive(full.owner, baseline["id"])
    inspected = full.history.inspect_archive(full.owner, exported["path"], exported["sha256"])
    assert inspected["verified"] is True
    with zipfile.ZipFile(exported["path"]) as archive:
        payload = parse_json(archive.read("snapshot.json"))
        stream = b"".join(archive.read("objects/" + part["sha256"])
                           for part in payload["records"]["chunks"])
    records = [parse_json(line) for line in stream.splitlines()]
    chunked = {section: [record["row"] for record in records
                         if record["section"] == section]
               for section in ("tasks", "candidates", "runs", "receipts", "repos")}
    assert direct["observed_context"]["tables"] == chunked
    shared_sections = {
        "tasks", "candidates", "runs", "receipts", "repos",
        "assurance_objects", "assurance_refs",
        "traceability_sets", "traceability_revisions", "traceability_items",
        "traceability_proposals", "traceability_decisions", "traceability_mappings",
        "traceability_bindings", "traceability_records",
    }
    assert {key for key in direct_rows if key[0] in shared_sections} == {
        key for key in chunked_rows if key[0] in shared_sections
    }


@pytest.mark.parametrize("mutation", [
    "missing_task", "missing_candidate", "missing_run", "missing_receipt",
    "missing_repo", "foreign_row", "duplicate_task", "missing_blob",
    "extra_blob", "bad_base64", "duplicate_blob", "foreign_run_binding",
    "missing_task_body_digest", "missing_run_result_digest",
    "receipt_binding", "v6_as_v5",
    "v6_as_v5_without_context",
])
def test_direct_context_rejects_missing_foreign_duplicate_or_tampered_wire(
        full, full_project, mutation):
    project, repository, _task, executed, _result = _run_collect(full, full_project)
    specification = full.k.export(full.owner, project)
    damaged = copy.deepcopy(specification)
    context = damaged["observed_context"]
    tables = context["tables"]
    if mutation in {"missing_task", "missing_candidate", "missing_run", "missing_receipt", "missing_repo"}:
        candidate = next(row for row in tables["candidates"] if row["id"] == executed["candidate"])
        selected = {
            "missing_task": ("tasks", candidate["task"]),
            "missing_candidate": ("candidates", candidate["id"]),
            "missing_run": ("runs", candidate["implementation_run"]),
            "missing_receipt": ("receipts", next(
                row["id"] for row in tables["receipts"]
                if row.get("run") == candidate["implementation_run"])),
            "missing_repo": ("repos", repository),
        }[mutation]
        section, ident = selected
        assert any(row["id"] == ident for row in tables[section])
        tables[section] = [row for row in tables[section] if row["id"] != ident]
        _refresh_context_digest(damaged)
    elif mutation == "foreign_row":
        tables["tasks"][0]["project"] = "PRJ-foreign"
        _refresh_context_digest(damaged)
    elif mutation == "duplicate_task":
        tables["tasks"].append(copy.deepcopy(tables["tasks"][0]))
        _refresh_context_digest(damaged)
    elif mutation == "missing_blob":
        material = next(row for row in damaged["assurance_history"]["assurance_objects"]
                        if row.get("kind") == "material")
        payload_blob = json.loads(material["body"])["payload_blob"]
        context["blobs"] = [row for row in context["blobs"] if row["sha256"] != payload_blob]
        _refresh_context_digest(damaged)
    elif mutation == "extra_blob":
        raw = b"unreferenced portable context bytes"
        context["blobs"].append({"sha256": digest(raw), "bytes": len(raw),
                                 "encoding": "base64", "data": base64.b64encode(raw).decode("ascii")})
        context["blobs"].sort(key=lambda item: item["sha256"])
        _refresh_context_digest(damaged)
    elif mutation == "bad_base64":
        context["blobs"][0]["data"] = "***="
        _refresh_context_digest(damaged)
    elif mutation == "duplicate_blob":
        context["blobs"].append(copy.deepcopy(context["blobs"][0]))
        context["blobs"].sort(key=lambda item: item["sha256"])
        _refresh_context_digest(damaged)
    elif mutation == "foreign_run_binding":
        candidate = next(row for row in tables["candidates"] if row["id"] == executed["candidate"])
        candidate["implementation_run"] = "RUN-foreign"
        _refresh_context_digest(damaged)
    elif mutation == "missing_task_body_digest":
        tables["tasks"][0].pop("body_digest")
        _refresh_context_digest(damaged)
    elif mutation == "missing_run_result_digest":
        run = next(row for row in tables["runs"] if row.get("result") is not None)
        run.pop("result_digest")
        _refresh_context_digest(damaged)
    elif mutation == "receipt_binding":
        candidate = next(row for row in tables["candidates"] if row["id"] == executed["candidate"])
        receipt = next(row for row in tables["receipts"]
                       if row.get("run") == candidate["implementation_run"])
        receipt["binding"] = "BINDING-foreign"
        _refresh_context_digest(damaged)
    elif mutation == "v6_as_v5":
        damaged["format"] = "daikibo.spec.v5"
    else:
        damaged["format"] = "daikibo.spec.v5"
        damaged.pop("observed_context")
    with pytest.raises(Fault):
        validate_specifications(damaged)


@pytest.mark.parametrize("target", ["root", "child"])
def test_direct_context_requires_complete_material_cas_closure(full, full_project, target):
    from daikibo.assurance import material_cas_closure

    project, _repository, task, _executed, _result = _run_collect(full, full_project)
    full.rt.tests(full.owner, task)
    specification = full.k.export(full.owner, project)
    closures = []
    for material in specification["assurance_history"]["assurance_objects"]:
        if material.get("kind") != "material":
            continue
        root = json.loads(material["body"]).get("payload_blob")
        if isinstance(root, str):
            closure = material_cas_closure(full.s, root)
            closures.append((root, closure))
    selected = ([root for root, _closure in closures][:1] if target == "root" else
                [child for root, closure in closures for child in closure if child != root])
    assert selected
    for missing in selected:
        damaged = copy.deepcopy(specification)
        damaged["observed_context"]["blobs"] = [
            item for item in damaged["observed_context"]["blobs"] if item["sha256"] != missing
        ]
        _refresh_context_digest(damaged)
        with pytest.raises(Fault):
            validate_specifications(damaged)


def test_direct_spec_preserves_legacy_v2_to_v5_readers(full):
    project = full.k.create_project(full.owner, "legacy spec formats")["id"]
    specification = full.k.export(full.owner, project)
    assert specification["format"] == "daikibo.spec.v5"
    for version in range(2, 6):
        assert validate_specifications(_empty_legacy_history(specification, version))["artifacts"] == 0


def test_v6_empty_observed_context_stays_valid_and_chunked_exportable(full):
    project = full.k.create_project(full.owner, "empty observed context")["id"]
    full.assurance.store_object(
        full.owner, project, "profile", "portable-empty-context", 1,
        {"format": "identity.v1", "project": project, "name": "portable"},
    )

    specification = full.k.export(full.owner, project)
    assert specification["format"] == "daikibo.spec.v6"
    context = specification["observed_context"]
    assert set(context["tables"]) == {"tasks", "candidates", "runs", "receipts", "repos"}
    assert all(rows == [] for rows in context["tables"].values())
    assert context["blobs"] == []
    assert validate_specifications(specification)["artifacts"] == 0

    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    inspected = full.history.inspect_archive(full.owner, exported["path"], exported["sha256"])
    assert inspected["verified"] is True


def test_direct_context_uses_retained_task_revision_history_for_old_candidate(full, full_project):
    project, _repository, _requirement, _root = full_project
    task = make_task(full, full_project)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")
    observed = full.rt.tests(full.owner, task)["checks"][0]
    receipt = full.g.receipt(observed["receipt"])
    for role in ("spec", "quality", "test_adequacy"):
        full.rt.review(full.owner, task, role, "fixture")
    full.w.complete(full.owner, task, full.w.task(full.owner, task)["revision"])
    before = full.w.task(full.owner, task)
    full.w.replan(full.owner, task, before["revision"], "Retain the original candidate identity")
    full.w.plan_tests(full.owner, task, {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    full.assurance.store_object(
        full.owner, project, "profile", "portable-old-candidate", 1,
        {"format": "profile.v1", "project": project, "observed": {
            "kind": "observed_result", "project": project,
            "receipt": receipt["id"], "run": receipt["run"],
            "receipt_digest": digest(receipt), "run_binding": receipt["binding"],
            "snapshot_digest": receipt["snapshot"], "result_digest": digest(receipt["result"]),
        }},
    )

    specification = full.k.export(full.owner, project)
    assert validate_specifications(specification)["artifacts"] >= 1
    assert "task_revision_history" not in specification["observed_context"]["tables"]
    damaged = copy.deepcopy(specification)
    damaged["task_revision_history"] = [row for row in damaged["task_revision_history"]
                                        if row["task"] != task]
    with pytest.raises(Fault):
        validate_specifications(damaged)


def test_typed_cas_enumerator_ignores_ordinary_sha256_fields():
    from daikibo.portable_context import row_cas_refs

    value = "a" * 64
    assert row_cas_refs("traceability_records", {
        "id": "TRACE-ordinary-digest", "digest": value,
        "ordinary_identity_digest": value, "body": {"checksum": value},
    }, {}) == set()


def test_direct_context_capacity_is_checked_before_base64_decode(monkeypatch):
    import daikibo.portable_context as portable_context

    raw = b"abc"
    wire = {
        "format": portable_context.CONTEXT_FORMAT,
        "project": "PRJ-capacity",
        "historical_only": True,
        "runtime_restore_supported": False,
        "fresh_review_or_test_evidence": False,
        "tables": {section: [] for section in portable_context.CONTEXT_SECTIONS},
        "blobs": [{"sha256": digest(raw), "bytes": len(raw), "encoding": "base64",
                   "data": base64.b64encode(raw).decode("ascii")}],
    }
    wire["digest"] = digest(wire)
    monkeypatch.setattr(portable_context, "MAX_SNAPSHOT_BYTES", 2)

    def no_decode(*_args, **_kwargs):
        raise AssertionError("base64 decoding began before the byte budget check")

    monkeypatch.setattr(portable_context.base64, "b64decode", no_decode)
    with pytest.raises(Fault) as rejected:
        portable_context.PortableObservedContext.from_wire(wire, project="PRJ-capacity",
                                                           code="invalid_snapshot")
    assert rejected.value.code == "snapshot_too_large"
    assert rejected.value.details["kind"] == "single_blob_bytes"


def test_direct_context_cumulative_encoded_capacity_is_checked_before_decode(monkeypatch):
    import daikibo.portable_context as portable_context

    blobs = []
    for raw in (b"a", b"b"):
        blobs.append({"sha256": digest(raw), "bytes": len(raw), "encoding": "base64",
                      "data": base64.b64encode(raw).decode("ascii")})
    wire = {
        "format": portable_context.CONTEXT_FORMAT,
        "project": "PRJ-capacity",
        "historical_only": True,
        "runtime_restore_supported": False,
        "fresh_review_or_test_evidence": False,
        "tables": {section: [] for section in portable_context.CONTEXT_SECTIONS},
        "blobs": sorted(blobs, key=lambda item: item["sha256"]),
    }
    wire["digest"] = digest(wire)
    monkeypatch.setattr(portable_context, "MAX_SNAPSHOT_BYTES", 7)

    def no_decode(*_args, **_kwargs):
        raise AssertionError("base64 decoding began before the cumulative byte budget check")

    monkeypatch.setattr(portable_context.base64, "b64decode", no_decode)
    with pytest.raises(Fault) as rejected:
        portable_context.PortableObservedContext.from_wire(wire, project="PRJ-capacity",
                                                           code="invalid_snapshot")
    assert rejected.value.code == "snapshot_too_large"
    assert rejected.value.details == {
        "kind": "encoded_blob_bytes", "required": 8, "limit": 7,
    }


def test_direct_context_accepts_encoded_cas_exactly_at_limit(monkeypatch):
    import daikibo.portable_context as portable_context

    raw = b"abc"
    wire = {
        "format": portable_context.CONTEXT_FORMAT,
        "project": "PRJ-capacity",
        "historical_only": True,
        "runtime_restore_supported": False,
        "fresh_review_or_test_evidence": False,
        "tables": {section: [] for section in portable_context.CONTEXT_SECTIONS},
        "blobs": [{"sha256": digest(raw), "bytes": len(raw), "encoding": "base64",
                   "data": base64.b64encode(raw).decode("ascii")}],
    }
    wire["digest"] = digest(wire)
    monkeypatch.setattr(portable_context, "MAX_SNAPSHOT_BYTES", 4)
    reader = portable_context.PortableObservedContext.from_wire(
        wire, project="PRJ-capacity", code="invalid_snapshot")
    assert reader.blob_get(digest(raw)) == raw


def test_direct_spec_total_capacity_has_structured_snapshot_too_large(full, full_project, monkeypatch):
    import daikibo.knowledge_history as history

    project, _repository, _task, _executed, _result = _run_collect(full, full_project)
    specification = full.k.export(full.owner, project)
    monkeypatch.setattr(history, "MAX_SNAPSHOT_BYTES", len(canonical(specification)) - 1)
    with pytest.raises(Fault) as rejected:
        validate_specifications(specification)
    assert rejected.value.code == "snapshot_too_large"
    assert rejected.value.details["kind"] == "specification_bytes"
    assert rejected.value.details["required"] > rejected.value.details["limit"]


def test_direct_spec_rejects_non_json_specification_as_structured_fault(full):
    project = full.k.create_project(full.owner, "non-json specification")["id"]
    specification = full.k.export(full.owner, project)
    specification["unexpected"] = object()
    with pytest.raises(Fault) as rejected:
        validate_specifications(specification)
    assert rejected.value.code == "invalid_snapshot"


def test_direct_export_capacity_failure_does_not_mutate_source_history(
        full, full_project, monkeypatch):
    import daikibo.portable_context as portable_context

    project, _repository, _task, _executed, _result = _run_collect(full, full_project)
    before = {
        "events": full.s.one("SELECT count(*) AS n FROM events")["n"],
        "tasks": full.s.all("SELECT id,revision,status FROM tasks WHERE project=? ORDER BY id", (project,)),
        "assurance": full.s.one("SELECT count(*) AS n FROM assurance_objects WHERE project=?", (project,))["n"],
    }
    monkeypatch.setattr(portable_context, "MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(Fault) as rejected:
        full.k.export(full.owner, project)
    assert rejected.value.code == "snapshot_too_large"
    assert full.s.one("SELECT count(*) AS n FROM events")["n"] == before["events"]
    assert full.s.all("SELECT id,revision,status FROM tasks WHERE project=? ORDER BY id", (project,)) == before["tasks"]
    assert full.s.one("SELECT count(*) AS n FROM assurance_objects WHERE project=?", (project,))["n"] == before["assurance"]
