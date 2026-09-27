"""Mechanical contracts for the edge assurance relation registry.

This module deliberately stops at identity and storage.  It does not decide
whether a reviewer should adopt an edge or a relation set.  The registry is a
small, immutable input to that later decision surface and is therefore kept
independent from the workflow implementation.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .common import Fault, canonical, digest, need


# E1's identity-only registry is retained in historical objects.  E2 adds the
# syntactic endpoint and stage/criterion contract required to make a proposal
# reviewable; its digest is therefore deliberately a new contract revision.
RELATION_CONTRACT_VERSION = "daikibo.assurance-relation-contract.v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _project(value: Any, expected: str | None) -> None:
    need(isinstance(value, str) and bool(value) and "\x00" not in value,
         "invalid_reference", "Reference project is invalid")
    if expected is not None:
        need(value == expected, "cross_project", "Reference belongs to another project", value)


def _string(value: Any, name: str) -> None:
    need(isinstance(value, str) and bool(value) and "\x00" not in value,
         "invalid_reference", f"{name} is invalid")


def _revision(value: Any, name: str) -> None:
    need(type(value) is int and value >= 1, "invalid_reference", f"{name} must be an integer >= 1")


def _sha(value: Any, name: str) -> None:
    need(isinstance(value, str) and _SHA256.fullmatch(value),
         "invalid_reference", f"{name} must be a lowercase SHA-256")


def _oid(value: Any, object_format: str, name: str) -> None:
    """Validate a Git object ID without treating it as a generic SHA-256."""
    need(object_format in {"sha1", "sha256"}, "invalid_reference", "Git object format is invalid")
    width = 40 if object_format == "sha1" else 64
    need(isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{width}}}", value),
         "invalid_reference", f"{name} is not a {object_format} object ID")


def _nonnegative(value: Any, name: str) -> None:
    need(type(value) is int and value >= 0, "invalid_reference", f"{name} must be an integer >= 0")


def _entry(name: str, source: tuple[str, ...], target: tuple[str, ...],
           checks: tuple[str, ...], sets: tuple[str, ...], cycle: str,
           stage: str, evidence: tuple[str, ...]) -> dict[str, Any]:
    return {
        "relation": name,
        "contract_version": RELATION_CONTRACT_VERSION,
        "source_kinds": list(source),
        "target_kinds": list(target),
        "edge_checks": list(checks),
        "set_checks": list(sets),
        "cycle_rule": cycle,
        "default_stage": stage,
        "evidence_requirements": list(evidence),
    }


# The order is frozen because it is part of the registry digest and the
# archive/API compatibility contract.
RELATION_REGISTRY: tuple[dict[str, Any], ...] = (
    _entry("extracted_from", ("artifact",), ("traceability_ref", "source_span"),
           ("same_project", "pinned_source_span", "source_content", "semantic_artifact_kind"),
           ("all_source_spans", "no_duplicate_identity", "meaning_review"), "proof_dag", "plan",
           ("source_span_material",)),
    _entry("decomposes", ("artifact",), ("artifact",),
           ("same_project", "same_revision_domain", "requirement_artifact"),
           ("all_child_obligations", "no_self_edge", "meaning_review"), "proof_dag", "plan",
           ("requirement_identity",)),
    _entry("realizes", ("artifact",), ("artifact",),
           ("same_project", "pinned_target", "design_to_requirement"),
           ("all_acceptance_conditions", "no_duplicate_identity", "meaning_review"), "proof_dag", "plan",
           ("design_identity",)),
    _entry("implements", ("candidate_symbol", "traceability_ref", "git_file", "git_symbol"), ("artifact",),
           ("same_project", "pinned_source", "pinned_target", "implementation_target"),
           ("all_responsibilities", "no_duplicate_identity", "meaning_review"), "proof_dag", "task",
           ("source_identity",)),
    _entry("verifies", ("artifact", "test_plan_check", "delivery_check"), ("artifact",),
           ("same_project", "pinned_target", "test_target", "acceptance_pointer"),
           ("all_acceptance_conditions", "no_duplicate_identity", "meaning_review"), "proof_dag", "plan",
           ("test_artifact_identity",)),
    _entry("exercises", ("artifact", "test_plan_check", "delivery_check"), ("candidate_symbol", "traceability_ref", "git_file", "git_symbol"),
           ("same_project", "pinned_target", "test_target", "implementation_membership"),
           ("all_required_paths", "no_duplicate_identity", "meaning_review"), "proof_dag", "task",
           ("test_artifact_identity",)),
    _entry("execution_of", ("observed_result",), ("artifact", "test_plan_check", "delivery_check"),
           ("same_project", "pinned_source", "pinned_target", "execution_material", "result_identity"),
           ("all_required_checks", "no_duplicate_identity", "meaning_review"), "proof_dag", "task",
           ("observed_result_material",)),
    _entry("assigned_to", ("artifact",), ("task_revision",),
           ("same_project", "pinned_target", "task_assignment"),
           ("all_requirements", "no_duplicate_identity", "meaning_review"), "proof_dag", "plan",
           ("task_revision_material",)),
    _entry("produced_by", ("candidate", "candidate_symbol", "artifact"), ("task_revision",),
           ("same_project", "pinned_target", "candidate_output"),
           ("all_declared_outputs", "no_duplicate_identity", "meaning_review"), "proof_dag", "task",
           ("output_material",)),
    _entry("migrated_to", ("population_item", "source_span"), ("candidate_symbol", "traceability_ref", "git_file", "git_symbol", "artifact_ac"),
           ("same_project", "pinned_source", "pinned_target", "migration_membership"),
           ("all_population_leaves", "no_duplicate_identity", "meaning_review"), "proof_dag", "task",
           ("migration_material",)),
    _entry("depends_on", ("artifact", "task_revision", "traceability_ref", "git_file", "git_symbol"), ("artifact", "task_revision", "traceability_ref", "git_file", "git_symbol"),
           ("same_project", "pinned_source", "pinned_target", "dependency_identity"),
           ("no_self_edge", "meaning_review"), "product_cycle_allowed", "task",
           ("dependency_identity",)),
    _entry("affects", ("change", "proposal"), ("artifact", "task_revision", "traceability_ref", "git_file", "git_symbol", "candidate", "delivery_snapshot", "actual_delivery_commit"),
           ("same_project", "pinned_target", "change_identity"),
           ("all_impacted_targets", "no_duplicate_identity", "meaning_review"), "proof_dag", "plan",
           ("change_identity",)),
    _entry("contains", ("delivery_snapshot", "actual_delivery_commit"),
           ("candidate", "candidate_symbol", "traceability_ref", "git_file", "git_symbol", "artifact"),
           ("same_project", "pinned_source", "pinned_target", "delivery_membership"),
           ("all_required_outputs", "no_duplicate_identity", "meaning_review"), "none", "delivery",
           ("delivery_material",)),
)

need(len(RELATION_REGISTRY) == 13, "invalid_registry", "The assurance relation registry must contain exactly thirteen contracts")
_REGISTRY_BY_NAME = {entry["relation"]: entry for entry in RELATION_REGISTRY}
need(len(_REGISTRY_BY_NAME) == len(RELATION_REGISTRY), "invalid_registry", "Relation names must be unique")
REGISTRY_DIGEST = digest(list(RELATION_REGISTRY))

# The E1/E2 registry above is the first persisted wire for this component.
# Unit2c adds one output endpoint pair and the corresponding bundle-membership
# target.  Keep the original bytes and digest addressable: old edge/set
# objects must never be reinterpreted under the new endpoint grammar.
REGISTRY_V1_VERSION = RELATION_CONTRACT_VERSION
REGISTRY_V1 = tuple(copy.deepcopy(item) for item in RELATION_REGISTRY)
REGISTRY_V1_DIGEST = REGISTRY_DIGEST
V1_DIGEST = REGISTRY_V1_DIGEST

_v2_entries = copy.deepcopy(list(RELATION_REGISTRY))
for _entry_item in _v2_entries:
    if _entry_item["relation"] == "produced_by":
        _entry_item["source_kinds"] = [*_entry_item["source_kinds"], "output_artifact"]
        _entry_item["target_kinds"] = [*_entry_item["target_kinds"], "delivery_check"]
        _entry_item["edge_checks"] = [*_entry_item["edge_checks"], "output_producer_identity"]
        _entry_item["evidence_requirements"] = [*_entry_item["evidence_requirements"], "output_producer_identity"]
    elif _entry_item["relation"] == "contains":
        _entry_item["target_kinds"] = [* _entry_item["target_kinds"], "output_artifact"]
        _entry_item["edge_checks"] = [* _entry_item["edge_checks"], "output_bundle_membership"]
        _entry_item["evidence_requirements"] = [* _entry_item["evidence_requirements"], "output_bundle_membership"]
_v2_entries = tuple(_v2_entries)
REGISTRY_V2_VERSION = "daikibo.assurance-relation-contract.v2-output"
for _entry_item in _v2_entries:
    _entry_item["contract_version"] = REGISTRY_V2_VERSION
REGISTRY_V2 = tuple(_v2_entries)
REGISTRY_V2_DIGEST = digest(list(REGISTRY_V2))
V2_DIGEST = REGISTRY_V2_DIGEST
SUPPORTED_REGISTRIES = {
    REGISTRY_V1_DIGEST: {"version": 1, "contract_version": REGISTRY_V1_VERSION,
                         "digest": REGISTRY_V1_DIGEST, "relations": REGISTRY_V1},
    REGISTRY_V2_DIGEST: {"version": 2, "contract_version": REGISTRY_V2_VERSION,
                         "digest": REGISTRY_V2_DIGEST, "relations": REGISTRY_V2},
}


# Kinds are intentionally finite.  A caller cannot turn an arbitrary string
# into a resolvable assurance reference and then use it as evidence.
TYPED_REF_KINDS = frozenset({
    kind for entry in RELATION_REGISTRY
    for kind in entry["source_kinds"] + entry["target_kinds"]
} | {
    "artifact", "artifact_ac", "source", "source_span", "git_file", "git_symbol",
    "candidate", "candidate_symbol", "task_revision", "test_plan", "test_plan_check",
    "observed_result", "test_artifact", "delivery_snapshot", "delivery_check",
    "actual_delivery_commit", "output_artifact", "population", "population_item",
    "change", "proposal", "assurance_object", "source_code", "document_item",
    "traceability_ref", "scope", "obligations", "profile", "material",
})

# These types must carry a durable locator/material projection.  An ID alone
# is insufficient, especially for mutable plans and observed results.
MATERIAL_LOCATOR_KINDS = frozenset({
    "source_span", "git_file", "git_symbol", "candidate_symbol", "task_revision",
    "test_plan", "test_plan_check", "observed_result", "test_artifact", "delivery_snapshot",
    "delivery_check", "actual_delivery_commit", "output_artifact", "artifact_ac",
    "candidate", "population", "population_item", "change", "proposal", "assurance_object", "material",
})


def relation_catalog(*, contract_digest: str | None = None) -> dict[str, Any]:
    """Return a detached catalog suitable for a read-only API response."""
    if contract_digest is not None:
        selected = registry_for_digest(contract_digest)
        return {"contract_version": selected["contract_version"],
                "registry_digest": selected["digest"],
                "relations": copy.deepcopy(selected["relations"]),
                "version": selected["version"],
                "typed_ref_kinds": sorted(TYPED_REF_KINDS),
                "material_locator_kinds": sorted(MATERIAL_LOCATOR_KINDS)}
    return {"contract_version": RELATION_CONTRACT_VERSION,
            "registry_digest": REGISTRY_DIGEST,
            "relations": copy.deepcopy(list(RELATION_REGISTRY)),
            "supported_registries": [
                {"version": item["version"], "contract_version": item["contract_version"],
                 "digest": item["digest"], "relation_count": len(item["relations"])}
                for item in sorted(SUPPORTED_REGISTRIES.values(), key=lambda value: value["version"])
            ],
            "typed_ref_kinds": sorted(TYPED_REF_KINDS),
            "material_locator_kinds": sorted(MATERIAL_LOCATOR_KINDS)}


def _registry(contract_digest: str | None) -> tuple[dict[str, Any], ...]:
    selected = REGISTRY_V1_DIGEST if contract_digest is None else contract_digest
    entry = SUPPORTED_REGISTRIES.get(selected)
    need(entry is not None, "invalid_registry", "Relation registry digest is unknown", selected)
    return entry["relations"]


def registry_for_digest(contract_digest: str | None = None) -> dict[str, Any]:
    """Return detached metadata for an explicitly selected registry wire."""
    selected = REGISTRY_V1_DIGEST if contract_digest is None else contract_digest
    entry = SUPPORTED_REGISTRIES.get(selected)
    need(entry is not None, "invalid_registry", "Relation registry digest is unknown", selected)
    return {"version": entry["version"], "contract_version": entry["contract_version"],
            "digest": entry["digest"], "relations": copy.deepcopy(list(entry["relations"]))}


def validate_typed_ref(ref: Any, *, project: str | None = None,
                       expected_kinds: set[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Validate one of the closed typed-ref variants from the design contract."""
    need(type(ref) is dict, "invalid_reference", "Typed reference must be an object")
    kind = ref.get("kind")
    need(isinstance(kind, str) and kind in TYPED_REF_KINDS, "unknown_reference", "Unknown typed reference kind", kind)
    if expected_kinds is not None:
        need(kind in set(expected_kinds), "invalid_reference", "Typed reference kind is not allowed", kind)
    if kind == "traceability_ref":
        need(set(ref) == {"kind", "project", "locator"}, "invalid_reference", "Traceability wrapper keys differ")
        locator = ref["locator"]
        need(isinstance(locator, dict) and locator.get("ref_type") in {"git_file", "git_symbol", "candidate_symbol", "source_span", "artifact_ac"},
             "invalid_reference", "Traceability wrapper locator type is invalid")
        locator_required={
            "git_file":{"ref_type","repository","object_format","commit","path","blob_oid","sha256","mode"},
            "git_symbol":{"ref_type","repository","object_format","commit","path","blob_oid","sha256","mode","adapter","adapter_digest","qualified_name","kind","ordinal","start_byte","end_byte","span_sha256","signature_hash"},
            "candidate_symbol":{"ref_type","candidate","task","task_revision","candidate_digest","snapshot_digest","repository","path","sha256","mode","adapter","adapter_digest","qualified_name","kind","ordinal","start_byte","end_byte","span_sha256","signature_hash"},
            "source_span":{"ref_type","source_id","blob_digest","byte_start","byte_end","unicode_start","unicode_end","span_hash"},
            "artifact_ac":{"ref_type","artifact","revision","body_digest","ac_pointer","ac_digest"},
        }
        optional={"pin_revision","pin_revision_digest"} if locator["ref_type"] in {"git_file","git_symbol"} else {"ac_id"} if locator["ref_type"] == "artifact_ac" else set()
        need(set(locator) == locator_required[locator["ref_type"]] | optional,
             "invalid_reference", "Traceability locator keys differ", locator["ref_type"])
        ref_project = ref["project"]
        _project(ref_project, project)
        locator_type = locator["ref_type"]
        if locator_type in {"git_file", "git_symbol"}:
            _string(locator["repository"], "repository"); _string(locator["path"], "path")
            _string(locator["object_format"], "object_format")
            _oid(locator["commit"], locator["object_format"], "commit")
            _oid(locator["blob_oid"], locator["object_format"], "blob_oid")
            _sha(locator["sha256"], "sha256"); _nonnegative(locator["mode"], "mode")
            if "pin_revision" in locator: _string(locator["pin_revision"], "pin_revision")
            if "pin_revision_digest" in locator: _sha(locator["pin_revision_digest"], "pin_revision_digest")
            if locator_type == "git_symbol":
                for field in ("adapter", "qualified_name", "kind"):
                    _string(locator[field], field)
                _sha(locator["adapter_digest"], "adapter_digest")
                _nonnegative(locator["ordinal"], "ordinal")
                _nonnegative(locator["start_byte"], "start_byte")
                _nonnegative(locator["end_byte"], "end_byte")
                need(locator["end_byte"] >= locator["start_byte"], "invalid_reference", "Symbol byte range is inverted")
                _sha(locator["span_sha256"], "span_sha256"); _sha(locator["signature_hash"], "signature_hash")
        elif locator_type == "candidate_symbol":
            for field in ("candidate", "task", "repository", "path", "adapter", "qualified_name", "kind"):
                _string(locator[field], field)
            _revision(locator["task_revision"], "task_revision")
            for field in ("candidate_digest", "snapshot_digest", "sha256", "adapter_digest", "span_sha256", "signature_hash"):
                _sha(locator[field], field)
            _nonnegative(locator["mode"], "mode"); _nonnegative(locator["ordinal"], "ordinal")
            _nonnegative(locator["start_byte"], "start_byte"); _nonnegative(locator["end_byte"], "end_byte")
            need(locator["end_byte"] >= locator["start_byte"], "invalid_reference", "Candidate symbol byte range is inverted")
        elif locator_type == "source_span":
            _string(locator["source_id"], "source_id"); _sha(locator["blob_digest"], "blob_digest")
            _nonnegative(locator["byte_start"], "byte_start"); _nonnegative(locator["byte_end"], "byte_end")
            need(locator["byte_end"] >= locator["byte_start"], "invalid_reference", "Source byte range is inverted")
            _nonnegative(locator["unicode_start"], "unicode_start"); _nonnegative(locator["unicode_end"], "unicode_end")
            need(locator["unicode_end"] >= locator["unicode_start"], "invalid_reference", "Source Unicode range is inverted")
            _sha(locator["span_hash"], "span_hash")
        else:
            _string(locator["artifact"], "artifact"); _revision(locator["revision"], "revision")
            _sha(locator["body_digest"], "body_digest"); _sha(locator["ac_digest"], "ac_digest")
            _string(locator["ac_pointer"], "ac_pointer")
            if "ac_id" in locator: _string(locator["ac_id"], "ac_id")
        return {"kind": kind, "project": ref_project, "locator": copy.deepcopy(locator),
                "semantic_kind": locator["ref_type"], "identity_digest": digest(ref)}

    if kind == "output_artifact":
        required = {"kind", "project", "delivery", "check", "observed", "output_id", "output_digest"}
        need(set(ref) == required, "invalid_reference", "Output artifact reference keys differ")
        _project(ref["project"], project)
        _string(ref["output_id"], "output_id")
        _sha(ref["output_digest"], "output_digest")
        validate_typed_ref(ref["delivery"], project=ref["project"], expected_kinds={"delivery_snapshot"})
        validate_typed_ref(ref["check"], project=ref["project"], expected_kinds={"delivery_check"})
        validate_typed_ref(ref["observed"], project=ref["project"], expected_kinds={"observed_result"})
        need(ref["check"]["delivery"] == ref["delivery"],
             "invalid_reference", "Output check is bound to another delivery snapshot")
        result = copy.deepcopy(ref)
        result["identity_digest"] = digest(ref)
        return result

    schemas: dict[str, tuple[set[str], set[str]]] = {
        "artifact": ({"kind", "project", "artifact", "revision", "body_digest"}, set()),
        "task_revision": ({"kind", "project", "task", "revision", "definition_digest"}, set()),
        "candidate": ({"kind", "project", "candidate", "task", "task_revision", "candidate_digest", "snapshot_digest"}, set()),
        "source": ({"kind", "project", "source", "blob_digest"}, set()),
        "population": ({"kind", "project", "revision", "revision_digest", "population_digest"}, set()),
        "population_item": ({"kind", "project", "population", "item", "item_digest"}, set()),
        "test_plan": ({"kind", "project", "task", "task_revision", "plan_digest", "pin"}, {"history"}),
        "test_plan_check": ({"kind", "project", "plan", "check_id", "check_digest"}, set()),
        "change": ({"kind", "project", "change", "revision", "body_digest", "pin"}, set()),
        "proposal": ({"kind", "project", "table", "proposal", "proposal_digest"}, set()),
        "observed_result": ({"kind", "project", "receipt", "run", "receipt_digest", "run_binding", "snapshot_digest", "result_digest"}, set()),
        "delivery_snapshot": ({"kind", "project", "delivery", "binding_digest", "snapshot_digest", "pin"}, set()),
        "delivery_check": ({"kind", "project", "delivery", "check_id", "check_digest"}, set()),
        "actual_delivery_commit": ({"kind", "project", "delivery", "repository", "object_format", "commit", "tree", "pin"}, set()),
        "assurance_object": ({"kind", "project", "object", "object_kind", "object_digest"}, set()),
    }
    required, optional = schemas.get(kind, (set(), set()))
    need(required and set(ref) <= required | optional and required <= set(ref),
         "invalid_reference", "Typed reference has missing or unknown fields", kind)
    _project(ref.get("project"), project)
    # Exact common scalar rules.  The population revision is the one explicit
    # string-ID exception in the contract.
    id_fields = {
        "artifact": ("artifact",), "task_revision": ("task",), "candidate": ("candidate", "task"),
        "source": ("source",), "population_item": ("item",), "test_plan": ("task",),
        "test_plan_check": ("check_id",), "change": ("change",), "proposal": ("proposal",),
        "observed_result": ("receipt", "run", "run_binding"), "delivery_snapshot": ("delivery",),
        "delivery_check": ("check_id",),
        # ``delivery`` is the nested delivery_snapshot reference, not an ID
        # scalar.  Its exact shape is validated in the branch below.
        "actual_delivery_commit": ("repository",),
        "assurance_object": ("object",),
    }
    for field in id_fields.get(kind, ()):
        _string(ref[field], field)
    if kind == "population":
        _string(ref["revision"], "population revision")
    elif kind in {"artifact", "task_revision", "change"}:
        _revision(ref["revision"], "revision")
    elif kind in {"candidate", "test_plan"}:
        _revision(ref["task_revision"], "task_revision")
    for field in ("body_digest", "definition_digest", "candidate_digest", "snapshot_digest", "blob_digest",
                  "revision_digest", "population_digest", "item_digest", "plan_digest", "check_digest",
                  "proposal_digest", "receipt_digest", "result_digest", "binding_digest", "object_digest"):
        if field in ref: _sha(ref[field], field)
    if kind == "proposal":
        need(ref["table"] in {"task_revision_proposals", "traceability_proposals", "assurance_objects"},
             "invalid_reference", "Proposal table is not supported", ref["table"])
    if kind == "assurance_object":
        need(ref["object_kind"] in {"scope", "obligations", "profile", "edge", "set"},
             "invalid_reference", "Assurance object kind is not a reviewable object", ref["object_kind"])
    if kind == "test_plan":
        if "history" in ref:
            history = ref["history"]
            need(isinstance(history, dict) and set(history) == {"id", "digest", "side"},
                 "invalid_reference", "Historical test-plan selector is malformed")
            _string(history["id"], "history.id"); _sha(history["digest"], "history.digest")
            need(history["side"] in {"before", "after"}, "invalid_reference", "Historical test-plan side is invalid")
    if kind == "actual_delivery_commit":
        _string(ref["object_format"], "object_format")
        _oid(ref["commit"], ref["object_format"], "commit"); _oid(ref["tree"], ref["object_format"], "tree")
        validate_typed_ref(ref["delivery"], project=ref["project"], expected_kinds={"delivery_snapshot"})
    if "pin" in ref:
        pin = ref["pin"]
        need(isinstance(pin, dict) and set(pin) == {"id", "digest"} and isinstance(pin["id"], str) and bool(pin["id"]),
             "invalid_reference", "Material pin must contain exactly id and digest")
        _sha(pin["digest"], "pin.digest")
    if kind == "test_plan_check":
        need(isinstance(ref["plan"], dict), "invalid_reference", "Test check plan is missing")
        validate_typed_ref(ref["plan"], project=ref["project"], expected_kinds={"test_plan"})
    if kind == "delivery_check":
        need(isinstance(ref["delivery"], dict), "invalid_reference", "Delivery check snapshot is missing")
        validate_typed_ref(ref["delivery"], project=ref["project"], expected_kinds={"delivery_snapshot"})
    if kind == "population_item":
        validate_typed_ref(ref["population"], project=ref["project"], expected_kinds={"population"})
    result = copy.deepcopy(ref)
    result["identity_digest"] = digest(ref)
    return result


