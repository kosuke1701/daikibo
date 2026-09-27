"""Focused regressions for the Unit B closure and archive contracts."""

from __future__ import annotations

import json
import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.traceability import Traceability, inspect_archive
from daikibo.traceability_refs import TraceabilityRefResolver
from test_traceability_refs import refs_fixture


def _leaf_ids(fixture):
    control = fixture["control"]
    return [row["id"] for row in control.s.all(
        "SELECT id FROM traceability_items WHERE revision=? AND leaf=1 ORDER BY ordinal",
        (fixture["revision"],),
    )]


def _decisions(fixture, assignments, *, ac_id=False):
    control = fixture["control"]
    requirement = dict(fixture["artifact"])
    if ac_id:
        requirement["ac_id"] = "AC-ADD"
    assert TraceabilityRefResolver(control).resolve(
        control.owner, fixture["project"], requirement)["ref_type"] == "artifact_ac"
    return control.traceability.decide_propose(
        control.owner,
        fixture["project"],
        fixture["revision"],
        [{
            "item": leaf,
            "handling": "port",
            "reason": "fixed responsibility",
            "evidence": [fixture["source"]["source_id"]],
            "purpose": "document_requirement",
            "task": task,
            "contributors": [{"task": task, "revision": 1, "required": True}],
            "requirement": requirement,
            "acceptance": requirement,
        } for leaf, task in assignments],
    )


def _fixture_adopt(control, project, revision, table, row_id, kind):
    row = control.s.one(f"SELECT * FROM {table} WHERE id=?", (row_id,), True)
    body = json.loads(row["body"])
    with control.s.transaction():
        control.traceability._append_record(
            project,
            revision,
            body["proposal"],
            kind,
            control.traceability._record_body(
                kind,
                project,
                revision,
                body["proposal"],
                {"table": table, "id": row_id, "digest": row["digest"]},
                fixture_state_only=True,
            ),
        )


def _mapping(control, fixture, decision, leaf, target):
    return control.traceability.map_propose(
        control.owner,
        fixture["project"],
        fixture["revision"],
        [{
            "leaf_ids": [leaf],
            "purpose": "code_port",
            "decision_ref": decision["id"],
            "contributors": [{"task": fixture["task"], "revision": 1, "required": True}],
            "target_refs": [target],
            "evidence_refs": [fixture["source"]["source_id"]],
        }],
    )


def _write_traceability_archive(payload, blobs, destination: Path):
    raw = canonical(payload)
    manifest = {
        "format": payload["format"],
        "project": payload["project"],
        "history_version": payload["history_version"],
        "payload": {"name": "traceability.json", "sha256": digest(raw), "bytes": len(raw)},
        "blobs": payload["blob_manifest"],
    }
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("manifest.json", canonical(manifest))
        archive.writestr("traceability.json", raw)
        for blob, value in blobs.items():
            archive.writestr(f"blobs/{blob}", value)


def _rewrite_target_and_rehash(payload, ref_type, field, value):
    def change(node):
        if isinstance(node, dict):
            if node.get("ref_type") == ref_type:
                node[field] = value
            for child in node.values():
                change(child)
        elif isinstance(node, list):
            for child in node:
                change(child)

    string_bodies = {
        (table, row["id"])
        for table, rows in payload["tables"].items()
        for row in rows
        if isinstance(row.get("body"), str)
    }
    for rows in payload["tables"].values():
        for row in rows:
            if isinstance(row.get("body"), str):
                row["body"] = json.loads(row["body"])
    for table in ("traceability_proposals", "traceability_mappings"):
        for row in payload["tables"][table]:
            change(row.get("body", {}).get("mappings", []))

    for _ in range(30):
        old_to_new = {}
        for rows in payload["tables"].values():
            for row in rows:
                if "body" not in row or "digest" not in row:
                    continue
                body = row["body"]
                if body.get("format") == "traceability.review-packet.v1":
                    body["binding"] = Traceability._packet_binding(body)
                new_digest = digest(body)
                if new_digest != row["digest"]:
                    old_to_new[row["digest"]] = new_digest
                    row["digest"] = new_digest
        if not old_to_new:
            for table, rows in payload["tables"].items():
                for row in rows:
                    if (table, row["id"]) in string_bodies:
                        row["body"] = canonical(row["body"]).decode()
            return

        def replace(node):
            if isinstance(node, dict):
                return {key: replace(child) for key, child in node.items()}
            if isinstance(node, list):
                return [replace(child) for child in node]
            return old_to_new.get(node, node) if isinstance(node, str) else node

        rewritten = replace(payload)
        payload.clear()
        payload.update(rewritten)
    raise AssertionError("archive digest rehash did not converge")


