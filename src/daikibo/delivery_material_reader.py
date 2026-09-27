"""Read-only Delivery snapshot and Git material collection.

The Delivery writer stores one immutable snapshot material and one immutable
actual commit material per repository.  The actual material deliberately
contains only the Git observation and a typed dependency on its snapshot; it
does not duplicate the Delivery checks or build declarations.  This module is
the small read boundary that follows that dependency, retains the complete
repository population, and exposes the two center types separately.

There are no writer calls in this module.  In particular, an observed
``body.git`` row without a material pin is reported as missing material.  A
reader must never create a pin, capture a snapshot, run Git, or repair a CAS
leaf while collecting evidence.
"""
from __future__ import annotations

from typing import Any

from .assurance_relations import validate_typed_ref
from .common import Fault, canonical, digest, need, parse_json
from .verification_materials import validate_sealed_snapshot


DELIVERY_MATERIAL_FORMAT = "assurance.delivery-material.v1"
DELIVERY_REPOSITORY_FORMAT = "delivery-repository.v1"
DELIVERY_MATERIAL_EXTRACTOR = "delivery-material.v1"
DELIVERY_REPOSITORY_EXTRACTOR = "delivery-repository.v1"

_MATERIAL_KEYS = {
    "format", "project", "delivery_id", "snapshot_ref", "snapshot_payload",
    "check_refs", "declared_outputs", "repositories", "actual_commit_refs",
    "input_ref", "centers", "status", "diagnostics",
}
_REPOSITORY_KEYS = {
    "format", "repository", "saved_observed_identity", "actual_ref",
    "candidate_refs", "integrity_state", "currentness_state", "diagnostics",
}
_CENTER_KEYS = {"delivery_snapshots", "actual_commits"}


def _copy(value: Any) -> Any:
    return parse_json(canonical(value))


def _identity(value: Any) -> Any:
    """Drop resolver-only projections, retaining exact persisted ref syntax."""
    if isinstance(value, dict):
        return {key: _identity(item) for key, item in value.items()
                if key not in {"identity_digest", "semantic_kind"}}
    if isinstance(value, list):
        return [_identity(item) for item in value]
    return value


def _semantic(value: Any) -> Any:
    """Project capture pins out of Delivery refs for meaning comparison.

    A restart can capture an equivalent snapshot under another material pin.
    The pin remains in the returned exact candidate references, while the
    snapshot meaning used to establish belonging ignores only that resolver
    capture identity.  Git commit/tree/ref and all payload values remain
    semantic.
    """
    if isinstance(value, dict):
        result = {key: _semantic(item) for key, item in value.items()}
        if result.get("kind") in {"delivery_snapshot", "actual_delivery_commit"}:
            result.pop("pin", None)
        # These fields describe where/how a saved observation was collected;
        # they do not change the repository material meaning.  A restart can
        # retain the same commit under another checkout path or reconciliation
        # marker and must still compare as the same capture.
        observed = result.get("observed_result")
        if isinstance(observed, dict):
            observed.pop("git_dir", None)
            observed.pop("reconciled", None)
        return result
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def _diagnostic(code: str, reason: str, **details: Any) -> dict[str, Any]:
    result = {"code": code, "reason": reason}
    for key, value in details.items():
        if value is not None:
            result[key] = _copy(value)
    return result


