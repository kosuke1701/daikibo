"""Controller-owned provenance for artifacts collected from a sealed candidate.

The public collection action accepts only selectors.  This module is the
read-only contract shared by that live action and the portable assurance
archive validator.  It deliberately resolves candidate identity through the
existing candidate provenance context instead of trusting a caller supplied
run, receipt, epoch, or environment value.
"""
from __future__ import annotations

import fnmatch
from typing import Any, Callable

from .assurance_relations import validate_typed_ref
from .candidate_provenance import _resolve_candidate_identity_state
from .common import Fault, digest, need, parse_json, relative_path
from .knowledge import Knowledge


FORMAT = "daikibo.artifact-production.v1"
MANIFEST_FORMAT = "daikibo.artifact-output.v1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_OUTPUTS = 200
MAX_MATERIAL_BYTES = 1024 * 1024
MATERIAL_KIND = "artifact_production"
MATERIAL_FORMAT = "daikibo.assurance-material.v1"

_PAYLOAD_FIELDS = {
    "format", "project", "task_ref", "candidate_ref", "implementation_run",
    "implementation_receipt", "producer_actor", "producer_epoch",
    "manifest_repository", "manifest_path", "manifest_blob", "declaration_id",
    "artifact_ref", "collection_key",
}
_MANIFEST_FIELDS = {"format", "outputs"}
_OUTPUT_FIELDS = {"declaration_id", "kind", "body"}
_FORBIDDEN_BODY_FIELDS = frozenset({
    "accepted", "accepted_at", "artifact_production", "material", "provenance",
    "producer", "producer_actor", "run", "receipt", "status",
})


def _failure(code: str):
    def fail(kind: str, message: str, details: Any = None) -> None:
        if code == "invalid_archive":
            raise Fault(code, message, details)
        mapped = {
            "invalid": "invalid_reference", "missing": "unknown_reference",
            "cross_project": "cross_project", "stale": "stale_reference",
            "ambiguous": "ambiguous_reference", "unresolved": "unresolved_reference",
            "integrity": "integrity_error",
        }
        raise Fault(mapped.get(kind, code), message, details)
    return fail


def _strip_identity(ref: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in ref.items() if key not in {"identity_digest", "semantic_kind"}}


def task_ref(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "task_revision", "project": state["project"],
        "task": state["task_id"], "revision": state["task_revision"],
        "definition_digest": state["task_definition_digest"],
    }


def observed_ref(state: dict[str, Any]) -> dict[str, Any]:
    observed = state["observed"]
    return {
        "kind": "observed_result", "project": state["project"],
        "receipt": state["receipt_id"], "run": state["run_id"],
        "receipt_digest": digest(observed),
        "run_binding": state["run"].get("binding"),
        "snapshot_digest": state["snapshot_digest"],
        "result_digest": digest(observed.get("result", {})),
    }


def collection_key(candidate: dict[str, Any], repository: str, path: str,
                   manifest_blob: str, declaration_id: str) -> str:
    # Keep the ordered tuple explicit.  It is the stable identity for one
    # declaration collected from one sealed candidate output packet.
    return digest([candidate, repository, path, manifest_blob, declaration_id])


def _run_metadata(state: dict[str, Any], *, code: str) -> dict[str, Any]:
    control = state["run_body"].get("execution_control")
    need(isinstance(control, dict), code,
         "Implementation run has no controller producer record", state["run_id"])
    actor = control.get("producer_actor")
    need(type(actor) is str and bool(actor) and "\x00" not in actor, code,
         "Implementation producer actor is missing", state["run_id"])
    revision = control.get("task_revision")
    epoch = control.get("epoch")
    need(type(revision) is int and revision == state["task_revision"], code,
         "Implementation producer Task revision differs", state["run_id"])
    need(type(epoch) is int and epoch == state["candidate_epoch"], code,
         "Implementation producer epoch differs", state["run_id"])
    return {"producer_actor": actor, "task_revision": revision, "epoch": epoch}


def _allowed_output_path(task_body: dict[str, Any], repository: str, path: str,
                         *, code: str) -> None:
    relative_path(path)
    patterns = task_body.get("write_paths")
    need(isinstance(patterns, list) and patterns, code,
         "Task has no fixed output path declarations")
    names = (path, f"{repository}/{path}")
    need(any(fnmatch.fnmatchcase(name, pattern) for name in names for pattern in patterns),
         code, "Manifest path is outside the Task write scope", path)


