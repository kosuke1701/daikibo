"""Closed additive schemas for explicit structural obligations.

Unit2c already derives source and hierarchy denominators from controller-owned
material.  This module adds the small, declarative A/B boundary used by that
extractor.  It intentionally contains no review or adoption decision: the
validator proves shape and exact references, while the denominator collector
proves the referenced historical material and derives immutable identities.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from .assurance_relations import validate_typed_ref
from .common import Fault, canonical, digest, text


ARTIFACT_STRUCTURAL_FORMAT = "daikibo.structural-obligations.v1"
TASK_STRUCTURAL_FORMAT = "daikibo.task-structural-obligations.v1"
ARTIFACT_STRUCTURAL_KINDS = frozenset({"domain", "design", "component", "interface"})
STRUCTURAL_MAX_ITEMS = 10_000
STRUCTURAL_MAX_REFS = 200
STRUCTURAL_MAX_STATEMENT = 10_000
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


def _fail(message: str, details: Any = None, code: str = "invalid_input") -> None:
    raise Fault(code, message, details)


def _obj(value: Any, required: set[str], *, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{name} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        _fail(f"{name} is missing fields", missing)
    if extra:
        _fail(f"{name} has unknown fields", extra)
    return value


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        _fail(f"{name} must match [A-Za-z0-9_.-]{{1,200}}")
    return value


def _statement(value: Any, name: str) -> str:
    try:
        return text(value, name, STRUCTURAL_MAX_STATEMENT)
    except Fault:
        raise


def _sha(value: Any, name: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        _fail(f"{name} must be a lowercase SHA-256")
    return value


def _index(value: Any, name: str) -> int:
    # ``bool`` is an ``int`` subclass; exact type is part of the wire contract.
    if type(value) is not int or value < 0:
        _fail(f"{name} must be an integer >= 0")
    return value


def _project(ref: dict[str, Any], project: str | None) -> None:
    if project is not None:
        if ref.get("project") != project:
            raise Fault("cross_project", "Structural reference belongs to another project", ref.get("project"))


def _artifact_ref(ref: Any, *, project: str | None, name: str) -> dict[str, Any]:
    if type(ref) is not dict:
        _fail(f"{name} must be a typed artifact reference")
    try:
        validate_typed_ref(ref, project=project, expected_kinds={"artifact"})
    except Fault:
        raise
    # validate_typed_ref already rejects unknown/extra fields.  Copying here
    # ensures callers cannot mutate a validated declaration through an alias.
    return copy.deepcopy(ref)


def _refs(value: Any, *, project: str | None, read_artifacts: set[str] | None, name: str) -> list[dict[str, Any]]:
    if type(value) is not list or not (1 <= len(value) <= STRUCTURAL_MAX_REFS):
        _fail(f"{name} must contain 1..{STRUCTURAL_MAX_REFS} artifact references")
    result = []
    identities = set()
    artifact_ids = set()
    for index, ref in enumerate(value):
        normalized = _artifact_ref(ref, project=project, name=f"{name}[{index}]")
        identity = canonical(normalized)
        if identity in identities or normalized["artifact"] in artifact_ids:
            _fail(f"{name} contains duplicate references", index)
        identities.add(identity)
        artifact_ids.add(normalized["artifact"])
        if read_artifacts is not None and normalized["artifact"] not in read_artifacts:
            raise Fault("invalid_reference", "Structural Task reference is not in read_artifacts", normalized["artifact"])
        result.append(normalized)
    return result


def _domain_item(value: Any, *, project: str | None, resolver: Callable[[dict[str, Any]], dict[str, Any]] | None,
                 index: int) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("structural responsibility must be an object", index)
    kind = value.get("type")
    if kind == "statement":
        item = _obj(value, {"id", "type", "statement"}, name="structural statement")
        _identifier(item["id"], "structural statement id")
        _statement(item["statement"], "structural statement statement")
        return copy.deepcopy(item)
    if kind == "domain_reference":
        item = _obj(value, {"id", "type", "domain", "responsibility_index", "responsibility_digest"},
                    name="structural domain reference")
        _identifier(item["id"], "structural domain reference id")
        ref = _artifact_ref(item["domain"], project=project, name="structural domain reference domain")
        index_value = _index(item["responsibility_index"], "responsibility_index")
        digest_value = _sha(item["responsibility_digest"], "responsibility_digest")
        if resolver is not None:
            resolved = resolver(ref)
            if type(resolved) is not dict:
                _fail("domain reference resolver did not return an artifact", code="integrity_error")
            if resolved.get("project") != ref["project"]:
                raise Fault("cross_project", "Resolved domain reference belongs to another project")
            if resolved.get("kind") != "domain":
                _fail("domain reference does not resolve to a domain artifact", ref["artifact"], "invalid_reference")
            body = resolved.get("body")
            if type(body) is not dict or type(body.get("responsibilities")) is not list:
                _fail("domain reference has no canonical responsibilities", ref["artifact"], "integrity_error")
            if index_value >= len(body["responsibilities"]):
                _fail("domain responsibility index is outside the pinned historical body", index_value, "invalid_reference")
            expected = digest(body["responsibilities"][index_value])
            if expected != digest_value:
                _fail("domain responsibility digest differs from the pinned historical body", ref["artifact"], "stale_reference")
            if resolved.get("digest") != ref["body_digest"]:
                _fail("domain reference body digest differs", ref["artifact"], "stale_reference")
        item = copy.deepcopy(item)
        item["domain"] = ref
        return item
    _fail("Unknown structural responsibility type", kind)


def validate_artifact_structural_obligations(value: Any = _MISSING, *, kind: str | None = None,
                                             project: str | None = None,
                                             resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Validate A's exact object and return an isolated normalized value.

    The private missing sentinel means the optional field was absent and is
    retained as a distinct legacy state by :func:`artifact_structural_metadata`.
    An explicit JSON ``null`` is invalid.  A resolver is supplied by
    Knowledge/denominator collection when the actual historical domain pin
    must be checked.
    """
    if value is _MISSING:
        return {"status": "legacy_unavailable", "items": []}
    if value is None:
        _fail("artifact structural_obligations must be an object when present")
    if kind is not None and kind not in ARTIFACT_STRUCTURAL_KINDS:
        _fail("structural_obligations is not supported for this artifact kind", kind)
    container = _obj(value, {"format", "responsibilities"}, name="artifact structural_obligations")
    if container["format"] != ARTIFACT_STRUCTURAL_FORMAT:
        _fail("artifact structural_obligations format differs")
    items = container["responsibilities"]
    if type(items) is not list or len(items) > STRUCTURAL_MAX_ITEMS:
        _fail("artifact structural responsibilities exceed the bounded list")
    result = []
    ids = set()
    for index, item in enumerate(items):
        normalized = _domain_item(item, project=project, resolver=resolver, index=index)
        if normalized["id"] in ids:
            _fail("artifact structural responsibility id is duplicated", normalized["id"])
        ids.add(normalized["id"])
        result.append(normalized)
    return {"status": "explicit_empty" if not result else "declared", "items": result}


