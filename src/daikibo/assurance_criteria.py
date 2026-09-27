"""Finite, controller backed criterion evaluation for E3 Unit 2b.

The public function in this module is deliberately small.  The important
boundary is inside it: an edge supplied by a caller is only evidence after it
has been matched to the immutable edge row and its stored body has been
resolved through the controller's typed-reference resolver.  In particular,
``obligation_ids`` is a claim made by an edge; it is never an authority for
which denominator member that edge covers.

This module does not add a dispatcher operation or a new persistence type.
It uses the sealed Unit 2a denominator and Unit 2b node-review result and
keeps criterion families without a finite controller source unsupported.
"""
from __future__ import annotations

from .assurance_profile_contract import (CANONICAL_PROFILE_FORMATS, profile_registry, profile_has_outputs)

import copy
from typing import Any

from .assurance import SET_UNIVERSAL_CRITERIA
from .artifact_provenance import (
    resolve_artifact_production_material,
    resolve_produced_artifact,
)
from .assurance_denominators import (
    CONTEXT_FORMAT,
    CONTEXT_V3_FORMAT,
    CONTEXT_V4_FORMAT,
    DELIVERY_DECLARED_OUTPUT_CATEGORY,
    DENOMINATOR_FORMAT,
    DENOMINATOR_V3_FORMAT,
    DENOMINATOR_V4_FORMAT,
    _validate_context,
    _validate_denominator,
    _validate_projection,
    CHECKPOINT_PROJECTION_FORMAT,
    GLOBAL_CHECKPOINT_PROJECTION_FORMAT,
)
from .assurance_delivery import delivery_declaration_owner_matches
from .assurance_node_reviews import validated_reviews_origin
from .assurance_relations import (
    REGISTRY_DIGEST,
    REGISTRY_V1_DIGEST,
    REGISTRY_V2_DIGEST,
    registry_entry,
    semantic_kind,
    validate_relation,
    validate_typed_ref,
)
from .common import Fault, canonical, digest, need, parse_json
from .execution_record import execution_record_consistency
from .observed_receipts import ordered_observed_receipts
from .task_revisions import task_definition_digest
from .verification_materials import validate_test_plan_definition_identity


CRITERION_STATUSES = frozenset(
    {"satisfied", "missing", "stale", "failed", "unverified", "unsupported"}
)
_UNSUPPORTED_PREFIXES = frozenset({
    "all_requirements", "all_child_obligations", "all_responsibilities",
    "all_required_paths", "all_declared_outputs", "all_required_outputs",
    "all_impacted_targets", "all_source_spans",
})
_EDGE_FIELDS = {
    "format", "project", "source_ref", "target_ref", "relation",
    "relation_contract_digest", "scope_ref", "claim", "obligation_ids",
    "required_evidence_refs", "authority_refs", "supersedes_ref",
}
_DERIVED_REF_FIELDS = {"identity_digest", "semantic_kind"}
_EDGE_ORIGIN = object()
_REQUEST_ORIGIN = object()
_RELATION_REVIEWS_ORIGIN = object()

_REQUEST_FORMAT = "assurance.relation-request.v1"
_RELATION_REVIEWS_FORMAT = "assurance.relation-reviews.v1"
_NEW_CATEGORIES = frozenset({
    "artifact_responsibility", "artifact_structural_responsibility",
    "required_output", "required_exercise", "source_span", "requirement",
    "child_obligation", "impacted_target", DELIVERY_DECLARED_OUTPUT_CATEGORY,
})
_RELATION_CATEGORY_MAP = {
    "extracted_from": {"source_span"},
    "decomposes": {"requirement", "child_obligation"},
    "realizes": {"acceptance_condition"},
    "implements": {"artifact_responsibility", "artifact_structural_responsibility"},
    "verifies": {"acceptance_condition"},
    "exercises": {"required_exercise"},
    "execution_of": {"required_check", "delivery_check"},
    "assigned_to": {"requirement"},
    "produced_by": {"required_output"},
    "migrated_to": {"population_leaf"},
    "depends_on": set(),
    "affects": {"impacted_target"},
    "contains": {"required_output"},
}
_EXTRACTOR_CATEGORY = {
    "source_span": "source_span",
    "requirement": "requirement",
    "child_obligation": "child_obligation",
    "artifact_responsibility": "artifact_responsibility",
    "artifact_structural_responsibility": "artifact_responsibility",
    "required_output": "task_structural",
    "required_exercise": "task_structural",
    DELIVERY_DECLARED_OUTPUT_CATEGORY: DELIVERY_DECLARED_OUTPUT_CATEGORY,
}
_CRITERION_CATEGORIES = {
    "all_source_spans": {"source_span"},
    "all_child_obligations": {"child_obligation"},
    "all_responsibilities": {"artifact_responsibility", "artifact_structural_responsibility"},
    "all_required_paths": {"required_exercise"},
    "all_declared_outputs": {"required_output"},
    "all_required_outputs": {"required_output"},
    "all_impacted_targets": {"impacted_target"},
    "all_requirements": {"requirement"},
}


_TASK_OUTPUT_CENTER_KINDS = frozenset({
    "candidate", "candidate_symbol", "artifact", "task_revision",
})
_DELIVERY_OUTPUT_CENTER_KINDS = frozenset({
    "delivery_snapshot", "actual_delivery_commit", "delivery_check",
    "output_artifact",
})
_TASK_PRODUCER_KINDS = frozenset({"candidate", "candidate_symbol", "artifact"})
_DELIVERY_CONTAINED_KINDS = frozenset({
    "candidate", "candidate_symbol", "traceability_ref", "git_file",
    "git_symbol", "artifact",
})


def _delivery_context_has_inventory(context: dict[str, Any] | None) -> bool:
    """Return whether a sealed context carries the v3 Delivery inventory.

    A Delivery snapshot is also a valid center for the historical Task
    ``contains`` relation.  The context's controller-derived declaration
    inventory is the boundary that distinguishes that path from the v2
    output endpoint; a caller cannot select this mode with a free-form
    category or an empty dictionary.
    """
    if not isinstance(context, dict):
        return False
    selection = context.get("capabilities", {}).get("selection", {})
    if not isinstance(selection, dict) or not profile_has_outputs(selection.get("profile_format")):
        return False
    material = context.get("delivery_material")
    if not isinstance(material, dict):
        return False
    return isinstance(material.get("declared_outputs"), dict)


def _endpoint_family(relation: str, source_ref: dict[str, Any] | None,
                     target_ref: dict[str, Any] | None) -> str | None:
    """Classify a v2 output edge from its verified typed endpoint pair.

    Registry v2 deliberately keeps the old Task endpoint pairs and adds a
    separate Delivery pair.  The pair, rather than the registry version
    alone, is the immutable family discriminator.
    """
    if not isinstance(source_ref, dict) or not isinstance(target_ref, dict):
        return None
    try:
        source_kind, target_kind = semantic_kind(source_ref), semantic_kind(target_ref)
    except (KeyError, TypeError):
        return None
    if relation == "produced_by":
        if source_kind == "output_artifact" and target_kind == "delivery_check":
            return "delivery"
        if source_kind in _TASK_PRODUCER_KINDS and target_kind == "task_revision":
            return "task"
        return None
    if relation == "contains":
        if source_kind not in {"delivery_snapshot", "actual_delivery_commit"}:
            return None
        if target_kind == "output_artifact":
            return "delivery"
        if target_kind in _DELIVERY_CONTAINED_KINDS:
            return "task"
    return None


def _relation_family(relation: str, registry_digest: str,
                     *, center_ref: dict[str, Any] | None = None,
                     context: dict[str, Any] | None = None,
                     source_ref: dict[str, Any] | None = None,
                     target_ref: dict[str, Any] | None = None) -> str | None:
    """Resolve the closed Task/Delivery family for an output relation.

    ``None`` is intentional: an untyped or conflicting family must remain
    unresolved.  In particular, this helper never returns a union of Task and
    Delivery populations merely because both are present in a v2 denominator.
    """
    if registry_digest != REGISTRY_V2_DIGEST or relation not in {"produced_by", "contains"}:
        return "task"

    endpoint_family = _endpoint_family(relation, source_ref, target_ref)
    center_family: str | None = None
    try:
        center_kind = semantic_kind(center_ref) if isinstance(center_ref, dict) else None
    except (KeyError, TypeError):
        center_kind = None

    if relation == "produced_by":
        if center_kind in _DELIVERY_OUTPUT_CENTER_KINDS:
            center_family = "delivery"
        elif center_kind in _TASK_OUTPUT_CENTER_KINDS:
            center_family = "task"
    else:  # contains: the target endpoint distinguishes the two v2 wires.
        if center_kind == "output_artifact" or center_kind == "delivery_check":
            center_family = "delivery"
        elif center_kind in {"candidate", "candidate_symbol", "artifact"}:
            center_family = "task"
        elif center_kind in {"delivery_snapshot", "actual_delivery_commit"}:
            center_family = "delivery" if _delivery_context_has_inventory(context) else "task"

    if endpoint_family is not None and center_family is not None and endpoint_family != center_family:
        return None
    return endpoint_family or center_family


def _relation_categories(relation: str, registry_digest: str = REGISTRY_V1_DIGEST,
                         *, center_ref: dict[str, Any] | None = None,
                         context: dict[str, Any] | None = None,
                         source_ref: dict[str, Any] | None = None,
                         target_ref: dict[str, Any] | None = None) -> set[str]:
    """Return the one denominator family selected by a relation wire.

    v2 carries both the historical Task output pair and the additive
    Delivery output pair.  Selection is made from the verified typed center,
    endpoint pair, and (for an ambiguous Delivery snapshot center) the sealed
    controller context.  No caller can obtain both populations by omitting a
    discriminator.
    """
    if registry_digest == REGISTRY_V2_DIGEST and relation in {"produced_by", "contains"}:
        family = _relation_family(
            relation, registry_digest, center_ref=center_ref, context=context,
            source_ref=source_ref, target_ref=target_ref,
        )
        if family == "delivery":
            return {DELIVERY_DECLARED_OUTPUT_CATEGORY}
        if family == "task":
            return {"required_output"}
        return set()
    return set(_RELATION_CATEGORY_MAP[relation])


def _criterion_categories(name: str, registry_digest: str = REGISTRY_V1_DIGEST,
                          *, relation: str | None = None,
                          center_ref: dict[str, Any] | None = None,
                          context: dict[str, Any] | None = None) -> set[str]:
    categories = set(_CRITERION_CATEGORIES[name])
    if (registry_digest == REGISTRY_V2_DIGEST and
            name in {"all_declared_outputs", "all_required_outputs"}):
        if relation not in {"produced_by", "contains"}:
            return set()
        selected = _relation_categories(
            relation, registry_digest, center_ref=center_ref, context=context,
        )
        return selected
    return categories

# The request center is one side of a relation, while the selected scope is
# the independent population on the other side.  Keep both choices explicit:
# a category match alone is not an owner match.  The names below are closed
# dispatch modes, rather than a fallback that silently accepts every row.
_CENTER_OWNER_RULES = {
    "extracted_from": {"incoming": "anchor_member", "outgoing": "source_owner"},
    "decomposes": {"incoming": "anchor_member", "outgoing": "decomposes_child"},
    "realizes": {"incoming": "anchor_member", "outgoing": "scope_population"},
    "implements": {"incoming": "anchor_member", "outgoing": "scope_population"},
    "verifies": {"incoming": "anchor_member", "outgoing": "scope_population"},
    "exercises": {"incoming": "declared_exercise", "outgoing": "scope_population"},
    "execution_of": {"incoming": "anchor_member", "outgoing": "scope_population"},
    "assigned_to": {"incoming": "contributor", "outgoing": "anchor_member"},
    "produced_by": {"incoming": "anchor_member", "outgoing": "contributor"},
    "migrated_to": {"incoming": "contributor", "outgoing": "scope_population"},
    "depends_on": {"incoming": "edge_endpoint", "outgoing": "edge_endpoint"},
    "affects": {"incoming": "anchor_member", "outgoing": "impact_owner"},
    "contains": {"incoming": "declared_output", "outgoing": "scope_population"},
}