def _artifact_declarations(task_body: dict[str, Any], *, code: str) -> dict[str, dict[str, Any]]:
    structural = task_body.get("structural_obligations")
    need(isinstance(structural, dict), code,
         "Task has no fixed required output declaration")
    outputs = structural.get("required_outputs")
    need(isinstance(outputs, list), code,
         "Task required output declarations are unavailable")
    result: dict[str, dict[str, Any]] = {}
    for item in outputs:
        need(isinstance(item, dict) and isinstance(item.get("id"), str), code,
             "Task required output declaration is malformed")
        if item.get("realization_kind") == "artifact":
            ident = item["id"]
            need(ident not in result, code, "Task required output declaration is duplicated", ident)
            result[ident] = item
    return result


def _validate_manifest(state: dict[str, Any], repository: str, path: str,
                       blob_get: Callable[[str], bytes], *, code: str) -> tuple[dict[str, Any], list[dict[str, Any]], bytes]:
    task_body = state["selected_task_body"]
    _allowed_output_path(task_body, repository, path, code=code)
    snapshot_repos = state["snapshot"]["repos"]
    need(repository in snapshot_repos, code,
         "Manifest repository is outside the sealed candidate snapshot", repository)
    files = snapshot_repos[repository].get("files", {})
    entry = files.get(path)
    need(isinstance(entry, dict) and entry.get("kind") == "file", code,
         "Manifest path is not a regular file in the sealed candidate", path)
    need(set(entry) == {"kind", "blob", "mode", "size"}, code,
         "Manifest snapshot entry is malformed", path)
    blob = entry.get("blob")
    need(isinstance(blob, str) and len(blob) == 64 and all(c in "0123456789abcdef" for c in blob),
         code, "Manifest snapshot blob is malformed", path)
    raw = blob_get(blob)
    need(isinstance(raw, (bytes, bytearray)) and digest(bytes(raw)) == blob, code,
         "Manifest snapshot blob is missing or changed", blob)
    raw = bytes(raw)
    need(len(raw) == entry.get("size") and len(raw) <= MAX_MANIFEST_BYTES, code,
         "Manifest exceeds its packet byte bound", path)
    try:
        manifest = parse_json(raw, limit=MAX_MANIFEST_BYTES)
    except Fault as exc:
        raise Fault(code, "Manifest is not valid UTF-8 JSON", path) from exc
    need(isinstance(manifest, dict) and set(manifest) == _MANIFEST_FIELDS and
         manifest.get("format") == MANIFEST_FORMAT and isinstance(manifest.get("outputs"), list),
         code, "Artifact output manifest shape differs", path)
    outputs = manifest["outputs"]
    need(len(outputs) <= MAX_OUTPUTS, code,
         "Artifact output packet exceeds its bounded item count", path)
    declarations = _artifact_declarations(task_body, code=code)
    seen: set[str] = set()
    for output in outputs:
        need(isinstance(output, dict) and set(output) == _OUTPUT_FIELDS, code,
             "Artifact output item shape differs", path)
        ident = output.get("declaration_id")
        need(isinstance(ident, str) and ident in declarations, code,
             "Artifact output declaration is not a fixed Task declaration", ident)
        need(ident not in seen, code, "Artifact output declaration is duplicated", ident)
        seen.add(ident)
        kind, body = output.get("kind"), output.get("body")
        need(isinstance(kind, str), code, "Artifact output kind is malformed", ident)
        try:
            Knowledge.validate_body(kind, body)
        except Fault as exc:
            raise Fault(code, "Artifact output body is invalid", {"declaration_id": ident, "error": exc.as_dict()}) from exc
        need(not (_FORBIDDEN_BODY_FIELDS & set(body)), code,
             "Artifact output body contains producer or acceptance state", ident)
    return manifest, outputs, raw


def _resolve_candidate(ref: dict[str, Any], context: Any, *, code: str) -> dict[str, Any]:
    # The candidate implementation's shared resolver is the source of truth
    # for run/receipt/snapshot/Task-history closure.  A legacy candidate that
    # predates the producer record remains historical and is not retrofitted.
    state = _resolve_candidate_identity_state(_strip_identity(ref), context,
                                              failure=_failure(code))
    _run_metadata(state, code=code)
    return state