def artifact_structural_metadata(body: dict[str, Any], *, kind: str | None = None,
                                 project: str | None = None,
                                 resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return bounded A metadata while keeping missing and empty distinct."""
    if type(body) is not dict:
        _fail("artifact body must be an object")
    if "structural_obligations" not in body:
        return {"status": "legacy_unavailable", "items": []}
    raw = body["structural_obligations"]
    if raw is None:
        return {"status": "invalid", "items": [], "reason": "explicit_null"}
    validated = validate_artifact_structural_obligations(raw, kind=kind, project=project, resolver=resolver)
    return {"status": validated["status"], "items": validated["items"]}


def _task_item(value: Any, *, output: bool, project: str | None,
               read_artifacts: set[str] | None) -> dict[str, Any]:
    required = {"id", "statement", "artifact_refs", "realization_kind"} if output else {"id", "statement", "artifact_refs"}
    item = _obj(value, required, name="required output" if output else "required exercise")
    _identifier(item["id"], ("required output" if output else "required exercise") + " id")
    _statement(item["statement"], ("required output" if output else "required exercise") + " statement")
    refs = _refs(item["artifact_refs"], project=project, read_artifacts=read_artifacts,
                 name=("required output" if output else "required exercise") + " artifact_refs")
    if output:
        realization_kind = item["realization_kind"]
        if type(realization_kind) is not str:
            _fail("required output realization_kind must be a string")
        if realization_kind not in {"candidate_member", "artifact"}:
            _fail("required output realization_kind is unknown", realization_kind)
    result = copy.deepcopy(item)
    result["artifact_refs"] = refs
    return result


def validate_task_structural_obligations(value: Any = _MISSING, *, project: str | None = None,
                                         read_artifacts: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Validate B's exact optional Task declaration."""
    if value is _MISSING:
        return {"status": "legacy_unavailable", "required_outputs": [], "required_exercises": []}
    if value is None:
        _fail("Task structural_obligations must be an object when present")
    container = _obj(value, {"format", "required_outputs", "required_exercises"}, name="Task structural_obligations")
    if container["format"] != TASK_STRUCTURAL_FORMAT:
        _fail("Task structural_obligations format differs")
    allowed = None if read_artifacts is None else set(read_artifacts)
    if allowed is not None and any(type(item) is not str for item in allowed):
        _fail("Task read_artifacts contains an invalid artifact ID")
    outputs = container["required_outputs"]
    exercises = container["required_exercises"]
    if type(outputs) is not list or len(outputs) > STRUCTURAL_MAX_ITEMS:
        _fail("required_outputs exceeds the bounded list")
    if type(exercises) is not list or len(exercises) > STRUCTURAL_MAX_ITEMS:
        _fail("required_exercises exceeds the bounded list")
    result_outputs = []
    result_exercises = []
    output_ids = set()
    exercise_ids = set()
    for index, item in enumerate(outputs):
        normalized = _task_item(item, output=True, project=project, read_artifacts=allowed)
        if normalized["id"] in output_ids:
            _fail("required output id is duplicated", normalized["id"])
        output_ids.add(normalized["id"])
        result_outputs.append(normalized)
    for index, item in enumerate(exercises):
        normalized = _task_item(item, output=False, project=project, read_artifacts=allowed)
        if normalized["id"] in exercise_ids:
            _fail("required exercise id is duplicated", normalized["id"])
        exercise_ids.add(normalized["id"])
        result_exercises.append(normalized)
    status = "explicit_empty" if not result_outputs and not result_exercises else "declared"
    return {"status": status, "required_outputs": result_outputs, "required_exercises": result_exercises}


def task_structural_metadata(body: dict[str, Any], *, project: str | None = None) -> dict[str, Any]:
    if type(body) is not dict:
        _fail("Task body must be an object")
    if "structural_obligations" not in body:
        return {"status": "legacy_unavailable", "required_outputs": [], "required_exercises": []}
    raw = body["structural_obligations"]
    if raw is None:
        return {"status": "invalid", "required_outputs": [], "required_exercises": [],
                "reason": "explicit_null"}
    return validate_task_structural_obligations(raw, project=project,
                                                read_artifacts=body.get("read_artifacts"))


def structural_contract() -> dict[str, Any]:
    """Public, detached metadata for api.describe and Skill guidance."""
    return {
        "artifact": {
            "field": "structural_obligations",
            "supported_kinds": sorted(ARTIFACT_STRUCTURAL_KINDS),
            "format": ARTIFACT_STRUCTURAL_FORMAT,
            "required": ["format", "responsibilities"],
            "responsibility": {
                "statement": ["id", "type", "statement"],
                "domain_reference": ["id", "type", "domain", "responsibility_index", "responsibility_digest"],
                "types": ["statement", "domain_reference"],
            },
            "limits": {"responsibilities": STRUCTURAL_MAX_ITEMS, "statement": STRUCTURAL_MAX_STATEMENT},
            "missing": "legacy_unavailable",
            "empty": "explicit_empty",
            "null": "invalid_input",
            "domain_pin": "artifact kind=domain; exact historical revision/body_digest and responsibility digest",
        },
        "task": {
            "field": "structural_obligations",
            "format": TASK_STRUCTURAL_FORMAT,
            "required": ["format", "required_outputs", "required_exercises"],
            "required_output": ["id", "statement", "artifact_refs", "realization_kind"],
            "required_exercise": ["id", "statement", "artifact_refs"],
            "realization_kinds": ["candidate_member", "artifact"],
            "limits": {"items": STRUCTURAL_MAX_ITEMS, "artifact_refs": STRUCTURAL_MAX_REFS,
                        "statement": STRUCTURAL_MAX_STATEMENT},
            "read_artifacts": "Every declared artifact_ref must name an actual Task read_artifacts ID",
            "missing": "legacy_unavailable",
            "empty": "explicit_empty",
            "null": "invalid_input",
        },
        "denominator": {
            "semantic": "Declaration body changes alter the Task/artifact definition and denominator input digest",
            "telemetry": "status, receipt counts, and other observation-only fields do not alter the input digest",
            "inference": "Never infer obligations from write_paths, future candidates, or producer discovery",
        },
    }


def task_definition_contract() -> dict[str, Any]:
    return {
        "required": ["title", "goal", "read_artifacts", "write_paths", "acceptance", "dependencies", "repos", "non_goals"],
        "optional": ["risk", "resource_writes", "resource_reads", "max_attempts", "timeout", "auxiliary_criteria",
                     "maintenance_reason", "phase", "workflow_id", "origin_change", "acceptance_refs",
                     "structural_obligations"],
        "structural_obligations": structural_contract()["task"],
        "revision_identity": "SHA-256 of the complete canonical Task body, including structural_obligations when present",
        "missing_vs_empty": {"missing": "legacy_unavailable", "empty": "explicit_empty",
                              "null": "invalid_input"},
    }
