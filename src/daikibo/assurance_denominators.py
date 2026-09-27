"""Controller-derived E3 Unit 2a denominator material.

This module is deliberately read-only.  It is the boundary between the
controller's canonical rows and the later E3 evaluator: it derives a complete
obligation denominator, then makes a Task-local projection without allowing a
caller to submit an arbitrary subset.  Unit 2b node receipts and criterion
checking, and all gate routes, remain separate components.

The public shapes in this module are JSON mappings with a private in-process
seal on controller-collected Context and Denominator values.  References
inside them use the existing :func:`validate_typed_ref` contract; the one
breakdown descriptor is an internal controller material descriptor because the
existing public typed-ref catalog has no breakdown kind.  It is never exposed
as a new wire reference kind.
"""
from __future__ import annotations

import copy
from functools import wraps
from typing import Any, Iterable

from .assurance_relations import REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST, validate_typed_ref
from .assurance_profile_contract import (
    PROFILE_V2_FORMAT, PROFILE_V3_FORMAT, PROFILE_V4_FORMAT,
    CANONICAL_PROFILE_FORMATS, profile_registry, profile_has_outputs,
)
from .build_outputs import validate_definition
from .assurance_additive import (
    artifact_structural_metadata,
    task_structural_metadata,
)
from .assurance_impact import (
    IMPACT_CATEGORY,
    IMPACT_CONSUMER,
    IMPACT_DERIVATION_VERSION,
    collect_impact_inventory,
    impact_input_refs,
    impact_obligations,
    validate_impact_inventory,
)
from .common import Fault, canonical, digest, need, parse_json
from .delivery_material_reader import (
    DELIVERY_MATERIAL_EXTRACTOR,
    DELIVERY_MATERIAL_FORMAT,
    DELIVERY_REPOSITORY_EXTRACTOR,
    validate_delivery_material,
    read_delivery_material,
)
from .verification_materials import validate_test_plan_definition_identity

CONTEXT_FORMAT = "assurance.stage-context.v2"
CONTEXT_V3_FORMAT = "assurance.stage-context.v3"
CONTEXT_V4_FORMAT = "assurance.stage-context.v4"
DENOMINATOR_FORMAT = "assurance.denominator.v2"
DENOMINATOR_V3_FORMAT = "assurance.denominator.v3"
DENOMINATOR_V4_FORMAT = "assurance.denominator.v4"
PROJECTION_FORMAT = "assurance.task-projection.v1"
CHECKPOINT_PROJECTION_FORMAT = "assurance.task-checkpoint-projection.v1"
GLOBAL_CHECKPOINT_PROJECTION_FORMAT = "assurance.stage-checkpoint-projection.v1"
PAGE_FORMAT = "assurance.denominator-page.v1"
DERIVATION_VERSION = "controller-denominator.v3"
DERIVATION_V4 = "controller-denominator.v4"
DERIVATION_V5 = "controller-denominator.v5"
DELIVERY_DECLARED_OUTPUT_CATEGORY = "delivery_declared_output"
SOURCE_PARTITION_FORMAT = "assurance.source-partition.v1"
REQUIREMENT_SCOPE_FORMAT = "assurance.requirement-scope.v1"
MAX_OBLIGATIONS = 100_000
MAX_PAGE = 100
STAGES = ("plan", "task", "integration", "delivery")
_STAGE_ORDER = {name: index for index, name in enumerate(STAGES)}

# The context has an exact top-level shape.  Nested records are validated by
# the existing typed-reference validator wherever they are typed refs.
_CONTEXT_KEYS = {
    "format", "project", "program", "stage", "selection_ref", "root_plan_ref",
    "source_inputs", "artifacts", "task_definitions", "assignments", "unit_b",
    "delivery_material", "source_partitions", "requirement_scope", "breakdown_scope",
    "impact_inventory", "input_refs", "capabilities", "unresolved",
}
_DENOMINATOR_KEYS = {
    "format", "project", "program", "stage", "derivation_version", "input_refs",
    "input_digest", "obligations", "count", "digest", "unresolved", "capabilities",
    "extractor_versions",
}
_PROJECTION_KEYS = {
    "format", "global_digest", "task_ref", "obligation_ids",
    "contributor_requirements", "unresolved", "digest",
}
_CHECKPOINT_PROJECTION_KEYS = {
    "format", "global_digest", "task_ref", "obligation_ids",
    "contributor_requirements", "unresolved", "checkpoint", "relation",
    "direction", "center_ref", "population_ids", "required_now_ids",
    "deferred_future_ids", "schedule", "digest",
}
_GLOBAL_CHECKPOINT_PROJECTION_KEYS = {
    "format", "global_digest", "obligation_ids", "contributor_requirements",
    "unresolved", "checkpoint", "relation", "direction", "center_ref",
    "population_ids", "required_now_ids", "deferred_future_ids", "schedule",
    "digest",
}


