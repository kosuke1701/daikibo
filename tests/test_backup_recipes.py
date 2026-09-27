"""Local backup-artifact contract tests; no live controller or provider is used."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest

from daikibo.backup_artifacts import DirectoryArtifactStore, publish_recipe, validate_recipe
from daikibo.common import Actor, Fault, canonical
from daikibo.control import Control
from daikibo import archive_chunks
from daikibo.operations import file_hash, restore_backup


def _read_owner_pages(control, archive_sha):
    chunks = []
    offset = 0
    while True:
        page = control.blob_read(control.owner, archive_sha, offset=offset, limit=64 * 1024)
        chunks.append(base64.b64decode(page["base64"]))
        if page["next_offset"] is None:
            return b"".join(chunks)
        offset = page["next_offset"]


def _zip_bytes(compression):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "asset.zip"
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            archive.writestr("user-data.txt", b"ordinary user ZIP payload")
        return path.read_bytes()


def test_three_generations_reuse_leaves_owner_ranges_and_restore_without_old_home(full, tmp_path):
    control = full
    project = control.k.create_project(control.owner, "backup recipe")['id']
    regular = control.s.blob_put(bytes((index * 73) % 256 for index in range(2 * 1024 * 1024)))
    user_zip_bytes = _zip_bytes(zipfile.ZIP_DEFLATED)
    legacy_backup_bytes = _zip_bytes(zipfile.ZIP_DEFLATED)
    user_zip = control.s.blob_put(user_zip_bytes)
    legacy_backup_blob = control.s.blob_put(legacy_backup_bytes)
    results = []
    archive_copies = []
    recipe_bytes = {}
    for generation in range(3):
        result = control.ops.backup(control.owner)
        results.append(result)
        archive = Path(result["path"])
        raw = archive.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == result["sha256"]
        assert control.s.blob_get(result["blob"]) == raw
        assert _read_owner_pages(control, result["blob"]) == raw
        assert not (control.s.blobs / result["blob"][:2] / result["blob"][2:]).exists()
        recipe = control.s.backup_recipes / f"{result['blob']}.json"
        assert recipe.is_file()
        recipe_bytes[recipe.name] = recipe.read_bytes()
        copy = tmp_path / f"generation-{generation}.zip"
        shutil.copyfile(archive, copy)
        archive_copies.append(copy)
        with zipfile.ZipFile(archive) as inspected:
            names = set(inspected.namelist())
            assert f"blobs/{result['blob'][:2]}/{result['blob'][2:]}" not in names
            if generation:
                assert f"backup-recipes/{results[generation - 1]['blob']}.json" in names

    assert results[1]["bytes"] < results[0]["bytes"] * 2
    assert results[2]["bytes"] < results[1]["bytes"] * 2
    assert control.s.blob_get(regular) == bytes((index * 73) % 256 for index in range(2 * 1024 * 1024))
    assert control.s.blob_get(user_zip) == user_zip_bytes
    assert control.s.blob_get(legacy_backup_blob) == legacy_backup_bytes
    end_page = control.blob_read(control.owner, results[-1]["blob"], offset=results[-1]["bytes"], limit=1)
    beyond_page = control.blob_read(control.owner, results[-1]["blob"], offset=results[-1]["bytes"] + 17, limit=1)
    assert end_page["base64"] == beyond_page["base64"] == "" and end_page["next_offset"] is None
    for kwargs in ({"offset": True, "limit": 1}, {"offset": 0, "limit": True}, {"offset": 0, "limit": 1048577}):
        with pytest.raises(Fault) as invalid_range:
            control.blob_read(control.owner, results[-1]["blob"], **kwargs)
        assert invalid_range.value.code == "invalid_range"
    with pytest.raises(Fault) as denied:
        control.blob_read(Actor("agent", "agent", project=project), results[0]["blob"], project=project)
    assert denied.value.code == "forbidden"
    with pytest.raises(Fault) as no_path:
        control.s.blob_path(results[0]["blob"])
    assert no_path.value.code == "artifact_requires_stream"

    old_home = control.s.home
    control.close()
    shutil.rmtree(old_home)
    restored_home = tmp_path / "restored-fresh-home"
    restore_backup(archive_copies[-1], restored_home, results[-1]["sha256"])
    restored = Control(restored_home, mode="validation", start_workers=False)
    try:
        for result, archive in zip(results, archive_copies):
            assert restored.s.blob_get(result["blob"]) == archive.read_bytes()
        for name in (f"{results[0]['blob']}.json", f"{results[1]['blob']}.json"):
            assert (restored.s.backup_recipes / name).read_bytes() == recipe_bytes[name]
        assert restored.s.blob_get(regular) == bytes((index * 73) % 256 for index in range(2 * 1024 * 1024))
        assert restored.s.blob_get(user_zip) == user_zip_bytes
        assert restored.s.blob_get(legacy_backup_blob) == legacy_backup_bytes
    finally:
        restored.close()


def test_recipe_publish_failure_retains_export_and_previous_recipe(full, monkeypatch):
    control = full
    first = control.ops.backup(control.owner)
    before = {path.name: path.read_bytes() for path in control.s.backup_recipes.glob("*.json")}
    import daikibo.operations as operations

    def fail_publish(*args, **kwargs):
        raise OSError("simulated recipe disk failure")

    monkeypatch.setattr(operations, "publish_recipe", fail_publish)
    with pytest.raises(OSError, match="simulated recipe disk failure"):
        control.ops.backup(control.owner)
    after_exports = sorted(control.s.home.joinpath("exports").glob("*.zip"))
    assert len(after_exports) == 2
    failed = next(path for path in after_exports if file_hash(path) != first["sha256"])
    failed_sha = file_hash(failed)
    assert not (control.s.backup_recipes / f"{failed_sha}.json").exists()
    assert {path.name: path.read_bytes() for path in control.s.backup_recipes.glob("*.json")} == before
    assert not (control.s.blobs / failed_sha[:2] / failed_sha[2:]).exists()


def test_recipe_publication_is_create_only_and_chunked_consumers_read_aliases(full, tmp_path):
    control = full
    result = control.ops.backup(control.owner)
    recipe_path = control.s.backup_recipes / f"{result['blob']}.json"
    original = recipe_path.read_bytes()
    conflicting = json.loads(original)
    conflicting["backup_id"] = "concurrent-writer"
    with pytest.raises(Fault):
        publish_recipe(control.s, conflicting)
    assert recipe_path.read_bytes() == original

    physical = control.s.blobs / result["blob"][:2] / result["blob"][2:]
    physical.parent.mkdir(parents=True, exist_ok=True)
    physical.write_bytes(b"corrupt physical shadow")
    with pytest.raises(Fault):
        control.s.blob_get(result["blob"])
    assert recipe_path.read_bytes() == original
    physical.unlink()

    # Knowledge-history chunking is an existing blob_path consumer.  A
    # recipe-backed backup hash must remain usable without materializing the
    # export as a new physical leaf.
    raw = Path(result["path"]).read_bytes()
    split = archive_chunks.Builder(control.s, chunk_bytes=64 * 1024).raw(result["blob"])
    rebuilt = b"".join(control.s.blob_get(item["sha256"]) for item in split["chunks"])
    assert split["sha256"] == result["blob"] and split["bytes"] == len(raw) and rebuilt == raw

    orphan = control.s.blob_put(b"orphan eligible for collection")
    orphan_path = control.s.blobs / orphan[:2] / orphan[2:]
    old = 1
    os.utime(orphan_path, (old, old))
    damaged = json.loads(original)
    damaged["segments"][0]["blob"] = "0" * 64
    recipe_path.write_bytes(canonical(damaged))
    with pytest.raises(Fault):
        control.ops.garbage_collect(control.owner, dry_run=False)
    assert orphan_path.is_file(), "GC must not unlink after a recipe preflight failure"
    recipe_path.write_bytes(original)


def test_recipe_missing_leaf_and_alias_cycle_fail_before_gc(full, tmp_path):
    control = full
    result = control.ops.backup(control.owner)
    recipe_path = control.s.backup_recipes / f"{result['blob']}.json"
    recipe = json.loads(recipe_path.read_text())
    recipe["segments"][0]["blob"] = "0" * 64
    recipe_path.write_bytes(canonical(recipe))
    with pytest.raises(Fault):
        validate_recipe(control.s, result["blob"])
    with pytest.raises(Fault):
        control.ops.garbage_collect(control.owner, dry_run=True)

    alias_root = tmp_path / "alias-store"
    store = DirectoryArtifactStore(alias_root)
    alias_sha = "1" * 64
    alias = {"format": "daikibo.backup-byte-recipe.v1", "artifact_sha256": alias_sha,
             "artifact_bytes": 1, "backup_id": "cycle", "segments":[{"blob":alias_sha,"offset":0,"bytes":1}]}
    recipe_file = store.backup_recipes / f"{alias_sha}.json"
    recipe_file.write_bytes(canonical(alias))
    with pytest.raises(Fault):
        validate_recipe(store, alias_sha)