def _rewrite_standard_from_tables(source: Path, destination: Path, modified):
    """Rewrite only typed traceability rows while preserving standard chunks."""
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    header = json.loads(members["manifest.json"])
    snapshot = json.loads(members["snapshot.json"])
    parts = snapshot["records"]["chunks"]
    stream = b"".join(members["objects/" + part["sha256"]] for part in parts)
    lookup = {
        table: {row["id"]: row for row in modified["tables"][table]}
        for table in ("traceability_proposals", "traceability_mappings", "traceability_records")
    }
    output = []
    for line in stream.splitlines():
        record = json.loads(line)
        table = record["section"]
        if table in lookup:
            old = record["row"]
            new = dict(lookup[table][old["id"]])
            if isinstance(old.get("body"), str) and not isinstance(new.get("body"), str):
                new["body"] = canonical(new["body"]).decode()
            elif not isinstance(old.get("body"), str) and isinstance(new.get("body"), str):
                new["body"] = json.loads(new["body"])
            record["row"] = new
        output.append(canonical(record) + b"\n")
    data = b"".join(output)
    new_parts = []
    for start in range(0, len(data), snapshot["chunk_bytes"]):
        chunk = data[start:start + snapshot["chunk_bytes"]]
        sha = digest(chunk)
        members["objects/" + sha] = chunk
        snapshot["objects"][sha] = {"bytes": len(chunk)}
        new_parts.append({"sha256": sha, "bytes": len(chunk)})
    new_keys = {part["sha256"] for part in new_parts}
    for part in parts:
        if part["sha256"] not in new_keys:
            members.pop("objects/" + part["sha256"], None)
            snapshot["objects"].pop(part["sha256"], None)
    snapshot["records"] = {
        **snapshot["records"],
        "chunks": new_parts,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    snapshot_raw = canonical(snapshot)
    members["snapshot.json"] = snapshot_raw
    header["snapshot"] = {"sha256": digest(snapshot_raw), "bytes": len(snapshot_raw)}
    members["manifest.json"] = canonical(header)
    with zipfile.ZipFile(destination, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def test_task_closure_currentness_is_limited_to_selected_tdec_entries(refs_fixture):
    fixture = refs_fixture
    control = fixture["control"]
    project = fixture["project"]
    revision = fixture["revision"]
    leaves = _leaf_ids(fixture)

    other_body = dict(control.w.task(control.owner, fixture["task"])["body"])
    other_body.pop("task_kind", None)
    other_body.update({"title": "Unrelated B", "goal": "Independent B work"})
    task_b = control.w.create(control.owner, project, other_body)["id"]
    decision = _decisions(
        fixture,
        [(leaves[0], fixture["task"]), *[(leaf, task_b) for leaf in leaves[1:]]],
    )
    _fixture_adopt(control, project, revision, "traceability_decisions", decision["id"], "decision_adopted")
    mapping = _mapping(control, fixture, decision, leaves[0], fixture["artifact"])
    _fixture_adopt(control, project, revision, "traceability_mappings", mapping["id"], "mapping_adopted")

    closure = control.traceability.closure_propose(
        control.owner, project, revision, "task", task=fixture["task"])
    saved = json.loads(control.s.one(
        "SELECT body FROM traceability_records WHERE id=?", (closure["id"],), True)["body"])
    control.traceability._validate_closure_dependencies(control.owner, project, saved, "task")

    control.task_revisions.replan(control.owner, task_b, 1, "B-only revision")
    control.traceability._validate_closure_dependencies(control.owner, project, saved, "task")


def test_archive_accepts_string_artifact_ac_id(refs_fixture):
    fixture = refs_fixture
    _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])], ac_id=True)
    baseline = fixture["control"].k.baseline(fixture["control"].owner, fixture["project"])
    exported = fixture["control"].history.export_archive(fixture["control"].owner, baseline["id"])
    assert Path(exported["path"]).exists()


@pytest.mark.parametrize(
    ("ref_name", "field", "value"),
    [("git_file", "pin_revision_digest", "0" * 64),
     ("source", "source_id", "SRC-MISSING-IN-ARCHIVE")],
)
def test_archive_checks_saved_typed_ref_context(refs_fixture, tmp_path, ref_name, field, value):
    fixture = refs_fixture
    control = fixture["control"]
    decision = _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])])
    _mapping(control, fixture, decision, _leaf_ids(fixture)[0], fixture[ref_name])
    payload, blobs = control.traceability._archive_payload(control.owner, fixture["project"])

    positive = tmp_path / f"{ref_name}-positive.zip"
    _write_traceability_archive(payload, blobs, positive)
    assert inspect_archive(positive)["verified"]

    _rewrite_target_and_rehash(payload, fixture[ref_name]["ref_type"], field, value)
    corrupt = tmp_path / f"{ref_name}-corrupt.zip"
    _write_traceability_archive(payload, blobs, corrupt)
    with pytest.raises(Fault) as error:
        inspect_archive(corrupt)
    assert error.value.code == "invalid_archive"