def _material_body(row: dict[str, Any], *, project: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read one immutable material envelope and its canonical payload."""
    raw = row.get("body")
    body = parse_json(raw) if isinstance(raw, str) else raw
    need(isinstance(body, dict), "integrity_error", "Delivery material envelope is not an object", row.get("id"))
    expected = {"format", "material_kind", "project", "origin", "semantic_digest",
                "payload_blob", "dependency_refs", "captured_from"}
    need(set(body) == expected and body.get("format") == "daikibo.assurance-material.v1",
         "integrity_error", "Delivery material envelope shape differs", row.get("id"))
    need(body.get("project") == project, "cross_project", "Delivery material belongs to another project", row.get("id"))
    need(isinstance(body.get("payload_blob"), str), "integrity_error", "Delivery material payload pointer is malformed", row.get("id"))
    payload_raw = row.get("_store").blob_get(body["payload_blob"])
    payload = parse_json(payload_raw)
    need(isinstance(payload, dict) and digest(payload) == body.get("semantic_digest"),
         "integrity_error", "Delivery material payload digest differs", row.get("id"))
    return body, payload


def _material_ref(row: dict[str, Any], body: dict[str, Any], payload: dict[str, Any], *, project: str) -> dict[str, Any]:
    """Reconstruct the exact typed ref represented by one stored material."""
    kind = body.get("material_kind")
    pin = {"id": row.get("id"), "digest": row.get("digest")}
    if kind == "delivery_snapshot":
        binding = payload.get("binding")
        snapshot = payload.get("snapshot")
        need(isinstance(binding, dict) and isinstance(snapshot, dict),
             "integrity_error", "Delivery snapshot material identity is incomplete", row.get("id"))
        snapshot_digest = snapshot.get("digest")
        need(isinstance(snapshot_digest, str), "integrity_error", "Delivery snapshot digest is missing", row.get("id"))
        ref = {"kind": "delivery_snapshot", "project": project,
               "delivery": payload.get("delivery"),
               "binding_digest": digest(binding), "snapshot_digest": snapshot_digest,
               "pin": pin}
        validate_typed_ref(ref, project=project, expected_kinds={"delivery_snapshot"})
        return ref
    need(kind == "actual_delivery_commit", "invalid_material", "Unexpected Delivery material kind", kind)
    nested = payload.get("delivery_snapshot_ref")
    validate_typed_ref(nested, project=project, expected_kinds={"delivery_snapshot"})
    ref = {"kind": "actual_delivery_commit", "project": project,
           "delivery": _identity(nested), "repository": payload.get("repository"),
           "object_format": payload.get("object_format"), "commit": payload.get("commit"),
           "tree": payload.get("tree"), "pin": pin}
    validate_typed_ref(ref, project=project, expected_kinds={"actual_delivery_commit"})
    return ref


def _check_ref(project: str, snapshot_ref: dict[str, Any], check: Any, index: int) -> dict[str, Any]:
    need(isinstance(check, dict) and isinstance(check.get("id"), str) and check["id"],
         "integrity_error", "Delivery check identity is malformed", index)
    ref = {"kind": "delivery_check", "project": project, "delivery": _identity(snapshot_ref),
           "check_id": check["id"], "check_digest": digest(check)}
    validate_typed_ref(ref, project=project, expected_kinds={"delivery_check"})
    return ref


def _repository_identity(value: Any, repository: str, snapshot_digest: str) -> tuple[str, str, str, str] | None:
    if not isinstance(value, dict):
        return None
    if value.get("repository") != repository or value.get("snapshot") != snapshot_digest:
        return None
    commit, tree, ref = value.get("commit"), value.get("tree"), value.get("ref")
    if not all(isinstance(item, str) and item for item in (commit, tree, ref)):
        return None
    object_format = value.get("object_format")
    if object_format is None:
        object_format = "sha256" if len(commit) == 64 else "sha1" if len(commit) == 40 else None
    if object_format not in {"sha1", "sha256"}:
        return None
    width = 64 if object_format == "sha256" else 40
    hexchars = set("0123456789abcdef")
    if (len(commit) != width or len(tree) != width or
            set(commit) - hexchars or set(tree) - hexchars):
        return None
    return commit, tree, ref, object_format


def _validate_repository_entry(value: Any, *, project: str) -> dict[str, Any]:
    need(type(value) is dict and set(value) == _REPOSITORY_KEYS,
         "invalid_delivery_material", "Delivery repository entry shape differs")
    need(value["format"] == DELIVERY_REPOSITORY_FORMAT,
         "invalid_delivery_material", "Delivery repository format differs")
    need(isinstance(value["repository"], str) and value["repository"],
         "invalid_delivery_material", "Delivery repository id is malformed")
    saved = value["saved_observed_identity"]
    need(saved is None or isinstance(saved, dict),
         "invalid_delivery_material", "Saved Delivery Git identity is malformed")
    actual = value["actual_ref"]
    if actual is not None:
        validate_typed_ref(actual, project=project, expected_kinds={"actual_delivery_commit"})
        need(actual["repository"] == value["repository"],
             "invalid_delivery_material", "Actual repository reference does not belong to its entry")
    need(isinstance(value["candidate_refs"], list),
         "invalid_delivery_material", "Delivery candidate refs are not a list")
    for ref in value["candidate_refs"]:
        validate_typed_ref(ref, project=project, expected_kinds={"actual_delivery_commit"})
        need(ref["repository"] == value["repository"],
             "invalid_delivery_material", "Candidate repository reference does not belong to its entry")
    need(value["integrity_state"] in {"verified", "missing_observation", "missing_material", "invalid", "ambiguous"},
         "invalid_delivery_material", "Delivery repository integrity state is invalid")
    need(isinstance(value["currentness_state"], dict) and
         value["currentness_state"].get("state") in {"not_evaluated", "current", "stale", "unknown"},
         "invalid_delivery_material", "Delivery repository currentness state is invalid")
    if "resolution_current" in value["currentness_state"]:
        need(type(value["currentness_state"]["resolution_current"]) is bool,
             "invalid_delivery_material", "Delivery currentness boolean is invalid")
    if "resolution_state" in value["currentness_state"]:
        need(type(value["currentness_state"]["resolution_state"]) is str,
             "invalid_delivery_material", "Delivery currentness resolver state is invalid")
    need(isinstance(value["diagnostics"], list),
         "invalid_delivery_material", "Delivery repository diagnostics are not a list")
    return value


def validate_delivery_material(value: Any, *, project: str | None = None) -> dict[str, Any]:
    """Validate the normalized read-only Delivery material wire."""
    need(type(value) is dict and set(value) == _MATERIAL_KEYS,
         "invalid_delivery_material", "Delivery material shape differs")
    need(value["format"] == DELIVERY_MATERIAL_FORMAT,
         "invalid_delivery_material", "Delivery material format differs")
    if project is not None:
        need(value["project"] == project, "cross_project", "Delivery material belongs to another project")
    for field in ("project", "delivery_id"):
        need(isinstance(value[field], str) and value[field],
             "invalid_delivery_material", f"Delivery material {field} is invalid")
        need("\x00" not in value[field],
             "invalid_delivery_material", f"Delivery material {field} contains a NUL")
    snapshot_ref = _identity(validate_typed_ref(
        value["snapshot_ref"], project=value["project"], expected_kinds={"delivery_snapshot"},
    ))
    need(snapshot_ref["delivery"] == value["delivery_id"],
         "invalid_delivery_material", "Delivery material Delivery ID differs from its snapshot")
    need(isinstance(value["snapshot_payload"], dict),
         "invalid_delivery_material", "Delivery material snapshot payload is missing")
    snapshot_payload = value["snapshot_payload"]
    need(snapshot_payload.get("delivery") == value["delivery_id"],
         "invalid_delivery_material", "Delivery snapshot payload ID differs")
    binding = snapshot_payload.get("binding")
    snapshot = snapshot_payload.get("snapshot")
    need(isinstance(binding, dict) and digest(binding) == snapshot_ref["binding_digest"],
         "invalid_delivery_material", "Delivery snapshot payload binding differs")
    need(isinstance(snapshot, dict) and snapshot.get("digest") == snapshot_ref["snapshot_digest"],
         "invalid_delivery_material", "Delivery snapshot payload digest differs")
    sealed_repositories = snapshot.get("repos") if isinstance(snapshot, dict) else None
    need(isinstance(sealed_repositories, dict),
         "invalid_delivery_material", "Delivery snapshot repository map is missing")
    need(isinstance(value["check_refs"], list) and isinstance(value["actual_commit_refs"], list),
         "invalid_delivery_material", "Delivery material references are malformed")
    for ref in value["check_refs"]:
        validate_typed_ref(ref, project=value["project"], expected_kinds={"delivery_check"})
        need(ref["delivery"] == value["snapshot_ref"],
             "invalid_delivery_material", "Delivery check is bound to another snapshot")
    actuals = []
    for ref in value["actual_commit_refs"]:
        normalized = validate_typed_ref(ref, project=value["project"], expected_kinds={"actual_delivery_commit"})
        need(_semantic(normalized["delivery"]) == _semantic(value["snapshot_ref"]),
             "invalid_delivery_material", "Actual commit belongs to another snapshot")
        actuals.append(ref)
    actual_repositories = [ref["repository"] for ref in actuals]
    need(actual_repositories == sorted(actual_repositories) and
         len(actual_repositories) == len(set(actual_repositories)),
         "invalid_delivery_material", "Delivery actual population is unordered or duplicated")
    need(isinstance(value["declared_outputs"], dict),
         "invalid_delivery_material", "Delivery declared output inventory is malformed")
    need(set(value["declared_outputs"]) == {"status", "items"} and
         value["declared_outputs"]["status"] in
         {"available", "explicit_empty", "unverified", "invalid", "not_applicable"} and
         isinstance(value["declared_outputs"]["items"], list),
         "invalid_delivery_material", "Delivery declared output inventory is invalid")
    need(isinstance(value["repositories"], list),
         "invalid_delivery_material", "Delivery repository inventory is not a list")
    repositories = [_validate_repository_entry(item, project=value["project"]) for item in value["repositories"]]
    names = [item["repository"] for item in repositories]
    need(names == sorted(names) and len(names) == len(set(names)),
         "invalid_delivery_material", "Delivery repository inventory is unordered or duplicated")
    need(set(names) == set(sealed_repositories),
         "invalid_delivery_material", "Delivery repository inventory differs from sealed snapshot")
    for entry in repositories:
        if entry["saved_observed_identity"] is not None:
            need(_repository_identity(
                entry["saved_observed_identity"], entry["repository"],
                snapshot_ref["snapshot_digest"],
            ) is not None,
                 "invalid_delivery_material", "Saved Delivery Git identity is invalid")
        candidate_refs = entry["candidate_refs"]
        need(candidate_refs == sorted(candidate_refs, key=canonical) and
             len(candidate_refs) == len({canonical(ref) for ref in candidate_refs}),
             "invalid_delivery_material", "Delivery candidate refs are unordered or duplicated")
        if entry["actual_ref"] is not None:
            need(entry["actual_ref"] in candidate_refs,
                 "invalid_delivery_material", "Delivery actual reference is absent from its candidates")
        for ref in candidate_refs:
            need(_semantic(ref["delivery"]) == _semantic(snapshot_ref),
                 "invalid_delivery_material", "Delivery candidate belongs to another snapshot")
        matching = [ref for ref in actuals if ref["repository"] == entry["repository"]]
        need(len(matching) <= 1 and
             (not matching or entry["actual_ref"] == matching[0]),
             "invalid_delivery_material", "Delivery actual reference population differs from repository entries")
    need({ref["repository"] for ref in actuals}.issubset(set(names)),
         "invalid_delivery_material", "Delivery actual reference names an unknown repository")
    need(isinstance(value["input_ref"], dict),
         "invalid_delivery_material", "Delivery material input reference is missing")
    input_ref = _identity(validate_typed_ref(
        value["input_ref"], project=value["project"],
        expected_kinds={"delivery_snapshot", "actual_delivery_commit"},
    ))
    if input_ref["kind"] == "delivery_snapshot":
        need(_identity(input_ref) == _identity(value["snapshot_ref"]),
             "invalid_delivery_material", "Delivery input snapshot is not the anchor")
    else:
        need(_semantic(input_ref["delivery"]) == _semantic(value["snapshot_ref"]),
             "invalid_delivery_material", "Delivery input actual belongs to another snapshot")
    need(isinstance(value["centers"], dict) and set(value["centers"]) == _CENTER_KEYS,
         "invalid_delivery_material", "Delivery center inventory shape differs")
    need(value["centers"]["delivery_snapshots"] == [value["snapshot_ref"]],
         "invalid_delivery_material", "Delivery snapshot center is not the anchor")
    need(value["centers"]["actual_commits"] == actuals,
         "invalid_delivery_material", "Delivery actual center differs from actual population")
    need(value["status"] in {"available", "unresolved", "invalid"},
         "invalid_delivery_material", "Delivery material status is invalid")
    need(isinstance(value["diagnostics"], list),
         "invalid_delivery_material", "Delivery material diagnostics are not a list")
    return value


def _currentness(resolution: dict[str, Any]) -> dict[str, Any]:
    """Keep resolver currentness explicit; a state mapping is not a bool."""
    current = resolution.get("current")
    value = {"state": "not_evaluated"}
    if isinstance(current, bool):
        value["resolution_current"] = current
    elif isinstance(current, dict):
        value["resolution_state"] = current.get("state", "not_evaluated")
    return value


def _safe_profile_repositories(control: Any, project: str, binding: dict[str, Any], diagnostics: list[dict[str, Any]]) -> set[str] | None:
    profile_digest = binding.get("profile") if isinstance(binding, dict) else None
    if not isinstance(profile_digest, str):
        diagnostics.append(_diagnostic("delivery_profile_missing", "Saved Delivery binding has no profile digest"))
        return None
    row = control.s.one("SELECT body,digest FROM profiles WHERE project=?", (project,))
    if row is None:
        diagnostics.append(_diagnostic("delivery_profile_missing", "Saved Delivery profile is missing"))
        return None
    if row.get("digest") != profile_digest:
        diagnostics.append(_diagnostic("delivery_profile_mismatch", "Saved Delivery profile digest differs"))
        return None
    try:
        body = parse_json(row["body"])
        values = body.get("repo_order") if isinstance(body, dict) else None
        if not isinstance(values, list) or any(type(item) is not str or not item for item in values):
            raise ValueError("profile.repo_order is malformed")
        return set(values)
    except (Fault, ValueError, TypeError) as exc:
        diagnostics.append(_diagnostic("delivery_profile_invalid", str(exc)))
        return None


def _iter_actual_candidates(control: Any, actor: Any, project: str, anchor_ref: dict[str, Any],
                            anchor_payload: dict[str, Any], expected: set[str],
                            diagnostics: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Enumerate every project material row and retain only verified actuals."""
    assurance = getattr(control, "assurance", None)
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name in expected}
    if assurance is None:
        diagnostics.append(_diagnostic("delivery_material_unsupported", "E1 material resolver is unavailable"))
        return groups
    # Keep the enumeration read-only and bounded so a large project cannot
    # force one unbounded result set into the reader.  Paging by immutable
    # object id also makes a restart deterministic within the transaction.
    page_size = 100
    offset = 0
    while True:
        rows = control.s.all(
            "SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id LIMIT ? OFFSET ?",
            (project, page_size, offset),
        )
        if not rows:
            break
        for raw_row in rows:
            row = dict(raw_row)
            row["_store"] = control.s
            try:
                body, payload = _material_body(row, project=project)
            except Fault as exc:
                # A malformed actual row is retained as a diagnostic.  Unrelated
                # malformed material is still visible to the reader but must not
                # turn a valid repository's population into an arbitrary row.
                try:
                    raw_body = parse_json(row.get("body")) if isinstance(row.get("body"), str) else row.get("body")
                    if not isinstance(raw_body, dict) or raw_body.get("material_kind") != "actual_delivery_commit":
                        continue
                except Fault:
                    continue
                diagnostics.append(_diagnostic("delivery_actual_material_invalid", str(exc), material=row.get("id")))
                continue
            if body.get("material_kind") != "actual_delivery_commit":
                continue
            try:
                dependencies = body.get("dependency_refs")
                nested = payload.get("delivery_snapshot_ref")
                need(isinstance(dependencies, list) and len(dependencies) == 1,
                     "integrity_error", "Actual Delivery material dependency cardinality differs", row.get("id"))
                validate_typed_ref(nested, project=project, expected_kinds={"delivery_snapshot"})
                need(_identity(dependencies[0]) == _identity(nested),
                     "integrity_error", "Actual Delivery material dependency differs from payload", row.get("id"))
                candidate = _material_ref(row, body, payload, project=project)
                repository = candidate["repository"]
                # The project-wide material table is historical and contains
                # captures for every Delivery in this project.  Establish
                # canonical Delivery membership from the nested snapshot
                # before checking the target repository set.  A valid capture
                # for another Delivery is unrelated history and must not
                # become a mismatch/extra diagnostic for this anchor.  Once
                # the Delivery ID matches, binding/snapshot identity remains
                # strict so target corruption cannot be hidden as history.
                candidate_delivery = candidate.get("delivery")
                candidate_delivery_id = (
                    candidate_delivery.get("delivery")
                    if isinstance(candidate_delivery, dict) else None
                )
                if candidate_delivery_id != anchor_ref.get("delivery"):
                    continue
                if repository not in expected:
                    diagnostics.append(_diagnostic("delivery_repository_extra_material",
                                                   "Actual material names a repository outside the sealed snapshot",
                                                   repository=repository, material=row.get("id")))
                    continue
                if _semantic(candidate["delivery"]) != _semantic(anchor_ref):
                    diagnostics.append(_diagnostic("delivery_actual_snapshot_mismatch",
                                                   "Actual material belongs to another Delivery snapshot",
                                                   repository=repository, material=row.get("id")))
                    continue
                resolution = assurance.resolve_pinned(actor, candidate)
                resolved_payload = resolution.get("resolution", {}).get("payload")
                need(isinstance(resolved_payload, dict), "missing_evidence", "Actual Delivery payload is missing", repository)
                nested_resolution = assurance.resolve_pinned(actor, candidate["delivery"])
                nested_payload = nested_resolution.get("resolution", {}).get("payload")
                need(isinstance(nested_payload, dict), "missing_evidence", "Actual Delivery snapshot payload is missing", repository)
                need(_identity(nested_payload) == _identity(anchor_payload),
                     "integrity_error", "Actual Delivery snapshot meaning differs", repository)
                # Keep the candidate's own payload for semantic-equivalence checks;
                # this private field is removed before it enters the wire result.
                candidate_record = {
                    "ref": candidate, "payload": _copy(resolved_payload),
                    "resolution": resolution, "nested_resolution": nested_resolution,
                }
                groups[repository].append(candidate_record)
            except Fault as exc:
                repository = payload.get("repository") if isinstance(payload, dict) else None
                diagnostics.append(_diagnostic("delivery_actual_material_invalid", str(exc),
                                               repository=repository, material=row.get("id")))
        offset += page_size
    return groups