def prepare_artifact_collection(candidate: dict[str, Any], context: Any,
                                repository: str, path: str,
                                blob_get: Callable[[str], bytes], *, project: str,
                                task: str, revision: int, current_candidate: str | None,
                                current_task_status: str, current_task_epoch: int | None = None,
                                code: str = "integrity_error") -> dict[str, Any]:
    """Resolve a selector and its exact sealed manifest without writing state."""
    candidate = _strip_identity(validate_typed_ref(_strip_identity(candidate), project=project,
                                                   expected_kinds={"candidate"}))
    need(candidate["task"] == task and candidate["task_revision"] == revision,
         code, "Candidate is not bound to the requested current Task revision")
    need(current_candidate == candidate["candidate"], code,
         "Candidate is not the current Task candidate")
    need(current_task_status in {"submitted", "completed"}, code,
         "Task is not in a collectable state")
    state = _resolve_candidate(candidate, context, code=code)
    need(state["task_id"] == task and state["task_revision"] == revision, code,
         "Candidate Task revision differs from the current Task")
    if current_task_epoch is not None:
        need(state["candidate_epoch"] == current_task_epoch, code,
             "Candidate epoch differs from the current Task epoch")
    metadata = _run_metadata(state, code=code)
    manifest, outputs, raw = _validate_manifest(state, repository, path, blob_get, code=code)
    return {"state": state, "candidate_ref": candidate, "manifest": manifest,
            "outputs": outputs, "manifest_blob": digest(raw), "producer": metadata}


def validate_artifact_production_material(payload: dict[str, Any], *, context: Any,
                                          resolve_artifact: Callable[[dict[str, Any]], dict[str, Any]],
                                          blob_get: Callable[[str], bytes], project: str,
                                          code: str = "integrity_error") -> dict[str, Any]:
    """Validate one immutable artifact-production payload on live/archive data."""
    need(isinstance(payload, dict) and set(payload) == _PAYLOAD_FIELDS, code,
         "Artifact production payload shape differs")
    need(payload.get("format") == FORMAT and payload.get("project") == project, code,
         "Artifact production payload identity differs")
    task = _strip_identity(validate_typed_ref(payload["task_ref"], project=project,
                                              expected_kinds={"task_revision"}))
    candidate = _strip_identity(validate_typed_ref(payload["candidate_ref"], project=project,
                                                    expected_kinds={"candidate"}))
    artifact = _strip_identity(validate_typed_ref(payload["artifact_ref"], project=project,
                                                   expected_kinds={"artifact"}))
    need(task["task"] == candidate["task"] and task["revision"] == candidate["task_revision"],
         code, "Artifact production Task and candidate pins differ")
    state = _resolve_candidate(candidate, context, code=code)
    need(state["task_id"] == task["task"] and state["task_revision"] == task["revision"] and
         state["task_definition_digest"] == task["definition_digest"], code,
         "Artifact production Task revision differs")
    metadata = _run_metadata(state, code=code)
    need(payload["implementation_run"] == state["run_id"] and
         payload["implementation_receipt"] == state["receipt_id"], code,
         "Artifact production execution identity differs")
    need(payload["producer_actor"] == metadata["producer_actor"] and
         payload["producer_epoch"] == metadata["epoch"], code,
         "Artifact production producer identity differs")
    repository, path = payload["manifest_repository"], payload["manifest_path"]
    need(type(repository) is str and bool(repository), code, "Manifest repository is malformed")
    need(type(path) is str, code, "Manifest path is malformed")
    manifest, outputs, raw = _validate_manifest(state, repository, path, blob_get, code=code)
    need(payload["manifest_blob"] == digest(raw), code,
         "Artifact production manifest blob differs")
    declaration = payload["declaration_id"]
    matches = [item for item in outputs if item["declaration_id"] == declaration]
    need(len(matches) == 1, code, "Artifact production declaration is absent from its manifest")
    output = matches[0]
    expected_key = collection_key(candidate, repository, path, payload["manifest_blob"], declaration)
    need(payload["collection_key"] == expected_key, code,
         "Artifact production collection identity differs")
    resolved = resolve_artifact(artifact)
    need(resolved.get("project") == project and resolved.get("id") == artifact["artifact"] and
         resolved.get("revision") == artifact["revision"] and
         resolved.get("digest") == artifact["body_digest"] and
         resolved.get("kind") == output["kind"] and resolved.get("body") == output["body"], code,
         "Produced artifact body or revision differs")
    return {"state": state, "candidate_ref": candidate, "task_ref": task,
            "artifact_ref": artifact, "artifact": resolved, "manifest": manifest,
            "output": output, "producer": metadata}


