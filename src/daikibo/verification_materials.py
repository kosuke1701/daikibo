"""Controller-owned verification material for observed test executions.

The verification material is the bridge between a mutable test definition and
one concrete ``Runtime.observe`` invocation.  This module deliberately does
not implement assurance storage.  ``Assurance.store_object`` (the E1
immutable-object boundary) owns persistence, CAS and archive closure; this
module only builds and validates the controller-owned material passed to it.

There are two different digests in the execution material:

* the digest in ``definition_ref`` identifies the frozen plan/check body;
* ``runtime_check_blob`` identifies the check after controller adjustments.

They must remain separate.  A changed report path, injected build input or
runner argument is an observed execution fact, not a rewrite of the original
test-plan definition.
"""
from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .common import Fault, canonical, digest, finite_duration, need, parse_json, relative_path, text, uid
from .gitops import git
from .traceability import _git_object, _git_object_payload, _sha256_oid, _tree_entries


MATERIAL_FORMAT = "daikibo.assurance-material.v1"
EXECUTION_MATERIAL_KIND = "verification_execution"
# Mutable definition refs are resolved by E1 against the material kind named by
# the ref itself.  Keep these values aligned with that public typed-ref
# contract; execution material is a separate controller-owned object.
PLAN_MATERIAL_KIND = "test_plan"
DELIVERY_MATERIAL_KIND = "delivery_snapshot"
LAUNCH_RECIPE_FORMAT = "daikibo.verification-launch.v1"
ENVIRONMENT_FORMAT = "daikibo.verification-environment.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()
MAX_OBJECT_BYTES = 1024 * 1024


def sealed_snapshot_digest(snapshot: Any) -> str:
    """Return and validate the identity digest of one sealed source snapshot.

    ``Snapshots.capture`` deliberately calculates the digest without the
    derived ``digest`` member.  A delivery adapter must use that rule rather
    than hashing the already decorated object (which would make every valid
    snapshot look different).  Keeping the check here gives the writer and
    resolver one identity rule.
    """
    need(isinstance(snapshot, dict), "integrity_error", "Sealed snapshot is not an object")
    need(set(snapshot) == {"format", "repos", "digest"},
         "integrity_error", "Sealed snapshot shape differs")
    value = snapshot.get("digest")
    _sha(value, "snapshot.digest")
    need(snapshot.get("format") == "snapshot.v1", "integrity_error", "Sealed snapshot format differs")
    need(digest({key: item for key, item in snapshot.items() if key != "digest"}) == value,
         "integrity_error", "Sealed snapshot digest differs")
    need(isinstance(snapshot.get("repos"), dict), "integrity_error", "Sealed snapshot repositories are malformed")
    return value


def validate_sealed_snapshot(store: Any, snapshot: Any) -> str:
    """Validate the snapshot manifest and every referenced source CAS leaf."""
    snapshot_digest = sealed_snapshot_digest(snapshot)
    repo_names: set[str] = set()
    for repository, repo in snapshot["repos"].items():
        need(isinstance(repository, str) and repository and "\x00" not in repository,
             "integrity_error", "Snapshot repository identity is malformed")
        need(isinstance(repo, dict) and set(repo) == {"name", "head", "files", "bytes", "unknown"},
             "integrity_error", "Snapshot repository shape differs", repository)
        name = repo["name"]
        need(isinstance(name, str) and name and name == relative_path(name) and "/" not in name,
             "integrity_error", "Snapshot repository name is unsafe", repository)
        need(name not in repo_names, "integrity_error", "Snapshot repository names are duplicated", name)
        repo_names.add(name)
        head = repo["head"]
        need(head is None or (isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)),
             "integrity_error", "Snapshot repository head is malformed", repository)
        files = repo["files"]
        need(isinstance(files, dict), "integrity_error", "Snapshot repository files are malformed", repository)
        need(type(repo["bytes"]) is int and repo["bytes"] >= 0,
             "integrity_error", "Snapshot repository byte count is malformed", repository)
        need(isinstance(repo["unknown"], list), "integrity_error", "Snapshot unknown inventory is malformed", repository)
        total = 0
        for path, entry in files.items():
            need(isinstance(path, str) and path == relative_path(path),
                 "integrity_error", "Snapshot file path is unsafe", path)
            need(isinstance(entry, dict) and isinstance(entry.get("kind"), str),
                 "integrity_error", "Snapshot file entry is malformed", path)
            if entry["kind"] == "file":
                need(set(entry) == {"kind", "blob", "mode", "size"},
                     "integrity_error", "Snapshot file entry shape differs", path)
                _sha(entry["blob"], "snapshot file blob")
                need(type(entry["mode"]) is int and entry["mode"] in {0o100644, 0o100755},
                     "integrity_error", "Snapshot file mode is invalid", path)
                need(type(entry["size"]) is int and entry["size"] >= 0,
                     "integrity_error", "Snapshot file size is invalid", path)
                raw = store.blob_get(entry["blob"])
                need(len(raw) == entry["size"] and digest(raw) == entry["blob"],
                     "integrity_error", "Snapshot file CAS differs", path)
                total += len(raw)
            elif entry["kind"] == "symlink":
                need(set(entry) == {"kind", "target", "mode"},
                     "integrity_error", "Snapshot symlink entry shape differs", path)
                need(isinstance(entry["target"], str) and "\x00" not in entry["target"],
                     "integrity_error", "Snapshot symlink target is malformed", path)
                need(entry["mode"] == 0o120000, "integrity_error", "Snapshot symlink mode is invalid", path)
            else:
                need(False, "integrity_error", "Snapshot entry kind is unsupported", entry["kind"])
        need(repo["bytes"] == total, "integrity_error", "Snapshot repository byte count differs", repository)
    return snapshot_digest


def _git_oid(value: Any, object_format: str, name: str) -> str:
    need(object_format in {"sha1", "sha256"}, "integrity_error", "Git object format is invalid")
    width = 40 if object_format == "sha1" else 64
    need(isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{width}}}", value),
         "integrity_error", f"{name} is not a complete {object_format} object ID")
    return value


