"""Flat, content-addressed representations for operational backup artifacts.

The ordinary blob namespace stores immutable physical leaves.  A completed
backup ZIP is represented separately by an exact ordered recipe whose segments
point only at those physical leaves.  Recipes never point at another recipe;
that invariant keeps range reads, restore and garbage collection finite.
"""
from __future__ import annotations

import hashlib
import ctypes
import errno
import os
import stat
import struct
import tempfile
import threading
import zipfile
from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import Fault, atomic_write, canonical, digest, need, parse_json


RECIPE_FORMAT = "daikibo.backup-byte-recipe.v1"
LEAF_BYTES = 1024 * 1024
MAX_FILE_BYTES = 64 * 1024**3
# The operational backup limit is an expanded-payload limit.  A completed
# stored ZIP also contains member headers, descriptors, the central directory,
# and the manifest member.  Keep those limits separate: using the payload
# limit as a whole-artifact limit rejects otherwise legal near-boundary
# backups.  The representability ceiling below is deliberately derived from
# ZIP's bounded 16-bit name/extra/comment fields and the existing manifest
# bound; it is a hostile-input bound, not a new application payload policy.
MAX_LOGICAL_BACKUP_BYTES = 256 * 1024**3
MAX_BACKUP_MANIFEST_BYTES = 64 * 1024**2
MAX_ZIP_MEMBER_NAME_BYTES = 0xFFFF
MAX_ZIP_MEMBER_COUNT = MAX_BACKUP_MANIFEST_BYTES
_ZIP_MEMBER_OVERHEAD = (30 + MAX_ZIP_MEMBER_NAME_BYTES + MAX_ZIP_MEMBER_NAME_BYTES
                        + 46 + MAX_ZIP_MEMBER_NAME_BYTES + MAX_ZIP_MEMBER_NAME_BYTES
                        + 24)
MAX_ZIP_STRUCTURE_BYTES = (MAX_ZIP_MEMBER_COUNT * _ZIP_MEMBER_OVERHEAD
                           + 2 * MAX_ZIP_MEMBER_NAME_BYTES + 22 + 2 * 64 * 1024**2)
MAX_ARTIFACT_BYTES = MAX_LOGICAL_BACKUP_BYTES + MAX_BACKUP_MANIFEST_BYTES + MAX_ZIP_STRUCTURE_BYTES
# A recipe records bounded ZIP structure in addition to the canonical manifest.
# Four manifest bounds leave representation headroom without changing the
# expanded payload limit; MAX_SEGMENTS is only a cheap early shape guard and
# the byte bound remains authoritative for the serialized recipe.
MAX_RECIPE_BYTES = 4 * MAX_BACKUP_MANIFEST_BYTES
MAX_SEGMENTS = MAX_RECIPE_BYTES // 48
_HEX = frozenset("0123456789abcdef")


def _sha(value: Any, field: str = "sha256") -> str:
    need(isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value),
         "invalid_recipe", f"{field} must be a lowercase SHA-256")
    return value


def _root(store_or_root: Any) -> Path:
    value = getattr(store_or_root, "home", store_or_root)
    return Path(value).absolute()


def _blobs(store_or_root: Any) -> Path:
    value = getattr(store_or_root, "blobs", None)
    return Path(value).absolute() if value is not None else _root(store_or_root) / "blobs"


def _recipes(store_or_root: Any) -> Path:
    value = getattr(store_or_root, "backup_recipes", None)
    return Path(value).absolute() if value is not None else _root(store_or_root) / "backup-recipes"


def _physical_path(store_or_root: Any, blob: str) -> Path:
    return _blobs(store_or_root) / blob[:2] / blob[2:]


def _regular(path: Path, field: str) -> Path:
    need(path.is_file() and not path.is_symlink(), "integrity_error", f"{field} is not a regular file", str(path))
    return path


def _read_leaf(store_or_root: Any, blob: str, cache: dict[str, tuple[Path, int, str]] | None = None) -> tuple[Path, int, str]:
    blob = _sha(blob, "segment.blob")
    path = _regular(_physical_path(store_or_root, blob), "recipe physical leaf")
    size = path.stat().st_size
    # Ordinary CAS already permits an empty blob.  Recipes never emit a
    # zero-byte segment, but a zero-byte ordinary payload still belongs in a
    # backup and must retain its historical blob behavior.
    need(0 <= size <= MAX_FILE_BYTES, "integrity_error", "Physical leaf has an invalid size", blob)
    if cache is not None and blob in cache:
        previous = cache[blob]
        need(previous[0] == path and previous[1] == size, "integrity_error", "Recipe physical leaf changed", blob)
        return previous
    with path.open("rb") as stream:
        observed = hashlib.file_digest(stream, "sha256").hexdigest()
    need(observed == blob, "integrity_error", "Recipe physical leaf hash differs", blob)
    value = (path, size, observed)
    if cache is not None:
        cache[blob] = value
    return value


@dataclass(frozen=True)
class RecipeMetadata:
    format: str
    artifact_sha256: str
    artifact_bytes: int
    backup_id: str
    segments: tuple[dict[str, Any], ...]
    physical_refs: tuple[str, ...]