def resolve_produced_artifact(storage: Any, ref: dict[str, Any], *, project: str,
                              code: str = "integrity_error", current: bool = False) -> dict[str, Any]:
    """Resolve the Knowledge revision named by a production material.

    Consumer-P output rows are intentionally still drafts.  Existing
    Assurance artifact endpoints remain accepted-only; this small resolver is
    the explicit production-material boundary used by P and by the
    produced-by Consumer-M matcher.
    """
    normalized = _strip_identity(validate_typed_ref(
        _strip_identity(ref), project=project, expected_kinds={"artifact"},
    ))
    item = storage.one(
        "SELECT * FROM artifacts WHERE id=? AND project=?",
        (normalized["artifact"], project),
    )
    need(item is not None and item.get("status") in {"draft", "accepted"}, code,
         "Produced artifact endpoint is missing or invalid", normalized["artifact"])
    if current:
        need(item.get("revision") == normalized["revision"] and
             item.get("digest") == normalized["body_digest"],
             "stale_reference", "Produced artifact endpoint is not the current head",
             normalized["artifact"])
    revision = storage.one(
        "SELECT * FROM revisions WHERE artifact=? AND revision=?",
        (normalized["artifact"], normalized["revision"]),
    )
    need(revision is not None and revision.get("status") in {"draft", "accepted"}, code,
         "Produced artifact revision is missing or invalid", normalized["artifact"])
    body = parse_json(revision["body"])
    need(revision["digest"] == normalized["body_digest"] and digest(body) == normalized["body_digest"],
         code, "Produced artifact revision digest differs", normalized["artifact"])
    need(isinstance(body, dict), code, "Produced artifact body is malformed", normalized["artifact"])
    return {"id": item["id"], "project": item["project"], "kind": item["kind"],
            "revision": revision["revision"], "status": revision["status"],
            "body": body, "digest": revision["digest"]}


def _material_identity(ref: dict[str, Any], *, project: str,
                       expected_kind: str) -> dict[str, Any]:
    normalized = _strip_identity(validate_typed_ref(
        _strip_identity(ref), project=project, expected_kinds={expected_kind},
    ))
    return normalized


def _production_material_rows(storage: Any, *, project: str,
                              artifact_ref: dict[str, Any],
                              task_ref: dict[str, Any]) -> list[dict[str, Any]]:
    """Select only material rows indexed by this exact artifact/Task pair.

    Assurance's immutable typed-reference index is the saved locator for a
    material envelope.  Filter through it before reading any payload CAS so a
    broken production packet for another Task cannot poison this Task's
    local matcher.  The selected rows still receive full envelope, payload,
    P-provenance, and dependency validation in the shared resolver below.
    """
    artifact = _material_identity(artifact_ref, project=project, expected_kind="artifact")
    task = _material_identity(task_ref, project=project, expected_kind="task_revision")
    return storage.all(
        """
        SELECT DISTINCT material.*
        FROM assurance_objects AS material
        JOIN assurance_refs AS artifact_index
          ON artifact_index.object_id = material.id
        JOIN assurance_refs AS task_index
          ON task_index.object_id = material.id
        WHERE material.project=? AND material.kind='material'
          AND artifact_index.purpose LIKE 'body.dependency_refs[%]'
          AND artifact_index.ref_kind='artifact'
          AND artifact_index.ref_id=?
          AND artifact_index.ref_revision=?
          AND artifact_index.ref_digest=?
          AND task_index.purpose LIKE 'body.dependency_refs[%]'
          AND task_index.ref_kind='task_revision'
          AND task_index.ref_id=?
          AND task_index.ref_revision=?
          AND task_index.ref_digest=?
        ORDER BY material.id
        """,
        (project, artifact["artifact"], str(artifact["revision"]), artifact["body_digest"],
         task["task"], str(task["revision"]), task["definition_digest"]),
    )