class _SealedMapping(dict):
    """A JSON mapping with a private controller-origin seal.

    Contexts and denominators are passed between read-only components in the
    same process.  Their public fields remain ordinary JSON so callers can
    inspect them, while the private seal prevents a copied/mutated mapping
    from being mistaken for a fresh controller enumeration.  The seal is a
    digest of the public mapping, not an authority string supplied by a
    caller.  ``deepcopy`` deliberately preserves the origin token and the
    original seal so mutations in the copy are detected by validation.
    """

    __slots__ = ("_seal", "_token")

    def __init__(self, value: dict[str, Any], *, token: object) -> None:
        super().__init__(value)
        object.__setattr__(self, "_seal", canonical(dict(value)))
        object.__setattr__(self, "_token", token)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("sealed mapping metadata is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__slots__:
            raise AttributeError("sealed mapping metadata is immutable")
        object.__delattr__(self, name)

    def __deepcopy__(self, memo: dict[int, Any]) -> "_SealedMapping":
        copied = type(self).__new__(type(self))
        memo[id(self)] = copied
        dict.__init__(copied, copy.deepcopy(dict(self), memo))
        object.__setattr__(copied, "_seal", self._seal)
        object.__setattr__(copied, "_token", self._token)
        return copied


_CONTEXT_TOKEN = object()
_DENOMINATOR_TOKEN = object()
_CHECKPOINT_PROJECTION_TOKEN = object()
_GLOBAL_CHECKPOINT_PROJECTION_TOKEN = object()
_CHECKPOINT_PLAN_TOKEN = object()


class _CheckpointPlan:
    """Opaque result of the Unit 2/B controller classifier.

    A projection factory must never receive population or partition lists from
    a relation caller.  The stage reader creates this object only after it has
    classified the selected request against the sealed context and complete
    denominator.  Keeping the plan as an object, rather than accepting a
    JSON-shaped ``schedule`` argument, also makes a copied mapping fail closed
    before it can acquire a projection seal.
    """

    __slots__ = ("_body", "_seal", "_token")

    def __init__(self, body: dict[str, Any]) -> None:
        object.__setattr__(self, "_body", _copy_json(body, "checkpoint classifier plan"))
        object.__setattr__(self, "_seal", canonical(self._body))
        object.__setattr__(self, "_token", _CHECKPOINT_PLAN_TOKEN)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("checkpoint classifier plan is immutable")
        object.__setattr__(self, name, value)

    def __deepcopy__(self, memo: dict[int, Any]) -> "_CheckpointPlan":
        copied = type(self).__new__(type(self))
        memo[id(self)] = copied
        object.__setattr__(copied, "_body", copy.deepcopy(self._body, memo))
        object.__setattr__(copied, "_seal", self._seal)
        object.__setattr__(copied, "_token", self._token)
        return copied


def _make_checkpoint_plan(*, stage: str, checkpoint: str, relation: str,
                          direction: str, center_ref: dict[str, Any],
                          schedule: dict[str, Any]) -> _CheckpointPlan:
    """Create the opaque handoff from the canonical stage classifier.

    This is intentionally an in-process handoff.  The public projection
    functions below reject caller-supplied population/partition fields and
    only consume this exact plan object.
    """
    if type(stage) is not str or not stage:
        _invalid("checkpoint classifier stage is invalid")
    if type(checkpoint) is not str or not checkpoint:
        _invalid("checkpoint classifier checkpoint is invalid")
    if type(relation) is not str or not relation:
        _invalid("checkpoint classifier relation is invalid")
    if direction not in {"incoming", "outgoing"}:
        _invalid("checkpoint classifier direction is invalid")
    if type(center_ref) is not dict:
        _invalid("checkpoint classifier center is invalid")
    if type(schedule) is not dict:
        _invalid("checkpoint classifier schedule is invalid")
    required = {"population_ids", "required_now_ids", "deferred_future_ids", "schedule"}
    if not required <= set(schedule):
        _invalid("checkpoint classifier schedule shape differs")
    return _CheckpointPlan({
        "stage": stage, "checkpoint": checkpoint, "relation": relation,
        "direction": direction, "center_ref": center_ref,
        "population_ids": schedule["population_ids"],
        "required_now_ids": schedule["required_now_ids"],
        "deferred_future_ids": schedule["deferred_future_ids"],
        "schedule": schedule["schedule"],
    })


def _sealed(value: dict[str, Any], token: object) -> _SealedMapping:
    return _SealedMapping(_copy_json(value), token=token)


def _verify_seal(value: Any, *, token: object, name: str) -> None:
    if not isinstance(value, _SealedMapping) or value._token is not token:
        _invalid(f"{name} must come from the controller collector")
    if value._seal != canonical(dict(value)):
        raise Fault("denominator_input_mismatch", f"{name} was modified after controller enumeration")


def _invalid(message: str, details: Any = None) -> None:
    raise Fault("invalid_input", message, details)


def _integrity(message: str, details: Any = None) -> None:
    raise Fault("integrity_error", message, details)


def _unsupported(message: str, details: Any = None) -> None:
    raise Fault("unsupported", message, details)


def semantic_definition_projection(value: Any) -> Any:
    """Project mutable observation pins away from definition meaning.

    A Runtime.tests capture is an immutable authority/proof observation, so
    its material pin remains in the controller context and denominator body.
    The pin is not part of the frozen Task/plan/check definition, however.  A
    second equivalent capture must therefore retain its authority while
    leaving semantic fingerprints and obligation identities unchanged.
    """
    if isinstance(value, dict):
        kind = value.get("kind")
        return {
            key: semantic_definition_projection(item)
            for key, item in value.items()
            if not (kind == "test_plan" and key == "pin")
        }
    if isinstance(value, list):
        return [semantic_definition_projection(item) for item in value]
    return value


def _denominator_semantic_digest(value: dict[str, Any]) -> str:
    projected = semantic_definition_projection(value)
    if isinstance(projected, dict):
        projected.pop("digest", None)
    return digest(projected)


def _object(value: Any, required: Iterable[str], optional: Iterable[str] = (), *, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        _invalid(f"{name} must be an object")
    required_set, optional_set = set(required), set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - required_set - optional_set)
    if missing:
        _invalid(f"{name} is missing fields", missing)
    if unknown:
        _invalid(f"{name} has unknown fields", unknown)
    return value


def _string(value: Any, name: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value) or "\x00" in value:
        _invalid(f"{name} must be a {'possibly empty ' if empty else 'nonempty '}string")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        _invalid(f"{name} is below its minimum", value)
    if maximum is not None and value > maximum:
        _invalid(f"{name} is above its maximum", value)
    return value


def _copy_json(value: Any, name: str = "value") -> Any:
    try:
        return parse_json(canonical(value))
    except Fault:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        _invalid(f"{name} is not canonical JSON")
        raise AssertionError from exc


def _body(row: dict[str, Any], *, field: str = "body", name: str = "body") -> dict[str, Any]:
    raw = row.get(field)
    if isinstance(raw, str):
        try:
            value = parse_json(raw)
        except Fault as exc:
            _integrity(f"{name} is not valid JSON", row.get("id"))
            raise AssertionError from exc
    else:
        value = raw
    if type(value) is not dict:
        _integrity(f"{name} is not an object", row.get("id"))
    return _copy_json(value, name)


def _typed(ref: Any, project: str, *, expected: set[str] | None = None, name: str = "reference") -> dict[str, Any]:
    try:
        validate_typed_ref(ref, project=project, expected_kinds=expected)
    except Fault:
        raise
    return _copy_json(ref, name)


def _unique_sorted(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[bytes, dict[str, Any]] = {}
    for value in values:
        marker = canonical(value)
        result[marker] = value
    return [result[key] for key in sorted(result)]


def _unresolved(code: str, *, reason: str, **details: Any) -> dict[str, Any]:
    value = {"code": code, "reason": reason}
    for key, item in details.items():
        if item is not None:
            value[key] = _copy_json(item, key)
    return value


def _read_transaction(function):
    """Run one controller snapshot under the store's serialized read view."""
    @wraps(function)
    def wrapped(control: Any, actor: Any, *args: Any, **kwargs: Any) -> Any:
        with control.s.transaction():
            return function(control, actor, *args, **kwargs)
    return wrapped


def _breakdown_ref(row: dict[str, Any]) -> dict[str, Any]:
    """Return an internal breakdown material descriptor.

    ``breakdown`` is intentionally not added to the public typed-ref catalog;
    this descriptor is kept in controller context only and is never accepted
    by ``validate_typed_ref`` as a new public kind.
    """
    body = _body(row, name="breakdown body")
    if digest(body) != row.get("digest"):
        _integrity("Breakdown body digest differs", row.get("id"))
    return {
        "format": "assurance.controller-breakdown.v1",
        "project": row["project"], "program": row["program"], "id": row["id"],
        "digest": row["digest"], "status": row["status"],
    }


def _artifact_ref(project: str, row: dict[str, Any]) -> dict[str, Any]:
    ref = {"kind": "artifact", "project": project, "artifact": row["id"],
           "revision": row["revision"], "body_digest": row["digest"]}
    return _typed(ref, project, expected={"artifact"}, name="artifact reference")


def _task_ref(project: str, row: dict[str, Any]) -> dict[str, Any]:
    body = _body(row, name="task definition")
    ref = {"kind": "task_revision", "project": project, "task": row["id"],
           "revision": row["revision"], "definition_digest": digest(body)}
    return _typed(ref, project, expected={"task_revision"}, name="task revision reference")


def _source_ref(project: str, row: dict[str, Any]) -> dict[str, Any]:
    ref = {"kind": "source", "project": project, "source": row["id"],
           "blob_digest": row["blob"]}
    return _typed(ref, project, expected={"source"}, name="source reference")


def _source_span_ref(project: str, source_id: str, blob_digest: str,
                     byte_start: int, byte_end: int, unicode_start: int | None,
                     unicode_end: int | None, span_hash: str) -> dict[str, Any]:
    """Build the existing typed source-span wrapper from verified bytes.

    Unit2c adds no public reference kind.  The source span is still the
    traceability resolver's exact locator, including both byte and Unicode
    coordinates, so a later consumer can re-resolve the same CAS interval.
    """
    locator = {
        "ref_type": "source_span", "source_id": source_id,
        "blob_digest": blob_digest, "byte_start": byte_start,
        "byte_end": byte_end, "unicode_start": unicode_start,
        "unicode_end": unicode_end, "span_hash": span_hash,
    }
    ref = {"kind": "traceability_ref", "project": project, "locator": locator}
    return _typed(ref, project, expected={"traceability_ref"}, name="source span reference")


def _unicode_offsets(raw: bytes, offsets: Iterable[int]) -> dict[int, int | None]:
    """Map UTF-8 byte boundaries to Knowledge's character coordinates.

    ``Knowledge.source`` counts a leading U+FEFF as a character.  Decoding
    ordinary UTF-8 (rather than ``utf-8-sig``) therefore matches the existing
    source/read and traceability source-span contract.  Invalid input returns
    explicit ``None`` coordinates; it is never repaired by guessing.
    """
    wanted = sorted(set(offsets))
    if any(type(value) is not int or value < 0 or value > len(raw) for value in wanted):
        _integrity("Source span boundary is outside the CAS bytes")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return {value: None for value in wanted}
    result: dict[int, int] = {}
    cursor = 0
    index = 0
    for value, byte in enumerate(raw):
        while index < len(wanted) and wanted[index] == value:
            result[value] = cursor
            index += 1
        if byte & 0xC0 != 0x80:
            cursor += 1
    while index < len(wanted) and wanted[index] == len(raw):
        result[len(raw)] = cursor
        index += 1
    return result


def _ac_ref(project: str, artifact: dict[str, Any], index: int, value: str) -> dict[str, Any]:
    pointer = f"/acceptance/{index}"
    ref = {"kind": "traceability_ref", "project": project, "locator": {
        "ref_type": "artifact_ac", "artifact": artifact["id"],
        "revision": artifact["revision"], "body_digest": artifact["digest"],
        "ac_pointer": pointer, "ac_digest": digest(value), "ac_id": value,
    }}
    return _typed(ref, project, expected={"traceability_ref"}, name="acceptance reference")


def _population_ref(project: str, revision: dict[str, Any]) -> dict[str, Any]:
    ref = {"kind": "population", "project": project, "revision": revision["id"],
           "revision_digest": revision["digest"], "population_digest": revision["population_digest"]}
    return _typed(ref, project, expected={"population"}, name="population reference")


def _population_item_ref(project: str, population_ref: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    ref = {"kind": "population_item", "project": project, "population": population_ref,
           "item": row["id"], "item_digest": row["digest"]}
    return _typed(ref, project, expected={"population_item"}, name="population item reference")


def _test_plan_ref(control: Any, actor: Any, project: str, task: dict[str, Any], plan: dict[str, Any],
                   unresolved: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find an existing controller material pin without creating one."""
    task_id, revision = task["id"], task["revision"]
    plan_body = _body(plan, name="test plan")
    if digest(plan_body) != plan.get("digest"):
        _integrity("Frozen test plan digest differs", task_id)
    matches: dict[str, list[dict[str, Any]]] = {}
    conflicting = False
    assurance = getattr(control, "assurance", None)
    store = getattr(control, "s", None)
    if assurance is None or store is None:
        unresolved.append(_unresolved("test_plan_material_unsupported", reason="E1 material resolver is unavailable", task=task_id))
        return None
    for row in store.all("SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id", (project,)):
        try:
            envelope = _body(row, name="material envelope")
            if digest(envelope) != row.get("digest"):
                _integrity("Verification material envelope digest differs", row.get("id"))
            if envelope.get("material_kind") != "test_plan":
                continue
            payload = parse_json(store.blob_get(envelope["payload_blob"]))
            if not isinstance(payload, dict):
                _integrity("Test plan material payload is not an object", row["id"])
            if (payload.get("task") == task_id and payload.get("task_revision") == revision
                    and payload.get("plan_digest") == plan["digest"]
                    and isinstance(payload.get("plan_body"), dict)):
                if payload["plan_body"] != plan_body:
                    unresolved.append(_unresolved("test_plan_material_stale", reason="Pinned test plan body differs from the controller plan", task=task_id, reference=row["id"]))
                    conflicting = True
                    continue
                ref = {"kind": "test_plan", "project": project, "task": task_id,
                       "task_revision": revision, "plan_digest": plan["digest"],
                       "pin": {"id": row["id"], "digest": row["digest"]}}
                try:
                    identity = validate_test_plan_definition_identity(
                        payload, envelope.get("dependency_refs"), project=project,
                        task_ref=_task_ref(project, task), plan_body=plan_body,
                        plan_digest=plan["digest"],
                    )
                    _typed(ref, project, expected={"test_plan"}, name="test plan reference")
                    assurance.resolve_pinned(actor, ref)
                except Fault as exc:
                    unresolved.append(_unresolved("test_plan_material_invalid", reason=exc.code,
                                                  task=task_id, reference=ref))
                    conflicting = True
                    continue
                matches.setdefault(digest(identity), []).append(ref)
        except Fault:
            raise
    if conflicting:
        return None
    if len(matches) > 1:
        unresolved.append(_unresolved("test_plan_material_ambiguous", reason="Multiple pins match one plan",
                                      task=task_id, plan_digest=plan["digest"]))
        return None
    if not matches:
        unresolved.append(_unresolved("test_plan_material_missing", reason="Frozen plan has no retained pin",
                                      task=task_id, plan_digest=plan["digest"]))
        return None
    # Capture provenance is deliberately different for each Runtime.tests
    # invocation.  Once every candidate has passed the shared definition and
    # pinned-material validators, select a deterministic representative of
    # the one immutable definition identity; no latest-pin preference is used.
    equivalent = next(iter(matches.values()))
    return sorted(equivalent, key=canonical)[0]


def _check_ref(project: str, plan_ref: dict[str, Any], check: dict[str, Any], index: int) -> dict[str, Any]:
    check_id = check.get("id")
    if type(check_id) is not str or not check_id:
        _integrity("Test plan check has no stable id")
    ref = {"kind": "test_plan_check", "project": project, "plan": plan_ref,
           "check_id": check_id, "check_digest": digest(check)}
    return _typed(ref, project, expected={"test_plan_check"}, name=f"test plan check {index}")


def _find_current_candidate(control: Any, actor: Any, project: str, task: dict[str, Any],
                            unresolved: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidate_id = task.get("candidate")
    if not candidate_id:
        return None
    row = control.s.one("SELECT * FROM candidates WHERE id=? AND task=?", (candidate_id, task["id"]))
    if row is None:
        unresolved.append(_unresolved("candidate_missing", reason="Task candidate reference has no row",
                                      task=task["id"], candidate=candidate_id))
        return None
    body = _body(row, name="candidate")
    if digest(body) != row.get("digest"):
        _integrity("Candidate body digest differs", candidate_id)
    snapshot = body.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("digest"), str):
        unresolved.append(_unresolved("candidate_snapshot_missing", reason="Candidate has no pinned snapshot",
                                      task=task["id"], candidate=candidate_id))
        return None
    ref = {"kind": "candidate", "project": project, "candidate": candidate_id,
           "task": task["id"], "task_revision": task["revision"],
           "candidate_digest": row["digest"], "snapshot_digest": snapshot["digest"]}
    ref = _typed(ref, project, expected={"candidate"}, name="candidate reference")
    run = control.s.one("SELECT id FROM runs WHERE id=? AND project=?", (row["implementation_run"], project))
    if run is None:
        unresolved.append(_unresolved("implementation_run_missing", reason="Candidate implementation run is absent",
                                      task=task["id"], candidate=candidate_id, run=row["implementation_run"]))
    return {"ref": ref, "body_digest": row["digest"], "implementation_run": row["implementation_run"]}


def _source_inventory(control: Any, actor: Any, project: str,
                      unresolved: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values, refs = [], []
    for row in control.s.all("SELECT * FROM sources WHERE project=? ORDER BY id", (project,)):
        ref = _source_ref(project, row)
        material = {"status": "available", "digest": row.get("blob"), "characters": row.get("characters")}
        source_error: dict[str, Any] | None = None
        try:
            raw = control.s.blob_get(row["blob"])
            if digest(raw) != row["blob"]:
                raise Fault("integrity_error", "Source CAS digest differs", row["id"])
            text = raw.decode("utf-8")
            if len(text) != row["characters"]:
                raise Fault("integrity_error", "Source CAS character count differs", row["id"])
        except Fault as exc:
            if exc.code in {"missing_evidence", "not_found"}:
                material["status"] = "missing"
                source_error = _unresolved("source_material_missing",
                                           reason="Controller source CAS material is missing",
                                           source=row["id"], blob=row.get("blob"))
            else:
                material["status"] = "integrity_error"
                source_error = _unresolved("source_material_integrity",
                                           reason="Controller source CAS material is unreadable or mismatched",
                                           source=row["id"], blob=row.get("blob"))
        except UnicodeDecodeError:
            material["status"] = "integrity_error"
            source_error = _unresolved("source_material_integrity",
                                       reason="Controller source CAS is not valid UTF-8 text",
                                       source=row["id"], blob=row.get("blob"))
        if source_error is not None:
            unresolved.append(source_error)
        dispositions = control.s.all("SELECT id,start,end,category,refs,reason FROM dispositions WHERE source=? ORDER BY start,id", (row["id"],))
        spans = [(item["start"], item["end"]) for item in dispositions]
        cursor, gaps = 0, []
        for start, end in spans:
            if start > cursor:
                gaps.append([cursor, start])
            cursor = max(cursor, end)
        if cursor < row["characters"]:
            gaps.append([cursor, row["characters"]])
        values.append({"ref": ref, "locator": row["locator"], "characters": row["characters"],
                       "material": material,
                       "unclassified": gaps,
                       "dispositions": [{"id": d["id"], "start": d["start"], "end": d["end"],
                                         "category": d["category"], "refs": parse_json(d["refs"]),
                                         "reason": d["reason"]} for d in dispositions]})
        refs.append(ref)
    return values, refs


def _byte_offsets_for_characters(raw: bytes, offsets: Iterable[int]) -> dict[int, int | None]:
    """Map Knowledge character offsets to exact UTF-8 byte boundaries."""
    wanted = sorted(set(offsets))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {value: None for value in wanted}
    if any(type(value) is not int or value < 0 or value > len(text) for value in wanted):
        return {value: None for value in wanted}
    result: dict[int, int] = {}
    wanted_index = 0
    character = 0
    byte_offset = 0
    while wanted_index < len(wanted) and wanted[wanted_index] == 0:
        result[0] = 0
        wanted_index += 1
    for value in text:
        byte_offset += len(value.encode("utf-8"))
        character += 1
        while wanted_index < len(wanted) and wanted[wanted_index] == character:
            result[character] = byte_offset
            wanted_index += 1
    return result


def _traceability_source_partition(control: Any, project: str, source: dict[str, Any],
                                   unresolved: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Read the latest retained document partition for one canonical source.

    Unit A keeps revisions and item bodies immutable.  The extractor reads the
    saved partition as evidence and verifies every identity against the source
    CAS; it never asks the live working tree or a caller for replacement bytes.
    """
    source_id = source["ref"]["source"]
    blob_digest = source["ref"]["blob_digest"]
    candidates: list[dict[str, Any]] = []
    for row in control.s.all(
        "SELECT id,revision,status,digest,body,created FROM traceability_revisions "
        "WHERE project=? ORDER BY revision,id", (project,)
    ):
        try:
            body = parse_json(row["body"])
            if digest(body) != row["digest"]:
                _integrity("Traceability revision digest differs", row["id"])
            if body.get("kind") != "document":
                continue
            if row.get("status") not in {"ready", "active", "superseded"}:
                unresolved.append(_unresolved(
                    "source_partition_integrity",
                    reason="Saved source partition revision is not a completed immutable revision",
                    source=source_id, revision=row["id"], status=row.get("status"),
                ))
                continue
            scope = body.get("scope")
            scope_source = scope.get("source") if isinstance(scope, dict) else None
            if not isinstance(scope_source, dict) or scope_source.get("source_id") != source_id:
                continue
            if scope_source.get("blob") != blob_digest:
                unresolved.append(_unresolved(
                    "source_partition_source_mismatch",
                    reason="Saved source partition names the source with a different CAS blob",
                    source=source_id, revision=row["id"], expected=blob_digest,
                    actual=scope_source.get("blob"),
                ))
                continue
            candidates.append({"row": row, "body": body})
        except Fault as exc:
            unresolved.append(_unresolved(
                "source_partition_integrity", reason=exc.code,
                source=source_id, revision=row["id"],
            ))
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: (item["row"].get("revision", 0), item["row"]["id"]))
    row, revision_body = selected["row"], selected["body"]
    leaves: list[dict[str, Any]] = []
    for item in control.s.all(
        "SELECT id,ordinal,item_kind,path,status,start_byte,end_byte,body,digest,leaf "
        "FROM traceability_items WHERE revision=? ORDER BY ordinal,id", (row["id"],)
    ):
        if not bool(item["leaf"]):
            continue
        try:
            body = parse_json(item["body"])
            if digest(body) != item["digest"]:
                _integrity("Traceability item digest differs", item["id"])
            span = body.get("source_span")
            _object(span, {"ref_type", "source_id", "blob_digest", "byte_start", "byte_end",
                           "unicode_start", "unicode_end", "span_hash"}, name="saved source span")
            if span["ref_type"] != "source_span" or span["source_id"] != source_id:
                _integrity("Saved source span source identity differs", item["id"])
            if span["blob_digest"] != blob_digest:
                _integrity("Saved source span CAS identity differs", item["id"])
            if (span["byte_start"], span["byte_end"]) != (item["start_byte"], item["end_byte"]):
                _integrity("Saved source span columns differ", item["id"])
            if (type(span["byte_start"]) is not int or type(span["byte_end"]) is not int
                    or type(span["unicode_start"]) is not int or type(span["unicode_end"]) is not int):
                _integrity("Saved source span coordinates are malformed", item["id"])
            leaves.append({
                "id": item["id"], "ordinal": item["ordinal"],
                "item_kind": item["item_kind"], "status": item["status"],
                "byte_start": span["byte_start"], "byte_end": span["byte_end"],
                "unicode_start": span["unicode_start"], "unicode_end": span["unicode_end"],
                "span_hash": span["span_hash"], "digest": item["digest"],
            })
        except Fault as exc:
            unresolved.append(_unresolved(
                "source_partition_integrity", reason=exc.code,
                source=source_id, revision=row["id"], item=item["id"],
            ))
    if not leaves:
        unresolved.append(_unresolved(
            "source_partition_missing", reason="Saved document revision has no valid leaf source spans",
            source=source_id, revision=row["id"],
        ))
    return {
        "format": SOURCE_PARTITION_FORMAT, "source_ref": source["ref"],
        "source_id": source_id, "blob_digest": blob_digest,
        "characters": source["characters"], "revision": row["id"],
        "revision_number": row["revision"], "revision_digest": row["digest"],
        "status": row["status"], "leaves": leaves,
    }


def _source_partition_inventory(control: Any, actor: Any, project: str,
                                sources: list[dict[str, Any]],
                                unresolved: list[dict[str, Any]], *,
                                include_unpartitioned: bool = True) -> list[dict[str, Any]]:
    """Derive canonical non-overlapping source intervals and classification gaps."""
    partitions: list[dict[str, Any]] = []
    for source in sources:
        dispositions = source.get("dispositions", [])
        saved = _traceability_source_partition(control, project, source, unresolved)
        if saved is None and not dispositions and not include_unpartitioned:
            # Unit2a sources without a saved partition or classification remain
            # valid historical input.  They simply do not claim a 2c source
            # span denominator yet.
            continue
        raw: bytes | None = None
        if source.get("material", {}).get("status") == "available":
            try:
                raw = control.s.blob_get(source["ref"]["blob_digest"])
                if digest(raw) != source["ref"]["blob_digest"]:
                    _integrity("Source partition CAS digest differs", source["ref"]["source"])
                if len(raw.decode("utf-8")) != source["characters"]:
                    _integrity("Source partition character count differs", source["ref"]["source"])
            except (Fault, UnicodeDecodeError) as exc:
                unresolved.append(_unresolved(
                    "source_partition_material_unresolved", reason=getattr(exc, "code", type(exc).__name__),
                    source=source["ref"]["source"], blob=source["ref"]["blob_digest"],
                ))
                raw = None
        saved_leaves = (saved or {}).get("leaves", [])
        if saved is not None and not saved_leaves:
            # A retained revision with no valid leaf is an incomplete saved
            # partition (including a malformed/empty non-empty source).  Do
            # not replace it with a synthetic full-source span.
            partitions.append({
                "format": SOURCE_PARTITION_FORMAT, "source_ref": source["ref"],
                "source_id": source["ref"]["source"], "blob_digest": source["ref"]["blob_digest"],
                "characters": source["characters"], "revision": saved.get("revision"),
                "revision_number": saved.get("revision_number"), "revision_digest": saved.get("revision_digest"),
                "status": "incomplete", "leaves": [], "saved_leaves": [],
                "unclassified": _copy_json(source.get("unclassified", [])),
            })
            continue
        intervals: list[dict[str, Any]] = []
        coverage_errors = False
        byte_boundaries: set[int] = set()
        if raw is not None:
            byte_boundaries.update((0, len(raw)))
            character_offsets = {0, source["characters"]}
            valid_dispositions: list[dict[str, Any]] = []
            last_end = 0
            disposition_ids: set[str] = set()
            for raw_disposition in dispositions:
                disposition = _copy_json(raw_disposition, "source disposition")
                _object(disposition, {"id", "start", "end", "category", "refs", "reason"}, name="source disposition")
                if type(disposition["id"]) is not str or not disposition["id"] or disposition["id"] in disposition_ids:
                    unresolved.append(_unresolved(
                        "source_classification_invalid", reason="Classification identity is duplicated or malformed",
                        source=source["ref"]["source"], disposition=disposition.get("id"),
                    ))
                    coverage_errors = True
                    continue
                disposition_ids.add(disposition["id"])
                if (type(disposition["start"]) is not int or type(disposition["end"]) is not int
                        or not 0 <= disposition["start"] < disposition["end"] <= source["characters"]):
                    unresolved.append(_unresolved(
                        "source_classification_invalid", reason="Classification range is outside source characters",
                        source=source["ref"]["source"], disposition=disposition.get("id"),
                    ))
                    coverage_errors = True
                    continue
                if disposition["category"] not in {"requirement", "constraint", "question", "reference", "out_of_scope"}:
                    unresolved.append(_unresolved(
                        "source_classification_invalid", reason="Classification category is outside the Knowledge vocabulary",
                        source=source["ref"]["source"], disposition=disposition["id"],
                    ))
                    coverage_errors = True
                    continue
                if disposition["start"] < last_end:
                    unresolved.append(_unresolved(
                        "source_classification_overlap", reason="Classification ranges overlap",
                        source=source["ref"]["source"], disposition=disposition["id"],
                    ))
                    coverage_errors = True
                    continue
                if not isinstance(disposition["refs"], list) or any(type(ref) is not str for ref in disposition["refs"]):
                    unresolved.append(_unresolved(
                        "source_classification_invalid", reason="Classification target refs are malformed",
                        source=source["ref"]["source"], disposition=disposition["id"],
                    ))
                    coverage_errors = True
                    continue
                if disposition["category"] in {"requirement", "constraint"} and not disposition["refs"]:
                    unresolved.append(_unresolved(
                        "source_classification_invalid", reason="Requirement/constraint classification has no artifact reference",
                        source=source["ref"]["source"], disposition=disposition["id"],
                    ))
                    coverage_errors = True
                for reference in disposition["refs"]:
                    artifact_row = control.s.one(
                        "SELECT id FROM artifacts WHERE id=? AND project=?", (reference, project),
                    )
                    if artifact_row is None:
                        unresolved.append(_unresolved(
                            "source_classification_reference_unknown",
                            reason="Source classification names no canonical project artifact",
                            source=source["ref"]["source"], disposition=disposition["id"],
                            reference=reference,
                        ))
                        coverage_errors = True
                character_offsets.update((disposition["start"], disposition["end"]))
                valid_dispositions.append(disposition)
                last_end = disposition["end"]
            byte_for_character = _byte_offsets_for_characters(raw, character_offsets)
            for value in character_offsets:
                if byte_for_character.get(value) is None:
                    unresolved.append(_unresolved(
                        "source_unicode_boundary_unresolved", reason="Classification boundary is not valid UTF-8",
                        source=source["ref"]["source"], character=value,
                    ))
                    coverage_errors = True
            for disposition in valid_dispositions:
                start = byte_for_character[disposition["start"]]
                end = byte_for_character[disposition["end"]]
                if start is not None and end is not None:
                    disposition["byte_start"], disposition["byte_end"] = start, end
                    byte_boundaries.update((start, end))
            for leaf in saved_leaves:
                start, end = leaf["byte_start"], leaf["byte_end"]
                if (type(start) is not int or type(end) is not int
                        or not 0 <= start < end <= len(raw)):
                    unresolved.append(_unresolved(
                        "source_partition_integrity", reason="Saved source span is outside source CAS",
                        source=source["ref"]["source"], item=leaf["id"],
                    ))
                    coverage_errors = True
                    continue
                expected = digest(raw[start:end])
                if expected != leaf["span_hash"]:
                    unresolved.append(_unresolved(
                        "source_partition_integrity", reason="Saved source span hash differs from CAS",
                        source=source["ref"]["source"], item=leaf["id"],
                    ))
                    coverage_errors = True
                offsets = _unicode_offsets(raw, (start, end))
                if offsets.get(start) != leaf["unicode_start"] or offsets.get(end) != leaf["unicode_end"]:
                    unresolved.append(_unresolved(
                        "source_partition_integrity", reason="Saved source span Unicode coordinates differ",
                        source=source["ref"]["source"], item=leaf["id"],
                    ))
                    coverage_errors = True
                if offsets.get(start) is None or offsets.get(end) is None:
                    # The public source_span resolver requires exact Unicode
                    # coordinates.  Preserve the saved byte evidence in the
                    # context, but do not manufacture a typed ref for it.
                    coverage_errors = True
                byte_boundaries.update((start, end))
            ordered_leaves = sorted(saved_leaves, key=lambda item: (item["byte_start"], item["byte_end"], item["id"]))
            cursor = 0
            for leaf in ordered_leaves:
                if leaf["byte_start"] > cursor:
                    unresolved.append(_unresolved(
                        "source_partition_gap", reason="Saved partition omits source bytes",
                        source=source["ref"]["source"], start=cursor, end=leaf["byte_start"],
                    ))
                    coverage_errors = True
                if leaf["byte_start"] < cursor:
                    unresolved.append(_unresolved(
                        "source_partition_overlap", reason="Saved partition source spans overlap",
                        source=source["ref"]["source"], item=leaf["id"],
                    ))
                    coverage_errors = True
                cursor = max(cursor, leaf["byte_end"])
            if ordered_leaves and cursor < len(raw):
                unresolved.append(_unresolved(
                    "source_partition_gap", reason="Saved partition omits trailing source bytes",
                    source=source["ref"]["source"], start=cursor, end=len(raw),
                ))
                coverage_errors = True
            for start, end in zip(sorted(byte_boundaries), sorted(byte_boundaries)[1:]):
                if end <= start:
                    continue
                matching = [item for item in valid_dispositions
                            if item.get("byte_start", -1) <= start and end <= item.get("byte_end", -1)]
                classification = None
                if len(matching) == 1:
                    item = matching[0]
                    classification = {"category": item["category"], "refs": _copy_json(item["refs"]),
                                      "disposition": item["id"]}
                elif len(matching) > 1:
                    unresolved.append(_unresolved(
                        "source_classification_overlap", reason="A source interval has multiple classifications",
                        source=source["ref"]["source"], start=start, end=end,
                    ))
                    coverage_errors = True
                backing = [leaf["id"] for leaf in ordered_leaves
                           if leaf["byte_start"] <= start and end <= leaf["byte_end"]]
                if ordered_leaves and not backing:
                    unresolved.append(_unresolved(
                        "source_partition_gap", reason="Canonical source interval lacks a saved partition leaf",
                        source=source["ref"]["source"], start=start, end=end,
                    ))
                    coverage_errors = True
                    # A missing saved leaf is an unresolved interval, not a
                    # license to synthesize a replacement source span.
                    continue
                span_hash = digest(raw[start:end])
                coordinates = _unicode_offsets(raw, (start, end))
                if coordinates.get(start) is None or coordinates.get(end) is None:
                    unresolved.append(_unresolved(
                        "source_unicode_boundary_unresolved",
                        reason="Canonical source interval has no valid UTF-8 Unicode coordinates",
                        source=source["ref"]["source"], start=start, end=end,
                    ))
                    coverage_errors = True
                    continue
                source_span = _source_span_ref(
                    project, source["ref"]["source"], source["ref"]["blob_digest"],
                    start, end, coordinates[start], coordinates[end], span_hash,
                )
                trace = getattr(control, "traceability", None)
                if trace is None or not hasattr(trace, "_typed_resolve"):
                    unresolved.append(_unresolved(
                        "source_span_resolver_unavailable",
                        reason="Existing source_span resolver is unavailable",
                        source=source["ref"]["source"], start=start, end=end,
                    ))
                    coverage_errors = True
                    continue
                try:
                    trace._typed_resolve(actor, project, source_span["locator"], require_current=False)
                except Fault as exc:
                    unresolved.append(_unresolved(
                        "source_span_unresolved", reason=exc.code,
                        source=source["ref"]["source"], start=start, end=end,
                    ))
                    coverage_errors = True
                    continue
                intervals.append({
                    "id": "span:" + digest({"source": source["ref"]["source"], "start": start, "end": end, "hash": span_hash}),
                    "source_ref": source_span, "byte_start": start, "byte_end": end,
                    "unicode_start": coordinates[start], "unicode_end": coordinates[end],
                    "span_hash": span_hash, "classification": classification,
                    "partition_leaf_ids": sorted(backing),
                })
        if not intervals and saved_leaves:
            # Keep malformed/unknown saved identities visible to later review;
            # no obligation is generated without a verified CAS interval.
            coverage_errors = True
        if intervals:
            status = "incomplete" if coverage_errors else ("partitioned" if saved else "classification_only")
            partitions.append({
                "format": SOURCE_PARTITION_FORMAT, "source_ref": source["ref"],
                "source_id": source["ref"]["source"], "blob_digest": source["ref"]["blob_digest"],
                "characters": source["characters"], "revision": (saved or {}).get("revision"),
                "revision_number": (saved or {}).get("revision_number"),
                "revision_digest": (saved or {}).get("revision_digest"),
                "status": status, "leaves": intervals,
                "saved_leaves": _copy_json(saved_leaves),
                "unclassified": _copy_json(source.get("unclassified", [])),
            })
        elif saved is not None:
            partitions.append({
                "format": SOURCE_PARTITION_FORMAT, "source_ref": source["ref"],
                "source_id": source["ref"]["source"], "blob_digest": source["ref"]["blob_digest"],
                "characters": source["characters"], "revision": saved.get("revision"),
                "revision_number": saved.get("revision_number"), "revision_digest": saved.get("revision_digest"),
                "status": "incomplete", "leaves": [], "saved_leaves": _copy_json(saved_leaves),
                "unclassified": _copy_json(source.get("unclassified", [])),
            })
    return partitions


def _resolve_artifact_pin(control: Any, project: str, ref: dict[str, Any]) -> dict[str, Any]:
    """Resolve an artifact pin to the exact current or retained revision."""
    _typed(ref, project, expected={"artifact"}, name="structural artifact reference")
    current = control.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (ref["artifact"], project))
    if current is None:
        raise Fault("invalid_reference", "Structural artifact reference is unknown", ref["artifact"])
    if ref["revision"] == current["revision"]:
        row = current
    else:
        row = control.s.one("SELECT * FROM revisions WHERE artifact=? AND revision=?", (ref["artifact"], ref["revision"]))
        if row is None:
            raise Fault("invalid_reference", "Structural artifact revision is not retained", ref["revision"])
        row = {**current, **row}
    body = _body(row, name="structural artifact body")
    if digest(body) != ref["body_digest"] or row.get("digest") != ref["body_digest"]:
        raise Fault("stale_reference", "Structural artifact body digest differs", ref["artifact"])
    return {"id": ref["artifact"], "project": project, "kind": current["kind"],
            "revision": ref["revision"], "digest": ref["body_digest"], "body": body}


def _artifact_inventory(control: Any, project: str, unresolved: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values, refs = [], []
    for row in control.s.all("SELECT * FROM artifacts WHERE project=? AND status='accepted' ORDER BY id", (project,)):
        body = _body(row, name="artifact body")
        if digest(body) != row["digest"]:
            _integrity("Artifact body digest differs", row["id"])
        ref = _artifact_ref(project, row)
        structural = artifact_structural_metadata(
            body, kind=row["kind"], project=project,
            resolver=lambda pinned, _control=control: _resolve_artifact_pin(_control, project, pinned),
        )
        if structural["status"] == "invalid":
            unresolved.append(_unresolved(
                "artifact_structural_invalid",
                reason="Stored artifact structural_obligations is invalid and cannot be verified",
                artifact=row["id"], detail=structural.get("reason"),
            ))
        values.append({"ref": ref, "id": row["id"], "kind": row["kind"],
                       "revision": row["revision"], "digest": row["digest"], "body": body,
                       "structural_obligations": structural})
        refs.append(ref)
        source_refs = body.get("source_refs")
        if not isinstance(source_refs, list):
            unresolved.append(_unresolved("source_reference_invalid", reason="Accepted artifact source_refs is not a list", artifact=row["id"]))
            source_refs = []
        for source_id in source_refs:
            if type(source_id) is not str or not source_id:
                unresolved.append(_unresolved("source_reference_invalid", reason="Artifact source reference is not a nonempty source ID", artifact=row["id"], source=source_id))
                continue
            source = control.s.one("SELECT id,blob FROM sources WHERE id=? AND project=?", (source_id, project))
            if source is None:
                unresolved.append(_unresolved("source_reference_unknown", reason="Artifact source reference is not in the controller project", artifact=row["id"], source=source_id))
        # These are the canonical planning-phase artifact kinds.  Scenario
        # and feasibility finding records are retained alongside requirements,
        # boundaries, contracts, design, and test material; treating them as
        # unknown at the Unit3 context boundary would make a normal plan
        # impossible to admit even though Planning requires both kinds.
        if row["kind"] not in {
                "requirement", "scenario", "domain", "interface", "finding",
                "design", "component", "test",
        }:
            unresolved.append(_unresolved("artifact_kind_unsupported", reason="Accepted artifact kind has no Unit2a extractor",
                                          artifact=row["id"], kind=row["kind"]))
    if not any(item["kind"] == "requirement" for item in values):
        unresolved.append(_unresolved("empty_scope_without_authority", reason="No accepted requirement denominator was enumerated"))
    return values, refs


def _task_inventory(control: Any, actor: Any, project: str, stage: str,
                    unresolved: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values, refs = [], []
    for row in control.s.all("SELECT * FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id", (project,)):
        body = _body(row, name="task body")
        task_ref = _task_ref(project, row)
        structural = task_structural_metadata(body, project=project)
        if structural["status"] == "invalid":
            unresolved.append(_unresolved(
                "task_structural_invalid",
                reason="Stored Task structural_obligations is invalid and cannot be verified",
                task=row["id"], detail=structural.get("reason"),
            ))
        if structural["status"] == "declared":
            for collection in ("required_outputs", "required_exercises"):
                for item in structural[collection]:
                    for ref in item["artifact_refs"]:
                        _resolve_artifact_pin(control, project, ref)
        reads = []
        for read in control.s.all("SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact", (row["id"],)):
            artifact = control.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (read["artifact"], project))
            if artifact is None:
                unresolved.append(_unresolved("task_read_missing", reason="Task read artifact is absent",
                                              task=row["id"], artifact=read["artifact"]))
                continue
            if artifact["revision"] != read["revision"] or artifact["digest"] != read["digest"]:
                unresolved.append(_unresolved("task_read_stale", reason="Task read does not match current artifact",
                                              task=row["id"], artifact=read["artifact"],
                                              expected={"revision": read["revision"], "digest": read["digest"]},
                                              actual={"revision": artifact["revision"], "digest": artifact["digest"]}))
            reads.append({"artifact": _artifact_ref(project, artifact), "revision": read["revision"], "digest": read["digest"]})
        registered_read_ids = {item["artifact"]["artifact"] for item in reads}
        if structural["status"] == "declared":
            declared_read_ids = {
                ref["artifact"]
                for collection in ("required_outputs", "required_exercises")
                for item in structural[collection]
                for ref in item["artifact_refs"]
            }
            if not declared_read_ids.issubset(registered_read_ids):
                missing = sorted(declared_read_ids - registered_read_ids)
                raise Fault("integrity_error", "Structural Task reference is not registered in task_reads", missing)
        dependencies = [item["dependency"] for item in control.s.all("SELECT dependency FROM task_deps WHERE task=? ORDER BY dependency", (row["id"],))]
        for dependency in dependencies:
            dep = control.s.one("SELECT project FROM tasks WHERE id=?", (dependency,))
            if dep is None or dep["project"] != project:
                unresolved.append(_unresolved("task_dependency_invalid", reason="Task dependency is missing or cross-project",
                                              task=row["id"], dependency=dependency))
        plan = control.s.one("SELECT * FROM plans WHERE task=?", (row["id"],))
        plan_value = None
        check_refs = []
        if plan is None:
            unresolved.append(_unresolved("test_plan_missing", reason="Task has no frozen test plan", task=row["id"]))
        else:
            plan_body = _body(plan, name="test plan")
            if digest(plan_body) != plan["digest"]:
                _integrity("Frozen test plan digest differs", row["id"])
            plan_ref = _test_plan_ref(control, actor, project, row, plan, unresolved)
            checks = plan_body.get("checks")
            if type(checks) is not list:
                unresolved.append(_unresolved("test_plan_checks_invalid", reason="Frozen test plan checks are not a list", task=row["id"]))
                checks = []
            for index, check in enumerate(checks):
                if type(check) is not dict:
                    unresolved.append(_unresolved("test_check_invalid", reason="Test check is not an object", task=row["id"], index=index))
                    continue
                if plan_ref is not None:
                    check_refs.append(_check_ref(project, plan_ref, check, index))
            plan_value = {"digest": plan["digest"], "approved": plan.get("approved"),
                          "body": plan_body, "ref": plan_ref, "check_refs": check_refs}
            if stage == "task" and plan_ref is None:
                unresolved.append(_unresolved("task_test_plan_unpinned", reason="Task-stage denominator needs a retained plan pin", task=row["id"]))
        candidate = _find_current_candidate(control, actor, project, row, unresolved) if stage in {"task", "integration", "delivery"} else None
        if stage == "task" and row.get("candidate") is None:
            unresolved.append(_unresolved("candidate_missing", reason="Task-stage candidate has not been sealed", task=row["id"]))
        value = {"task_ref": task_ref, "id": row["id"], "revision": row["revision"],
                 "body": body, "reads": reads, "dependencies": sorted(dependencies),
                 "plan": plan_value, "candidate": candidate,
                 "structural_obligations": structural}
        values.append(value); refs.append(task_ref)
    return values, refs


def _requirement_scope_inventory(control: Any, actor: Any, project: str, program: str,
                                 root_row: dict[str, Any] | None,
                                 unresolved: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture the canonical accepted scope and the selected Breakdown body.

    ``Breakdowns._scope`` is the authority for accepted requirements and live
    task revisions.  A retained proposal is useful historical material, but
    its copied scope is never allowed to replace that authority.  Empty scope
    in old hand-built proposal fixtures is treated as absent legacy metadata;
    a non-empty stale scope remains visible as an unresolved integrity issue.
    """
    breakdowns = getattr(control, "breakdowns", None)
    if breakdowns is None or not hasattr(breakdowns, "_scope"):
        _unsupported("Breakdown scope extractor is unavailable")
    current_scope = _copy_json(breakdowns._scope(actor, project), "canonical breakdown scope")
    _object(current_scope, {"requirements", "tasks", "policy"}, name="canonical breakdown scope")
    if not isinstance(current_scope["requirements"], list) or not isinstance(current_scope["tasks"], list):
        _integrity("Canonical breakdown scope requirements/tasks are not lists")
    requirement_ids: set[str] = set()
    for item in current_scope["requirements"]:
        _object(item, {"id", "revision", "digest", "acceptance"}, name="canonical requirement scope item")
        _string(item["id"], "requirement scope id")
        _integer(item["revision"], "requirement scope revision", minimum=1)
        _string(item["digest"], "requirement scope digest")
        if len(item["digest"]) != 64:
            _integrity("Canonical requirement scope digest is malformed", item["id"])
        if not isinstance(item["acceptance"], list) or any(type(value) is not str or not value for value in item["acceptance"]):
            _integrity("Canonical requirement acceptance scope is malformed", item["id"])
        if item["id"] in requirement_ids:
            _integrity("Canonical requirement scope contains a duplicate", item["id"])
        requirement_ids.add(item["id"])
    for item in current_scope["tasks"]:
        _object(item, {"id", "definition_digest"}, name="canonical task scope item")
        _string(item["id"], "task scope id")
        _string(item["definition_digest"], "task scope digest")
        if len(item["definition_digest"]) != 64:
            _integrity("Canonical task scope digest is malformed", item["id"])
    scope = {"format": REQUIREMENT_SCOPE_FORMAT, "project": project, "program": program,
             "requirements": current_scope["requirements"], "tasks": current_scope["tasks"],
             "policy": current_scope["policy"]}
    scope["digest"] = digest(scope)

    body = None
    saved_scope = None
    if root_row is not None:
        body = _body(root_row, name="breakdown body")
        if root_row.get("project") != project or root_row.get("program") != program:
            raise Fault("cross_project", "Breakdown belongs to another project/program", root_row.get("id"))
        saved_scope = body.get("scope")
        if saved_scope is not None and type(saved_scope) is not dict:
            _integrity("Saved breakdown scope is not an object", root_row.get("id"))
        # Some historical fixtures predate the retained scope field and carry
        # ``{}``.  It is not evidence of a competing scope.  Any populated
        # scope, however, must match the canonical authority exactly.
        if saved_scope and saved_scope != current_scope:
            _integrity("Saved Breakdown scope differs from the current accepted scope", root_row.get("id"))
    breakdown_scope = {
        "format": "assurance.breakdown-scope.v1", "project": project, "program": program,
        "id": root_row.get("id") if root_row is not None else None,
        "digest": root_row.get("digest") if root_row is not None else None,
        "status": root_row.get("status") if root_row is not None else "none",
        "body": body, "saved_scope": saved_scope, "current_scope": current_scope,
        "current_scope_digest": scope["digest"],
    }
    return scope, breakdown_scope


def _assignment_inventory(control: Any, project: str, program: str, root: dict[str, Any] | None,
                          task_values: list[dict[str, Any]], requirement_scope: dict[str, Any],
                          breakdown_scope: dict[str, Any], unresolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if root is None or breakdown_scope.get("body") is None:
        return []
    body = breakdown_scope["body"]
    units = body.get("units")
    if type(units) is not list:
        _integrity("Breakdown units are not a list", root.get("id"))
    task_by_id = {item["id"]: item["task_ref"] for item in task_values}
    required_tasks = {item["id"] for item in requirement_scope["tasks"]}
    requirement_by_id = {item["id"]: item for item in requirement_scope["requirements"]}
    unit_by_id: dict[str, dict[str, Any]] = {}
    for unit in units:
        _object(unit, {"id", "title", "parent", "domain", "rationale", "obligations", "tasks", "interfaces", "dependencies"}, name="breakdown unit")
        _string(unit["id"], "breakdown unit id")
        if unit["id"] in unit_by_id:
            _integrity("Breakdown unit identity is duplicated", unit["id"])
        if unit["parent"] is not None:
            _string(unit["parent"], "breakdown unit parent")
        for field in ("obligations", "tasks", "interfaces", "dependencies"):
            if not isinstance(unit[field], list):
                _integrity("Breakdown unit field is not a list", {"unit": unit["id"], "field": field})
        unit_by_id[unit["id"]] = unit
    for unit in units:
        parent = unit["parent"]
        if parent is not None and parent not in unit_by_id:
            _integrity("Breakdown unit parent is unknown", {"unit": unit["id"], "parent": parent})
    # A bounded iterative walk rejects unknown parents and cycles without
    # relying on Python recursion for a large retained hierarchy.
    state: dict[str, int] = {}
    for start in unit_by_id:
        cursor = start
        path: set[str] = set()
        while cursor is not None:
            if cursor in path:
                _integrity("Breakdown unit hierarchy contains a cycle", cursor)
            if state.get(cursor) == 2:
                break
            path.add(cursor)
            state[cursor] = 1
            cursor = unit_by_id[cursor]["parent"]
        for value in path:
            state[value] = 2
    parents = {unit["parent"] for unit in units if unit["parent"] is not None}
    result = []
    seen_pairs: set[tuple[str, str]] = set()
    for unit in units:
        unit_id = unit["id"]
        if unit_id in parents and (unit["obligations"] or unit["tasks"] or unit["interfaces"] or unit["dependencies"] or unit["domain"] is not None):
            _integrity("Aggregate Breakdown unit owns leaf material", unit_id)
        task_refs = []
        for task_id in unit["tasks"]:
            if type(task_id) is not str or not task_id:
                _integrity("Breakdown task identity is malformed", {"unit": unit_id, "task": task_id})
            if task_id not in task_by_id:
                unresolved.append(_unresolved("assignment_task_missing", reason="Breakdown names no current Task", breakdown=root["id"], unit=unit_id, task=task_id))
            else:
                task_refs.append(task_by_id[task_id])
            if task_id not in required_tasks:
                unresolved.append(_unresolved("assignment_task_out_of_scope", reason="Breakdown task is outside canonical current scope", breakdown=root["id"], unit=unit_id, task=task_id))
        pairs = []
        for index, item in enumerate(unit["obligations"]):
            _object(item, {"requirement", "acceptance"}, name="breakdown obligation")
            _string(item["requirement"], "breakdown obligation requirement")
            _string(item["acceptance"], "breakdown obligation acceptance")
            requirement = requirement_by_id.get(item["requirement"])
            if requirement is None:
                _integrity("Breakdown obligation requirement is outside accepted scope", item["requirement"])
            matches = [position for position, value in enumerate(requirement["acceptance"])
                       if value == item["acceptance"]]
            if len(matches) != 1:
                _integrity("Breakdown obligation acceptance is missing or ambiguous", {
                    "requirement": item["requirement"], "acceptance": item["acceptance"],
                })
            pair = (item["requirement"], item["acceptance"])
            if pair in seen_pairs:
                _integrity("Breakdown acceptance is assigned more than once", list(pair))
            seen_pairs.add(pair)
            pairs.append({"requirement": item["requirement"], "acceptance": item["acceptance"],
                          "acceptance_index": matches[0], "obligation_index": index})
        result.append({"breakdown": root, "unit_id": unit_id, "leaf": unit_id not in parents,
                       "task_refs": _unique_sorted(task_refs),
                       "obligation_pairs": pairs})
    return result


def _unit_b_inventory(control: Any, actor: Any, project: str, program: str, stage: str,
                      unresolved: list[dict[str, Any]]) -> dict[str, Any]:
    trace = getattr(control, "traceability", None)
    if trace is None or not hasattr(trace, "_mandatory_bindings"):
        return {"format": "assurance.unit-b-context.v1", "status": "unsupported",
                "bindings": [], "leaves": [], "capabilities": {"supported": False, "reason": "traceability service unavailable"}}
    phase = {"plan": "plan", "task": "implementation", "integration": "integration", "delivery": "delivery"}[stage]
    try:
        bindings = trace._mandatory_bindings(project, program=program, phase=phase)
    except Fault as exc:
        unresolved.append(_unresolved("unit_b_extractor_failed", reason=exc.code, program=program))
        return {"format": "assurance.unit-b-context.v1", "status": "unsupported", "bindings": [], "leaves": [],
                "capabilities": {"supported": False, "reason": exc.code}}
    if not bindings:
        return {"format": "assurance.unit-b-context.v1", "status": "not_applicable", "bindings": [], "leaves": [],
                "capabilities": {"supported": False, "reason": "no_mandatory_binding"}}
    leaves = []
    binding_values = []
    for binding in bindings:
        revision_id = binding["revision"]
        binding_body = _body(binding, name="traceability binding")
        binding_stage = {"plan": "plan", "implementation": "task",
                         "integration": "integration", "delivery": "delivery"}.get(binding_body.get("applicable_from"))
        if binding_stage is None:
            unresolved.append(_unresolved("unit_b_binding_stage_invalid", reason="Mandatory binding has no stable applicable stage", binding=binding["id"]))
            binding_stage = "task"
        binding_adoptions = trace._records_for_subject(project, "traceability_bindings", binding["id"], "binding_adopted") if hasattr(trace, "_records_for_subject") else []
        if len(binding_adoptions) != 1:
            unresolved.append(_unresolved("unit_b_binding_adoption_invalid", reason="Mandatory binding does not have exactly one retained adoption record", binding=binding["id"], count=len(binding_adoptions)))
        binding_adoption = ({"id": binding_adoptions[0]["id"], "digest": binding_adoptions[0]["digest"]}
                            if len(binding_adoptions) == 1 else None)
        revision = control.s.one("SELECT * FROM traceability_revisions WHERE id=? AND project=?", (revision_id, project), True)
        revision_body = _body(revision, name="traceability revision")
        if digest(revision_body) != revision["digest"]:
            _integrity("Traceability revision digest differs", revision_id)
        population = _population_ref(project, revision)
        decisions: dict[str, list[dict[str, Any]]] = {}
        for decision in control.s.all("SELECT * FROM traceability_decisions WHERE revision=? AND project=? ORDER BY created,id", (revision_id, project)):
            if trace._effective_status("traceability_decisions", decision["id"], project) != "accepted":
                continue
            decision_body = trace._decision_body(decision)
            for entry in decision_body.get("decisions", []):
                if isinstance(entry, dict) and isinstance(entry.get("item"), str):
                    decisions.setdefault(entry["item"], []).append({"body": decision_body, "row": decision, "entry": entry})
        items = control.s.all("SELECT * FROM traceability_items WHERE revision=? AND leaf=1 ORDER BY ordinal,id", (revision_id,))
        item_ids = {item["id"] for item in items}
        for item_id in sorted(set(decisions) - item_ids):
            unresolved.append(_unresolved("unit_b_decision_item_missing", reason="Accepted Unit B decision names no retained leaf", revision=revision_id, item=item_id))
        selected_decisions: dict[str, dict[str, Any]] = {}
        for item_id, values in decisions.items():
            if len(values) > 1:
                unresolved.append(_unresolved("unit_b_decision_ambiguous", reason="More than one accepted decision names one leaf", revision=revision_id, item=item_id))
            elif values:
                adoption_records = trace._records_for_subject(project, "traceability_decisions", values[0]["row"]["id"], "decision_adopted") if hasattr(trace, "_records_for_subject") else []
                if len(adoption_records) != 1:
                    unresolved.append(_unresolved("unit_b_decision_adoption_invalid", reason="Accepted decision does not have exactly one retained adoption record", revision=revision_id, item=item_id, count=len(adoption_records)))
                values[0]["adoption_ref"] = ({"id": adoption_records[0]["id"], "digest": adoption_records[0]["digest"]}
                                               if len(adoption_records) == 1 else None)
                selected_decisions[item_id] = values[0]
        mapped_ids = [item_id for item_id, value in selected_decisions.items()
                      if value["entry"].get("handling") in {"port", "replace"}]
        assignment_contracts: dict[str, list[dict[str, Any]]] = {}
        if mapped_ids and hasattr(trace, "_task_assignment_contracts"):
            try:
                assignment_contracts = trace._task_assignment_contracts(
                    project, revision_id, mapped_ids, current=False)
            except Fault as exc:
                unresolved.append(_unresolved("unit_b_assignment_unresolved", reason=exc.code, revision=revision_id))
        for item in items:
            item_body = _body(item, name="traceability item")
            if digest(item_body) != item["digest"]:
                _integrity("Traceability item digest differs", item["id"])
            if item.get("item_kind") not in {"file", "atom", "symbol", "line", "group"}:
                unresolved.append(_unresolved("unit_b_item_type_unsupported", reason="Population item type is outside the finite resolver", item=item["id"], item_kind=item.get("item_kind")))
            decision = selected_decisions.get(item["id"])
            entry = decision["entry"] if decision else None
            handling = entry.get("handling") if entry else "unprocessed"
            if handling not in {"port", "replace", "exclude", "unprocessed"}:
                unresolved.append(_unresolved("unit_b_handling_unsupported", reason="Unit B handling is outside the finite contract", item=item["id"], handling=handling))
                handling = "unprocessed"
            contributors = []
            if entry and handling in {"port", "replace"}:
                if item.get("status") != "known":
                    unresolved.append(_unresolved("unit_b_unknown_item_assignment", reason="Unknown or tombstoned Unit B leaf cannot be ported/replaced", item=item["id"], status=item.get("status")))
                    raw_contributors = None
                else:
                    raw_contributors = assignment_contracts.get(item["id"])
                if not isinstance(raw_contributors, list) or not raw_contributors:
                    unresolved.append(_unresolved("unit_b_contributors_missing", reason="port/replace leaf has no retained contributor contract", item=item["id"]))
                else:
                    for contributor in raw_contributors:
                        if type(contributor) is not dict or type(contributor.get("task")) is not str or type(contributor.get("revision")) is not int or contributor.get("required") is not True:
                            unresolved.append(_unresolved("unit_b_contributor_invalid", reason="Contributor contract is malformed", item=item["id"]))
                            continue
                        task_row = control.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (contributor["task"], project))
                        if task_row is None:
                            unresolved.append(_unresolved("unit_b_task_missing", reason="Contributor Task is absent", item=item["id"], task=contributor["task"]))
                            continue
                        task_ref = _task_ref(project, task_row)
                        if task_row["revision"] != contributor["revision"]:
                            unresolved.append(_unresolved("unit_b_task_stale", reason="Contributor revision is not current", item=item["id"], task=contributor["task"], revision=contributor["revision"]))
                        contributors.append({"task_ref": task_ref, "assignment_refs": [{
                            "kind": "traceability_decision", "id": decision["row"]["id"],
                            "digest": decision["row"]["digest"], "leaf": item["id"], "handling": handling,
                            "adoption_ref": decision.get("adoption_ref"),
                        }]})
            item_ref = _population_item_ref(project, population, item)
            leaves.append({"binding_ref": {"id": binding["id"], "digest": binding["digest"], "revision": revision_id,
                                            "adoption_ref": binding_adoption},
                           "population_ref": population, "ref": item_ref, "id": item["id"], "digest": item["digest"],
                           "status": item["status"], "handling": handling, "required_stage": binding_stage,
                           "contributors": _unique_sorted(contributors),
                           "decision_ref": ({"id": decision["row"]["id"], "digest": decision["row"]["digest"],
                                             "adoption_ref": decision.get("adoption_ref")} if decision else None),
                           "entry": _copy_json(entry) if entry else None})
        binding_values.append({"id": binding["id"], "digest": binding["digest"], "revision": revision_id,
                               "population_ref": population, "adoption_ref": binding_adoption,
                               "body": binding_body})
    return {"format": "assurance.unit-b-context.v1", "status": "available", "bindings": binding_values,
            "leaves": sorted(leaves, key=lambda item: (item["ref"]["population"]["revision"], item["id"])),
            "capabilities": {"supported": True, "reason": "controller_read"}}


def _delivery_declared_outputs(project: str, payload: dict[str, Any], source_ref: dict[str, Any],
                               check_refs: list[dict[str, Any]],
                               unresolved: list[dict[str, Any]]) -> dict[str, Any]:
    """Enumerate the immutable Delivery output declaration for profile v3.

    The declaration is read from the pinned Delivery snapshot only.  Current
    build results, output files, and observed receipts are deliberately not
    consulted here: they are evidence for the later Consumer-C matcher.  A
    malformed or incomplete declaration remains an explicit unresolved
    diagnostic and never turns into an empty successful denominator.
    """
    if "build_definitions" not in payload:
        unresolved.append(_unresolved(
            "delivery_build_definitions_missing",
            reason="Pinned Delivery snapshot has no build_definitions declaration",
            reference=source_ref,
        ))
        return {"status": "unverified", "items": []}
    definitions = payload.get("build_definitions")
    if not isinstance(definitions, list):
        unresolved.append(_unresolved(
            "delivery_build_definitions_invalid",
            reason="Pinned Delivery build_definitions is not a list",
            reference=source_ref,
        ))
        return {"status": "invalid", "items": []}
    if not definitions:
        return {"status": "explicit_empty", "items": []}

    snapshot = payload.get("snapshot")
    repos = snapshot.get("repos") if isinstance(snapshot, dict) else None
    if not isinstance(repos, dict):
        unresolved.append(_unresolved(
            "delivery_snapshot_repository_map_missing",
            reason="Pinned Delivery snapshot has no repository map for output membership",
            reference=source_ref,
        ))
        repos = None
    checks = payload.get("checks")
    if not isinstance(checks, list):
        unresolved.append(_unresolved(
            "delivery_checks_invalid",
            reason="Pinned Delivery checks are unavailable for output producer resolution",
            reference=source_ref,
        ))
        checks = []
    check_by_id: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        if isinstance(check, dict) and isinstance(check.get("id"), str) and check["id"]:
            check_by_id.setdefault(check["id"], []).append(check)
    ref_by_id: dict[str, list[dict[str, Any]]] = {}
    for check_ref in check_refs:
        ref_by_id.setdefault(check_ref["check_id"], []).append(check_ref)

    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(definitions):
        pointer = f"/build_definitions/{index}"
        if type(value) is not dict or set(value) != {"id", "repo", "path"}:
            unresolved.append(_unresolved(
                "delivery_build_definition_invalid",
                reason="Build definition must have exactly id, repo, and path",
                reference=source_ref, index=index,
            ))
            continue
        definition = {key: value[key] for key in ("id", "repo", "path")}
        try:
            validate_definition(definition)
        except Fault as exc:
            unresolved.append(_unresolved(
                "delivery_build_definition_invalid",
                reason=exc.code,
                reference=source_ref, index=index,
            ))
            continue
        output_id = definition["id"]
        if output_id in seen_ids:
            unresolved.append(_unresolved(
                "delivery_build_definition_duplicate",
                reason="Build definition IDs must be unique",
                reference=source_ref, index=index, output_id=output_id,
            ))
            continue
        seen_ids.add(output_id)
        if repos is None or definition["repo"] not in repos:
            unresolved.append(_unresolved(
                "delivery_build_definition_repository_unknown",
                reason="Build definition repository is absent from the pinned snapshot",
                reference=source_ref, index=index, repository=definition["repo"],
            ))
            continue
        producers = []
        for check in checks:
            produced = check.get("produces") if isinstance(check, dict) else None
            if type(produced) is list and all(type(item) is str and item for item in produced):
                if output_id in produced:
                    producers.append(check)
        if len(producers) != 1:
            unresolved.append(_unresolved(
                "delivery_build_definition_producer_unresolved",
                reason="Each declared output must have exactly one producer check",
                reference=source_ref, index=index, output_id=output_id,
                producer_count=len(producers),
            ))
            continue
        producer = producers[0]
        producer_refs = ref_by_id.get(producer.get("id"), [])
        if len(producer_refs) != 1 or digest(producer) != producer_refs[0].get("check_digest"):
            unresolved.append(_unresolved(
                "delivery_build_definition_producer_unresolved",
                reason="Producer check does not have one matching pinned reference",
                reference=source_ref, index=index, output_id=output_id,
            ))
            continue
        delivery_snapshot = source_ref
        if source_ref.get("kind") == "actual_delivery_commit":
            delivery_snapshot = source_ref.get("delivery")
        if not isinstance(delivery_snapshot, dict) or delivery_snapshot.get("kind") != "delivery_snapshot":
            unresolved.append(_unresolved(
                "delivery_build_definition_source_invalid",
                reason="Output declaration source is not a pinned Delivery snapshot",
                reference=source_ref, index=index, output_id=output_id,
            ))
            continue
        identity = {
            "category": DELIVERY_DECLARED_OUTPUT_CATEGORY,
            "source_ref": delivery_snapshot,
            "pointer": pointer,
            "value_digest": digest(definition),
        }
        items.append({
            "index": index, "definition": definition,
            "source_ref": delivery_snapshot, "pointer": pointer,
            "value_digest": identity["value_digest"], "identity": identity,
            "producer_ref": producer_refs[0],
        })
    return {"status": "available" if items else "invalid", "items": items}


def _delivery_material(control: Any, actor: Any, project: str, stage: str, delivery: Any,
                       unresolved: list[dict[str, Any]], *,
                       include_declared_outputs: bool = False) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if delivery is None:
        if stage in {"integration", "delivery"}:
            unresolved.append(_unresolved("delivery_material_missing", reason="Integration/delivery context needs a pinned delivery material"))
        return None, []
    ref = _typed(delivery, project, expected={"delivery_snapshot", "actual_delivery_commit"}, name="delivery reference")
    delivery_id = ref.get("delivery")
    if isinstance(delivery_id, dict):
        delivery_id = delivery_id.get("delivery")
    assurance = getattr(control, "assurance", None)
    if assurance is None:
        unresolved.append(_unresolved("delivery_material_unsupported", reason="E1 material resolver is unavailable", reference=ref))
        material = {"ref": ref, "delivery_id": delivery_id, "resolution": None, "payload": None, "check_refs": []}
        if include_declared_outputs:
            material["declared_outputs"] = {"status": "unverified", "items": []}
        return material, [ref]
    # The Delivery reader follows an actual commit's own typed snapshot
    # dependency and enumerates every saved repository/material row.  A
    # collector failure remains an unresolved v4 material; it must never fall
    # through to the old resolver projection, which could reinterpret a raw
    # selector as a verified snapshot.  Task/plan contexts, where no Delivery
    # selector is supplied, retain their existing compatibility wire.
    try:
        reader = read_delivery_material(control, actor, project=project, delivery=ref)
    except Fault as exc:
        unresolved.append(_unresolved("delivery_material_unresolved", reason=exc.code, reference=ref))
        # Both a precommit snapshot and an actual selector belong to the new
        # Delivery collector boundary.  Preserve only the typed selector and
        # the unresolved diagnostic; do not manufacture payload/check/output
        # values from an older resolver projection.
        material = {
            "ref": ref, "delivery_id": delivery_id,
            "resolution": None, "payload": None, "check_refs": [],
            "reader_context_v4": True,
        }
        if include_declared_outputs:
            material["declared_outputs"] = {"status": "unverified", "items": []}
        return material, [ref]
    else:
        unresolved.extend(reader.get("diagnostics", []))
        reader_ref = reader["snapshot_ref"]
        delivery_refs = [reader_ref, *reader.get("check_refs", []),
                         *reader.get("actual_commit_refs", [])]
        delivery_refs = _unique_sorted([item for item in delivery_refs if isinstance(item, dict)])
        material = {
            "ref": ref,
            "resolution": {"current": {"state": "not_evaluated"}},
            # Existing declared-output/obligation code consumes the immutable
            # snapshot payload.  An actual payload is never substituted here.
            "payload": reader["snapshot_payload"],
            "check_refs": reader["check_refs"],
            "reader": reader,
            "delivery_id": reader["delivery_id"],
            "snapshot_ref": reader_ref,
            "actual_commit_refs": reader["actual_commit_refs"],
            "repositories": reader["repositories"],
            "centers": reader["centers"],
        }
        if include_declared_outputs:
            material["declared_outputs"] = reader["declared_outputs"]
        # Every successful Delivery collection, including a precommit snapshot
        # with no observed Git yet, uses the new reader wire.  Task-only
        # contexts above remain the compatibility boundary for older formats.
        material["reader_context_v4"] = True
        return material, delivery_refs


def _check_delivery_repositories(control: Any, project: str, delivery_material: dict[str, Any] | None,
                                 stage: str, unresolved: list[dict[str, Any]]) -> None:
    if delivery_material is None or stage != "delivery":
        return
    if isinstance(delivery_material.get("reader"), dict):
        # The normalized reader has already checked the complete sealed
        # repository population, including saved observations and every valid
        # actual material candidate.  Do not reintroduce the old one-row
        # ``body.git`` approximation beside it.
        return
    payload = delivery_material.get("payload")
    if not isinstance(payload, dict):
        return
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
    repos = snapshot.get("repos") if isinstance(snapshot, dict) else None
    if not isinstance(repos, dict):
        unresolved.append(_unresolved("delivery_snapshot_unsupported", reason="Delivery snapshot has no repository map"))
        return
    delivery_id = delivery_material["ref"].get("delivery")
    if isinstance(delivery_id, dict):
        delivery_id = delivery_id.get("delivery")
    row = control.s.one("SELECT body FROM deliveries WHERE id=? AND project=?", (delivery_id, project)) if isinstance(delivery_id, str) else None
    body = _body(row, name="delivery row") if row else {}
    observed = body.get("git") if isinstance(body, dict) else {}
    if not isinstance(observed, dict):
        observed = {}
    for repository in sorted(repos):
        if repository not in observed or not isinstance(observed[repository], dict) or not observed[repository].get("commit"):
            unresolved.append(_unresolved("delivery_repository_missing", reason="Delivery stage needs an observed commit for every snapshot repository", repository=repository))


@_read_transaction
def collect_stage_context(control: Any, actor: Any, *, project: str, program: str, stage: str,
                          proposed_breakdown: str | None = None, task: dict[str, Any] | None = None,
                          delivery: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read one controller snapshot and return the exact Unit2a context shape.

    This function only reads.  It does not pin plans, create receipts, change
    selection heads, or update any workflow row.
    """
    if type(stage) is not str or stage not in STAGES:
        raise Fault("invalid_stage", "stage must be one of plan/task/integration/delivery", stage)
    _string(project, "project"); _string(program, "program")
    if proposed_breakdown is not None:
        _string(proposed_breakdown, "proposed_breakdown")
    if task is not None and stage != "task":
        raise Fault("invalid_stage_context", "task context is only valid at task stage")
    if stage == "task" and task is None:
        raise Fault("invalid_stage_context", "task context is required at task stage")
    if task is not None:
        task = _typed(task, project, expected={"task_revision"}, name="task context reference")
    if delivery is not None and stage in {"plan", "task"}:
        raise Fault("invalid_stage_context", "delivery context is not valid at plan/task stage")
    control.k.project(actor, project)
    program_row = control.s.one("SELECT * FROM programs WHERE id=?", (program,), True)
    if program_row["project"] != project:
        raise Fault("cross_project", "Program belongs to another project", program)
    program_body = _body(program_row, name="program body")
    unresolved: list[dict[str, Any]] = []
    capabilities: dict[str, Any] = {
        "controller_read": {"supported": True, "authority": "controller_read"},
        "program": {"id": program, "revision": program_row["revision"],
                     "phase": program_row["phase"], "body_digest": digest(program_body)},
    }

    selection_ref = None
    profile_format = None
    assurance = getattr(control, "assurance", None)
    if assurance is None or not hasattr(assurance, "selected_profile"):
        capabilities["selection"] = {"supported": False, "reason": "selection service unavailable"}
    else:
        selection = assurance.selected_profile(actor, project, program)
        profile_format = selection.get("profile_format")
        selection_ref = _copy_json(selection.get("profile_ref")) if selection.get("profile_ref") is not None else None
        if selection_ref is not None:
            _typed(selection_ref, project, expected={"assurance_object"}, name="selection reference")
            capabilities["selection"] = {"supported": True, "state": selection.get("state")}
        else:
            capabilities["selection"] = {"supported": False, "reason": selection.get("state", "not_enabled")}

    root_row = None
    if proposed_breakdown is not None:
        root_row = control.s.one("SELECT * FROM breakdowns WHERE id=?", (proposed_breakdown,), True)
        if root_row["project"] != project:
            raise Fault("cross_project", "Breakdown belongs to another project", proposed_breakdown)
        if root_row["program"] != program:
            raise Fault("invalid_reference", "Breakdown belongs to another program", proposed_breakdown)
        if root_row["status"] not in {"proposed", "active"}:
            unresolved.append(_unresolved("breakdown_not_selectable", reason="Only proposed or active breakdowns can be read", breakdown=proposed_breakdown))
    else:
        # The existing active index is the canonical root selector.  A latest
        # created proposal is not a substitute for an adopted breakdown.
        root_row = control.s.one("SELECT * FROM breakdowns WHERE program=? AND project=? AND status='active'", (program, project))
    root_plan_ref = _breakdown_ref(root_row) if root_row else None
    if root_plan_ref is None:
        capabilities["breakdown"] = {"supported": False, "reason": "no_selected_or_proposed_breakdown"}
        if stage in {"integration", "delivery"}:
            unresolved.append(_unresolved("breakdown_missing", reason="Integration/delivery denominator needs a canonical root plan", program=program))
    else:
        capabilities["breakdown"] = {"supported": True, "status": root_plan_ref["status"]}

    requirement_scope, breakdown_scope = _requirement_scope_inventory(
        control, actor, project, program, root_row, unresolved,
    )
    source_inputs, source_refs = _source_inventory(control, actor, project, unresolved)
    source_partitions = _source_partition_inventory(
        control, actor, project, source_inputs, unresolved,
        include_unpartitioned=not (
            breakdown_scope["id"] is not None and breakdown_scope["saved_scope"] == {}
        ),
    )
    artifacts, artifact_refs = _artifact_inventory(control, project, unresolved)
    task_values, task_refs = _task_inventory(control, actor, project, stage, unresolved)
    if task is not None:
        selected = next((item for item in task_values if item["task_ref"] == task), None)
        if selected is None:
            unresolved.append(_unresolved("task_context_missing", reason="Requested Task revision is not in the controller enumeration", task=task))
        capabilities["selected_task"] = {"supported": selected is not None, "task_ref": task}
    assignments = _assignment_inventory(
        control, project, program, root_plan_ref, task_values, requirement_scope,
        breakdown_scope, unresolved,
    )
    unit_b = _unit_b_inventory(control, actor, project, program, stage, unresolved)
    capabilities["unit_b"] = unit_b.get("capabilities", {"supported": False, "reason": "unknown"})
    v3_profile = profile_has_outputs(profile_format)
    delivery_material, delivery_refs = _delivery_material(
        control, actor, project, stage, delivery, unresolved,
        include_declared_outputs=v3_profile,
    )
    _check_delivery_repositories(control, project, delivery_material, stage, unresolved)
    capabilities["delivery"] = {"supported": delivery_material is not None if stage in {"integration", "delivery"} else True,
                                  "reason": "controller_read" if delivery_material is not None else "missing_material",
                                  "profile_format": profile_format}
    capabilities["selection"]["profile_format"] = profile_format
    # The relation-registry selector is a v3 addition.  Profile v2 plan and
    # Task contexts deliberately retain their old wire shape, including the
    # absence of this capability.  Adding a digest to that context changes its
    # denominator meaning even when the underlying saved inputs are identical.
    if profile_has_outputs(profile_format):
        capabilities["selection"]["effective_relation_contract_digest"] = (
            selection.get("effective_relation_contract_digest")
            if isinstance(selection, dict) else None
        )
    impact_inventory = collect_impact_inventory(control, actor, project=project, program=program)
    capabilities["impact"] = {
        "supported": True,
        "category": IMPACT_CATEGORY,
        "derivation_version": IMPACT_DERIVATION_VERSION,
        "consumer": IMPACT_CONSUMER,
        "change_count": len(impact_inventory.get("changes", [])),
        "unresolved": len(impact_inventory.get("unresolved", [])),
    }

    # Keep the input set semantic: no current status, attempts, epoch,
    # completed timestamp, receipt arrival, or other telemetry enters it.
    material_refs = [*source_refs, *artifact_refs, *task_refs,
                     *[item["ref"] for item in unit_b.get("leaves", [])], *delivery_refs]
    for partition in source_partitions:
        material_refs.extend(item["source_ref"] for item in partition.get("leaves", []))
    for task_value in task_values:
        plan_value = task_value.get("plan") or {}
        if plan_value.get("ref") is not None:
            material_refs.append(plan_value["ref"])
        material_refs.extend(plan_value.get("check_refs", []))
        structural = task_value.get("structural_obligations")
        if isinstance(structural, dict):
            for collection in ("required_outputs", "required_exercises"):
                for item in structural.get(collection, []):
                    material_refs.extend(item.get("artifact_refs", []))
        candidate_value = task_value.get("candidate")
        if isinstance(candidate_value, dict) and candidate_value.get("ref") is not None:
            material_refs.append(candidate_value["ref"])
    input_refs = _unique_sorted(material_refs)
    input_refs = _unique_sorted([*input_refs, *impact_input_refs(impact_inventory, project)])
    if selection_ref is not None:
        input_refs = _unique_sorted([*input_refs, selection_ref])
    if root_plan_ref is not None:
        capabilities["root_plan_identity"] = {"id": root_plan_ref["id"], "digest": root_plan_ref["digest"]}
    capabilities["enumeration"] = {
        "sources": len(source_inputs), "artifacts": len(artifacts), "tasks": len(task_values),
        "assignments": len(assignments), "unit_b_leaves": len(unit_b.get("leaves", [])),
        "source_partitions": len(source_partitions),
        "source_spans": sum(len(item.get("leaves", [])) for item in source_partitions),
        "child_obligations": sum(len(item.get("obligation_pairs", [])) for item in assignments),
        "test_checks": sum(len(item.get("plan", {}).get("check_refs", [])) for item in task_values if item.get("plan")),
        "impact_changes": len(impact_inventory.get("changes", [])),
        "impacted_targets": len({
            (kind, ident)
            for change in impact_inventory.get("changes", [])
            for side in ("baseline", "current")
            for kind, field in (("artifact", "artifacts"), ("task_revision", "tasks"))
            for ident in change.get(side, {}).get(field, [])
        }),
    }
    if v3_profile and isinstance(delivery_material, dict):
        declared = delivery_material.get("declared_outputs", {})
        if isinstance(declared, dict):
            capabilities["enumeration"]["delivery_declared_outputs"] = len(declared.get("items", []))
    changes = []
    for row in control.s.all("SELECT id,revision,body FROM changes WHERE project=? ORDER BY id", (project,)):
        body = _body(row, name="change body")
        changes.append({"id": row["id"], "revision": row["revision"], "body_digest": digest(body)})
    capabilities["changes"] = changes
    # The pure derivation validates this controller inventory again.  The
    # expected obligation count is computed after deriving in a detached pass;
    # storing row counts above still catches hidden/omitted inputs.
    context = _sealed({
        "format": (CONTEXT_V4_FORMAT if isinstance(delivery_material, dict) and
                    delivery_material.get("reader_context_v4") else
                    CONTEXT_V3_FORMAT if v3_profile else CONTEXT_FORMAT),
        "project": project, "program": program, "stage": stage,
        "selection_ref": selection_ref, "root_plan_ref": root_plan_ref,
        "source_inputs": source_inputs, "artifacts": artifacts, "task_definitions": task_values,
        "assignments": assignments, "unit_b": unit_b, "delivery_material": delivery_material,
        "source_partitions": source_partitions, "requirement_scope": requirement_scope,
        "breakdown_scope": breakdown_scope,
        "impact_inventory": impact_inventory,
        "input_refs": input_refs, "capabilities": capabilities,
        "unresolved": _unique_sorted(unresolved),
    }, _CONTEXT_TOKEN)
    # Derive once for the expected obligation inventory and retain only its
    # count/digest in capability metadata; this does not write any material.
    derived = derive_denominator(context)
    context["capabilities"]["enumeration"]["obligations"] = derived["count"]
    # Adding the final obligation count changes the public mapping, so seal
    # the complete enumeration again before returning it.
    context = _sealed(context, _CONTEXT_TOKEN)
    # Revalidate with the now complete enumeration metadata.  The output is a
    # fresh JSON copy so callers cannot mutate the collector's intermediate.
    derive_denominator(context)
    return context


def _validate_context(context: Any) -> dict[str, Any]:
    _verify_seal(context, token=_CONTEXT_TOKEN, name="stage context")
    _object(dict(context), _CONTEXT_KEYS, name="stage context")
    if context["format"] not in {CONTEXT_FORMAT, CONTEXT_V3_FORMAT, CONTEXT_V4_FORMAT}:
        _invalid("stage context format differs")
    _string(context["project"], "context.project"); _string(context["program"], "context.program")
    if type(context["stage"]) is not str or context["stage"] not in STAGES:
        _invalid("context.stage is invalid")
    for field in ("source_inputs", "artifacts", "task_definitions", "assignments", "input_refs", "unresolved"):
        if not isinstance(context[field], list):
            _invalid(f"context.{field} must be a list")
    if not isinstance(context["source_partitions"], list):
        _invalid("context.source_partitions must be a list")
    for partition in context["source_partitions"]:
        _object(partition, {"format", "source_ref", "source_id", "blob_digest", "characters",
                             "revision", "revision_number", "revision_digest", "status", "leaves",
                             "saved_leaves", "unclassified"}, name="source partition")
        if partition["format"] != SOURCE_PARTITION_FORMAT:
            _invalid("source partition format differs")
        _typed(partition["source_ref"], context["project"], expected={"source"}, name="source partition source")
        locator = partition["source_ref"]
        if locator.get("source") != partition["source_id"] or locator.get("blob_digest") != partition["blob_digest"]:
            _integrity("Source partition source identity differs", partition["source_id"])
        _string(partition["source_id"], "source partition source id")
        _string(partition["blob_digest"], "source partition blob digest")
        if len(partition["blob_digest"]) != 64:
            _integrity("Source partition blob digest is malformed", partition["source_id"])
        _integer(partition["characters"], "source partition characters", minimum=0)
        if partition["status"] not in {"partitioned", "classification_only", "incomplete"}:
            _invalid("source partition status is invalid")
        if partition["revision"] is not None:
            _string(partition["revision"], "source partition revision")
        if partition["revision_number"] is not None:
            _integer(partition["revision_number"], "source partition revision number", minimum=1)
        if partition["revision_digest"] is not None:
            _string(partition["revision_digest"], "source partition revision digest")
        if not isinstance(partition["leaves"], list) or not isinstance(partition["saved_leaves"], list):
            _invalid("source partition leaves must be lists")
        if not isinstance(partition["unclassified"], list):
            _invalid("source partition unclassified must be a list")
        for leaf in partition["leaves"]:
            _object(leaf, {"id", "source_ref", "byte_start", "byte_end", "unicode_start", "unicode_end",
                           "span_hash", "classification", "partition_leaf_ids"}, name="source partition leaf")
            _string(leaf["id"], "source partition leaf id")
            _typed(leaf["source_ref"], context["project"], expected={"traceability_ref"}, name="source partition leaf ref")
            leaf_locator = leaf["source_ref"]["locator"]
            if leaf_locator.get("ref_type") != "source_span":
                _integrity("Source partition leaf is not a source span")
            for field in ("byte_start", "byte_end", "unicode_start", "unicode_end"):
                _integer(leaf[field], f"source partition leaf {field}", minimum=0)
            if leaf["byte_end"] <= leaf["byte_start"] or leaf["unicode_end"] < leaf["unicode_start"]:
                _integrity("Source partition leaf interval is inverted", leaf["id"])
            if leaf_locator.get("byte_start") != leaf["byte_start"] or leaf_locator.get("byte_end") != leaf["byte_end"]:
                _integrity("Source partition leaf locator differs", leaf["id"])
            _string(leaf["span_hash"], "source partition leaf hash")
            if len(leaf["span_hash"]) != 64:
                _integrity("Source partition leaf hash is malformed", leaf["id"])
            if not isinstance(leaf["partition_leaf_ids"], list) or any(type(item) is not str for item in leaf["partition_leaf_ids"]):
                _invalid("source partition leaf backing ids are malformed")
            classification = leaf["classification"]
            if classification is not None:
                _object(classification, {"category", "refs", "disposition"}, name="source partition classification")
                _string(classification["category"], "source partition classification category")
                if not isinstance(classification["refs"], list) or any(type(item) is not str for item in classification["refs"]):
                    _invalid("source partition classification refs are malformed")
                _string(classification["disposition"], "source partition disposition")
    _object(context["requirement_scope"], {"format", "project", "program", "requirements", "tasks", "policy", "digest"}, name="requirement scope")
    if context["requirement_scope"]["format"] != REQUIREMENT_SCOPE_FORMAT:
        _invalid("requirement scope format differs")
    if context["requirement_scope"]["project"] != context["project"] or context["requirement_scope"]["program"] != context["program"]:
        raise Fault("cross_project", "Requirement scope belongs to another project/program")
    if not isinstance(context["requirement_scope"]["requirements"], list) or not isinstance(context["requirement_scope"]["tasks"], list):
        _invalid("requirement scope requirements/tasks must be lists")
    scope_digest = dict(context["requirement_scope"]); scope_digest.pop("digest")
    if digest(scope_digest) != context["requirement_scope"]["digest"]:
        _integrity("Requirement scope digest differs")
    req_ids: set[str] = set()
    for item in context["requirement_scope"]["requirements"]:
        _object(item, {"id", "revision", "digest", "acceptance"}, name="requirement scope requirement")
        _string(item["id"], "requirement scope requirement id")
        _integer(item["revision"], "requirement scope requirement revision", minimum=1)
        _string(item["digest"], "requirement scope requirement digest")
        if len(item["digest"]) != 64 or item["id"] in req_ids:
            _integrity("Requirement scope requirement identity is malformed or duplicated", item["id"])
        req_ids.add(item["id"])
        if not isinstance(item["acceptance"], list) or any(type(value) is not str or not value for value in item["acceptance"]):
            _integrity("Requirement scope acceptance list is malformed", item["id"])
    for item in context["requirement_scope"]["tasks"]:
        _object(item, {"id", "definition_digest"}, name="requirement scope task")
        _string(item["id"], "requirement scope task id")
        _string(item["definition_digest"], "requirement scope task digest")
        if len(item["definition_digest"]) != 64:
            _integrity("Requirement scope task digest is malformed", item["id"])
    _object(context["breakdown_scope"], {"format", "project", "program", "id", "digest", "status", "body", "saved_scope", "current_scope", "current_scope_digest"}, name="breakdown scope")
    if context["breakdown_scope"]["format"] != "assurance.breakdown-scope.v1":
        _invalid("breakdown scope format differs")
    if context["breakdown_scope"]["project"] != context["project"] or context["breakdown_scope"]["program"] != context["program"]:
        raise Fault("cross_project", "Breakdown scope belongs to another project/program")
    if context["breakdown_scope"]["id"] is None:
        if any(context["breakdown_scope"][field] is not None for field in ("digest", "body", "saved_scope")):
            _integrity("Empty breakdown scope carries an identity")
        if context["breakdown_scope"]["status"] != "none":
            _integrity("Empty breakdown scope status differs")
    else:
        _string(context["breakdown_scope"]["id"], "breakdown scope id")
        _string(context["breakdown_scope"]["digest"], "breakdown scope digest")
        if len(context["breakdown_scope"]["digest"]) != 64:
            _integrity("Breakdown scope digest is malformed")
        if not isinstance(context["breakdown_scope"]["body"], dict):
            _integrity("Breakdown scope body is missing")
        if digest(context["breakdown_scope"]["body"]) != context["breakdown_scope"]["digest"]:
            _integrity("Breakdown scope body digest differs")
        if context["breakdown_scope"]["saved_scope"] is not None and type(context["breakdown_scope"]["saved_scope"]) is not dict:
            _integrity("Breakdown saved scope is malformed")
    if context["breakdown_scope"]["current_scope"] != {
            "requirements": context["requirement_scope"]["requirements"],
            "tasks": context["requirement_scope"]["tasks"],
            "policy": context["requirement_scope"]["policy"]}:
        _integrity("Breakdown and requirement canonical scopes differ")
    if context["breakdown_scope"]["current_scope_digest"] != context["requirement_scope"]["digest"]:
        _integrity("Breakdown current scope digest differs")
    if not isinstance(context["capabilities"], dict):
        _invalid("context.capabilities must be an object")
    if not isinstance(context.get("impact_inventory"), dict):
        _invalid("context.impact_inventory must be an object")
    validate_impact_inventory(
        context["impact_inventory"], context["project"], context["program"], require_seal=False,
    )
    if not isinstance(context["unit_b"], dict):
        _invalid("context.unit_b must be an object with explicit unsupported/not_applicable status")
    _object(context["unit_b"], {"format", "status", "bindings", "leaves", "capabilities"}, name="Unit B context")
    if context["unit_b"]["format"] != "assurance.unit-b-context.v1":
        _invalid("Unit B context format differs")
    if context["unit_b"]["status"] not in {"available", "not_applicable", "unsupported"}:
        _invalid("Unit B context status is invalid")
    if not isinstance(context["unit_b"]["bindings"], list) or not isinstance(context["unit_b"]["leaves"], list):
        _invalid("Unit B context bindings/leaves must be lists")
    if not isinstance(context["unit_b"]["capabilities"], dict):
        _invalid("Unit B context capabilities must be an object")
    if context["selection_ref"] is not None:
        _typed(context["selection_ref"], context["project"], expected={"assurance_object"}, name="context selection reference")
    if context["root_plan_ref"] is not None:
        _object(context["root_plan_ref"], {"format", "project", "program", "id", "digest", "status"}, name="context root plan reference")
        if context["root_plan_ref"]["project"] != context["project"] or context["root_plan_ref"]["program"] != context["program"]:
            raise Fault("cross_project", "Context root plan belongs to another project/program")
    selection_capability = context["capabilities"].get("selection")
    if not isinstance(selection_capability, dict):
        _integrity("Context selection capability is missing")
    profile_format = selection_capability.get("profile_format")
    if context["format"] == CONTEXT_V3_FORMAT and not profile_has_outputs(profile_format):
        _integrity("v3 context is not bound to a selected profile.v3 head")
    effective_registry = selection_capability.get("effective_relation_contract_digest")
    if context["format"] == CONTEXT_V4_FORMAT:
        expected_registry = profile_registry(profile_format)
        if (profile_format is not None and
                (profile_format not in CANONICAL_PROFILE_FORMATS or
                 effective_registry != expected_registry)):
            _integrity("v4 context is not bound to the selected relation registry")
    elif context["format"] == CONTEXT_V3_FORMAT:
        if effective_registry != REGISTRY_V2_DIGEST:
            _integrity("v3 context is not bound to the v2 relation registry")
    elif effective_registry is not None and effective_registry != REGISTRY_V1_DIGEST:
        _integrity("legacy context is bound to an unexpected relation registry")
    delivery = context.get("delivery_material")
    reader = delivery.get("reader") if isinstance(delivery, dict) else None
    if reader is not None:
        validate_delivery_material(reader, project=context["project"])
        if context["format"] == CONTEXT_V4_FORMAT and not delivery.get("reader_context_v4"):
            _integrity("v4 context is not bound to a full Delivery material reader")
        if delivery.get("snapshot_ref") != reader.get("snapshot_ref") or delivery.get("delivery_id") != reader.get("delivery_id"):
            _integrity("Delivery material reader identity differs from its context projection")
        if delivery.get("payload") != reader.get("snapshot_payload"):
            _integrity("Delivery material context payload is not the snapshot anchor")
    if profile_has_outputs(profile_format) and isinstance(delivery, dict):
        declared = delivery.get("declared_outputs")
        if declared is not None:
            _object(declared, {"status", "items"}, name="delivery declared outputs")
            if declared["status"] not in {"available", "explicit_empty", "unverified", "invalid"}:
                _invalid("delivery declared output status is invalid")
            if not isinstance(declared["items"], list):
                _invalid("delivery declared output items must be a list")
            seen: set[str] = set()
            for item in declared["items"]:
                _object(item, {"index", "definition", "source_ref", "pointer", "value_digest", "identity", "producer_ref"}, name="delivery declared output")
                _integer(item["index"], "delivery declared output index", minimum=0)
                definition = item["definition"]
                if type(definition) is not dict or set(definition) != {"id", "repo", "path"}:
                    _integrity("Delivery declared output definition is not exact")
                try:
                    validate_definition(definition)
                except Fault as exc:
                    _integrity("Delivery declared output definition is invalid", exc.code)
                if definition["id"] in seen:
                    _integrity("Delivery declared output identity is duplicated", definition["id"])
                seen.add(definition["id"])
                source_ref = _typed(item["source_ref"], context["project"], expected={"delivery_snapshot"}, name="delivery output source")
                _typed(item["producer_ref"], context["project"], expected={"delivery_check"}, name="delivery output producer")
                identity = {"category": DELIVERY_DECLARED_OUTPUT_CATEGORY,
                            "source_ref": source_ref, "pointer": item["pointer"],
                            "value_digest": digest(definition)}
                if item["identity"] != identity or item["value_digest"] != identity["value_digest"]:
                    _integrity("Delivery declared output identity differs")
    return _copy_json(context, "stage context")


def _assignment_lookup(context: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for assignment in context["assignments"]:
        _object(assignment, {"breakdown", "unit_id", "leaf", "task_refs", "obligation_pairs"}, name="assignment")
        if not isinstance(assignment["breakdown"], dict) or not assignment["breakdown"].get("id"):
            _integrity("Assignment has no controller breakdown identity")
        for task_ref in assignment["task_refs"]:
            _typed(task_ref, context["project"], expected={"task_revision"}, name="assignment task reference")
        for pair in assignment["obligation_pairs"]:
            _object(pair, {"requirement", "acceptance", "acceptance_index", "obligation_index"}, name="assignment obligation")
            _string(pair["requirement"], "assignment requirement")
            _string(pair["acceptance"], "assignment acceptance")
            _integer(pair["acceptance_index"], "assignment acceptance index", minimum=0)
            _integer(pair["obligation_index"], "assignment obligation index", minimum=0)
            key = (pair["requirement"], pair["acceptance"])
            result.setdefault(key, []).append(assignment)
    return result


def _unit_b_by_identity(context: dict[str, Any]) -> dict[bytes, list[dict[str, Any]]]:
    result: dict[bytes, list[dict[str, Any]]] = {}
    unit_b = context["unit_b"]
    status = unit_b.get("status")
    if status not in {"available", "not_applicable", "unsupported"}:
        _integrity("Unit B context status is invalid")
    if status != "available":
        return result
    for leaf in unit_b.get("leaves", []):
        _object(leaf, {"binding_ref", "population_ref", "ref", "id", "digest", "status", "handling", "required_stage", "contributors", "decision_ref", "entry"}, name="Unit B leaf")
        _typed(leaf["ref"], context["project"], expected={"population_item"}, name="Unit B leaf reference")
        if leaf["handling"] not in {"port", "replace", "exclude", "defer", "unprocessed"}:
            _unsupported("Unit B handling is unsupported", leaf["handling"])
        values = result.setdefault(canonical(leaf["ref"]["population"]), [])
        values.append(leaf)
    return result


def _expected_enumeration(context: dict[str, Any], obligations_count: int) -> None:
    enumeration = context["capabilities"].get("enumeration")
    if not isinstance(enumeration, dict):
        return
    expected = {
        "sources": len(context["source_inputs"]), "artifacts": len(context["artifacts"]),
        "tasks": len(context["task_definitions"]), "assignments": len(context["assignments"]),
        "unit_b_leaves": len(context["unit_b"].get("leaves", [])),
        "source_partitions": len(context["source_partitions"]),
        "source_spans": sum(len(item.get("leaves", [])) for item in context["source_partitions"]),
        "child_obligations": sum(len(item.get("obligation_pairs", [])) for item in context["assignments"]),
        "test_checks": sum(len(item.get("plan", {}).get("check_refs", [])) for item in context["task_definitions"] if item.get("plan")),
        "impact_changes": len(context["impact_inventory"].get("changes", [])),
        "impacted_targets": len({
            (kind, ident)
            for change in context["impact_inventory"].get("changes", [])
            for side in ("baseline", "current")
            for kind, field in (("artifact", "artifacts"), ("task_revision", "tasks"))
            for ident in change.get(side, {}).get(field, [])
        }),
    }
    if "delivery_declared_outputs" in enumeration:
        delivery = context.get("delivery_material")
        declared = delivery.get("declared_outputs", {}) if isinstance(delivery, dict) else {}
        actual = len(declared.get("items", [])) if isinstance(declared, dict) else 0
        if enumeration["delivery_declared_outputs"] != actual:
            raise Fault("denominator_input_mismatch", "Controller delivery output enumeration differs from supplied context", {
                "expected": enumeration["delivery_declared_outputs"], "actual": actual,
            })
    for key, actual in expected.items():
        if key in enumeration and enumeration[key] != actual:
            raise Fault("denominator_input_mismatch", "Controller enumeration count differs from supplied context", {"field": key, "expected": enumeration[key], "actual": actual})
    if "obligations" in enumeration and enumeration["obligations"] != obligations_count:
        raise Fault("denominator_input_mismatch", "Controller obligation enumeration differs from derived denominator", {"expected": enumeration["obligations"], "actual": obligations_count})


def _expected_input_refs(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-enumerate semantic refs so a pure caller cannot add or hide inputs."""
    values: list[dict[str, Any]] = []
    values.extend(item["ref"] for item in context["source_inputs"] if isinstance(item, dict) and item.get("ref") is not None)
    values.extend(item["ref"] for item in context["artifacts"] if isinstance(item, dict) and item.get("ref") is not None)
    for task in context["task_definitions"]:
        if not isinstance(task, dict):
            continue
        if task.get("task_ref") is not None:
            values.append(task["task_ref"])
        structural = task.get("structural_obligations")
        if isinstance(structural, dict):
            for collection in ("required_outputs", "required_exercises"):
                for item in structural.get(collection, []):
                    values.extend(item.get("artifact_refs", []))
        plan = task.get("plan") if isinstance(task.get("plan"), dict) else {}
        if plan.get("ref") is not None:
            values.append(plan["ref"])
        values.extend(plan.get("check_refs", []))
        candidate = task.get("candidate") if isinstance(task.get("candidate"), dict) else {}
        if candidate.get("ref") is not None:
            values.append(candidate["ref"])
    values.extend(item["ref"] for item in context["unit_b"].get("leaves", [])
                  if isinstance(item, dict) and item.get("ref") is not None)
    for partition in context["source_partitions"]:
        values.extend(item["source_ref"] for item in partition.get("leaves", [])
                      if isinstance(item, dict) and item.get("source_ref") is not None)
    delivery = context.get("delivery_material")
    if isinstance(delivery, dict):
        reader = delivery.get("reader")
        if isinstance(reader, dict):
            # The selector is an audit input, while the semantic Delivery
            # inventory is the anchor snapshot, every declared check, and all
            # validated actual repository refs.  Keeping the same expansion
            # here and in the collector prevents a caller from hiding one
            # repository by passing a one-shot commit return value.
            values.append(reader["snapshot_ref"])
            values.extend(reader.get("check_refs", []))
            values.extend(reader.get("actual_commit_refs", []))
        elif delivery.get("ref") is not None:
            values.append(delivery["ref"])
            values.extend(delivery.get("check_refs", []))
    if context.get("selection_ref") is not None:
        values.append(context["selection_ref"])
    values.extend(impact_input_refs(context["impact_inventory"], context["project"]))
    return _unique_sorted(values)


def _delivery_semantic_projection(value: Any) -> Any:
    """Remove only Delivery capture pins from a semantic material input."""
    if isinstance(value, dict):
        result = {key: _delivery_semantic_projection(item) for key, item in value.items()}
        if result.get("kind") in {"delivery_snapshot", "actual_delivery_commit"}:
            result.pop("pin", None)
        observed = result.get("observed_result")
        if isinstance(observed, dict):
            observed.pop("git_dir", None)
            observed.pop("reconciled", None)
        return result
    if isinstance(value, list):
        return [_delivery_semantic_projection(item) for item in value]
    return value


def _delivery_semantic_inputs(context: dict[str, Any]) -> dict[str, Any]:
    """Return the full Delivery material meaning for the v4 denominator."""
    material = context.get("delivery_material")
    reader = material.get("reader") if isinstance(material, dict) else None
    if not isinstance(reader, dict):
        return _copy_json(material, "delivery material semantic inputs")
    repositories = []
    for entry in reader.get("repositories", []):
        if not isinstance(entry, dict):
            continue
        repositories.append({
            "repository": entry.get("repository"),
            # Git directory and reconciliation markers are observation
            # metadata.  They stay in the retained reader wire but must not
            # change the Delivery meaning digest across restart captures.
            "saved_observed_identity": _delivery_semantic_projection(
                entry.get("saved_observed_identity")
            ),
            "actual_ref": _delivery_semantic_projection(entry.get("actual_ref")),
        })
    return {
        "format": DELIVERY_MATERIAL_FORMAT,
        "delivery_id": reader.get("delivery_id"),
        "snapshot_ref": _delivery_semantic_projection(reader.get("snapshot_ref")),
        "check_refs": _delivery_semantic_projection(reader.get("check_refs", [])),
        "declared_outputs": (
            _delivery_semantic_projection(reader.get("declared_outputs", {}))
            if profile_has_outputs(context.get("capabilities", {}).get("selection", {}).get("profile_format"))
            else {"status": "not_applicable", "items": []}
        ),
        "repositories": repositories,
        "actual_commit_refs": _delivery_semantic_projection(reader.get("actual_commit_refs", [])),
        "extractor": DELIVERY_MATERIAL_EXTRACTOR,
        "repository_extractor": DELIVERY_REPOSITORY_EXTRACTOR,
    }


def _source_semantic_inputs(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the stable source material/partition meaning for input_digest.

    The source blob reference alone is insufficient: changing a disposition
    changes which source span is covered by which artifact.  Reasons are
    explanatory text and are intentionally excluded, while span identity,
    category and exact target refs remain semantic inputs.
    """
    result = []
    for source in context["source_inputs"]:
        _object(source, {"ref", "locator", "characters", "material", "unclassified", "dispositions"},
                name="source inventory item")
        material = source["material"]
        _object(material, {"status", "digest", "characters"}, name="source material status")
        dispositions = []
        for disposition in source["dispositions"]:
            _object(disposition, {"id", "start", "end", "category", "refs", "reason"},
                    name="source disposition")
            dispositions.append({"id": disposition["id"], "start": disposition["start"],
                                 "end": disposition["end"], "category": disposition["category"],
                                 "refs": disposition["refs"]})
        result.append({"ref": source["ref"], "characters": source["characters"],
                       "material": material, "unclassified": source["unclassified"],
                       "dispositions": dispositions})
    return _unique_sorted(result)


def _source_partition_semantic_inputs(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the verified source partition and classification boundaries.

    The saved leaf item digest and exact source span are semantic.  A review
    reason or receipt is deliberately absent, so explanatory telemetry cannot
    invalidate a denominator.
    """
    result = []
    for partition in context["source_partitions"]:
        _object(partition, {"format", "source_ref", "source_id", "blob_digest", "characters",
                             "revision", "revision_number", "revision_digest", "status", "leaves",
                             "saved_leaves", "unclassified"}, name="source partition semantic input")
        leaves = []
        for leaf in partition["leaves"]:
            classification = leaf.get("classification")
            leaves.append({"source_ref": leaf["source_ref"], "byte_start": leaf["byte_start"],
                           "byte_end": leaf["byte_end"], "unicode_start": leaf["unicode_start"],
                           "unicode_end": leaf["unicode_end"], "span_hash": leaf["span_hash"],
                           "classification": classification,
                           "partition_leaf_ids": leaf["partition_leaf_ids"]})
        saved_leaves = []
        for leaf in partition["saved_leaves"]:
            saved_leaves.append({key: leaf[key] for key in (
                "id", "ordinal", "item_kind", "status", "byte_start", "byte_end",
                "unicode_start", "unicode_end", "span_hash", "digest")})
        result.append({"source_ref": partition["source_ref"], "source_id": partition["source_id"],
                       "blob_digest": partition["blob_digest"], "characters": partition["characters"],
                       "revision": partition["revision"], "revision_number": partition["revision_number"],
                       "revision_digest": partition["revision_digest"], "status": partition["status"],
                       "leaves": leaves, "saved_leaves": saved_leaves,
                       "unclassified": partition["unclassified"]})
    return _unique_sorted(result)


def _contributors_for_pair(context: dict[str, Any], requirement: str, acceptance: str,
                           assignments: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    for assignment in assignments.get((requirement, acceptance), []):
        for task_ref in assignment["task_refs"]:
            result.append({"task_ref": task_ref, "assignment_refs": [{
                "kind": "breakdown_assignment", "breakdown": assignment["breakdown"]["id"],
                "breakdown_digest": assignment["breakdown"]["digest"], "unit": assignment["unit_id"],
            }]})
    # A Unit B artifact_ac assignment is a second authoritative contributor
    # source.  Match only exact artifact/ac identity; never assign it by leaf
    # position or by a caller-supplied subset.
    unit_b = context["unit_b"]
    if unit_b.get("status") == "available":
        for leaf in unit_b.get("leaves", []):
            for contributor in leaf.get("contributors", []):
                entry = leaf.get("entry") or {}
                for field in ("requirement", "acceptance"):
                    ref = entry.get(field)
                    if not isinstance(ref, dict):
                        continue
                    locator = ref.get("locator") if ref.get("kind") == "traceability_ref" else None
                    if not isinstance(locator, dict):
                        continue
                    expected_pointer = f"/acceptance/{_acceptance_index(context, requirement, acceptance)}"
                    if (locator.get("artifact") == requirement
                            and locator.get("ac_pointer") == expected_pointer
                            and locator.get("ac_digest") == digest(acceptance)):
                        result.append(contributor)
                        break
    return _unique_sorted(result)


def _contributors_for_requirement(context: dict[str, Any], requirement: str,
                                  assignments: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    accepted = next((item.get("body", {}).get("acceptance", [])
                     for item in context["artifacts"]
                     if item.get("id") == requirement and item.get("kind") == "requirement"), [])
    for value in accepted:
        result.extend(_contributors_for_pair(context, requirement, value, assignments))
    return _unique_sorted(result)


def _acceptance_index(context: dict[str, Any], artifact_id: str, value: str) -> int:
    for item in context["artifacts"]:
        if item.get("id") == artifact_id and item.get("kind") == "requirement":
            values = item.get("body", {}).get("acceptance", [])
            for index, candidate in enumerate(values):
                if candidate == value:
                    return index
    return -1


def _validate_obligation(obligation: Any, project: str) -> None:
    _object(obligation, {"id", "category", "source_ref", "pointer", "value_digest", "contributors", "introduced_at", "required_at"}, name="obligation")
    if type(obligation["id"]) is not str or not obligation["id"].startswith("obligation:"):
        _integrity("Obligation identity is malformed")
    _typed(obligation["source_ref"], project, name="obligation source reference")
    _string(obligation["category"], "obligation.category")
    _string(obligation["pointer"], "obligation.pointer")
    _string(obligation["value_digest"], "obligation.value_digest")
    if len(obligation["value_digest"]) != 64:
        _integrity("Obligation value digest is malformed", obligation["id"])
    if not isinstance(obligation["contributors"], list):
        _integrity("Obligation contributors are not a list", obligation["id"])
    for contributor in obligation["contributors"]:
        _object(contributor, {"task_ref", "assignment_refs"}, name="obligation contributor")
        _typed(contributor["task_ref"], project, expected={"task_revision"}, name="obligation contributor task")
        if not isinstance(contributor["assignment_refs"], list):
            _integrity("Obligation assignment refs are not a list", obligation["id"])
    _string(obligation["introduced_at"], "obligation.introduced_at")
    _string(obligation["required_at"], "obligation.required_at")
    if obligation["introduced_at"] not in STAGES or obligation["required_at"] not in STAGES:
        _invalid("obligation stage is outside the stable stage vocabulary")
    if _STAGE_ORDER[obligation["required_at"]] < _STAGE_ORDER[obligation["introduced_at"]]:
        _integrity("Obligation required stage precedes its introduction", obligation["id"])


def derive_denominator(context: dict[str, Any]) -> dict[str, Any]:
    """Derive the complete denominator from a validated controller context."""
    context = _validate_context(context)
    project, program, stage = context["project"], context["program"], context["stage"]
    assignments = _assignment_lookup(context)
    obligations: dict[str, dict[str, Any]] = {}
    unresolved = list(context["unresolved"])

    artifacts_by_id = {}
    for artifact in context["artifacts"]:
        _object(artifact, {"ref", "id", "kind", "revision", "digest", "body"},
                optional={"structural_obligations"}, name="artifact inventory item")
        if artifact["id"] in artifacts_by_id:
            _integrity("Artifact inventory identity is duplicated", artifact["id"])
        expected_structural = artifact_structural_metadata(artifact["body"], kind=artifact["kind"],
                                                           project=project)
        if artifact.get("structural_obligations", expected_structural) != expected_structural:
            _integrity("Artifact structural obligation metadata differs", artifact["id"])
        artifacts_by_id[artifact["id"]] = artifact
    scope_requirements = {item["id"]: item for item in context["requirement_scope"]["requirements"]}
    if set(scope_requirements) != {item["id"] for item in context["artifacts"] if item["kind"] == "requirement"}:
        _integrity("Canonical requirement scope and accepted artifact inventory differ")
    for requirement_id, scope_item in scope_requirements.items():
        artifact = artifacts_by_id.get(requirement_id)
        if artifact is None:
            _integrity("Canonical requirement is absent from artifact inventory", requirement_id)
        if (artifact["revision"], artifact["digest"], artifact["body"].get("acceptance")) != (
                scope_item["revision"], scope_item["digest"], scope_item["acceptance"]):
            _integrity("Canonical requirement identity differs from artifact inventory", requirement_id)

    for task in context["task_definitions"]:
        _object(task, {"task_ref", "id", "revision", "body", "reads", "dependencies", "plan", "candidate"},
                optional={"structural_obligations"}, name="task inventory item")
        task_ref = _typed(task["task_ref"], project, expected={"task_revision"}, name="task inventory reference")
        if task_ref.get("task") != task["id"] or task_ref.get("revision") != task["revision"]:
            _integrity("Task inventory reference identity differs", task["id"])
        if digest(task["body"]) != task_ref.get("definition_digest"):
            _integrity("Task inventory definition digest differs", task["id"])
        expected_structural = task_structural_metadata(task["body"], project=project)
        if task.get("structural_obligations", expected_structural) != expected_structural:
            _integrity("Task structural obligation metadata differs", task["id"])

    # A pre-Unit2c hand-built proposal may have an empty copied ``scope``.
    # Keep the old denominator readable while explicitly leaving the new
    # requirement/child extractor unavailable for that material.  A canonical
    # accepted scope (including a proposed root) enables both categories.
    hierarchy_extractor_supported = not (
        context["breakdown_scope"]["id"] is not None
        and context["breakdown_scope"]["saved_scope"] == {}
    )

    # Unit2c-1 source-span denominator.  A verified CAS interval is retained
    # even when its classification is background/excluded; meaning review is
    # represented by unresolved metadata and is never inferred here.
    for partition in context["source_partitions"]:
        for leaf in partition["leaves"]:
            source_ref = _typed(leaf["source_ref"], project, expected={"traceability_ref"}, name="source span obligation ref")
            locator = source_ref["locator"]
            pointer = f"/sources/{partition['source_id']}/spans/{leaf['byte_start']}:{leaf['byte_end']}"
            identity = {"category": "source_span", "source_ref": source_ref,
                        "pointer": pointer, "value_digest": leaf["span_hash"]}
            oid = "obligation:" + digest(identity)
            if oid in obligations:
                _integrity("Duplicate source span obligation identity", oid)
            obligations[oid] = {"id": oid, "category": "source_span", "source_ref": source_ref,
                                "pointer": pointer, "value_digest": leaf["span_hash"],
                                "contributors": [], "introduced_at": "plan", "required_at": "plan"}
        if partition["status"] != "partitioned":
            unresolved.append(_unresolved(
                "source_partition_incomplete",
                reason="Source span extraction has an incomplete or classification-only partition",
                source=partition["source_id"], status=partition["status"],
            ))
        if partition["unclassified"]:
            unresolved.append(_unresolved(
                "source_classification_gap",
                reason="Source characters remain outside saved classification dispositions",
                source=partition["source_id"], gaps=partition["unclassified"],
            ))

    for artifact in context["artifacts"]:
        ref = _typed(artifact["ref"], project, expected={"artifact"}, name="artifact inventory ref")
        if artifact["id"] != ref["artifact"] or artifact["digest"] != ref["body_digest"]:
            _integrity("Artifact inventory identity differs", artifact["id"])
        if artifact["kind"] != "requirement":
            continue
        body = artifact["body"]
        if hierarchy_extractor_supported:
            requirement_identity = {"category": "requirement", "source_ref": ref,
                                    "pointer": f"/requirements/{artifact['id']}",
                                    "value_digest": artifact["digest"]}
            requirement_oid = "obligation:" + digest(requirement_identity)
            obligations[requirement_oid] = {
                "id": requirement_oid, "category": "requirement", "source_ref": ref,
                "pointer": requirement_identity["pointer"], "value_digest": artifact["digest"],
                "contributors": _contributors_for_requirement(context, artifact["id"], assignments),
                "introduced_at": "plan", "required_at": "plan",
            }
        acceptance = body.get("acceptance")
        if not isinstance(acceptance, list):
            unresolved.append(_unresolved("requirement_acceptance_unsupported", reason="Requirement acceptance is not a list", artifact=artifact["id"]))
            continue
        for index, value in enumerate(acceptance):
            if type(value) is not str or not value:
                unresolved.append(_unresolved("acceptance_identity_invalid", reason="Acceptance condition is not a nonempty string", artifact=artifact["id"], index=index))
                continue
            source_ref = _ac_ref(project, artifact, index, value)
            category, pointer, value_digest = "acceptance_condition", f"/acceptance/{index}", digest(value)
            identity = {"category": category, "source_ref": source_ref, "pointer": pointer, "value_digest": value_digest}
            oid = "obligation:" + digest(identity)
            obligations[oid] = {"id": oid, "category": category, "source_ref": source_ref,
                                "pointer": pointer, "value_digest": value_digest,
                                "contributors": _contributors_for_pair(context, artifact["id"], value, assignments),
                                "introduced_at": "plan", "required_at": "plan"}

    # Every retained Breakdown child obligation is a distinct denominator
    # identity.  The exact saved unit/index and the pinned requirement AC
    # reference are both part of its pointer/value; no unit hierarchy is
    # interpreted as a semantic decomposes relation here.
    if hierarchy_extractor_supported:
        for assignment in context["assignments"]:
            for pair in assignment["obligation_pairs"]:
                artifact = artifacts_by_id[pair["requirement"]]
                acceptance = artifact["body"]["acceptance"][pair["acceptance_index"]]
                source_ref = _ac_ref(project, artifact, pair["acceptance_index"], acceptance)
                pointer = (f"/breakdowns/{assignment['breakdown']['id']}/units/"
                           f"{assignment['unit_id']}/obligations/{pair['obligation_index']}")
                value_digest = digest({"requirement": pair["requirement"],
                                       "revision": artifact["revision"],
                                       "requirement_digest": artifact["digest"],
                                       "acceptance_index": pair["acceptance_index"],
                                       "acceptance": acceptance})
                identity = {"category": "child_obligation", "source_ref": source_ref,
                            "pointer": pointer, "value_digest": value_digest}
                oid = "obligation:" + digest(identity)
                if oid in obligations:
                    _integrity("Duplicate child obligation identity", oid)
                contributors = _contributors_for_pair(
                    context, pair["requirement"], pair["acceptance"], assignments,
                )
                if not contributors:
                    unresolved.append(_unresolved(
                        "child_obligation_unassigned",
                        reason="Retained child obligation has no current Task contributor",
                        breakdown=assignment["breakdown"]["id"], unit=assignment["unit_id"],
                        obligation_index=pair["obligation_index"],
                    ))
                obligations[oid] = {"id": oid, "category": "child_obligation", "source_ref": source_ref,
                                    "pointer": pointer, "value_digest": value_digest,
                                    "contributors": contributors, "introduced_at": "plan", "required_at": "plan"}

    # Partial retained material keeps every valid Q alongside its diagnostics;
    # complete writers and node/history issuance use the strict shared wrapper.
    from .domain_responsibility import responsibility_material
    for artifact in context["artifacts"]:
        artifact_ref = _typed(artifact["ref"], project, expected={"artifact"}, name="artifact responsibility ref")
        records, _, diagnostics = responsibility_material(artifact_ref, artifact["kind"], artifact["body"])
        for diagnostic in diagnostics:
            if diagnostic.unresolved not in unresolved:
                unresolved.append(diagnostic.unresolved)
        for record in records:
            if record["id"] in obligations:
                _integrity("Duplicate artifact responsibility identity", record["id"])
            obligations[record["id"]] = record

    # B's Task declarations are explicit denominator leaves.  They are tied
    # to the exact Task revision and declaration item; no write_path, future
    # candidate, or output_artifact adapter is consulted here.
    for task in context["task_definitions"]:
        structural = task.get("structural_obligations", {})
        if not isinstance(structural, dict):
            continue
        task_ref = _typed(task["task_ref"], project, expected={"task_revision"}, name="Task structural obligation ref")
        for collection, category in (("required_outputs", "required_output"), ("required_exercises", "required_exercise")):
            for index, item in enumerate(structural.get(collection, [])):
                pointer = f"/tasks/{task['id']}/structural_obligations/{collection}/{index}"
                value_digest = digest(item)
                identity = {"category": category, "source_ref": task_ref,
                            "pointer": pointer, "value_digest": value_digest}
                oid = "obligation:" + digest(identity)
                if oid in obligations:
                    _integrity("Duplicate Task structural obligation identity", oid)
                obligations[oid] = {
                    "id": oid, "category": category, "source_ref": task_ref,
                    "pointer": pointer, "value_digest": value_digest,
                    "contributors": [{"task_ref": task_ref, "assignment_refs": [{
                        "kind": "task_structural", "task": task["id"],
                        "collection": collection, "index": index,
                    }]}],
                    "introduced_at": "task", "required_at": "task",
                }

    # A fixed test plan is part of the Task denominator.  Unpinned plans are
    # retained as unresolved material rather than represented by a fake ref.
    for task in context["task_definitions"]:
        plan = task.get("plan")
        if not isinstance(plan, dict):
            continue
        plan_ref = plan.get("ref")
        checks = plan.get("body", {}).get("checks", [])
        if not isinstance(checks, list):
            _integrity("Task test plan checks are not a list", task.get("id"))
        if plan_ref is None:
            if plan.get("check_refs") not in (None, []):
                raise Fault("denominator_input_mismatch", "Unpinned task plan cannot carry check references", task.get("id"))
            if checks:
                unresolved.append(_unresolved("required_checks_unpinned", reason="Required check identities need a retained test-plan pin", task=task["id"], count=len(checks)))
            continue
        _typed(plan_ref, project, expected={"test_plan"}, name="task plan ref")
        check_refs = plan.get("check_refs", [])
        if not isinstance(check_refs, list):
            _integrity("Task test plan check references are not a list", task.get("id"))
        if len(check_refs) != len(checks):
            raise Fault("denominator_input_mismatch", "Pinned plan check inventory differs", {
                "task": task.get("id"), "expected": len(checks), "actual": len(check_refs),
            })
        for index, check_ref in enumerate(check_refs):
            _typed(check_ref, project, expected={"test_plan_check"}, name="task check ref")
            check = checks[index] if index < len(checks) else {}
            pointer, value_digest = f"/tasks/{task['id']}/plan/checks/{index}", check_ref["check_digest"]
            # The check reference retains its actual material pin for
            # authority resolution. Obligation meaning is bound to the
            # immutable Task/plan/check definition, so equivalent capture
            # pins cannot replace the obligation identity.
            identity = {"category": "required_check",
                        "source_ref": semantic_definition_projection(check_ref),
                        "pointer": pointer, "value_digest": value_digest}
            oid = "obligation:" + digest(identity)
            obligations[oid] = {"id": oid, "category": "required_check", "source_ref": check_ref,
                                "pointer": pointer, "value_digest": value_digest,
                                "contributors": [{"task_ref": task["task_ref"], "assignment_refs": [{"kind": "task_check", "task": task["id"], "check": check_ref["check_id"]}]}],
                                "introduced_at": "task", "required_at": "task"}

    # Delivery snapshots carry their own immutable declared checks.  They are
    # separate identities from Task checks and remain explicit even when the
    # later execution observation is absent or failed.
    delivery_material = context.get("delivery_material")
    if isinstance(delivery_material, dict):
        delivery_checks = delivery_material.get("check_refs", [])
        for index, check_ref in enumerate(delivery_checks):
            _typed(check_ref, project, expected={"delivery_check"}, name="delivery check ref")
            identity = {"category": "delivery_check", "source_ref": check_ref,
                        "pointer": f"/delivery/checks/{index}",
                        "value_digest": check_ref["check_digest"]}
            oid = "obligation:" + digest(identity)
            obligations[oid] = {"id": oid, "category": "delivery_check", "source_ref": check_ref,
                                "pointer": identity["pointer"], "value_digest": check_ref["check_digest"],
                                "contributors": [], "introduced_at": "integration", "required_at": "integration"}
        if profile_has_outputs(context["capabilities"].get("selection", {}).get("profile_format")):
            declared = delivery_material.get("declared_outputs")
            if not isinstance(declared, dict):
                unresolved.append(_unresolved(
                    "delivery_build_definitions_missing",
                    reason="v3 delivery context has no declaration inventory",
                ))
            else:
                for item in declared.get("items", []):
                    source_ref = _typed(item["source_ref"], project,
                                        expected={"delivery_snapshot"},
                                        name="delivery output declaration source")
                    identity = {
                        "category": DELIVERY_DECLARED_OUTPUT_CATEGORY,
                        "source_ref": source_ref,
                        "pointer": item["pointer"],
                        "value_digest": item["value_digest"],
                    }
                    oid = "obligation:" + digest(identity)
                    if oid in obligations:
                        _integrity("Duplicate delivery declared output identity", oid)
                    obligations[oid] = {
                        "id": oid, "category": DELIVERY_DECLARED_OUTPUT_CATEGORY,
                        "source_ref": source_ref, "pointer": item["pointer"],
                        "value_digest": item["value_digest"], "contributors": [],
                        "introduced_at": "integration", "required_at": "delivery",
                    }

    # Every retained Unit B leaf is an obligation, including exclude/defer and
    # unprocessed leaves.  A leaf is never removed because it has no mapping.
    unit_b = context["unit_b"]
    if unit_b.get("status") == "available":
        for leaf in unit_b.get("leaves", []):
            source_ref = _typed(leaf["ref"], project, expected={"population_item"}, name="population leaf ref")
            identity = {"category": "population_leaf", "source_ref": source_ref,
                        "pointer": f"/population/{source_ref['population']['revision']}/items/{leaf['id']}",
                        "value_digest": leaf["digest"]}
            oid = "obligation:" + digest(identity)
            obligations[oid] = {"id": oid, "category": "population_leaf", "source_ref": source_ref,
                                "pointer": identity["pointer"], "value_digest": leaf["digest"],
                                "contributors": _unique_sorted(leaf.get("contributors", [])),
                                "introduced_at": leaf.get("required_stage", "task"),
                                "required_at": leaf.get("required_stage", "task")}

    # Unit 2c-4 impact targets are derived from the complete baseline/current
    # change snapshots.  Packet leaves are evidence only; they never create
    # extra denominator leaves.  A colliding identity must have identical
    # meaning across categories rather than being silently overwritten.
    for impact_obligation in impact_obligations(context["impact_inventory"], project):
        existing = obligations.get(impact_obligation["id"])
        if existing is not None and existing != impact_obligation:
            _integrity("Duplicate obligation identity across categories", impact_obligation["id"])
        obligations[impact_obligation["id"]] = impact_obligation

    ordered = [obligations[key] for key in sorted(obligations)]
    for obligation in ordered:
        _validate_obligation(obligation, project)
    # Duplicate identities are rejected rather than collapsed.  Dict insertion
    # above is only a convenience; two equal IDs with unequal content are an
    # integrity error, and equal identities from two source positions are
    # impossible because source ref + pointer are part of the identity.
    identity_ids = [item["id"] for item in ordered]
    if len(identity_ids) != len(set(identity_ids)):
        _integrity("Duplicate obligation identity")
    input_refs = []
    for ref in context["input_refs"]:
        input_refs.append(_typed(ref, project, name="context input reference"))
    input_refs = _unique_sorted(input_refs)
    expected_input_refs = _expected_input_refs(context)
    if [canonical(ref) for ref in input_refs] != [canonical(ref) for ref in expected_input_refs]:
        raise Fault("denominator_input_mismatch", "Controller semantic input enumeration differs from supplied context", {
            "expected": expected_input_refs, "actual": input_refs,
        })
    semantic_input_refs = semantic_definition_projection(input_refs)
    if context["format"] == CONTEXT_V4_FORMAT:
        semantic_input_refs = _delivery_semantic_projection(semantic_input_refs)
    semantic_inputs = {
        "selection_ref": context["selection_ref"], "root_plan_ref": context["root_plan_ref"],
        # Keep the full input refs above as retained authority. Only the
        # meaning digest strips equivalent test-plan observation pins.
        "input_refs": semantic_input_refs,
        "source_inputs": _source_semantic_inputs(context),
        "source_partitions": _source_partition_semantic_inputs(context),
        "artifact_structural_obligations": [
            {"id": item["id"], "revision": item["revision"], "digest": item["digest"],
             "kind": item["kind"], "declaration": item.get("structural_obligations", {
                 "status": "legacy_unavailable", "items": []})}
            for item in context["artifacts"]
        ],
        "task_structural_obligations": [
            {"task_ref": item["task_ref"], "declaration": item.get("structural_obligations", {
                 "status": "legacy_unavailable", "required_outputs": [], "required_exercises": []})}
            for item in context["task_definitions"]
        ],
        "requirement_scope": context["requirement_scope"],
        "breakdown_scope": {key: context["breakdown_scope"][key]
                            for key in ("id", "digest", "status", "saved_scope", "current_scope_digest")},
        "assignments": context["assignments"],
        "unit_b": context["unit_b"],
        "delivery_material": (
            _delivery_semantic_inputs(context)
            if context["format"] == CONTEXT_V4_FORMAT and isinstance(context.get("delivery_material"), dict)
            else {"ref": context["delivery_material"].get("ref"),
                  "check_refs": context["delivery_material"].get("check_refs", []),
                  "declared_outputs": context["delivery_material"].get("declared_outputs", {})}
            if context["format"] == CONTEXT_V3_FORMAT and isinstance(context.get("delivery_material"), dict)
            else context["delivery_material"]
        ),
        "impact_inventory": context["impact_inventory"],
        "changes": context["capabilities"].get("changes", []),
        "program": context["capabilities"].get("program"),
        "stage": stage,
    }
    input_digest = digest(semantic_inputs)
    capabilities = _copy_json(context["capabilities"], "context capabilities")
    capabilities["extractors"] = {
        "source_span": {"supported": True, "version": "source-span.v1",
                        "meaning_review": "pending" if context["source_partitions"] else "not_applicable"},
        "requirement": {"supported": hierarchy_extractor_supported, "version": "requirement-scope.v1",
                         "reason": "canonical_scope" if hierarchy_extractor_supported else "legacy_scope_missing"},
        "child_obligation": {"supported": hierarchy_extractor_supported, "version": "requirement-hierarchy.v1",
                              "reason": "canonical_scope" if hierarchy_extractor_supported else "legacy_scope_missing",
                              "meaning_review": "pending" if hierarchy_extractor_supported and context["assignments"] else "not_applicable"},
        "artifact_responsibility": {"supported": True, "version": "artifact-structural.v1",
                                     "legacy_unavailable": sum(
                                         item.get("structural_obligations", {}).get("status") == "legacy_unavailable"
                                         for item in context["artifacts"]),
                                     "explicit_empty": sum(
                                         item.get("structural_obligations", {}).get("status") == "explicit_empty"
                                         for item in context["artifacts"]),
                                     "invalid": sum(
                                         item.get("structural_obligations", {}).get("status") == "invalid"
                                         for item in context["artifacts"])},
        "task_structural": {"supported": True, "version": "task-structural.v1",
                             "legacy_unavailable": sum(
                                 item.get("structural_obligations", {}).get("status") == "legacy_unavailable"
                                 for item in context["task_definitions"]),
                             "explicit_empty": sum(
                                 item.get("structural_obligations", {}).get("status") == "explicit_empty"
                                 for item in context["task_definitions"]),
                             "invalid": sum(
                                 item.get("structural_obligations", {}).get("status") == "invalid"
                                 for item in context["task_definitions"])},
    }
    selected_profile_format = context["capabilities"].get("selection", {}).get("profile_format")
    if (context["format"] in {CONTEXT_V3_FORMAT, CONTEXT_V4_FORMAT} and
            profile_has_outputs(selected_profile_format)):
        # Plan/Task v3 contexts have no Delivery snapshot.  Preserve the
        # explicit unavailable inventory while still publishing the v3
        # extractor capability; a missing value must not be treated as a
        # mapping with ``.get`` semantics.
        delivery_material = context.get("delivery_material")
        declared = delivery_material.get("declared_outputs", {}) if isinstance(delivery_material, dict) else {}
        capabilities["extractors"][DELIVERY_DECLARED_OUTPUT_CATEGORY] = {
            "supported": isinstance(declared, dict) and
                         declared.get("status") in {"available", "explicit_empty"},
            "version": "delivery-declared-output.v1",
            "status": declared.get("status") if isinstance(declared, dict) else "unverified",
        }
    _expected_enumeration(context, len(ordered))
    if context["format"] == CONTEXT_V4_FORMAT:
        derivation_version = DERIVATION_V5
        denominator_format = DENOMINATOR_V4_FORMAT
    elif context["format"] == CONTEXT_V3_FORMAT:
        derivation_version = DERIVATION_V4
        denominator_format = DENOMINATOR_V3_FORMAT
    else:
        derivation_version = DERIVATION_VERSION
        denominator_format = DENOMINATOR_FORMAT
    capabilities.setdefault("derivation", {"supported": True, "version": derivation_version})
    capabilities["derivation"] = {**capabilities["derivation"], "version": derivation_version}
    extractor_versions = {"source_span": "source-span.v1",
                          "requirement": "requirement-scope.v1",
                          "child_obligation": "requirement-hierarchy.v1",
                          "artifact_responsibility": "artifact-structural.v1",
                          "task_structural": "task-structural.v1"}
    if (context["format"] in {CONTEXT_V3_FORMAT, CONTEXT_V4_FORMAT} and
            profile_has_outputs(context["capabilities"].get("selection", {}).get("profile_format"))):
        extractor_versions["delivery_declared_output"] = "delivery-declared-output.v1"
    if context["format"] == CONTEXT_V4_FORMAT:
        extractor_versions["delivery_material"] = DELIVERY_MATERIAL_EXTRACTOR
        extractor_versions["delivery_repository"] = DELIVERY_REPOSITORY_EXTRACTOR
    body = {"format": denominator_format, "project": project, "program": program,
            "stage": stage, "derivation_version": derivation_version, "input_refs": input_refs,
            "input_digest": input_digest, "obligations": ordered, "count": len(ordered),
            "unresolved": _unique_sorted(unresolved), "capabilities": capabilities,
            "extractor_versions": extractor_versions}
    # The denominator digest is the semantic identity of this complete
    # definition/projection. Actual material refs remain in ``input_refs``
    # and obligation source refs for proof resolution and history; those
    # observation envelopes must not perturb this meaning digest.
    body["digest"] = _denominator_semantic_digest(body)
    return _sealed(body, _DENOMINATOR_TOKEN)


def _validate_denominator(denominator: Any) -> dict[str, Any]:
    _verify_seal(denominator, token=_DENOMINATOR_TOKEN, name="denominator")
    _object(dict(denominator), _DENOMINATOR_KEYS, name="denominator")
    if denominator["format"] not in {DENOMINATOR_FORMAT, DENOMINATOR_V3_FORMAT, DENOMINATOR_V4_FORMAT}:
        _invalid("denominator format differs")
    v4 = denominator["format"] == DENOMINATOR_V4_FORMAT
    v3 = denominator["format"] in {DENOMINATOR_V3_FORMAT, DENOMINATOR_V4_FORMAT}
    _string(denominator["project"], "denominator.project"); _string(denominator["program"], "denominator.program")
    if denominator["stage"] not in STAGES or type(denominator["stage"]) is not str:
        _invalid("denominator.stage is invalid")
    expected_derivation = DERIVATION_V5 if v4 else DERIVATION_V4 if v3 else DERIVATION_VERSION
    if denominator["derivation_version"] != expected_derivation:
        _invalid("denominator derivation version differs")
    selection_capability = denominator.get("capabilities", {}).get("selection")
    if v4:
        profile_format = selection_capability.get("profile_format") if isinstance(selection_capability, dict) else None
        expected_registry = profile_registry(profile_format)
        if (profile_format is not None and
                (profile_format not in CANONICAL_PROFILE_FORMATS or
                 selection_capability.get("effective_relation_contract_digest") != expected_registry)):
            _integrity("v4 denominator is not bound to the selected relation registry")
    elif v3:
        if (not isinstance(selection_capability, dict) or
                not profile_has_outputs(selection_capability.get("profile_format")) or
                selection_capability.get("effective_relation_contract_digest") != REGISTRY_V2_DIGEST):
            _integrity("v3 denominator is not bound to the v2 relation registry")
    elif (isinstance(selection_capability, dict) and
          selection_capability.get("effective_relation_contract_digest") not in {None, REGISTRY_V1_DIGEST}):
        _integrity("legacy denominator is bound to an unexpected relation registry")
    required_extractors = {"source_span", "requirement", "child_obligation"}
    optional_extractors = {"artifact_responsibility", "task_structural"}
    if v3 and (not v4 or (isinstance(selection_capability, dict) and
                          profile_has_outputs(selection_capability.get("profile_format")))):
        required_extractors.add("delivery_declared_output")
    if v4:
        required_extractors.update({"delivery_material", "delivery_repository"})
    _object(denominator["extractor_versions"], required_extractors,
            optional=optional_extractors, name="denominator extractor versions")
    for name, value in denominator["extractor_versions"].items():
        _string(value, f"denominator extractor version {name}")
    if not isinstance(denominator["input_refs"], list) or not isinstance(denominator["unresolved"], list) or not isinstance(denominator["capabilities"], dict):
        _integrity("Denominator metadata has an invalid shape")
    for ref in denominator["input_refs"]:
        _typed(ref, denominator["project"], name="denominator input reference")
    if type(denominator["count"]) is not int or denominator["count"] < 0 or denominator["count"] != len(denominator["obligations"]):
        _integrity("Denominator count differs from its obligations")
    if not isinstance(denominator["obligations"], list) or len(denominator["obligations"]) > MAX_OBLIGATIONS:
        _invalid("denominator obligations are not bounded")
    ids = []
    for obligation in denominator["obligations"]:
        _validate_obligation(obligation, denominator["project"])
        if v3 and obligation.get("category") == DELIVERY_DECLARED_OUTPUT_CATEGORY:
            _typed(obligation["source_ref"], denominator["project"],
                   expected={"delivery_snapshot"}, name="declared output source")
            if not obligation["pointer"].startswith("/build_definitions/"):
                _integrity("Declared output obligation pointer is not a build definition")
        ids.append(obligation["id"])
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        _integrity("Denominator obligation ordering or duplicate identity differs")
    if _denominator_semantic_digest(dict(denominator)) != denominator["digest"]:
        _integrity("Denominator digest differs")
    enumeration = denominator["capabilities"].get("enumeration")
    if isinstance(enumeration, dict) and "obligations" in enumeration and enumeration["obligations"] != denominator["count"]:
        raise Fault("denominator_input_mismatch", "Controller obligation enumeration differs from denominator", {"expected": enumeration["obligations"], "actual": denominator["count"]})
    return denominator


def project_task(denominator: dict[str, Any], task_ref: dict[str, Any]) -> dict[str, Any]:
    """Project a complete denominator onto one exact Task revision."""
    denominator = _validate_denominator(denominator)
    project = denominator["project"]
    ref = _typed(task_ref, project, expected={"task_revision"}, name="task projection reference")
    same_task_revisions = {
        input_ref["revision"]
        for input_ref in denominator["input_refs"]
        if input_ref.get("kind") == "task_revision" and input_ref.get("task") == ref["task"]
    }
    same_task_revisions.update({
        contributor["task_ref"]["revision"]
        for obligation in denominator["obligations"]
        for contributor in obligation["contributors"]
        if contributor["task_ref"].get("task") == ref["task"]
    })
    if same_task_revisions and ref["revision"] not in same_task_revisions:
        raise Fault("stale_reference", "Task projection revision is not the denominator's current revision",
                    {"task": ref["task"], "requested": ref["revision"], "available": sorted(same_task_revisions)})
    selected = []
    unresolved = []
    for item in denominator["obligations"]:
        matches = [contributor for contributor in item["contributors"] if canonical(contributor["task_ref"]) == canonical(ref)]
        if matches:
            selected.append({"obligation_id": item["id"], "assignment_refs": _unique_sorted([x for match in matches for x in match["assignment_refs"]])})
    selected.sort(key=lambda item: item["obligation_id"])
    if not selected:
        unresolved.append(_unresolved("task_not_assigned", reason="Task has no contributor obligations", task=ref["task"]))
    for item in denominator["unresolved"]:
        details = item
        task = details.get("task") if isinstance(details, dict) else None
        if task is None or task == ref["task"]:
            unresolved.append(item)
    body = {"format": PROJECTION_FORMAT, "global_digest": denominator["digest"],
            "task_ref": ref, "obligation_ids": [item["obligation_id"] for item in selected],
            "contributor_requirements": selected, "unresolved": _unique_sorted(unresolved)}
    body["digest"] = digest(body)
    return body


def _checkpoint_plan_values(plan: Any, *, denominator: dict[str, Any],
                            checkpoint: str, relation: str, direction: str,
                            center_ref: dict[str, Any], allowed_ids: set[str],
                            name: str) -> dict[str, Any]:
    """Validate the opaque classifier result before producing a projection."""
    if (not isinstance(plan, _CheckpointPlan) or
            plan._token is not _CHECKPOINT_PLAN_TOKEN or
            plan._seal != canonical(plan._body)):
        _invalid(f"{name} requires a controller-derived checkpoint classifier plan")
    value = plan._body
    required = {"stage", "checkpoint", "relation", "direction", "center_ref",
                "population_ids", "required_now_ids", "deferred_future_ids", "schedule"}
    if set(value) != required:
        _invalid(f"{name} classifier plan shape differs")
    if (value["checkpoint"] != checkpoint or value["relation"] != relation or
            value["direction"] != direction or
            canonical(value["center_ref"]) != canonical(center_ref)):
        _invalid(f"{name} classifier identity differs from the requested projection")
    project = denominator["project"]
    if value["stage"] != denominator.get("stage"):
        _invalid(f"{name} classifier stage differs from the denominator")
    _typed(value["center_ref"], project, name=f"{name} classifier center")
    if type(value["population_ids"]) is not list or value["population_ids"] != sorted(set(value["population_ids"])):
        _invalid(f"{name} classifier population is not canonical")
    if any(type(item) is not str or not item for item in value["population_ids"]):
        _invalid(f"{name} classifier population identity is malformed")
    if not set(value["population_ids"]) <= allowed_ids:
        _invalid(f"{name} classifier population escapes the selected denominator")
    population = list(value["population_ids"])
    now, future = value["required_now_ids"], value["deferred_future_ids"]
    for items, label in ((now, "required_now_ids"), (future, "deferred_future_ids")):
        if (type(items) is not list or items != sorted(set(items)) or
                any(type(item) is not str or not item for item in items) or
                not set(items) <= set(population)):
            _invalid(f"{name} classifier {label} is not canonical")
    if set(now) | set(future) != set(population) or set(now) & set(future):
        _invalid(f"{name} classifier partition differs from its population")
    schedule = value["schedule"]
    if (type(schedule) is not list or
            schedule != sorted(schedule, key=lambda item: item.get("obligation_id", "")) or
            [item.get("obligation_id") for item in schedule] != population):
        _invalid(f"{name} classifier schedule does not cover its population")
    for item in schedule:
        if (type(item) is not dict or set(item) != {
                "obligation_id", "classification", "first_required_checkpoint",
                "producer_kind", "reason", "owner_refs"}):
            _invalid(f"{name} classifier schedule entry shape differs")
        expected = "required_now" if item["obligation_id"] in set(now) else "deferred_future"
        if item["classification"] != expected:
            _invalid(f"{name} classifier schedule partition differs")
        if (type(item["first_required_checkpoint"]) is not str or
                not item["first_required_checkpoint"] or
                type(item["producer_kind"]) is not str or
                not item["producer_kind"] or type(item["reason"]) is not str or
                not item["reason"] or type(item["owner_refs"]) is not list or
                not item["owner_refs"]):
            _invalid(f"{name} classifier schedule entry is malformed")
        for owner in item["owner_refs"]:
            _typed(owner, project, name=f"{name} classifier owner")
    return value


def project_task_checkpoint(denominator: dict[str, Any], task_ref: dict[str, Any], *,
                            checkpoint: str, relation: str, direction: str,
                            center_ref: dict[str, Any],
                            population_ids: list[str] | None = None,
                            required_now_ids: list[str] | None = None,
                            deferred_future_ids: list[str] | None = None,
                            schedule: list[dict[str, Any]] | None = None,
                            _plan: Any = None) -> dict[str, Any]:
    """Create a Task checkpoint view from the canonical controller plan.

    The legacy partition arguments remain named solely so old callers receive
    a structured ``Fault``.  They are never accepted as an authority.
    """
    denominator = _validate_denominator(denominator)
    if any(value is not None for value in
           (population_ids, required_now_ids, deferred_future_ids, schedule)):
        _invalid("checkpoint projection partitions must come from the controller classifier")
    base = project_task(denominator, task_ref)
    plan = _checkpoint_plan_values(
        _plan, denominator=denominator, checkpoint=checkpoint, relation=relation,
        direction=direction, center_ref=center_ref,
        allowed_ids=set(base["obligation_ids"]), name="Task checkpoint projection",
    )
    project = denominator["project"]
    center = _typed(center_ref, project, name="checkpoint projection center")
    body = {key: copy.deepcopy(base[key]) for key in (
        "format", "global_digest", "task_ref", "obligation_ids",
        "contributor_requirements", "unresolved")}
    body.update({"format": CHECKPOINT_PROJECTION_FORMAT,
                 "checkpoint": checkpoint, "relation": relation,
                 "direction": direction, "center_ref": center,
                 "population_ids": copy.deepcopy(plan["population_ids"]),
                 "required_now_ids": copy.deepcopy(plan["required_now_ids"]),
                 "deferred_future_ids": copy.deepcopy(plan["deferred_future_ids"]),
                 "schedule": copy.deepcopy(plan["schedule"])})
    body["digest"] = digest(body)
    return _SealedMapping(body, token=_CHECKPOINT_PROJECTION_TOKEN)


def project_global_checkpoint(denominator: dict[str, Any], *, checkpoint: str,
                             relation: str, direction: str,
                             center_ref: dict[str, Any],
                             population_ids: list[str] | None = None,
                             required_now_ids: list[str] | None = None,
                             deferred_future_ids: list[str] | None = None,
                             schedule: list[dict[str, Any]] | None = None,
                             _plan: Any = None) -> dict[str, Any]:
    """Create a global checkpoint view from the canonical controller plan."""
    denominator = _validate_denominator(denominator)
    if any(value is not None for value in
           (population_ids, required_now_ids, deferred_future_ids, schedule)):
        _invalid("global checkpoint projection partitions must come from the controller classifier")
    project = denominator["project"]
    center = _typed(center_ref, project, name="global checkpoint projection center")
    all_ids = [item["id"] for item in denominator["obligations"]]
    plan = _checkpoint_plan_values(
        _plan, denominator=denominator, checkpoint=checkpoint, relation=relation,
        direction=direction, center_ref=center,
        allowed_ids=set(all_ids), name="Global checkpoint projection",
    )
    contributor_requirements = []
    for obligation in denominator["obligations"]:
        refs = []
        for contributor in obligation.get("contributors", []):
            if isinstance(contributor, dict):
                refs.extend(contributor.get("assignment_refs", []))
        contributor_requirements.append({
            "obligation_id": obligation["id"],
            "assignment_refs": _unique_sorted(refs),
        })
    body = {
        "format": GLOBAL_CHECKPOINT_PROJECTION_FORMAT,
        "global_digest": denominator["digest"],
        "obligation_ids": all_ids,
        "contributor_requirements": contributor_requirements,
        "unresolved": copy.deepcopy(denominator["unresolved"]),
        "checkpoint": checkpoint,
        "relation": relation,
        "direction": direction,
        "center_ref": center,
        "population_ids": copy.deepcopy(plan["population_ids"]),
        "required_now_ids": copy.deepcopy(plan["required_now_ids"]),
        "deferred_future_ids": copy.deepcopy(plan["deferred_future_ids"]),
        "schedule": copy.deepcopy(plan["schedule"]),
    }
    body["digest"] = digest(body)
    return _SealedMapping(body, token=_GLOBAL_CHECKPOINT_PROJECTION_TOKEN)


def _validate_projection_fields(value: Any, *, checkpoint: bool = False) -> dict[str, Any]:
    """Validate the shared, non-scheduling projection fields."""
    # ``project_task_checkpoint`` returns the controller-sealed mapping, a
    # dict subclass carrying its private seal token.  Structural validation
    # operates on its immutable mapping view while the seal validator above
    # remains responsible for rejecting caller-created or modified values.
    _object(dict(value) if isinstance(value, _SealedMapping) else value,
            _CHECKPOINT_PROJECTION_KEYS if checkpoint else _PROJECTION_KEYS,
            name="Checkpoint projection" if checkpoint else "Task projection")
    expected = CHECKPOINT_PROJECTION_FORMAT if checkpoint else PROJECTION_FORMAT
    if value["format"] != expected:
        _invalid("projection format differs")
    _string(value["global_digest"], "projection.global_digest")
    _typed(value["task_ref"], value["task_ref"].get("project")
           if isinstance(value["task_ref"], dict) else None,
           expected={"task_revision"}, name="projection task reference")
    if (not isinstance(value["obligation_ids"], list) or
            value["obligation_ids"] != sorted(value["obligation_ids"])):
        _integrity("Projection obligation ordering differs")
    if not isinstance(value["contributor_requirements"], list):
        _integrity("Projection contributor requirements are not a list")
    return value


def _validate_checkpoint_projection(value: Any) -> dict[str, Any]:
    """Validate the controller-only checkpoint projection seal and partition."""
    if (not isinstance(value, _SealedMapping) or
            value._token is not _CHECKPOINT_PROJECTION_TOKEN):
        _invalid("checkpoint projection must come from the controller builder")
    if value._seal != canonical(dict(value)):
        raise Fault("denominator_input_mismatch", "checkpoint projection was modified")
    _validate_projection_fields(value, checkpoint=True)
    if (type(value["checkpoint"]) is not str or not value["checkpoint"] or
            type(value["relation"]) is not str or not value["relation"] or
            value["direction"] not in {"incoming", "outgoing"}):
        _invalid("checkpoint projection scheduling identity is malformed")
    _typed(value["center_ref"], value["task_ref"]["project"],
           name="checkpoint projection center")
    all_ids = set(value["obligation_ids"])
    for name in ("population_ids", "required_now_ids", "deferred_future_ids"):
        items = value[name]
        if (type(items) is not list or items != sorted(set(items)) or
                any(type(item) is not str or not item for item in items) or
                not set(items) <= all_ids):
            _invalid(f"checkpoint projection {name} is not canonical")
    population = set(value["population_ids"])
    now, future = set(value["required_now_ids"]), set(value["deferred_future_ids"])
    if now & future or now | future != population:
        _invalid("checkpoint projection partition differs from population")
    schedule = value["schedule"]
    if type(schedule) is not list or schedule != sorted(schedule, key=lambda item: item.get("obligation_id", "")):
        _invalid("checkpoint projection schedule is not canonical")
    if [item.get("obligation_id") for item in schedule] != value["population_ids"]:
        _invalid("checkpoint projection schedule population differs")
    for item in schedule:
        if type(item) is not dict or set(item) != {
                "obligation_id", "classification", "first_required_checkpoint",
                "producer_kind", "reason", "owner_refs"}:
            _invalid("checkpoint projection schedule entry shape differs")
        if item["classification"] not in {"required_now", "deferred_future"}:
            _invalid("checkpoint projection classification is unknown")
        expected_class = "required_now" if item["obligation_id"] in now else "deferred_future"
        if item["classification"] != expected_class:
            _invalid("checkpoint projection schedule classification differs")
        if (type(item["first_required_checkpoint"]) is not str or
                not item["first_required_checkpoint"] or
                type(item["producer_kind"]) is not str or
                not item["producer_kind"] or type(item["reason"]) is not str or
                not item["reason"] or type(item["owner_refs"]) is not list):
            _invalid("checkpoint projection schedule entry is malformed")
        for owner in item["owner_refs"]:
            _typed(owner, value["task_ref"]["project"], name="checkpoint projection owner")
    body = dict(value)
    body.pop("digest")
    if digest(body) != value["digest"]:
        raise Fault("integrity_error", "Checkpoint projection digest differs")
    return value


def _validate_global_checkpoint_projection(value: Any) -> dict[str, Any]:
    """Validate the controller-only program checkpoint projection seal."""
    if (not isinstance(value, _SealedMapping) or
            value._token is not _GLOBAL_CHECKPOINT_PROJECTION_TOKEN):
        _invalid("global checkpoint projection must come from the controller builder")
    if value._seal != canonical(dict(value)):
        raise Fault("denominator_input_mismatch", "global checkpoint projection was modified")
    _object(dict(value), _GLOBAL_CHECKPOINT_PROJECTION_KEYS,
            name="Global checkpoint projection")
    if value["format"] != GLOBAL_CHECKPOINT_PROJECTION_FORMAT:
        _invalid("global checkpoint projection format differs")
    _string(value["global_digest"], "global checkpoint projection.global_digest")
    ids = value["obligation_ids"]
    if (type(ids) is not list or ids != sorted(set(ids)) or
            any(type(item) is not str or not item for item in ids)):
        _invalid("global checkpoint projection obligation identities are not canonical")
    entries = value["contributor_requirements"]
    if type(entries) is not list:
        _invalid("global checkpoint projection contributor requirements are not a list")
    entry_ids = []
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"obligation_id", "assignment_refs"}:
            _invalid("global checkpoint projection contributor requirement shape differs")
        if (type(entry["obligation_id"]) is not str or
                type(entry["assignment_refs"]) is not list or
                entry["assignment_refs"] != sorted(entry["assignment_refs"], key=canonical)):
            _invalid("global checkpoint projection contributor requirement is not canonical")
        entry_ids.append(entry["obligation_id"])
    if entry_ids != ids:
        _invalid("global checkpoint projection contributor identities differ")
    _string(value["checkpoint"], "global checkpoint projection.checkpoint")
    _string(value["relation"], "global checkpoint projection.relation")
    if value["direction"] not in {"incoming", "outgoing"}:
        _invalid("global checkpoint projection direction is invalid")
    project = value["center_ref"].get("project") if isinstance(value["center_ref"], dict) else None
    _typed(value["center_ref"], project, name="global checkpoint projection center")
    all_ids = set(ids)
    for name in ("population_ids", "required_now_ids", "deferred_future_ids"):
        items = value[name]
        if (type(items) is not list or items != sorted(set(items)) or
                any(type(item) is not str or not item for item in items) or
                not set(items) <= all_ids):
            _invalid(f"global checkpoint projection {name} is not canonical")
    population = set(value["population_ids"])
    now, future = set(value["required_now_ids"]), set(value["deferred_future_ids"])
    if now & future or now | future != population:
        _invalid("global checkpoint projection partition differs from population")
    schedule = value["schedule"]
    if (type(schedule) is not list or
            schedule != sorted(schedule, key=lambda item: item.get("obligation_id", ""))):
        _invalid("global checkpoint projection schedule is not canonical")
    if [item.get("obligation_id") for item in schedule] != value["population_ids"]:
        _invalid("global checkpoint projection schedule population differs")
    for item in schedule:
        if type(item) is not dict or set(item) != {
                "obligation_id", "classification", "first_required_checkpoint",
                "producer_kind", "reason", "owner_refs"}:
            _invalid("global checkpoint projection schedule entry shape differs")
        expected_class = "required_now" if item["obligation_id"] in now else "deferred_future"
        if item["classification"] != expected_class:
            _invalid("global checkpoint projection schedule classification differs")
        if (item["classification"] not in {"required_now", "deferred_future"} or
                type(item["first_required_checkpoint"]) is not str or
                not item["first_required_checkpoint"] or
                type(item["producer_kind"]) is not str or
                not item["producer_kind"] or type(item["reason"]) is not str or
                not item["reason"] or type(item["owner_refs"]) is not list):
            _invalid("global checkpoint projection schedule entry is malformed")
        for owner in item["owner_refs"]:
            _typed(owner, project, name="global checkpoint projection owner")
    body = dict(value)
    body.pop("digest")
    if digest(body) != value["digest"]:
        raise Fault("integrity_error", "Global checkpoint projection digest differs")
    return value


def _validate_projection(projection: Any) -> dict[str, Any]:
    if (isinstance(projection, dict) and
            projection.get("format") == GLOBAL_CHECKPOINT_PROJECTION_FORMAT):
        return _validate_global_checkpoint_projection(projection)
    if isinstance(projection, dict) and projection.get("format") == CHECKPOINT_PROJECTION_FORMAT:
        return _validate_checkpoint_projection(projection)
    _validate_projection_fields(projection)
    computed = dict(projection); computed.pop("digest")
    if digest(computed) != projection["digest"]:
        _integrity("Task projection digest differs")
    return _copy_json(projection, "Task projection")


def page_obligations(denominator: dict[str, Any], *, offset: int = 0, limit: int = 100,
                     expected_digest: str | None = None) -> dict[str, Any]:
    """Return a bounded immutable page tied to the full denominator digest."""
    denominator = _validate_denominator(denominator)
    if type(offset) is not int or offset < 0:
        raise Fault("invalid_range", "offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise Fault("invalid_range", f"limit must be between 1 and {MAX_PAGE}")
    if offset > 0 and (type(expected_digest) is not str or expected_digest != denominator["digest"]):
        raise Fault("stale_page", "Continuation page must name the current denominator digest", {"expected": denominator["digest"], "actual": expected_digest})
    if expected_digest is not None and expected_digest != denominator["digest"]:
        raise Fault("stale_page", "Page digest differs from denominator", {"expected": denominator["digest"], "actual": expected_digest})
    values = denominator["obligations"][offset:offset + limit]
    next_offset = offset + len(values) if offset + len(values) < denominator["count"] else None
    return {"format": PAGE_FORMAT, "offset": offset, "limit": limit,
            "total": denominator["count"], "items": _copy_json(values),
            "next_offset": next_offset, "digest": denominator["digest"]}


# Unit 2b owns node receipt reuse and criterion evaluation.  The names remain
# deliberately unavailable here instead of returning a fake PASS.
def select_node_reviews(*args: Any, **kwargs: Any) -> Any:
    raise Fault("unsupported", "Unit2b node review selection is not implemented in Unit2a")


def evaluate_criteria(*args: Any, **kwargs: Any) -> Any:
    raise Fault("unsupported", "Unit2b criterion evaluation is not implemented in Unit2a")