def _normalize_document(store_or_root: Any, document: Any, archive_sha: str | None = None,
                        leaf_lookup=None) -> RecipeMetadata:
    """Validate recipe shape/ranges and optionally use already-open leaves.

    ``leaf_lookup`` is private to the verified read session.  The ordinary
    path-based validator keeps its historical hash checks; a session supplies
    opened, separately hashed leaves so the ordered artifact hash is computed
    exactly once for that session.
    """
    need(isinstance(document, dict), "invalid_recipe", "Backup recipe must be an object")
    required = {"format", "artifact_sha256", "artifact_bytes", "backup_id", "segments"}
    need(set(document) == required, "invalid_recipe", "Backup recipe has an unexpected shape")
    need(document["format"] == RECIPE_FORMAT, "invalid_recipe", "Unsupported backup recipe format")
    actual_sha = _sha(document["artifact_sha256"], "artifact_sha256")
    if archive_sha is not None:
        need(actual_sha == _sha(archive_sha, "archive_sha256"), "invalid_recipe", "Recipe filename and artifact digest differ")
    size = document["artifact_bytes"]
    need(type(size) is int and 0 < size <= MAX_ARTIFACT_BYTES, "invalid_recipe", "Recipe artifact size is outside the backup limit")
    backup_id = document["backup_id"]
    need(isinstance(backup_id, str) and backup_id and len(backup_id) <= 512 and "\x00" not in backup_id,
         "invalid_recipe", "Recipe backup_id is invalid")
    raw_segments = document["segments"]
    need(isinstance(raw_segments, list) and 0 < len(raw_segments) <= MAX_SEGMENTS,
         "invalid_recipe", "Recipe segment count is outside the bounded limit")
    total = 0
    segments: list[dict[str, Any]] = []
    refs: list[str] = []
    ref_seen: set[str] = set()
    leaf_cache: dict[str, tuple[Path, int, str]] = {}
    for index, segment in enumerate(raw_segments):
        need(isinstance(segment, dict) and set(segment) == {"blob", "offset", "bytes"},
             "invalid_recipe", "Recipe segment has an unexpected shape", index)
        blob = _sha(segment["blob"], "segment.blob")
        offset = segment["offset"]
        length = segment["bytes"]
        need(type(offset) is int and offset >= 0 and type(length) is int and 0 < length <= MAX_FILE_BYTES,
             "invalid_recipe", "Recipe segment range is invalid", index)
        if leaf_lookup is None:
            _, leaf_size, _ = _read_leaf(store_or_root, blob, leaf_cache)
        else:
            _, leaf_size, _ = leaf_lookup(blob)
        need(offset + length <= leaf_size, "integrity_error", "Recipe segment exceeds its physical leaf", index)
        if blob not in ref_seen:
            ref_seen.add(blob)
            refs.append(blob)
        segments.append({"blob": blob, "offset": offset, "bytes": length})
        total += length
        need(total <= MAX_ARTIFACT_BYTES, "invalid_recipe", "Recipe expanded size exceeds the backup limit")
    need(total == size, "invalid_recipe", "Recipe segment sizes do not equal artifact_bytes")
    return RecipeMetadata(RECIPE_FORMAT, actual_sha, size, backup_id, tuple(segments), tuple(refs))


def _recipe_path(store_or_root: Any, archive_sha: str) -> Path:
    archive_sha = _sha(archive_sha, "archive_sha256")
    root = _recipes(store_or_root)
    if root.exists():
        need(root.is_dir() and not root.is_symlink(), "integrity_error", "Backup recipe namespace is not a real directory")
    return root / f"{archive_sha}.json"


def _validate_document(store_or_root: Any, document: Any, archive_sha: str | None = None,
                       *, verify_artifact: bool = True) -> RecipeMetadata:
    metadata = _normalize_document(store_or_root, document, archive_sha)
    if not verify_artifact:
        return metadata
    cache: dict[str, tuple[Path, int, str]] = {}
    artifact_hash = hashlib.sha256()
    for index, segment in enumerate(metadata.segments):
        path, _, _ = _read_leaf(store_or_root, segment["blob"], cache)
        with path.open("rb") as stream:
            stream.seek(segment["offset"])
            remaining = segment["bytes"]
            while remaining:
                block = stream.read(min(LEAF_BYTES, remaining))
                need(block, "integrity_error", "Recipe physical leaf ended before its segment", index)
                artifact_hash.update(block)
                remaining -= len(block)
    need(artifact_hash.hexdigest() == metadata.artifact_sha256, "integrity_error", "Recipe reassembly hash differs from artifact_sha256")
    return metadata


