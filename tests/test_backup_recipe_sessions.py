"""Focused verified-session and encoded-capacity regressions."""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

import daikibo.backup_artifacts as artifacts
from daikibo.backup_artifacts import (
    DirectoryArtifactStore,
    open_artifact_session,
    publish_recipe,
    recipe_from_stored_zip,
)
from daikibo.common import Fault, canonical


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _recipe(store, parts: list[bytes], backup_id: str = "session"):
    refs = [store.blob_put(part) for part in parts]
    payload = b"".join(parts)
    return refs, {
        "format": artifacts.RECIPE_FORMAT,
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "artifact_bytes": len(payload),
        "backup_id": backup_id,
        "segments": [
            {"blob": ref, "offset": 0, "bytes": len(part)}
            for ref, part in zip(refs, parts)
        ],
    }, payload


def test_store_owned_session_reassembles_once_and_shares_range_accounting(tmp_path):
    store = DirectoryArtifactStore(tmp_path / "session-store")
    refs, recipe, expected = _recipe(store, [b"A" * 100, b"B" * 150])
    publish_recipe(store, recipe)

    with open_artifact_session(store, recipe["artifact_sha256"]) as session:
        assert session.size == len(expected)
        assert session.read_range(90, 80) == expected[90:170]
        assert session.read_range(len(expected), 64) == b""
        stats = session.stats

    recipe_bytes = (store.backup_recipes / f'{recipe["artifact_sha256"]}.json').read_bytes()
    assert stats["validation_bytes"] == len(recipe_bytes) + sum(map(len, (b"A" * 100, b"B" * 150))) + len(expected)
    assert stats["range_bytes"] == 80
    assert stats["recipe_bytes_sha256"] == hashlib.sha256(recipe_bytes).hexdigest()
    assert store._get_artifact_manager().cache_supported

    with open_artifact_session(store, recipe["artifact_sha256"]) as cached:
        assert cached.read_range(0, 1) == b"A"
        if store._get_artifact_manager().cache_supported:
            assert cached.stats["cache_hit"] is True
    store.close()


def test_session_revalidates_changed_offset_and_keeps_recipe_bytes_binding(tmp_path):
    store = DirectoryArtifactStore(tmp_path / "offset-store")
    refs, recipe, expected = _recipe(store, [b"AC", b"BD"])
    publish_recipe(store, recipe)
    assert recipe["artifact_sha256"] == hashlib.sha256(b"ACBD").hexdigest()

    with open_artifact_session(store, recipe["artifact_sha256"]) as session:
        assert session.read_range(0, 4) == expected
    path = store.backup_recipes / f'{recipe["artifact_sha256"]}.json'
    original = path.read_bytes()
    damaged = json.loads(original)
    damaged["segments"][1]["offset"] = 1
    path.write_bytes(canonical(damaged))
    try:
        with pytest.raises(Fault) as failure:
            with open_artifact_session(store, recipe["artifact_sha256"]) as session:
                session.read_range(0, 1)
        assert failure.value.code in {"integrity_error", "invalid_recipe"}
    finally:
        path.write_bytes(original)
    with open_artifact_session(store, recipe["artifact_sha256"]) as repaired:
        assert repaired.read_range(0, 4) == expected
        assert repaired.stats["recipe_bytes_sha256"] == hashlib.sha256(original).hexdigest()
    store.close()


def test_unsupported_change_monitor_falls_back_to_full_validation(tmp_path):
    store = DirectoryArtifactStore(tmp_path / "fallback-store")
    _, recipe, expected = _recipe(store, [b"fallback"])
    publish_recipe(store, recipe)
    manager = store._get_artifact_manager()
    manager._monitor.close()
    manager.cache_supported = False

    with open_artifact_session(store, recipe["artifact_sha256"]) as first:
        assert first.read_range(0, len(expected)) == expected
        first_stats = first.stats
    with open_artifact_session(store, recipe["artifact_sha256"]) as second:
        assert second.read_range(0, len(expected)) == expected
        second_stats = second.stats
    assert first_stats["cache_hit"] is False and second_stats["cache_hit"] is False
    assert first_stats["validation_bytes"] == second_stats["validation_bytes"]
    store.close()