def semantic_kind(ref: dict[str, Any]) -> str:
    """Return the relation endpoint kind without erasing wrapper identity."""
    if ref.get("kind") == "traceability_ref":
        # ``semantic_kind`` is a resolver projection.  Public exact refs do
        # not carry it, so derive the stable kind from the exact locator when
        # a normalized resolver result has not supplied the projection.
        return ref.get("semantic_kind") or ref.get("locator", {}).get("ref_type")
    return ref["kind"]


def validate_relation(relation: str, source_ref: Any, target_ref: Any, *,
                      project: str, contract_digest: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    need(isinstance(relation, str) and relation in _REGISTRY_BY_NAME,
         "unknown_relation", "Relation is not in the frozen registry", relation)
    selected_digest = REGISTRY_V1_DIGEST if contract_digest is None else contract_digest
    registry = _registry(selected_digest)
    source = validate_typed_ref(source_ref, project=project)
    target = validate_typed_ref(target_ref, project=project)
    entry = next(item for item in registry if item["relation"] == relation)
    source_kind, target_kind = semantic_kind(source), semantic_kind(target)
    need(source_kind in entry["source_kinds"], "invalid_relation_endpoint", "Source kind is not allowed for relation", relation)
    need(target_kind in entry["target_kinds"], "invalid_relation_endpoint", "Target kind is not allowed for relation", relation)
    if selected_digest == REGISTRY_V2_DIGEST and relation == "produced_by":
        allowed = {
            (source_kind, target_kind)
            for source_kind in ("candidate", "candidate_symbol", "artifact")
            for target_kind in ("task_revision",)
        } | {("output_artifact", "delivery_check")}
        need((source_kind, target_kind) in allowed,
             "invalid_relation_endpoint", "V2 produced_by endpoint pair is not allowed", relation)
    if "no_self_edge" in entry["set_checks"]:
        need(source_kind != target_kind or digest(source) != digest(target),
             "invalid_relation", "Self relation is not allowed", relation)
    return source, target


def registry_entry(relation: str, *, contract_digest: str | None = None) -> dict[str, Any]:
    registry = _registry(contract_digest)
    need(any(item["relation"] == relation for item in registry),
         "unknown_relation", "Relation is not in the frozen registry", relation)
    return copy.deepcopy(next(item for item in registry if item["relation"] == relation))