def _identity(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the identity and freshness fields used by a read session."""
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode), st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _namespace_member(path: Path, namespace: Path, field: str) -> os.stat_result:
    """Check every path component without resolving a symlink."""
    namespace = namespace.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(namespace)
    except ValueError as exc:
        raise Fault("integrity_error", f"{field} is outside its namespace", str(path)) from exc
    try:
        root_stat = os.lstat(namespace)
    except OSError as exc:
        raise Fault("missing_evidence", f"{field} namespace is missing", str(namespace)) from exc
    need(stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISLNK(root_stat.st_mode),
         "integrity_error", f"{field} namespace is not a real directory", str(namespace))
    current = namespace
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            item = os.lstat(current)
        except OSError as exc:
            raise Fault("missing_evidence", f"{field} is missing", str(path)) from exc
        if index < len(parts) - 1:
            need(stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode),
                 "integrity_error", f"{field} parent is not a real directory", str(current))
    need(parts, "integrity_error", f"{field} path is empty", str(path))
    return item


class _OpenBlob:
    """An O_NOFOLLOW handle plus both path and descriptor observations."""

    def __init__(self, path: Path, fd: int, field: str, namespace: Path):
        self.path = path
        self.fd = fd
        self.field = field
        self.namespace = namespace
        self.fd_identity = _identity(os.fstat(fd))
        self.path_identity = _identity(_namespace_member(path, namespace, field))
        need(self.fd_identity == self.path_identity and stat.S_ISREG(self.fd_identity[2]),
             "integrity_error", f"{field} changed while opening", str(path))
        self.size = self.fd_identity[3]

    @classmethod
    def open(cls, path: Path, namespace: Path, field: str) -> "_OpenBlob":
        _namespace_member(path, namespace, field)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            code = "integrity_error" if exc.errno in {errno.ELOOP, errno.EACCES, errno.EPERM} else "missing_evidence"
            raise Fault(code, f"Unable to open {field}", str(path)) from exc
        try:
            return cls(path, fd, field, namespace)
        except BaseException:
            os.close(fd)
            raise

    def check_stable(self) -> None:
        try:
            current_fd = _identity(os.fstat(self.fd))
            current_path = _identity(_namespace_member(self.path, self.namespace, self.field))
        except OSError as exc:
            raise Fault("integrity_error", f"{self.field} disappeared during a read", str(self.path)) from exc
        need(current_fd == self.fd_identity and current_path == self.path_identity and current_fd == current_path,
             "integrity_error", f"{self.field} changed during a read", str(self.path))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class _ValidatedArtifact:
    """Immutable validation result retained by one manager entry."""

    def __init__(self, manager: "ArtifactSessionManager", artifact_sha: str):
        self.manager = manager
        self.artifact_sha = artifact_sha
        self.size = 0
        self.kind = ""
        self.metadata: RecipeMetadata | None = None
        self.recipe_file: _OpenBlob | None = None
        self.recipe_bytes_sha: str | None = None
        self.leaves: dict[str, _OpenBlob] = {}
        self.segments: tuple[tuple[_OpenBlob, int, int, int], ...] = ()
        self.generation = 0
        self.refs = 0
        self.cached = False
        self.invalidated = False
        self.validation_bytes = 0
        self.range_bytes = 0

    def close(self) -> None:
        handles: list[_OpenBlob] = list(self.leaves.values())
        if self.recipe_file is not None:
            handles.append(self.recipe_file)
        seen: set[int] = set()
        for handle in handles:
            if id(handle) not in seen:
                seen.add(id(handle))
                handle.close()
        self.leaves.clear()
        self.recipe_file = None

    def _read(self, handle: _OpenBlob, offset: int, length: int, *, validation: bool | None) -> bytes:
        need(0 <= offset <= handle.size and 0 <= length <= handle.size - offset,
             "integrity_error", "Read exceeded the observed physical file", str(handle.path))
        try:
            value = os.pread(handle.fd, length, offset)
        except OSError as exc:
            raise Fault("integrity_error", "Physical artifact read failed", str(handle.path)) from exc
        if validation is True:
            self.validation_bytes += len(value)
        elif validation is False:
            self.range_bytes += len(value)
        return value

    def _hash_handle(self, handle: _OpenBlob) -> str:
        """Hash one opened leaf while retaining observable validation bytes."""
        duplicate = os.dup(handle.fd)
        try:
            with os.fdopen(duplicate, "rb", closefd=True) as stream:
                duplicate = -1
                observed = hashlib.file_digest(stream, "sha256").hexdigest()
        finally:
            if duplicate >= 0:
                os.close(duplicate)
        self.validation_bytes += handle.size
        return observed

    def check_identities(self) -> None:
        if self.kind == "physical":
            need(self.leaves, "integrity_error", "Physical artifact session has no handle")
        for handle in self.leaves.values():
            handle.check_stable()
        if self.recipe_file is not None:
            self.recipe_file.check_stable()
            # The recipe's actual bytes are independently bound.  This read is
            # bounded and is separate from the artifact digest claimed inside.
            payload = self._read(self.recipe_file, 0, self.recipe_file.size, validation=None)
            need(hashlib.sha256(payload).hexdigest() == self.recipe_bytes_sha,
                 "integrity_error", "Backup recipe bytes changed during a read")

    def stats(self) -> dict[str, Any]:
        return {"validation_bytes": self.validation_bytes,
                "range_bytes": self.range_bytes,
                "recipe_bytes_sha256": self.recipe_bytes_sha,
                "cache_hit": False}


class _NamespaceMonitor:
    """Small in-process inotify monitor; unsupported backends use full validation."""

    _IN_ACCESS = 0x00000001
    _IN_MODIFY = 0x00000002
    _IN_ATTRIB = 0x00000004
    _IN_CLOSE_WRITE = 0x00000008
    _IN_MOVED_FROM = 0x00000040
    _IN_MOVED_TO = 0x00000080
    _IN_CREATE = 0x00000100
    _IN_DELETE = 0x00000200
    _IN_DELETE_SELF = 0x00000400
    _IN_MOVE_SELF = 0x00000800
    _IN_UNMOUNT = 0x00002000
    _IN_Q_OVERFLOW = 0x00004000
    _IN_IGNORED = 0x00008000
    _IN_ISDIR = 0x40000000
    _MASK = (_IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE | _IN_MOVED_FROM | _IN_MOVED_TO |
             _IN_CREATE | _IN_DELETE | _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT |
             _IN_Q_OVERFLOW | _IN_IGNORED)
    _EVENT = struct.Struct("iIII")

    def __init__(self, blobs: Path, recipes: Path):
        self.fd = -1
        self.supported = False
        self._wd_paths: dict[int, Path] = {}
        self.blobs = blobs
        self.recipes = recipes
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = getattr(libc, "inotify_init1")
            add = getattr(libc, "inotify_add_watch")
            init.argtypes = [ctypes.c_int]
            init.restype = ctypes.c_int
            add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            add.restype = ctypes.c_int
            fd = init(os.O_CLOEXEC | os.O_NONBLOCK)
            if fd < 0:
                return
            self.fd = fd
            self._add = add
            if not self._watch(recipes) or not self._watch(blobs):
                self.close()
                return
            if blobs.is_dir() and not blobs.is_symlink():
                for child in blobs.iterdir():
                    if child.is_dir() and not child.is_symlink() and not self._watch(child):
                        self.close()
                        return
            self.supported = True
        except (AttributeError, OSError, TypeError, ValueError):
            self.close()

    def _watch(self, path: Path) -> bool:
        if self.fd < 0 or not path.is_dir() or path.is_symlink():
            return False
        wd = self._add(self.fd, os.fsencode(str(path)), self._MASK)
        if wd < 0:
            return False
        self._wd_paths[wd] = path
        return True

    def drain(self) -> bool:
        if not self.supported:
            return False
        changed = False
        try:
            while True:
                payload = os.read(self.fd, 1024 * 1024)
                if not payload:
                    break
                cursor = 0
                while cursor + self._EVENT.size <= len(payload):
                    wd, mask, _, name_len = self._EVENT.unpack_from(payload, cursor)
                    cursor += self._EVENT.size
                    raw_name = payload[cursor:cursor + name_len].rstrip(b"\0")
                    cursor += name_len
                    if mask & self._MASK:
                        changed = True
                    if mask & (self._IN_Q_OVERFLOW | self._IN_IGNORED | self._IN_UNMOUNT
                               | self._IN_DELETE_SELF | self._IN_MOVE_SELF):
                        self.supported = False
                    parent = self._wd_paths.get(wd)
                    if (parent is not None and parent == self.blobs and mask & self._IN_CREATE
                            and mask & self._IN_ISDIR and raw_name):
                        child = parent / os.fsdecode(raw_name)
                        if not self._watch(child):
                            self.supported = False
                            changed = True
            return changed
        except BlockingIOError:
            return changed
        except OSError:
            self.supported = False
            return True

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self._wd_paths.clear()
        self.supported = False


class ArtifactSessionManager:
    """Store-owned verified artifact entries and conservative invalidation."""

    def __init__(self, store_or_root: Any):
        self.store = store_or_root
        self.blobs = _blobs(store_or_root)
        self.recipes = _recipes(store_or_root)
        self._lock = threading.RLock()
        self._generation = 0
        self._cache: OrderedDict[tuple[str, str | None], _ValidatedArtifact] = OrderedDict()
        self._active: set[_ValidatedArtifact] = set()
        self._max_entries = 16
        self._closed = False
        self._ephemeral = False
        self._monitor = _NamespaceMonitor(self.blobs, self.recipes)
        self.cache_supported = self._monitor.supported

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def _invalidate_locked(self) -> None:
        self._generation += 1
        for entry in list(self._cache.values()):
            entry.invalidated = True
            if entry.refs == 0:
                entry.close()
                self._active.discard(entry)
        self._cache = OrderedDict((key, entry) for key, entry in self._cache.items() if entry.refs)

    def _sync_locked(self) -> bool:
        if self._closed:
            raise Fault("closed", "Artifact session manager is closed")
        if self._monitor.supported:
            if self._monitor.drain():
                self._invalidate_locked()
            if not self._monitor.supported:
                self.cache_supported = False
        else:
            self.cache_supported = False
        return self.cache_supported

    def bump(self) -> None:
        with self._lock:
            if not self._closed:
                self._invalidate_locked()

    def _open_entry(self, artifact_sha: str) -> _ValidatedArtifact:
        entry = _ValidatedArtifact(self, artifact_sha)
        try:
            return self._populate_entry(entry, artifact_sha)
        except BaseException:
            # Every descriptor opened by _populate_entry is registered on the
            # entry immediately.  One owner closes all failure paths, including
            # ordered reassembly and post-open identity failures.
            entry.close()
            raise

    def _populate_entry(self, entry: _ValidatedArtifact, artifact_sha: str) -> _ValidatedArtifact:
        physical_path = _physical_path(self.store, artifact_sha)
        # lexists deliberately includes a broken symlink: an unsafe/corrupt
        # physical namespace entry must never fall through to a recipe alias.
        if os.path.lexists(physical_path):
            leaf = _OpenBlob.open(physical_path, self.blobs, "physical artifact")
            entry.kind = "physical"
            entry.size = leaf.size
            entry.leaves[artifact_sha] = leaf
            need(entry._hash_handle(leaf) == artifact_sha,
                 "integrity_error", "Physical artifact hash differs", artifact_sha)
            leaf.check_stable()
            return entry

        recipe_path = _recipe_path(self.store, artifact_sha)
        if not os.path.lexists(recipe_path):
            raise Fault("missing_evidence", "Content-addressed artifact is missing", artifact_sha)
        recipe_file = _OpenBlob.open(recipe_path, self.recipes, "backup recipe")
        entry.kind = "recipe"
        entry.recipe_file = recipe_file
        try:
            need(recipe_file.size <= MAX_RECIPE_BYTES, "backup_capacity", "Backup recipe exceeds the explicit metadata bound")
            payload = entry._read(recipe_file, 0, recipe_file.size, validation=True)
            entry.recipe_bytes_sha = hashlib.sha256(payload).hexdigest()
            document = parse_json(payload, limit=MAX_RECIPE_BYTES)
            # Open and hash every distinct physical leaf before validating
            # segment ranges.  A valid replacement leaf therefore cannot
            # satisfy a stale ordered recipe merely because its size matches.
            raw_segments = document.get("segments") if isinstance(document, dict) else None
            candidate_refs = []
            if isinstance(raw_segments, list):
                for raw in raw_segments:
                    if (isinstance(raw, dict) and isinstance(raw.get("blob"), str)
                            and raw["blob"] not in candidate_refs):
                        candidate_refs.append(raw["blob"])
            for blob in candidate_refs:
                _sha(blob, "segment.blob")
                leaf = _OpenBlob.open(_physical_path(self.store, blob), self.blobs, "recipe physical leaf")
                entry.leaves[blob] = leaf
                need(0 <= leaf.size <= MAX_FILE_BYTES, "integrity_error", "Physical leaf has an invalid size", blob)
                need(entry._hash_handle(leaf) == blob,
                     "integrity_error", "Recipe physical leaf hash differs", blob)
            metadata = _normalize_document(
                self.store, document, artifact_sha,
                leaf_lookup=lambda blob: (entry.leaves[blob].path, entry.leaves[blob].size, blob),
            )
        except BaseException:
            # The encompassing _open_entry owner closes every handle.  Keep
            # this boundary explicit so no inner path takes ownership away.
            raise
        entry.metadata = metadata
        entry.size = metadata.artifact_bytes
        ordered: list[tuple[_OpenBlob, int, int, int]] = []
        prefix = 0
        stream_hash = hashlib.sha256()
        for index, segment in enumerate(metadata.segments):
            leaf = entry.leaves[segment["blob"]]
            offset = segment["offset"]
            remaining = segment["bytes"]
            cursor = offset
            while remaining:
                block = entry._read(leaf, cursor, min(LEAF_BYTES, remaining), validation=True)
                need(block, "integrity_error", "Recipe physical leaf ended before its segment", index)
                stream_hash.update(block)
                remaining -= len(block)
                cursor += len(block)
            ordered.append((leaf, offset, segment["bytes"], prefix))
            prefix += segment["bytes"]
        need(prefix == metadata.artifact_bytes and stream_hash.hexdigest() == metadata.artifact_sha256,
             "integrity_error", "Recipe reassembly hash differs from artifact_sha256")
        entry.segments = tuple(ordered)
        recipe_file.check_stable()
        for leaf in entry.leaves.values():
            leaf.check_stable()
        return entry

    def _release(self, entry: _ValidatedArtifact) -> None:
        with self._lock:
            entry.refs = max(0, entry.refs - 1)
            if (entry.invalidated or not entry.cached) and entry.refs == 0:
                entry.close()
                self._active.discard(entry)

    def _evict_locked(self) -> None:
        while len(self._cache) > self._max_entries:
            key, entry = self._cache.popitem(last=False)
            entry.invalidated = True
            if entry.refs == 0:
                entry.close()
                self._active.discard(entry)

    def open(self, artifact_sha: str) -> "ArtifactSession":
        artifact_sha = _sha(artifact_sha, "archive_sha256")
        with self._lock:
            cache_supported = self._sync_locked()
            if cache_supported:
                for key, entry in list(self._cache.items()):
                    if entry.artifact_sha != artifact_sha or entry.invalidated:
                        continue
                    try:
                        need(entry.generation == self._generation, "integrity_error", "Artifact session generation changed")
                        entry.check_identities()
                    except Fault:
                        entry.invalidated = True
                        self._cache.pop(key, None)
                        if entry.refs == 0:
                            entry.close()
                            self._active.discard(entry)
                        break
                    entry.refs += 1
                    self._cache.move_to_end(key)
                    return ArtifactSession(self, entry, cache_hit=True)
            start_generation = self._generation
            entry: _ValidatedArtifact | None = None
            try:
                entry = self._open_entry(artifact_sha)
                self._sync_locked()
                need(self._generation == start_generation,
                     "integrity_error", "Artifact namespace changed during validation")
                entry.generation = self._generation
                entry.refs = 1
                self._active.add(entry)
                if cache_supported and self.cache_supported:
                    key = (artifact_sha, entry.recipe_bytes_sha)
                    entry.cached = True
                    self._cache[key] = entry
                    self._evict_locked()
                return ArtifactSession(self, entry, cache_hit=False)
            except BaseException:
                if entry is not None:
                    entry.close()
                    self._active.discard(entry)
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._monitor.close()
            for entry in set(self._cache.values()) | set(self._active):
                entry.invalidated = True
                entry.close()
            self._cache.clear()
            self._active.clear()


class ArtifactSession(AbstractContextManager):
    """A verified artifact view shared by size and all ranges in one operation."""

    def __init__(self, manager: ArtifactSessionManager, entry: _ValidatedArtifact, cache_hit: bool):
        self.manager = manager
        self.entry = entry
        self.cache_hit = cache_hit
        self._closed = False

    @property
    def size(self) -> int:
        self._boundary()
        return self.entry.size

    @property
    def metadata(self) -> RecipeMetadata | None:
        self._boundary()
        return self.entry.metadata

    @property
    def stats(self) -> dict[str, Any]:
        value = dict(self.entry.stats())
        value["cache_hit"] = self.cache_hit
        return value

    def _boundary(self) -> None:
        need(not self._closed, "closed", "Artifact session is closed")
        with self.manager._lock:
            self.manager._sync_locked()
            need(not self.entry.invalidated and self.entry.generation == self.manager._generation,
                 "integrity_error", "Artifact namespace changed during a read")
            self.entry.check_identities()

    def read_range(self, offset: int, length: int) -> bytes:
        need(type(offset) is int and offset >= 0 and type(length) is int and 0 <= length <= LEAF_BYTES,
             "invalid_range", "Artifact range is outside the bounded range")
        self._boundary()
        if offset >= self.entry.size or length == 0:
            self._boundary()
            return b""
        requested_end = min(self.entry.size, offset + length)
        output = bytearray()
        if self.entry.kind == "physical":
            leaf = next(iter(self.entry.leaves.values()))
            output.extend(self.entry._read(leaf, offset, requested_end - offset, validation=False))
        else:
            for leaf, segment_offset, segment_length, position in self.entry.segments:
                segment_end = position + segment_length
                if segment_end <= offset:
                    continue
                if position >= requested_end:
                    break
                start_in_segment = max(0, offset - position)
                take = min(segment_length - start_in_segment, requested_end - max(position, offset))
                output.extend(self.entry._read(leaf, segment_offset + start_in_segment, take, validation=False))
        self._boundary()
        need(len(output) == requested_end - offset, "integrity_error", "Artifact range ended before its declared size")
        return bytes(output)

    def __enter__(self) -> "ArtifactSession":
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.manager._release(self.entry)
            if self.manager._ephemeral:
                self.manager.close()


def _store_artifact_manager(store_or_root: Any) -> ArtifactSessionManager:
    getter = getattr(store_or_root, "_get_artifact_manager", None)
    if getter is not None:
        return getter()
    manager = getattr(store_or_root, "_artifact_manager", None)
    if manager is None:
        manager = ArtifactSessionManager(store_or_root)
        try:
            setattr(store_or_root, "_artifact_manager", manager)
        except Exception:
            # A bare Path is an offline helper, not a persistent Store.  Its
            # session closes the short-lived manager after the context exits.
            manager._ephemeral = True
    return manager


def open_artifact_session(store_or_root: Any, archive_sha: str) -> ArtifactSession:
    """Open one fully verified artifact session for a Store or filesystem adapter."""
    return _store_artifact_manager(store_or_root).open(archive_sha)


def _load_recipe(store_or_root: Any, archive_sha: str, *, verify_artifact: bool) -> RecipeMetadata:
    path = _regular(_recipe_path(store_or_root, archive_sha), "backup recipe")
    need(path.stat().st_size <= MAX_RECIPE_BYTES, "backup_capacity", "Backup recipe exceeds the explicit metadata bound")
    with path.open("rb") as stream:
        payload = stream.read(MAX_RECIPE_BYTES + 1)
    document = parse_json(payload, limit=MAX_RECIPE_BYTES)
    return _validate_document(store_or_root, document, archive_sha, verify_artifact=verify_artifact)


def validate_recipe(store_or_root: Any, archive_sha: str) -> RecipeMetadata:
    """Validate a committed recipe and its complete physical closure."""
    return _load_recipe(store_or_root, archive_sha, verify_artifact=True)


def validate_all_recipes(store_or_root: Any, *, verify_artifact: bool = True) -> list[RecipeMetadata]:
    root = _recipes(store_or_root)
    if not root.exists():
        return []
    need(root.is_dir() and not root.is_symlink(), "integrity_error", "Backup recipe namespace is not a real directory")
    result = []
    for path in sorted(root.iterdir()):
        need(path.is_file() and not path.is_symlink() and path.name.endswith(".json"),
             "integrity_error", "Backup recipe namespace contains an unsupported member", path.name)
        archive_sha = path.name[:-5]
        _sha(archive_sha, "backup recipe filename")
        result.append(_load_recipe(store_or_root, archive_sha, verify_artifact=verify_artifact))
    return result


def artifact_size(store_or_root: Any, archive_sha: str) -> int:
    with open_artifact_session(store_or_root, archive_sha) as session:
        return session.size


def artifact_references(store_or_root: Any, archive_sha: str) -> tuple[str, ...]:
    with open_artifact_session(store_or_root, archive_sha) as session:
        if session.metadata is None:
            return (session.entry.artifact_sha,)
        return session.metadata.physical_refs


def iter_artifact_range(store_or_root: Any, archive_sha: str, offset: int, length: int) -> Iterable[bytes]:
    """Yield a bounded range without materializing a recipe-backed artifact."""
    with open_artifact_session(store_or_root, archive_sha) as session:
        value = session.read_range(offset, length)
        if value:
            yield value


def _put_physical(store_or_root: Any, data: bytes) -> str:
    value = digest(data)
    path = _physical_path(store_or_root, value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _read_leaf(store_or_root, value)
    else:
        atomic_write(path, data, mode=0o600)
        manager = getattr(store_or_root, "_artifact_manager", None)
        if manager is not None:
            manager.bump()
    return value


class DirectoryArtifactStore:
    """Small filesystem-only adapter used while validating a restore staging tree."""

    def __init__(self, root: str | Path):
        self.home = Path(root).absolute()
        self.blobs = self.home / "blobs"
        self.backup_recipes = self.home / "backup-recipes"
        self.blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backup_recipes.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._artifact_manager = None

    def _get_artifact_manager(self):
        if self._artifact_manager is None:
            self._artifact_manager = ArtifactSessionManager(self)
        return self._artifact_manager

    def close(self) -> None:
        if self._artifact_manager is not None:
            self._artifact_manager.close()

    def blob_put(self, data: bytes) -> str:
        return _put_physical(self, data)


def _publish_create_only(store_or_root: Any, path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    need(path.parent.is_dir() and not path.parent.is_symlink(),
         "integrity_error", "Backup recipe namespace is not a real directory")
    fd, temporary = tempfile.mkstemp(prefix=".recipe-", dir=path.parent)
    created = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _regular(path, "existing backup recipe").read_bytes()
            need(existing == payload, "integrity_error", "A different recipe already exists for this artifact", str(path))
        else:
            created = True
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if created:
        manager = getattr(store_or_root, "_artifact_manager", None)
        if manager is not None:
            manager.bump()


def publish_recipe(store_or_root: Any, recipe: Mapping[str, Any]) -> RecipeMetadata:
    """Validate, then atomically publish one immutable recipe JSON."""
    document = dict(recipe)
    archive_sha = _sha(document.get("artifact_sha256"), "artifact_sha256")
    metadata = _validate_document(store_or_root, document, archive_sha, verify_artifact=True)
    payload = canonical(document)
    need(len(payload) <= MAX_RECIPE_BYTES, "backup_capacity", "Backup recipe exceeds the explicit metadata bound")
    path = _recipe_path(store_or_root, archive_sha)
    _publish_create_only(store_or_root, path, payload)
    return _load_recipe(store_or_root, archive_sha, verify_artifact=True)


def _mapping_segment(value: Any, field: str) -> dict[str, int | str]:
    need(isinstance(value, dict) and set(value) == {"blob", "offset", "bytes"},
         "backup_corrupt", "Payload mapping has an unexpected shape", field)
    blob = _sha(value["blob"], field + ".blob")
    offset = value["offset"]
    length = value["bytes"]
    need(type(offset) is int and offset >= 0 and type(length) is int and length > 0,
         "backup_corrupt", "Payload mapping range is invalid", field)
    return {"blob": blob, "offset": offset, "bytes": length}


def _iter_mapping(store_or_root: Any, mapping: list[dict[str, Any]]) -> Iterable[bytes]:
    cache: dict[str, tuple[Path, int, str]] = {}
    for index, raw in enumerate(mapping):
        segment = _mapping_segment(raw, f"payload[{index}]")
        path, leaf_size, _ = _read_leaf(store_or_root, segment["blob"], cache)
        need(segment["offset"] + segment["bytes"] <= leaf_size, "backup_corrupt", "Payload mapping exceeds its leaf", index)
        with path.open("rb") as stream:
            stream.seek(segment["offset"])
            remaining = segment["bytes"]
            while remaining:
                block = stream.read(min(LEAF_BYTES, remaining))
                need(block, "backup_corrupt", "Payload mapping leaf ended early", index)
                yield block
                remaining -= len(block)


def _verify_payload_mapping(store_or_root: Any, source, start: int, length: int,
                             mapping: list[dict[str, Any]], name: str) -> None:
    expected = sum(_mapping_segment(item, f"{name}.mapping")["bytes"] for item in mapping)
    need(expected == length, "backup_corrupt", "Payload mapping size differs from the ZIP member", name)
    source.seek(start)
    remaining = length
    mapped = iter(_iter_mapping(store_or_root, mapping))
    mapped_block = b""
    while remaining:
        if not mapped_block:
            try:
                mapped_block = next(mapped)
            except StopIteration as exc:
                raise Fault("backup_corrupt", "Payload mapping ended early", name) from exc
        take = min(len(mapped_block), remaining, LEAF_BYTES)
        actual = source.read(take)
        need(len(actual) == take, "backup_corrupt", "ZIP member ended early", name)
        need(actual == mapped_block[:take], "backup_corrupt", "ZIP payload differs from its physical leaf mapping", name)
        mapped_block = mapped_block[take:]
        remaining -= take
    need(not mapped_block and next(mapped, None) is None, "backup_corrupt", "Payload mapping has trailing bytes", name)


def _read_range(source: Path, start: int, length: int) -> Iterable[bytes]:
    with source.open("rb") as stream:
        stream.seek(start)
        remaining = length
        while remaining:
            block = stream.read(min(LEAF_BYTES, remaining))
            need(block, "backup_corrupt", "ZIP structural segment ended early")
            yield block
            remaining -= len(block)


def _append_metadata_segments(store_or_root: Any, source: Path, start: int, end: int,
                              output: list[dict[str, Any]]) -> None:
    need(0 <= start <= end <= source.stat().st_size, "backup_corrupt", "ZIP structural range is invalid")
    for block in _read_range(source, start, end - start):
        blob = store_or_root.blob_put(block) if hasattr(store_or_root, "blob_put") else _put_physical(store_or_root, block)
        output.append({"blob": blob, "offset": 0, "bytes": len(block)})


def recipe_from_stored_zip(source: str | Path, verified_member_payload_mapping: Mapping[str, list[dict[str, Any]]],
                           store_or_root: Any, backup_id: str) -> dict[str, Any]:
    """Derive an exact flat recipe from the finalized ZIP bytes.

    Structural bytes are copied from the actual ZIP and payload ranges are
    checked against caller-supplied physical leaf mappings.  No ZIP header is
    reconstructed from a guessed writer default.
    """
    source = _regular(Path(source), "backup export")
    size = source.stat().st_size
    need(0 < size <= MAX_ARTIFACT_BYTES, "backup_capacity", "Backup export exceeds the explicit total bound")
    artifact_sha = file_digest(source)
    segments: list[dict[str, Any]] = []
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        need(len(names) == len(set(names)), "backup_corrupt", "Backup export contains duplicate ZIP members")
        need(set(names) == set(verified_member_payload_mapping), "backup_corrupt", "Payload mapping does not cover the ZIP members")
        need("manifest.json" in names, "backup_corrupt", "Stored backup export has no manifest member")
        manifest_info = archive.getinfo("manifest.json")
        need(manifest_info.file_size <= MAX_BACKUP_MANIFEST_BYTES,
             "backup_capacity", "Backup manifest exceeds the explicit metadata bound")
        manifest = parse_json(archive.read("manifest.json"), limit=MAX_BACKUP_MANIFEST_BYTES)
        need(isinstance(manifest, dict) and manifest.get("format") == "daikibo.backup.v1"
             and isinstance(manifest.get("files"), dict),
             "backup_corrupt", "Stored backup manifest has an unsupported shape")
        need(set(manifest["files"]) | {"manifest.json"} == set(names),
             "backup_corrupt", "Stored backup manifest does not cover the ZIP members")
        expanded = 0
        for name, record in manifest["files"].items():
            need(isinstance(record, dict) and type(record.get("bytes")) is int and record["bytes"] >= 0,
                 "backup_corrupt", "Stored backup manifest has an invalid file size", name)
            expanded += record["bytes"]
            need(expanded <= MAX_LOGICAL_BACKUP_BYTES,
                 "backup_capacity", "Expanded backup exceeds the explicit total bound")
            mapped_bytes = sum(_mapping_segment(item, name + ".mapping")["bytes"]
                               for item in verified_member_payload_mapping[name])
            need(mapped_bytes == record["bytes"], "backup_corrupt",
                 "Stored backup payload mapping differs from the manifest", name)
        # The manifest member is itself part of the measured encoded payload;
        # all remaining bytes are actual ZIP framing observed below.  This
        # identity makes structural overhead explicit instead of treating the
        # expanded-payload cap as a complete-archive cap.
        encoded_payload_bytes = expanded + manifest_info.file_size
        structural_bytes = size - encoded_payload_bytes
        need(structural_bytes >= 0, "backup_corrupt", "ZIP structural bytes underflow")
        ordered = sorted(infos, key=lambda item: item.header_offset)
        cursor = 0
        start_dir = int(archive.start_dir)
        need(0 <= start_dir <= size, "backup_corrupt", "ZIP central directory offset is invalid")
        with source.open("rb") as raw:
            for index, info in enumerate(ordered):
                offset = int(info.header_offset)
                need(offset >= cursor, "backup_corrupt", "ZIP local headers overlap", info.filename)
                _append_metadata_segments(store_or_root, source, cursor, offset, segments)
                raw.seek(offset)
                fixed = raw.read(30)
                need(len(fixed) == 30 and fixed[:4] == b"PK\x03\x04", "backup_corrupt", "ZIP local header is invalid", info.filename)
                filename_bytes, extra_bytes = struct.unpack_from("<HH", fixed, 26)
                data_start = offset + 30 + filename_bytes + extra_bytes
                need(data_start <= size, "backup_corrupt", "ZIP local header exceeds export", info.filename)
                _append_metadata_segments(store_or_root, source, offset, data_start, segments)
                need(info.compress_type == zipfile.ZIP_STORED and not (info.flag_bits & 0x1),
                     "backup_corrupt", "Recipe derivation requires an unencrypted ZIP_STORED member", info.filename)
                need(info.compress_size == info.file_size, "backup_corrupt", "ZIP_STORED member size differs", info.filename)
                payload_end = data_start + int(info.compress_size)
                next_header = int(ordered[index + 1].header_offset) if index + 1 < len(ordered) else start_dir
                need(payload_end <= next_header <= size, "backup_corrupt", "ZIP payload/descriptor boundaries overlap", info.filename)
                mapping = list(verified_member_payload_mapping[info.filename])
                if info.file_size:
                    _verify_payload_mapping(store_or_root, raw, data_start, int(info.file_size), mapping, info.filename)
                    for item in mapping:
                        segment = _mapping_segment(item, info.filename)
                        segments.append(segment)
                else:
                    need(not mapping, "backup_corrupt", "Empty ZIP member has a payload mapping", info.filename)
                _append_metadata_segments(store_or_root, source, payload_end, next_header, segments)
                cursor = next_header
            _append_metadata_segments(store_or_root, source, cursor, size, segments)
    need(0 < len(segments) <= MAX_SEGMENTS, "backup_capacity", "Backup recipe has too many segments")
    document = {"format": RECIPE_FORMAT, "artifact_sha256": artifact_sha,
                "artifact_bytes": size, "backup_id": backup_id, "segments": segments}
    _validate_document(store_or_root, document, artifact_sha, verify_artifact=True)
    return document


def freeze_file_to_leaves(store_or_root: Any, source: str | Path, destination: str | Path) -> tuple[Path, list[dict[str, Any]]]:
    """Freeze a mutable payload into <=1MiB physical leaves and a temp copy."""
    source = _regular(Path(source), "backup payload")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mapping: list[dict[str, Any]] = []
    total = 0
    with source.open("rb") as incoming, destination.open("wb") as frozen:
        while block := incoming.read(LEAF_BYTES):
            total += len(block)
            need(total <= MAX_FILE_BYTES, "backup_capacity", "Backup payload exceeds the explicit file bound", str(source))
            blob = store_or_root.blob_put(block) if hasattr(store_or_root, "blob_put") else _put_physical(store_or_root, block)
            mapping.append({"blob": blob, "offset": 0, "bytes": len(block)})
            frozen.write(block)
        frozen.flush();os.fsync(frozen.fileno())
    return destination, mapping


def direct_physical_mapping(store_or_root: Any, source: str | Path) -> list[dict[str, Any]] | None:
    """Return a verified one-leaf mapping when source is already a CAS leaf."""
    source = Path(source).absolute()
    blobs = _blobs(store_or_root)
    try:
        relative = source.relative_to(blobs)
    except ValueError:
        return None
    need(source.is_file() and not source.is_symlink(),
         "integrity_error", "Physical blob path is not a regular file", str(source))
    need(len(relative.parts) == 2 and len(relative.parts[0]) == 2 and len(relative.parts[1]) == 62,
         "integrity_error", "Physical blob path has an invalid layout", str(source))
    blob = relative.parts[0] + relative.parts[1]
    path, size, _ = _read_leaf(store_or_root, blob)
    return [] if size == 0 else [{"blob": blob, "offset": 0, "bytes": size}]


def file_digest(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