def _small_stored_backup(directory: Path, payload_size: int) -> tuple[Path, DirectoryArtifactStore, dict[str, list[dict[str, Any]]]]:
    directory.mkdir(parents=True, exist_ok=True)
    store = DirectoryArtifactStore(directory / "store")
    payload = bytes(range(payload_size))
    manifest = {
        "format": "daikibo.backup.v1",
        "schema": 13,
        "created": 0,
        "audit": [],
        "files": {"payload.bin": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": payload_size}},
        "confidential": True,
        "contains_credentials": True,
    }
    manifest_bytes = canonical(manifest)
    payload_blob = store.blob_put(payload)
    manifest_blob = store.blob_put(manifest_bytes)
    path = directory / f"backup-{payload_size}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("payload.bin", payload)
        archive.writestr("manifest.json", manifest_bytes)
    mapping = {
        "payload.bin": [{"blob": payload_blob, "offset": 0, "bytes": payload_size}],
        "manifest.json": [{"blob": manifest_blob, "offset": 0, "bytes": len(manifest_bytes)}],
    }
    return path, store, mapping


def test_encoded_zip_framing_is_separate_from_expanded_payload_limit(tmp_path, monkeypatch):
    payload_limit = 5
    monkeypatch.setattr(artifacts, "MAX_LOGICAL_BACKUP_BYTES", payload_limit)
    monkeypatch.setattr(artifacts, "MAX_BACKUP_MANIFEST_BYTES", 4096)
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 4096)

    valid_zip, store, mapping = _small_stored_backup(tmp_path / "valid", payload_limit)
    valid_recipe = recipe_from_stored_zip(valid_zip, mapping, store, "capacity-valid")
    assert valid_zip.stat().st_size > payload_limit
    publish_recipe(store, valid_recipe)
    with open_artifact_session(store, valid_recipe["artifact_sha256"]) as session:
        assert session.size == valid_zip.stat().st_size
    store.close()

    over_zip, over_store, over_mapping = _small_stored_backup(tmp_path / "over", payload_limit + 1)
    with pytest.raises(Fault) as failure:
        recipe_from_stored_zip(over_zip, over_mapping, over_store, "capacity-over")
    assert failure.value.code == "backup_capacity"
    over_store.close()


def test_failed_validation_owns_every_descriptor_and_close_is_idempotent(tmp_path, monkeypatch):
    store = DirectoryArtifactStore(tmp_path / "fd-store")
    first, second = store.blob_put(b"A"), store.blob_put(b"B")
    artifact_sha = hashlib.sha256(b"AB").hexdigest()
    recipe = {
        "format": artifacts.RECIPE_FORMAT,
        "artifact_sha256": artifact_sha,
        "artifact_bytes": 2,
        "backup_id": "fd-lifetime",
        "segments": [
            {"blob": first, "offset": 0, "bytes": 1},
            {"blob": second, "offset": 0, "bytes": 1},
        ],
    }
    publish_recipe(store, recipe)
    manager = store._get_artifact_manager()
    before = _fd_count()
    path = store.backup_recipes / f"{artifact_sha}.json"
    original = path.read_bytes()
    reordered = json.loads(original)
    reordered["segments"].reverse()
    path.write_bytes(canonical(reordered))
    try:
        for _ in range(5):
            with pytest.raises(Fault):
                with open_artifact_session(store, artifact_sha):
                    pass
        assert _fd_count() <= before + 1
    finally:
        path.write_bytes(original)

    # A newly opened leaf is registered before hashing, so a bad physical
    # digest also closes both the recipe and leaf handles.
    second_path = store.blobs / second[:2] / second[2:]
    second_path.write_bytes(b"C")
    try:
        for _ in range(3):
            with pytest.raises(Fault):
                with open_artifact_session(store, artifact_sha):
                    pass
        assert _fd_count() <= before + 1
    finally:
        second_path.write_bytes(b"B")

    # Identity failure after all handles are open follows the same owner.
    original_check = artifacts._OpenBlob.check_stable

    def fail_identity(self):
        raise Fault("integrity_error", "injected identity failure")

    monkeypatch.setattr(artifacts._OpenBlob, "check_stable", fail_identity)
    try:
        with pytest.raises(Fault, match="injected identity"):
            with open_artifact_session(store, artifact_sha):
                pass
        assert _fd_count() <= before + 1
    finally:
        monkeypatch.setattr(artifacts._OpenBlob, "check_stable", original_check)

    # A generation change after validation but before cache publication must
    # close the returned entry instead of leaving an unowned set of fds.
    original_open = manager._open_entry

    def open_then_bump(sha):
        entry = original_open(sha)
        manager.bump()
        return entry

    monkeypatch.setattr(manager, "_open_entry", open_then_bump)
    with pytest.raises(Fault, match="namespace changed"):
        with open_artifact_session(store, artifact_sha):
            pass
    assert _fd_count() <= before + 1
    manager.close()
    manager.close()
    store.close()


def test_corrupt_physical_hash_rejection_does_not_accumulate_descriptors(tmp_path):
    store = DirectoryArtifactStore(tmp_path / "physical-fd-store")
    blob = store.blob_put(b"physical")
    manager = store._get_artifact_manager()
    before = _fd_count()
    path = store.blobs / blob[:2] / blob[2:]
    path.write_bytes(b"changed")
    try:
        for _ in range(5):
            with pytest.raises(Fault):
                with open_artifact_session(store, blob):
                    pass
        assert _fd_count() <= before + 1
    finally:
        manager.close()
        store.close()
