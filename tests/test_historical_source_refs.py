"""Portable history keeps old source-reference bytes without reinterpreting them."""
from __future__ import annotations

import copy
import zipfile

import pytest

from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.knowledge import Knowledge
from daikibo.knowledge_history import inspect_archive, validate_specifications
from test_chunked_knowledge import rewrite as rewrite_chunked


def _legacy_requirement(control, project, source, refs, monkeypatch):
    """Persist an old-shaped revision through the normal artifact APIs."""
    body = {
        "title": "Legacy source reference",
        "statement": "Retain this historical source reference exactly.",
        "acceptance": ["AC-LEGACY"],
        "source_refs": refs,
    }
    current = dict(body)
    current["source_refs"] = [source]
    original = Knowledge.validate_body

    def old_writer(kind, value):
        if kind == "requirement":
            accepted_shape = dict(value)
            accepted_shape.pop("source_refs", None)
            return original(kind, accepted_shape)
        return original(kind, value)

    monkeypatch.setattr(Knowledge, "validate_body", staticmethod(old_writer))
    try:
        artifact = control.k.propose(control.owner, project, "requirement", body)
    finally:
        monkeypatch.setattr(Knowledge, "validate_body", staticmethod(original))
    control.k.revise(control.owner, artifact["id"], 1, current, "Normalize the current source reference shape.")
    control.k.accept(control.owner, artifact["id"], 2)
    return artifact["id"], body, current


def _diagnostic_types(report):
    return [(item["pointer"], item["actual_type"]) for item in report["historical_reference_diagnostics"]]


@pytest.mark.parametrize("shape", ["mixed", "object"])
def test_old_revision_source_refs_are_preserved_in_direct_and_chunked_exports(full, full_project, monkeypatch, shape):
    project = full_project[0]
    source = full.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (project,), True)["id"]
    refs = ([source, {"source": "SRC-nested-not-validated", "start": 0, "end": 1}, 4, None, [], ""]
            if shape == "mixed" else {"source": "SRC-nested-not-validated", "start": 0, "end": 1})
    artifact, historical_body, current_body = _legacy_requirement(full, project, source, refs, monkeypatch)
    current_before = full.k.artifact(full.owner, artifact)

    direct_baseline = full.k.baseline(full.owner, project, layout="legacy")
    direct_archive = full.history.export_archive(full.owner, direct_baseline["id"])
    direct = inspect_archive(direct_archive["path"], direct_archive["sha256"])

    chunked_baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    chunked_archive = full.history.export_archive(full.owner, chunked_baseline["id"])
    chunked = inspect_archive(chunked_archive["path"], chunked_archive["sha256"])

    expected = ([
        ("/source_refs/1", "object"),
        ("/source_refs/2", "number"),
        ("/source_refs/3", "null"),
        ("/source_refs/4", "array"),
        ("/source_refs/5", "string"),
    ] if shape == "mixed" else [("/source_refs", "object")])
    for report in (direct, chunked):
        assert report["history_integrity"] == "verified"
        assert report["historical_reference_diagnostic_count"] == len(expected)
        assert report["historical_reference_interpretation"] == "unresolved"
        assert _diagnostic_types(report) == expected
        assert report["historical_reference_diagnostics_total"] == len(expected)
        assert report["historical_reference_diagnostics_truncated"] is False

    assert _diagnostic_types(direct) == _diagnostic_types(chunked)
    with zipfile.ZipFile(direct_archive["path"]) as archive:
        payload = parse_json(archive.read("snapshot.json"))
    retained = next(row for row in payload["specifications"]["revisions"]
                    if row["artifact"] == artifact and row["revision"] == 1)
    assert retained["body"] == historical_body
    assert retained["digest"] == digest(historical_body)
    assert full.k.artifact(full.owner, artifact)["body"] == current_body
    assert current_before["status"] == "accepted"
    assert full.k.artifact(full.owner, artifact)["status"] == "accepted"


def test_source_ref_projection_is_bounded_and_reports_original_pointers(full, full_project, monkeypatch):
    project = full_project[0]
    source = full.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (project,), True)["id"]
    refs = [source] + [{"source": "SRC-nested-not-validated", "start": index, "end": index + 1} for index in range(150)]
    artifact, historical_body, _ = _legacy_requirement(full, project, source, refs, monkeypatch)
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    archive = full.history.export_archive(full.owner, baseline["id"])
    report = inspect_archive(archive["path"], archive["sha256"])

    assert report["historical_reference_diagnostic_count"] == 150
    assert len(report["historical_reference_diagnostics"]) == 100
    assert report["historical_reference_diagnostics_truncated"] is True
    assert report["historical_reference_diagnostics_total"] == 150
    assert report["historical_reference_diagnostics"][-1]["pointer"] == "/source_refs/100"
    assert historical_body["source_refs"][1]["source"] == "SRC-nested-not-validated"
    assert artifact


