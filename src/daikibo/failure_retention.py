"""Durable, non-adoptable evidence for collector failures.

The ordinary snapshot collector is intentionally strict: a source file over
32 MiB (or an unsafe filesystem object) makes the snapshot fail.  A failed
collector still needs a bounded, reviewable inventory of the worker tree so
that the failure is diagnosable after the mutable worker directory is gone.
This module owns that second responsibility.  It never produces a candidate
or changes an attempt/receipt result.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .backup_artifacts import artifact_size, iter_artifact_range, open_artifact_session, validate_recipe
from .common import Fault, atomic_write, canonical, digest, need, number, parse_json, timestamp
from .gitops import EXCLUDED_DIRS, EXCLUDED_FILES


FORMAT = "failed-artifacts.v1"
PENDING_FORMAT = "daikibo.failed-artifact-pending.v1"
PENDING_INVENTORY_FORMAT = "daikibo.pending-recovery-inventory.v1"
PENDING_INVENTORY_SUFFIX = ".inventory"
ENTRY_CHUNK_FORMAT = "failed-artifact-entry-chunk.v1"
ENTRY_PAGE_FORMAT = "failed-artifact-entry-page.v1"
ENTRY_INDEX_FORMAT = "failed-artifact-entry-index.v1"
ENTRY_CHUNK_SIZE = 128
ENTRY_PAGE_CHUNKS = 256
ENTRY_INDEX_SIZE = 128
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
PENDING_INVENTORY_MAX_BYTES = MAX_MANIFEST_BYTES
MAX_DETAIL_LIMIT = 1000
ERROR_SAMPLE_LIMIT = 32


def _mode(st: os.stat_result) -> int:
    return stat.S_IFMT(st.st_mode) | stat.S_IMODE(st.st_mode)


def _kind(st: os.stat_result) -> str:
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISFIFO(st.st_mode):
        return "fifo"
    if stat.S_ISSOCK(st.st_mode):
        return "socket"
    if stat.S_ISCHR(st.st_mode):
        return "character_device"
    if stat.S_ISBLK(st.st_mode):
        return "block_device"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    return "other"


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)


class FailureRetention:
    """Collect and read failed artifacts without changing workflow state."""

    def __init__(self, store, security=None):
        self.s = store
        self.sec = security
        self.root = self.s.home / "recovery"
        self.pending = self.root / "pending"
        self.staging = self.root / "staging"
        self.pending.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _marker_path(self, run: str) -> Path:
        need(isinstance(run, str) and run and "/" not in run and "\\" not in run,
             "invalid_run", "Invalid run identifier")
        return self.pending / f"{run}.json"

    def _write_marker(self, marker: dict) -> None:
        marker = dict(marker)
        marker["updated"] = timestamp()
        atomic_write(self._marker_path(marker["run"]), canonical(marker), mode=0o600)

    def _read_marker(self, run: str) -> dict | None:
        path = self._marker_path(run)
        if not path.is_file() or path.is_symlink():
            return None
        try:
            value = parse_json(path.read_bytes(), limit=2 * 1024 * 1024)
            need(isinstance(value, dict) and value.get("format") == PENDING_FORMAT,
                 "invalid_retention_marker", "Pending retention marker has an unsupported format")
            return value
        except (OSError, Fault, UnicodeError, json.JSONDecodeError) as exc:
            # A corrupt marker is itself durable evidence.  Do not remove or
            # replace it while the operator is deciding how to recover it.
            return {"format": PENDING_FORMAT, "run": run, "state": "marker_error",
                    "retention_error": {"code": "invalid_retention_marker",
                                         "message": str(exc)[:2000]}}

    def _inventory_path(self, run: str) -> Path:
        return self.pending / f"{run}{PENDING_INVENTORY_SUFFIX}"

    def _read_pending_inventory(self, run: str, marker: dict | None = None) -> dict | None:
        path = self._inventory_path(run)
        if not path.exists():
            need(not (marker or {}).get("inventory_required"), "integrity_error",
                 "Pending recovery inventory sidecar is missing")
            return None
        need(path.is_file() and not path.is_symlink(), "integrity_error",
             "Pending recovery inventory is not a regular file")
        inventory = parse_json(path.read_bytes(), limit=PENDING_INVENTORY_MAX_BYTES)
        need(isinstance(inventory, dict) and inventory.get("format") == PENDING_INVENTORY_FORMAT,
             "integrity_error", "Pending recovery inventory format differs")
        need(inventory.get("run") == run, "integrity_error", "Pending recovery inventory run differs")
        roots = inventory.get("roots")
        need(isinstance(roots, dict), "integrity_error", "Pending recovery inventory roots are malformed")
        for key, relative in roots.items():
            need(key in {"worker_root", "staged_root", "work_root"}, "integrity_error",
                 "Pending recovery inventory has an unknown root key", key)
            need(isinstance(relative, str) and relative, "integrity_error",
                 "Pending recovery inventory root is not a string", key)
            relative_path = PurePosixPath(relative)
            need(not relative_path.is_absolute() and "\\" not in relative
                 and ".." not in relative_path.parts,
                 "integrity_error", "Pending recovery inventory root is unsafe", key)
        raw_root = inventory.get("raw_root")
        need(raw_root in {"worker_root", "staged_root"} and raw_root in roots and "work_root" in roots,
             "integrity_error", "Pending recovery inventory has no raw root")
        need(PurePosixPath(roots["work_root"]) == PurePosixPath(roots[raw_root]) / "work",
             "integrity_error", "Pending recovery work root is not structurally bound")
        entries = inventory.get("entries")
        need(isinstance(entries, list), "integrity_error", "Pending recovery inventory entries are malformed")
        seen = set()
        for item in entries:
            need(isinstance(item, dict), "integrity_error", "Pending recovery inventory entry is malformed")
            relative = item.get("path")
            kind = item.get("kind")
            need(isinstance(relative, str) and relative, "integrity_error",
                 "Pending recovery inventory entry path is not a string")
            relative_path = PurePosixPath(relative)
            need(not relative_path.is_absolute() and "\\" not in relative
                 and ".." not in relative_path.parts,
                 "integrity_error", "Pending recovery inventory entry path is unsafe")
            need(kind in {"file", "symlink", "fifo", "socket", "character_device", "block_device", "other"},
                 "integrity_error", "Pending recovery inventory entry kind is unsupported", kind)
            root_key = item.get("root")
            need(root_key in {"worker_root", "staged_root"} and root_key in roots,
                 "integrity_error", "Pending recovery inventory entry root is unknown", relative)
            need(relative_path.is_relative_to(PurePosixPath(roots[root_key])),
                 "integrity_error", "Pending recovery inventory entry escapes its root", relative)
            need(relative not in seen, "integrity_error", "Pending recovery inventory contains duplicate entries", relative)
            seen.add(relative)
            need(type(item.get("mode")) is int and type(item.get("bytes")) is int and item["bytes"] >= 0,
                 "integrity_error", "Pending recovery inventory metadata is incomplete", relative)
            if kind == "file":
                sha256_value = item.get("sha256")
                need(isinstance(sha256_value, str) and len(sha256_value) == 64
                     and all(char in "0123456789abcdef" for char in sha256_value),
                     "integrity_error", "Pending regular inventory entry has no valid hash", relative)
            elif kind == "symlink":
                need(isinstance(item.get("target"), str), "integrity_error",
                     "Pending symlink metadata has no target", relative)
        expected_digest = (marker or {}).get("inventory_digest")
        if (marker or {}).get("inventory_required"):
            need(isinstance(expected_digest, str) and len(expected_digest) == 64,
                 "integrity_error", "Pending recovery marker has no inventory digest")
        need(expected_digest is None or expected_digest == digest(inventory), "integrity_error",
             "Pending recovery inventory does not match its marker digest")
        return inventory

    def _validate_pending_inventory_raw(self, inventory: dict, marker: dict) -> None:
        """Validate a sidecar's home binding and raw regular payloads.

        Backup validation protects the archive boundary.  Startup has a second
        boundary: the staged tree may have been changed after restore, so the
        exact sidecar expectations must be checked before a recovery manifest is
        declared durable.  Special entries may be absent because restore keeps
        them as metadata-only evidence; if one is present, its identity must
        still agree with the sidecar.
        """
        roots = inventory["roots"]
        root_paths = {}
        for key in ("worker_root", "staged_root"):
            if key not in roots:
                continue
            marker_value = marker.get(key)
            need(isinstance(marker_value, str) and marker_value, "integrity_error",
                 "Pending recovery marker is missing an inventory root", key)
            expected = self.s.home / PurePosixPath(roots[key])
            expected_resolved = expected.resolve(strict=False)
            need(expected_resolved.is_relative_to(self.s.home.resolve(strict=True)), "integrity_error",
                 "Pending recovery inventory root escapes its control home", key)
            actual = Path(marker_value)
            need(actual.is_absolute(), "integrity_error",
                 "Pending recovery marker root is not absolute", key)
            need(actual.resolve(strict=False) == expected_resolved, "integrity_error",
                 "Pending recovery marker root differs from its inventory binding", key)
            need(actual.exists() and actual.is_dir() and not actual.is_symlink(), "integrity_error",
                 "Pending recovery inventory root is missing or unsafe", key)
            root_paths[key] = actual
        raw_root = inventory["raw_root"]
        need(raw_root in root_paths, "integrity_error",
             "Pending recovery raw root is missing", raw_root)
        work_relative = PurePosixPath(roots["work_root"])
        marker_work = marker.get("work_root")
        expected_work = self.s.home / work_relative
        need(isinstance(marker_work, str) and marker_work, "integrity_error",
             "Pending recovery marker is missing its work root")
        actual_work = Path(marker_work)
        need(actual_work.is_absolute() and actual_work.resolve(strict=False) == expected_work.resolve(strict=False),
             "integrity_error", "Pending recovery marker work root differs from its inventory binding")
        need(actual_work.is_dir() and not actual_work.is_symlink(), "integrity_error",
             "Pending recovery work root is missing or unsafe")

        for item in inventory["entries"]:
            relative = PurePosixPath(item["path"])
            root_key = item["root"]
            root_relative = PurePosixPath(roots[root_key])
            need(relative.is_relative_to(root_relative), "integrity_error",
                 "Pending recovery inventory entry escapes its root", item["path"])
            path = self.s.home / relative
            need(path.is_relative_to(self.s.home), "integrity_error",
                 "Pending recovery inventory entry escapes its control home", item["path"])
            # Resolve only parent components: the final member may be a
            # deliberately un-followed symlink whose target is recorded as
            # metadata and is allowed to point outside the raw root.
            need(path.parent.resolve(strict=True).is_relative_to(root_paths[root_key].resolve(strict=True)),
                 "integrity_error", "Pending recovery inventory entry escapes its bound root", item["path"])
            kind = item["kind"]
            exists = path.exists() or path.is_symlink()
            if kind == "file":
                need(exists and path.is_file() and not path.is_symlink(), "integrity_error",
                     "Pending regular payload is missing from the raw tree", item["path"])
                observed = path.stat()
                need(observed.st_size == item["bytes"], "integrity_error",
                     "Pending regular payload size differs from its inventory", item["path"])
                with path.open("rb") as stream:
                    observed_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                need(observed_hash == item["sha256"], "integrity_error",
                     "Pending regular payload hash differs from its inventory", item["path"])
                continue
            if not exists:
                # Restore intentionally omits FIFOs, sockets, devices and
                # symlinks. Their exact metadata remains in the manifest.
                continue
            observed = path.lstat()
            need(_kind(observed) == kind, "integrity_error",
                 "Pending special metadata kind differs from its inventory", item["path"])
            need(_mode(observed) == item["mode"] and observed.st_size == item["bytes"],
                 "integrity_error", "Pending special metadata differs from its inventory", item["path"])
            if kind == "symlink":
                need(os.readlink(path) == item["target"], "integrity_error",
                     "Pending symlink target differs from its inventory", item["path"])

    @staticmethod
    def _metadata_entries(inventory: dict | None, repos: list[dict]) -> dict[tuple[str, str], dict]:
        """Translate backup-only special metadata into recovery manifest entries."""
        if inventory is None:
            return {}
        roots = {key: PurePosixPath(value) for key, value in inventory["roots"].items()
                 if key in {"worker_root", "staged_root"}}
        by_name = {repo["name"]: repo for repo in repos}
        result = {}
        for item in inventory["entries"]:
            path = PurePosixPath(item["path"])
            root_key = item.get("root")
            root = roots.get(root_key)
            need(root is not None and path.is_relative_to(root), "integrity_error",
                 "Pending recovery metadata is outside its raw root", item["path"])
            if item["kind"] == "file":
                continue
            work = root / "work"
            if not path.is_relative_to(work):
                continue
            parts = path.relative_to(work).parts
            if len(parts) < 2:
                continue
            repo_name = parts[0]
            repo = by_name.get(repo_name)
            if repo is None:
                continue
            relative = "/".join(parts[1:])
            kind = item["kind"]
            entry = {
                "repo": repo["id"], "repo_name": repo["name"], "path": relative,
                "kind": kind, "mode": item.get("mode"), "observed_bytes": item.get("bytes", 0),
                "blob": None, "sha256": None, "classification": "unsafe_metadata_only",
                "retention_status": "metadata_only", "reason": f"{kind}_not_read",
            }
            if kind == "symlink":
                entry["reason"] = "symlink_not_followed"
                entry["target"] = item["target"]
            key = (repo["id"], relative)
            need(key not in result, "integrity_error", "Pending recovery metadata maps to duplicate entries", relative)
            result[key] = entry
        return result

    def begin(self, *, project, run, task, epoch, role, root: Path, snapshot: dict) -> dict:
        """Create the durable marker before a managed process starts."""
        repos = [{"id": rid, "name": repo["name"]} for rid, repo in snapshot.get("repos", {}).items()]
        marker = {
            "format": PENDING_FORMAT, "run": run, "project": project, "task": task,
            "epoch": epoch, "role": role, "worker_root": str(root),
            "work_root": str(root / "work"), "staged_root": None,
            "repos": repos, "input_snapshot": snapshot.get("digest"),
            "state": "running", "created": timestamp(), "updated": timestamp(),
        }
        self._write_marker(marker)
        return marker

    @staticmethod
    def _bounded(value, limit=8192):
        try:
            encoded = canonical(value)
        except Exception:
            return {"truncated": True, "type": type(value).__name__}
        if len(encoded) <= limit:
            return value
        return {"truncated": True, "digest": digest(value), "bytes": len(encoded)}

    @staticmethod
    def _error(exc: BaseException, operation: str) -> dict:
        if isinstance(exc, Fault):
            return {"code": exc.code, "message": str(exc.message)[:2000], "details": FailureRetention._bounded(exc.details),
                    "operation": operation}
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            return {"code": "disk_full", "message": "Retention storage ran out of space",
                    "operation": operation}
        return {"code": "retention_write_failed", "message": str(exc)[:2000],
                "type": type(exc).__name__, "operation": operation}

    def _iter_paths(self, root: Path):
        """Yield deterministic paths without following symlinks.

        Eagerly sorting a complete directory iterator materializes every entry in the
        Python heap.  Retention is specifically used when a collector has
        already failed, so a large output directory must not turn diagnosis
        into a second memory failure.  The temporary SQLite index keeps the
        directory names on disk and lets SQLite perform the ordering; only
        the current entry and the bounded directory stack are held in Python.
        """
        stack = [("", root)]
        with tempfile.TemporaryDirectory(prefix="retention-scan-", dir=self.root) as temp:
            db = sqlite3.connect(Path(temp) / "entries.sqlite3")
            try:
                db.execute("PRAGMA temp_store=FILE")
                db.execute("CREATE TABLE entries(parent TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(parent, name))")
                db.execute("CREATE TABLE directories(parent TEXT NOT NULL, name TEXT NOT NULL, relative TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(parent, name))")
                while stack:
                    prefix, current = stack.pop()
                    parent = str(current)
                    try:
                        with os.scandir(current) as entries:
                            for entry in entries:
                                if entry.name not in EXCLUDED_FILES:
                                    db.execute("INSERT OR IGNORE INTO entries(parent,name) VALUES(?,?)",
                                               (parent, entry.name))
                        db.commit()
                    except OSError as exc:
                        yield prefix, current, None, exc
                        continue
                    rows = db.execute("SELECT name FROM entries WHERE parent=? ORDER BY name", (parent,))
                    for (name,) in rows:
                        rel = f"{prefix}/{name}" if prefix else name
                        path = current / name
                        try:
                            st = path.lstat()
                        except OSError as exc:
                            yield rel, path, None, exc
                            continue
                        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                            if name not in EXCLUDED_DIRS:
                                db.execute("INSERT OR REPLACE INTO directories(parent,name,relative,path) VALUES(?,?,?,?)",
                                           (parent, name, rel, str(path)))
                            continue
                        yield rel, path, st, None
                    db.execute("DELETE FROM entries WHERE parent=?", (parent,))
                    db.commit()
                    for rel, name, path in db.execute(
                            "SELECT relative,name,path FROM directories WHERE parent=? ORDER BY name DESC", (parent,)):
                        stack.append((rel, Path(path)))
                    db.execute("DELETE FROM directories WHERE parent=?", (parent,))
                    db.commit()
            finally:
                db.close()

    def _regular_entry(self, path: Path, before: os.stat_result, common: dict) -> dict:
        entry = dict(common)
        entry["classification"] = "bounded_source_candidate" if before.st_size <= 32 * 1024 * 1024 else "oversized_unclassified"
        entry["observed_bytes"] = 0
        entry["blob"] = None
        try:
            # Store.blob_put_file opens with O_NOFOLLOW and streams in bounded
            # blocks.  The before/after identity check makes a concurrent
            # mutation explicit instead of presenting a mixed file as exact.
            blob = self.s.blob_put_file(path)
            after = path.lstat()
            # blob_put_file normally produces a physical leaf, but retain the
            # same read contract if a durable failure record points at a
            # recipe-backed artifact.  artifact_size deliberately refuses a
            # corrupt physical leaf instead of falling through to a recipe.
            total = artifact_size(self.s, blob)
            entry["observed_bytes"] = total
            entry["blob"] = blob
            if not _same_identity(before, after) or total != after.st_size:
                entry.update(retention_status="unstable", reason="file_changed_during_retention")
            else:
                entry["retention_status"] = "stored"
                entry["sha256"] = blob
            return entry
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            entry.update(retention_status="not_stored", reason=self._error(exc, "blob"))
            return entry

    def _entry(self, repo: dict, relative: str, path: Path, st: os.stat_result | None, error: BaseException | None) -> dict:
        base = {"repo": repo["id"], "repo_name": repo["name"], "path": relative,
                "kind": _kind(st) if st is not None else "unknown",
                "mode": _mode(st) if st is not None else None,
                "observed_bytes": 0, "blob": None, "sha256": None}
        if error is not None or st is None:
            base.update(classification="unsafe_metadata_only", retention_status="metadata_only",
                        reason=self._error(error or OSError("metadata unavailable"), "stat"))
            return base
        kind = _kind(st)
        if kind == "file":
            return self._regular_entry(path, st, base)
        if kind == "symlink":
            try:
                base.update(classification="unsafe_metadata_only", retention_status="metadata_only",
                            reason="symlink_not_followed", target=os.readlink(path))
            except OSError as exc:
                base.update(classification="unsafe_metadata_only", retention_status="metadata_only",
                            reason=self._error(exc, "readlink"))
            return base
        base.update(classification="unsafe_metadata_only", retention_status="metadata_only",
                    reason=f"{kind}_not_read")
        return base

    def _store_entry_chunk(self, entries: list[dict]) -> dict:
        body = {"format": ENTRY_CHUNK_FORMAT, "entries": entries}
        blob = self.s.blob_put(canonical(body))
        return {"blob": blob, "count": len(entries), "bytes": sum(e.get("observed_bytes", 0) or 0 for e in entries),
                "first": entries[0]["path"] if entries else None, "last": entries[-1]["path"] if entries else None}

    def _store_entry_page(self, chunks: list[dict], count: int, first: str | None, last: str | None) -> dict:
        body = {"format": ENTRY_PAGE_FORMAT, "chunks": chunks, "entry_count": count}
        blob = self.s.blob_put(canonical(body))
        return {"blob": blob, "chunks": len(chunks), "entry_count": count, "first": first, "last": last}

    def _store_entry_index(self, pages: list[dict], previous: str | None, start: int) -> str:
        body = {"format": ENTRY_INDEX_FORMAT, "start": start, "pages": pages, "previous": previous}
        return self.s.blob_put(canonical(body))

    def retain(self, *, project, run, task, epoch, role, snapshot_digest, collector_failure,
               work: Path, repos: list[dict], ignored=(), metadata_inventory=None) -> dict:
        """Scan a stopped worker tree and commit a bounded manifest."""
        marker = self._read_marker(run)
        if marker is None:
            raise Fault("retention_marker_missing", "Collector failure has no durable retention marker")
        # Only the current bounded chunk is held in memory.  The complete
        # inventory is represented by content-addressed chunk/page blobs.
        entry_total = 0
        error_count = 0
        error_sample: list[dict] = []
        ignored = set(ignored or ())
        pending_metadata = dict(metadata_inventory or {})
        chunk_descriptors: list[dict] = []
        page_index_tail = None
        page_index_buffer: list[dict] = []
        page_total = 0
        chunk: list[dict] = []
        page_count = 0
        page_first = page_last = None
        inventory_complete = True

        def index_page(descriptor: dict) -> None:
            nonlocal page_index_tail, page_total, page_index_buffer
            page_index_buffer.append(descriptor)
            page_total += 1
            if len(page_index_buffer) >= ENTRY_INDEX_SIZE:
                page_index_tail = self._store_entry_index(
                    page_index_buffer, page_index_tail, page_total - len(page_index_buffer))
                page_index_buffer = []

        def record_error(value: dict) -> None:
            nonlocal error_count
            error_count += 1
            if len(error_sample) < ERROR_SAMPLE_LIMIT:
                error_sample.append(value)

        def flush_chunk():
            nonlocal chunk, page_count, page_first, page_last
            if not chunk:
                return
            descriptor = self._store_entry_chunk(chunk)
            chunk_descriptors.append(descriptor)
            page_count += descriptor["count"]
            page_first = page_first or descriptor["first"]
            page_last = descriptor["last"]
            chunk = []
            if len(chunk_descriptors) >= ENTRY_PAGE_CHUNKS:
                flush_page()

        def flush_page():
            nonlocal chunk_descriptors, page_count, page_first, page_last
            if not chunk_descriptors:
                return
            index_page(self._store_entry_page(chunk_descriptors, page_count, page_first, page_last))
            chunk_descriptors = []
            page_count = 0
            page_first = page_last = None

        for repo in repos:
            root = work / repo["name"]
            try:
                need(root.is_dir() and not root.is_symlink(), "missing_repository",
                     "Worker repository root is missing during failure retention", repo["name"])
                for relative, path, st, error in self._iter_paths(root):
                    if relative in ignored:
                        continue
                    if st is None or error is not None:
                        inventory_complete = False
                    entry = self._entry(repo, relative, path, st, error)
                    metadata_entry = pending_metadata.pop((repo["id"], relative), None)
                    if metadata_entry is not None:
                        need(entry.get("kind") == metadata_entry.get("kind"), "integrity_error",
                             "Pending recovery metadata differs from the restored filesystem entry", relative)
                        entry = metadata_entry
                    entry_total += 1
                    if entry.get("retention_status") not in {"stored", "metadata_only"}:
                        record_error({"repo": repo["id"], "path": relative, "reason": entry.get("reason")})
                    chunk.append(entry)
                    if len(chunk) >= ENTRY_CHUNK_SIZE:
                        flush_chunk()
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                inventory_complete = False
                record_error({"repo": repo["id"], "path": "", "reason": self._error(exc, "inventory")})
        # A normal backup cannot recreate FIFOs, sockets, devices, or unsafe
        # links as executable filesystem objects.  Their exact metadata is
        # carried by the backup sidecar and is therefore retained in the
        # diagnostic manifest even when the restored raw tree contains no
        # corresponding special node.
        for key in sorted(pending_metadata, key=lambda value: (value[0], value[1])):
            entry = pending_metadata[key]
            entry_total += 1
            chunk.append(entry)
            if len(chunk) >= ENTRY_CHUNK_SIZE:
                flush_chunk()
        flush_chunk()
        flush_page()
        if page_index_buffer:
            page_index_tail = self._store_entry_index(
                page_index_buffer, page_index_tail, page_total - len(page_index_buffer))
            page_index_buffer = []
        complete = inventory_complete and error_count == 0
        marker_created = marker.get("manifest_created") or marker.get("created") or timestamp()
        marker["manifest_created"] = marker_created
        collector_failure = self._bounded(collector_failure)
        manifest = {
            "format": FORMAT, "project": project, "run": run, "task": task, "epoch": epoch,
            "role": role, "input_snapshot": snapshot_digest, "collector_failure": collector_failure,
            "complete": complete, "inventory_complete": inventory_complete,
            "entry_total": entry_total,
            "entry_page_index": {"blob": page_index_tail, "pages": page_total} if page_index_tail else None,
            "entry_page_size": ENTRY_PAGE_CHUNKS * ENTRY_CHUNK_SIZE,
            "entry_chunk_size": ENTRY_CHUNK_SIZE,
            "retention_error": error_sample or None,
            "retention_error_count": error_count,
            "retention_error_sample": error_sample or None,
            "created": marker_created, "adoptable": False, "requires_reassessment": True,
        }
        encoded = canonical(manifest)
        need(len(encoded) <= MAX_MANIFEST_BYTES, "retention_manifest_too_large",
             "Failure manifest index exceeds its explicit bounded size")
        manifest_blob = self.s.blob_put(encoded)
        summary = {
            "format": FORMAT, "manifest_blob": manifest_blob, "manifest_digest": digest(manifest),
            "status": "complete" if complete else "partial", "complete": complete,
            "inventory_complete": inventory_complete, "entry_total": entry_total,
            "entry_pages": page_total, "retention_error": error_sample or None,
            "retention_error_count": error_count,
            "retention_error_sample": error_sample or None,
            "adoptable": False, "requires_reassessment": True,
            "detail_pointer": {"route": "run.recovery", "run": run, "manifest": manifest_blob},
        }
        marker.update(state="durable" if complete else "pending", collector_failure=collector_failure,
                      manifest_blob=manifest_blob, summary=summary,
                      staged_root=marker.get("staged_root"))
        self._write_marker(marker)
        return summary

    def pending_summary(self, run: str, error: BaseException | dict) -> dict:
        marker = self._read_marker(run) or {"run": run}
        detail = self._bounded(error if isinstance(error, dict) else self._error(error, "retention"))
        summary = {"format": FORMAT, "status": "pending", "complete": False,
                    "inventory_complete": False, "entry_total": None, "manifest_blob": None,
                    "retention_error": [detail], "retention_error_count": 1,
                    "retention_error_sample": [detail], "adoptable": False,
                    "requires_reassessment": True,
                    "detail_pointer": {"route": "run.recovery", "run": run}}
        marker.update(state="pending", summary=summary, retention_error=detail)
        try:
            self._write_marker(marker)
        except BaseException as marker_error:
            if isinstance(marker_error, (KeyboardInterrupt, SystemExit)):
                raise
            # The caller can still commit the original failed receipt.  The
            # worker cleanup path will keep the tree when this final marker
            # write is itself unavailable (for example, a full disk).
            marker_detail = self._error(marker_error, "pending_marker")
            summary["retention_error_count"] = 2
            summary["retention_error_sample"] = [detail, marker_detail][:ERROR_SAMPLE_LIMIT]
            summary["retention_error"] = summary["retention_error_sample"]
        return summary

    def mark_receipt(self, run: str, receipt: str, summary: dict | None) -> None:
        marker = self._read_marker(run)
        if marker is None:
            return
        marker.update(state="receipt_committed", receipt=receipt, summary=summary)
        self._write_marker(marker)

    def stage(self, run: str, reason: BaseException | dict | None = None) -> dict | None:
        marker = self._read_marker(run)
        if marker is None:
            return None
        root = Path(marker.get("worker_root", "")) if marker.get("worker_root") else None
        staged = Path(marker["staged_root"]) if marker.get("staged_root") else self.staging / run
        if root and root.exists() and not marker.get("staged_root"):
            try:
                staged.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Publish the destination before moving the tree.  If the
                # second marker write or the process itself fails after the
                # move, startup can still find the staged path.
                marker["staged_root"] = str(staged)
                marker["state"] = "staging"
                self._write_marker(marker)
                os.replace(root, staged)
                marker["worker_root"] = None
            except OSError as exc:
                # The marker remains durable even if a cross-device move or
                # permissions failure prevents staging.  Never delete root.
                if root.exists():
                    marker["staged_root"] = None
                marker["retention_error"] = self._error(exc, "stage")
        if reason is not None:
            marker["retention_error"] = self._bounded(reason if isinstance(reason, dict) else self._error(reason, "cleanup"))
        marker["state"] = "pending_staging"
        self._write_marker(marker)
        return marker

    def cleanup(self, run: str) -> None:
        path = self._marker_path(run)
        path.unlink(missing_ok=True)
        self._inventory_path(run).unlink(missing_ok=True)

    def has_pending(self) -> bool:
        return any(path.is_file() and not path.is_symlink() for path in self.pending.glob("*.json"))

    def marker_rows(self) -> list[dict]:
        result = []
        for path in sorted(self.pending.glob("*.json")):
            marker = self._read_marker(path.stem)
            if marker:
                result.append(marker)
        return result

    def summary(self, run: str) -> dict | None:
        receipt = self.s.one("SELECT body FROM receipts WHERE run=?", (run,))
        receipt_summary = None
        if receipt:
            body = parse_json(receipt["body"])
            if body.get("recovery_artifacts") is not None:
                receipt_summary = body["recovery_artifacts"]
        # A crash before receipt commit is represented by an immutable event or
        # the pending marker; neither invents a receipt or an accepted result.
        events = self.s.all("SELECT body FROM events WHERE kind IN ('retention_reconciled','retention_pending') ORDER BY seq", ())
        for row in reversed(events):
            body = parse_json(row["body"])
            if body.get("run") == run and body.get("recovery_artifacts"):
                event_summary = body["recovery_artifacts"]
                if receipt_summary is None or receipt_summary.get("status") in {"pending", "partial"}:
                    return event_summary
        if receipt_summary is not None:
            return receipt_summary
        marker = self._read_marker(run)
        return marker.get("summary") if marker else None

    def _manifest(self, summary: dict) -> dict:
        blob = summary.get("manifest_blob")
        need(blob, "recovery_unavailable", "No durable failure manifest is available")
        manifest = parse_json(self.s.blob_get(blob), limit=MAX_MANIFEST_BYTES)
        need(manifest.get("format") == FORMAT, "integrity_error", "Failure manifest format differs")
        if "manifest_digest" in manifest:
            unsigned = {k: v for k, v in manifest.items() if k != "manifest_digest"}
            need(digest(unsigned) == manifest["manifest_digest"],
                 "integrity_error", "Failure manifest digest differs")
        return manifest

    def _page_descriptors(self, manifest: dict):
        """Yield manifest pages in original order using disk-backed reversal."""
        if isinstance(manifest.get("entry_pages"), list) and manifest.get("entry_pages"):
            yield from manifest["entry_pages"]
            return
        index = manifest.get("entry_page_index") or {}
        blob = index.get("blob")
        if not blob:
            return
        with tempfile.TemporaryDirectory(prefix="retention-pages-", dir=self.root) as temp:
            db = sqlite3.connect(Path(temp) / "pages.sqlite3")
            try:
                db.execute("CREATE TABLE pages(position INTEGER PRIMARY KEY, body TEXT NOT NULL)")
                seen = set()
                while blob:
                    need(blob not in seen, "integrity_error", "Failure manifest index contains a cycle")
                    seen.add(blob)
                    body = parse_json(self.s.blob_get(blob), limit=4 * 1024 * 1024)
                    need(body.get("format") == ENTRY_INDEX_FORMAT,
                         "integrity_error", "Failure manifest index format differs")
                    start = body.get("start")
                    pages = body.get("pages")
                    need(type(start) is int and start >= 0 and isinstance(pages, list),
                         "integrity_error", "Failure manifest index is malformed")
                    for offset, page in enumerate(pages):
                        db.execute("INSERT INTO pages(position,body) VALUES(?,?)",
                                   (start + offset, canonical(page).decode()))
                    db.commit()
                    blob = body.get("previous")
                for row in db.execute("SELECT body FROM pages ORDER BY position"):
                    yield parse_json(row[0], limit=1024 * 1024)
            finally:
                db.close()

    def detail(self, actor, run: str, offset=0, limit=100, expected_digest=None) -> dict:
        number(offset, "offset", 0, 10**12, integer=True)
        number(limit, "limit", 1, MAX_DETAIL_LIMIT, integer=True)
        row = self.s.one("SELECT project,task FROM runs WHERE id=?", (run,), True)
        actor.require("owner", "agent", "reviewer", "observer", project=row["project"], task=row["task"])
        self._project_scope(actor, row["project"])
        summary = self.summary(run)
        need(summary, "recovery_unavailable", "No failed-artifact retention record exists for this run")
        manifest = self._manifest(summary)
        stamp = summary.get("manifest_digest") or digest(manifest)
        need(expected_digest is None or expected_digest == stamp, "stale_recovery", "Failure manifest changed between pages")
        total = manifest.get("entry_total", 0)
        need(type(total) is int and total >= 0, "integrity_error", "Failure manifest entry total is invalid")
        selected = []
        cursor = 0
        for page in self._page_descriptors(manifest):
            page_body = parse_json(self.s.blob_get(page["blob"]), limit=4 * 1024 * 1024)
            for chunk in page_body.get("chunks", []):
                if cursor + chunk.get("count", 0) <= offset:
                    cursor += chunk.get("count", 0); continue
                chunk_body = parse_json(self.s.blob_get(chunk["blob"]), limit=16 * 1024 * 1024)
                for entry in chunk_body.get("entries", []):
                    if cursor >= offset and len(selected) < limit:
                        selected.append(entry)
                    cursor += 1
                    if len(selected) >= limit:
                        break
                if len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        end = offset + len(selected)
        return {"run": run, "manifest": summary.get("manifest_blob"), "manifest_digest": stamp,
                "status": summary.get("status", "partial"), "complete": bool(manifest.get("complete")),
                "entries": selected, "total": total,
                "next_offset": end if end < total else None, "adoptable": False,
                "requires_reassessment": True,
                "notice": "Failure evidence is diagnostic only; it never adopts a candidate or changes a receipt."}

    def read(self, actor, run: str, repo: str, path: str, expected_digest: str, offset=0, limit=65536, expected_manifest=None) -> dict:
        number(offset, "offset", 0, 10**12, integer=True)
        number(limit, "limit", 1, 1048576, integer=True)
        row = self.s.one("SELECT project,task FROM runs WHERE id=?", (run,), True)
        actor.require("owner", "agent", "reviewer", "observer", project=row["project"], task=row["task"])
        self._project_scope(actor, row["project"])
        summary = self.summary(run)
        need(summary, "recovery_unavailable", "No failed-artifact retention record exists for this run")
        manifest = self._manifest(summary)
        stamp = summary.get("manifest_digest") or digest(manifest)
        need(expected_manifest is None or expected_manifest == stamp,
             "stale_recovery", "Failure manifest changed between pages")
        entry = None
        for page in self._page_descriptors(manifest):
            page_body = parse_json(self.s.blob_get(page["blob"]), limit=4 * 1024 * 1024)
            for chunk in page_body.get("chunks", []):
                chunk_body = parse_json(self.s.blob_get(chunk["blob"]), limit=16 * 1024 * 1024)
                entry = next((item for item in chunk_body.get("entries", [])
                              if item.get("repo") == repo and item.get("path") == path), None)
                if entry is not None:
                    break
            if entry is not None:
                break
        need(entry, "not_found", "File is not in the recorded failed-artifact manifest")
        need(entry.get("kind") == "file" and entry.get("retention_status") == "stored" and entry.get("blob"),
             "not_regular_file", "This failed artifact has metadata only or was not durably stored")
        need(entry["blob"] == expected_digest, "stale_recovery", "Expected failed-artifact digest differs")
        blob = entry["blob"]
        need(blob == entry["blob"], "integrity_error", "Failed artifact digest is malformed")
        # Keep one fully verified session for the digest pass and requested
        # page.  A recipe alias cannot be accepted by a metadata-only size
        # call, and repeated pages do not rehash the entire closure here.
        with open_artifact_session(self.s, blob) as session:
            total = session.size
            hashed = hashlib.sha256()
            verify_offset = 0
            while verify_offset < total:
                block = session.read_range(verify_offset, min(1024 * 1024, total - verify_offset))
                need(block, "integrity_error", "Content-addressed failed artifact ended during verification")
                hashed.update(block); verify_offset += len(block)
            need(hashed.hexdigest() == blob, "integrity_error", "Content-addressed failed artifact was modified")
            data = session.read_range(offset, min(limit, max(0, total - offset)))
        end = offset + len(data)
        return {"run": run, "repo": repo, "path": path, "sha256": entry["blob"],
                "base64": __import__("base64").b64encode(data).decode("ascii"),
                "total_bytes": total, "next_offset": end if end < total else None,
                "adopted_by_read": False}

    def _project_scope(self, actor, project: str) -> None:
        if actor.role != "owner":
            need(actor.project == project, "forbidden", "Capability belongs to another project")

    def reconcile(self) -> dict:
        """Revisit pending markers without making a new receipt or acceptance."""
        results = []
        for marker in self.marker_rows():
            run = marker.get("run")
            state = marker.get("state")
            summary_state = (marker.get("summary") or {}).get("status")
            if not run or (state not in {"running", "pending", "pending_staging", "staging", "marker_error", "durable", "receipt_committed"}
                           and summary_state != "partial"):
                continue
            root = Path(marker.get("staged_root") or marker.get("worker_root", ""))
            work = root / "work"
            try:
                pending_inventory = self._read_pending_inventory(run, marker)
                if pending_inventory is not None and work.is_dir():
                    self._validate_pending_inventory_raw(pending_inventory, marker)
                metadata_inventory = self._metadata_entries(pending_inventory, marker.get("repos") or [])
                if summary_state == "complete" and not work.is_dir():
                    # A crash can occur after the durable event and raw-tree
                    # cleanup but before the marker unlink.  The event is the
                    # immutable record; remove only this stale marker.
                    self.cleanup(run)
                    results.append({"run": run, "status": "complete", "reconciled": True})
                    continue
                if work.is_dir() and marker.get("repos"):
                    summary = self.retain(project=marker.get("project"), run=run, task=marker.get("task"),
                                          epoch=marker.get("epoch"), role=marker.get("role", "unknown"),
                                          snapshot_digest=marker.get("input_snapshot"),
                                          collector_failure=marker.get("collector_failure"), work=work,
                                          repos=marker["repos"], metadata_inventory=metadata_inventory)
                    body = {"run": run, "recovery_artifacts": summary, "reconciled": True}
                    with self.s.transaction():
                        if self.sec and not self.s.one(
                            "SELECT seq FROM events WHERE kind='retention_reconciled' "
                            "AND json_extract(body,'$.run')=? "
                            "AND json_extract(body,'$.recovery_artifacts.manifest_digest')=?",
                            (run, summary.get("manifest_digest"))):
                            self.sec.event(marker.get("project"), "retention_reconciled", "recovery", body)
                    if summary.get("status") == "complete":
                        raw_root = marker.get("staged_root") or marker.get("worker_root")
                        try:
                            if raw_root and Path(raw_root).exists():
                                shutil.rmtree(raw_root, ignore_errors=False)
                            self.cleanup(run)
                        except BaseException as cleanup_error:
                            if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
                                raise
                            marker["state"] = "durable"
                            marker["retention_error"] = self._error(cleanup_error, "reconcile_cleanup")
                            self._write_marker(marker)
                    results.append(body)
                else:
                    results.append({"run": run, "status": "pending", "reason": "staging_missing"})
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                error = self._error(exc, "reconcile")
                marker["state"] = "pending"
                marker["retention_error"] = error
                self._write_marker(marker)
                body = {"run": run, "status": "pending", "retention_error": error}
                with self.s.transaction():
                    if self.sec and not self.s.one(
                        "SELECT seq FROM events WHERE kind='retention_pending' AND json_extract(body,'$.run')=? "
                        "AND json_extract(body,'$.retention_error.code')=?",
                        (run, error.get("code"))):
                        self.sec.event(marker.get("project"), "retention_pending", "recovery", body)
                results.append(body)
        return {"scanned": len(results), "items": results, "writes": len(results)}