def resolve_artifact_production_material(storage: Any, *, project: str,
                                         artifact_ref: dict[str, Any],
                                         task_ref: dict[str, Any],
                                         context: Any,
                                         resolve_artifact: Callable[[dict[str, Any]], dict[str, Any]],
                                         blob_get: Callable[[str], bytes],
                                         code: str = "integrity_error",
                                         missing_code: str = "artifact_producer_material_missing",
                                         current: bool = True) -> dict[str, Any]:
    """Resolve one exact P material without scanning unrelated project rows.

    Both live Consumer-M readers use this boundary.  Historical archive code
    deliberately keeps its own retained-data walk, while a live current edge
    asks ``resolve_artifact`` to enforce the current artifact head.
    """
    artifact = _material_identity(artifact_ref, project=project, expected_kind="artifact")
    task = _material_identity(task_ref, project=project, expected_kind="task_revision")
    rows = _production_material_rows(
        storage, project=project, artifact_ref=artifact, task_ref=task,
    )
    matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    required_envelope = {
        "format", "material_kind", "project", "origin", "semantic_digest",
        "payload_blob", "dependency_refs", "captured_from",
    }
    for row in rows:
        envelope = parse_json(row["body"], limit=MAX_MATERIAL_BYTES)
        need(isinstance(envelope, dict) and set(envelope) == required_envelope and
             envelope.get("format") == MATERIAL_FORMAT and
             envelope.get("material_kind") == MATERIAL_KIND and
             envelope.get("project") == project and
             digest(envelope) == row.get("digest"),
             "integrity_error", "Artifact production material envelope differs", row.get("id"))
        payload_blob = envelope.get("payload_blob")
        need(isinstance(payload_blob, str), "integrity_error",
             "Artifact production material payload reference is malformed", row.get("id"))
        payload = parse_json(blob_get(payload_blob), limit=MAX_MATERIAL_BYTES)
        need(isinstance(payload, dict) and digest(payload) == envelope.get("semantic_digest"),
             "integrity_error", "Artifact production material payload digest differs", row.get("id"))
        payload_artifact = payload.get("artifact_ref")
        payload_task = payload.get("task_ref")
        need(isinstance(payload_artifact, dict) and isinstance(payload_task, dict),
             "integrity_error", "Artifact production material identity is malformed", row.get("id"))
        need(_strip_identity(payload_artifact) == artifact and
             _strip_identity(payload_task) == task,
             "integrity_error", "Artifact production material index differs from payload",
             row.get("id"))
        matches.append((row, envelope, payload))

    if not matches:
        raise Fault(missing_code, "Artifact production material is missing", artifact)
    need(len(matches) == 1, "ambiguous_reference",
         "Artifact production material is ambiguous", artifact)
    row, envelope, payload = matches[0]
    checked = validate_artifact_production_material(
        payload, context=context, resolve_artifact=resolve_artifact,
        blob_get=blob_get, project=project, code=code,
    )
    expected_dependencies = [
        checked["task_ref"], checked["candidate_ref"],
        observed_ref(checked["state"]), checked["artifact_ref"],
    ]
    dependency_values = envelope.get("dependency_refs")
    need(isinstance(dependency_values, list), "integrity_error",
         "Artifact production material dependencies are malformed", row.get("id"))
    actual_dependencies = [
        _strip_identity(validate_typed_ref(item, project=project))
        for item in dependency_values
    ]
    need(actual_dependencies == expected_dependencies, "integrity_error",
         "Artifact production material dependencies differ", row.get("id"))
    if current:
        # The historical P resolver validates the pinned revision/body.  A
        # live edge additionally requires that pin to remain the Knowledge
        # artifact's current head; a later revise must stale the edge.
        resolve_produced_artifact(
            storage, checked["artifact_ref"], project=project,
            code="stale_reference", current=True,
        )
    return checked


__all__ = [
    "FORMAT", "MANIFEST_FORMAT", "MATERIAL_KIND", "MATERIAL_FORMAT",
    "MAX_MANIFEST_BYTES", "MAX_MATERIAL_BYTES", "MAX_OUTPUTS", "collection_key", "task_ref",
    "observed_ref", "prepare_artifact_collection",
    "validate_artifact_production_material", "resolve_produced_artifact",
    "resolve_artifact_production_material",
]