def _git_manifest_from_repository(store: Any, git_dir: Any, object_format: str, commit: str,
                                  tree: str, snapshot: dict[str, Any], repository: str) -> tuple[dict[str, Any], str]:
    """Read and seal the actual commit/tree/blob closure for one delivery."""
    repo = Path(git_dir)
    need(repo.is_dir() and not repo.is_symlink(), "missing_evidence", "Delivery Git directory is unavailable", repository)
    detected = git(repo, "rev-parse", "--show-object-format").stdout.decode().strip()
    need(detected == object_format, "integrity_error", "Delivery Git object format differs", repository)
    _git_oid(commit, object_format, "commit")
    _git_oid(tree, object_format, "tree")
    objects: dict[str, dict[str, str]] = {}
    entries: list[dict[str, Any]] = []

    def add(oid: str, expected: str) -> bytes:
        existing = objects.get(oid)
        if existing is not None:
            need(existing["kind"] == expected, "integrity_error", "Git object is used with conflicting types", oid)
            return store.blob_get(existing["blob"])
        raw = _git_object(repo, oid, object_format, expected)
        cas = store.blob_put(raw)
        objects[oid] = {"oid": oid, "kind": expected, "blob": cas}
        return raw

    commit_raw = add(commit, "commit")
    _kind, commit_payload = _git_object_payload(commit_raw, "commit")
    first = commit_payload.split(b"\n", 1)[0].split()
    need(len(first) == 2 and first[0] == b"tree" and first[1].decode("ascii") == tree,
         "integrity_error", "Delivery commit does not point to the sealed root tree")

    def visit_tree(oid: str, prefix: str = "") -> None:
        raw = add(oid, "tree")
        for mode, kind, child_oid, name in _tree_entries(raw, object_format):
            path = f"{prefix}/{name}".strip("/")
            need(path and path == relative_path(path), "integrity_error", "Git tree path is unsafe", path)
            entries.append({"path": path, "mode": mode, "kind": kind, "oid": child_oid})
            if kind == "tree":
                visit_tree(child_oid, path)
            elif kind in {"blob", "commit"}:
                add(child_oid, kind)
            else:
                need(False, "integrity_error", "Git tree entry kind is unsupported", kind)

    visit_tree(tree)
    repo_snapshot = snapshot["repos"].get(repository)
    need(isinstance(repo_snapshot, dict), "stale_reference", "Delivery snapshot lacks the committed repository", repository)
    expected: dict[str, dict[str, Any]] = {}
    for path, entry in repo_snapshot["files"].items():
        if entry["kind"] == "file":
            raw = store.blob_get(entry["blob"])
        else:
            raw = entry["target"].encode()
        expected[path] = {"mode": entry["mode"], "kind": "blob",
                          "oid": _sha256_oid(f"blob {len(raw)}\0".encode() + raw, object_format)}
    actual_leaves = {item["path"]: item for item in entries if item["kind"] != "tree"}
    need(set(actual_leaves) == set(expected), "stale_reference", "Delivery Git tree paths differ from snapshot", repository)
    for path, expected_entry in expected.items():
        actual = actual_leaves[path]
        need({key: actual[key] for key in ("mode", "kind", "oid")} == expected_entry,
             "stale_reference", "Delivery Git tree content differs from snapshot", path)
    manifest = {"format": "daikibo.delivery-git-objects.v1", "object_format": object_format,
                "repository": repository, "commit": commit, "tree": tree,
                "snapshot_digest": snapshot["digest"],
                "objects": sorted(objects.values(), key=lambda value: value["oid"]),
                "entries": sorted(entries, key=lambda value: (value["path"], value["mode"], value["oid"]))}
    manifest_blob = store.blob_put(canonical(manifest))
    return {"manifest": manifest, "manifest_blob": manifest_blob,
            "commit_object_blob": objects[commit]["blob"]}, snapshot["digest"]