def _criterion(status: str, required: list[str] | None = None,
               observed: list[str] | None = None, missing: list[str] | None = None,
               evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if status not in CRITERION_STATUSES:
        raise AssertionError(status)
    return {
        "status": status,
        "required_ids": sorted(required or []),
        "observed_ids": sorted(observed or []),
        "missing_ids": sorted(missing or []),
        "evidence_refs": list(evidence or [])[:1000],
    }


def _unresolved(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "required_ids": [], "observed_ids": [],
            "missing_ids": [], "evidence_refs": []}


def _plain_json(value: Any) -> Any:
    """Remove resolver-only projections before comparing stored wire data."""
    if isinstance(value, dict):
        return {key: _plain_json(item) for key, item in value.items()
                if key not in _DERIVED_REF_FIELDS}
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


def _same_ref(left: Any, right: Any) -> bool:
    return canonical(_plain_json(left)) == canonical(_plain_json(right))


def _json_body(value: Any, name: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = parse_json(value)
    if type(value) is not dict:
        raise Fault("integrity_error", f"{name} is not an object")
    return value


def _identity(value: Any) -> Any:
    """Drop resolver-only projections before comparing exact wire identities."""
    if isinstance(value, dict):
        return {key: _identity(item) for key, item in value.items()
                if key not in _DERIVED_REF_FIELDS}
    if isinstance(value, list):
        return [_identity(item) for item in value]
    return value


def _assurance(control: Any) -> Any:
    value = getattr(control, "assurance", None)
    if value is None:
        raise Fault("unsupported", "Assurance controller is unavailable")
    return value


def _resolution_content(resolved: dict[str, Any]) -> Any:
    """Extract canonical content from all existing resolver envelopes."""
    inner = resolved.get("resolution") if isinstance(resolved, dict) else None
    if not isinstance(inner, dict):
        return None
    if isinstance(inner.get("content"), dict):
        return inner["content"]
    result = inner.get("result")
    if isinstance(result, dict) and isinstance(result.get("content"), dict):
        return result["content"]
    if isinstance(inner.get("object"), dict):
        return inner["object"]
    if isinstance(inner.get("payload"), dict):
        return inner["payload"]
    return None


def _resolve_content(control: Any, actor: Any, ref: dict[str, Any], *, current: bool = False) -> Any:
    assurance = _assurance(control)
    if current and hasattr(assurance, "evaluate_current"):
        resolved = assurance.evaluate_current(actor, _plain_json(ref))
        state = (resolved.get("current") or {}).get("state") if isinstance(resolved, dict) else None
        if state != "current":
            raise Fault("stale_reference" if state == "stale" else "unresolved_reference",
                        "Typed endpoint is not current", ref)
    else:
        resolved = assurance.resolve_pinned(actor, _plain_json(ref))
    content = _resolution_content(resolved)
    if not isinstance(content, dict):
        raise Fault("unresolved_reference", "Typed endpoint has no canonical content", ref)
    return content


def _resolve_endpoint_current(control: Any, actor: Any, ref: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        _resolve_content(control, actor, ref, current=True)
        return True, None
    except Fault as exc:
        # Keep the resolver's exact fault code in the request diagnostic; the
        # public path still maps it to stale/unresolved at the caller boundary.
        return False, exc.code


def _source_ref_for_artifact(control: Any, project: str, body: dict[str, Any], source_id: str) -> dict[str, Any] | None:
    row = control.s.one("SELECT id,blob,project FROM sources WHERE id=? AND project=?", (source_id, project))
    if row is None or source_id not in body.get("source_refs", []):
        return None
    return {"kind": "source", "project": project, "source": row["id"], "blob_digest": row["blob"]}


def _artifact_from_ref(ref: dict[str, Any], project: str) -> dict[str, Any] | None:
    if ref.get("kind") == "artifact":
        return ref
    if ref.get("kind") == "traceability_ref" and ref.get("locator", {}).get("ref_type") == "artifact_ac":
        locator = ref["locator"]
        return {"kind": "artifact", "project": project,
                "artifact": locator["artifact"], "revision": locator["revision"],
                "body_digest": locator["body_digest"]}
    return None


def _artifact_kind(control: Any, ref: dict[str, Any]) -> str | None:
    """Read the canonical Knowledge row kind for an artifact endpoint."""
    if not isinstance(ref, dict) or ref.get("kind") != "artifact":
        return None
    row = control.s.one("SELECT kind FROM artifacts WHERE id=? AND project=?",
                        (ref.get("artifact"), ref.get("project")))
    return row.get("kind") if row is not None else None


def _artifact_production_material(control: Any, actor: Any,
                                  artifact_ref: dict[str, Any],
                                  task_ref: dict[str, Any]) -> dict[str, Any]:
    """Resolve one controller-owned P production material for a produced edge.

    Knowledge output artifacts remain draft until a separate meaning
    workflow accepts them.  The P material is therefore the only provenance
    authority for this produced-by endpoint; this reader validates its full
    envelope, candidate/runtime chain, manifest declaration, artifact body,
    and dependency list before M can mark the edge mechanically eligible.
    """
    project = artifact_ref.get("project")
    assurance = _assurance(control)
    return resolve_artifact_production_material(
        control.s, project=project, artifact_ref=artifact_ref,
        task_ref=task_ref, context=assurance._candidate_context,
        resolve_artifact=lambda ref: resolve_produced_artifact(
            control.s, ref, project=project, code="integrity_error", current=True,
        ),
        blob_get=control.s.blob_get, code="integrity_error",
        missing_code="artifact_producer_material_missing", current=True,
    )


def _obligation_task_ids(obligation: dict[str, Any]) -> set[str]:
    result = set()
    for item in obligation.get("contributors", []):
        if isinstance(item, dict) and isinstance(item.get("task_ref"), dict):
            task = item["task_ref"].get("task")
            if isinstance(task, str):
                result.add(task)
    return result


def _task_structural_item(control: Any, actor: Any, obligation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the exact required_output/required_exercise declaration item."""
    task_ref = obligation.get("source_ref")
    if not isinstance(task_ref, dict) or task_ref.get("kind") != "task_revision":
        raise Fault("invalid_obligation", "Structural obligation has no Task revision source")
    content = _resolve_content(control, actor, task_ref, current=False)
    if not isinstance(content, dict):
        raise Fault("unresolved_reference", "Task declaration body is unavailable")
    pointer = obligation.get("pointer")
    if not isinstance(pointer, str):
        raise Fault("integrity_error", "Structural obligation pointer is missing")
    parts = pointer.split("/")
    # /tasks/<id>/structural_obligations/<required_outputs|required_exercises>/<index>
    if len(parts) != 6 or parts[1] != "tasks" or parts[3] != "structural_obligations":
        raise Fault("integrity_error", "Structural obligation pointer has an unsupported shape")
    collection, raw_index = parts[4], parts[5]
    if collection not in {"required_outputs", "required_exercises"} or not raw_index.isdigit():
        raise Fault("integrity_error", "Structural obligation pointer has an invalid collection")
    structural = content.get("structural_obligations")
    if not isinstance(structural, dict):
        raise Fault("unresolved_reference", "Task structural declaration is missing")
    values = structural.get(collection)
    index = int(raw_index)
    if not isinstance(values, list) or index >= len(values) or not isinstance(values[index], dict):
        raise Fault("unresolved_reference", "Task structural declaration item is missing")
    item = values[index]
    if digest(item) != obligation.get("value_digest"):
        raise Fault("integrity_error", "Task structural obligation value digest differs")
    return task_ref, item


def _pointer_value(body: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise Fault("integrity_error", "Typed obligation pointer is malformed")
    value = body
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(token)] if isinstance(value, list) else value[token]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise Fault("unresolved_reference", "Typed obligation pointer is missing") from exc
    return value


def _change_target_in_context(context: dict[str, Any], source_ref: dict[str, Any], target_ref: dict[str, Any]) -> bool:
    """Check an affects target against the retained baseline/current impact set."""
    impact = context.get("impact_inventory")
    if not isinstance(impact, dict):
        return False
    for change in impact.get("changes", []):
        change_ref = change.get("change_ref") if isinstance(change, dict) else None
        if not isinstance(change_ref, dict) or not _same_ref(change_ref, source_ref):
            continue
        wanted = _identity(target_ref)
        for side in ("baseline", "current"):
            snapshot = change.get(side, {})
            for field, kind in (("artifact_refs", "artifact"), ("task_refs", "task_revision")):
                for candidate in snapshot.get(field, []) if isinstance(snapshot, dict) else []:
                    if isinstance(candidate, dict) and _same_ref(candidate, wanted):
                        return True
                    # Older impact material may retain only exact IDs in the
                    # parallel list.  That list is a diagnostic, never an
                    # authority, so it cannot satisfy a typed target.
        return False
    return False


def _relation_obligation_match(control: Any, actor: Any, obligation: dict[str, Any],
                               source: dict[str, Any], target: dict[str, Any],
                               relation: str, body: dict[str, Any],
                               request: _RelationRequest | None,
                               delivery_rows: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return the closed mechanical result required by Consumer-M.

    The result deliberately separates mechanical eligibility from meaning
    review.  A true endpoint/member match never becomes a semantic PASS here.
    """
    category = obligation.get("category")
    registry_digest = (request.get("registry_digest", REGISTRY_V1_DIGEST)
                       if request is not None else REGISTRY_V1_DIGEST)
    expected_categories = _relation_categories(
        relation, registry_digest,
        center_ref=request.get("center_ref") if request is not None else None,
        context=getattr(request, "_context", None) if request is not None else None,
        source_ref=source, target_ref=target,
    )
    if request is None:
        return {"mechanical_state": "unverified", "matched_obligation_ids": [],
                "required_meaning_subjects": [],
                "diagnostics": [{"code": "relation_request_required",
                                 "category": category}]}
    if category not in expected_categories:
        return {"mechanical_state": "unverified", "matched_obligation_ids": [],
                "required_meaning_subjects": [],
                "diagnostics": [{"code": "category_not_for_relation", "category": category}]}
    if request is not None and obligation.get("id") not in set(request.get("required_obligation_ids", [])):
        return {"mechanical_state": "unverified", "matched_obligation_ids": [],
                "required_meaning_subjects": [],
                "diagnostics": [{"code": "obligation_outside_request", "obligation": obligation.get("id")}]}

    expected = obligation.get("source_ref")
    if not isinstance(expected, dict):
        return {"mechanical_state": "unverified", "matched_obligation_ids": [],
                "required_meaning_subjects": [],
                "diagnostics": [{"code": "obligation_source_missing"}]}
    endpoint = target
    reason: str | None = None

    try:
        if category == DELIVERY_DECLARED_OUTPUT_CATEGORY:
            row = (delivery_rows or {}).get(obligation.get("id"))
            if not isinstance(row, dict):
                reason = "delivery_observation_unresolved"
            elif row.get("mechanical_state") == "failed":
                return {
                    "mechanical_state": "failed", "matched_obligation_ids": [],
                    "required_meaning_subjects": [],
                    "diagnostics": [{"code": "producer_execution_failed",
                                     "obligation": obligation.get("id")}],
                }
            elif row.get("mechanical_state") != "eligible":
                reason = "delivery_output_material_unresolved"
            else:
                output_ref = row.get("output_ref")
                producer_ref = row.get("producer_ref")
                definition = row.get("definition")
                if (not isinstance(output_ref, dict) or
                        not isinstance(producer_ref, dict) or
                        not isinstance(definition, dict)):
                    reason = "delivery_output_material_unresolved"
                elif relation == "produced_by":
                    if (not _same_ref(source, output_ref) or
                            not _same_ref(target, producer_ref) or
                            target.get("kind") != "delivery_check"):
                        reason = "delivery_producer_endpoint_mismatch"
                elif relation == "contains":
                    if (not _same_ref(target, output_ref) or
                            target.get("kind") != "output_artifact"):
                        reason = "delivery_output_endpoint_mismatch"
                    else:
                        membership = _assurance(control).contains(
                            actor, source["project"], _identity(source), _identity(target),
                        )
                        if membership.get("contains") is not True:
                            reason = "delivery_output_membership_unresolved"
                else:
                    reason = "delivery_output_relation_mismatch"
        elif category == "source_span":
            if not _same_ref(target, expected) or semantic_kind(source) != "artifact":
                reason = "source_span_endpoint_identity_mismatch"
            else:
                content = _resolve_content(control, actor, source, current=False)
                source_ref = expected["locator"].get("source_id") if expected.get("kind") == "traceability_ref" else None
                source_root = _source_ref_for_artifact(control, source["project"], content.get("body", {}), source_ref)
                if source_root is None:
                    reason = "source_span_source_reference_missing"
                else:
                    # Resolve the exact span wrapper as well as the artifact
                    # source membership.  Bounds alone would accept a forged
                    # Unicode range or span hash.
                    _resolve_content(control, actor, expected, current=False)
                    membership = _assurance(control)._member_of(actor, source["project"], source_root, expected)
                    if not membership:
                        reason = "source_span_membership_mismatch"
        elif category in {"artifact_responsibility", "artifact_structural_responsibility"}:
            if not _same_ref(target, expected) or semantic_kind(target) != "artifact":
                reason = "responsibility_target_identity_mismatch"
            else:
                resolved = _resolve_content(control, actor, target, current=False)
                target_body = resolved.get("body")
                if type(target_body) is not dict:
                    reason = "responsibility_target_body_missing"
                else:
                    try:
                        value = _pointer_value(target_body, obligation.get("pointer"))
                    except Fault as exc:
                        reason = exc.code
                    else:
                        if digest(value) != obligation.get("value_digest"):
                            reason = "responsibility_declaration_digest_mismatch"
                        elif category == "artifact_responsibility" and _artifact_kind(control, target) != "domain":
                            reason = "domain_responsibility_owner_kind_mismatch"
        elif category == "requirement":
            if not _same_ref(source, expected) or semantic_kind(source) != "artifact" or semantic_kind(target) != "task_revision":
                reason = "requirement_assignment_endpoint_mismatch"
            elif target.get("task") not in _obligation_task_ids(obligation):
                reason = "requirement_assignment_owner_mismatch"
            else:
                _resolve_content(control, actor, source, current=False)
                _resolve_content(control, actor, target, current=False)
        elif category == "child_obligation":
            # The denominator stores the *parent* acceptance identity for a
            # child obligation.  It does not invent a child artifact ref from
            # a unit label.  Resolve that parent AC, then require the stored
            # asserted decomposes link to identify exactly one child source.
            parent = _artifact_from_ref(expected, source["project"])
            if parent is None or semantic_kind(source) != "artifact":
                reason = "child_obligation_parent_identity_missing"
            elif not _same_ref(target, parent):
                reason = "child_obligation_parent_identity_mismatch"
            else:
                parents = control.s.all(
                    "SELECT source FROM links WHERE target=? AND relation='decomposes' AND confidence='asserted' "
                    "ORDER BY source",
                    (parent.get("artifact"),),
                )
                if len(parents) != 1:
                    reason = "child_obligation_child_identity_unresolved"
                else:
                    child_row = control.s.one(
                        "SELECT * FROM artifacts WHERE id=? AND project=?",
                        (parents[0]["source"], source["project"]),
                    )
                    if child_row is None:
                        reason = "child_obligation_child_missing"
                    else:
                        child = {"kind": "artifact", "project": source["project"],
                                 "artifact": child_row["id"], "revision": child_row["revision"],
                                 "body_digest": child_row["digest"]}
                        if not _same_ref(source, child):
                            reason = "child_obligation_child_identity_mismatch"
            if reason is None:
                _resolve_content(control, actor, source, current=False)
                _resolve_content(control, actor, target, current=False)
                if (_artifact_kind(control, source) != "requirement" or
                        _artifact_kind(control, target) != "requirement"):
                    reason = "child_obligation_requirement_kind_mismatch"
        elif category == "required_output":
            task_ref, item = _task_structural_item(control, actor, obligation)
            if relation == "produced_by" and not _same_ref(target, task_ref):
                reason = "required_output_task_identity_mismatch"
            elif relation == "produced_by" and semantic_kind(source) == "artifact":
                try:
                    _artifact_production_material(control, actor, source, task_ref)
                except Fault as exc:
                    reason = exc.code
            elif relation == "produced_by" and semantic_kind(source) in {"candidate", "candidate_symbol"}:
                # A candidate endpoint has no declaration selector.  It may
                # satisfy only an explicitly typed candidate_member output;
                # an artifact output must come through the immutable
                # artifact-production material branch above.  Required-output
                # cardinality never supplies that missing producer identity.
                if item.get("realization_kind") != "candidate_member":
                    reason = "artifact_producer_material_missing"
                else:
                    locator = source.get("locator", {}) if source.get("kind") == "traceability_ref" else source
                    candidate_task = locator.get("task")
                    candidate_revision = locator.get("task_revision")
                    if (candidate_task != task_ref.get("task") or
                            candidate_revision != task_ref.get("revision")):
                        reason = "candidate_task_identity_mismatch"
                    else:
                        _resolve_content(control, actor, source, current=False)
            elif relation == "contains":
                declared = item.get("artifact_refs")
                if type(declared) is not list or not any(_same_ref(target, ref) for ref in declared):
                    reason = "delivery_output_target_not_declared"
                else:
                    _resolve_content(control, actor, source, current=False)
                if reason is None and not _assurance(control)._member_of(actor, source["project"], source, target):
                    # A delivery snapshot cannot be treated as containing a
                    # Task output until the delivery material adapter selects
                    # that exact member.  ``_member_of`` is authoritative for
                    # its supported delivery member families.
                    reason = "delivery_output_membership_unresolved"
            else:
                reason = "required_output_relation_mismatch"
            if reason is None and digest(item) != obligation.get("value_digest"):
                reason = "required_output_declaration_digest_mismatch"
        elif category == "required_exercise":
            task_ref, item = _task_structural_item(control, actor, obligation)
            if not isinstance(item.get("artifact_refs"), list) or not any(_same_ref(target, ref) for ref in item["artifact_refs"]):
                reason = "required_exercise_target_not_declared"
            elif semantic_kind(source) == "artifact":
                _resolve_content(control, actor, source, current=False)
                if _artifact_kind(control, source) != "test":
                    reason = "required_exercise_source_not_test"
            elif semantic_kind(source) in {"test_plan_check", "delivery_check"}:
                _resolve_content(control, actor, source, current=False)
            else:
                reason = "required_exercise_source_identity_mismatch"
            if reason is None and digest(item) != obligation.get("value_digest"):
                reason = "required_exercise_declaration_digest_mismatch"
        elif category == "impacted_target":
            if not _same_ref(target, expected) or semantic_kind(source) not in {"change", "proposal"}:
                reason = "impact_target_identity_mismatch"
            elif not _change_target_in_context(getattr(request, "_context", {}) if request else {}, source, target):
                reason = "impact_target_not_in_canonical_inventory"
            else:
                _resolve_content(control, actor, source, current=False)
                _resolve_content(control, actor, target, current=False)
        else:
            reason = "unsupported_obligation_category"
    except Fault as exc:
        reason = exc.code

    if reason is None:
        return {"mechanical_state": "eligible", "matched_obligation_ids": [obligation["id"]],
                "required_meaning_subjects": [body.get("edge_id") or body.get("claim", "edge")],
                "diagnostics": []}
    state = "unverified"
    if reason in {"observed_execution_failed", "unit_b_handling_failed"}:
        state = "failed"
    return {"mechanical_state": state, "matched_obligation_ids": [],
            "required_meaning_subjects": [], "diagnostics": [{"code": reason, "obligation": obligation.get("id")}]}


def _closed_match(ok: bool, failed: bool, reason: str | None, *, obligation: str,
                  body: dict[str, Any]) -> dict[str, Any]:
    if ok:
        return {"mechanical_state": "eligible", "matched_obligation_ids": [obligation],
                "required_meaning_subjects": [body.get("edge_id") or body.get("claim", "edge")],
                "diagnostics": []}
    return {"mechanical_state": "failed" if failed else "unverified",
            "matched_obligation_ids": [], "required_meaning_subjects": [],
            "diagnostics": ([{"code": reason}] if reason else [])}


def _scope_and_owner_refs(control: Any, actor: Any, project: str,
                          scope_ref: dict[str, Any], center_ref: dict[str, Any], *,
                          context: dict[str, Any] | None = None,
                          denominator: dict[str, Any] | None = None,
                          relation: str | None = None,
                          direction: str | None = None,
                          projection: dict[str, Any] | None = None,
                          registry_digest: str = REGISTRY_V1_DIGEST) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Resolve the scope owner roots for a relation request.

    A typed candidate is not a member of an artifact scope root.  Its owner
    is established by the sealed candidate provenance chain and the canonical
    Task assignment in the denominator.  Keep this compatibility wrapper
    small; all center ownership decisions are made by
    :func:`resolve_center_owners` so admission and obligation selection share
    one boundary.
    """
    resolved = resolve_center_owners(
        control, actor, context=context, denominator=denominator,
        scope_ref=scope_ref, center_ref=center_ref, relation=relation,
        direction=direction, projection=projection,
        registry_digest=registry_digest,
    )
    return resolved["selected_scope"], resolved["canonical_scope"], resolved["scope_owner_refs"]


def _projection_binding(denominator: dict[str, Any], projection: dict[str, Any], *,
                        relation: str | None = None,
                        direction: str | None = None,
                        center_ref: dict[str, Any] | None = None) -> set[str]:
    """Validate a controller-produced local Task projection.

    ``project_task`` is the only public producer of this shape.  The global
    denominator remains the authority for obligation meaning and all
    contributors; the projection may restrict which obligations this request
    evaluates, but it may not manufacture an ID or omit an assignment from
    the selected Task's local view.
    """
    is_checkpoint = (isinstance(projection, dict) and
                     projection.get("format") in {
                         CHECKPOINT_PROJECTION_FORMAT,
                         GLOBAL_CHECKPOINT_PROJECTION_FORMAT,
                     })
    _validate_projection(projection)
    if projection["global_digest"] != denominator["digest"]:
        raise Fault("stale_projection", "Task projection is from another denominator")
    if is_checkpoint:
        if relation is not None and projection.get("relation") != relation:
            raise Fault("invalid_projection", "Checkpoint projection relation differs from request")
        if direction is not None and projection.get("direction") != direction:
            raise Fault("invalid_projection", "Checkpoint projection direction differs from request")
        if center_ref is not None and not _same_ref(projection.get("center_ref"), center_ref):
            raise Fault("invalid_projection", "Checkpoint projection center differs from request")
    project = denominator["project"]
    if projection.get("format") == GLOBAL_CHECKPOINT_PROJECTION_FORMAT:
        ids = projection["obligation_ids"]
        expected_ids = [item["id"] for item in denominator["obligations"]]
        if ids != expected_ids:
            raise Fault("invalid_projection", "Global checkpoint projection does not retain the complete denominator")
        return set(projection["required_now_ids"])
    task_ref = validate_typed_ref(projection["task_ref"], project=project,
                                  expected_kinds={"task_revision"})
    ids = projection["obligation_ids"]
    if type(ids) is not list or ids != sorted(set(ids)) or any(type(item) is not str or not item for item in ids):
        raise Fault("invalid_projection", "Projection obligation identities are not canonical")
    entries = projection["contributor_requirements"]
    if type(entries) is not list:
        raise Fault("invalid_projection", "Projection contributor requirements are not a list")
    entry_ids: list[str] = []
    entry_by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"obligation_id", "assignment_refs"}:
            raise Fault("invalid_projection", "Projection contributor requirement shape differs")
        oid = entry["obligation_id"]
        if type(oid) is not str or not oid or oid in entry_by_id:
            raise Fault("invalid_projection", "Projection contributor obligation identity is invalid")
        refs = entry["assignment_refs"]
        if type(refs) is not list or refs != sorted(refs, key=canonical):
            raise Fault("invalid_projection", "Projection assignment references are not canonical")
        entry_ids.append(oid)
        entry_by_id[oid] = entry
    if entry_ids != ids:
        raise Fault("invalid_projection", "Projection IDs and contributor entries differ")
    by_id = {item["id"]: item for item in denominator["obligations"]}
    if not set(ids) <= set(by_id):
        raise Fault("invalid_projection", "Projection names an obligation outside the global denominator")
    expected: dict[str, list[Any]] = {}
    selected: set[str] = set()
    for obligation in denominator["obligations"]:
        contributors = []
        for contributor in obligation.get("contributors", []):
            candidate = contributor.get("task_ref") if isinstance(contributor, dict) else None
            if isinstance(candidate, dict) and _same_ref(candidate, task_ref):
                contributors.extend(contributor.get("assignment_refs", []))
        if contributors:
            selected.add(obligation["id"])
            expected[obligation["id"]] = sorted(
                {_canonical_key(item): item for item in contributors}.values(), key=canonical,
            )
    if set(ids) != selected:
        raise Fault("invalid_projection", "Projection does not equal the selected Task's global assignment")
    for oid in ids:
        if entry_by_id[oid]["assignment_refs"] != expected[oid]:
            raise Fault("invalid_projection", "Projection assignment identity differs from denominator", oid)
    if is_checkpoint:
        # The full Task assignment above remains the authority.  Only the
        # controller-created checkpoint partition narrows what this relation
        # request asks the criteria reader to prove now.
        return set(projection["required_now_ids"])
    return set(ids)


def _validate_checkpoint_projection_authority(
        control: Any, actor: Any, *, context: dict[str, Any],
        denominator: dict[str, Any], relation: str, direction: str,
        center_ref: dict[str, Any], scope_ref: dict[str, Any],
        projection: dict[str, Any], registry_digest: str) -> None:
    """Recompute the sealed checkpoint population and classifier result.

    The projection factory carries an opaque classifier plan, but the M/R
    boundary still recomputes its meaning.  This catches a forged/copy-pasted
    schedule, a relation or center cross-use, an owner replacement, and a
    current leaf relabelled as future even when the projection's JSON digest
    is otherwise internally consistent.
    """
    if projection.get("format") not in {
            CHECKPOINT_PROJECTION_FORMAT, GLOBAL_CHECKPOINT_PROJECTION_FORMAT}:
        return
    from .assurance_stage import CHECKPOINTS, classify_relation_obligation

    stage = context.get("stage")
    checkpoint = projection.get("checkpoint")
    if stage not in CHECKPOINTS or checkpoint not in CHECKPOINTS[stage]:
        raise Fault("invalid_projection", "Checkpoint projection is outside the context stage")
    if projection.get("relation") != relation or projection.get("direction") != direction:
        raise Fault("invalid_projection", "Checkpoint projection relation identity differs")
    if not _same_ref(projection.get("center_ref"), center_ref):
        raise Fault("invalid_projection", "Checkpoint projection center identity differs")

    task_projection = (projection
                       if projection.get("format") == CHECKPOINT_PROJECTION_FORMAT
                       else None)
    expected_population = set(_request_required_ids(
        denominator, relation, None,
        control=control, actor=actor, center_ref=center_ref,
        direction=direction, scope_ref=scope_ref, context=context,
        projection=None, registry_digest=registry_digest,
    ))
    actual_population = set(projection.get("population_ids", []))
    if actual_population != expected_population:
        raise Fault("invalid_projection", "Checkpoint projection population differs from canonical request")

    by_id = {item.get("id"): item for item in denominator.get("obligations", [])}
    entries = {item.get("obligation_id"): item
               for item in projection.get("schedule", [])
               if isinstance(item, dict)}
    if set(entries) != actual_population:
        raise Fault("invalid_projection", "Checkpoint projection schedule population differs")
    for obligation_id in sorted(actual_population):
        obligation = by_id.get(obligation_id)
        if not isinstance(obligation, dict):
            raise Fault("invalid_projection", "Checkpoint projection names an unknown obligation")
        owners = _obligation_owner_refs(
            obligation, task_projection, context=context,
            registry_digest=registry_digest,
        )
        if not owners:
            raise Fault("invalid_projection", "Checkpoint projection owner is missing", obligation_id)
        classified = classify_relation_obligation(
            relation, stage=stage, checkpoint=checkpoint,
            obligation=obligation, owner_refs=owners, context=context,
        )
        entry = entries[obligation_id]
        expected_classification = classified.get("classification")
        if expected_classification not in {"required_now", "deferred_future"}:
            raise Fault("invalid_projection", "Checkpoint projection classification is unresolved", obligation_id)
        expected = {
            "obligation_id": obligation_id,
            "classification": expected_classification,
            "first_required_checkpoint": classified.get("first_required_checkpoint"),
            "producer_kind": classified.get("producer_kind"),
            "reason": classified.get("reason"),
            "owner_refs": owners,
        }
        if canonical(entry) != canonical(expected):
            raise Fault("invalid_projection", "Checkpoint projection schedule differs from canonical classifier", obligation_id)
        partition = projection["required_now_ids"] if expected_classification == "required_now" else projection["deferred_future_ids"]
        if obligation_id not in partition:
            raise Fault("invalid_projection", "Checkpoint projection partition differs from classifier", obligation_id)


def _canonical_key(value: Any) -> bytes:
    return canonical(value)


def _obligation_anchor_ref(obligation: dict[str, Any]) -> dict[str, Any] | None:
    """Return the typed population anchor for one denominator obligation.

    Acceptance and source-span obligations name a member (AC/span) rather
    than the containing artifact/source.  Population selection must therefore
    compare the containing identity, while retaining the exact leaf in the
    obligation itself.
    """
    source_ref = obligation.get("source_ref")
    if not isinstance(source_ref, dict):
        return None
    if source_ref.get("kind") == "traceability_ref":
        locator = source_ref.get("locator")
        if not isinstance(locator, dict):
            return None
        if locator.get("ref_type") == "artifact_ac":
            return {"kind": "artifact", "project": source_ref.get("project"),
                    "artifact": locator.get("artifact"), "revision": locator.get("revision"),
                    "body_digest": locator.get("body_digest")}
        if locator.get("ref_type") == "source_span":
            return {"kind": "source", "project": source_ref.get("project"),
                    "source": locator.get("source_id"), "blob_digest": locator.get("blob_digest")}
    return source_ref


def _obligation_scope_anchor_refs(control: Any, actor: Any, project: str,
                                  obligation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the canonical scope population anchors for an obligation.

    Structural Task leaves are sourced from a Task revision, but their scope
    population is the exact declared artifact member.  Treating the Task
    revision itself as the scope anchor makes a valid candidate look outside
    an artifact-rooted scope and invites a caller to widen roots.  Resolve the
    declaration body and retain every exact artifact ref instead.
    """
    if obligation.get("category") in {"required_output", "required_exercise"}:
        try:
            _task_ref, item = _task_structural_item(control, actor, obligation)
        except Fault:
            return []
        refs = item.get("artifact_refs")
        return [ref for ref in refs if isinstance(ref, dict)] if type(refs) is list else []
    anchor = _obligation_anchor_ref(obligation)
    return [anchor] if anchor is not None else []


def _delivery_declaration_item(context: dict[str, Any] | None,
                               obligation: dict[str, Any]) -> dict[str, Any] | None:
    """Find the controller-collected declaration backing one C obligation.

    The public obligation shape intentionally has no ad-hoc producer field.
    The producer owner is recovered from the sealed context's declaration
    inventory, using the complete source/pointer/value identity.
    """
    if not isinstance(context, dict):
        return None
    material = context.get("delivery_material")
    declared = material.get("declared_outputs") if isinstance(material, dict) else None
    items = declared.get("items") if isinstance(declared, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if (_same_ref(item.get("source_ref"), obligation.get("source_ref")) and
                item.get("pointer") == obligation.get("pointer") and
                item.get("value_digest") == obligation.get("value_digest")):
            return item
    return None


def _delivery_center_matches_obligation(context: dict[str, Any] | None,
                                        center_ref: dict[str, Any],
                                        obligation: dict[str, Any],
                                        relation: str, direction: str) -> bool:
    """Match a C center to the exact declaration and producer owner."""
    item = _delivery_declaration_item(context, obligation)
    if item is None:
        return False
    source_ref = item.get("source_ref")
    producer_ref = item.get("producer_ref")
    definition = item.get("definition")
    if not isinstance(source_ref, dict) or not isinstance(producer_ref, dict):
        return False
    if relation == "produced_by":
        if direction == "incoming":
            return _same_ref(center_ref, producer_ref)
        if direction == "outgoing":
            if (center_ref.get("kind") in {"delivery_snapshot", "actual_delivery_commit"} and
                    delivery_declaration_owner_matches(center_ref, definition)):
                delivery = (center_ref if center_ref.get("kind") == "delivery_snapshot"
                            else center_ref.get("delivery"))
                return _same_ref(delivery, source_ref)
            return (
                center_ref.get("kind") == "output_artifact" and
                _same_ref(center_ref.get("delivery"), producer_ref.get("delivery")) and
                _same_ref(center_ref.get("check"), producer_ref) and
                isinstance(definition, dict) and
                center_ref.get("output_id") == definition.get("id")
            )
    if relation == "contains":
        if direction == "outgoing":
            if _same_ref(center_ref, source_ref):
                return True
            if center_ref.get("kind") == "actual_delivery_commit":
                return (
                    delivery_declaration_owner_matches(center_ref, item.get("definition")) and
                    _same_ref(center_ref.get("delivery"), source_ref)
                )
        if direction == "incoming":
            return (
                center_ref.get("kind") == "output_artifact" and
                _same_ref(center_ref.get("delivery"), source_ref) and
                isinstance(definition, dict) and
                center_ref.get("output_id") == definition.get("id")
            )
    return False


def _delivery_explicit_empty_center(context: dict[str, Any] | None,
                                    center_ref: dict[str, Any],
                                    relation: str, direction: str) -> bool:
    """Admit the exact Delivery center for an explicit empty inventory.

    An explicit empty declaration set still has a requestable population.  A
    missing or invalid inventory remains unresolved and cannot be converted
    into an empty successful request.
    """
    if relation not in {"produced_by", "contains"} or direction != "outgoing":
        return False
    if not isinstance(context, dict):
        return False
    material = context.get("delivery_material")
    if not isinstance(material, dict):
        return False
    declared = material.get("declared_outputs")
    delivery_ref = material.get("ref")
    if (not isinstance(declared, dict) or declared.get("status") != "explicit_empty" or
            not isinstance(delivery_ref, dict)):
        return False
    source_ref = (delivery_ref.get("delivery")
                  if delivery_ref.get("kind") == "actual_delivery_commit"
                  else delivery_ref)
    if not isinstance(source_ref, dict) or source_ref.get("kind") != "delivery_snapshot":
        return False
    if center_ref.get("kind") == "delivery_snapshot":
        return _same_ref(center_ref, source_ref)
    return center_ref.get("kind") == "actual_delivery_commit" and _same_ref(center_ref, delivery_ref)


def _assigned_task_matches_scope_anchor(context: dict[str, Any] | None,
                                        control: Any, actor: Any, project: str,
                                        scope: dict[str, Any],
                                        task_ref: dict[str, Any],
                                        anchors: list[dict[str, Any]]) -> bool:
    """Require a candidate's Task to have a retained Breakdown assignment.

    A structural output declaration is a Task-owned leaf, but it is not by
    itself a selection assignment.  The canonical assignment inventory binds
    the Task revision to a requirement/acceptance pair.  Resolve that pair to
    the same exact artifact anchor used for scope admission before allowing a
    candidate center to own the leaf.  ``context is None`` is retained only
    for the private legacy matcher used by older direct unit tests; public
    relation requests always carry the sealed collector context.
    """
    if context is None:
        return True
    artifacts = {
        item.get("id"): item.get("ref")
        for item in context.get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and isinstance(item.get("ref"), dict)
    }
    for assignment in context.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        task_refs = assignment.get("task_refs")
        if not isinstance(task_refs, list) or not any(
                isinstance(candidate, dict) and _same_ref(candidate, task_ref)
                for candidate in task_refs):
            continue
        for pair in assignment.get("obligation_pairs", []):
            if not isinstance(pair, dict):
                continue
            requirement_ref = artifacts.get(pair.get("requirement"))
            if not isinstance(requirement_ref, dict):
                continue
            if any(_same_ref(requirement_ref, anchor) and _ref_in_scope(
                    control, actor, project, scope, anchor,
            ) for anchor in anchors):
                return True
    return False


def _scope_for_request(control: Any, project: str, scope_ref: dict[str, Any]) -> dict[str, Any]:
    selected = _assurance(control)._object_by_ref(scope_ref, project, kinds={"scope", "profile"})
    if selected["kind"] == "profile":
        nested = selected["body"].get("scope_ref")
        need(isinstance(nested, dict), "unresolved_reference", "Selected profile has no scope identity")
        selected = _assurance(control)._object_by_ref(nested, project, kinds={"scope"})
    roots = selected["body"].get("roots", [])
    need(type(roots) is list, "integrity_error", "Selected scope roots are malformed")
    return selected


def _ref_in_scope(control: Any, actor: Any, project: str,
                  scope: dict[str, Any], ref: dict[str, Any]) -> bool:
    assurance = _assurance(control)
    for root in scope["body"].get("roots", []):
        if not isinstance(root, dict):
            continue
        if _same_ref(root, ref):
            return True
        try:
            if assurance._member_of(actor, project, root, ref):
                return True
        except Fault:
            # Unsupported membership is an unresolved population edge, not
            # implicit inclusion in a request.
            continue
    return False


def _center_anchor_matches(control: Any, actor: Any, project: str,
                           center_ref: dict[str, Any],
                           anchor: dict[str, Any]) -> bool:
    try:
        return _same_ref(center_ref, anchor) or _assurance(control)._member_of(
            actor, project, center_ref, anchor,
        )
    except Fault:
        return False


def _center_source_owner_matches(control: Any, actor: Any, project: str,
                                 center_ref: dict[str, Any],
                                 anchor: dict[str, Any]) -> bool:
    """Match an outgoing artifact to a saved source-span population."""
    if center_ref.get("kind") != "artifact" or anchor.get("kind") != "source":
        return False
    try:
        content = _resolve_content(control, actor, center_ref, current=False)
    except Fault:
        return False
    return anchor.get("source") in content.get("body", {}).get("source_refs", [])


def _center_decomposes_child_matches(control: Any, project: str,
                                     center_ref: dict[str, Any],
                                     anchor: dict[str, Any]) -> bool:
    """Match an outgoing child requirement to its saved parent obligations."""
    if center_ref.get("kind") != "artifact" or anchor.get("kind") != "artifact":
        return False
    rows = control.s.all(
        "SELECT target FROM links WHERE source=? AND relation='decomposes' AND confidence='asserted' "
        "ORDER BY target",
        (center_ref.get("artifact"),),
    )
    return any(row.get("target") == anchor.get("artifact") for row in rows)


def _center_declared_member_matches(control: Any, actor: Any, project: str,
                                    center_ref: dict[str, Any],
                                    obligation: dict[str, Any],
                                    *, collection: str) -> bool:
    """Match a test/produced member to an exact saved Task declaration."""
    try:
        _task_ref, item = _task_structural_item(control, actor, obligation)
    except Fault:
        return False
    refs = item.get("artifact_refs")
    return type(refs) is list and any(_same_ref(center_ref, ref) for ref in refs)


def _center_impact_matches(center_ref: dict[str, Any], obligation: dict[str, Any]) -> bool:
    """Bind an outgoing change center to its own impact inventory."""
    if center_ref.get("kind") != "change":
        # Proposal-to-change resolution is a later authority adapter.  The
        # selected scope still bounds this population, but an unrelated typed
        # proposal must not be accepted as a change owner.
        return False
    parts = obligation.get("pointer", "").split("/")
    return len(parts) >= 3 and parts[1] == "changes" and parts[2] == center_ref.get("change")


def _assurance_resolution(control: Any, actor: Any, ref: dict[str, Any], *, current: bool) -> dict[str, Any]:
    """Resolve one endpoint through the real Assurance typed resolver."""
    assurance = _assurance(control)
    if current and hasattr(assurance, "evaluate_current"):
        result = assurance.evaluate_current(actor, _plain_json(ref))
        state = (result.get("current") or {}).get("state") if isinstance(result, dict) else None
        if state != "current":
            raise Fault("stale_reference" if state == "stale" else "unresolved_reference",
                        "Typed center is not current", ref)
    else:
        result = assurance.resolve_pinned(actor, _plain_json(ref))
    resolution = result.get("resolution") if isinstance(result, dict) else None
    if not isinstance(resolution, dict):
        raise Fault("unresolved_reference", "Typed center has no canonical resolution", ref)
    return resolution


def _generic_candidate_ref(project: str, locator: dict[str, Any]) -> dict[str, Any]:
    required = ("candidate", "task", "task_revision", "candidate_digest", "snapshot_digest")
    if any(key not in locator for key in required):
        raise Fault("invalid_reference", "Candidate symbol locator lacks generic candidate identity")
    return {"kind": "candidate", "project": project,
            **{key: locator[key] for key in required}}


def _resolve_center_task_ref(control: Any, actor: Any, project: str,
                             center_ref: dict[str, Any], *, current: bool = True) -> dict[str, Any] | None:
    """Resolve candidate/code centers to the exact Task revision owner.

    The candidate resolver validates the implementation run, receipt, pinned
    snapshot/CAS and retained Task definition.  Candidate symbols first pass
    their existing symbol resolver, then use the same generic candidate
    identity to obtain the Task definition digest.  No task ID supplied by a
    caller is trusted without this chain.
    """
    kind = semantic_kind(center_ref)
    if kind == "task_revision":
        _assurance_resolution(control, actor, center_ref, current=current)
        return _identity(center_ref)
    if kind == "candidate":
        resolution = _assurance_resolution(control, actor, center_ref, current=current)
    elif kind == "candidate_symbol":
        # Resolve the actual code symbol first.  This checks candidate
        # provenance, source bytes, AST span and adapter identity.
        _assurance_resolution(control, actor, center_ref, current=current)
        locator = center_ref.get("locator")
        if not isinstance(locator, dict):
            raise Fault("invalid_reference", "Candidate symbol locator is malformed")
        resolution = _assurance_resolution(
            control, actor, _generic_candidate_ref(project, locator), current=current,
        )
    else:
        return None
    identity = resolution.get("candidate_identity")
    if not isinstance(identity, dict):
        raise Fault("unresolved_reference", "Candidate resolution has no Task identity", center_ref)
    task_ref = {
        "kind": "task_revision", "project": project,
        "task": identity.get("task"), "revision": identity.get("task_revision"),
        "definition_digest": identity.get("task_definition_digest"),
    }
    return _identity(validate_typed_ref(task_ref, project=project, expected_kinds={"task_revision"}))


def _center_contributor_matches(control: Any, actor: Any, project: str,
                                center_ref: dict[str, Any],
                                obligation: dict[str, Any], *,
                                center_task_ref: dict[str, Any] | None = None) -> bool:
    try:
        task_ref = center_task_ref or _resolve_center_task_ref(
            control, actor, project, center_ref, current=True,
        )
    except Fault:
        return False
    if task_ref is None:
        return False
    return any(
        isinstance(item, dict) and isinstance(item.get("task_ref"), dict)
        and _same_ref(item["task_ref"], task_ref)
        for item in obligation.get("contributors", [])
    )


def _center_matches_obligation(control: Any, actor: Any, project: str,
                               relation: str, direction: str,
                               center_ref: dict[str, Any],
                               obligation: dict[str, Any], *,
                               center_task_ref: dict[str, Any] | None = None,
                               context: dict[str, Any] | None = None,
                               registry_digest: str = REGISTRY_V1_DIGEST) -> bool:
    """Apply the closed relation/direction owner rule to one obligation.

    Scope membership selects the opposite population in ``_request_required_ids``;
    this dispatch selects the center side.  Every registry relation has an
    explicit mode so a newly unsupported owner type becomes an empty/unknown
    request rather than silently inheriting a broad ``True`` branch.
    """
    if (registry_digest == REGISTRY_V2_DIGEST and
            obligation.get("category") == DELIVERY_DECLARED_OUTPUT_CATEGORY):
        return _delivery_center_matches_obligation(
            context, center_ref, obligation, relation, direction,
        )
    mode = _CENTER_OWNER_RULES.get(relation, {}).get(direction)
    anchor = _obligation_anchor_ref(obligation)
    if mode is None or anchor is None:
        return False
    if mode == "anchor_member":
        # A leaf center (artifact AC, source span, plan check, or delivery
        # check) is an exact owner in its own right.  Container centers use
        # the typed member resolver against the normalized anchor.
        source_ref = obligation.get("source_ref")
        return ((isinstance(source_ref, dict) and _same_ref(center_ref, source_ref)) or
                _center_anchor_matches(control, actor, project, center_ref, anchor))
    if mode == "source_owner":
        return _center_source_owner_matches(control, actor, project, center_ref, anchor)
    if mode == "decomposes_child":
        return _center_decomposes_child_matches(control, project, center_ref, anchor)
    if mode == "contributor":
        return _center_contributor_matches(
            control, actor, project, center_ref, obligation,
            center_task_ref=center_task_ref,
        )
    if mode == "declared_exercise":
        return _center_declared_member_matches(
            control, actor, project, center_ref, obligation, collection="required_exercises",
        )
    if mode == "declared_output":
        return _center_declared_member_matches(
            control, actor, project, center_ref, obligation, collection="required_outputs",
        )
    if mode == "impact_owner":
        return _center_impact_matches(center_ref, obligation)
    if mode in {"scope_population", "edge_endpoint"}:
        # The selected scope is the authoritative opposite-side population;
        # edge_endpoint is deliberately empty for depends_on because that
        # relation has no independent denominator obligations.
        return True
    return False


def resolve_center_owners(control: Any, actor: Any, *, context: dict[str, Any] | None,
                          denominator: dict[str, Any] | None,
                          scope_ref: dict[str, Any], center_ref: dict[str, Any],
                          relation: str | None, direction: str | None,
                          projection: dict[str, Any] | None = None,
                          registry_digest: str = REGISTRY_V1_DIGEST) -> dict[str, Any]:
    """Resolve a typed relation center through sealed scope and assignment data.

    The returned closed result is shared by scope admission, required-leaf
    selection and local/global contributor projection.  A valid candidate or
    Task is still rejected when its verified Task revision has no canonical
    assignment to the selected scope.  Scope roots are never expanded to
    include Task/candidate types merely to make a center pass.
    """
    assurance = _assurance(control)
    if denominator is not None and context is None:
        raise Fault("denominator_input_mismatch",
                    "Typed center ownership needs the sealed stage context")
    project = scope_ref.get("project")
    need(project == center_ref.get("project"), "cross_project", "Typed center and scope belong to different projects")
    validate_typed_ref(_identity(scope_ref), project=project, expected_kinds={"assurance_object"})
    validate_typed_ref(_identity(center_ref), project=project)
    selected = assurance._object_by_ref(scope_ref, project, kinds={"scope", "profile"})
    scope = selected
    if selected["kind"] == "profile":
        nested = selected["body"].get("scope_ref")
        need(isinstance(nested, dict), "unresolved_reference", "Selected profile has no scope identity")
        scope = assurance._object_by_ref(nested, project, kinds={"scope"})
    roots = scope["body"].get("roots", [])
    need(type(roots) is list, "integrity_error", "Assurance scope roots are malformed")
    normalized_roots = []
    for root in roots:
        normalized = validate_typed_ref(_identity(root), project=project)
        need(semantic_kind(normalized) in {"artifact", "source", "population"},
             "invalid_scope", "Scope root kind is outside the canonical population contract")
        normalized_roots.append(normalized)

    direct_owner_refs = []
    for root in normalized_roots:
        if _same_ref(root, center_ref):
            direct_owner_refs.append(root)
            continue
        try:
            if assurance._member_of(actor, project, root, center_ref):
                direct_owner_refs.append(root)
        except Fault:
            # Unsupported membership is unresolved, never an implicit pass.
            continue

    task_owner_refs: list[dict[str, Any]] = []
    scoped_anchor_refs: list[dict[str, Any]] = []
    matched_obligation_ids: list[str] = []
    unresolved: list[dict[str, Any]] = []
    center_task_ref = None
    if relation is not None and direction is not None and denominator is not None:
        _validate_denominator(denominator)
        if context is not None:
            _validate_context(context)
            need(context["project"] == project and
                 context["program"] == denominator["program"] and
                 context["stage"] == denominator["stage"],
                 "denominator_input_mismatch",
                 "Typed center context and denominator identity differs")
        if projection is not None:
            _projection_binding(
                denominator, projection, relation=relation,
                direction=direction, center_ref=center_ref,
            )
        try:
            if semantic_kind(center_ref) in {"candidate", "candidate_symbol", "task_revision"}:
                center_task_ref = _resolve_center_task_ref(
                    control, actor, project, center_ref, current=True,
                )
        except Fault as exc:
            unresolved.append({"code": exc.code, "reason": str(exc), "center": _identity(center_ref)})

        projection_ids = None
        if projection is not None:
            projection_ids = set(projection["obligation_ids"])
        categories = _relation_categories(
            relation, registry_digest, center_ref=center_ref, context=context,
        )
        for obligation in denominator.get("obligations", []):
            if obligation.get("category") not in categories:
                continue
            if projection_ids is not None and obligation.get("id") not in projection_ids:
                continue
            anchors = _obligation_scope_anchor_refs(control, actor, project, obligation)
            in_scope = any(_ref_in_scope(control, actor, project, scope, anchor)
                           for anchor in anchors)
            if (registry_digest == REGISTRY_V2_DIGEST and
                    obligation.get("category") == DELIVERY_DECLARED_OUTPUT_CATEGORY):
                # Delivery declarations are selected by the v3 profile and
                # the sealed delivery material, not by pretending a Delivery
                # snapshot is a child of an artifact scope root.
                in_scope = _delivery_declaration_item(context, obligation) is not None
            if not anchors or not in_scope:
                continue
            if not _center_matches_obligation(
                    control, actor, project, relation, direction, center_ref,
                    obligation, center_task_ref=center_task_ref, context=context,
                    registry_digest=registry_digest):
                continue
            if (center_task_ref is not None
                    and obligation.get("category") in {"required_output", "required_exercise"}
                    and not _assigned_task_matches_scope_anchor(
                        context, control, actor, project, scope, center_task_ref, anchors,
                    )):
                unresolved.append({
                    "code": "task_assignment_missing",
                    "reason": "Typed center Task is not assigned to the selected scope anchor",
                    "center": _identity(center_ref),
                    "obligation": obligation.get("id"),
                })
                continue
            matched_obligation_ids.append(obligation["id"])
            scoped_anchor_refs.extend(anchors)
            task_owner_refs.extend(_obligation_owner_refs(
                obligation, projection, context=context,
                registry_digest=registry_digest,
            ))

    scope_owner_refs = list(direct_owner_refs)
    for anchor in scoped_anchor_refs:
        for root in normalized_roots:
            if _same_ref(root, anchor):
                scope_owner_refs.append(root)
                continue
            try:
                if assurance._member_of(actor, project, root, anchor):
                    scope_owner_refs.append(root)
            except Fault:
                continue

    def unique(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted({_canonical_key(_identity(value)): _identity(value) for value in values}.values(), key=canonical)

    scope_owner_refs = unique(scope_owner_refs)
    empty_delivery_center = _delivery_explicit_empty_center(
        context, center_ref, relation or "", direction or "",
    )
    delivery_family = (
        relation is not None and
        _relation_family(
            relation, registry_digest, center_ref=center_ref, context=context,
        ) == "delivery"
    )
    if (delivery_family and (matched_obligation_ids or empty_delivery_center)):
        # The selected profile is the owner boundary for Delivery material;
        # the snapshot itself is not a member of an artifact-rooted scope.
        scope_owner_refs = unique([*scope_owner_refs, _identity(selected)])
    task_owner_refs = unique(task_owner_refs)
    scoped_anchor_refs = unique(scoped_anchor_refs)
    matched_obligation_ids = sorted(set(matched_obligation_ids))
    provenance_refs = unique([center_ref, *task_owner_refs, *scoped_anchor_refs])
    state = "verified" if scope_owner_refs and (
        not relation or direct_owner_refs or matched_obligation_ids or empty_delivery_center
    ) else "unverified"
    return {
        "state": state,
        "selected_scope": selected,
        "canonical_scope": scope,
        "scope_owner_refs": scope_owner_refs,
        "center_ref": _identity(center_ref),
        "task_owner_refs": task_owner_refs,
        "scoped_anchor_refs": scoped_anchor_refs,
        "matched_obligation_ids": matched_obligation_ids,
        "provenance_refs": provenance_refs,
        "unresolved": sorted(unresolved, key=canonical),
    }


def _obligation_owner_refs(obligation: dict[str, Any],
                           projection: dict[str, Any] | None = None, *,
                           context: dict[str, Any] | None = None,
                           registry_digest: str = REGISTRY_V1_DIGEST) -> list[dict[str, Any]]:
    """Return the global or Task-local contributor owners for one obligation."""
    if (registry_digest == REGISTRY_V2_DIGEST and
            obligation.get("category") == DELIVERY_DECLARED_OUTPUT_CATEGORY):
        item = _delivery_declaration_item(context, obligation)
        producer = item.get("producer_ref") if isinstance(item, dict) else None
        return [_identity(producer)] if isinstance(producer, dict) else []
    task_filter = None
    if isinstance(projection, dict):
        candidate = projection.get("task_ref")
        if isinstance(candidate, dict):
            task_filter = candidate
    contributors = [
        item for item in obligation.get("contributors", [])
        if isinstance(item, dict) and isinstance(item.get("task_ref"), dict)
        and (task_filter is None or _same_ref(item["task_ref"], task_filter))
    ]
    if contributors:
        values = [_identity(item["task_ref"]) for item in contributors]
    elif obligation.get("contributors"):
        # A local projection that names an obligation but has no contributor
        # assignment cannot fall back to the source owner.  That would turn a
        # missing local assignment into a valid owner.
        values = []
    elif isinstance(obligation.get("source_ref"), dict):
        values = [_identity(obligation["source_ref"])]
    else:
        values = []
    unique = {canonical(value): value for value in values}
    return sorted(unique.values(), key=canonical)


def _request_required_ids(denominator: dict[str, Any], relation: str,
                          projection_ids: set[str] | None = None, *,
                          control: Any | None = None, actor: Any | None = None,
                          center_ref: dict[str, Any] | None = None,
                          direction: str | None = None,
                          scope_ref: dict[str, Any] | None = None,
                          context: dict[str, Any] | None = None,
                          projection: dict[str, Any] | None = None,
                          registry_digest: str = REGISTRY_V1_DIGEST) -> list[str]:
    categories = _relation_categories(
        relation, registry_digest, center_ref=center_ref, context=context,
    )
    if control is None or actor is None or center_ref is None or direction is None or scope_ref is None:
        # Kept only for internal historical callers that do not have a
        # controller request boundary.  Public Consumer-M paths always pass
        # the sealed center/scope arguments below.
        return sorted(item["id"] for item in denominator["obligations"]
                      if item.get("category") in categories and
                      (projection_ids is None or item["id"] in projection_ids))
    project = denominator["project"]
    if context is not None:
        resolved = resolve_center_owners(
            control, actor, context=context, denominator=denominator,
            scope_ref=scope_ref, center_ref=center_ref, relation=relation,
            direction=direction, projection=projection,
            registry_digest=registry_digest,
        )
        allowed = set(resolved["matched_obligation_ids"])
    else:
        scope = _scope_for_request(control, project, scope_ref)
        allowed = {
            item["id"] for item in denominator["obligations"]
            if item.get("category") in categories
            and (projection_ids is None or item["id"] in projection_ids)
            and (lambda anchor: anchor is not None and _ref_in_scope(
                control, actor, project, scope, anchor,
            ))(_obligation_anchor_ref(item))
        }
    return sorted(item_id for item_id in allowed
                  if projection_ids is None or item_id in projection_ids)


def _verify_relation_request(value: Any, control: Any, *, project: str,
                             relation: str | None = None,
                             denominator: dict[str, Any] | None = None) -> _RelationRequest:
    if (not isinstance(value, _RelationRequest) or value._control is not control or
            value._origin is not _REQUEST_ORIGIN):
        raise Fault("invalid_relation_request", "relation_request must come from build_relation_request")
    if value._seal != canonical(dict(value)):
        raise Fault("invalid_relation_request", "relation_request was modified after collection")
    required = {
        "format", "project", "program", "stage", "relation", "direction",
        "center_ref", "scope_ref", "registry_digest", "global_denominator_digest",
        "local_projection", "required_obligation_ids", "owner_mapping",
        "unresolved", "capabilities", "request_digest",
    }
    if set(value) != required:
        raise Fault("invalid_relation_request", "relation_request wire shape differs",
                    sorted(set(value) ^ required))
    if value["format"] != _REQUEST_FORMAT:
        raise Fault("invalid_relation_request", "relation_request format differs")
    try:
        registry_entry(value["relation"], contract_digest=value.get("registry_digest"))
    except Fault as exc:
        raise Fault("invalid_relation_request", "relation_request relation is not registered") from exc
    if project != value["project"]:
        raise Fault("cross_project", "relation_request belongs to another project")
    if relation is not None and value["relation"] != relation:
        raise Fault("invalid_relation_request", "relation_request relation differs", relation)
    registry_digest = value["registry_digest"]
    if registry_digest not in {REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST}:
        raise Fault("invalid_registry", "relation_request registry digest differs")
    if value["direction"] not in {"incoming", "outgoing"}:
        raise Fault("invalid_relation_request", "relation_request direction is invalid")
    validate_typed_ref(value["center_ref"], project=project)
    validate_typed_ref(value["scope_ref"], project=project, expected_kinds={"assurance_object"})
    if value["scope_ref"].get("object_kind") not in {"scope", "profile"}:
        raise Fault("invalid_relation_request", "relation_request scope_ref is not a scope/profile")
    if type(value["required_obligation_ids"]) is not list or value["required_obligation_ids"] != sorted(set(value["required_obligation_ids"])):
        raise Fault("invalid_relation_request", "relation_request obligations are not canonical")
    if any(type(item) is not str or not item for item in value["required_obligation_ids"]):
        raise Fault("invalid_relation_request", "relation_request obligation identity is malformed")
    if type(value["owner_mapping"]) is not list:
        raise Fault("invalid_relation_request", "relation_request owner mapping is malformed")
    ids = []
    for item in value["owner_mapping"]:
        if type(item) is not dict or set(item) != {"obligation_id", "owners"}:
            raise Fault("invalid_relation_request", "relation_request owner mapping entry is malformed")
        if type(item["obligation_id"]) is not str or type(item["owners"]) is not list:
            raise Fault("invalid_relation_request", "relation_request owner mapping entry is malformed")
        if not item["owners"]:
            raise Fault("invalid_relation_request", "relation_request owner mapping entry is empty")
        if item["owners"] != sorted(item["owners"], key=canonical):
            raise Fault("invalid_relation_request", "relation_request owners are not canonical")
        if len({canonical(_identity(owner)) for owner in item["owners"]}) != len(item["owners"]):
            raise Fault("invalid_relation_request", "relation_request owner mapping contains duplicates")
        for owner in item["owners"]:
            validate_typed_ref(owner, project=project)
        ids.append(item["obligation_id"])
    if ids != sorted(set(ids)) or ids != value["required_obligation_ids"]:
        raise Fault("invalid_relation_request", "relation_request owner mapping does not cover obligations")
    capabilities = value["capabilities"]
    if type(capabilities) is not dict or set(capabilities) != {
            "categories", "extractors", "center_current", "scope_selected",
            "projection_scope", "required_count", "context_digest"}:
        raise Fault("invalid_relation_request", "relation_request capability shape differs")
    expected_categories = _relation_categories(
        value["relation"], registry_digest, center_ref=value.get("center_ref"),
        context=getattr(value, "_context", None),
    )
    if capabilities["categories"] != sorted(expected_categories):
        raise Fault("invalid_relation_request", "relation_request category set differs")
    if type(capabilities["extractors"]) is not dict or type(capabilities["center_current"]) is not bool or \
            type(capabilities["scope_selected"]) is not bool or \
            capabilities["projection_scope"] not in {"global", "task"} or \
            type(capabilities["required_count"]) is not int or capabilities["required_count"] < 0:
        raise Fault("invalid_relation_request", "relation_request capability values are malformed")
    if capabilities["center_current"] is not True or capabilities["scope_selected"] is not True:
        raise Fault("invalid_relation_request", "relation_request currentness/selection markers are false")
    if type(value["unresolved"]) is not list or value["unresolved"] != sorted(value["unresolved"], key=canonical):
        raise Fault("invalid_relation_request", "relation_request unresolved diagnostics are not canonical")
    projection = value["local_projection"]
    projection_ids = None
    if projection is not None:
        if denominator is None:
            _validate_projection(projection)
            if projection["global_digest"] != value["global_denominator_digest"]:
                raise Fault("stale_projection", "relation_request projection is from another denominator")
        else:
            projection_ids = _projection_binding(
                denominator, projection, relation=value["relation"],
                direction=value["direction"], center_ref=value["center_ref"],
            )
            if projection.get("format") in {
                    CHECKPOINT_PROJECTION_FORMAT,
                    GLOBAL_CHECKPOINT_PROJECTION_FORMAT,
            }:
                _validate_checkpoint_projection_authority(
                    control, value._actor, context=value._context,
                    denominator=denominator, relation=value["relation"],
                    direction=value["direction"], center_ref=value["center_ref"],
                    scope_ref=value["scope_ref"], projection=projection,
                    registry_digest=registry_digest,
                )
    if projection_ids is not None:
        expected_local_ids = set(_request_required_ids(
            denominator, value["relation"], projection_ids,
            control=control, actor=value._actor, center_ref=value["center_ref"],
            direction=value["direction"], scope_ref=value["scope_ref"],
            context=value._context, projection=projection,
            registry_digest=registry_digest,
        ))
        if set(value["required_obligation_ids"]) != expected_local_ids:
            raise Fault("invalid_relation_request", "relation_request local projection obligations differ")
    elif denominator is not None:
        expected_global_ids = set(_request_required_ids(
            denominator, value["relation"],
            control=control, actor=value._actor, center_ref=value["center_ref"],
            direction=value["direction"], scope_ref=value["scope_ref"],
            context=value._context, registry_digest=registry_digest,
        ))
        if set(value["required_obligation_ids"]) != expected_global_ids:
            raise Fault("invalid_relation_request", "relation_request denominator obligations differ")
    if (projection is None and capabilities["projection_scope"] != "global") or \
            (projection is not None and capabilities["projection_scope"] != "task"):
        raise Fault("invalid_relation_request", "relation_request projection scope marker differs")
    body = dict(value)
    body.pop("request_digest")
    if digest(body) != value["request_digest"]:
        raise Fault("integrity_error", "relation_request digest differs")
    if denominator is not None and denominator["digest"] != value["global_denominator_digest"]:
        raise Fault("stale_denominator", "relation_request denominator differs")
    if denominator is not None:
        by_id = {item["id"]: item for item in denominator["obligations"]}
        for item in value["owner_mapping"]:
            obligation = by_id.get(item["obligation_id"])
            if obligation is None:
                raise Fault("invalid_relation_request", "relation_request owner names unknown obligation")
            expected_owners = _obligation_owner_refs(
                obligation, value.get("local_projection"), context=value._context,
                registry_digest=registry_digest,
            )
            if sorted(expected_owners, key=canonical) != sorted(item["owners"], key=canonical):
                raise Fault("invalid_relation_request", "relation_request owner identity differs", item["obligation_id"])
        expected_required = _request_required_ids(
            denominator, value["relation"], projection_ids,
            control=control, actor=value._actor, center_ref=value["center_ref"],
            direction=value["direction"], scope_ref=value["scope_ref"],
            context=value._context, projection=projection,
            registry_digest=registry_digest,
        )
        if value["required_obligation_ids"] != expected_required:
            raise Fault("invalid_relation_request", "relation_request required population differs")
    if capabilities["required_count"] != len(value["required_obligation_ids"]):
        raise Fault("invalid_relation_request", "relation_request required count differs")
    context = value._context
    if not isinstance(context, dict):
        raise Fault("invalid_relation_request", "relation_request has no sealed context")
    if value._context_seal != canonical(dict(context)):
        raise Fault("invalid_relation_request", "relation_request context was modified")
    if value._denominator is not None and value._denominator_seal != canonical(dict(value._denominator)):
        raise Fault("invalid_relation_request", "relation_request denominator was modified")
    context = _validate_context(context)
    selection = context.get("capabilities", {}).get("selection", {})
    selected_format = selection.get("profile_format") if isinstance(selection, dict) else None
    selected_digest = selection.get("effective_relation_contract_digest") if isinstance(selection, dict) else None
    expected_registry = profile_registry(selected_format)
    if selected_digest is not None and selected_digest != expected_registry:
        raise Fault("invalid_registry", "Selected profile effective registry is inconsistent")
    if registry_digest != expected_registry:
        raise Fault("invalid_registry", "relation_request registry does not match selected profile")
    if value["capabilities"].get("context_digest") != digest(context):
        raise Fault("integrity_error", "relation_request context digest differs")
    return value


def build_relation_request(control: Any, actor: Any, *, context: Any,
                           denominator: Any, relation: str, center_ref: dict[str, Any],
                           direction: str, scope_ref: dict[str, Any],
                           registry_digest: str, projection: Any = None,
                           checkpoint: str | None = None) -> _RelationRequest:
    """Seal one controller-selected relation population.

    Context and denominator must be the private collector results.  A caller
    may inspect the resulting JSON mapping, but only this in-process object
    retains the context needed by impact/source matchers and the origin token
    used by :func:`evaluate_criteria`.
    """
    if registry_digest not in {REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST}:
        raise Fault("invalid_registry", "Relation registry digest differs")
    entry = registry_entry(relation, contract_digest=registry_digest)
    context_value = _validate_context(context)
    denominator_value = _validate_denominator(denominator)
    if (context_value["format"] not in {CONTEXT_FORMAT, CONTEXT_V3_FORMAT, CONTEXT_V4_FORMAT} or
            context_value["project"] != denominator_value["project"] or
            context_value["program"] != denominator_value["program"] or
            context_value["stage"] != denominator_value["stage"]):
        raise Fault("denominator_input_mismatch", "Context and denominator identity differs")
    selection = context_value.get("capabilities", {}).get("selection", {})
    selected_format = selection.get("profile_format") if isinstance(selection, dict) else None
    selected_digest = selection.get("effective_relation_contract_digest") if isinstance(selection, dict) else None
    expected_registry = profile_registry(selected_format)
    if selected_digest is not None and selected_digest != expected_registry:
        raise Fault("invalid_registry", "Selected profile effective registry is inconsistent")
    if registry_digest != expected_registry:
        raise Fault("invalid_registry", "Relation registry digest does not match selected profile")
    if registry_digest == REGISTRY_V2_DIGEST and denominator_value["format"] not in {DENOMINATOR_V3_FORMAT, DENOMINATOR_V4_FORMAT}:
        raise Fault("invalid_registry", "Output registry requires denominator.v3 or denominator.v4")
    project = denominator_value["project"]
    control.k.project(actor, project)
    center = validate_typed_ref(center_ref, project=project)
    scope = validate_typed_ref(scope_ref, project=project, expected_kinds={"assurance_object"})
    if direction not in {"incoming", "outgoing"}:
        raise Fault("invalid_relation_request", "Relation direction is invalid")
    assurance = _assurance(control)
    need(assurance._relation_center_supported(
        relation, center, contract_digest=registry_digest,
    ),
         "invalid_relation_request", "Relation center kind is outside the registry contract")
    scope_object = assurance._object_by_ref(scope, project, kinds={"scope", "profile"})
    if scope_object["kind"] == "profile":
        selectors = scope_object["body"].get("relation_selectors", [])
        need(relation in selectors, "invalid_relation_request",
             "Relation is outside the selected profile", relation)
    if context_value["format"] in {CONTEXT_V3_FORMAT, CONTEXT_V4_FORMAT}:
        selection_ref = context_value.get("selection_ref")
        need(isinstance(selection_ref, dict), "invalid_registry",
             "selected output context has no selected profile reference")
        selected_profile = assurance._object_by_ref(
            selection_ref, project, kinds={"profile"},
        )
        need(_same_ref(selection_ref, scope), "invalid_registry",
             "Relation request scope is not the selected profile")
        selected_body = selected_profile.get("body", {})
        selected_profile_format = selected_body.get("format")
        expected_selected_registry = profile_registry(selected_profile_format)
        need(selected_profile_format in CANONICAL_PROFILE_FORMATS and
             selected_body.get("required_relation_contract_digest",
                               selected_body.get("relation_contract_digest", REGISTRY_V1_DIGEST)) == expected_selected_registry,
             "invalid_registry", "Selected profile is not bound to its canonical relation registry")
        try:
            assurance._ensure_object_current(actor, project, selected_profile, require_self=True)
        except Fault as exc:
            raise Fault("stale_reference", "Selected profile is not current", str(exc)) from exc
    projection_ids = None
    local_projection = None
    if projection is not None:
        local_projection = _validate_projection(projection)
        projection_ids = _projection_binding(
            denominator_value, local_projection, relation=relation,
            direction=direction, center_ref=center,
        )
        if local_projection.get("format") in {
                CHECKPOINT_PROJECTION_FORMAT,
                GLOBAL_CHECKPOINT_PROJECTION_FORMAT,
        }:
            if checkpoint is None:
                raise Fault(
                    "invalid_relation_request",
                    "Checkpoint projection requires its request checkpoint",
                )
            if local_projection.get("checkpoint") != checkpoint:
                raise Fault(
                    "invalid_relation_request",
                    "Checkpoint projection checkpoint differs from request",
                )
            _validate_checkpoint_projection_authority(
                control, actor, context=context,
                denominator=denominator_value, relation=relation,
                direction=direction, center_ref=center, scope_ref=scope,
                projection=local_projection, registry_digest=registry_digest,
            )
    center_owners = resolve_center_owners(
        control, actor, context=context, denominator=denominator,
        scope_ref=scope, center_ref=center, relation=relation,
        direction=direction, projection=local_projection,
        registry_digest=registry_digest,
    )
    need(center_owners["scope_owner_refs"], "invalid_relation_request",
         "Relation center is outside the selected scope owner set")
    # A scope/profile may be the immutable proposal that is about to receive
    # its first review.  Resolve its exact row and dependencies here, while
    # leaving adopted-head currentness to the set/review resolver; requiring a
    # head at this point would make the normal proposal→review→adopt flow
    # impossible.
    _resolve_content(control, actor, scope, current=False)
    current, reason = _resolve_endpoint_current(control, actor, center)
    if not current:
        raise Fault("stale_reference" if reason == "stale_reference" else "unresolved_reference",
                    "Relation center is not current", reason)
    selected_categories = _relation_categories(
        relation, registry_digest, center_ref=center, context=context_value,
    )
    if (registry_digest == REGISTRY_V2_DIGEST and relation in {"produced_by", "contains"}
            and not selected_categories):
        raise Fault(
            "invalid_relation_request",
            "Typed relation center or endpoint family is unresolved",
            {"relation": relation, "center_ref": _identity(center)},
        )
    required = _request_required_ids(
        denominator_value, relation, projection_ids,
        control=control, actor=actor, center_ref=center, direction=direction,
        scope_ref=scope, context=context, projection=local_projection,
        registry_digest=registry_digest,
    )
    by_id = {item["id"]: item for item in denominator_value["obligations"]}
    owner_mapping = []
    for obligation_id in required:
        obligation = by_id[obligation_id]
        owners = _obligation_owner_refs(
            obligation, local_projection, context=context,
            registry_digest=registry_digest,
        )
        need(owners, "invalid_relation_request",
             "Relation obligation has no selected typed owner", obligation_id)
        owner_mapping.append({"obligation_id": obligation_id,
                              "owners": owners})
    unresolved = list(context_value.get("unresolved", []))
    unresolved.extend(item for item in denominator_value.get("unresolved", [])
                      if isinstance(item, dict))
    unresolved = sorted(unresolved, key=canonical)
    if len(unresolved) > 10000:
        raise Fault("invalid_relation_request", "relation_request unresolved diagnostics exceed the bound")
    extractor = {}
    for category in selected_categories:
        key = _EXTRACTOR_CATEGORY.get(category)
        if key is not None:
            info = denominator_value.get("capabilities", {}).get("extractors", {}).get(key)
            extractor[category] = info or {"supported": False, "reason": "extractor_metadata_missing"}
    body = {
        "format": _REQUEST_FORMAT, "project": project,
        "program": denominator_value["program"], "stage": denominator_value["stage"],
        "relation": relation, "direction": direction,
        "center_ref": _identity(center), "scope_ref": _identity(scope),
        "registry_digest": registry_digest,
        "global_denominator_digest": denominator_value["digest"],
        "local_projection": copy.deepcopy(local_projection),
        "required_obligation_ids": required,
        "owner_mapping": owner_mapping,
        "unresolved": unresolved,
        "capabilities": {"categories": sorted(selected_categories), "extractors": extractor,
                          "center_current": True, "scope_selected": True,
                          "projection_scope": "task" if local_projection is not None else "global",
                          "required_count": len(required),
                          "context_digest": digest(context_value)},
    }
    body["request_digest"] = digest(body)
    # Retain the original sealed collector object for the private matcher
    # context.  ``_validate_context`` returns a detached public copy by
    # design; storing that copy would erase the collector origin token.
    return _RelationRequest(body, control=control, actor=actor, context=context,
                            denominator=denominator,
                            _origin=_REQUEST_ORIGIN)


def _object_row(control: Any, ref: dict[str, Any], project: str,
                kinds: set[str] | None = None) -> dict[str, Any]:
    return _assurance(control)._object_by_ref(ref, project, kinds=kinds)


def _packet_marker(row: dict[str, Any]) -> str:
    return f"{row['kind']}:{row['id']}@{row['digest']}"


def _packet_body(control: Any, project: str, packet_id: str) -> dict[str, Any]:
    row = control.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='packet'",
                        (packet_id, project))
    if row is None:
        raise Fault("unverified_relation_review", "Assurance review packet is missing", packet_id)
    body = _json_body(row["body"], "assurance review packet")
    if digest(body) != row["digest"]:
        raise Fault("integrity_error", "Assurance review packet digest differs", packet_id)
    if body.get("format") != "assurance.review-packet.v1":
        raise Fault("integrity_error", "Assurance review packet format differs", packet_id)
    return {"id": row["id"], "digest": row["digest"], "body": body,
            "kind": row["kind"], "project": project}


def _latest_packet_receipt(control: Any, packet: dict[str, Any], role: str) -> tuple[dict[str, Any] | None, str | None]:
    rows = control.s.all(
        "SELECT id FROM receipts WHERE project=? AND subject=? AND binding=? AND role=? "
        "ORDER BY id",
        (packet["project"], packet["id"], packet["digest"], role),
    )
    if not rows:
        return None, "no_current_receipt"
    try:
        ordered = ordered_observed_receipts(
            control, project=packet["project"], subject=packet["id"],
            role=role, binding=packet["digest"],
            receipt_ids=[row["id"] for row in rows],
        )
    except Fault as exc:
        return None, exc.code
    if not ordered:
        return None, "no_current_receipt"
    latest = ordered[-1]["row"]
    try:
        body = control.g.require_review(latest["id"], packet["id"], packet["digest"], {role})
    except Fault as exc:
        raw = control.s.one("SELECT body FROM receipts WHERE id=?", (latest["id"],))
        if raw is not None:
            try:
                observed = parse_json(raw["body"])
            except Fault:
                observed = {}
            result = observed.get("result", {}) if isinstance(observed, dict) else {}
            if result.get("verdict") != "pass" or observed.get("failure") or observed.get("exit_code") != 0:
                return None, "failed:" + exc.code
        return None, exc.code
    run = control.s.one("SELECT * FROM runs WHERE id=? AND project=?", (body.get("run"), packet["project"]))
    if run is None:
        return None, "review_run_missing"
    if run.get("status") != "finished" or run.get("role") != role or run.get("binding") != packet["digest"]:
        return None, "review_run_identity_mismatch"
    return body, None


def _packet_review(control: Any, actor: Any, packet: dict[str, Any]) -> dict[str, Any]:
    body = packet["body"]
    roles = body.get("required_roles")
    if type(roles) is not list or roles != sorted(set(roles)) or not roles:
        return {"packet": {"id": packet["id"], "digest": packet["digest"]},
                "status": "unverified", "reason": "packet_roles_invalid", "receipts": []}
    receipts = []
    statuses = []
    reasons = []
    for role in roles:
        receipt, reason = _latest_packet_receipt(control, packet, role)
        if receipt is None:
            statuses.append("failed" if isinstance(reason, str) and reason.startswith("failed:") else "unverified")
            reasons.append(reason or "review_missing")
            continue
        covered = receipt.get("result", {}).get("covered", [])
        required = body.get("required_coverage", [])
        if type(covered) is not list or any(type(value) is not str for value in covered):
            statuses.append("unverified"); reasons.append("review_coverage_invalid"); continue
        if not set(required) <= set(covered):
            statuses.append("unverified"); reasons.append("required_coverage_missing"); continue
        receipts.append({"id": receipt["id"], "digest": digest(receipt),
                         "run": receipt["run"], "role": role,
                         "covered": sorted(set(covered))})
        statuses.append("satisfied")
    if "failed" in statuses:
        status = "failed"
    elif "unverified" in statuses:
        status = "unverified"
    else:
        status = "satisfied"
    return {"packet": {"id": packet["id"], "digest": packet["digest"]},
            "status": status, "reason": reasons[0] if reasons else "review_accepted",
            "receipts": receipts}


def _verify_relation_reviews(value: Any, control: Any, request: _RelationRequest,
                             project: str) -> _RelationReviews:
    if (not isinstance(value, _RelationReviews) or value._control is not control or
            value._origin is not _RELATION_REVIEWS_ORIGIN):
        raise Fault("invalid_relation_reviews", "relation_reviews must come from build_review_assurance")
    if value._seal != canonical(dict(value)):
        raise Fault("invalid_relation_reviews", "relation_reviews was modified after collection")
    required = {"format", "project", "relation", "set_ref", "edge_refs",
                "edge_reviews", "synthesis_review", "packet_reviews",
                "independent_runs", "status", "digest"}
    if set(value) != required:
        raise Fault("invalid_relation_reviews", "relation_reviews wire shape differs")
    if value["format"] != _RELATION_REVIEWS_FORMAT or value["project"] != project:
        raise Fault("invalid_relation_reviews", "relation_reviews identity differs")
    if value["relation"] != request["relation"]:
        raise Fault("invalid_relation_reviews", "relation_reviews relation differs")
    if value._actor is not request._actor:
        raise Fault("invalid_relation_reviews", "relation_reviews actor differs from request")
    if value._request_digest != request["request_digest"]:
        raise Fault("invalid_relation_reviews", "relation_reviews request binding differs")
    validate_typed_ref(value["set_ref"], project=project, expected_kinds={"assurance_object"})
    if value["set_ref"].get("object_kind") != "set":
        raise Fault("invalid_relation_reviews", "relation_reviews set_ref is not a set")
    if value["status"] not in CRITERION_STATUSES:
        raise Fault("invalid_relation_reviews", "relation_reviews status is unknown")
    if type(value["edge_refs"]) is not list or value["edge_refs"] != sorted(value["edge_refs"], key=canonical):
        raise Fault("invalid_relation_reviews", "relation_reviews edge_refs are malformed")
    edge_identities = []
    for ref in value["edge_refs"]:
        validate_typed_ref(ref, project=project, expected_kinds={"assurance_object"})
        if ref.get("object_kind") != "edge":
            raise Fault("invalid_relation_reviews", "relation_reviews edge ref is not an edge")
        edge_identities.append(canonical(_identity(ref)))
    if len(edge_identities) != len(set(edge_identities)):
        raise Fault("invalid_relation_reviews", "relation_reviews edge refs contain duplicates")
    if type(value["edge_reviews"]) is not list or type(value["packet_reviews"]) is not list:
        raise Fault("invalid_relation_reviews", "relation_reviews packet lists are malformed")
    for item in value["edge_reviews"]:
        if type(item) is not dict or set(item) != {"edge_ref", "status", "reason", "packet_reviews", "covered_obligation_ids"}:
            raise Fault("invalid_relation_reviews", "relation_reviews edge result shape differs")
        validate_typed_ref(item["edge_ref"], project=project, expected_kinds={"assurance_object"})
        if item["edge_ref"].get("object_kind") != "edge" or item["status"] not in CRITERION_STATUSES:
            raise Fault("invalid_relation_reviews", "relation_reviews edge result is invalid")
        if type(item["packet_reviews"]) is not list or type(item["covered_obligation_ids"]) is not list:
            raise Fault("invalid_relation_reviews", "relation_reviews edge result lists are invalid")
    synthesis = value["synthesis_review"]
    if type(synthesis) is not dict or set(synthesis) != {"status", "reason", "packet_reviews"}:
        raise Fault("invalid_relation_reviews", "relation_reviews synthesis shape differs")
    if synthesis["status"] not in CRITERION_STATUSES or type(synthesis["packet_reviews"]) is not list:
        raise Fault("invalid_relation_reviews", "relation_reviews synthesis is invalid")
    independent = value["independent_runs"]
    if (type(independent) is not dict or set(independent) != {"runs", "independent", "count"} or
            type(independent["runs"]) is not list or independent["runs"] != sorted(set(independent["runs"])) or
            type(independent["independent"]) is not bool or type(independent["count"]) is not int or
            independent["count"] < len(independent["runs"])):
        raise Fault("invalid_relation_reviews", "relation_reviews independent run shape differs")
    body = dict(value); body.pop("digest")
    if digest(body) != value["digest"]:
        raise Fault("integrity_error", "relation_reviews digest differs")
    return value


def build_review_assurance(control: Any, actor: Any, *, relation_request: Any,
                           set_ref: dict[str, Any], edge_refs: list[dict[str, Any]] | None = None) -> _RelationReviews:
    """Resolve actual edge and set packet receipts through Governance.

    This is deliberately a read operation.  It never accepts raw verdicts or
    caller supplied receipt bodies and never turns adoption preparation into a
    final PASS.  The set's immutable manifest determines the edge population.
    """
    if not isinstance(relation_request, _RelationRequest):
        raise Fault("invalid_relation_request", "relation_request must come from build_relation_request")
    request = _verify_relation_request(relation_request, control,
                                       project=relation_request.get("project"))
    if request._actor is not actor:
        raise Fault("invalid_relation_request", "relation_request actor differs from review actor")
    project = request["project"]
    assurance = _assurance(control)
    set_identity = validate_typed_ref(set_ref, project=project, expected_kinds={"assurance_object"})
    need(set_identity.get("object_kind") == "set", "invalid_relation_reviews", "set_ref must reference a set")
    set_row = _object_row(control, set_identity, project, {"set"})
    set_body = set_row["body"]
    need(set_body.get("relation") == request["relation"] and
         set_body.get("direction") == request["direction"] and
         _same_ref(set_body.get("scope_ref"), request["scope_ref"]) and
         _same_ref(set_body.get("center_ref"), request["center_ref"]),
         "stale_set", "Relation set does not match the sealed request")
    need(set_body.get("relation_contract_digest") == request["registry_digest"],
         "invalid_registry", "Relation set registry digest differs")
    denominator = request._denominator
    if not isinstance(denominator, dict):
        raise Fault("invalid_relation_request", "relation_request has no sealed denominator")
    denominator_alignment, denominator_reason = _set_denominator_alignment(
        control, project, set_body,
        {item["id"]: item for item in denominator.get("obligations", [])
         if isinstance(item, dict) and isinstance(item.get("id"), str)},
        request["required_obligation_ids"],
        actor,
    )
    try:
        # Review preparation is not a completed semantic result.  The set
        # must already be the adopted current head before its receipts can be
        # used by the N/E/S gate; adoption itself still performs its own
        # governed receipt check through Assurance.
        assurance._ensure_object_current(actor, project, set_row, require_self=True)
        dependency_state = "current"
    except Fault as exc:
        dependency_state = "stale" if exc.code in {"stale_reference", "stale_set", "set_incomplete"} else "unverified"

    descriptors = assurance._manifest_edge_descriptors(project,
                                                        set_body.get("selected_edge_manifest_ref") or {},
                                                        {"center_ref": set_body["center_ref"],
                                                         "relation": set_body["relation"],
                                                         "direction": set_body["direction"],
                                                         "scope_ref": set_body["scope_ref"]})
    manifest_refs = []
    edge_rows = []
    for descriptor in descriptors:
        ref = {"kind": "assurance_object", "project": project, "object": descriptor["id"],
               "object_kind": "edge", "object_digest": descriptor["digest"]}
        ref = validate_typed_ref(ref, project=project, expected_kinds={"assurance_object"})
        manifest_refs.append(_identity(ref))
        edge_rows.append(_object_row(control, ref, project, {"edge"}))
    supplied_refs = manifest_refs if edge_refs is None else [
        _identity(validate_typed_ref(item, project=project, expected_kinds={"assurance_object"}))
        for item in edge_refs
    ]
    need(sorted(supplied_refs, key=canonical) == sorted(manifest_refs, key=canonical),
         "stale_set", "Supplied edge population differs from the immutable set manifest")

    roots = []
    edge_packets: dict[str, list[dict[str, Any]]] = {}
    packet_reviews: list[dict[str, Any]] = []
    for edge in edge_rows:
        roots.append(edge)
        packets = assurance._root_packets(project, edge)
        edge_packets[edge["id"]] = packets
        for packet in packets:
            body = packet["body"]
            if body.get("review_kind") != "edge" or body.get("root_ref") != assurance._object_ref(edge):
                packet_reviews.append({"packet": {"id": packet["id"], "digest": packet["digest"]},
                                       "status": "unverified", "reason": "edge_packet_binding_mismatch", "receipts": []})
            else:
                packet_reviews.append(_packet_review(control, actor, packet))
    roots.append(set_row)
    set_packets = assurance._root_packets(project, set_row)
    synthesis_packets = []
    set_leaf_packets = []
    for packet in set_packets:
        body = packet["body"]
        if body.get("review_kind") != "relation_set" or body.get("root_ref") != assurance._object_ref(set_row):
            packet_reviews.append({"packet": {"id": packet["id"], "digest": packet["digest"]},
                                   "status": "unverified", "reason": "set_packet_binding_mismatch", "receipts": []})
        else:
            packet_result = _packet_review(control, actor, packet)
            packet_reviews.append(packet_result)
            if body.get("partition", {}).get("kind") == "synthesis" or body.get("children") is True:
                synthesis_packets.append(packet_result)
            else:
                set_leaf_packets.append(packet_result)
    by_packet = {item["packet"]["id"]: item for item in packet_reviews}
    edge_results = []
    for edge in edge_rows:
        values = [by_packet[item["id"]] for item in edge_packets.get(edge["id"], []) if item["id"] in by_packet]
        if not values:
            status, reason = "unverified", "edge_packet_missing"
        elif any(item["status"] == "failed" for item in values):
            status, reason = "failed", "edge_review_failed"
        elif any(item["status"] != "satisfied" for item in values):
            status, reason = "unverified", "edge_review_incomplete"
        else:
            status, reason = "satisfied", "edge_review_accepted"
        edge_results.append({"edge_ref": assurance._object_ref(edge), "status": status, "reason": reason,
                             "packet_reviews": [item["packet"] for item in values],
                             "covered_obligation_ids": []})
    # A leaf packet is never enough: every synthesis level and the final
    # synthesis root must have an independent actual receipt.
    if any(item["status"] == "failed" for item in set_leaf_packets):
        synthesis = {"status": "failed", "reason": "set_leaf_review_failed",
                     "packet_reviews": [item["packet"] for item in set_leaf_packets]}
    elif any(item["status"] != "satisfied" for item in set_leaf_packets):
        synthesis = {"status": "unverified", "reason": "set_leaf_review_incomplete",
                     "packet_reviews": [item["packet"] for item in set_leaf_packets]}
    elif not synthesis_packets:
        synthesis = {"status": "unverified", "reason": "synthesis_packet_missing", "packet_reviews": []}
    elif any(item["status"] == "failed" for item in synthesis_packets):
        synthesis = {"status": "failed", "reason": "synthesis_review_failed",
                     "packet_reviews": [item["packet"] for item in synthesis_packets]}
    elif any(item["status"] != "satisfied" for item in synthesis_packets):
        synthesis = {"status": "unverified", "reason": "synthesis_review_incomplete",
                     "packet_reviews": [item["packet"] for item in synthesis_packets]}
    else:
        synthesis = {"status": "satisfied", "reason": "synthesis_review_accepted",
                     "packet_reviews": [item["packet"] for item in synthesis_packets]}
    if not denominator_alignment:
        # A review over a set carrying a different or incomplete denominator
        # is retained as evidence, but cannot satisfy this request's global
        # population.  Do not rewrite the historical set or invent aliases.
        synthesis["status"] = "unverified" if synthesis["status"] == "satisfied" else synthesis["status"]
        synthesis["reason"] = denominator_reason or "set_expected_denominator_mismatch"
    runs = [receipt["run"] for item in packet_reviews for receipt in item.get("receipts", [])]
    independent = len(runs) == len(set(runs)) and bool(runs)
    if not independent:
        # Reusing one review run for multiple packets is a concrete
        # unverified condition, even when every receipt body says PASS.
        if runs:
            synthesis["status"] = "unverified" if synthesis["status"] == "satisfied" else synthesis["status"]
            synthesis["reason"] = "review_run_reused"
    if dependency_state == "stale":
        overall = "stale"
    elif any(item["status"] == "failed" for item in edge_results) or synthesis["status"] == "failed":
        overall = "failed"
    elif any(item["status"] != "satisfied" for item in edge_results) or synthesis["status"] != "satisfied":
        overall = "unverified"
    elif not independent:
        overall = "unverified"
    else:
        overall = "satisfied"
    body = {"format": _RELATION_REVIEWS_FORMAT, "project": project,
            "relation": request["relation"], "set_ref": _identity(set_identity),
            "edge_refs": sorted(manifest_refs, key=canonical),
            "edge_reviews": edge_results, "synthesis_review": synthesis,
            "packet_reviews": packet_reviews,
            "independent_runs": {"runs": sorted(set(runs)), "independent": independent,
                                 "count": len(runs)},
            "status": overall}
    body["digest"] = digest(body)
    return _RelationReviews(body, control=control, actor=actor,
                            request_digest=request["request_digest"],
                            _origin=_RELATION_REVIEWS_ORIGIN)


class _ControllerEdgeSet(list):
    """Private result of the stored-edge adapter."""

    __slots__ = (
        "_origin", "_coverage", "_failures", "_current", "_diagnostics",
        "_edge_refs", "_endpoint_resolutions", "_matches",
    )

    def __init__(self, rows: list[dict[str, Any]], *, coverage: dict[tuple[str, int, str], set[str]],
                 failures: set[str], current: dict[tuple[str, int, str], bool],
                 diagnostics: list[dict[str, Any]], edge_refs: dict[tuple[str, int, str], dict[str, Any]],
                 endpoint_resolutions: dict[tuple[str, int, str], dict[str, Any]],
                 matches: dict[tuple[str, int, str], dict[str, dict[str, Any]]],
                 _origin: object | None = None) -> None:
        if _origin is not _EDGE_ORIGIN:
            raise Fault("invalid_criteria", "Controller edge sets are private adapter results")
        list.__init__(self, rows)
        object.__setattr__(self, "_origin", _EDGE_ORIGIN)
        object.__setattr__(self, "_coverage", coverage)
        object.__setattr__(self, "_failures", failures)
        object.__setattr__(self, "_current", current)
        object.__setattr__(self, "_diagnostics", diagnostics)
        object.__setattr__(self, "_edge_refs", edge_refs)
        object.__setattr__(self, "_endpoint_resolutions", endpoint_resolutions)
        object.__setattr__(self, "_matches", matches)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("controller edge adapter metadata is immutable")
        object.__setattr__(self, name, value)


class _RelationRequest(dict):
    """Controller-only sealed population selection for Consumer-M.

    The public mapping is intentionally inspectable, but its private origin
    binds it to the control instance and to the exact context/denominator
    enumeration that produced it.  A JSON copy therefore cannot become an
    authority by supplying plausible fields or a copied digest.
    """

    __slots__ = ("_control", "_actor", "_context", "_context_seal",
                 "_denominator", "_denominator_seal", "_seal", "_origin")

    def __init__(self, value: dict[str, Any], *, control: Any, actor: Any,
                 context: dict[str, Any] | None = None,
                 denominator: dict[str, Any] | None = None,
                 _origin: object | None = None) -> None:
        if _origin is not _REQUEST_ORIGIN:
            raise Fault("invalid_relation_request", "Relation requests can only come from the controller builder")
        dict.__init__(self, copy.deepcopy(value))
        object.__setattr__(self, "_control", control)
        object.__setattr__(self, "_actor", actor)
        # Preserve the collector's origin seal.  The context itself is an
        # immutable controller snapshot; copying it to a plain dict would
        # erase the collector token and make request verification impossible.
        copied_context = copy.deepcopy(context) if context is not None else None
        object.__setattr__(self, "_context", copied_context)
        object.__setattr__(self, "_context_seal",
                           canonical(dict(copied_context)) if copied_context is not None else None)
        copied_denominator = copy.deepcopy(denominator) if denominator is not None else None
        object.__setattr__(self, "_denominator", copied_denominator)
        object.__setattr__(self, "_denominator_seal",
                           canonical(dict(copied_denominator)) if copied_denominator is not None else None)
        object.__setattr__(self, "_seal", canonical(dict(self)))
        object.__setattr__(self, "_origin", _REQUEST_ORIGIN)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("relation request metadata is immutable")
        object.__setattr__(self, name, value)


class _RelationReviews(dict):
    """Sealed result of reading actual edge/set Governance receipts."""

    __slots__ = ("_control", "_actor", "_request_digest", "_seal", "_origin")

    def __init__(self, value: dict[str, Any], *, control: Any, actor: Any,
                 request_digest: str | None = None,
                 _origin: object | None = None) -> None:
        if _origin is not _RELATION_REVIEWS_ORIGIN:
            raise Fault("invalid_relation_reviews", "Relation reviews must come from the receipt resolver")
        dict.__init__(self, copy.deepcopy(value))
        object.__setattr__(self, "_control", control)
        object.__setattr__(self, "_actor", actor)
        object.__setattr__(self, "_request_digest", request_digest)
        object.__setattr__(self, "_seal", canonical(dict(self)))
        object.__setattr__(self, "_origin", _RELATION_REVIEWS_ORIGIN)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("relation review metadata is immutable")
        object.__setattr__(self, name, value)


def _edge_key(row: dict[str, Any]) -> tuple[str, int, str]:
    return row["id"], row["revision"], row["digest"]


def _evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "edge", "id": row["id"], "revision": row["revision"],
             "digest": row["digest"]} for row in rows[:1000]]


def _diagnostic(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def _resolve_pinned(control: Any, actor: Any, ref: dict[str, Any], *,
                    allow_draft_artifact: bool = False) -> dict[str, Any]:
    assurance = getattr(control, "assurance", None)
    if assurance is None or not hasattr(assurance, "resolve_pinned"):
        raise Fault("unsupported_reference", "Controller typed-reference resolver is unavailable")
    plain = _plain_json(ref)
    validate_typed_ref(plain, project=ref.get("project"))
    # resolve_pinned performs the same exact wire validation; passing the
    # plain form avoids feeding its resolver-only derived fields back into the
    # public validator.
    try:
        return assurance.resolve_pinned(actor, plain)
    except Fault as exc:
        if not (allow_draft_artifact and plain.get("kind") == "artifact" and
                exc.code == "unresolved_reference"):
            raise
        resolved = assurance._resolve_produced_artifact_endpoint(
            plain["project"], plain, current=True,
        )
        return {
            "format": "daikibo.assurance-resolved.v1",
            "canonical_ref": plain,
            "identity_digest": digest(plain),
            "semantic_kind": "artifact",
            "dependency_refs": [], "content_refs": [],
            "current": {"state": "not_evaluated", "reasons": []},
            "authority": {"state": "not_evaluated", "reasons": []},
            "membership": [],
            "resolution": {
                "mode": "artifact", "content": {
                    "artifact": resolved["ref"]["artifact"],
                    "revision": resolved["ref"]["revision"],
                    "body": resolved["body"],
                },
                "current": True,
            },
            "context": None,
        }


def _resolution_payload(resolution: dict[str, Any]) -> Any:
    inner = resolution.get("resolution") if isinstance(resolution, dict) else None
    if not isinstance(inner, dict):
        return None
    if "content" in inner:
        return inner["content"]
    if "payload" in inner:
        return inner["payload"]
    if "result" in inner:
        return inner["result"]
    return None


def _validate_edge_wire(body: dict[str, Any], *, project: str, relation: str,
                        obligation_ids: set[str], edge_id: str,
                        registry_digest: str = REGISTRY_V1_DIGEST) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if body.get("format") != "assurance.edge.v1" or not set(body) <= _EDGE_FIELDS:
        raise Fault("invalid_criteria", "edge wire shape is not the stored controller shape", edge_id)
    if (body.get("project") != project or
            body.get("relation_contract_digest") != registry_digest):
        raise Fault("invalid_criteria", "edge project or relation contract differs", edge_id)
    validate_typed_ref(body.get("scope_ref"), project=project, expected_kinds={"assurance_object"})
    if body.get("relation") != relation:
        raise Fault("invalid_criteria", "edge relation differs from the requested registry relation", edge_id)
    source, target = validate_relation(
        relation, body.get("source_ref"), body.get("target_ref"),
        project=project, contract_digest=registry_digest,
    )
    refs = body.get("obligation_ids", [])
    if (type(refs) is not list or refs != sorted(set(refs)) or
            any(type(item) is not str for item in refs)):
        raise Fault("integrity_error", "edge obligation identity list is malformed", edge_id)
    unknown = set(refs) - obligation_ids
    if unknown:
        raise Fault("invalid_criteria", "edge names obligations outside the sealed denominator",
                    sorted(unknown))
    evidence = body.get("required_evidence_refs")
    authority = body.get("authority_refs")
    if type(evidence) is not list or type(authority) is not list:
        raise Fault("integrity_error", "edge evidence or authority list is malformed", edge_id)
    for ref in evidence + authority:
        validate_typed_ref(ref, project=project)
    normalized = dict(body)
    normalized["source_ref"] = source
    normalized["target_ref"] = target
    return normalized, source, list(refs)


def _legacy_obligation_aliases(control: Any, project: str, scope_ref: dict[str, Any],
                               obligations: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map historical Assurance obligation IDs to additive denominator IDs.

    E2 edges were persisted against the Assurance scope denominator before
    Unit2c added typed requirement/source/Task leaves.  The alias is allowed
    only when the exact endpoint, pointer and value digest identify the same
    immutable meaning.  It is never a display-label or ID-prefix fallback.
    """
    try:
        assurance = _assurance(control)
        scope = assurance._scope_from_ref(project, scope_ref)
        old_body = assurance._derive_obligations_body(project, scope)
    except (Fault, AttributeError):
        return {}
    aliases: dict[str, str] = {}
    for old in old_body.get("obligations", []) if isinstance(old_body, dict) else []:
        if not isinstance(old, dict) or not isinstance(old.get("id"), str):
            continue
        candidates = []
        for new in obligations.values():
            if old.get("kind") == "artifact_acceptance" and new.get("category") == "acceptance_condition":
                old_ref, new_ref = old.get("source_ref"), new.get("source_ref")
                locator = new_ref.get("locator") if isinstance(new_ref, dict) else None
                if (isinstance(old_ref, dict) and old_ref.get("kind") == "artifact" and
                        isinstance(locator, dict) and locator.get("ref_type") == "artifact_ac" and
                        locator.get("artifact") == old_ref.get("artifact") and
                        locator.get("revision") == old_ref.get("revision") and
                        locator.get("body_digest") == old_ref.get("body_digest") and
                        new.get("pointer") == old.get("pointer") and
                        new.get("value_digest") == old.get("value_digest")):
                    candidates.append(new["id"])
            elif old.get("kind") == "population_leaf" and new.get("category") == "population_leaf":
                if (_same_ref(old.get("source_ref"), new.get("source_ref")) and
                        old.get("value_digest") == new.get("value_digest")):
                    candidates.append(new["id"])
        if len(candidates) == 1:
            aliases[old["id"]] = candidates[0]
    return aliases


def _set_denominator_alignment(control: Any, project: str, set_body: dict[str, Any],
                               denominator: dict[str, dict[str, Any]],
                               required_ids: list[str], actor: Any | None = None) -> tuple[bool, str | None]:
    """Check that the adopted set retained the request's exact meaning set."""
    if not required_ids:
        return True, None
    expected_ref = set_body.get("expected_obligations_ref")
    if not isinstance(expected_ref, dict):
        return False, "set_expected_denominator_missing"
    try:
        expected_row = _assurance(control)._object_by_ref(expected_ref, project, kinds={"obligations"})
    except Fault as exc:
        return False, exc.code
    values = expected_row.get("body", {}).get("obligations", [])
    if not isinstance(values, list):
        return False, "set_expected_denominator_malformed"
    # Only records retained by the immutable expected-obligations object can
    # establish set membership.  A current scope re-derivation is deliberately
    # not a substitute: it could silently fill a missing saved denominator.
    available: set[str] = set()
    for saved in values:
        if not isinstance(saved, dict) or not isinstance(saved.get("id"), str):
            return False, "set_expected_denominator_malformed"
        identity = saved["id"]
        current = denominator.get(identity)
        if current is not None:
            if canonical(saved) != canonical(current):
                return False, "set_expected_denominator_identity_mismatch"
            available.add(identity)
            continue
        # Historical E2 acceptance/population IDs may be mapped only from the
        # actual saved record.  The exact owner/source/pointer/value material
        # must match one and only one current additive obligation.
        candidates = []
        for new in denominator.values():
            if saved.get("kind") == "artifact_acceptance" and new.get("category") == "acceptance_condition":
                old_ref, new_ref = saved.get("source_ref"), new.get("source_ref")
                locator = new_ref.get("locator") if isinstance(new_ref, dict) else None
                if (isinstance(old_ref, dict) and old_ref.get("kind") == "artifact" and
                        isinstance(locator, dict) and locator.get("ref_type") == "artifact_ac" and
                        locator.get("artifact") == old_ref.get("artifact") and
                        locator.get("revision") == old_ref.get("revision") and
                        locator.get("body_digest") == old_ref.get("body_digest") and
                        new.get("pointer") == saved.get("pointer") and
                        new.get("value_digest") == saved.get("value_digest")):
                    candidates.append(new["id"])
            elif saved.get("kind") == "population_leaf" and new.get("category") == "population_leaf":
                if (_same_ref(saved.get("source_ref"), new.get("source_ref")) and
                        saved.get("value_digest") == new.get("value_digest")):
                    candidates.append(new["id"])
        if len(candidates) != 1:
            return False, "set_expected_denominator_identity_unresolved"
        available.add(candidates[0])
    if set_body.get("relation") == "produced_by":
        # E2's scope obligations contain the accepted requirement leaves; the
        # additive Task structural output leaves are proven by the immutable
        # P material attached to each selected produced edge.  Carry those
        # exact declaration identities into alignment without accepting a
        # caller label or treating the draft as a meaning review.
        assurance = _assurance(control)
        actor = actor if actor is not None else getattr(control, "owner", None)
        try:
            descriptors = assurance._manifest_edge_descriptors(
                project, set_body.get("selected_edge_manifest_ref") or {},
            )
            for descriptor in descriptors:
                edge_row = control.s.one(
                    "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='edge'",
                    (descriptor.get("id"), project), True,
                )
                if edge_row is None or edge_row.get("digest") != descriptor.get("digest"):
                    continue
                edge_body = _json_body(edge_row.get("body"), "stored produced edge")
                source = edge_body.get("source_ref")
                target = edge_body.get("target_ref")
                if (semantic_kind(target) != "task_revision" or
                        semantic_kind(source) != "artifact"):
                    continue
                produced = _artifact_production_material(control, actor, source, target)
                declaration_id = produced["output"]["declaration_id"]
                for identity, item in denominator.items():
                    if (item.get("category") != "required_output" or
                            not _same_ref(item.get("source_ref"), target)):
                        continue
                    try:
                        _task_ref, declared = _task_structural_item(control, actor, item)
                    except Fault:
                        continue
                    if declared.get("id") == declaration_id:
                        available.add(identity)
        except Fault:
            # The regular alignment result below reports this as a missing
            # output obligation; the full matcher will retain the exact P
            # diagnostic when criteria evaluation seals the edge.
            pass
    missing = set(required_ids) - available
    if missing:
        return False, "set_expected_denominator_missing_obligations"
    return True, None


def _observed_execution(control: Any, actor: Any, ref: dict[str, Any]) -> tuple[bool | None, str | None, dict[str, Any] | None]:
    """Resolve and integrity-check one observed execution result.

    ``passed=False`` is a valid observed failure.  Missing or malformed
    execution material is unverified and is never treated as a pass.
    """
    try:
        resolved = _resolve_pinned(control, actor, ref)
        inner = resolved.get("resolution", {})
        observed = inner.get("content")
        if type(observed) is not dict:
            return None, "observed_result_content_missing", None
        receipt_id = ref.get("receipt")
        receipt_row = control.s.one("SELECT * FROM receipts WHERE id=? AND project=?",
                                    (receipt_id, ref.get("project")))
        run_row = control.s.one("SELECT * FROM runs WHERE id=? AND project=?",
                                (ref.get("run"), ref.get("project")))
        if receipt_row is None or run_row is None:
            return None, "observed_execution_row_missing", observed
        run_body = parse_json(run_row["body"])
        run_result = parse_json(run_row["result"])
        execution_record_consistency(run_row, run_body, run_result, receipt_row, observed)
        result = observed.get("result")
        if type(result) is not dict or type(result.get("passed")) is not bool:
            return None, "observed_result_passed_missing", observed
        if observed.get("failure") is not None or result["passed"] is False:
            return False, "observed_execution_failed", observed
        return True, None, observed
    except Fault as exc:
        return None, exc.code, None


def _observed_definition_ref(control: Any, actor: Any, ref: dict[str, Any],
                             observed: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Read the immutable controller material bound to one observed run.

    ``observed.check_id`` and ``observed.check_digest`` are execution
    telemetry.  They are useful consistency fields, but they do not identify
    which Task or Delivery definition supplied the check.  The controller
    material is the authority for that parent identity.  Reuse Runtime's
    existing E1-backed pin validator so a caller cannot supply a replacement
    payload or turn a missing material into a successful observation.
    """
    pin = observed.get("verification_material")
    if type(pin) is not dict:
        return None, None, "verification_material_missing"
    validator = getattr(getattr(control, "rt", None), "verification_materials", None)
    validate_pin = getattr(validator, "validate_stored_pin", None)
    if not callable(validate_pin):
        return None, None, "verification_material_unavailable"
    try:
        row = validate_pin(actor, ref["project"], pin, run_id=ref.get("run"))
        body = row.get("body") if isinstance(row, dict) else None
        if isinstance(body, str):
            body = parse_json(body)
        if type(body) is not dict:
            return None, None, "verification_material_body_missing"
        payload_blob = body.get("payload_blob")
        if type(payload_blob) is not str:
            return None, None, "verification_material_payload_missing"
        payload = parse_json(control.s.blob_get(payload_blob))
        if type(payload) is not dict:
            return None, None, "verification_material_payload_missing"
        definition_ref = payload.get("definition_ref")
        if type(definition_ref) is not dict:
            return None, None, "verification_definition_ref_missing"
        runtime_check_blob = payload.get("runtime_check_blob")
        if type(runtime_check_blob) is not str:
            return None, None, "verification_runtime_check_missing"
        runtime_check = parse_json(control.s.blob_get(runtime_check_blob))
        if type(runtime_check) is not dict or digest(runtime_check) != runtime_check_blob:
            return None, None, "verification_runtime_check_integrity_mismatch"
        normalized = validate_typed_ref(definition_ref, project=ref["project"])
        return _plain_json(normalized), runtime_check, None
    except Fault as exc:
        return None, None, exc.code


def _test_plan_check_semantic_identity(control: Any, actor: Any, ref: dict[str, Any],
                                       resolved: dict[str, Any]) -> dict[str, Any]:
    """Return the shared immutable identity for one validated Task check.

    A plan material pin records a capture.  ``plan_tests`` and ``Runtime.tests``
    therefore have distinct pin ids even when they captured the same frozen
    Task definition.  The nested plan pin, its CAS payload, Task dependency,
    and check digest are still resolved exactly before this helper projects
    their meaning identity for criteria comparison.
    """
    need(ref.get("kind") == "test_plan_check", "invalid_reference",
         "Task check semantic identity requires a test-plan check reference")
    resolution = resolved.get("resolution") if isinstance(resolved, dict) else None
    need(isinstance(resolution, dict) and resolution.get("mode") == "test_plan_check",
         "unresolved_reference", "Task check resolver returned no immutable definition")
    plan_ref = ref.get("plan")
    dependencies = resolution.get("dependencies")
    need(isinstance(plan_ref, dict) and isinstance(dependencies, list) and
         len(dependencies) == 1 and _same_ref(dependencies[0], plan_ref),
         "integrity_error", "Task check resolver dependency differs from its plan pin")
    payload = resolution.get("payload")
    need(isinstance(payload, dict), "unresolved_reference",
         "Task check resolver returned no test-plan payload")
    plan_body = payload.get("plan_body")
    plan_digest = payload.get("plan_digest")
    need(isinstance(plan_body, dict) and plan_digest == plan_ref.get("plan_digest") and
         payload.get("task") == plan_ref.get("task") and
         payload.get("task_revision") == plan_ref.get("task_revision"),
         "integrity_error", "Task check plan payload differs from its typed reference")
    task = control.s.one(
        "SELECT id,project,revision,body FROM tasks WHERE id=? AND project=?",
        (plan_ref.get("task"), ref.get("project")),
    )
    need(task is not None, "unresolved_reference", "Task check Task is missing", plan_ref.get("task"))
    task_body = parse_json(task["body"])
    task_ref = {
        "kind": "task_revision", "project": ref["project"],
        "task": task["id"], "revision": task["revision"],
        "definition_digest": task_definition_digest(task_body),
    }
    validate_typed_ref(task_ref, project=ref["project"], expected_kinds={"task_revision"})
    identity = validate_test_plan_definition_identity(
        payload, _plain_json(resolution.get("material_dependencies")),
        project=ref["project"], task_ref=task_ref,
        plan_body=plan_body, plan_digest=plan_digest,
    )
    check = resolution.get("content")
    need(isinstance(check, dict) and check.get("id") == ref.get("check_id") and
         digest(check) == ref.get("check_digest"),
         "integrity_error", "Task check content differs from its typed reference")
    return {
        **identity,
        "check_id": ref["check_id"],
        "check_digest": ref["check_digest"],
    }


def _acceptance_match(control: Any, actor: Any, obligation: dict[str, Any],
                      source: dict[str, Any], target: dict[str, Any]) -> tuple[bool, str | None]:
    ref = obligation.get("source_ref")
    if type(ref) is not dict or ref.get("kind") != "traceability_ref":
        return False, "acceptance_reference_kind_mismatch"
    locator = ref.get("locator")
    if type(locator) is not dict or locator.get("ref_type") != "artifact_ac":
        return False, "acceptance_locator_missing"
    endpoint = target if semantic_kind(target) == "artifact" else source if semantic_kind(source) == "artifact" else None
    if endpoint is None:
        return False, "acceptance_artifact_endpoint_missing"
    if (locator.get("artifact") != endpoint.get("artifact") or
            locator.get("revision") != endpoint.get("revision") or
            locator.get("body_digest") != endpoint.get("body_digest")):
        return False, "acceptance_endpoint_identity_mismatch"
    if (locator.get("ac_pointer") != obligation.get("pointer") or
            locator.get("ac_digest") != obligation.get("value_digest")):
        return False, "acceptance_definition_identity_mismatch"
    try:
        _resolve_pinned(control, actor, ref)
    except Fault as exc:
        return False, exc.code
    return True, None


def _check_match(control: Any, actor: Any, obligation: dict[str, Any],
                 source: dict[str, Any], target: dict[str, Any], relation: str) -> tuple[bool, bool, str | None, dict[str, Any] | None]:
    """Return ``covered, failed, reason, observed_body`` for one check edge."""
    ref = obligation.get("source_ref")
    if type(ref) is not dict or ref.get("kind") not in {"test_plan_check", "delivery_check"}:
        return False, False, "check_reference_kind_mismatch", None
    if relation != "execution_of" or not _same_ref(target, ref) or semantic_kind(source) != "observed_result":
        return False, False, "check_endpoint_identity_mismatch", None
    try:
        target_resolution = _resolve_pinned(control, actor, ref)
    except Fault as exc:
        return False, False, exc.code, None
    passed, reason, observed = _observed_execution(control, actor, source)
    if observed is None:
        return False, False, reason, None
    material_ref, runtime_check, material_reason = _observed_definition_ref(control, actor, source, observed)
    if material_ref is None:
        return False, False, material_reason, observed
    try:
        material_resolution = _resolve_pinned(control, actor, material_ref)
    except Fault as exc:
        return False, False, exc.code, observed
    if (ref.get("kind") == "test_plan_check" and
            material_ref.get("kind") == "test_plan_check"):
        try:
            target_identity = _test_plan_check_semantic_identity(
                control, actor, ref, target_resolution,
            )
            material_identity = _test_plan_check_semantic_identity(
                control, actor, material_ref, material_resolution,
            )
        except Fault as exc:
            return False, False, exc.code, observed
        if target_identity != material_identity:
            return False, False, "observed_definition_identity_mismatch", observed
    elif not _same_ref(material_ref, ref) or not _same_ref(material_ref, target):
        return False, False, "observed_definition_identity_mismatch", observed
    # The definition digest identifies the frozen check.  Runtime may adjust
    # its command (for example report paths or build inputs), so compare
    # telemetry to the pinned runtime-check CAS leaf rather than to the frozen
    # definition digest.
    if (type(runtime_check) is not dict or
            runtime_check.get("id") != ref.get("check_id") or
            observed.get("check_id") != runtime_check.get("id") or
            observed.get("check_digest") != digest(runtime_check)):
        return False, False, "observed_definition_identity_mismatch", observed
    if passed is False:
        return True, True, reason or "observed_execution_failed", observed
    if passed is not True:
        return False, False, reason or "observed_result_unresolved", observed
    return True, False, None, observed


def _traceability_decision(control: Any, project: str, population_ref: dict[str, Any], item_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read the retained Unit B decision for one immutable population leaf."""
    population = population_ref.get("revision")
    revision = control.s.one("SELECT * FROM traceability_revisions WHERE id=? AND project=?",
                             (population, project))
    if revision is None:
        return None, "population_revision_missing"
    trace = getattr(control, "traceability", None)
    if trace is None or not hasattr(trace, "_effective_status"):
        return None, "unit_b_resolver_unavailable"
    matches = []
    for row in control.s.all("SELECT * FROM traceability_decisions WHERE revision=? AND project=? ORDER BY created,id",
                             (population, project)):
        try:
            if trace._effective_status("traceability_decisions", row["id"], project) != "accepted":
                continue
            body = trace._decision_body(row)
        except Fault as exc:
            return None, exc.code
        if digest(body) != row.get("digest"):
            return None, "unit_b_decision_digest_mismatch"
        for entry in body.get("decisions", []):
            if isinstance(entry, dict) and entry.get("item") == item_id:
                matches.append({"row": row, "body": body, "entry": entry})
    if len(matches) > 1:
        return None, "unit_b_decision_ambiguous"
    if not matches:
        return None, "unit_b_decision_missing"
    value = matches[0]
    records = trace._records_for_subject(project, "traceability_decisions", value["row"]["id"], "decision_adopted")
    if len(records) != 1:
        return None, "unit_b_decision_adoption_invalid"
    value["adoption"] = records[0]
    return value, None


def _population_match(control: Any, actor: Any, obligation: dict[str, Any],
                      source: dict[str, Any], target: dict[str, Any],
                      edge_body: dict[str, Any]) -> tuple[bool, str | None]:
    ref = obligation.get("source_ref")
    if type(ref) is not dict or ref.get("kind") != "population_item":
        return False, "population_reference_kind_mismatch"
    if semantic_kind(source) != "population_item" or not _same_ref(source, ref):
        return False, "population_source_identity_mismatch"
    try:
        resolution = _resolve_pinned(control, actor, ref)
    except Fault as exc:
        return False, exc.code
    content = _resolution_payload(resolution)
    if type(content) is not dict or content.get("leaf") is not True:
        return False, "population_leaf_unresolved"
    decision, reason = _traceability_decision(control, ref["project"], ref["population"], ref["item"])
    if decision is None:
        return False, reason
    handling = decision["entry"].get("handling")
    if handling not in {"port", "replace", "exclude"}:
        return False, "unit_b_handling_unresolved"
    if handling == "exclude":
        entry = decision["entry"]
        if not isinstance(entry.get("reason"), str) or not entry["reason"]:
            return False, "unit_b_exclusion_reason_missing"
        return True, None
    contributors = obligation.get("contributors")
    if type(contributors) is not list or not contributors:
        return False, "unit_b_contributors_missing"
    evidence = edge_body.get("required_evidence_refs", [])
    evidence_keys = {canonical(_plain_json(item)) for item in evidence if isinstance(item, dict)}
    for contributor in contributors:
        if type(contributor) is not dict or not isinstance(contributor.get("task_ref"), dict):
            return False, "unit_b_contributor_invalid"
        if canonical(_plain_json(contributor["task_ref"])) not in evidence_keys:
            return False, "unit_b_contributor_evidence_missing"
    trace = getattr(control, "traceability", None)
    if trace is None or not hasattr(trace, "_task_assignment_contracts"):
        return False, "unit_b_assignment_resolver_unavailable"
    try:
        actual = trace._task_assignment_contracts(
            ref["project"], ref["population"]["revision"], [ref["item"]], current=False,
        ).get(ref["item"])
    except Fault as exc:
        return False, exc.code
    if not isinstance(actual, list) or not actual:
        return False, "unit_b_contributors_missing"
    actual_tasks = {(item.get("task"), item.get("revision"), item.get("required"))
                    for item in actual if isinstance(item, dict)}
    expected_tasks = {(item["task_ref"].get("task"), item["task_ref"].get("revision"), True)
                      for item in contributors if isinstance(item, dict) and isinstance(item.get("task_ref"), dict)}
    if actual_tasks != expected_tasks:
        return False, "unit_b_contributor_contract_mismatch"
    return True, None


def _map_obligation(control: Any, actor: Any, obligation: dict[str, Any],
                    source: dict[str, Any], target: dict[str, Any], relation: str,
                    body: dict[str, Any], request: _RelationRequest | None = None,
                    delivery_rows: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Map one edge to a closed mechanical result.

    New denominator categories all pass through the sealed request matcher.
    The legacy tuple based matchers remain adapters here so existing Unit 2b
    callers retain their exact behavior while no caller supplied claim can
    become denominator authority.
    """
    if obligation.get("category") in _NEW_CATEGORIES:
        return _relation_obligation_match(
            control, actor, obligation, source, target, relation, body, request,
            delivery_rows,
        )
    category = obligation.get("category")
    if category == "acceptance_condition":
        ok, reason = _acceptance_match(control, actor, obligation, source, target)
        return _closed_match(ok, False, reason, obligation=obligation["id"], body=body)
    if category in {"required_check", "delivery_check"}:
        ok, failed, reason, _ = _check_match(control, actor, obligation, source, target, relation)
        return _closed_match(ok, failed, reason, obligation=obligation["id"], body=body)
    if category == "population_leaf":
        ok, reason = _population_match(control, actor, obligation, source, target, body)
        return _closed_match(ok, False, reason, obligation=obligation["id"], body=body)
    return {
        "mechanical_state": "unverified", "matched_obligation_ids": [],
        "required_meaning_subjects": [],
        "diagnostics": [{"code": "unsupported_obligation_category",
                         "obligation": obligation.get("id")}],
    }


def _seal_controller_edges(edges: Any, *, control: Any, actor: Any, project: str,
                           relation: str, obligations: dict[str, dict[str, Any]],
                           relation_request: _RelationRequest | None = None) -> _ControllerEdgeSet:
    if type(edges) is not list:
        raise Fault("invalid_criteria", "edges must be a list")
    rows: list[dict[str, Any]] = []
    coverage: dict[tuple[str, int, str], set[str]] = {}
    failures: set[str] = set()
    current: dict[tuple[str, int, str], bool] = {}
    diagnostics: list[dict[str, Any]] = []
    edge_refs: dict[tuple[str, int, str], dict[str, Any]] = {}
    endpoint_resolutions: dict[tuple[str, int, str], dict[str, Any]] = {}
    matches: dict[tuple[str, int, str], dict[str, dict[str, Any]]] = {}
    assurance = getattr(control, "assurance", None)
    registry_digest = (relation_request.get("registry_digest", REGISTRY_V1_DIGEST)
                       if relation_request is not None else REGISTRY_V1_DIGEST)
    delivery_rows: dict[str, dict[str, Any]] = {}
    delivery_diagnostics: list[dict[str, Any]] = []
    requested_delivery_ids = set(relation_request.get("required_obligation_ids", [])) if relation_request is not None else set()
    delivery_family = (
        relation_request is not None and
        _relation_family(
            relation, registry_digest,
            center_ref=relation_request.get("center_ref"),
            context=getattr(relation_request, "_context", None),
        ) == "delivery"
    )
    if delivery_family:
        try:
            from .assurance_delivery import match_live_delivery_declared_outputs

            delivery_result = match_live_delivery_declared_outputs(
                control, actor, relation_request._denominator,
            )
            delivery_rows = {
                item.get("obligation_id"): item
                for item in delivery_result.get("obligations", [])
                if isinstance(item, dict) and isinstance(item.get("obligation_id"), str)
            }
            for item in delivery_result.get("obligations", []):
                if not isinstance(item, dict):
                    continue
                state = item.get("mechanical_state")
                oid = item.get("obligation_id")
                if oid not in obligations or oid not in requested_delivery_ids:
                    continue
                if state == "failed":
                    failures.add(oid)
                elif state in {"unverified", "stale"}:
                    delivery_diagnostics.extend(
                        _diagnostic(
                            "delivery_observation_unresolved", obligation=oid,
                            detail=item.get("diagnostics", []),
                        ) for _ in [0]
                    )
        except Fault as exc:
            delivery_diagnostics.append(_diagnostic(
                "delivery_match_unavailable", detail=exc.as_dict(),
            ))
    diagnostics.extend(delivery_diagnostics)
    for supplied in edges:
        if type(supplied) is not dict:
            raise Fault("invalid_criteria", "edge must be an object")
        required = {"id", "revision", "digest", "body"}
        if not required <= set(supplied):
            raise Fault("invalid_criteria", "edge identity/body fields are missing")
        edge_id = supplied.get("id")
        if (type(edge_id) is not str or not edge_id or type(supplied.get("revision")) is not int
                or supplied["revision"] < 1 or type(supplied.get("digest")) is not str):
            raise Fault("invalid_criteria", "edge identity is malformed", edge_id)
        body = _json_body(supplied.get("body"), "edge body")
        if digest(body) != supplied.get("digest"):
            raise Fault("integrity_error", "edge body digest differs", edge_id)
        raw_claimed = body.get("obligation_ids", [])
        claim_names = set(raw_claimed) if isinstance(raw_claimed, list) and all(type(item) is str for item in raw_claimed) else set()
        aliases = _legacy_obligation_aliases(
            control, project, body.get("scope_ref"), obligations,
        ) if isinstance(body.get("scope_ref"), dict) else {}
        normalized, source, claimed = _validate_edge_wire(
            body, project=project, relation=relation,
            obligation_ids=set(obligations) | claim_names,
            edge_id=edge_id, registry_digest=registry_digest,
        )
        produced_output_id = None
        if (relation == "produced_by" and
                semantic_kind(normalized.get("target_ref", {})) == "task_revision" and
                semantic_kind(source) == "artifact"):
            # E2 scope obligations predate Task structural leaves and therefore
            # expose only the accepted requirement IDs to edge_propose.  The
            # controller-owned P material carries the exact declaration ID;
            # map that historical claim to the corresponding sealed
            # required_output leaf without trusting the caller's label.
            try:
                produced = _artifact_production_material(
                    control, actor, source, normalized["target_ref"],
                )
                declaration_id = produced["output"]["declaration_id"]
                for candidate_id, candidate_obligation in obligations.items():
                    if candidate_obligation.get("category") != "required_output":
                        continue
                    if not _same_ref(candidate_obligation.get("source_ref"), normalized["target_ref"]):
                        continue
                    try:
                        _task_ref, declared = _task_structural_item(
                            control, actor, candidate_obligation,
                        )
                    except Fault:
                        continue
                    if declared.get("id") == declaration_id:
                        produced_output_id = candidate_id
                        break
            except Fault as exc:
                # The edge remains a retained mechanical claim; currentness
                # and criterion matching below will report the missing or
                # invalid P material as unresolved evidence.
                diagnostics.append(_diagnostic(
                    "edge_producer_material_unresolved", id=edge_id,
                    reason=exc.code,
                ))
        mapped_claimed = []
        for claimed_id in claimed:
            mapped = claimed_id if claimed_id in obligations else aliases.get(claimed_id)
            if (produced_output_id is not None and
                    (mapped is None or obligations.get(mapped, {}).get("category") != "required_output")):
                mapped = produced_output_id
            if mapped is None:
                diagnostics.append(_diagnostic("edge_obligation_outside_denominator",
                                               id=edge_id, obligation=claimed_id))
                continue
            mapped_claimed.append(mapped)
        # Consumer-C edges intentionally carry an empty claim list.  Their
        # coverage comes from the sealed Delivery declaration resolver and
        # exact output/producer endpoint match, never from a caller-supplied
        # obligation ID.  Select only the declaration whose controller-owned
        # output and producer refs are this edge's endpoints; mapping every
        # declaration would turn the other declarations' endpoint mismatch
        # diagnostics into a false unverified result.
        if delivery_family:
            for oid, delivery_row in (delivery_rows or {}).items():
                if oid not in obligations or not isinstance(delivery_row, dict):
                    continue
                if delivery_row.get("mechanical_state") != "eligible":
                    continue
                output_ref = delivery_row.get("output_ref")
                producer_ref = delivery_row.get("producer_ref")
                endpoint_match = False
                if relation == "produced_by":
                    endpoint_match = (
                        isinstance(output_ref, dict) and isinstance(producer_ref, dict) and
                        _same_ref(normalized.get("source_ref"), output_ref) and
                        _same_ref(normalized.get("target_ref"), producer_ref)
                    )
                elif relation == "contains":
                    endpoint_match = (
                        isinstance(output_ref, dict) and
                        _same_ref(normalized.get("target_ref"), output_ref)
                    )
                if endpoint_match:
                    mapped_claimed.append(oid)
        stored = None
        if assurance is not None:
            stored = control.s.one(
                "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='edge'",
                (edge_id, project),
            )
        if stored is None:
            diagnostics.append(_diagnostic("edge_not_controller_stored", id=edge_id))
            continue
        key = (edge_id, supplied["revision"], supplied["digest"])
        if stored.get("revision") != supplied["revision"] or stored.get("digest") != supplied["digest"]:
            diagnostics.append(_diagnostic("edge_identity_differs", id=edge_id))
            continue
        stored_body = _json_body(stored.get("body"), "stored edge body")
        if digest(stored_body) != stored.get("digest"):
            diagnostics.append(_diagnostic("stored_edge_digest_invalid", id=edge_id))
            continue
        if canonical(_plain_json(normalized)) != canonical(_plain_json(stored_body)):
            diagnostics.append(_diagnostic("edge_body_differs_from_controller", id=edge_id))
            continue
        try:
            stored_normalized, source, claimed = _validate_edge_wire(
                stored_body, project=project, relation=relation,
                obligation_ids=set(obligations) | set(stored_body.get("obligation_ids", [])), edge_id=edge_id,
                registry_digest=registry_digest,
            )
            if assurance is not None and hasattr(assurance, "_validate_edge_semantics"):
                # ``store_object`` is the immutable storage primitive and can
                # be used by fixtures without passing through proposal.  The
                # adapter still applies the controller's endpoint semantics
                # before treating the stored row as evidence.
                assurance._validate_edge_semantics(
                    actor, project, relation, source,
                    stored_normalized["target_ref"], registry_digest,
                )
                scope_object = assurance._object_by_ref(
                    stored_normalized["scope_ref"], project,
                    kinds={"scope", "profile"},
                )
                if scope_object["kind"] == "profile":
                    selectors = scope_object["body"].get("relation_selectors", [])
                    if relation not in selectors:
                        raise Fault("invalid_relation", "Relation is outside the selected profile", relation)
                    if (relation == "execution_of" and
                            semantic_kind(stored_normalized["target_ref"]) == "artifact"):
                        bindings = scope_object["body"].get("test_definition_bindings", [])
                        if not any(
                                _same_ref(item.get("artifact_ref"), stored_normalized["target_ref"])
                                for item in bindings if isinstance(item, dict)):
                            raise Fault(
                                "invalid_relation_endpoint",
                                "Execution test artifact is not bound by the selected profile",
                            )
            endpoints = {
                "source": _resolve_pinned(
                    control, actor, source,
                    allow_draft_artifact=(relation == "produced_by" and
                                           semantic_kind(source) == "artifact"),
                ),
                "target": _resolve_pinned(control, actor, stored_normalized["target_ref"]),
                "scope": _resolve_pinned(control, actor, stored_normalized["scope_ref"]),
            }
            for ref in stored_normalized.get("required_evidence_refs", []):
                _resolve_pinned(control, actor, ref)
            for ref in stored_normalized.get("authority_refs", []):
                _resolve_pinned(control, actor, ref)
            if relation_request is not None:
                center = relation_request["center_ref"]
                endpoint = (stored_normalized["source_ref"]
                            if relation_request["direction"] == "outgoing"
                            else stored_normalized["target_ref"])
                if not (_same_ref(endpoint, center) or
                        bool(assurance._member_of(actor, project, center, endpoint))):
                    raise Fault("invalid_relation_request",
                                "Edge endpoint is outside the sealed relation population", edge_id)
        except Fault as exc:
            diagnostics.append(_diagnostic(
                "edge_endpoint_unresolved", id=edge_id, reason=exc.code,
                detail=str(exc),
            ))
            continue
        row = {"id": stored["id"], "revision": stored["revision"],
               "digest": stored["digest"], "body": stored_normalized}
        rows.append(row)
        coverage[key] = set()
        edge_refs[key] = {
            "kind": "assurance_object", "project": project, "object": stored["id"],
            "object_kind": "edge", "object_digest": stored["digest"],
        }
        endpoint_resolutions[key] = endpoints
        matches[key] = {}
        # The head event is only the local CAS projection.  Ask the existing
        # controller currentness walker to re-check the edge's typed
        # dependencies as well; a caller supplied ``current`` flag or a
        # matching edge digest cannot promote stale endpoint material.
        is_current = False
        if assurance is not None and hasattr(assurance, "_ensure_object_current"):
            try:
                current_row = dict(stored)
                current_row["body"] = stored_body
                assurance._ensure_object_current(actor, project, current_row, require_self=True)
                is_current = True
            except Fault:
                is_current = False
        elif assurance is not None and hasattr(assurance, "_object_is_current"):
            is_current = bool(assurance._object_is_current(stored))
        current[key] = is_current
        for oid in sorted(set(mapped_claimed)):
            obligation = obligations.get(oid)
            if obligation is None:
                continue
            mapped = _map_obligation(
                control, actor, obligation, stored_normalized["source_ref"],
                stored_normalized["target_ref"], relation, stored_normalized,
                relation_request, delivery_rows=delivery_rows,
            )
            matches[key][oid] = mapped
            if mapped["mechanical_state"] == "eligible":
                coverage[key].add(oid)
            if mapped["mechanical_state"] == "failed":
                failures.add(oid)
            for detail in mapped.get("diagnostics", []):
                diagnostic = dict(detail)
                diagnostic.setdefault("obligation", oid)
                reason = diagnostic.pop("code", "unknown_obligation_diagnostic")
                diagnostics.append(_diagnostic("edge_obligation_unresolved", id=edge_id,
                                               reason=reason, **diagnostic))
    return _ControllerEdgeSet(
        rows, coverage=coverage, failures=failures, current=current,
        diagnostics=diagnostics, edge_refs=edge_refs,
        endpoint_resolutions=endpoint_resolutions, matches=matches,
        _origin=_EDGE_ORIGIN,
    )


def _coverage_result(obligations: list[dict[str, Any]], edge_set: _ControllerEdgeSet,
                     categories: set[str]) -> dict[str, Any]:
    required = [item["id"] for item in obligations if item.get("category") in categories]
    observed: set[str] = set()
    for covered in edge_set._coverage.values():
        observed.update(covered.intersection(required))
    missing = sorted(set(required) - observed)
    evidence = _evidence(list(edge_set))
    if edge_set._failures.intersection(required):
        return _criterion("failed", required, sorted(observed), missing, evidence)
    if edge_set._diagnostics:
        return _criterion("unverified", required, sorted(observed), missing, evidence)
    if not required:
        return _criterion("unsupported", [], [], [], evidence)
    if missing:
        return _criterion("missing", required, sorted(observed), missing, evidence)
    return _criterion("satisfied", required, sorted(observed), [], evidence)


def _node_review_status(origin: dict[str, Any], expected_refs: set[str] | None = None) -> tuple[str, list[str]]:
    """Collapse selected N reviews without treating an empty selection as PASS.

    When a relation request supplies a population, only the exact typed node
    identities required by that population can satisfy N.  A caller-supplied
    review for an unrelated node is retained as evidence but cannot discharge
    a missing owner/endpoint review.
    """
    statuses: list[str] = []
    reasons: list[str] = []
    observed_refs: set[str] = set()
    for item in origin.get("items", []):
        ref = item.get("node_ref") if isinstance(item, dict) else None
        ref_key = canonical(_identity(ref)).decode("utf-8") if isinstance(ref, dict) else None
        if ref_key is not None:
            observed_refs.add(ref_key)
        if expected_refs is not None and ref_key not in expected_refs:
            continue
        roles = item.get("roles", {}) if isinstance(item, dict) else {}
        for role, result in roles.items():
            status = result.get("status") if isinstance(result, dict) else None
            if status not in CRITERION_STATUSES:
                statuses.append("unverified")
                reasons.append(f"{role}:invalid_status")
            else:
                statuses.append(status)
                if status != "satisfied":
                    reasons.append(f"{role}:{result.get('reason', status)}")
    if expected_refs is not None:
        missing_refs = sorted(expected_refs - observed_refs)
        if missing_refs:
            reasons.append("node_reviews_missing:" + ",".join(missing_refs))
            statuses.append("unverified")
    if not statuses:
        return "unverified", ["node_reviews_missing"]
    if "failed" in statuses:
        return "failed", reasons
    if "stale" in statuses:
        return "stale", reasons
    if "missing" in statuses:
        return "missing", reasons
    if any(status in {"unverified", "unsupported"} for status in statuses):
        return "unverified", reasons
    return "satisfied", reasons


def _new_coverage_result(name: str, relation: str, obligations: list[dict[str, Any]],
                         edge_set: _ControllerEdgeSet, origin: dict[str, Any],
                         relation_reviews: _RelationReviews | None,
                         request: _RelationRequest) -> dict[str, Any]:
    """Evaluate one Consumer-M population with the N/E/S AND gate.

    ``_coverage`` is only the mechanical M projection.  A criterion reaches
    ``satisfied`` after every required obligation has an eligible edge, a
    current independent edge review, all selected node reviews are current,
    and the sealed set synthesis receipt is accepted.  The distinction keeps
    local packet preparation from becoming a global semantic PASS.
    """
    registry_digest = request.get("registry_digest", REGISTRY_V1_DIGEST)
    categories = _criterion_categories(
        name, registry_digest, relation=relation,
        center_ref=request.get("center_ref"),
        context=getattr(request, "_context", None),
    )
    requested_ids = set(request.get("required_obligation_ids", []))
    required = [item["id"] for item in obligations
                if item.get("id") in requested_ids and item.get("category") in categories]
    required_set = set(required)
    observed: set[str] = set()
    obligation_edges: dict[str, list[dict[str, Any]]] = {oid: [] for oid in required}
    for key, covered in edge_set._coverage.items():
        refs = edge_set._edge_refs.get(key)
        for oid in covered.intersection(required_set):
            observed.add(oid)
            if refs is not None:
                obligation_edges.setdefault(oid, []).append(refs)
    missing = sorted(required_set - observed)
    # A shared requirement obligation is covered only when every canonical
    # contributor Task has an exact assigned_to edge.  One contributor's edge
    # cannot discharge the global denominator for the other contributors.
    if relation == "assigned_to":
        by_id = {item.get("id"): item for item in obligations if isinstance(item, dict)}
        owners_by_id = {
            item.get("obligation_id"): item.get("owners", [])
            for item in request.get("owner_mapping", [])
            if isinstance(item, dict)
        }
        edge_rows_by_ref = {
            (row.get("id"), row.get("digest")): row
            for row in edge_set
            if isinstance(row, dict)
        }
        for oid in sorted(required_set):
            obligation = by_id.get(oid)
            if not isinstance(obligation, dict) or obligation.get("category") != "requirement":
                continue
            expected_tasks = {
                canonical(_identity(owner))
                for owner in owners_by_id.get(oid, [])
                if isinstance(owner, dict) and owner.get("kind") == "task_revision"
            }
            observed_tasks = set()
            for edge_ref in obligation_edges.get(oid, []):
                row = edge_rows_by_ref.get((edge_ref.get("object"), edge_ref.get("object_digest")))
                if row is not None:
                    target_ref = row.get("body", {}).get("target_ref")
                    if isinstance(target_ref, dict) and target_ref.get("kind") == "task_revision":
                        observed_tasks.add(canonical(_identity(target_ref)))
            if not expected_tasks or not expected_tasks <= observed_tasks:
                observed.discard(oid)
                if oid not in missing:
                    missing.append(oid)
        missing = sorted(set(missing))
    evidence = _evidence(list(edge_set))
    if relation_reviews is not None:
        for item in relation_reviews.get("packet_reviews", []):
            if isinstance(item, dict) and isinstance(item.get("packet"), dict):
                evidence.append({"kind": "packet", **item["packet"]})
    expected_nodes = _expected_node_refs(request, obligations, edge_set)
    # Delivery declarations and checks are controller execution material, not
    # artifact/Task nodes in the Unit 2b node-review vocabulary.  For the
    # v2 output relation an empty typed-node population is therefore the
    # sealed N boundary; E and S remain mandatory below.  Other relations
    # retain the strict empty-selection/unverified behavior.
    if (not expected_nodes and registry_digest == REGISTRY_V2_DIGEST and
            DELIVERY_DECLARED_OUTPUT_CATEGORY in _relation_categories(
                relation, registry_digest, center_ref=request.get("center_ref"),
                context=getattr(request, "_context", None))):
        node_status, node_reasons = "satisfied", []
    else:
        node_status, node_reasons = _node_review_status(origin, expected_nodes)

    if edge_set._failures.intersection(required_set):
        return _criterion("failed", required, sorted(observed), missing, evidence)
    if edge_set._diagnostics:
        # A malformed/foreign edge or an unresolved typed obligation is an
        # unresolved controller population, rather than an empty denominator.
        return _criterion("unverified", required, sorted(observed), missing, evidence)
    if missing:
        return _criterion("missing", required, sorted(observed), missing, evidence)
    if relation_reviews is None:
        return _criterion("unverified", required, sorted(observed), [], evidence)
    if relation_reviews.get("status") == "failed":
        return _criterion("failed", required, sorted(observed), [], evidence)
    if relation_reviews.get("status") == "stale":
        return _criterion("stale", required, sorted(observed), [], evidence)
    if relation_reviews.get("status") != "satisfied":
        return _criterion("unverified", required, sorted(observed), [], evidence)
    edge_review_by_ref = {
        canonical(_identity(item.get("edge_ref"))): item
        for item in relation_reviews.get("edge_reviews", [])
        if isinstance(item, dict) and isinstance(item.get("edge_ref"), dict)
    }
    for oid in required:
        refs = obligation_edges.get(oid, [])
        if not refs:
            return _criterion("unverified", required, sorted(observed), [], evidence)
        reviews = [edge_review_by_ref.get(canonical(_identity(ref))) for ref in refs]
        reviews = [item for item in reviews if item is not None]
        if not reviews:
            return _criterion("unverified", required, sorted(observed), [], evidence)
        if any(item.get("status") == "failed" for item in reviews):
            return _criterion("failed", required, sorted(observed), [], evidence)
        if any(item.get("status") == "stale" for item in reviews):
            return _criterion("stale", required, sorted(observed), [], evidence)
        if any(item.get("status") != "satisfied" for item in reviews):
            return _criterion("unverified", required, sorted(observed), [], evidence)
    if node_status != "satisfied":
        return _criterion(node_status, required, sorted(observed), [], evidence)
    if not required:
        # Explicit empty is accepted only after the controller supplied both
        # N and S evidence.  A zero edge list itself is never enough.
        return _criterion("satisfied", [], [], [], evidence)
    return _criterion("satisfied", required, sorted(observed), [], evidence)


def _node_review_shape(validated_reviews: Any, control: Any, project: str) -> dict[str, Any]:
    origin = validated_reviews_origin(validated_reviews, control)
    if origin["project"] != project:
        raise Fault("cross_project", "Validated node reviews belong to another project")
    for item in origin["items"]:
        if type(item) is not dict or set(item) != {"node_ref", "selector", "subject", "binding", "semantic_context_digest", "roles"}:
            raise Fault("invalid_validated_reviews", "NodeResult shape differs")
        roles = item["roles"]
        if type(roles) is not dict:
            raise Fault("invalid_validated_reviews", "NodeResult roles are not an object")
        for result in roles.values():
            if type(result) is not dict or set(result) != {"status", "selected_receipt", "binding", "semantic_context_digest", "reason", "evidence_refs"}:
                raise Fault("invalid_validated_reviews", "NodeResult role result shape differs")
            if result["status"] not in CRITERION_STATUSES:
                raise Fault("invalid_validated_reviews", "NodeResult status is unknown")
    return origin


def _expected_node_refs(request: _RelationRequest,
                        obligations: list[dict[str, Any]],
                        edge_set: _ControllerEdgeSet) -> set[str]:
    """Return exact artifact/Task node identities needed by N for a request."""
    refs: list[dict[str, Any]] = []

    def add(ref: Any) -> None:
        if isinstance(ref, dict) and ref.get("kind") in {"artifact", "task_revision"}:
            refs.append(_identity(ref))

    add(request.get("center_ref"))
    required = set(request.get("required_obligation_ids", []))
    by_id = {item.get("id"): item for item in obligations if isinstance(item, dict)}
    for oid in required:
        item = by_id.get(oid)
        if item is not None:
            add(item.get("source_ref"))
        for owner in request.get("owner_mapping", []):
            if isinstance(owner, dict) and owner.get("obligation_id") == oid:
                for ref in owner.get("owners", []):
                    add(ref)
    for row in edge_set:
        body = row.get("body", {}) if isinstance(row, dict) else {}
        # A P-produced output is a Knowledge draft until a separate meaning
        # workflow accepts it.  Its producer material is mechanically checked
        # by M, while N reviews the retained Task/declaration context; asking
        # for a draft artifact N review would silently turn meaning review into
        # an acceptance operation.
        source_ref = body.get("source_ref")
        if not (body.get("relation") == "produced_by" and
                isinstance(source_ref, dict) and semantic_kind(source_ref) == "artifact"):
            add(source_ref)
        add(body.get("target_ref"))
    return {canonical(ref).decode("utf-8") for ref in refs}


def _edge_node_refs(edge_set: _ControllerEdgeSet) -> set[str]:
    """Return exact node identities for the legacy no-request path."""
    refs: list[dict[str, Any]] = []
    for row in edge_set:
        body = row.get("body", {}) if isinstance(row, dict) else {}
        for field in ("source_ref", "target_ref"):
            ref = body.get(field)
            if isinstance(ref, dict) and ref.get("kind") in {"artifact", "task_revision"}:
                refs.append(_identity(ref))
    return {canonical(ref).decode("utf-8") for ref in refs}


def _denominator_input(denominator: Any) -> tuple[dict[str, Any] | None, str | None]:
    # Unit 2a's sealed denominator evolved from v1 to the v2 context/impact
    # union.  Reuse the same validator and keep the Unit 2b criteria adapter
    # format-agnostic across that published denominator revision; this does
    # not broaden any criterion category or turn unsupported categories into
    # PASS.
    if (isinstance(denominator, dict) and
            denominator.get("format") in {DENOMINATOR_FORMAT, DENOMINATOR_V3_FORMAT, DENOMINATOR_V4_FORMAT}):
        return _validate_denominator(denominator), None
    if isinstance(denominator, dict) and denominator.get("format") == "assurance.task-projection.v1":
        _validate_projection(denominator)
        return None, "task_projection_origin_adapter_unconnected"
    raise Fault("invalid_criteria", "denominator must be a validated Unit 2a denominator or projection")


def _top_status(results: dict[str, dict[str, Any]]) -> str:
    statuses = [value["status"] for value in results.values()]
    if all(status == "satisfied" for status in statuses) and statuses:
        return "satisfied"
    for status in ("failed", "stale", "missing", "unverified", "unsupported"):
        if status in statuses:
            return status
    return "unsupported"


def evaluate_criteria(*, relation: str, requirements: list[str], denominator: Any,
                      edges: list[dict[str, Any]], validated_reviews: Any,
                      relation_request: Any = None,
                      relation_reviews: Any = None) -> dict[str, Any]:
    """Evaluate finite obligations using only controller-resolved evidence."""
    requested_registry = (
        relation_request.get("registry_digest", REGISTRY_V1_DIGEST)
        if isinstance(relation_request, _RelationRequest) else REGISTRY_V1_DIGEST
    )
    entry = registry_entry(relation, contract_digest=requested_registry)
    expected_requirements = sorted(SET_UNIVERSAL_CRITERIA | set(entry["set_checks"]))
    if type(requirements) is not list or requirements != sorted(set(requirements)):
        raise Fault("invalid_criteria", "requirements must be sorted and unique")
    if requirements != expected_requirements:
        raise Fault("invalid_criteria", "requirements do not equal the registry obligations", {
            "expected": expected_requirements, "actual": requirements,
        })
    denominator_value, denominator_reason = _denominator_input(denominator)
    if denominator_value is None:
        criteria = {name: _unresolved("unsupported", denominator_reason or "denominator_adapter_unconnected")
                    for name in requirements}
        return {"format": "assurance.criteria-results.v1", "relation": relation,
                "requirements": requirements, "criteria": criteria,
                "status": "unsupported", "capabilities": {"denominator": False,
                "reason": denominator_reason}}
    project = denominator_value["project"]
    origin = _node_review_shape(validated_reviews, getattr(validated_reviews, "_control", None), project)
    control, actor = origin["control"], origin["actor"]
    request = None
    new_requirements = [name for name in requirements if name in _CRITERION_CATEGORIES]
    if relation_request is not None:
        request = _verify_relation_request(
            relation_request, control, project=project,
            relation=relation, denominator=denominator_value,
        )
        if request._actor is not actor:
            raise Fault("invalid_relation_request", "relation_request actor differs from node reviews")
    elif new_requirements:
        # Keep the old API callable, but never let its raw edges promote a
        # newly added denominator family.  Each new family reports its own
        # unverified input boundary below.
        request = None
    reviews = None
    if relation_reviews is not None:
        if request is None:
            raise Fault("invalid_relation_reviews", "relation_reviews requires a sealed relation_request")
        reviews = _verify_relation_reviews(relation_reviews, control, request, project)
        if reviews._actor is not actor:
            raise Fault("invalid_relation_reviews", "relation_reviews actor differs from node reviews")
    obligations = denominator_value["obligations"]
    by_id = {item["id"]: item for item in obligations}
    edge_set = _seal_controller_edges(
        edges, control=control, actor=actor, project=project,
        relation=relation, obligations=by_id, relation_request=request,
    )
    rows = list(edge_set)
    evidence = _evidence(rows)
    identities = [_edge_key(row) for row in rows]
    complete_empty = False
    if (relation == "implements" and request is not None and not request["required_obligation_ids"]
            and not edge_set._diagnostics and reviews is not None
            and reviews.get("status") == "satisfied"):
        assurance = _assurance(control)
        scope = assurance._scope_from_ref(project, request["scope_ref"])
        if scope["body"].get("format") == "assurance.scope.v2":
            pair = assurance._obligations_for_scope(project, scope)
            complete_empty = (pair is not None and assurance._decode_object(pair)["body"].get("enumeration_status") == "complete")
    criteria: dict[str, dict[str, Any]] = {}
    for name in requirements:
        if name == "no_duplicate_identity":
            status = "unverified" if edge_set._diagnostics else ("satisfied" if (rows or complete_empty) and len(identities) == len(set(identities)) else "unsupported" if not rows else "failed")
            criteria[name] = _criterion(status, [], ["%s@%s:%s" % identity for identity in identities], [], evidence)
            continue
        if name == "no_self_edge":
            self_edges = [row["id"] for row in rows if _same_ref(row["body"].get("source_ref"), row["body"].get("target_ref"))]
            status = "unverified" if edge_set._diagnostics else ("failed" if self_edges else ("satisfied" if rows else "unsupported"))
            criteria[name] = _criterion(status, [], [row["id"] for row in rows], self_edges, evidence)
            continue
        if name == "all_edges_current":
            if edge_set._diagnostics:
                criteria[name] = _unresolved("unverified", "controller_edge_adapter_unresolved")
            elif not rows and complete_empty:
                criteria[name] = _criterion("satisfied", [], [], [], evidence)
            elif not rows:
                criteria[name] = _unresolved("unsupported", "edge_current_resolver_unconnected")
            else:
                stale = [row["id"] for row in rows if not edge_set._current.get(_edge_key(row), False)]
                criteria[name] = _criterion("stale" if stale else "satisfied", [], [row["id"] for row in rows], stale, evidence)
            criteria[name]["evidence_refs"] = evidence
            continue
        if name == "all_obligations_covered":
            required = [item["id"] for item in obligations
                        if request is None or item["id"] in set(request.get("required_obligation_ids", []))]
            observed = sorted({oid for covered in edge_set._coverage.values() for oid in covered})
            missing = sorted(set(required) - set(observed))
            if edge_set._failures:
                criteria[name] = _criterion("failed", required, observed, missing, evidence)
            elif edge_set._diagnostics:
                criteria[name] = _criterion("unverified", required, observed, missing, evidence)
            elif not required:
                criteria[name] = (_criterion("satisfied", [], [], [], evidence) if complete_empty else
                    _unresolved("unverified", "empty_denominator_requires_controller_empty_scope_review"))
            else:
                criteria[name] = _criterion("missing" if missing else "satisfied", required, observed, missing, evidence)
            continue
        if name == "meaning_review":
            if reviews is None:
                criteria[name] = _unresolved("unverified", "relation_review_adapter_unconnected")
            elif reviews.get("status") in {"failed", "stale"}:
                criteria[name] = _unresolved(reviews["status"], "relation_review_not_accepted")
            elif reviews.get("status") == "satisfied":
                expected_nodes = (
                    _expected_node_refs(request, obligations, edge_set)
                    if request is not None else _edge_node_refs(edge_set)
                )
                if (not expected_nodes and request is not None and
                        request.get("registry_digest") == REGISTRY_V2_DIGEST and
                        DELIVERY_DECLARED_OUTPUT_CATEGORY in _relation_categories(
                            relation, REGISTRY_V2_DIGEST,
                            center_ref=request.get("center_ref"),
                            context=getattr(request, "_context", None))):
                    node_status = "satisfied"
                else:
                    node_status, _ = _node_review_status(origin, expected_nodes)
                criteria[name] = _unresolved(node_status, "node_reviews_not_accepted") if node_status != "satisfied" else _criterion("satisfied", [], [], [], evidence)
            else:
                criteria[name] = _unresolved("unverified", "relation_review_not_accepted")
            criteria[name]["evidence_refs"] = evidence
            continue
        if name == "independent_synthesis":
            if reviews is None:
                criteria[name] = _unresolved("unverified", "synthesis_review_adapter_unconnected")
            else:
                synthesis = reviews.get("synthesis_review", {})
                status = synthesis.get("status") if isinstance(synthesis, dict) else None
                if status not in CRITERION_STATUSES:
                    status = "unverified"
                if status == "satisfied" and not reviews.get("independent_runs", {}).get("independent"):
                    status = "unverified"
                criteria[name] = _unresolved(status, "synthesis_review_not_accepted")
            criteria[name]["evidence_refs"] = evidence
            continue
        if name == "all_acceptance_conditions":
            selected = [item for item in obligations if request is None or
                        item["id"] in set(request.get("required_obligation_ids", []))]
            criteria[name] = _coverage_result(selected, edge_set, {"acceptance_condition"})
            continue
        if name == "all_required_checks":
            selected = [item for item in obligations if request is None or
                        item["id"] in set(request.get("required_obligation_ids", []))]
            criteria[name] = _coverage_result(selected, edge_set, {"required_check", "delivery_check"})
            continue
        if name == "all_population_leaves":
            selected = [item for item in obligations if request is None or
                        item["id"] in set(request.get("required_obligation_ids", []))]
            criteria[name] = _coverage_result(selected, edge_set, {"population_leaf"})
            continue
        if name in _CRITERION_CATEGORIES:
            if request is None:
                criteria[name] = _unresolved("unverified", "relation_request_required")
            else:
                criteria[name] = _new_coverage_result(
                    name, relation, obligations, edge_set, origin, reviews, request,
                )
            criteria[name]["evidence_refs"] = evidence
            continue
        if name in _UNSUPPORTED_PREFIXES:
            criteria[name] = _unresolved("unsupported", "controller_denominator_category_unconnected")
            criteria[name]["evidence_refs"] = evidence
            continue
        criteria[name] = _unresolved("unsupported", "criterion_adapter_unconnected")
        criteria[name]["evidence_refs"] = evidence
    return {"format": "assurance.criteria-results.v1", "relation": relation,
            "requirements": requirements, "criteria": criteria,
            "status": _top_status(criteria),
            "capabilities": {"denominator": True, "edge_current": True,
                             "synthesis": False, "registry_digest": requested_registry,
                             "controller_edge_adapter": True,
                             "adapter_diagnostics": edge_set._diagnostics[:1000]}}


__all__ = ["build_relation_request", "build_review_assurance", "evaluate_criteria"]