def read_delivery_material(control: Any, actor: Any, *, project: str, delivery: dict[str, Any]) -> dict[str, Any]:
    """Resolve one Delivery selector into the complete readonly material wire.

    ``delivery`` may be the saved snapshot selector or an actual commit
    selector.  In the latter case the actual's own typed snapshot dependency
    is resolved and used as the anchor.  This function intentionally keeps
    historical material readable while returning currentness as a diagnostic
    state; it does not promote a retained pin to current authority.
    """
    validate_typed_ref(delivery, project=project,
                       expected_kinds={"delivery_snapshot", "actual_delivery_commit"})
    input_ref = _identity(delivery)
    assurance = getattr(control, "assurance", None)
    need(assurance is not None and callable(getattr(assurance, "resolve_pinned", None)),
         "unsupported", "E1 material resolver is unavailable")
    diagnostics: list[dict[str, Any]] = []
    try:
        input_resolution = assurance.resolve_pinned(actor, input_ref)
    except Fault as exc:
        # A caller's typed selector is valid, but its retained material may
        # be missing or corrupt.  Preserve a structured result for the stage
        # denominator instead of manufacturing a snapshot.
        raise Fault(exc.code, str(exc), {"delivery_material": input_ref}) from exc
    input_payload = input_resolution.get("resolution", {}).get("payload")
    need(isinstance(input_payload, dict), "missing_evidence", "Pinned Delivery payload is missing")
    if input_ref["kind"] == "delivery_snapshot":
        anchor_ref = input_ref
        anchor_resolution = input_resolution
    else:
        nested = input_payload.get("delivery_snapshot_ref")
        validate_typed_ref(nested, project=project, expected_kinds={"delivery_snapshot"})
        need(_identity(nested) == _identity(input_ref["delivery"]),
             "integrity_error", "Actual Delivery payload snapshot dependency differs")
        anchor_ref = _identity(nested)
        anchor_resolution = assurance.resolve_pinned(actor, anchor_ref)
    anchor_payload = anchor_resolution.get("resolution", {}).get("payload")
    need(isinstance(anchor_payload, dict), "missing_evidence", "Pinned Delivery snapshot payload is missing")
    snapshot = anchor_payload.get("snapshot")
    need(isinstance(snapshot, dict), "integrity_error", "Pinned Delivery snapshot manifest is missing")
    validate_sealed_snapshot(control.s, snapshot)
    delivery_id = anchor_ref.get("delivery")
    need(isinstance(delivery_id, str) and delivery_id,
         "invalid_reference", "Pinned Delivery ID is not a string")

    row = control.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery_id, project))
    need(row is not None, "unresolved_reference", "Saved Delivery row is missing", delivery_id)
    try:
        delivery_body = parse_json(row["body"])
    except Fault as exc:
        raise Fault("integrity_error", "Saved Delivery body is malformed", delivery_id) from exc
    need(isinstance(delivery_body, dict), "integrity_error", "Saved Delivery body is not an object", delivery_id)
    binding = delivery_body.get("binding")
    need(isinstance(binding, dict), "integrity_error", "Saved Delivery binding is missing", delivery_id)
    binding_digest = digest(binding)
    need(row.get("digest") == binding_digest == anchor_ref.get("binding_digest"),
         "stale_reference", "Saved Delivery binding identity differs", delivery_id)
    saved_snapshot = delivery_body.get("snapshot")
    need(isinstance(saved_snapshot, dict), "integrity_error", "Saved Delivery snapshot is missing", delivery_id)
    need(_identity(saved_snapshot) == _identity(snapshot),
         "stale_reference", "Saved Delivery snapshot meaning differs", delivery_id)

    repos = snapshot.get("repos")
    need(isinstance(repos, dict), "integrity_error", "Pinned Delivery snapshot repository map is missing")
    expected_repositories = set(repos)
    profile_repositories = _safe_profile_repositories(control, project, binding, diagnostics)
    if profile_repositories is not None and profile_repositories != expected_repositories:
        diagnostics.append(_diagnostic("delivery_repository_set_mismatch",
                                       "Saved profile repository set differs from sealed snapshot",
                                       expected=sorted(expected_repositories), profile=sorted(profile_repositories)))
    observed = delivery_body.get("git")
    if not isinstance(observed, dict):
        diagnostics.append(_diagnostic("delivery_observed_git_invalid", "Saved Delivery git map is not an object"))
        observed = {}
    for repository in sorted(set(observed) - expected_repositories):
        diagnostics.append(_diagnostic("delivery_repository_extra_observation",
                                       "Saved Delivery git map contains an unexpected repository",
                                       repository=repository))

    checks = anchor_payload.get("checks")
    check_refs: list[dict[str, Any]] = []
    if not isinstance(checks, list):
        diagnostics.append(_diagnostic("delivery_checks_invalid", "Pinned Delivery snapshot checks are not a list"))
        checks = []
    else:
        seen_checks: set[str] = set()
        for index, check in enumerate(checks):
            try:
                check_ref = _check_ref(project, anchor_ref, check, index)
                if check_ref["check_id"] in seen_checks:
                    raise Fault("integrity_error", "Delivery check identity is duplicated", check_ref["check_id"])
                seen_checks.add(check_ref["check_id"])
                check_refs.append(check_ref)
            except Fault as exc:
                diagnostics.append(_diagnostic("delivery_check_invalid", str(exc), index=index))

    # The declaration extractor is shared with the denominator.  Importing it
    # lazily avoids a module cycle while keeping one producer/output identity
    # implementation for both the old and new context wires.
    declared_outputs: dict[str, Any]
    try:
        from .assurance_denominators import _delivery_declared_outputs
        declared_outputs = _delivery_declared_outputs(
            project, anchor_payload, anchor_ref, check_refs, diagnostics,
        )
    except Fault as exc:
        diagnostics.append(_diagnostic("delivery_declared_outputs_invalid", str(exc)))
        declared_outputs = {"status": "unverified", "items": []}

    groups = _iter_actual_candidates(
        control, actor, project, anchor_ref, anchor_payload,
        expected_repositories, diagnostics,
    )

    repositories: list[dict[str, Any]] = []
    actual_commit_refs: list[dict[str, Any]] = []
    for repository in sorted(expected_repositories):
        saved = observed.get(repository)
        saved_identity = _repository_identity(saved, repository, anchor_ref["snapshot_digest"])
        entry_diagnostics: list[dict[str, Any]] = []
        if saved is None:
            entry_diagnostics.append(_diagnostic(
                "delivery_repository_observation_missing",
                "Delivery has not saved an observed Git identity for this repository",
                repository=repository,
            ))
            state = "missing_observation"
        elif saved_identity is None:
            entry_diagnostics.append(_diagnostic(
                "delivery_repository_observation_invalid",
                "Saved Delivery Git identity is malformed or bound to another snapshot",
                repository=repository,
            ))
            state = "invalid"
        else:
            state = "missing_material"
        candidates = groups.get(repository, [])
        matching = [item for item in candidates
                    if saved_identity is not None and
                    _repository_identity(item["payload"].get("observed_result"), repository,
                                         anchor_ref["snapshot_digest"]) == saved_identity]
        if len(matching) > 1:
            meanings = [_semantic(item["payload"]) for item in matching]
            if len({canonical(item) for item in meanings}) != 1:
                entry_diagnostics.append(_diagnostic(
                    "delivery_actual_material_ambiguous",
                    "Equivalent repository identity has non-equivalent actual captures",
                    repository=repository,
                ))
                state = "ambiguous"
                matching = []
        # Retain every validated candidate for this sealed repository.  Only a
        # candidate matching the saved body.git identity may become actual_ref;
        # a missing/invalid saved observation therefore cannot be bypassed by
        # promoting a material merely because it exists in the object store.
        candidate_refs = sorted((_identity(item["ref"]) for item in candidates), key=canonical)
        chosen_refs = sorted((_identity(item["ref"]) for item in matching), key=canonical)
        chosen = chosen_refs[0] if chosen_refs else None
        if chosen is not None and state not in {"invalid", "ambiguous"}:
            state = "verified"
            actual_commit_refs.append(chosen)
        elif saved_identity is not None and not matching and state == "missing_material":
            entry_diagnostics.append(_diagnostic(
                "delivery_actual_material_missing",
                "Saved observed Git identity has no valid pinned actual material",
                repository=repository,
            ))
        # A retained material can be historical.  Keep that fact visible but
        # never turn the resolver's state mapping into ``current=True``.
        currentness = {"state": "not_evaluated"}
        if chosen is not None:
            chosen_record = next(
                item for item in matching if _identity(item["ref"]) == chosen
            )
            currentness = _currentness(chosen_record["resolution"])
        repositories.append({
            "format": DELIVERY_REPOSITORY_FORMAT,
            "repository": repository,
            "saved_observed_identity": _copy(saved) if isinstance(saved, dict) else None,
            "actual_ref": chosen,
            "candidate_refs": candidate_refs,
            "integrity_state": state,
            "currentness_state": currentness,
            "diagnostics": entry_diagnostics,
        })
        diagnostics.extend(entry_diagnostics)

    actual_commit_refs.sort(key=lambda ref: ref["repository"])
    centers = {"delivery_snapshots": [anchor_ref], "actual_commits": actual_commit_refs}
    status = "available" if all(item["integrity_state"] == "verified" for item in repositories) and not diagnostics else "unresolved"
    if any(item.get("integrity_state") in {"invalid", "ambiguous"} for item in repositories):
        status = "invalid"
    material = {
        "format": DELIVERY_MATERIAL_FORMAT,
        "project": project,
        "delivery_id": delivery_id,
        "snapshot_ref": _copy(anchor_ref),
        "snapshot_payload": _copy(anchor_payload),
        "check_refs": check_refs,
        "declared_outputs": declared_outputs,
        "repositories": repositories,
        "actual_commit_refs": actual_commit_refs,
        "input_ref": _copy(input_ref),
        "centers": centers,
        "status": status,
        "diagnostics": diagnostics,
    }
    return validate_delivery_material(material, project=project)


__all__ = [
    "DELIVERY_MATERIAL_EXTRACTOR", "DELIVERY_MATERIAL_FORMAT",
    "DELIVERY_REPOSITORY_EXTRACTOR", "DELIVERY_REPOSITORY_FORMAT",
    "read_delivery_material", "validate_delivery_material",
]
