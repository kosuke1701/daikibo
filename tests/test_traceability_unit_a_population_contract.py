"""Real archive negatives for the Unit A population contract.

The mutations below rewrite the payload and its outer manifest.  They do not
rely on the old archive SHA, so a positive result can only come from the
row/population/CAS validator rather than stale ZIP metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.control import Control
from daikibo.knowledge_history import inspect_archive as inspect_standard_archive
from daikibo.traceability import inspect_archive as inspect_traceability_archive


def _commit(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "trace-contract", "GIT_AUTHOR_EMAIL": "trace@invalid",
           "GIT_COMMITTER_NAME": "trace-contract", "GIT_COMMITTER_EMAIL": "trace@invalid"}
    subprocess.run(["git", "-c", "user.name=trace-contract", "-c", "user.email=trace@invalid",
                    "commit", "-qm", "fixture"], cwd=root, check=True, env=env)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _rewrite(source: Path, target: Path, mutate, *, remove_blob: str | None = None) -> str:
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    payload = json.loads(files["traceability.json"])
    manifest = json.loads(files["manifest.json"])
    mutate(payload, manifest)
    body = canonical(payload)
    manifest["payload"] = {"name": "traceability.json", "bytes": len(body), "sha256": digest(body)}
    if remove_blob:
        payload["blob_manifest"] = [item for item in payload["blob_manifest"] if item["sha256"] != remove_blob]
        # The payload is part of the mutation, so refresh its digest once more.
        body = canonical(payload)
        manifest["payload"] = {"name": "traceability.json", "bytes": len(body), "sha256": digest(body)}
        files.pop(f"blobs/{remove_blob}", None)
        manifest["blobs"] = [item for item in manifest["blobs"] if item["sha256"] != remove_blob]
    files["traceability.json"] = body
    files["manifest.json"] = canonical(manifest)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _complete_rows(payload):
    tables = payload["tables"]
    revision = next(row for row in tables["traceability_revisions"] if row["status"] in {"ready", "active", "superseded"})
    proposal = next(row for row in tables["traceability_proposals"] if row["id"] == next(
        row["proposal"] for row in tables["traceability_records"]
        if row.get("revision") == revision["id"] and row["kind"] == "extracted"))
    items = [row for row in tables["traceability_items"] if row["revision"] == revision["id"]]
    return tables, revision, proposal, items


def _refresh_complete(payload, revision, proposal, *, refresh_items=True):
    """Refresh immutable digest links after an intentional row mutation."""
    tables = payload["tables"]
    items = [row for row in tables["traceability_items"] if row["revision"] == revision["id"]]
    if refresh_items:
        for row in items:
            body = row["body"]
            row["digest"] = digest(body)
    ordered = sorted(items, key=lambda row: (row["ordinal"], row["id"]))
    leaves = [row for row in ordered if row.get("leaf")]
    revision_body = revision["body"]
    revision_body["item_digest"] = _sequence_digest_for_test(row["digest"] for row in ordered)
    revision_body["leaf_ids_digest"] = digest(sorted(row["id"] for row in leaves))
    revision_body["counts"]["leaf"] = len(leaves)
    revision_body["counts"]["unknown"] = sum(row["status"] == "unknown" for row in leaves)
    revision["digest"] = digest(revision_body)
    revision["population_digest"] = digest({"proposal": proposal["digest"], "items": revision_body["item_digest"],
                                             "leaf_count": len(leaves), "unknown_count": revision_body["counts"]["unknown"]})
    result = proposal.get("result") or {}
    result.update({"revision": revision["id"], "revision_digest": revision["digest"],
                   "population_digest": revision["population_digest"], "status": "ready"})
    proposal["result"] = result
    for record in tables["traceability_records"]:
        if record.get("revision") == revision["id"] and record.get("kind") == "extracted":
            record["body"] = dict(result)
            record["digest"] = digest(record["body"])


def _sequence_digest_for_test(values):
    import hashlib as _hashlib
    h = _hashlib.sha256()
    for value in values:
        h.update(value.encode("ascii")); h.update(b"\n")
    return h.hexdigest()


def _rewrite_standard_membership(source: Path, target: Path) -> str:
    """Rewrite one standard-v10 JSONL stream with a consistent AST mutation."""
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    snapshot = json.loads(files["snapshot.json"])
    stream = b"".join(files["objects/" + chunk["sha256"]] for chunk in snapshot["records"]["chunks"])
    records = [json.loads(line) for line in stream.splitlines()]
    item_records = [record for record in records if record.get("section") == "traceability_items"]
    group_record = next(record for record in item_records if record["row"].get("item_kind") == "symbol")
    group_record["row"]["body"]["atom_ids"] = []
    group_record["row"]["digest"] = digest(group_record["row"]["body"])
    revision_record = next(record for record in records if record.get("section") == "traceability_revisions")
    proposal_record = next(record for record in records if record.get("section") == "traceability_proposals")
    revision = revision_record["row"]; proposal = proposal_record["row"]
    items = [record["row"] for record in item_records if record["row"].get("revision") == revision["id"]]
    ordered = sorted(items, key=lambda row: (row["ordinal"], row["id"]))
    item_digest = _sequence_digest_for_test(row["digest"] for row in ordered)
    leaves = [row for row in ordered if row.get("leaf")]
    revision["body"]["item_digest"] = item_digest
    revision["body"]["leaf_ids_digest"] = digest(sorted(row["id"] for row in leaves))
    revision["digest"] = digest(revision["body"])
    revision["population_digest"] = digest({"proposal": proposal["digest"], "items": item_digest,
                                             "leaf_count": len(leaves),
                                             "unknown_count": sum(row["status"] == "unknown" for row in leaves)})
    proposal["result"].update({"revision": revision["id"], "revision_digest": revision["digest"],
                                "population_digest": revision["population_digest"], "status": "ready"})
    extracted = next(record for record in records if record.get("section") == "traceability_records"
                     and record["row"].get("kind") == "extracted")
    extracted["row"]["body"] = dict(proposal["result"])
    extracted["row"]["digest"] = digest(extracted["row"]["body"])
    replacement = b"".join(canonical(record) + b"\n" for record in records)
    old = snapshot["records"]["chunks"][0]["sha256"]
    new = hashlib.sha256(replacement).hexdigest()
    snapshot["records"]["chunks"] = [{"bytes": len(replacement), "sha256": new}]
    snapshot["records"]["bytes"] = len(replacement)
    snapshot["records"]["sha256"] = new
    snapshot["objects"].pop(old, None); snapshot["objects"][new] = {"bytes": len(replacement)}
    files.pop("objects/" + old); files["objects/" + new] = replacement
    snapshot_raw = canonical(snapshot)
    header = json.loads(files["manifest.json"])
    header["snapshot"] = {"bytes": len(snapshot_raw), "sha256": digest(snapshot_raw)}
    files["snapshot.json"] = snapshot_raw; files["manifest.json"] = canonical(header)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items(): archive.writestr(name, data)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def test_complete_population_negatives_recompute_outer_manifest(full, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "a.py").write_text("class A:\n    def f(self):\n        return 1\n\ndef g():\n    return 2\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "population negatives")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    full.traceability.extract(full.owner, proposal["id"])
    original = Path(full.traceability.export(full.owner, project)["path"])

    def run(name, mutate, expected="invalid_archive", remove_blob=None):
        target = tmp_path / f"{name}.zip"
        observed = _rewrite(original, target, mutate, remove_blob=remove_blob)
        with pytest.raises(Fault) as error:
            inspect_traceability_archive(target, observed)
        assert error.value.code == expected

    def missing_leaf(payload, _manifest):
        tables, revision, _proposal, items = _complete_rows(payload)
        victim = next(row for row in items if row["leaf"])
        tables["traceability_items"].remove(victim)

    run("missing-leaf", missing_leaf)

    def duplicate_ordinal(payload, _manifest):
        _tables, _revision, _proposal, items = _complete_rows(payload)
        items[1]["ordinal"] = items[0]["ordinal"]

    run("duplicate-ordinal", duplicate_ordinal)

    def range_change(payload, _manifest):
        _tables, _revision, _proposal, items = _complete_rows(payload)
        atom = next(row for row in items if row["item_kind"] == "atom")
        atom["start_byte"] += 1

    run("range-change", range_change)

    def count_mismatch(payload, _manifest):
        _tables, revision, proposal, _items = _complete_rows(payload)
        revision["body"]["counts"]["items"] += 1
        _refresh_complete(payload, revision, proposal, refresh_items=False)

    run("count-mismatch", count_mismatch)

    def foreign_group(payload, _manifest):
        _tables, revision, proposal, items = _complete_rows(payload)
        group = next(row for row in items if row["item_kind"] == "symbol")
        group["body"]["atom_ids"] = ["ITEM-foreign-atom"]
        _refresh_complete(payload, revision, proposal)

    run("foreign-group", foreign_group)

    def bad_inventory_oid(payload, _manifest):
        _tables, revision, proposal, _items = _complete_rows(payload)
        entry = next(entry for entry in revision["body"]["inventory"] if entry["type"] == "blob")
        entry["blob_oid"] = "0" * len(entry["blob_oid"])
        _refresh_complete(payload, revision, proposal, refresh_items=False)

    run("bad-git-object-identity", bad_inventory_oid)

    def partial_ready(payload, _manifest):
        _tables, _revision, proposal, _items = _complete_rows(payload)
        proposal["status"] = "staging"
        proposal["result"] = {"status": "staging", "proposal": proposal["id"]}

    run("staging-ready-inconsistency", partial_ready)

    blob = next(item["sha256"] for item in _complete_rows(json.loads((zipfile.ZipFile(original).read("traceability.json"))))[1]["body"]["inventory"]
                if item["type"] == "blob")
    run("missing-cas", lambda _payload, _manifest: None, remove_blob=blob)


def test_symbol_membership_projection_rejects_empty_partial_swap_owner_and_parent(full, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "a.py").write_text(
        "@decorator\n"
        "def alpha(x):\n"
        "    return x + 1\n\n"
        "class C:\n"
        "    def child(self):\n"
        "        return alpha(1)\n\n"
        "def residual():\n"
        "    return 3\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "membership negatives")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    full.traceability.extract(full.owner, proposal["id"])
    original = Path(full.traceability.export(full.owner, project)["path"])

    def run(name, mutate):
        target = tmp_path / f"membership-{name}.zip"
        observed = _rewrite(original, target, mutate)
        with pytest.raises(Fault) as error:
            inspect_traceability_archive(target, observed)
        assert error.value.code == "invalid_archive"

    def mutate_group(payload, change):
        _tables, revision, proposal_row, items = _complete_rows(payload)
        groups = [row for row in items if row["item_kind"] == "symbol"]
        atoms = [row for row in items if row["item_kind"] == "atom"]
        change(groups, atoms)
        _refresh_complete(payload, revision, proposal_row)

    run("empty", lambda payload, _manifest: mutate_group(payload, lambda groups, atoms: groups[0]["body"].update(atom_ids=[])))
    run("partial", lambda payload, _manifest: mutate_group(payload, lambda groups, atoms: groups[0]["body"].update(atom_ids=groups[0]["body"]["atom_ids"][:-1])))
    run("same-file-swap", lambda payload, _manifest: mutate_group(
        payload, lambda groups, atoms: groups[0]["body"].update(
            atom_ids=[atom["id"] for atom in atoms
                      if atom["id"] not in groups[0]["body"]["atom_ids"]][:1])))

    def owner_reverse(groups, atoms):
        atom = next(atom for atom in atoms if atom["body"].get("owner_symbol") == groups[0]["id"])
        atom["body"]["owner_symbol"] = None

    run("owner-reverse", lambda payload, _manifest: mutate_group(payload, owner_reverse))

    def parent_exclusion(groups, atoms):
        parent = next(group for group in groups if group["body"].get("parent") is None and group["body"].get("atom_ids"))
        parent["body"]["atom_ids"] = parent["body"]["atom_ids"][:-1]

    run("parent-child-exclusion", lambda payload, _manifest: mutate_group(payload, parent_exclusion))


def test_same_name_nested_projection_is_stable_and_rejects_membership_swap(full, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "same.py").write_text(
        "@deco\n"
        "def overloaded(x):\n"
        "    return x\n\n"
        "@deco\n"
        "def overloaded(x, y):\n"
        "    return x + y\n\n"
        "class K:\n"
        "    def overloaded(self):\n"
        "        return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "same name groups")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    full.traceability.extract(full.owner, proposal["id"])
    original = Path(full.traceability.export(full.owner, project)["path"])

    def mutate(payload, _manifest):
        _tables, revision, proposal_row, items = _complete_rows(payload)
        groups = [row for row in items if row["item_kind"] == "symbol" and row["body"].get("qualified_name") == "overloaded"]
        assert len(groups) == 2
        groups[0]["body"]["atom_ids"], groups[1]["body"]["atom_ids"] = groups[1]["body"]["atom_ids"], groups[0]["body"]["atom_ids"]
        _refresh_complete(payload, revision, proposal_row)

    target = tmp_path / "same-name-swap.zip"
    observed = _rewrite(original, target, mutate)
    with pytest.raises(Fault) as error:
        inspect_traceability_archive(target, observed)
    assert error.value.code == "invalid_archive"


def test_standard_v10_archive_uses_same_complete_validator(full, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "a.py").write_text("def a():\n    return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "standard population")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    revision = full.traceability.extract(full.owner, proposal["id"])["revision"]
    baseline = full.history.create(full.owner, project, layout="chunked")
    exported = full.history.export_archive(full.owner, baseline["id"])
    report = inspect_standard_archive(exported["path"], exported["sha256"])
    assert report["verified"] and report["format"] == "daikibo.knowledge-archive.v12"
    assert report["counts"]["traceability_revisions"] == 1
    assert report["counts"]["traceability_items"] > 0
    # The standard reader has no controller or source repository available at
    # this point; all complete-population bytes came from trace raw closure.
    full.close(); shutil.rmtree(repo)
    assert inspect_standard_archive(exported["path"], exported["sha256"])["verified"]


def test_standard_v10_missing_leaf_rebuilds_all_outer_hashes_but_is_rejected(full, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "a.py").write_text("def a():\n    return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "standard negative")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    full.traceability.extract(full.owner, proposal["id"])
    baseline = full.history.create(full.owner, project, layout="chunked")
    exported = full.history.export_archive(full.owner, baseline["id"])
    source = Path(exported["path"]); target = tmp_path / "standard-missing-leaf.zip"
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    snapshot = json.loads(files["snapshot.json"])
    stream = b"".join(files["objects/" + chunk["sha256"]] for chunk in snapshot["records"]["chunks"])
    lines = stream.splitlines(keepends=True); removed = False; kept = []
    for line in lines:
        record = json.loads(line)
        if (not removed and record.get("section") == "traceability_items"
                and record.get("row", {}).get("leaf")):
            removed = True
            snapshot["records"]["counts"]["traceability_items"] -= 1
            continue
        kept.append(line)
    assert removed
    replacement = b"".join(kept); old = snapshot["records"]["chunks"][0]["sha256"]
    new = hashlib.sha256(replacement).hexdigest()
    snapshot["records"]["chunks"] = [{"bytes": len(replacement), "sha256": new}]
    snapshot["records"]["bytes"] = len(replacement)
    snapshot["records"]["sha256"] = new
    snapshot["objects"].pop(old, None)
    snapshot["objects"][new] = {"bytes": len(replacement)}
    files.pop("objects/" + old)
    files["objects/" + new] = replacement
    snapshot_raw = canonical(snapshot)
    header = json.loads(files["manifest.json"])
    header["snapshot"] = {"bytes": len(snapshot_raw), "sha256": digest(snapshot_raw)}
    files["snapshot.json"] = snapshot_raw
    files["manifest.json"] = canonical(header)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    observed = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(Fault) as error:
        inspect_standard_archive(target, observed)
    assert error.value.code == "invalid_archive"


def test_standard_v10_group_membership_rebuilds_outer_hashes_but_is_rejected(full, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "a.py").write_text("class C:\n    def child(self):\n        return 1\n")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "standard membership")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    full.traceability.extract(full.owner, proposal["id"])
    baseline = full.history.create(full.owner, project, layout="chunked")
    exported = full.history.export_archive(full.owner, baseline["id"])
    target = tmp_path / "standard-membership.zip"
    observed = _rewrite_standard_membership(Path(exported["path"]), target)
    with pytest.raises(Fault) as error:
        inspect_standard_archive(target, observed)
    assert error.value.code == "invalid_archive"


def test_document_missing_leaf_is_rejected_after_outer_manifest_rebuild(full, tmp_path):
    project = full.k.create_project(full.owner, "document negative")["id"]
    source = full.k.source(full.owner, project, "first line\nsecond line\n")["id"]
    proposal = full.traceability.propose(full.owner, project, kind="document", source=source)
    full.traceability.extract(full.owner, proposal["id"])
    original = Path(full.traceability.export(full.owner, project)["path"])

    def remove_line(payload, _manifest):
        tables, _revision, _proposal, items = _complete_rows(payload)
        tables["traceability_items"].remove(next(row for row in items if row["item_kind"] == "line"))

    target = tmp_path / "document-missing-line.zip"
    observed = _rewrite(original, target, remove_line)
    with pytest.raises(Fault) as error:
        inspect_traceability_archive(target, observed)
    assert error.value.code == "invalid_archive"


def test_complete_document_empty_and_unknown_archives_keep_explicit_population(full, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "empty.py").write_bytes(b"")
    (repo / "bad.py").write_bytes(b"\xff\xfe")
    commit = _commit(repo)
    project = full.k.create_project(full.owner, "empty unknown archives")["id"]
    registered = full.sn.register(full.owner, project, "fixture", str(repo))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
        scope={"repository": registered, "commit": commit})
    revision = full.traceability.extract(full.owner, proposal["id"])["revision"]
    exported = full.traceability.export(full.owner, project)
    report = inspect_traceability_archive(exported["path"], exported["sha256"])
    assert report["verified"] and report["counts"]["traceability_revisions"] == 1
    rows = full.traceability.items(full.owner, revision, limit=100)["items"]
    assert any(row["path"] == "empty.py" and row["body"].get("empty") and row["leaf"] for row in rows)
    assert any(row["path"] == "bad.py" and row["status"] == "unknown" and row["leaf"] for row in rows)