def test_archive_requires_every_proposal_review_packet_group(refs_fixture, tmp_path):
    fixture = refs_fixture
    control = fixture["control"]
    decision = _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])])
    _mapping(control, fixture, decision, _leaf_ids(fixture)[0], fixture["git_file"])
    payload, blobs = control.traceability._archive_payload(control.owner, fixture["project"])

    def body(row):
        return json.loads(row["body"]) if isinstance(row.get("body"), str) else row["body"]

    mapping_proposal = next(
        row["id"] for row in payload["tables"]["traceability_proposals"]
        if body(row).get("kind") == "mapping"
    )
    payload["tables"]["traceability_records"] = [
        row for row in payload["tables"]["traceability_records"]
        if not (row.get("kind") == "review_packet" and row.get("proposal") == mapping_proposal)
    ]
    corrupt = tmp_path / "mapping-packet-group-absent.zip"
    _write_traceability_archive(payload, blobs, corrupt)
    with pytest.raises(Fault) as error:
        inspect_archive(corrupt)
    assert error.value.code == "invalid_archive"


@pytest.mark.parametrize("section", ["artifacts", "candidates", "tasks"])
def test_archive_derives_and_requires_context_for_each_typed_ref(refs_fixture, tmp_path, section):
    fixture = refs_fixture
    control = fixture["control"]
    decision = _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])])
    _mapping(control, fixture, decision, _leaf_ids(fixture)[0], fixture["candidate"])
    payload, blobs = control.traceability._archive_payload(control.owner, fixture["project"])

    positive = tmp_path / f"{section}-context-positive.zip"
    _write_traceability_archive(payload, blobs, positive)
    assert inspect_archive(positive)["verified"]

    payload["context"][section] = []
    corrupt = tmp_path / f"{section}-context-empty.zip"
    _write_traceability_archive(payload, blobs, corrupt)
    with pytest.raises(Fault) as error:
        inspect_archive(corrupt)
    assert error.value.code == "invalid_archive"


def test_dedicated_archive_requires_exact_candidate_task_history(refs_fixture, tmp_path):
    fixture = refs_fixture
    control = fixture["control"]
    decision = _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])])
    _mapping(control, fixture, decision, _leaf_ids(fixture)[0], fixture["candidate"])
    control.task_revisions.replan(control.owner, fixture["task"], 1, "retain candidate history")
    payload, blobs = control.traceability._archive_payload(control.owner, fixture["project"])

    positive = tmp_path / "candidate-history-positive.zip"
    _write_traceability_archive(payload, blobs, positive)
    assert inspect_archive(positive)["verified"]

    _rewrite_target_and_rehash(payload, "candidate_symbol", "task_revision", 2)
    corrupt = tmp_path / "candidate-history-false-r2.zip"
    _write_traceability_archive(payload, blobs, corrupt)
    with pytest.raises(Fault) as error:
        inspect_archive(corrupt)
    assert error.value.code == "invalid_archive"


def test_standard_archive_requires_exact_candidate_task_history(refs_fixture, tmp_path):
    from daikibo.knowledge_history import inspect_archive as inspect_standard

    fixture = refs_fixture
    control = fixture["control"]
    decision = _decisions(fixture, [(_leaf_ids(fixture)[0], fixture["task"])])
    _mapping(control, fixture, decision, _leaf_ids(fixture)[0], fixture["candidate"])
    control.task_revisions.replan(control.owner, fixture["task"], 1, "retain candidate history")
    baseline = control.k.baseline(control.owner, fixture["project"])
    exported = control.history.export_archive(control.owner, baseline["id"])
    positive = tmp_path / "candidate-history-standard-positive.zip"
    shutil.copy2(exported["path"], positive)
    assert inspect_standard(positive, digest(positive.read_bytes()))["verified"]

    payload, blobs = control.traceability._archive_payload(control.owner, fixture["project"])
    _rewrite_target_and_rehash(payload, "candidate_symbol", "task_revision", 2)
    corrupt = tmp_path / "candidate-history-standard-false-r2.zip"
    _rewrite_standard_from_tables(positive, corrupt, payload)
    with pytest.raises(Fault) as error:
        inspect_standard(corrupt, digest(corrupt.read_bytes()))
    assert error.value.code == "invalid_archive"