@pytest.mark.parametrize("layout", ["legacy", "chunked"])
def test_current_malformed_source_refs_are_controlled_rejections(full, full_project, tmp_path, layout):
    project = full_project[0]
    baseline = full.k.baseline(full.owner, project, layout=layout, chunk_bytes=1024)
    archive = full.history.export_archive(full.owner, baseline["id"])
    target = tmp_path / "current-malformed.zip"
    if layout == "legacy":
        with zipfile.ZipFile(archive["path"]) as original:
            manifest = parse_json(original.read("manifest.json"))
            payload = parse_json(original.read("snapshot.json"))
        spec = payload["specifications"]
        artifact = next(row for row in spec["artifacts"] if row["kind"] == "requirement")
        current = next(row for row in spec["revisions"]
                       if row["artifact"] == artifact["id"] and row["revision"] == artifact["revision"])
        current["body"]["source_refs"] = {"source": "must-not-be-coerced"}
        current["digest"] = digest(current["body"])
        artifact["body"] = copy.deepcopy(current["body"])
        artifact["digest"] = current["digest"]
        baseline_ref = next(row for row in payload["baseline"]["artifacts"] if row["id"] == artifact["id"])
        baseline_ref["digest"] = current["digest"]
        content = canonical(payload)
        manifest["files"]["snapshot.json"] = {"bytes": len(content), "sha256": digest(content)}
        with zipfile.ZipFile(target, "w") as output:
            output.writestr("manifest.json", canonical(manifest))
            output.writestr("snapshot.json", content)
        expected_code = "invalid_snapshot"
    else:
        def alter(payload, rows, objects):
            artifact = next(row["row"] for row in rows
                            if row["section"] == "artifacts" and row["row"]["kind"] == "requirement")
            current = next(row["row"] for row in rows
                           if row["section"] == "revisions"
                           and row["row"]["artifact"] == artifact["id"]
                           and row["row"]["revision"] == artifact["revision"])
            current["body"]["source_refs"] = {"source": "must-not-be-coerced"}
            current["digest"] = digest(current["body"])
            artifact["body"] = copy.deepcopy(current["body"])
            artifact["digest"] = current["digest"]
            reference = next(value for value in payload["baseline"]["artifacts"] if value["id"] == artifact["id"])
            reference["digest"] = current["digest"]

        checksum = rewrite_chunked(archive["path"], target, alter)
        expected_code = "invalid_archive"

    with pytest.raises(Fault) as error:
        inspect_archive(target, digest(target.read_bytes()) if layout == "legacy" else checksum)
    assert error.value.code == expected_code


@pytest.mark.parametrize("layout", ["legacy", "chunked"])
def test_historical_non_object_body_is_a_controlled_rejection(full, full_project, monkeypatch, tmp_path, layout):
    project = full_project[0]
    source = full.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (project,), True)["id"]
    artifact, _, _ = _legacy_requirement(
        full, project, source, {"source": "SRC-nested-not-validated", "start": 0, "end": 1}, monkeypatch)
    baseline = full.k.baseline(full.owner, project, layout=layout, chunk_bytes=1024)
    archive = full.history.export_archive(full.owner, baseline["id"])
    target = tmp_path / f"historical-body-{layout}.zip"
    if layout == "legacy":
        with zipfile.ZipFile(archive["path"]) as original:
            manifest = parse_json(original.read("manifest.json"))
            payload = parse_json(original.read("snapshot.json"))
        revision = next(row for row in payload["specifications"]["revisions"]
                        if row["artifact"] == artifact and row["revision"] == 1)
        revision["body"] = ["opaque historical body"]
        revision["digest"] = digest(revision["body"])
        content = canonical(payload)
        manifest["files"]["snapshot.json"] = {"bytes": len(content), "sha256": digest(content)}
        with zipfile.ZipFile(target, "w") as output:
            output.writestr("manifest.json", canonical(manifest))
            output.writestr("snapshot.json", content)
        checksum = digest(target.read_bytes())
        expected_code = "invalid_snapshot"
    else:
        def alter(payload, rows, objects):
            revision = next(row["row"] for row in rows
                            if row["section"] == "revisions"
                            and row["row"]["artifact"] == artifact
                            and row["row"]["revision"] == 1)
            revision["body"] = ["opaque historical body"]
            revision["digest"] = digest(revision["body"])

        checksum = rewrite_chunked(archive["path"], target, alter)
        expected_code = "invalid_archive"

    with pytest.raises(Fault) as error:
        inspect_archive(target, checksum)
    assert error.value.code == expected_code


def test_direct_spec_validation_exposes_historical_projection_without_rewriting_body(full, full_project, monkeypatch):
    project = full_project[0]
    source = full.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (project,), True)["id"]
    artifact, historical_body, _ = _legacy_requirement(
        full, project, source, [{"source": source, "start": 0, "end": 1}], monkeypatch)
    spec = full.k.export(full.owner, project)
    validation = validate_specifications(spec, include_projection=True)
    counts = validation["counts"]
    assert validation["history_integrity"] == "verified"
    assert validation["historical_reference_diagnostic_count"] == 1
    assert validation["historical_reference_diagnostics"][0]["digest"] == digest(historical_body)
    assert next(row for row in spec["revisions"] if row["artifact"] == artifact and row["revision"] == 1)["body"] == historical_body
