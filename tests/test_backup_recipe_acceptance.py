"""Independent acceptance fixtures for the flat backup-recipe implementation.

These tests use the public local system.backup job/result/event route where
an operational backup is involved. They are finite local evidence only: they
do not exercise live providers, a controller cutover, or climate/R1 acceptance.
The test worktree intentionally contains no production-source changes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest
from _child_import import child_env, child_import_guard

from daikibo.common import Actor, Fault, canonical
from daikibo.control import Control
from daikibo.operations import file_hash, restore_backup
from daikibo.backup_artifacts import validate_recipe


def _child_env(**updates: str) -> dict[str, str]:
    return child_env(**updates)


def _high_entropy(size: int, seed: bytes = b"backup-recipe-acceptance") -> bytes:
    """Deterministic incompressible-ish bytes, without a random test result."""
    blocks = []
    total = 0
    index = 0
    while total < size:
        block = hashlib.sha256(seed + index.to_bytes(8, "big")).digest()
        blocks.append(block)
        total += len(block)
        index += 1
    return b"".join(blocks)[:size]


def _read_owner_pages(control, archive_sha: str, *, limit: int = 64 * 1024) -> bytes:
    chunks = []
    offset = 0
    while True:
        page = control.blob_read(control.owner, archive_sha, offset=offset, limit=limit)
        chunks.append(base64.b64decode(page["base64"]))
        if page["next_offset"] is None:
            return b"".join(chunks)
        offset = page["next_offset"]


def _snapshot_files(root: Path, home: Path) -> dict[str, dict[str, int | str]]:
    """Record every regular file in a content namespace before fault injection."""
    if not root.exists():
        return {}
    return {
        path.relative_to(home).as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": file_hash(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _snapshot_inventory(control) -> dict[str, dict[str, dict[str, int | str]]]:
    """Capture the pre-failure byte inventory without including mutable SQLite state."""
    home = control.s.home
    return {
        "blobs": _snapshot_files(control.s.blobs, home),
        "backup_recipes": _snapshot_files(control.s.backup_recipes, home),
        "exports": _snapshot_files(home / "exports", home),
    }


def _assert_inventory_unchanged(control, snapshot):
    home = control.s.home
    for namespace in snapshot.values():
        for relative, expected in namespace.items():
            path = home / relative
            assert path.is_file() and not path.is_symlink(), relative
            assert path.stat().st_size == expected["bytes"], relative
            assert file_hash(path) == expected["sha256"], relative


def _read_recovery_file(control, run: str, repo: str, path: str, expected_digest: str) -> tuple[str, int]:
    """Read a retained regular file in bounded pages and return hash/size."""
    hasher = hashlib.sha256()
    offset = 0
    total = None
    while True:
        page = control.invoke(control.owner, "run.recovery_read", {
            "run": run,
            "repo": repo,
            "path": path,
            "expected_digest": expected_digest,
            "offset": offset,
            "limit": 1024 * 1024,
        })
        data = base64.b64decode(page["base64"])
        hasher.update(data)
        total = page["total_bytes"]
        if page["next_offset"] is None:
            return hasher.hexdigest(), total
        assert page["next_offset"] > offset
        offset = page["next_offset"]


def _run_system_backup(control, request_id: str):
    """Submit and dispatch the real public system.backup job route."""
    queued = control.request(None, {"id": request_id, "method": "system.backup", "params": {}})
    assert queued["status"] == "queued" and queued["project"] is None
    row = control.s.one("SELECT * FROM jobs WHERE id=?", (queued["id"],), True)
    assert row["kind"] == "ops.backup" and row["status"] == "queued"
    outcome = control.jobs.run_one(row)
    state = control.jobs.get(control.owner, queued["id"])
    assert outcome["status"] == state["status"] == "succeeded"
    assert state["result"] and state["result"]["blob"] == state["result"]["sha256"]
    events = control.s.all("SELECT kind,body FROM events ORDER BY seq")
    queued_events = [json.loads(e["body"]) for e in events if e["kind"] == "job_queued"]
    finished_events = [json.loads(e["body"]) for e in events if e["kind"] == "job_attempt_finished"]
    assert any(e.get("job") == queued["id"] and e.get("kind") == "ops.backup" for e in queued_events)
    assert any(e.get("job") == queued["id"] and e.get("status") == "succeeded" for e in finished_events)
    return state["result"], state


def _legacy_backup_v1(tmp_path: Path) -> tuple[bytes, dict, str, bytes]:
    """Create a real old-shape backup.v1 archive from an isolated control.

    The first operational backup has a valid state database and manifest but no
    prior recipe members. A known blob makes the restore check cover both the
    state database and a content-addressed payload. Re-encoding that exact
    member set as DEFLATED models a historical backup.v1 transport while
    retaining the old manifest/files contract. It is deliberately kept
    separate from an arbitrary user ZIP.
    """
    source_home = tmp_path / "legacy-source"
    source = Control(source_home, mode="validation", start_workers=False)
    source.owner = source.sec.authenticate(Path(source.sec.bootstrap()).read_text())
    try:
        payload = b"legacy backup.v1 payload"
        payload_blob = source.s.blob_put(payload)
        source_result = source.ops.backup(source.owner)
        source_archive = Path(source_result["path"])
        with zipfile.ZipFile(source_archive) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
            infos = {name: archive.getinfo(name) for name in archive.namelist()}
        assert "manifest.json" in members
        manifest = json.loads(members["manifest.json"])
        assert manifest["format"] == "daikibo.backup.v1"
        assert not any(name.startswith("backup-recipes/") for name in members)
        legacy_path = tmp_path / "legacy-backup-v1.zip"
        with zipfile.ZipFile(legacy_path, "w", compression=zipfile.ZIP_DEFLATED) as dest:
            for name, value in members.items():
                info = infos[name]
                dest.writestr(zipfile.ZipInfo(name, date_time=info.date_time), value,
                               compress_type=zipfile.ZIP_DEFLATED)
        raw = legacy_path.read_bytes()
        with zipfile.ZipFile(legacy_path) as checked:
            assert checked.getinfo("manifest.json").compress_type == zipfile.ZIP_DEFLATED
            assert json.loads(checked.read("manifest.json"))["format"] == "daikibo.backup.v1"
        return raw, manifest, payload_blob, payload
    finally:
        source.close()


def test_three_real_system_backups_keep_legacy_user_payloads_and_page_after_old_home_gone(
    full, tmp_path
):
    """Exercise three normal job/result/event generations and fresh-home paging."""
    control = full
    high_entropy = _high_entropy(2 * 1024 * 1024)
    regular_blob = control.s.blob_put(high_entropy)
    user_zip_path = tmp_path / "ordinary-user-upload.zip"
    with zipfile.ZipFile(user_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("arbitrary/high-entropy.bin", _high_entropy(256 * 1024, b"user-zip"))
    user_zip_bytes = user_zip_path.read_bytes()
    user_zip_blob = control.s.blob_put(user_zip_bytes)

    legacy_bytes, legacy_manifest, legacy_payload_blob, legacy_payload = _legacy_backup_v1(tmp_path)
    legacy_blob = control.s.blob_put(legacy_bytes)
    with zipfile.ZipFile(tmp_path / "legacy-backup-v1.zip") as legacy_archive:
        assert json.loads(legacy_archive.read("manifest.json"))["format"] == "daikibo.backup.v1"
        assert legacy_archive.getinfo("manifest.json").compress_type == zipfile.ZIP_DEFLATED
    assert legacy_blob != user_zip_blob and legacy_bytes != user_zip_bytes

    # Restore the generated legacy transport itself before exercising newer
    # generations.  This keeps the old backup.v1 path covered by an actual
    # state/blob round trip rather than using it only as an ordinary ZIP blob.
    legacy_restore_home = tmp_path / "legacy-restored"
    legacy_archive_path = tmp_path / "legacy-backup-v1.zip"
    restore_backup(legacy_archive_path, legacy_restore_home, file_hash(legacy_archive_path))
    legacy_restored = Control(legacy_restore_home, mode="validation", start_workers=False)
    legacy_restored.owner = legacy_restored.sec.authenticate(
        Path(legacy_restored.sec.bootstrap()).read_text()
    )
    try:
        assert (legacy_restore_home / "state.sqlite3").is_file()
        assert legacy_restored.s.blob_get(legacy_payload_blob) == legacy_payload
    finally:
        legacy_restored.close()

    results = []
    archive_copies = []
    recipe_refs = []
    for generation in range(3):
        result, state = _run_system_backup(control, f"normal-backup-generation-{generation}")
        archive = Path(result["path"])
        raw = archive.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == result["sha256"]
        assert result["bytes"] == len(raw)
        with zipfile.ZipFile(archive) as inspected:
            manifest = json.loads(inspected.read("manifest.json"))
            assert manifest["format"] == "daikibo.backup.v1"
            names = set(inspected.namelist())
            assert f"blobs/{legacy_blob[:2]}/{legacy_blob[2:]}" in names
            assert f"blobs/{user_zip_blob[:2]}/{user_zip_blob[2:]}" in names
            assert f"blobs/{regular_blob[:2]}/{regular_blob[2:]}" in names
            if generation:
                assert f"backup-recipes/{results[-1]['blob']}.json" in names
        recipe = json.loads((control.s.backup_recipes / f"{result['blob']}.json").read_text())
        refs = {segment["blob"] for segment in recipe["segments"]}
        assert refs and sum(segment["bytes"] for segment in recipe["segments"]) == result["bytes"]
        results.append(result)
        recipe_refs.append(refs)
        copy = tmp_path / f"generation-{generation}.zip"
        shutil.copyfile(archive, copy)
        archive_copies.append(copy)

    assert all(not (control.s.blobs / r["blob"][:2] / r["blob"][2:]).exists() for r in results)
    assert all(len(recipe_refs[i] - recipe_refs[i - 1]) > 0 for i in range(1, 3))
    assert results[1]["bytes"] < results[0]["bytes"] * 2
    assert results[2]["bytes"] < results[1]["bytes"] * 2
    assert control.s.blob_get(regular_blob) == high_entropy
    assert control.s.blob_get(user_zip_blob) == user_zip_bytes
    assert control.s.blob_get(legacy_blob) == legacy_bytes
    assert legacy_manifest["format"] == "daikibo.backup.v1"

    old_home = control.s.home
    control.close()
    shutil.rmtree(old_home)
    restored_home = tmp_path / "restored-after-old-home-removal"
    restore_backup(archive_copies[-1], restored_home, results[-1]["sha256"])
    restored = Control(restored_home, mode="validation", start_workers=False)
    restored.owner = restored.sec.authenticate(Path(restored.sec.bootstrap()).read_text())
    try:
        for result, archive in zip(results, archive_copies):
            expected = archive.read_bytes()
            assert _read_owner_pages(restored, result["blob"]) == expected
            assert restored.s.blob_get(result["blob"]) == expected
        assert restored.s.blob_get(regular_blob) == high_entropy
        assert restored.s.blob_get(user_zip_blob) == user_zip_bytes
        assert restored.s.blob_get(legacy_blob) == legacy_bytes
    finally:
        restored.close()


def test_public_ranges_refuse_reordered_recipe_even_at_end_or_beyond(full):
    """The advertised artifact digest binds segment ordering for every range."""
    control = full
    result, _ = _run_system_backup(control, "range-reorder")
    recipe_path = control.s.backup_recipes / f"{result['blob']}.json"
    original = recipe_path.read_bytes()
    recipe = json.loads(original)
    assert len(recipe["segments"]) >= 2
    recipe["segments"][0], recipe["segments"][1] = recipe["segments"][1], recipe["segments"][0]
    recipe_path.write_bytes(canonical(recipe))
    try:
        for offset in (0, result["bytes"], result["bytes"] + 1):
            with pytest.raises(Fault):
                control.blob_read(control.owner, result["blob"], offset=offset, limit=1)
    finally:
        recipe_path.write_bytes(original)


def test_public_ranges_refuse_equal_length_valid_leaf_substitution(full):
    """A valid replacement leaf cannot hide a changed archive under the old SHA."""
    control = full
    result, _ = _run_system_backup(control, "range-substitution")
    recipe_path = control.s.backup_recipes / f"{result['blob']}.json"
    original = recipe_path.read_bytes()
    recipe = json.loads(original)
    segment = recipe["segments"][0]
    leaf_path = control.s.blobs / segment["blob"][:2] / segment["blob"][2:]
    replacement = bytes([0xA5]) * segment["bytes"]
    replacement_blob = control.s.blob_put(replacement)
    assert replacement_blob != segment["blob"]
    recipe["segments"][0]["blob"] = replacement_blob
    recipe_path.write_bytes(canonical(recipe))
    try:
        with pytest.raises(Fault):
            control.blob_read(control.owner, result["blob"], offset=0, limit=1)
    finally:
        assert leaf_path.is_file()
        recipe_path.write_bytes(original)


def test_nonowner_range_denial_precedes_recipe_integrity_lookup(full, monkeypatch):
    control = full
    result, _ = _run_system_backup(control, "range-nonowner")
    import daikibo.backup_artifacts as artifacts

    other_project = control.k.create_project(control.owner, "non-owner-range")['id']
    touched = {"value": False}
    original = artifacts._load_recipe

    def observe_lookup(*args, **kwargs):
        touched["value"] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_load_recipe", observe_lookup)
    with pytest.raises(Fault) as denied:
        control.blob_read(Actor("other", "agent", project=other_project), result["blob"], project=other_project)
    assert denied.value.code == "forbidden"
    assert touched["value"] is False


def test_public_page_reads_amortize_physical_closure_hashing(full, monkeypatch):
    """A session-aware implementation must not hash the whole closure per page."""
    control = full
    control.s.blob_put(_high_entropy(2 * 1024 * 1024, b"paging"))
    result, _ = _run_system_backup(control, "range-page-performance")
    recipe = validate_recipe(control.s, result["blob"])
    pages = (result["bytes"] + 64 * 1024 - 1) // (64 * 1024)
    import daikibo.backup_artifacts as artifacts

    calls = {"count": 0}
    hashed = {"bytes": 0}
    original = artifacts._read_leaf

    def count_leaf(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    def count_digest(stream, algorithm, **kwargs):
        digest = hashlib.new(algorithm)
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            hashed["bytes"] += len(block)
            digest.update(block)
        return digest

    monkeypatch.setattr(artifacts, "_read_leaf", count_leaf)
    monkeypatch.setattr(artifacts.hashlib, "file_digest", count_digest)
    for offset in range(0, result["bytes"], 64 * 1024):
        control.blob_read(control.owner, result["blob"], offset=offset, limit=64 * 1024)
    assert calls["count"] < len(recipe.physical_refs) * pages
    closure_bytes = sum(
        (control.s.blobs / ref[:2] / ref[2:]).stat().st_size
        for ref in recipe.physical_refs
    )
    assert hashed["bytes"] >= closure_bytes
    assert hashed["bytes"] < closure_bytes * pages


def test_destructive_gc_preserves_valid_roots_and_unlinks_only_orphan_then_fails_closed(full):
    control = full
    result, _ = _run_system_backup(control, "gc-roots")
    metadata = validate_recipe(control.s, result["blob"])
    valid_refs = set(metadata.physical_refs)
    orphan = control.s.blob_put(b"collectible orphan")
    orphan_path = control.s.blobs / orphan[:2] / orphan[2:]
    os.utime(orphan_path, (1, 1))
    report = control.ops.garbage_collect(control.owner, dry_run=False)
    assert orphan in {item["blob"] for item in report["candidates"]}
    assert not orphan_path.exists()
    assert all((control.s.blobs / ref[:2] / ref[2:]).is_file() for ref in valid_refs)

    recipe_path = control.s.backup_recipes / f"{result['blob']}.json"
    original = recipe_path.read_bytes()
    orphan_after_corruption = control.s.blob_put(b"orphan retained after invalid closure")
    orphan_after_path = control.s.blobs / orphan_after_corruption[:2] / orphan_after_corruption[2:]
    os.utime(orphan_after_path, (1, 1))
    damaged = json.loads(original)
    damaged["segments"][0]["blob"] = "0" * 64
    recipe_path.write_bytes(canonical(damaged))
    try:
        with pytest.raises(Fault):
            control.ops.garbage_collect(control.owner, dry_run=False)
        assert orphan_after_path.is_file(), "invalid closure must cause zero destructive unlink"
        assert all((control.s.blobs / ref[:2] / ref[2:]).is_file() for ref in valid_refs)
    finally:
        recipe_path.write_bytes(original)


def test_gc_and_normal_backup_contention_serializes_without_losing_roots(full, monkeypatch):
    control = full
    import daikibo.operations as operations

    # Seed a committed recipe so the contention check has a complete existing
    # inventory to compare after the destructive competitor has run.
    seed, _ = _run_system_backup(control, "contention-seed")
    before = _snapshot_inventory(control)
    seed_recipe = validate_recipe(control.s, seed["blob"])

    entered = threading.Event()
    release = threading.Event()
    gc_started = threading.Event()
    original_copy = operations.copy_hashed
    original_gc = control.ops.garbage_collect

    def pause_copy(*args, **kwargs):
        entered.set()
        assert release.wait(10), "backup did not receive the release signal"
        return original_copy(*args, **kwargs)

    def observe_gc(actor, *args, **kwargs):
        gc_started.set()
        return original_gc(actor, *args, **kwargs)

    monkeypatch.setattr(operations, "copy_hashed", pause_copy)
    monkeypatch.setattr(control.ops, "garbage_collect", observe_gc)
    queued = control.request(None, {"id": "contention-backup", "method": "system.backup", "params": {}})
    row = control.s.one("SELECT * FROM jobs WHERE id=?", (queued["id"],), True)
    result_holder = {}
    worker = threading.Thread(target=lambda: result_holder.setdefault("outcome", control.jobs.run_one(row)))
    worker.start()
    assert entered.wait(10), "normal system.backup did not enter its export path"
    gc_holder = {}

    def run_gc():
        try:
            gc_holder["result"] = control.ops.garbage_collect(control.owner, dry_run=False)
        except Fault as exc:
            gc_holder["error"] = exc.as_dict()

    gc_thread = threading.Thread(target=run_gc)
    gc_thread.start()
    assert gc_started.wait(10), "GC did not enter while backup was in progress"
    release.set()
    worker.join(30)
    gc_thread.join(30)
    assert not worker.is_alive() and not gc_thread.is_alive()
    assert result_holder["outcome"]["status"] == "succeeded"
    assert gc_holder.get("error", {}).get("code") == "busy"

    # The competing destructive call must leave the complete pre-existing
    # physical/recipe/export inventory byte-identical.  Then exercise the
    # actual destructive path after the backup window with a fresh orphan and
    # verify both committed recipe closures remain rooted.
    _assert_inventory_unchanged(control, before)
    result = control.jobs.get(control.owner, queued["id"])["result"]
    result_recipe = validate_recipe(control.s, result["blob"])
    orphan = control.s.blob_put(b"contention orphan after backup")
    orphan_path = control.s.blobs / orphan[:2] / orphan[2:]
    os.utime(orphan_path, (1, 1))
    report = control.ops.garbage_collect(control.owner, dry_run=False)
    assert orphan in {item["blob"] for item in report["candidates"]}
    assert not orphan_path.exists()
    _assert_inventory_unchanged(control, before)
    for ref in set(seed_recipe.physical_refs) | set(result_recipe.physical_refs):
        assert (control.s.blobs / ref[:2] / ref[2:]).is_file()


@pytest.mark.parametrize("failure", ["leaf", "export", "recipe", "crash_after_recipe"])
def test_leaf_export_recipe_and_post_publication_failures_retain_evidence(full, monkeypatch, failure):
    control = full
    first, _ = _run_system_backup(control, f"failure-baseline-{failure}")
    before_inventory = _snapshot_inventory(control)
    before_recipes = {
        name.rsplit("/", 1)[-1]: record["sha256"]
        for name, record in before_inventory["backup_recipes"].items()
    }
    before_exports = sorted(control.s.home.joinpath("exports").glob("*.zip"))
    import daikibo.operations as operations

    if failure == "leaf":
        original = control.s.blob_put
        calls = {"count": 0}

        def fail_leaf(data):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("simulated leaf disk-full")
            return original(data)

        monkeypatch.setattr(control.s, "blob_put", fail_leaf)
    elif failure == "export":
        def fail_export(*args, **kwargs):
            raise OSError("simulated export disk-full")

        monkeypatch.setattr(operations, "copy_hashed", fail_export)
    elif failure == "recipe":
        def fail_recipe(*args, **kwargs):
            raise OSError("simulated recipe disk-full")

        monkeypatch.setattr(operations, "publish_recipe", fail_recipe)
    else:
        original_publish = operations.publish_recipe

        def crash_after_publish(*args, **kwargs):
            value = original_publish(*args, **kwargs)
            raise RuntimeError("simulated crash after recipe publication")

        monkeypatch.setattr(operations, "publish_recipe", crash_after_publish)

    queued = control.request(None, {"id": f"failure-{failure}", "method": "system.backup", "params": {}})
    row = control.s.one("SELECT * FROM jobs WHERE id=?", (queued["id"],), True)
    outcome = control.jobs.run_one(row)
    assert outcome["status"] == "failed"
    assert control.jobs.get(control.owner, queued["id"])["status"] == "failed"
    after_recipes = {p.name: p.read_bytes() for p in control.s.backup_recipes.glob("*.json")}
    if failure != "crash_after_recipe":
        assert {
            name: hashlib.sha256(value).hexdigest() for name, value in after_recipes.items()
        } == before_recipes
    else:
        assert all(
            hashlib.sha256(after_recipes[name]).hexdigest() == value
            for name, value in before_recipes.items()
        )
        assert len(after_recipes) == len(before_recipes) + 1
    _assert_inventory_unchanged(control, before_inventory)
    assert all(p.is_file() for p in before_exports)
    assert not list(control.s.home.joinpath("exports").glob("*.partial"))

    if failure == "crash_after_recipe":
        exports = sorted(control.s.home.joinpath("exports").glob("*.zip"))
        assert len(exports) == len(before_exports) + 1
        orphan_sha = file_hash(exports[-1])
        assert (control.s.backup_recipes / f"{orphan_sha}.json").is_file()
        assert validate_recipe(control.s, orphan_sha).artifact_sha256 == orphan_sha
    elif failure == "recipe":
        assert len(list(control.s.home.joinpath("exports").glob("*.zip"))) == len(before_exports) + 1


def test_abrupt_subprocess_after_recipe_publication_reopens_and_preserves_inventory(full, tmp_path):
    """An abnormal process exit after publication leaves inspectable evidence."""
    control = full
    retained_blob = control.s.blob_put(_high_entropy(256 * 1024, b"crash-retained"))
    _run_system_backup(control, "subprocess-crash-baseline")
    before = _snapshot_inventory(control)
    home = control.s.home
    control.close()

    crash_script = child_import_guard() + r'''
import os
import sys
from pathlib import Path

import daikibo.operations as operations
from daikibo.control import Control

home = Path(sys.argv[1])
control = Control(home, mode="validation", start_workers=False)
control.owner = control.sec.authenticate(Path(control.sec.bootstrap()).read_text())
original_publish = operations.publish_recipe

def publish_then_abort(*args, **kwargs):
    original_publish(*args, **kwargs)
    os._exit(73)

operations.publish_recipe = publish_then_abort
queued = control.request(None, {"id": "subprocess-crash", "method": "system.backup", "params": {}})
row = control.s.one("SELECT * FROM jobs WHERE id=?", (queued["id"],), True)
control.jobs.run_one(row)
os._exit(74)
'''
    environment = _child_env()
    completed = subprocess.run(
        [sys.executable, "-c", crash_script, str(home)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 73, (completed.stdout, completed.stderr)

    reopened = Control(home, mode="validation", start_workers=False)
    reopened.owner = reopened.sec.authenticate(Path(reopened.sec.bootstrap()).read_text())
    try:
        # Startup fences the interrupted job and runs normal retention
        # reconciliation; it must not discard the committed recipe/export.
        crashed_job = reopened.s.one(
            "SELECT id FROM jobs WHERE kind='ops.backup' ORDER BY created DESC LIMIT 1", (), True
        )
        state = reopened.jobs.get(reopened.owner, crashed_job["id"])
        assert state["status"] == "unknown"
        _assert_inventory_unchanged(reopened, before)
        assert reopened.s.blob_get(retained_blob) == _high_entropy(256 * 1024, b"crash-retained")

        recipe_names = {path.name for path in reopened.s.backup_recipes.glob("*.json")}
        before_recipe_names = set(before["backup_recipes"])
        before_recipe_names = {name.rsplit("/", 1)[-1] for name in before_recipe_names}
        new_recipe_names = recipe_names - before_recipe_names
        assert len(new_recipe_names) == 1
        orphan_sha = next(iter(new_recipe_names))[:-5]
        orphan_recipe = validate_recipe(reopened.s, orphan_sha)
        assert orphan_recipe.artifact_sha256 == orphan_sha
        new_exports = [
            path for path in (home / "exports").glob("*.zip")
            if file_hash(path) == orphan_sha
        ]
        assert len(new_exports) == 1

        # The crash-created recipe is a durable root, so a real destructive
        # sweep after fresh-process reopen must preserve it and the old set.
        reopened.ops.garbage_collect(reopened.owner, dry_run=False)
        _assert_inventory_unchanged(reopened, before)
        assert validate_recipe(reopened.s, orphan_sha).artifact_sha256 == orphan_sha
    finally:
        reopened.close()


def test_pending_oversized_retention_survives_managed_backup_restart_restore(full, full_project, tmp_path, monkeypatch):
    """Restore a pending >32MiB generation before and after reconciliation."""
    from test_collector_failure_retention import _force_collection_failure, _observe

    control = full
    project, repo_id, _, _ = full_project
    _force_collection_failure(monkeypatch, control)
    original_stream = control.s.__class__.blob_put_stream.__get__(control.s, control.s.__class__)

    def no_space(*args, **kwargs):
        raise OSError(28, "simulated retention disk full")

    monkeypatch.setattr(control.s, "blob_put_stream", no_space)
    snapshot = control.sn.capture(control.owner, project)
    observed, after, _ = _observe(
        control,
        project,
        snapshot,
        "from pathlib import Path; import os; "
        "Path('pending-oversized.bin').write_bytes(b'x'*(33*1024*1024)); "
        "Path('pending-link').symlink_to('/outside/pending-retention-target'); "
        "os.mkfifo('pending.pipe')",
    )
    assert after is None
    assert observed["recovery_artifacts"]["status"] == "partial"
    assert control.rt.retention.has_pending()
    initial_page = control.invoke(control.owner, "run.recovery", {"run": observed["run"], "limit": 20})
    initial_entries = {item["path"]: item for item in initial_page["entries"]}
    expected_special = {
        path: (item["kind"], item.get("target"), item["mode"])
        for path, item in initial_entries.items()
        if path in {"pending-link", "pending.pipe"}
    }
    assert expected_special["pending-link"][:2] == ("symlink", "/outside/pending-retention-target")
    assert expected_special["pending.pipe"][0] == "fifo"
    large_bytes = 33 * 1024 * 1024
    expected_large_digest = hashlib.sha256(b"x" * large_bytes).hexdigest()
    monkeypatch.setattr(control.s, "blob_put_stream", original_stream)

    first, _ = _run_system_backup(control, "pending-oversized-backup-before-restart")
    first_copy = tmp_path / "pending-before-restart.zip"
    shutil.copyfile(first["path"], first_copy)
    old_home = control.s.home
    control.close()

    restarted = Control(old_home, mode="validation", start_workers=False)
    restarted.owner = restarted.sec.authenticate(Path(restarted.sec.bootstrap()).read_text())

    def assert_reconciled_recovery(current):
        page = current.invoke(current.owner, "run.recovery", {"run": observed["run"], "limit": 20})
        entries = {item["path"]: item for item in page["entries"]}
        large = entries["pending-oversized.bin"]
        assert large["repo"] == repo_id
        assert large["observed_bytes"] == large_bytes
        assert large["retention_status"] == "stored"
        assert large["blob"] == expected_large_digest
        assert _read_recovery_file(
            current, observed["run"], repo_id, "pending-oversized.bin", large["blob"]
        ) == (expected_large_digest, large_bytes)
        for path, (kind, target, mode) in expected_special.items():
            entry = entries[path]
            assert (entry["kind"], entry.get("target"), entry["mode"]) == (kind, target, mode)
            assert entry["retention_status"] == "metadata_only"
        assert not current.rt.retention.has_pending()

    try:
        summary = restarted.rt.retention.summary(observed["run"])
        assert summary and summary["status"] == "complete"
        assert_reconciled_recovery(restarted)
        second, _ = _run_system_backup(restarted, "pending-oversized-backup-after-restart")
        second_copy = tmp_path / "pending-after-restart.zip"
        shutil.copyfile(second["path"], second_copy)
    finally:
        restarted.close()
    shutil.rmtree(old_home)

    # This first backup still contains the pending marker/staged raw tree.
    # Restoring it after the original home is gone proves that reconciliation
    # has all bytes and metadata needed for a fresh control to recover it.
    first_fresh = tmp_path / "fresh-pending-oversized-before-second-backup"
    restore_backup(first_copy, first_fresh, first["sha256"])
    first_restored = Control(first_fresh, mode="validation", start_workers=False)
    first_restored.owner = first_restored.sec.authenticate(Path(first_restored.sec.bootstrap()).read_text())
    try:
        assert_reconciled_recovery(first_restored)
        assert _read_owner_pages(first_restored, first["blob"]) == first_copy.read_bytes()
    finally:
        first_restored.close()

    second_fresh = tmp_path / "fresh-pending-oversized-after-second-backup"
    restore_backup(second_copy, second_fresh, second["sha256"])
    second_restored = Control(second_fresh, mode="validation", start_workers=False)
    second_restored.owner = second_restored.sec.authenticate(Path(second_restored.sec.bootstrap()).read_text())
    try:
        assert_reconciled_recovery(second_restored)
        assert _read_owner_pages(second_restored, second["blob"]) == second_copy.read_bytes()
    finally:
        second_restored.close()