def validate_git_material_payload(store: Any, payload: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate a stored actual-delivery commit payload without live Git."""
    need(isinstance(payload, dict), "integrity_error", "Actual delivery material payload is malformed")
    expected_fields = {"delivery_snapshot_ref", "repository", "object_format", "commit", "tree", "ref",
                       "commit_object_blob", "object_manifest_blob", "observed_result"}
    need(set(payload) == expected_fields, "integrity_error", "Actual delivery material payload shape differs")
    _sha(payload["commit_object_blob"], "commit_object_blob")
    _sha(payload["object_manifest_blob"], "object_manifest_blob")
    object_format = payload["object_format"]
    _git_oid(payload["commit"], object_format, "commit")
    _git_oid(payload["tree"], object_format, "tree")
    need(isinstance(payload["repository"], str) and payload["repository"],
         "integrity_error", "Actual delivery repository is malformed")
    need(isinstance(payload["ref"], str) and payload["ref"], "integrity_error", "Actual delivery ref is malformed")
    snapshot_digest = validate_sealed_snapshot(store, snapshot)
    manifest_raw = store.blob_get(payload["object_manifest_blob"])
    manifest = parse_json(manifest_raw, limit=MAX_OBJECT_BYTES)
    need(canonical(manifest) == manifest_raw, "integrity_error", "Git object manifest is not canonical")
    need(isinstance(manifest, dict) and set(manifest) == {"format", "object_format", "repository", "commit", "tree", "snapshot_digest", "objects", "entries"},
         "integrity_error", "Git object manifest shape differs")
    need(manifest["format"] == "daikibo.delivery-git-objects.v1" and
         manifest["object_format"] == object_format and manifest["repository"] == payload["repository"] and
         manifest["commit"] == payload["commit"] and manifest["tree"] == payload["tree"] and
         manifest["snapshot_digest"] == snapshot_digest, "integrity_error", "Git object manifest identity differs")
    objects = manifest["objects"]
    need(isinstance(objects, list) and objects, "missing_evidence", "Git object closure is empty")
    by_oid: dict[str, dict[str, Any]] = {}
    for item in objects:
        need(isinstance(item, dict) and set(item) == {"oid", "kind", "blob"},
             "integrity_error", "Git object manifest row shape differs")
        oid = _git_oid(item["oid"], object_format, "manifest object")
        need(item["kind"] in {"commit", "tree", "blob"}, "integrity_error", "Git object kind is invalid")
        _sha(item["blob"], "manifest object CAS")
        need(oid not in by_oid, "integrity_error", "Git object manifest contains a duplicate object", oid)
        raw = store.blob_get(item["blob"])
        actual_kind, _payload = _git_object_payload(raw)
        need(actual_kind == item["kind"] and _sha256_oid(raw, object_format) == oid,
             "integrity_error", "Git object CAS differs from its OID", oid)
        by_oid[oid] = item
    need(payload["commit"] in by_oid and by_oid[payload["commit"]]["kind"] == "commit",
         "missing_evidence", "Git commit object is absent from closure")
    need(payload["tree"] in by_oid and by_oid[payload["tree"]]["kind"] == "tree",
         "missing_evidence", "Git root tree object is absent from closure")
    need(by_oid[payload["commit"]]["blob"] == payload["commit_object_blob"],
         "integrity_error", "Commit object CAS does not match the manifest")
    commit_raw = store.blob_get(payload["commit_object_blob"])
    _kind, commit_payload = _git_object_payload(commit_raw, "commit")
    first = commit_payload.split(b"\n", 1)[0].split()
    need(len(first) == 2 and first[0] == b"tree" and first[1].decode("ascii") == payload["tree"],
         "integrity_error", "Git commit/tree identity differs")
    entries = manifest["entries"]
    need(isinstance(entries, list), "integrity_error", "Git entry manifest is malformed")
    entry_map: dict[str, dict[str, Any]] = {}
    all_entry_map: dict[str, dict[str, Any]] = {}
    tree_paths: set[str] = set()
    def walk_tree(oid: str, prefix: str = "") -> None:
        raw = store.blob_get(by_oid[oid]["blob"])
        for mode, kind, child_oid, name in _tree_entries(raw, object_format):
            path = f"{prefix}/{name}".strip("/")
            need(path not in tree_paths, "integrity_error", "Git tree path is duplicated", path)
            tree_paths.add(path)
            need(child_oid in by_oid and by_oid[child_oid]["kind"] == kind,
                 "missing_evidence", "Git tree child is absent from closure", child_oid)
            entry = {"path": path, "mode": mode, "kind": kind, "oid": child_oid}
            all_entry_map[path] = entry
            if kind != "tree":
                entry_map[path] = entry
            if kind == "tree":
                walk_tree(child_oid, path)
    walk_tree(payload["tree"])
    need(all(isinstance(item, dict) and set(item) == {"path", "mode", "kind", "oid"}
             and isinstance(item["path"], str) and item["path"] == relative_path(item["path"])
             and type(item["mode"]) is int and item["kind"] in {"blob", "commit", "tree"}
             for item in entries),
         "integrity_error", "Git entry manifest row shape differs")
    normalized_entries = sorted(entries, key=lambda value: (value["path"], value["mode"], value["oid"]))
    need(normalized_entries == sorted(all_entry_map.values(), key=lambda value: (value["path"], value["mode"], value["oid"])),
         "integrity_error", "Git entry manifest differs from the tree closure")
    repo_snapshot = snapshot["repos"].get(payload["repository"])
    need(isinstance(repo_snapshot, dict), "stale_reference", "Snapshot repository is missing", payload["repository"])
    expected: dict[str, dict[str, Any]] = {}
    for path, item in repo_snapshot["files"].items():
        raw = store.blob_get(item["blob"]) if item["kind"] == "file" else item["target"].encode()
        expected[path] = {"path": path, "mode": item["mode"], "kind": "blob",
                          "oid": _sha256_oid(f"blob {len(raw)}\0".encode() + raw, object_format)}
    need(entry_map == expected, "stale_reference", "Git tree does not reproduce the sealed snapshot")
    observed = payload["observed_result"]
    need(isinstance(observed, dict) and set(observed) <= {"repository", "object_format", "commit", "tree", "ref", "snapshot", "reconciled"},
         "integrity_error", "Observed delivery commit result shape differs")
    for key, value in {"repository": payload["repository"], "object_format": object_format,
                       "commit": payload["commit"], "tree": payload["tree"], "snapshot": snapshot_digest}.items():
        need(observed.get(key) == value, "integrity_error", "Observed delivery commit result differs", key)
    need(observed.get("ref") == payload["ref"], "integrity_error", "Observed delivery ref differs")
    return {"manifest": manifest, "entry_manifest": normalized_entries, "snapshot_digest": snapshot_digest}


def _sha(value: Any, name: str) -> str:
    need(isinstance(value, str) and _SHA256.fullmatch(value) is not None,
         "invalid_verification_material", f"{name} must be a lowercase SHA-256 digest")
    return value


def _copy_json(value: Any, name: str = "value") -> Any:
    """Copy only JSON-shaped data and reject values that cannot be pinned."""
    try:
        encoded = canonical(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise Fault("invalid_verification_material", f"{name} is not canonical JSON") from exc
    return parse_json(encoded)


def _ref_project(ref: Any, project: str, name: str) -> dict[str, Any]:
    need(isinstance(ref, dict), "invalid_verification_material", f"{name} must be an object")
    need(ref.get("project") == project, "cross_project", f"{name} belongs to another project")
    need(isinstance(ref.get("kind"), str) and ref["kind"],
         "invalid_verification_material", f"{name}.kind is required")
    return _copy_json(ref, name)


def _local_validate_ref(ref: Any, project: str, name: str) -> dict[str, Any]:
    """Validate the closed shapes used by this module.

    E1 supplies the complete typed-ref validator after integration.  Keeping a
    small local validator lets this helper be tested independently on the
    pre-E1 checkpoint without inventing a second persistence implementation.
    """
    ref = _ref_project(ref, project, name)
    kind = ref["kind"]
    exact = {
        "test_plan": {"kind", "project", "task", "task_revision", "plan_digest", "pin"},
        "test_plan_check": {"kind", "project", "plan", "check_id", "check_digest"},
        "delivery_snapshot": {"kind", "project", "delivery", "binding_digest", "snapshot_digest", "pin"},
        "delivery_check": {"kind", "project", "delivery", "check_id", "check_digest"},
        "actual_delivery_commit": {"kind", "project", "delivery", "repository", "object_format", "commit", "tree", "pin"},
        "task_revision": {"kind", "project", "task", "revision", "definition_digest"},
        "candidate": {"kind", "project", "candidate", "task", "task_revision", "candidate_digest", "snapshot_digest"},
    }
    need(kind in exact, "unknown_reference", f"Unsupported verification reference kind: {kind}")
    need(set(ref) == exact[kind], "invalid_verification_material", f"{name} has unknown or missing fields")
    for field in ("plan_digest", "check_digest", "binding_digest", "snapshot_digest",
                  "definition_digest", "candidate_digest"):
        if field in ref:
            _sha(ref[field], f"{name}.{field}")
    if kind in {"test_plan", "delivery_snapshot", "actual_delivery_commit"}:
        pin = ref["pin"]
        need(isinstance(pin, dict) and set(pin) == {"id", "digest"} and isinstance(pin["id"], str) and pin["id"],
             "invalid_verification_material", f"{name}.pin is invalid")
        _sha(pin["digest"], f"{name}.pin.digest")
    if kind == "test_plan":
        need(type(ref["task_revision"]) is int and ref["task_revision"] >= 1,
             "invalid_verification_material", f"{name}.task_revision is invalid")
    if kind == "test_plan_check":
        ref["plan"] = _local_validate_ref(ref["plan"], project, f"{name}.plan")
        need(isinstance(ref["check_id"], str) and bool(ref["check_id"]),
             "invalid_verification_material", f"{name}.check_id is invalid")
    if kind == "delivery_check":
        ref["delivery"] = _local_validate_ref(ref["delivery"], project, f"{name}.delivery")
        need(isinstance(ref["check_id"], str) and bool(ref["check_id"]),
             "invalid_verification_material", f"{name}.check_id is invalid")
    if kind == "actual_delivery_commit":
        need(isinstance(ref["repository"], str) and bool(ref["repository"]),
             "invalid_verification_material", f"{name}.repository is invalid")
        _git_oid(ref["commit"], ref["object_format"], f"{name}.commit")
        _git_oid(ref["tree"], ref["object_format"], f"{name}.tree")
        ref["delivery"] = _local_validate_ref(ref["delivery"], project, f"{name}.delivery")
    return ref


def validate_ref(ref: Any, project: str, name: str = "reference") -> dict[str, Any]:
    """Use E1's closed validator when installed, with a strict local fallback."""
    try:
        from .assurance_relations import validate_typed_ref  # type: ignore
    except ImportError:
        return _local_validate_ref(ref, project, name)
    normalized = validate_typed_ref(ref, project=project)
    # E1 returns a derived identity_digest in the normalized result.  Do not
    # put that derived field into a stored reference; it is not part of the
    # exact reference schema.
    result = copy.deepcopy(ref)
    need(result.get("project") == project, "cross_project", f"{name} belongs to another project")
    return result


def validate_test_plan_definition_identity(payload: Any, dependencies: Any, *, project: str,
                                           task_ref: dict[str, Any], plan_body: dict[str, Any],
                                           plan_digest: str) -> dict[str, Any]:
    """Validate and return the immutable identity carried by one test-plan pin.

    A capture envelope has provenance that is intentionally unique to one
    observation.  That provenance is not part of the frozen definition
    identity.  The payload and its single Task-revision dependency are the
    shared authority used by both the writer and the read-only stage consumer.
    Keeping this check in one helper prevents an equivalent re-capture from
    becoming ambiguous while still rejecting a pin whose definition or
    dependency was substituted.
    """
    need(isinstance(project, str) and project, "invalid_verification_material",
         "Test-plan material project is invalid")
    expected_task = validate_ref(task_ref, project, "test-plan Task dependency")
    need(expected_task.get("kind") == "task_revision",
         "invalid_verification_material", "Test-plan material dependency is not a Task revision")
    need(isinstance(plan_body, dict), "invalid_verification_material",
         "Test-plan definition body is not an object")
    _sha(plan_digest, "test-plan digest")
    required = {"task", "task_revision", "plan_body", "plan_digest"}
    need(isinstance(payload, dict) and set(payload) == required,
         "integrity_error", "Test-plan material payload shape differs")
    need(payload["task"] == expected_task["task"] and
         payload["task_revision"] == expected_task["revision"],
         "integrity_error", "Test-plan material Task identity differs")
    need(payload["plan_digest"] == plan_digest and
         payload["plan_body"] == plan_body and
         digest(payload["plan_body"]) == payload["plan_digest"],
         "integrity_error", "Test-plan material definition identity differs")
    need(isinstance(dependencies, list) and len(dependencies) == 1,
         "integrity_error", "Test-plan material Task dependency count differs")
    dependency = validate_ref(dependencies[0], project, "test-plan Task dependency")
    need(dependency == expected_task,
         "integrity_error", "Test-plan material Task definition dependency differs")
    return {
        "kind": "test_plan_definition",
        "project": project,
        "task": expected_task["task"],
        "task_revision": expected_task["revision"],
        "task_definition_digest": expected_task["definition_digest"],
        "plan_digest": payload["plan_digest"],
        "plan_body_digest": digest(payload["plan_body"]),
    }


def _validate_dependency_refs(refs: Any, project: str) -> list[dict[str, Any]]:
    need(isinstance(refs, list), "invalid_verification_material", "dependency_refs must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, ref in enumerate(refs):
        # A dependency may be one of the broader typed refs (artifact, source,
        # observed_result, ...); E1 is the authority for that complete closed
        # registry.  The fallback permits only refs used by this helper.
        normalized = validate_ref(ref, project, f"dependency_refs[{index}]")
        identity = digest(normalized)
        need(identity not in seen, "duplicate_reference", "Duplicate dependency reference")
        seen.add(identity)
        result.append(normalized)
    return result


def _capture_id(captured_from: dict[str, Any] | None, operation: str) -> dict[str, Any]:
    source = {} if captured_from is None else _copy_json(captured_from, "captured_from")
    need(isinstance(source, dict), "invalid_verification_material", "captured_from must be an object")
    source.setdefault("controller", "runtime")
    source.setdefault("operation", operation)
    need(source.get("controller") in {"runtime", "delivery"}, "invalid_verification_material",
         "verification material must be controller-generated")
    need(isinstance(source.get("operation"), str) and source["operation"],
         "invalid_verification_material", "captured_from.operation is required")
    source.setdefault("capture_id", uid("VMAT"))
    text(source["capture_id"], "captured_from.capture_id", 200)
    return source


def require_current(value: Any, message: str = "Execution inputs are not current") -> dict[str, Any] | bool:
    """Accept only an explicit controller currentness result."""
    need(value is True or (isinstance(value, dict) and value.get("current") is True),
         "stale_verification_material", message, value)
    return value


def _pin_from_result(result: Any, payload: dict[str, Any], material_kind: str,
                     project: str, dependency_refs: list[dict[str, Any]],
                     origin: dict[str, Any], captured_from: dict[str, Any]) -> dict[str, str]:
    # E1 returns ``(material_row, created)`` and may return an existing
    # content-addressed row.  Do not rebuild its envelope or CAS leaf here.
    if isinstance(result, tuple):
        need(len(result) == 2, "verification_material_unavailable", "E1 material store returned an invalid tuple")
        result = result[0]
    need(isinstance(result, dict), "verification_material_unavailable", "E1 material store returned no object")
    ident = result.get("id")
    stored_digest = result.get("digest")
    need(isinstance(ident, str) and ident, "verification_material_unavailable", "E1 material object has no id")
    _sha(stored_digest, "material digest")
    returned_body = result.get("body", _MISSING)
    need(returned_body is not _MISSING, "verification_material_unavailable", "E1 material store omitted the object body")
    if isinstance(returned_body, str):
        returned_body = parse_json(returned_body)
    need(isinstance(returned_body, dict), "integrity_error", "E1 material body is not an object")
    need(returned_body.get("format") == MATERIAL_FORMAT and
         returned_body.get("material_kind") == material_kind and
         returned_body.get("project") == project and
         returned_body.get("origin") == origin and
         returned_body.get("semantic_digest") == digest(payload) and
         returned_body.get("dependency_refs") == dependency_refs and
         returned_body.get("captured_from") == captured_from,
         "integrity_error", "E1 material store changed the controller-bound material")
    need(stored_digest == digest(returned_body), "integrity_error", "E1 material digest does not match its body")
    return {"id": ident, "digest": stored_digest}


@dataclass(frozen=True)
class ExecutionMaterialContext:
    """Non-serializable controller context used while starting one run."""

    actor: Any
    definition_ref: dict[str, Any]
    execution_subject: dict[str, Any]
    task_revision: int | None
    candidate_ref: dict[str, Any] | None
    binding: str
    timeout_authorization_refs: tuple[Any, ...]
    test_artifact_refs: tuple[dict[str, Any], ...]
    revalidate: Callable[[], Any] | None
    captured_from: dict[str, Any]


class VerificationMaterialCoordinator:
    """Build controller material and delegate immutable persistence to E1."""

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def backend(self):
        # Control/E1 wires ``runtime.assurance`` after the storage component is
        # initialized.  The second name is useful for focused unit tests and
        # remains an explicit dependency injection point, never a DB fallback.
        return getattr(self.runtime, "assurance", None) or getattr(self.runtime, "verification_material_store", None)

    def _require_backend(self):
        backend = self.backend
        need(backend is not None and callable(getattr(backend, "store_material", None)),
             "verification_material_unavailable",
             "E1 immutable material storage is not connected; execution is unknown")
        return backend

    def _pin(self, actor, project: str, material_kind: str, payload: dict[str, Any],
             dependencies: list[dict[str, Any]], captured_from: dict[str, Any], logical_id: str) -> dict[str, str]:
        backend = self._require_backend()
        dependency_refs = _validate_dependency_refs(dependencies, project)
        payload = _copy_json(payload, "material payload")
        origin = {"controller": captured_from.get("controller", "runtime"),
                  "operation": captured_from.get("operation", material_kind),
                  "id": logical_id}
        result = backend.store_material(actor, project, material_kind, payload,
                                        dependency_refs, origin, captured_from)
        return _pin_from_result(result, payload, material_kind, project, dependency_refs, origin, captured_from)

    @staticmethod
    def _task_body(task_row: dict[str, Any]) -> dict[str, Any]:
        body = task_row.get("body")
        if isinstance(body, str):
            body = parse_json(body)
        need(isinstance(body, dict), "integrity_error", "Task definition is not an object")
        return _copy_json(body, "task body")

    @staticmethod
    def _plan_body(plan_row: dict[str, Any]) -> dict[str, Any]:
        body = plan_row.get("body")
        if isinstance(body, str):
            body = parse_json(body)
        need(isinstance(body, dict), "integrity_error", "Test plan is not an object")
        body = _copy_json(body, "test plan")
        need(plan_row.get("digest") == digest(body), "integrity_error", "Frozen test plan digest differs")
        return body

    def pin_test_plan(self, actor, project: str, task_row: dict[str, Any], plan_row: dict[str, Any],
                      *, captured_from: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        task_id = task_row.get("id")
        plan = self._plan_body(plan_row)
        task_body = self._task_body(task_row)
        revision = task_row.get("revision")
        need(isinstance(task_id, str) and task_id, "invalid_verification_material", "Task id is missing")
        need(type(revision) is int and revision >= 1, "invalid_verification_material", "Task revision is invalid")
        task_ref = {
            "kind": "task_revision", "project": project, "task": task_id,
            "revision": revision, "definition_digest": digest(task_body),
        }
        payload = {"task": task_id, "task_revision": revision,
                   "plan_body": plan, "plan_digest": plan_row["digest"]}
        validate_test_plan_definition_identity(
            payload, [task_ref], project=project, task_ref=task_ref,
            plan_body=plan, plan_digest=plan_row["digest"],
        )
        captured = _capture_id(captured_from, "task.tests.definition")
        pin = self._pin(actor, project, PLAN_MATERIAL_KIND,
                        payload,
                        [task_ref], captured,
                        f"verification:test-plan:{task_id}:{revision}:{plan_row['digest']}:{captured['capture_id']}")
        plan_ref = {
            "kind": "test_plan", "project": project, "task": task_id,
            "task_revision": revision, "plan_digest": plan_row["digest"], "pin": pin,
        }
        validate_ref(plan_ref, project, "definition_ref.plan")
        return plan_ref, pin

    def pin_delivery_snapshot(self, actor, project: str, delivery_row: dict[str, Any], body: dict[str, Any],
                              *, captured_from: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        delivery = delivery_row.get("id") if isinstance(delivery_row, dict) else None
        need(isinstance(delivery, str) and delivery, "invalid_verification_material", "Delivery id is missing")
        # The row/body are only a controller observation hint.  Re-read the
        # authoritative Delivery under the same project boundary and reject a
        # caller-supplied body that differs from it.  This prevents a material
        # pin from becoming a generic payload upload route.
        stored = self.runtime.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery, project))
        need(stored is not None, "unresolved_reference", "Delivery is missing", delivery)
        stored_body = parse_json(stored["body"])
        need(isinstance(stored_body, dict), "integrity_error", "Stored Delivery body is not an object")
        supplied_body = body
        need(isinstance(supplied_body, dict), "integrity_error", "Delivery body is not an object")
        need(canonical(supplied_body) == canonical(stored_body),
             "integrity_error", "Delivery material was not captured from the authoritative row")
        binding = stored_body.get("binding")
        need(isinstance(binding, dict), "integrity_error", "Delivery binding is not an object")
        binding_digest = stored.get("digest")
        need(binding_digest == digest(binding), "integrity_error", "Delivery binding digest differs")
        if delivery_row.get("project") is not None:
            need(delivery_row["project"] == project, "cross_project", "Delivery row belongs to another project")
        if delivery_row.get("digest") is not None:
            need(delivery_row["digest"] == binding_digest, "stale_reference", "Delivery row digest differs")
        snapshot = stored_body.get("snapshot")
        snapshot_digest = validate_sealed_snapshot(self.runtime.s, snapshot)
        need(binding.get("snapshot") == snapshot_digest,
             "integrity_error", "Delivery binding does not identify its snapshot")
        checks = stored_body.get("checks")
        need(isinstance(checks, list), "integrity_error", "Delivery checks are not an array")
        check_ids: set[str] = set()
        for check in checks:
            need(isinstance(check, dict) and isinstance(check.get("id"), str) and check["id"],
                 "integrity_error", "Delivery check identity is malformed")
            need(check["id"] not in check_ids, "integrity_error", "Delivery check identity is duplicated", check["id"])
            check_ids.add(check["id"])
        fixed = {key: copy.deepcopy(stored_body.get(key)) for key in
                 ("delivery", "binding", "snapshot", "checks", "build_definitions", "target_environment", "applicability", "rollback")}
        fixed["delivery"] = delivery
        fixed["binding"] = binding
        captured = _capture_id(captured_from, "delivery.verify.definition")
        pin = self._pin(actor, project, DELIVERY_MATERIAL_KIND, fixed, [], captured,
                        f"verification:delivery:{delivery}:{binding_digest}:{captured['capture_id']}")
        ref = {
            "kind": "delivery_snapshot", "project": project, "delivery": delivery,
            "binding_digest": binding_digest, "snapshot_digest": snapshot_digest, "pin": pin,
        }
        validate_ref(ref, project, "definition_ref.delivery")
        return ref, pin

    def pin_actual_delivery_commit(self, actor, project: str, delivery_row: dict[str, Any], body: dict[str, Any],
                                   repository: str, result: dict[str, Any], *, snapshot_ref: dict[str, Any] | None = None,
                                   captured_from: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        """Pin one actual Delivery Git result after it was observed and saved.

        The caller supplies the Delivery operation's result, never an
        arbitrary Git OID or payload.  The authoritative row is re-read,
        the result is matched to its saved ``body.git`` entry, and the real
        bare repository is traversed before the immutable material is stored.
        """
        delivery = delivery_row.get("id") if isinstance(delivery_row, dict) else None
        need(isinstance(delivery, str) and delivery, "invalid_verification_material", "Delivery id is missing")
        need(isinstance(repository, str) and repository, "invalid_verification_material", "Delivery repository is missing")
        need(isinstance(result, dict), "integrity_error", "Delivery Git result is not an object")
        stored = self.runtime.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery, project))
        need(stored is not None, "unresolved_reference", "Delivery is missing", delivery)
        stored_body = parse_json(stored["body"])
        need(isinstance(stored_body, dict), "integrity_error", "Stored Delivery body is not an object")
        need(isinstance(body, dict) and canonical(body) == canonical(stored_body),
             "integrity_error", "Delivery commit was not captured from the authoritative row")
        if delivery_row.get("project") is not None:
            need(delivery_row["project"] == project, "cross_project", "Delivery row belongs to another project")
        binding = stored_body.get("binding")
        snapshot = stored_body.get("snapshot")
        binding_digest = stored.get("digest")
        snapshot_digest = validate_sealed_snapshot(self.runtime.s, snapshot)
        need(isinstance(binding, dict) and binding_digest == digest(binding),
             "integrity_error", "Delivery binding identity differs")
        need(binding.get("snapshot") == snapshot_digest,
             "integrity_error", "Delivery binding does not identify its snapshot")
        git_result = (stored_body.get("git") or {}).get(repository)
        need(isinstance(git_result, dict), "unresolved_reference", "Delivery has no saved Git result", repository)
        for key in ("repository", "commit", "tree", "ref", "snapshot"):
            need(result.get(key) == git_result.get(key),
                 "integrity_error", "Observed Git result differs from saved Delivery result", key)
        need(result.get("snapshot") == snapshot_digest,
             "integrity_error", "Observed Git result is bound to another snapshot")
        git_dir = result.get("git_dir") or git_result.get("git_dir")
        need(isinstance(git_dir, str) and git_dir, "missing_evidence", "Delivery Git result has no repository path")
        object_format = git(Path(git_dir), "rev-parse", "--show-object-format").stdout.decode().strip()
        _git_oid(result.get("commit"), object_format, "commit")
        _git_oid(result.get("tree"), object_format, "tree")
        observed = {key: copy.deepcopy(result[key]) for key in ("repository", "commit", "tree", "ref", "snapshot")}
        observed["object_format"] = object_format
        if "reconciled" in result:
            observed["reconciled"] = bool(result["reconciled"])
        manifest_result, _ = _git_manifest_from_repository(
            self.runtime.s, git_dir, object_format, result["commit"], result["tree"], snapshot, repository)
        payload = {
            "delivery_snapshot_ref": snapshot_ref,
            "repository": repository,
            "object_format": object_format,
            "commit": result["commit"],
            "tree": result["tree"],
            "ref": result["ref"],
            "commit_object_blob": manifest_result["commit_object_blob"],
            "object_manifest_blob": manifest_result["manifest_blob"],
            "observed_result": observed,
        }
        if snapshot_ref is None:
            snapshot_ref, _ = self.pin_delivery_snapshot(
                actor, project, stored, stored_body,
                captured_from={"controller": "delivery", "operation": "delivery.commit.snapshot"})
            payload["delivery_snapshot_ref"] = snapshot_ref
        else:
            payload["delivery_snapshot_ref"] = validate_ref(snapshot_ref, project, "delivery_snapshot_ref")
        need(payload["delivery_snapshot_ref"]["binding_digest"] == binding_digest and
             payload["delivery_snapshot_ref"]["snapshot_digest"] == snapshot_digest,
             "integrity_error", "Actual commit snapshot dependency differs")
        validate_git_material_payload(self.runtime.s, payload, snapshot)
        captured = _capture_id(captured_from, "delivery.commit.observation")
        pin = self._pin(actor, project, "actual_delivery_commit", payload,
                        [payload["delivery_snapshot_ref"]], captured,
                        f"verification:delivery-commit:{delivery}:{repository}:{result['commit']}:{captured['capture_id']}")
        ref = {
            "kind": "actual_delivery_commit", "project": project,
            "delivery": payload["delivery_snapshot_ref"], "repository": repository,
            "object_format": object_format, "commit": result["commit"], "tree": result["tree"], "pin": pin,
        }
        validate_ref(ref, project, "definition_ref.actual_delivery_commit")
        return ref, pin

    @staticmethod
    def test_plan_check_ref(project: str, plan_ref: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
        plan_ref = validate_ref(plan_ref, project, "definition_ref.plan")
        need(isinstance(check, dict) and isinstance(check.get("id"), str) and check["id"],
             "invalid_verification_material", "Test check has no id")
        ref = {"kind": "test_plan_check", "project": project, "plan": plan_ref,
               "check_id": check["id"], "check_digest": digest(check)}
        return validate_ref(ref, project, "definition_ref")

    @staticmethod
    def delivery_check_ref(project: str, snapshot_ref: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
        snapshot_ref = validate_ref(snapshot_ref, project, "definition_ref.delivery")
        need(isinstance(check, dict) and isinstance(check.get("id"), str) and check["id"],
             "invalid_verification_material", "Delivery check has no id")
        ref = {"kind": "delivery_check", "project": project, "delivery": snapshot_ref,
               "check_id": check["id"], "check_digest": digest(check)}
        return validate_ref(ref, project, "definition_ref")

    @staticmethod
    def candidate_ref(project: str, task_row: dict[str, Any], candidate_row: dict[str, Any]) -> dict[str, Any]:
        body = candidate_row.get("body")
        if isinstance(body, str):
            body = parse_json(body)
        need(isinstance(body, dict), "integrity_error", "Candidate is not an object")
        body = _copy_json(body, "candidate")
        candidate_digest = candidate_row.get("digest")
        need(candidate_digest == digest(body), "integrity_error", "Candidate digest differs")
        snapshot = body.get("snapshot")
        need(isinstance(snapshot, dict) and isinstance(snapshot.get("digest"), str),
             "integrity_error", "Candidate snapshot is missing")
        task_id = task_row.get("id"); revision = task_row.get("revision")
        candidate_id = candidate_row.get("id")
        need(isinstance(task_id, str) and isinstance(candidate_id, str), "invalid_verification_material", "Candidate identity is missing")
        need(type(revision) is int and revision >= 1, "invalid_verification_material", "Candidate task revision is invalid")
        ref = {"kind": "candidate", "project": project, "candidate": candidate_id,
               "task": task_id, "task_revision": revision, "candidate_digest": candidate_digest,
               "snapshot_digest": snapshot["digest"]}
        # candidate refs are resolved by E1 after integration; local validation
        # still makes the identity and snapshot binding explicit.
        return ref

    @staticmethod
    def context(*, actor: Any, definition_ref: dict[str, Any], execution_subject: dict[str, Any],
                task_revision: int | None, candidate_ref: dict[str, Any] | None, binding: str,
                timeout_authorization_refs: list[Any] | tuple[Any, ...] = (),
                test_artifact_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
                revalidate: Callable[[], Any] | None = None,
                captured_from: dict[str, Any] | None = None) -> ExecutionMaterialContext:
        need(isinstance(execution_subject, dict), "invalid_verification_material", "execution_subject is required")
        need(actor is not None, "invalid_verification_material", "Controller actor is required")
        need(execution_subject.get("kind") in {"task", "delivery"},
             "invalid_verification_material", "execution_subject.kind is invalid")
        text(execution_subject.get("id"), "execution_subject.id", 200)
        text(execution_subject.get("binding"), "execution_subject.binding", 200)
        text(binding, "binding", 200)
        need(execution_subject["binding"] == binding, "stale_verification_material", "Subject binding differs")
        if execution_subject["kind"] == "task":
            need(type(task_revision) is int and task_revision >= 1, "invalid_verification_material", "Task revision is required")
            need(candidate_ref is not None, "invalid_verification_material", "Task execution needs a candidate ref")
        else:
            need(task_revision is None and candidate_ref is None, "invalid_verification_material", "Delivery execution cannot carry a task candidate")
        project = definition_ref.get("project") if isinstance(definition_ref, dict) else None
        text(project, "definition_ref.project", 200)
        artifacts = tuple(validate_ref(ref, project, "test_artifact_ref")
                          for ref in test_artifact_refs) if test_artifact_refs else ()
        seen_artifacts: set[str] = set()
        for artifact in artifacts:
            need(artifact.get("kind") == "artifact", "invalid_verification_material",
                 "test_artifact_refs must use artifact references")
            identity = digest(artifact)
            need(identity not in seen_artifacts, "duplicate_reference",
                 "test_artifact_refs contains a duplicate artifact")
            seen_artifacts.add(identity)
        if candidate_ref is not None:
            validate_ref(candidate_ref, project, "candidate_ref")
        need(callable(revalidate), "invalid_verification_material",
             "Execution material requires a controller currentness callback")
        return ExecutionMaterialContext(
            actor=actor,
            definition_ref=_copy_json(definition_ref, "definition_ref"),
            execution_subject=_copy_json(execution_subject, "execution_subject"),
            task_revision=task_revision,
            candidate_ref=_copy_json(candidate_ref, "candidate_ref") if candidate_ref is not None else None,
            binding=binding,
            timeout_authorization_refs=tuple(_copy_json(list(timeout_authorization_refs), "timeout_authorization_refs")),
            test_artifact_refs=artifacts,
            revalidate=revalidate,
            captured_from=_capture_id(captured_from, "runtime.observe"),
        )

    @staticmethod
    def launch_recipe(check: dict[str, Any] | None, argv: list[str], cwd_relative: str) -> dict[str, Any]:
        check = {} if check is None else check
        requested = check.get("argv", [])
        requested_first = requested[0] if isinstance(requested, list) and requested else None
        pytest_args: list[str] = []
        if check.get("kind") in {"pytest", "junit"}:
            pytest_args = ["--junitxml", check.get("report", "results.xml"), "-p", "no:cacheprovider"]
        return {
            "format": LAUNCH_RECIPE_FORMAT,
            "argv": list(argv),
            "cwd_snapshot_relative": cwd_relative,
            "trusted_wrapper_version": "daikibo.runtime.argv-wrapper.v1",
            "generator": "Runtime.observe.argv_factory.v1",
            "python_executable": sys.executable,
            "python_replacement": {"requested": requested_first, "resolved": argv[0] if argv else None,
                                    "applied": requested_first in {"python", "python3"} and bool(argv) and argv[0] == sys.executable},
            "pytest_added_args": pytest_args,
        }

    @staticmethod
    def environment_input(extra_env: dict[str, str] | None = None,
                          managed_context: dict[str, Any] | None = None,
                          *, effective_env: dict[str, str] | None = None) -> dict[str, Any]:
        """Describe the exact environment mapping passed to ``Popen``.

        ``effective_env`` is assembled by Runtime from the inherited process
        environment and all controller/provider overrides.  It is copied here
        before the immutable material is written, then the same dict is passed
        to ``Popen`` by Runtime.  The declared inputs remain useful provenance,
        but they are not a substitute for the effective mapping.

        The material is controller-private CAS data.  Nothing in this payload
        is added to a source snapshot or portable source archive; provider
        secrets therefore follow the existing private-material handling.
        """
        if extra_env is None:
            extra_env = {}
        if managed_context is None:
            managed_context = {}
        if effective_env is None:
            # Preserve the focused helper's old two-argument API.  Runtime
            # always supplies the complete mapping explicitly.
            effective_env = dict(extra_env)
        need(isinstance(effective_env, dict), "invalid_verification_material",
             "effective_env must be an object")
        need(isinstance(extra_env, dict), "invalid_verification_material", "extra_env must be an object")
        need(isinstance(managed_context, dict), "invalid_verification_material",
             "managed_context must be an object")
        for name, values in (("effective environment", effective_env),
                             ("extra environment", extra_env)):
            for key, value in values.items():
                text(key, f"{name} key", 256)
                text(value, f"{name} value", 1_000_000, empty=True)
        return {
            "format": ENVIRONMENT_FORMAT,
            # This is the complete, sorted-by-canonical-JSON mapping inherited
            # and overridden for this one subprocess.  Keep it private in the
            # assurance material CAS; do not copy it into a source ZIP.
            "effective_environment": _copy_json(effective_env, "effective_env"),
            "extra_env": _copy_json(extra_env, "extra_env"),
            "managed_context": _copy_json(managed_context, "managed_context"),
        }

    def prepare_execution(self, actor, project: str, context: ExecutionMaterialContext, *,
                          check: dict[str, Any], argv: list[str], cwd_relative: str,
                          snapshot: dict[str, Any], timeout: float,
                          extra_env: dict[str, str] | None, managed_context: dict[str, Any],
                          run_id: str,
                          effective_env: dict[str, str] | None = None) -> dict[str, Any]:
        """Pin one execution material after a currentness check.

        The returned pin is the only verification material identity accepted by
        Runtime.  The callback is called once here and once immediately before
        ``Popen`` by Runtime; a changed plan/delivery/candidate therefore
        leaves the run unknown instead of launching against stale inputs.
        """
        if context.revalidate is not None:
            require_current(context.revalidate(), "Execution inputs changed before material pin")
        finite_duration(timeout, "effective timeout")
        need(isinstance(run_id, str) and run_id, "invalid_verification_material", "Run id is required")
        need(isinstance(snapshot, dict) and isinstance(snapshot.get("digest"), str),
             "invalid_verification_material", "Input snapshot is missing")
        runtime_check = _copy_json(check, "adjusted check")
        launch = self.launch_recipe(runtime_check, argv, cwd_relative)
        # Focused callers predating the Runtime integration may omit
        # ``effective_env``.  Their material remains structurally valid, while
        # Runtime always supplies the complete mapping it will pass to Popen.
        if effective_env is None:
            effective_env = dict(extra_env or {})
        environment = self.environment_input(extra_env, managed_context,
                                             effective_env=effective_env)
        payload = {
            "definition_ref": validate_ref(context.definition_ref, project, "definition_ref"),
            "execution_subject": _copy_json(context.execution_subject, "execution_subject"),
            "task_revision": context.task_revision,
            "candidate_ref": _copy_json(context.candidate_ref, "candidate_ref") if context.candidate_ref is not None else None,
            "input_snapshot_digest": snapshot["digest"],
            "input_snapshot_blob": self.runtime.s.blob_put(canonical(snapshot)),
            "runtime_check_blob": self.runtime.s.blob_put(canonical(runtime_check)),
            "launch_recipe_blob": self.runtime.s.blob_put(canonical(launch)),
            "environment_blob": self.runtime.s.blob_put(canonical(environment)),
            "resolved_timeout": float(timeout),
            "timeout_authorization_refs": list(context.timeout_authorization_refs),
            "test_artifact_refs": list(context.test_artifact_refs),
        }
        _sha(payload["input_snapshot_blob"], "input_snapshot_blob")
        _sha(payload["runtime_check_blob"], "runtime_check_blob")
        _sha(payload["launch_recipe_blob"], "launch_recipe_blob")
        _sha(payload["environment_blob"], "environment_blob")
        dependencies = [payload["definition_ref"]]
        if context.candidate_ref is not None:
            dependencies.append(context.candidate_ref)
        dependencies.extend(context.test_artifact_refs)
        pin = self._pin(actor, project, EXECUTION_MATERIAL_KIND, payload, dependencies,
                        {**context.captured_from, "run": run_id}, f"verification:execution:{run_id}")
        return {"pin": pin, "payload": payload, "runtime_check": runtime_check,
                "launch_recipe": launch, "environment": environment}

    def validate_stored_pin(self, actor, project: str, pin: dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
        """Resolve a stored pin through E1; never trust a caller-supplied body."""
        need(isinstance(pin, dict) and set(pin) == {"id", "digest"},
             "invalid_verification_material", "verification_material must contain exactly id and digest")
        _sha(pin["digest"], "verification_material.digest")
        backend = self._require_backend()
        getter = getattr(backend, "object_get", None)
        need(callable(getter), "verification_material_unavailable", "E1 material read boundary is not connected")
        try:
            row = getter(actor, project, pin["id"])
        except Fault:
            raise
        except (AssertionError, KeyError, LookupError) as exc:
            raise Fault("invalid_verification_material", "Stored verification material is missing", pin["id"]) from exc
        need(isinstance(row, dict) and row.get("kind") == "material" and row.get("digest") == pin["digest"],
             "invalid_verification_material", "Stored verification material is missing or changed")
        body = row.get("body")
        if isinstance(body, str): body = parse_json(body)
        need(isinstance(body, dict) and body.get("format") == MATERIAL_FORMAT and
             body.get("material_kind") == EXECUTION_MATERIAL_KIND,
             "invalid_verification_material", "Pin does not identify execution material")
        payload_blob = body.get("payload_blob")
        _sha(payload_blob, "payload_blob")
        payload = parse_json(self.runtime.s.blob_get(payload_blob))
        need(isinstance(payload, dict) and body.get("semantic_digest") == digest(payload),
             "integrity_error", "Stored verification payload digest differs")
        captured = body.get("captured_from", {})
        if run_id is not None:
            need(captured.get("run") == run_id, "invalid_verification_material", "Material pin belongs to another run")
        return row


__all__ = [
    "DELIVERY_MATERIAL_KIND", "EXECUTION_MATERIAL_KIND", "ExecutionMaterialContext",
    "MATERIAL_FORMAT", "PLAN_MATERIAL_KIND", "VerificationMaterialCoordinator",
    "require_current", "sealed_snapshot_digest", "validate_git_material_payload",
    "validate_ref", "validate_sealed_snapshot", "validate_test_plan_definition_identity",
]
