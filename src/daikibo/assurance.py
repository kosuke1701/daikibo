"""Immutable edge-assurance storage and bounded identity reads.

The storage component is intentionally below the review workflow.  It can
persist a canonical object, an event chain, its CAS-style head projection, and
typed reference indexes.  It cannot adopt an edge, complete a review, or
certify a relation set; those are the E2/E3 gates.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

from .assurance_profile_contract import (
    PROFILE_V1_FORMAT, PROFILE_V2_FORMAT, PROFILE_V3_FORMAT, PROFILE_V4_FORMAT, PROFILE_V5_FORMAT,
    CANONICAL_PROFILE_FORMATS, profile_registry, profile_has_outputs,
)
from .agents import REVIEW_ROLES
from .domain_responsibility import (SCOPE_V2, OBLIGATIONS_V2, NODE_V2, DOMAIN_ROLE, DOMAIN_SELECTORS,
                                    responsibility_records, validate_v2_body, artifact_dependency_closure)
from .common import Actor, Fault, canonical, digest, need, number, parse_json, timestamp, uid
from .candidate_provenance import resolve_candidate_identity
from .assurance_relations import (
    MATERIAL_LOCATOR_KINDS,
    REGISTRY_DIGEST,
    RELATION_CONTRACT_VERSION,
    TYPED_REF_KINDS,
    relation_catalog,
    semantic_kind,
    registry_entry,
    validate_relation,
    validate_typed_ref,
    REGISTRY_V1_DIGEST,
    REGISTRY_V2_DIGEST,
)
from .assurance_outputs import (
    OUTPUT_MATERIAL_KIND,
    capture_output_material,
    validate_output_material,
    validate_output_record,
    validate_output_reference,
)
from .assurance_delivery import delivery_declaration_owner_matches
from .verification_materials import (
    EXECUTION_MATERIAL_KIND,
    MATERIAL_FORMAT,
    validate_git_material_payload,
    validate_sealed_snapshot,
)
from .execution_record import execution_record_consistency


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS assurance_objects (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('edge','set','packet','profile','obligations','material','scope')),
 logical_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL,
 created REAL NOT NULL,
 UNIQUE(project,kind,logical_id,revision)
);
CREATE INDEX IF NOT EXISTS assurance_objects_project_kind ON assurance_objects(project,kind,logical_id,revision);
CREATE INDEX IF NOT EXISTS assurance_objects_digest ON assurance_objects(project,digest);
CREATE TABLE IF NOT EXISTS assurance_events (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id),
 subject_id TEXT NOT NULL REFERENCES assurance_objects(id),
 subject_digest TEXT NOT NULL,
 event_kind TEXT NOT NULL CHECK(event_kind IN ('adopt','withdraw','supersede')),
 expected_head TEXT,
 previous TEXT,
 body TEXT NOT NULL CHECK(json_valid(body)),
 created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assurance_events_subject ON assurance_events(project,subject_id,created,id);
CREATE INDEX IF NOT EXISTS assurance_events_project ON assurance_events(project,created,id);
CREATE TABLE IF NOT EXISTS assurance_heads (
 project TEXT NOT NULL REFERENCES projects(id),
 logical_id TEXT NOT NULL,
 head_event TEXT NOT NULL REFERENCES assurance_events(id),
 PRIMARY KEY(project,logical_id)
);
CREATE INDEX IF NOT EXISTS assurance_heads_event ON assurance_heads(head_event);
CREATE TABLE IF NOT EXISTS assurance_refs (
 object_id TEXT NOT NULL REFERENCES assurance_objects(id),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 purpose TEXT NOT NULL,
 ref_kind TEXT NOT NULL,
 ref_id TEXT NOT NULL,
 ref_revision TEXT NOT NULL,
 ref_digest TEXT NOT NULL,
 PRIMARY KEY(object_id,ordinal),
 UNIQUE(object_id,purpose,ref_kind,ref_id,ref_revision,ref_digest)
);
CREATE INDEX IF NOT EXISTS assurance_refs_lookup ON assurance_refs(ref_kind,ref_id,ref_revision,ref_digest);
CREATE INDEX IF NOT EXISTS assurance_refs_object ON assurance_refs(object_id,ordinal);
CREATE TRIGGER IF NOT EXISTS assurance_objects_no_update
 BEFORE UPDATE ON assurance_objects BEGIN SELECT RAISE(ABORT,'immutable assurance object'); END;
CREATE TRIGGER IF NOT EXISTS assurance_objects_no_delete
 BEFORE DELETE ON assurance_objects BEGIN SELECT RAISE(ABORT,'immutable assurance object'); END;
CREATE TRIGGER IF NOT EXISTS assurance_events_no_update
 BEFORE UPDATE ON assurance_events BEGIN SELECT RAISE(ABORT,'immutable assurance event'); END;
CREATE TRIGGER IF NOT EXISTS assurance_events_no_delete
 BEFORE DELETE ON assurance_events BEGIN SELECT RAISE(ABORT,'immutable assurance event'); END;
CREATE TRIGGER IF NOT EXISTS assurance_refs_no_update
 BEFORE UPDATE ON assurance_refs BEGIN SELECT RAISE(ABORT,'immutable assurance reference'); END;
CREATE TRIGGER IF NOT EXISTS assurance_refs_no_delete
 BEFORE DELETE ON assurance_refs BEGIN SELECT RAISE(ABORT,'immutable assurance reference'); END;
'''

OBJECT_KINDS = frozenset({"edge", "set", "packet", "profile", "obligations", "material", "scope"})
MAX_OBJECT_BYTES = 1024 * 1024
MAX_PAGE = 500
MAX_CAS_CHILDREN = 100_000

# These are the mechanical obligations shared by every relation set.  A
# registry entry may add relation-specific obligations, but a caller cannot
# use its ``criteria`` object to remove one of these requirements.  The
# values saved in a set body are *achieved observations*; the immutable
# ``criteria_requirements`` list below is the separate declaration of what
# the set must satisfy.
SET_UNIVERSAL_CRITERIA = frozenset({
    "all_edges_current", "all_obligations_covered", "no_duplicate_identity",
    "meaning_review", "independent_synthesis",
})

# E3 unit 1 deliberately keeps the profile vocabulary finite.  These values
# are part of the public catalog and are also used by the wire validator below
# so that an API caller cannot make a new stage selector by choosing a string
# that happens to be accepted by an older v1 profile.
PROFILE_V2_LOGICAL_PREFIX = "profile:program:"
PROFILE_STAGES = ("plan", "task", "integration", "delivery")
PROFILE_DENOMINATORS = {
    "plan": "program_plan",
    "task": "assigned_task_contributors",
    "integration": "program_integration",
    "delivery": "actual_delivery",
}
PROFILE_EXECUTION_RESULTS = {
    "plan": "none",
    "task": "assigned_checks",
    "integration": "integration_checks",
    "delivery": "certified_integration_and_actual_outputs",
}
PROFILE_RELATION_CENTERS = frozenset({
    "source_roots", "requirements", "design_artifacts", "assigned_tasks",
    "test_definitions", "selected_outputs", "populations",
    "delivery_snapshots", "actual_commits",
})
PROFILE_NODE_SELECTORS = {
    "requirement": "requirements",
    "accepted_requirement": "requirements",
    "design": "design",
    "accepted_design": "design",
    "component": "design",
    "accepted_component": "design",
    "interface": "consistency",
    "accepted_interface": "consistency",
    "test_artifact": "test_plan",
    "accepted_test_artifact": "test_plan",
    "test_plan": "test_plan",
    "fixed_test_plan": "test_plan",
}
PROFILE_NODE_ROLE_SET = frozenset({"spec", "quality", "test_adequacy", "specialist", "requirements", "design", "consistency", "trace", "impact", "feasibility", "phase", "test_plan", "integration", "goal_validation", "delivery_profile", "adapter_qualification", "decision_proposal", "execution_control"})
PROFILE_NODE_SELECTOR_SET = frozenset(PROFILE_NODE_SELECTORS)
PROFILE_V2_FIELDS = frozenset({
    "format", "project", "program", "scope_ref", "obligations_ref",
    "previous_selection_ref", "application_mode", "stage_rules",
    "node_review_rules", "relation_selectors", "test_definition_bindings",
    "change_reason", "authority_refs",
})
PROFILE_V3_FIELDS = PROFILE_V2_FIELDS | {"required_relation_contract_digest"}
PROFILE_V4_FIELDS = PROFILE_V3_FIELDS
PROFILE_V5_FIELDS = PROFILE_V4_FIELDS | {"required_scope_contract", "required_node_contract"}
PROFILE_V4_RELATION_CENTERS = PROFILE_RELATION_CENTERS | {"realization_sources"}


def _is_canonical_profile(value: Any) -> bool:
    """Dispatch only string profile formats; malformed JSON never reaches set membership."""
    return type(value) is str and value in CANONICAL_PROFILE_FORMATS


def _profile_input_format(body: dict[str, Any]) -> str | None:
    """Validate the public discriminator before selecting the v1/v2/v3 parser."""
    if "format" not in body:
        return None
    value = body["format"]
    need(type(value) is str, "invalid_profile", "Profile format must be a string")
    need(value in {PROFILE_V1_FORMAT, *CANONICAL_PROFILE_FORMATS},
         "invalid_profile", "Profile format is unsupported", value)
    return value


def _validate_profile_shape(body: dict[str, Any], *, profile_format: str,
                            fields: frozenset[str], centers_allowed: frozenset[str]) -> None:
    """Validate the closed profile.v2 shape without resolving live records.

    Resolution of program rows, scope/obligation derivation, authority
    material, and the canonical head belongs to ``Assurance.profile_propose``.
    Keeping this parser independent lets immutable archive import reject a
    malformed v2 row before any currentness or adoption calculation runs.
    """
    need(set(body) == fields, "invalid_profile",
         "Profile v2 keys differ", sorted(set(body) ^ fields))
    need(body.get("format") == profile_format, "invalid_profile",
         "Profile format is not assurance.profile.v2")
    need(isinstance(body.get("project"), str) and body["project"],
         "invalid_profile", "Profile project is invalid")
    need(isinstance(body.get("program"), str) and body["program"] and "\x00" not in body["program"],
         "invalid_profile", "Profile program is invalid")
    for name, expected_kind in (("scope_ref", "scope"), ("obligations_ref", "obligations")):
        validate_typed_ref(body[name], project=body["project"], expected_kinds={"assurance_object"})
        need(body[name].get("object_kind") == expected_kind, "invalid_profile",
             f"Profile {name} must reference {expected_kind}")
    previous = body["previous_selection_ref"]
    if previous is not None:
        validate_typed_ref(previous, project=body["project"], expected_kinds={"assurance_object"})
        need(previous.get("object_kind") == "profile", "invalid_profile",
             "previous_selection_ref must reference a profile")
    application_mode = body.get("application_mode")
    need(type(application_mode) is str and application_mode in {"mandatory", "disabled"}, "invalid_profile",
         "Profile application_mode is invalid")
    need(isinstance(body.get("stage_rules"), dict) and set(body["stage_rules"]) == set(PROFILE_STAGES),
         "invalid_profile", "Profile stage_rules must contain all four stages")
    required_stage_fields = {"denominator", "relation_sets", "node_rules", "execution_results"}
    for stage in PROFILE_STAGES:
        rule = body["stage_rules"][stage]
        need(isinstance(rule, dict) and set(rule) == required_stage_fields,
             "invalid_profile", f"Profile {stage} stage rule keys differ")
        need(rule["denominator"] == PROFILE_DENOMINATORS[stage],
             "invalid_profile", f"Profile {stage} denominator is invalid")
        need(rule["execution_results"] == PROFILE_EXECUTION_RESULTS[stage],
             "invalid_profile", f"Profile {stage} execution result selector is invalid")
        node_rules = rule["node_rules"]
        need(isinstance(node_rules, list) and node_rules and
             all(isinstance(value, str) and value for value in node_rules),
             "invalid_profile", f"Profile {stage} node rules are invalid")
        need(node_rules == sorted(set(node_rules)), "invalid_profile",
             f"Profile {stage} node rules are not canonical")
        relation_sets = rule["relation_sets"]
        need(isinstance(relation_sets, list) and relation_sets,
             "invalid_profile", f"Profile {stage} relation sets are invalid")
        seen_relation_sets: set[bytes] = set()
        for relation_set in relation_sets:
            need(isinstance(relation_set, dict) and set(relation_set) == {"relation", "direction", "centers"},
                 "invalid_profile", f"Profile {stage} relation set shape is invalid")
            relation = relation_set["relation"]
            need(type(relation) is str and relation, "invalid_profile",
                 f"Profile {stage} relation name is invalid")
            registry_entry(relation)
            direction = relation_set["direction"]
            need(type(direction) is str and direction in {"outgoing", "incoming"}, "invalid_profile",
                 f"Profile {stage} relation direction is invalid")
            centers = relation_set["centers"]
            need(isinstance(centers, list) and centers and
                 all(type(center) is str and center in centers_allowed for center in centers),
                 "invalid_profile", f"Profile {stage} relation centers are invalid")
            if "realization_sources" in centers:
                need(profile_format in {PROFILE_V4_FORMAT, PROFILE_V5_FORMAT} and stage in {"plan", "task"} and
                     relation == "realizes" and direction == "outgoing",
                     "invalid_profile", "realization_sources requires v4 plan/task realizes/outgoing")
            need(centers == sorted(set(centers)), "invalid_profile",
                 f"Profile {stage} relation centers are not canonical")
            marker = canonical(relation_set)
            need(marker not in seen_relation_sets, "invalid_profile",
                 f"Profile {stage} contains duplicate relation sets")
            seen_relation_sets.add(marker)
        need(relation_sets == sorted(relation_sets, key=canonical), "invalid_profile",
             f"Profile {stage} relation sets are not canonical")
    node_selectors = {**PROFILE_NODE_SELECTORS, **DOMAIN_SELECTORS} if profile_format == PROFILE_V5_FORMAT else PROFILE_NODE_SELECTORS
    node_roles = PROFILE_NODE_ROLE_SET | {DOMAIN_ROLE} if profile_format == PROFILE_V5_FORMAT else PROFILE_NODE_ROLE_SET
    node_rules = body["node_review_rules"]
    need(isinstance(node_rules, list), "invalid_profile", "Profile node review rules are invalid")
    node_ids: set[str] = set()
    for rule in node_rules:
        need(isinstance(rule, dict) and set(rule) == {"id", "selector", "roles"},
             "invalid_profile", "Profile node review rule shape is invalid")
        node_id, selector, roles = rule["id"], rule["selector"], rule["roles"]
        need(isinstance(node_id, str) and node_id and "\x00" not in node_id,
             "invalid_profile", "Profile node review rule id is invalid")
        need(node_id not in node_ids, "invalid_profile", "Profile node review rule id is duplicated", node_id)
        node_ids.add(node_id)
        need(type(selector) is str and selector in node_selectors, "invalid_profile",
             "Profile node review selector is unsupported", selector)
        need(isinstance(roles, list) and roles and
             all(type(role) is str and role for role in roles) and
             roles == sorted(set(roles)),
             "invalid_profile", "Profile node review roles are invalid", node_id)
        need(all(role in node_roles for role in roles), "invalid_profile",
             "Profile node review role is unsupported", node_id)
        need(node_selectors[selector] in roles, "invalid_profile",
             "Profile node review minimum role was removed", node_id)
    need(node_rules == sorted(node_rules, key=lambda item: item["id"]),
         "invalid_profile", "Profile node review rules are not canonical")
    for stage in PROFILE_STAGES:
        need(set(body["stage_rules"][stage]["node_rules"]) <= node_ids,
             "invalid_profile", f"Profile {stage} refers to an unknown node rule")
    selectors = body["relation_selectors"]
    need(isinstance(selectors, list) and all(isinstance(value, str) and value for value in selectors),
         "invalid_profile", "Profile relation selectors are invalid")
    need(selectors == sorted(set(selectors)), "invalid_profile",
         "Profile relation selectors are not canonical")
    for relation in selectors:
        registry_entry(relation)
    required_relations = {
        item["relation"]
        for stage in PROFILE_STAGES
        for item in body["stage_rules"][stage]["relation_sets"]
    }
    need(required_relations <= set(selectors), "invalid_profile",
         "Profile relation selector removes a required stage relation",
         sorted(required_relations - set(selectors)))
    bindings = body["test_definition_bindings"]
    need(isinstance(bindings, list), "invalid_profile", "Profile test definition bindings are invalid")
    binding_keys: list[bytes] = []
    for binding in bindings:
        need(isinstance(binding, dict) and set(binding) == {"artifact_ref", "check_ref"},
             "invalid_profile", "Profile test definition binding keys differ")
        validate_typed_ref(binding["artifact_ref"], project=body["project"], expected_kinds={"artifact"})
        validate_typed_ref(binding["check_ref"], project=body["project"],
                           expected_kinds={"test_plan_check", "delivery_check"})
        binding_keys.append(canonical(binding))
    need(binding_keys == sorted(set(binding_keys)), "invalid_profile",
         "Profile test definition bindings are not canonical")
    need(isinstance(body["change_reason"], str) and bool(body["change_reason"]) and "\x00" not in body["change_reason"],
         "invalid_profile", "Profile change_reason is required")
    need(isinstance(body["authority_refs"], list), "invalid_profile", "Profile authority_refs are invalid")
    authority_keys: list[bytes] = []
    for ref in body["authority_refs"]:
        validate_typed_ref(ref, project=body["project"])
        need(ref.get("kind") in {"source", "change", "artifact"}, "invalid_profile",
             "Profile authority reference family is unsupported")
        authority_keys.append(canonical(ref))
    need(authority_keys == sorted(set(authority_keys)), "invalid_profile",
         "Profile authority_refs are not canonical")


def _validate_profile_v2_wire(body: dict[str, Any]) -> None:
    """The historical closed v2 vocabulary and fields remain unchanged."""
    _validate_profile_shape(body, profile_format=PROFILE_V2_FORMAT,
                            fields=PROFILE_V2_FIELDS, centers_allowed=PROFILE_RELATION_CENTERS)


def _validate_profile_v4_wire(body: dict[str, Any]) -> None:
    """Validate v4 directly, without projecting new selectors through v2."""
    _validate_profile_shape(body, profile_format=PROFILE_V4_FORMAT,
                            fields=PROFILE_V4_FIELDS, centers_allowed=PROFILE_V4_RELATION_CENTERS)
    need(body.get("required_relation_contract_digest") == profile_registry(PROFILE_V4_FORMAT),
         "invalid_registry", "Profile v4 must pin the output-aware relation registry")
    for stage in PROFILE_STAGES:
        for relation_set in body["stage_rules"][stage]["relation_sets"]:
            registry_entry(relation_set["relation"], contract_digest=REGISTRY_V2_DIGEST)
    for relation in body["relation_selectors"]:
        registry_entry(relation, contract_digest=REGISTRY_V2_DIGEST)


def _validate_profile_v5_wire(body: dict[str, Any]) -> None:
    _validate_profile_shape(body, profile_format=PROFILE_V5_FORMAT,
                            fields=PROFILE_V5_FIELDS, centers_allowed=PROFILE_V4_RELATION_CENTERS)
    need(body.get("required_scope_contract") == SCOPE_V2 and body.get("required_node_contract") == NODE_V2,
         "invalid_profile", "Profile v5 requires scope v2 and node contract v2")
    need(body.get("required_relation_contract_digest") == REGISTRY_V2_DIGEST,
         "invalid_registry", "Profile v5 requires registry v2")
    for stage in PROFILE_STAGES:
        for rule in body["stage_rules"][stage]["relation_sets"]:
            registry_entry(rule["relation"], contract_digest=REGISTRY_V2_DIGEST)
    for relation in body["relation_selectors"]:
        registry_entry(relation, contract_digest=REGISTRY_V2_DIGEST)


def _validate_canonical_profile_wire(body: dict[str, Any]) -> None:
    parsers = {PROFILE_V2_FORMAT: _validate_profile_v2_wire,
               PROFILE_V3_FORMAT: _validate_profile_v3_wire,
               PROFILE_V4_FORMAT: _validate_profile_v4_wire, PROFILE_V5_FORMAT: _validate_profile_v5_wire}
    value = _profile_input_format(body)
    need(value in parsers, "invalid_profile", "Canonical profile format is unsupported", value)
    parsers[value](body)


def _validate_profile_v3_wire(body: dict[str, Any]) -> None:
    """Validate the strict v3 profile wire without changing the v2 parser.

    v3 deliberately reuses the closed v2 profile vocabulary.  Its only
    additional field is an explicit pin to the output-aware relation
    registry.  Parsing the legacy projection through the existing v2 parser
    keeps the v1/v2 bytes and all of their canonical ordering rules intact;
    relation names are then checked against the pinned v2 registry as well.
    """
    need(set(body) == PROFILE_V3_FIELDS, "invalid_profile",
         "Profile v3 keys differ", sorted(set(body) ^ PROFILE_V3_FIELDS))
    need(body.get("format") == PROFILE_V3_FORMAT, "invalid_profile",
         "Profile format is not assurance.profile.v3")
    need(body.get("required_relation_contract_digest") == REGISTRY_V2_DIGEST,
         "invalid_registry", "Profile v3 must pin the output-aware relation registry")
    legacy = dict(body)
    legacy["format"] = PROFILE_V2_FORMAT
    legacy.pop("required_relation_contract_digest", None)
    _validate_profile_v2_wire(legacy)
    for stage in PROFILE_STAGES:
        for relation_set in body["stage_rules"][stage]["relation_sets"]:
            registry_entry(relation_set["relation"], contract_digest=REGISTRY_V2_DIGEST)
    for relation in body["relation_selectors"]:
        registry_entry(relation, contract_digest=REGISTRY_V2_DIGEST)


def _selection_ref_identity(value: Any) -> Any:
    """Return the persisted identity portion of a selection reference."""
    if isinstance(value, dict):
        return {key: _selection_ref_identity(item) for key, item in value.items()
                if key not in {"identity_digest", "semantic_kind"}}
    if isinstance(value, list):
        return [_selection_ref_identity(item) for item in value]
    return value


def _validate_profile_event_predecessor(event: dict[str, Any], event_body: Any,
                                        profile_body: dict[str, Any],
                                        expected_previous: dict[str, Any] | None,
                                        *, code: str) -> None:
    """Check the three persisted predecessor projections as one invariant.

    A profile selection is represented in the immutable profile body, the
    event's CAS ``previous`` pointer, and the event selection payload.  Each
    projection must identify the same prior profile (or all be null).  This is
    shared by live currentness checks and archive validation so a malformed
    event adapter cannot turn an altered predecessor into a replay.
    """
    need(event.get("expected_head") == event.get("previous"), code,
         "Profile event compare-and-swap predecessor differs")
    need(isinstance(event_body, dict), code, "Profile event body is malformed")
    selection = event_body.get("selection")
    need(isinstance(selection, dict) and "previous_selection_ref" in selection,
         code, "Profile event selection predecessor is missing")
    recorded_previous = selection["previous_selection_ref"]
    project = profile_body.get("project")
    for value in (recorded_previous, profile_body.get("previous_selection_ref")):
        if value is not None:
            validate_typed_ref(value, project=project, expected_kinds={"assurance_object"})
            need(value.get("object_kind") == "profile", code,
                 "Profile event predecessor is not a profile")
    expected = _selection_ref_identity(expected_previous)
    need(_selection_ref_identity(recorded_previous) == expected and
         _selection_ref_identity(profile_body.get("previous_selection_ref")) == expected,
         code, "Profile selection predecessor projections differ")


def _material_child_digests(value: Any, path: str = "payload") -> list[tuple[str, str]]:
    """Return the explicit CAS children embedded in a material payload.

    Material payloads are controller-produced JSON envelopes.  A field whose
    name ends in ``_blob`` (and the snapshot ``blob`` field) is a durable CAS
    edge, rather than an opaque label.  Keeping this traversal here gives the
    writer, archive builder and GC the same closure definition.  Unknown
    ordinary strings remain ordinary data and are never guessed to be blobs.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if (key == "blob" or key.endswith("_blob")) and child is not None:
                need(isinstance(child, str) and _sha(child), "invalid_material",
                     "Material CAS child must be a lowercase SHA-256", child_path)
                found.append((child_path, child))
            found.extend(_material_child_digests(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_material_child_digests(child, f"{path}[{index}]"))
    return found


def material_cas_closure(store, root_digest: str, *, include_root: bool = True) -> set[str]:
    """Verify and return a material payload's complete transitive CAS closure.

    JSON children are traversed when present; binary leaves are valid CAS
    leaves and are not decoded.  Missing or corrupt referenced blobs are hard
    integrity failures.  This function intentionally never treats a missing
    child as an optional optimization.
    """
    need(_sha(root_digest), "integrity_error", "Material CAS root is malformed")
    result: set[str] = set()
    pending = [root_digest]
    while pending:
        blob = pending.pop()
        if blob in result:
            continue
        need(len(result) < MAX_CAS_CHILDREN, "material_too_large", "Material CAS closure exceeds the bounded size")
        raw = store.blob_get(blob)  # verifies existence and content-addressed identity
        result.add(blob)
        try:
            parsed = parse_json(raw, limit=MAX_OBJECT_BYTES)
        except Fault:
            continue
        for path, child in _material_child_digests(parsed):
            if child not in result:
                pending.append(child)
    if not include_root:
        result.discard(root_digest)
    return result


def _material_identity(value: Any) -> Any:
    """Return the persisted identity portion of a typed material reference."""
    if isinstance(value, dict):
        return {key: _material_identity(item) for key, item in value.items()
                if key not in {"identity_digest", "semantic_kind"}}
    if isinstance(value, list):
        return [_material_identity(item) for item in value]
    return value


def _validate_execution_definition_resolution(definition: dict[str, Any],
                                             resolution: Any,
                                             error_code: str) -> dict[str, Any]:
    """Require a resolver result that proves the immutable nested check.

    ``validate_typed_ref`` only proves the wire shape of a check reference.
    The execution material contract needs the resolver to read the pinned
    plan/delivery material and return the member body that carries the exact
    check digest.  Keeping this small postcondition in the shared relation
    validator prevents a live or archive adapter from silently replacing the
    endpoint with a shape-only callback.
    """
    need(isinstance(resolution, dict), error_code,
         "Execution definition resolver returned no immutable identity")
    kind = definition.get("kind")
    need(resolution.get("mode") == kind and isinstance(resolution.get("content"), dict),
         error_code, "Execution definition resolver returned an incomplete identity")
    content = resolution["content"]
    need(content.get("id") == definition.get("check_id") and
         digest(content) == definition.get("check_digest"),
         error_code, "Execution definition check identity differs")
    nested_name = "plan" if kind == "test_plan_check" else "delivery"
    nested = _material_identity(definition.get(nested_name))
    dependencies = resolution.get("dependencies")
    need(isinstance(dependencies, list) and
         [_material_identity(item) for item in dependencies] == [nested],
         error_code, "Execution definition dependency is not its immutable material")
    return resolution


def _validate_execution_candidate_resolution(candidate: dict[str, Any],
                                             resolution: Any,
                                             error_code: str) -> dict[str, Any]:
    """Require the full candidate provenance closure from the shared helper."""
    need(isinstance(resolution, dict) and resolution.get("kind") == "candidate",
         error_code, "Execution candidate resolver returned no immutable identity")
    expected = _material_identity(candidate)
    for field in ("project", "candidate", "task", "task_revision",
                  "candidate_digest", "snapshot_digest"):
        need(resolution.get(field) == expected.get(field), error_code,
             "Execution candidate identity differs from its pinned reference")
    need(_material_identity(resolution.get("canonical_ref")) == expected,
         error_code, "Execution candidate resolver returned another reference")
    dependencies = resolution.get("dependency_refs")
    need(isinstance(dependencies, list) and dependencies, error_code,
         "Execution candidate provenance closure is missing")
    kinds = {item.get("kind") for item in dependencies
             if isinstance(item, dict)}
    need({"task", "candidate", "implementation_run", "implementation_receipt",
          "snapshot", "repository", "cas"} <= kinds,
         error_code, "Execution candidate provenance closure is incomplete")
    return resolution


def _validate_execution_subject_definition_relation(
    *, project: str, definition: dict[str, Any], definition_resolution: dict[str, Any],
    subject: dict[str, Any], task_revision: Any, candidate: dict[str, Any] | None,
    candidate_resolution: dict[str, Any] | None, input_snapshot_digest: str,
    error_code: str,
) -> None:
    """Bind the immutable definition identity to the observed run subject.

    A valid check body is not sufficient evidence for an execution: the
    pinned plan or delivery must describe the same Task/Delivery and the same
    input snapshot that the run actually used.  Keep this relation in the
    shared live/archive validator so a callback cannot make a cross-subject
    material look valid merely by returning an otherwise authentic check.
    """
    if definition["kind"] == "test_plan_check":
        plan = _material_identity(definition.get("plan"))
        need(isinstance(plan, dict) and plan.get("kind") == "test_plan" and
             plan.get("project") == project and
             plan.get("task") == subject.get("id") and
             plan.get("task_revision") == task_revision,
             error_code, "Execution test plan does not match its Task subject")
        need(subject.get("kind") == "task" and isinstance(candidate, dict) and
             candidate.get("project") == project and
             candidate.get("task") == subject.get("id") and
             candidate.get("task_revision") == task_revision,
             error_code, "Execution candidate does not match its Task subject")
        need(isinstance(candidate_resolution, dict) and
             candidate_resolution.get("project") == project and
             candidate_resolution.get("task") == subject.get("id") and
             candidate_resolution.get("task_revision") == task_revision and
             _sha(candidate_resolution.get("task_definition_digest")) and
             candidate_resolution.get("snapshot_digest") == input_snapshot_digest and
             candidate.get("snapshot_digest") == input_snapshot_digest,
             error_code, "Execution candidate snapshot or revision differs from its input")
        material_dependencies = definition_resolution.get("material_dependencies")
        need(isinstance(material_dependencies, list), error_code,
             "Execution test plan Task dependency is unavailable")
        task_dependencies = [item for item in material_dependencies
                             if isinstance(item, dict) and item.get("kind") == "task_revision"]
        need(len(material_dependencies) == 1 and len(task_dependencies) == 1 and
             task_dependencies[0].get("project") == project and
             task_dependencies[0].get("task") == plan.get("task") and
             task_dependencies[0].get("revision") == plan.get("task_revision") and
             task_dependencies[0].get("definition_digest") ==
             candidate_resolution.get("task_definition_digest"),
             error_code, "Execution test plan Task definition differs from its candidate")
        return

    delivery = _material_identity(definition.get("delivery"))
    need(isinstance(delivery, dict) and delivery.get("kind") == "delivery_snapshot" and
         delivery.get("project") == project and
         subject.get("kind") == "delivery" and
         delivery.get("delivery") == subject.get("id") and
         delivery.get("binding_digest") == subject.get("binding") and
         delivery.get("snapshot_digest") == input_snapshot_digest,
         error_code, "Execution delivery definition does not match its subject")
    payload = definition_resolution.get("payload")
    need(isinstance(payload, dict) and payload.get("delivery") == subject.get("id"),
         error_code, "Execution delivery material does not match its subject")
    binding = payload.get("binding")
    snapshot = payload.get("snapshot")
    need(isinstance(binding, dict) and digest(binding) == subject.get("binding") and
         binding.get("snapshot") == input_snapshot_digest and
         isinstance(snapshot, dict) and snapshot.get("digest") == input_snapshot_digest,
         error_code, "Execution delivery binding or snapshot differs from its subject")


def validate_execution_material_relation(
    *, project: str, ref: dict[str, Any], run_row: dict[str, Any],
    run_body: dict[str, Any], observed: dict[str, Any],
    material_row: dict[str, Any], material_body: dict[str, Any],
    blob_store: Any, resolve_definition=None, resolve_candidate=None,
    resolve_artifact=None, error_code: str = "integrity_error",
) -> dict[str, Any]:
    """Validate the immutable relation between one run, receipt and material.

    Runtime and portable archive inspection use this same read-only contract.
    ``blob_store`` is either the live CAS store or an archive checksum adapter;
    endpoint callbacks resolve the surrounding typed context without granting
    this function authority to mutate or recapture it.
    """
    pin = run_body.get("verification_material")
    receipt_pin = observed.get("verification_material")
    need(isinstance(pin, dict) and set(pin) == {"id", "digest"} and
         isinstance(pin.get("id"), str) and bool(pin.get("id")) and _sha(pin.get("digest")),
         error_code, "Observed execution has no valid immutable verification material pin", ref.get("receipt"))
    need(receipt_pin == pin, error_code, "Run and receipt verification material pins differ")
    need(isinstance(material_row, dict) and material_row.get("project") == project and
         material_row.get("kind") == "material" and material_row.get("digest") == pin["digest"],
         error_code, "Execution material identity differs")
    required_envelope = {"format", "material_kind", "project", "origin", "semantic_digest",
                         "payload_blob", "dependency_refs", "captured_from"}
    need(isinstance(material_body, dict) and set(material_body) == required_envelope and
         material_body.get("format") == MATERIAL_FORMAT and
         material_body.get("material_kind") == EXECUTION_MATERIAL_KIND and
         material_body.get("project") == project and
         _sha(material_body.get("semantic_digest")) and
         _sha(material_body.get("payload_blob")) and
         digest(material_body) == material_row.get("digest"),
         error_code, "Execution material envelope shape or identity differs")
    captured = material_body.get("captured_from")
    need(isinstance(captured, dict) and captured.get("run") == run_row.get("id"),
         error_code, "Execution material capture does not identify this run")
    need(blob_store is not None and callable(getattr(blob_store, "blob_get", None)),
         error_code, "Execution material CAS read boundary is unavailable")
    material_cas_closure(blob_store, material_body["payload_blob"])
    payload = parse_json(blob_store.blob_get(material_body["payload_blob"]), limit=MAX_OBJECT_BYTES)
    payload_keys = {"definition_ref", "execution_subject", "task_revision", "candidate_ref",
                    "input_snapshot_digest", "input_snapshot_blob", "runtime_check_blob",
                    "launch_recipe_blob", "environment_blob", "resolved_timeout",
                    "timeout_authorization_refs", "test_artifact_refs"}
    need(isinstance(payload, dict) and set(payload) == payload_keys and
         material_body["semantic_digest"] == digest(payload),
         error_code, "Execution material payload identity differs")

    definition = validate_typed_ref(payload["definition_ref"], project=project,
                                    expected_kinds={"test_plan_check", "delivery_check"})
    need(callable(resolve_definition), error_code,
         "Execution definition read boundary is unavailable")
    definition_resolution = _validate_execution_definition_resolution(
        definition, resolve_definition(definition), error_code)
    subject = payload["execution_subject"]
    need(isinstance(subject, dict) and set(subject) == {"kind", "id", "binding"},
         error_code, "Execution subject shape differs")
    need(subject.get("kind") in {"task", "delivery"} and
         isinstance(subject.get("id"), str) and bool(subject["id"]) and
         isinstance(subject.get("binding"), str) and bool(subject["binding"]),
         error_code, "Execution subject identity is malformed")
    need(subject["id"] == run_row.get("subject") and subject["binding"] == run_row.get("binding"),
         error_code, "Execution subject does not match the observed run")
    if definition["kind"] == "test_plan_check":
        need(subject["kind"] == "task" and run_row.get("task") == subject["id"],
             error_code, "Task check material is bound to a non-task run")
        need(type(payload["task_revision"]) is int and payload["task_revision"] >= 1 and
             isinstance(payload["candidate_ref"], dict),
             error_code, "Task execution material omits its revision or candidate")
        candidate = validate_typed_ref(payload["candidate_ref"], project=project,
                                       expected_kinds={"candidate"})
        need(candidate["task"] == subject["id"] and
             candidate["task_revision"] == payload["task_revision"],
             error_code, "Candidate material identity differs from its execution subject")
        need(callable(resolve_candidate), error_code,
             "Execution candidate read boundary is unavailable")
        candidate_resolution = _validate_execution_candidate_resolution(
            candidate, resolve_candidate(candidate), error_code)
    else:
        need(subject["kind"] == "delivery" and run_row.get("task") is None and
             payload["task_revision"] is None and payload["candidate_ref"] is None,
             error_code, "Delivery check material carries task-only identity")
        candidate = None
        candidate_resolution = None

    input_digest = payload["input_snapshot_digest"]
    need(_sha(input_digest), error_code, "Execution material input snapshot digest is malformed")
    _validate_execution_subject_definition_relation(
        project=project, definition=definition,
        definition_resolution=definition_resolution, subject=subject,
        task_revision=payload["task_revision"], candidate=candidate,
        candidate_resolution=candidate_resolution,
        input_snapshot_digest=input_digest, error_code=error_code)
    for name in ("input_snapshot_blob", "runtime_check_blob", "launch_recipe_blob", "environment_blob"):
        need(_sha(payload[name]), error_code, f"Execution material {name} is malformed")
    snapshot = parse_json(blob_store.blob_get(payload["input_snapshot_blob"]), limit=MAX_OBJECT_BYTES)
    need(validate_sealed_snapshot(blob_store, snapshot) == input_digest == observed.get("snapshot") ==
         run_body.get("snapshot") == ref.get("snapshot_digest"),
         error_code, "Execution snapshot identity differs")

    runtime_check = parse_json(blob_store.blob_get(payload["runtime_check_blob"]), limit=MAX_OBJECT_BYTES)
    need(isinstance(runtime_check, dict) and runtime_check.get("id") == definition.get("check_id"),
         error_code, "Runtime check is not the pinned definition check")
    need(observed.get("check_id") == definition.get("check_id") and
         observed.get("check_digest") == digest(runtime_check),
         error_code, "Receipt check identity differs from execution material")

    launch = parse_json(blob_store.blob_get(payload["launch_recipe_blob"]), limit=MAX_OBJECT_BYTES)
    launch_keys = {"format", "argv", "cwd_snapshot_relative", "trusted_wrapper_version",
                   "generator", "python_executable", "python_replacement", "pytest_added_args"}
    need(isinstance(launch, dict) and set(launch) == launch_keys and
         isinstance(launch.get("argv"), list) and launch["argv"] == run_body.get("argv") and
         launch.get("cwd_snapshot_relative") == run_body.get("cwd_repo"),
         error_code, "Launch recipe differs from the observed run")
    environment = parse_json(blob_store.blob_get(payload["environment_blob"]), limit=MAX_OBJECT_BYTES)
    need(isinstance(environment, dict) and
         set(environment) == {"format", "effective_environment", "extra_env", "managed_context"} and
         isinstance(environment.get("effective_environment"), dict) and
         isinstance(environment.get("extra_env"), dict) and
         isinstance(environment.get("managed_context"), dict),
         error_code, "Execution environment material shape differs")
    run_environment = run_body.get("environment")
    need(isinstance(run_environment, dict) and
         run_environment.get("effective_environment_digest") == digest(environment["effective_environment"]),
         error_code, "Execution environment differs from the recorded Popen environment")
    need(type(payload["resolved_timeout"]) in {int, float} and
         not isinstance(payload["resolved_timeout"], bool) and
         math.isfinite(float(payload["resolved_timeout"])) and payload["resolved_timeout"] > 0 and
         float(payload["resolved_timeout"]) == float(run_body.get("timeout")),
         error_code, "Resolved execution timeout differs")
    need(isinstance(payload["timeout_authorization_refs"], list),
         error_code, "Timeout authorization references are malformed")

    artifacts = payload["test_artifact_refs"]
    need(isinstance(artifacts, list), error_code, "Execution test artifact references are malformed")
    need(callable(resolve_artifact), error_code, "Execution artifact read boundary is unavailable")
    normalized_artifacts: list[dict[str, Any]] = []
    seen_artifacts: set[str] = set()
    for index, artifact_ref in enumerate(artifacts):
        artifact_ref = validate_typed_ref(artifact_ref, project=project, expected_kinds={"artifact"})
        artifact_identity = _material_identity(artifact_ref)
        artifact_key = digest(artifact_identity)
        need(artifact_key not in seen_artifacts, error_code,
             "Execution test artifact references contain duplicates", index)
        seen_artifacts.add(artifact_key)
        resolved_artifact = resolve_artifact(artifact_ref)
        if isinstance(resolved_artifact, tuple):
            artifact_row, artifact_body = resolved_artifact
        else:
            artifact_row, artifact_body = resolved_artifact, None
        need(isinstance(artifact_row, dict) and artifact_row.get("kind") == "test",
             error_code, "Execution artifact is not a canonical test artifact", artifact_ref["artifact"])
        normalized_artifacts.append(artifact_identity)
    dependencies = [_material_identity(definition)]
    if candidate is not None:
        dependencies.append(_material_identity(candidate))
    dependencies.extend(normalized_artifacts)
    stored_dependencies = material_body.get("dependency_refs")
    need(isinstance(stored_dependencies, list) and
         [_material_identity(item) for item in stored_dependencies] == dependencies,
         error_code, "Execution material dependency closure differs")
    return {"material_row": material_row, "material_body": material_body, "payload": payload,
            "definition_ref": _material_identity(definition), "definition_resolution": definition_resolution,
            "candidate_ref": _material_identity(candidate) if candidate is not None else None,
            "candidate_resolution": candidate_resolution,
            "test_artifact_refs": normalized_artifacts, "dependencies": dependencies,
            "snapshot": snapshot, "runtime_check": runtime_check, "launch_recipe": launch,
            "environment": environment}


def _json_field(row: dict[str, Any], name: str = "body") -> Any:
    value = row.get(name)
    return parse_json(value, limit=MAX_OBJECT_BYTES) if isinstance(value, str) else value


def _canonical_unique(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return immutable projection values in canonical byte order.

    A structural projection may encounter the same immutable body through a
    repeated storage read.  Deduplicating the complete projected value keeps
    that storage detail out of the digest while retaining every distinct
    logical subject and body.
    """
    unique: dict[bytes, dict[str, Any]] = {}
    for value in values:
        marker = canonical(value)
        unique.setdefault(marker, value)
    return [unique[marker] for marker in sorted(unique)]


def _is_ref(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value.get("kind") not in TYPED_REF_KINDS:
        return False
    # A mutable selector has the same discriminator but lacks its retained
    # pin.  It is metadata in a material envelope, never a typed dependency.
    if value.get("kind") in {"test_plan", "change", "delivery_snapshot", "actual_delivery_commit"} and "pin" not in value:
        return False
    return True


def _walk_refs(value: Any, path: str = "body") -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    if _is_ref(value):
        result.append((path, value))
        for key in ("plan", "delivery", "population", "check", "observed"):
            if key in value and isinstance(value[key], dict):
                result.extend(_walk_refs(value[key], f"{path}.{key}"))
        return result
    if isinstance(value, dict):
        for key in sorted(value):
            result.extend(_walk_refs(value[key], f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(_walk_refs(item, f"{path}[{index}]"))
    return result


def _indexed_reference(path: str, raw: dict[str, Any], project: str) -> dict[str, Any]:
    """Project both generic and wrapped pins into the SQL index shape."""
    ref = validate_typed_ref(raw, project=project)
    locator = ref.get("locator") or {}
    if ref.get("kind") == "traceability_ref":
        ident = (locator.get("source_id") or locator.get("artifact") or locator.get("candidate") or
                 locator.get("path") or locator.get("qualified_name") or path)
        revision = locator.get("pin_revision", locator.get("revision", 0))
        ref_digest = ref["identity_digest"]
    elif ref.get("kind") == "artifact":
        ident, revision, ref_digest = ref["artifact"], ref["revision"], ref["body_digest"]
    elif ref.get("kind") == "task_revision":
        ident, revision, ref_digest = ref["task"], ref["revision"], ref["definition_digest"]
    elif ref.get("kind") == "candidate":
        ident, revision, ref_digest = ref["candidate"], ref["task_revision"], ref["candidate_digest"]
    elif ref.get("kind") == "source":
        ident, revision, ref_digest = ref["source"], 0, ref["blob_digest"]
    elif ref.get("kind") == "population":
        ident, revision, ref_digest = ref["revision"], 0, ref["population_digest"]
    elif ref.get("kind") == "population_item":
        ident, revision, ref_digest = ref["item"], 0, ref["item_digest"]
    elif ref.get("kind") == "assurance_object":
        ident, revision, ref_digest = ref["object"], 0, ref["object_digest"]
    elif ref.get("kind") == "test_plan":
        ident, revision, ref_digest = ref["task"], ref["task_revision"], ref["plan_digest"]
    elif ref.get("kind") == "test_plan_check":
        ident, revision, ref_digest = ref["check_id"], 0, ref["check_digest"]
    elif ref.get("kind") == "delivery_check":
        ident, revision, ref_digest = ref["check_id"], 0, ref["check_digest"]
    elif ref.get("kind") == "delivery_snapshot":
        ident, revision, ref_digest = ref["delivery"], 0, ref["snapshot_digest"]
    elif ref.get("kind") == "actual_delivery_commit":
        ident, revision, ref_digest = ref["commit"], 0, ref["delivery"]["snapshot_digest"]
    elif ref.get("kind") == "observed_result":
        ident, revision, ref_digest = ref["receipt"], 0, ref["result_digest"]
    elif ref.get("kind") == "output_artifact":
        ident, revision, ref_digest = ref["output_id"], 0, ref["output_digest"]
    elif ref.get("kind") == "change":
        ident, revision, ref_digest = ref["change"], ref["revision"], ref["body_digest"]
    elif ref.get("kind") == "proposal":
        ident, revision, ref_digest = ref["proposal"], 0, ref["proposal_digest"]
    else:
        # The closed validator should make this unreachable; keeping the
        # explicit failure prevents a future kind from becoming an ID-only ref.
        raise Fault("unknown_reference", "Reference kind has no index projection", ref.get("kind"))
    need(isinstance(ident, str) and bool(ident), "invalid_reference", "Reference index identity is invalid")
    need((type(revision) is int and revision >= 0) or (isinstance(revision, str) and bool(revision) and "\x00" not in revision),
         "invalid_reference", "Reference index revision is invalid")
    # The registry spans integer workflow revisions and string TREV IDs.  The
    # SQL index therefore stores the lossless textual projection for both;
    # callers must use the typed ref itself for the original scalar type.
    return {"purpose": path, "kind": semantic_kind(ref), "id": ident,
            "revision": str(revision), "digest": ref_digest, "ref": ref}


def _object_contract(kind: str, body: dict[str, Any]) -> None:
    need(kind in OBJECT_KINDS, "invalid_object_kind", "Unknown assurance object kind", kind)
    need(body.get("project") and isinstance(body.get("project"), str),
         "invalid_object", "Assurance object body must identify its project")
    expected = {
        "edge": {"format", "project", "source_ref", "target_ref", "relation", "relation_contract_digest",
                 "scope_ref", "claim", "obligation_ids", "required_evidence_refs", "authority_refs"},
        "set": {"format", "project", "center_ref", "relation", "direction", "scope_ref",
                "relation_contract_digest", "expected_obligations_ref", "selected_edge_manifest_ref",
                "coverage_assignment_ref", "required_evidence_refs", "criteria", "partition_manifest_ref"},
    }
    if kind in expected:
        need(expected[kind] <= set(body), "invalid_object", f"{kind} object is missing required fields",
             sorted(expected[kind] - set(body)))
        need(body.get("relation_contract_digest") in {REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST},
             "invalid_registry", "Object relation registry digest differs")
    if kind == "set":
        need(body.get("direction") in {"outgoing", "incoming"},
             "invalid_object", "Relation-set direction is invalid")
        need(isinstance(body.get("relation"), str), "invalid_object", "Relation-set relation is invalid")
        if body.get("format") == "assurance.set.v1":
            need(isinstance(body.get("criteria"), dict) and
                 all(type(value) is bool for value in body["criteria"].values()),
                 "invalid_object", "Relation-set achieved criteria are invalid")
            requirements = body.get("criteria_requirements")
            if requirements is not None:
                need(isinstance(requirements, list) and
                     all(isinstance(value, str) and value for value in requirements) and
                     requirements == sorted(set(requirements)),
                     "invalid_object", "Relation-set criteria requirements are invalid")
                need(body.get("criteria_requirements_digest") == digest(requirements),
                     "integrity_error", "Relation-set criteria requirements digest differs")
    if kind == "scope" and body.get("format") == "assurance.scope.v1":
        need(isinstance(body.get("roots"), list), "invalid_object", "Scope roots are invalid")
    if kind == "obligations" and body.get("format") == "assurance.obligations.v1":
        need(isinstance(body.get("obligations"), list), "invalid_object", "Obligations are invalid")
    if kind == "profile" and body.get("format") == "assurance.profile.v1":
        need(isinstance(body.get("stage_rules"), dict), "invalid_object", "Profile stage rules are invalid")
    if kind == "profile" and body.get("format") == PROFILE_V2_FORMAT:
        _validate_profile_v2_wire(body)
    if kind == "profile" and body.get("format") == PROFILE_V3_FORMAT:
        _validate_profile_v3_wire(body)
    if kind == "profile" and body.get("format") == PROFILE_V4_FORMAT:
        _validate_profile_v4_wire(body)
    if kind == "profile" and body.get("format") == PROFILE_V5_FORMAT:
        _validate_profile_v5_wire(body)
    if (kind == "scope" and body.get("format") == SCOPE_V2) or (kind == "obligations" and body.get("format") == OBLIGATIONS_V2):
        validate_v2_body(kind, body)
    if kind == "packet" and body.get("format") == "assurance.review-packet.v1":
        need(isinstance(body.get("partition"), dict), "invalid_object", "Review packet partition is invalid")
        need(isinstance(body.get("leaf_manifest"), list) and len(body["leaf_manifest"]) <= MAX_PAGE,
             "invalid_object", "Review packet leaf manifest exceeds its bound")
    if kind == "material":
        required = {"format", "material_kind", "project", "origin", "semantic_digest", "payload_blob",
                    "dependency_refs", "captured_from"}
        need(set(body) == required and body.get("format") == "daikibo.assurance-material.v1",
             "invalid_object", "Material envelope shape differs")
        need(isinstance(body.get("material_kind"), str) and bool(body["material_kind"]),
             "invalid_object", "Material kind is invalid")
        need(isinstance(body.get("origin"), dict) and isinstance(body.get("captured_from"), dict),
             "invalid_object", "Material provenance is invalid")
        _sha(body.get("semantic_digest"), "material.semantic_digest")
        _sha(body.get("payload_blob"), "material.payload_blob")
        need(isinstance(body.get("dependency_refs"), list), "invalid_object", "Material dependencies are invalid")
        for dependency in body["dependency_refs"]:
            validate_typed_ref(dependency, project=body["project"])
    # Extra fields are allowed for forward-compatible storage, but a body may
    # not quietly carry a second project identity.
    need("project" in body and isinstance(body["project"], str), "invalid_object", "Object project is invalid")


def _locator_digest(locator: dict[str, Any]) -> str:
    return digest(locator)


class Assurance:
    """E1 immutable storage, resolver, archive and bounded-read boundary."""

    def __init__(self, control):
        self.c = control
        self.s = control.s
        governance = getattr(control, "g", None)
        if governance is not None:
            # Standalone test compositions still use the same Governance
            # reader.  Binding the immutable Assurance service here gives
            # that reader an explicit provider without constructing a hidden
            # Control or a second Store.
            governance.assurance = self
        from .traceability_refs import TraceabilityRefResolver, _LivePinnedContext
        self._trace_refs = TraceabilityRefResolver(control)
        # Candidate identity is shared with the live Unit B resolver and both
        # archive readers.  Keep one read-only adapter here; the assurance
        # caller must not recreate a second, weaker candidate validator.
        self._candidate_context = _LivePinnedContext(self._trace_refs)

    def _project(self, actor, project: str, *, read: bool = False) -> None:
        actor.require("owner", "agent", "reviewer", "observer", project=project)
        # Project lookup is also the explicit boundary for references; the
        # resolver must never inspect a missing/cross-project project first.
        self.c.k.project(actor, project)

    def catalog(self, actor, contract_digest: str | None = None) -> dict[str, Any]:
        actor.require("owner", "agent", "reviewer", "observer")
        need(contract_digest is None or isinstance(contract_digest, str),
             "invalid_registry", "Relation registry digest is invalid")
        from .db import SCHEMA_VERSION
        profile_v2 = {
                    "format": PROFILE_V2_FORMAT,
                    "fields": sorted(PROFILE_V2_FIELDS),
                    "logical_id": PROFILE_V2_LOGICAL_PREFIX + "<program>",
                    "stages": {
                        stage: {
                            "denominator": PROFILE_DENOMINATORS[stage],
                            "execution_results": PROFILE_EXECUTION_RESULTS[stage],
                            "rule_fields": ["denominator", "relation_sets", "node_rules", "execution_results"],
                        } for stage in PROFILE_STAGES
                    },
                    "application_modes": ["disabled", "mandatory"],
                    "relation_centers": sorted(PROFILE_RELATION_CENTERS),
                    "node_selectors": sorted(PROFILE_NODE_SELECTOR_SET),
                    "node_roles": sorted(PROFILE_NODE_ROLE_SET),
                    "node_minimum_roles": dict(PROFILE_NODE_SELECTORS),
                    "selection": {
                        "head_source": "assurance_heads",
                        "cas": True,
                        "previous_selection_required_after_bootstrap": True,
                        "legacy_state": ["migration_pending", "not_enabled"],
                    },
                }
        profile_v3 = dict(profile_v2)
        profile_v3["format"] = PROFILE_V3_FORMAT
        profile_v3["fields"] = sorted(PROFILE_V3_FIELDS)
        profile_v3["required_relation_contract_digest"] = REGISTRY_V2_DIGEST
        profile_v4 = dict(profile_v3)
        profile_v4["format"] = PROFILE_V4_FORMAT
        profile_v4["relation_centers"] = sorted(PROFILE_V4_RELATION_CENTERS)
        profile_v4["selector_constraints"] = {"realization_sources": {
            "stages": ["plan", "task"], "relation": "realizes", "direction": "outgoing",
            "artifact_kinds": ["component", "design", "interface"],
        }}
        profile_v5 = {**profile_v4, "format":PROFILE_V5_FORMAT, "fields":sorted(PROFILE_V5_FIELDS),
                      "required_scope_contract":SCOPE_V2, "required_node_contract":NODE_V2,
                      "node_selectors":sorted({**PROFILE_NODE_SELECTORS, **DOMAIN_SELECTORS}),
                      "node_roles":sorted(PROFILE_NODE_ROLE_SET | {DOMAIN_ROLE}),
                      "node_minimum_roles":{**PROFILE_NODE_SELECTORS, **DOMAIN_SELECTORS}}
        profile_v5["contract_digest"] = digest(profile_v5)
        return {"format": "daikibo.assurance-catalog.v1", **relation_catalog(contract_digest=contract_digest),
                "schema": SCHEMA_VERSION,
                "limits": {"max_object_bytes": MAX_OBJECT_BYTES, "max_page": MAX_PAGE},
                # Keep the historical singular v2 projection byte-for-byte
                # compatible for clients that already consume it.  v3 is
                # published beside it so selection cannot be inferred from a
                # format-less catalog merge.
                "profile": profile_v2,
                "profiles": {"v2": profile_v2, "v3": profile_v3, "v4": profile_v4, "v5":profile_v5},
                "profile_v5":profile_v5,
                "profile_v4": profile_v4,
                "profile_v3": profile_v3,
                "e1": {"immutable_objects": True, "immutable_events": True, "cas_heads": True,
                       "typed_refs": True, "bounded_history": True, "review_adoption": False},
                "e3": {"stage_evaluator": False, "gate_routes": False,
                       "profile_v2_parser": True, "profile_v3_parser": True, "profile_v4_parser": True, "profile_v5_parser":True,
                       "canonical_selection": True}}

    def _decode_object(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["body"] = _json_field(result)
        need(isinstance(result["body"], dict) and digest(result["body"]) == row["digest"],
             "integrity_error", "Assurance object body digest differs", row.get("id"))
        result["refs"] = self.s.all("SELECT ordinal,purpose,ref_kind,ref_id,ref_revision,ref_digest FROM assurance_refs WHERE object_id=? ORDER BY ordinal", (row["id"],))
        return result

    def _body_refs(self, project: str, kind: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        found = []
        seen: set[tuple[str, str, int, str]] = set()
        for purpose, raw in _walk_refs(body):
            indexed = _indexed_reference(purpose, raw, project); ref = indexed.pop("ref")
            ident = (indexed["purpose"], indexed["kind"], indexed["id"], indexed["revision"], indexed["digest"])
            need(ident not in seen, "duplicate_reference", "An object contains a duplicate typed reference", purpose)
            seen.add(ident)
            found.append(indexed)
        if kind == "edge":
            source, target = validate_relation(body["relation"], body["source_ref"], body["target_ref"],
                                               project=project, contract_digest=body["relation_contract_digest"])
            need(source == validate_typed_ref(body["source_ref"], project=project), "invalid_object", "Edge source identity changed")
            need(target == validate_typed_ref(body["target_ref"], project=project), "invalid_object", "Edge target identity changed")
        return found

    def store_object(self, actor, project: str, kind: str, logical_id: str, revision: int,
                     body: dict[str, Any], object_id: str | None = None) -> dict[str, Any]:
        """Store one immutable mechanical object; this is not an adoption API."""
        self._project(actor, project)
        need(isinstance(logical_id, str) and logical_id and "\x00" not in logical_id,
             "invalid_object", "Object logical_id is invalid")
        need(type(revision) is int and revision > 0, "invalid_object", "Object revision must be positive")
        need(isinstance(body, dict), "invalid_object", "Object body must be an object")
        need(body.get("project") == project, "cross_project", "Object body belongs to another project")
        _object_contract(kind, body)
        encoded = canonical(body)
        need(len(encoded) <= MAX_OBJECT_BYTES, "object_too_large", "Assurance object exceeds the bounded body size")
        refs = self._body_refs(project, kind, body)
        if kind == "material":
            payload = parse_json(self.s.blob_get(body["payload_blob"]), limit=MAX_OBJECT_BYTES)
            need(isinstance(payload, dict) and digest(payload) == body["semantic_digest"],
                 "integrity_error", "Material payload CAS differs")
            # Validate every nested execution/snapshot/report CAS edge before
            # publishing the immutable material envelope.  A payload digest
            # alone is insufficient when its children are absent.
            material_cas_closure(self.s, body["payload_blob"])
        oid = object_id or uid("AOBJ")
        need(isinstance(oid, str) and oid, "invalid_object", "Object id is invalid")
        created = timestamp()
        with self.s.transaction():
            need(not self.s.one("SELECT id FROM assurance_objects WHERE id=?", (oid,)),
                 "duplicate_object", "Assurance object id already exists", oid)
            try:
                self.s.execute("INSERT INTO assurance_objects(id,project,kind,logical_id,revision,body,digest,created) VALUES(?,?,?,?,?,?,?,?)",
                               (oid, project, kind, logical_id, revision, encoded.decode(), digest(body), created))
                for ordinal, ref in enumerate(refs):
                    self.s.execute("INSERT INTO assurance_refs(object_id,ordinal,purpose,ref_kind,ref_id,ref_revision,ref_digest) VALUES(?,?,?,?,?,?,?)",
                                   (oid, ordinal, ref["purpose"], ref["kind"], ref["id"], ref["revision"], ref["digest"]))
            except Exception:
                raise
        return self._decode_object(self.s.one("SELECT * FROM assurance_objects WHERE id=?", (oid,), True))

    def pin(self, actor, project: str, selector: dict[str, Any]) -> dict[str, Any]:
        """Pin controller-owned material under one serialized read/write transaction."""
        self._project(actor, project)
        with self.s.transaction():
            return self._pin_unlocked(actor, project, selector)

    def _pin_unlocked(self, actor, project: str, selector: dict[str, Any]) -> dict[str, Any]:
        """Capture controller-owned mutable material into an immutable object.

        The caller supplies only an exact selector.  Payload bytes and their
        digest are read from the controller's current canonical rows inside
        this transaction; arbitrary body/blob pointers are rejected.
        """
        self._project(actor, project)
        need(isinstance(selector, dict) and isinstance(selector.get("kind"), str),
             "invalid_selector", "Material pin selector is invalid")
        kind = selector["kind"]
        if kind == "test_plan":
            need(set(selector) <= {"kind", "task", "task_revision", "plan_digest", "history"} and
                 {"kind", "task", "task_revision", "plan_digest"} <= set(selector),
                 "invalid_selector", "Test plan selector keys differ")
            need(type(selector["task_revision"]) is int and selector["task_revision"] >= 1, "invalid_selector", "Task revision is invalid")
            row = self.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (selector["task"], project))
            need(row is not None, "unresolved_reference", "Selected task is missing")
            if "history" in selector:
                history = selector["history"]
                need(isinstance(history, dict) and set(history) == {"id", "digest", "side"} and history["side"] in {"before", "after"},
                     "invalid_selector", "Historical test plan selector is malformed")
                hrow = self.s.one("SELECT * FROM task_revision_history WHERE id=? AND task=? AND project=?", (history["id"], selector["task"], project))
                need(hrow is not None and hrow["digest"] == history["digest"], "unresolved_reference", "Historical task revision is missing or changed")
                hbody = parse_json(hrow["body"], limit=MAX_OBJECT_BYTES)
                selected = hbody.get(history["side"], {}) if isinstance(hbody, dict) else {}
                selected_task = selected.get("task") if isinstance(selected, dict) else None
                need(isinstance(selected_task, dict) and selected_task.get("id") == selector["task"] and
                     selected_task.get("project") == project, "integrity_error", "Historical task identity differs")
                expected_revision = hrow["from_revision"] if history["side"] == "before" else hrow["to_revision"]
                need(selected_task.get("revision") == expected_revision and expected_revision == selector["task_revision"],
                     "stale_reference", "Historical task revision differs")
                plan_body = selected.get("test_plan") or selected.get("plan")
                need(isinstance(plan_body, dict), "unresolved_reference", "Historical test plan material is not retained")
                task_revision = expected_revision
            else:
                need(row["revision"] == selector["task_revision"], "unresolved_reference", "Selected task revision is not current")
                plan = self.s.one("SELECT * FROM plans WHERE task=?", (selector["task"],))
                need(plan is not None, "unresolved_reference", "Selected task has no test plan")
                plan_body = parse_json(plan["body"], limit=MAX_OBJECT_BYTES)
                need(plan["digest"] == digest(plan_body), "integrity_error", "Current test plan digest differs")
                task_revision = row["revision"]
            need(selector["plan_digest"] == digest(plan_body), "stale_reference", "Test plan digest differs")
            payload = {"task": selector["task"], "task_revision": task_revision, "plan_body": plan_body, "plan_digest": selector["plan_digest"]}
            material_kind = "test_plan"
            ref = {"kind": "test_plan", "project": project, "task": selector["task"], "task_revision": task_revision, "plan_digest": selector["plan_digest"]}
        elif kind == "change":
            need(set(selector) == {"kind", "change", "revision", "body_digest"},
                 "invalid_selector", "Change selector keys differ")
            need(type(selector["revision"]) is int and selector["revision"] >= 1,
                 "invalid_selector", "Change revision is invalid")
            row = self.s.one("SELECT * FROM changes WHERE id=? AND project=?", (selector["change"], project), True)
            need(row["revision"] == selector["revision"], "stale_reference", "Change revision differs")
            body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
            need(digest(body) == selector["body_digest"], "stale_reference", "Change body digest differs")
            payload = {"change": row["id"], "revision": selector["revision"], "body": body}
            material_kind = "change"
            ref = {"kind": "change", "project": project, "change": row["id"], "revision": selector["revision"], "body_digest": selector["body_digest"]}
        elif kind == "delivery_snapshot":
            need(set(selector) == {"kind", "delivery", "binding_digest", "snapshot_digest"},
                 "invalid_selector", "Delivery snapshot selector keys differ")
            row = self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (selector["delivery"], project))
            need(row is not None, "unresolved_reference", "Selected delivery is missing", selector["delivery"])
            body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
            need(isinstance(body, dict), "integrity_error", "Delivery body is malformed")
            need(row["digest"] == selector["binding_digest"], "stale_reference", "Delivery binding digest differs")
            snapshot = body.get("snapshot")
            need(isinstance(body.get("binding"), dict),
                 "integrity_error", "Delivery body is malformed")
            need(digest(body["binding"]) == selector["binding_digest"],
                 "integrity_error", "Delivery binding digest differs")
            need(isinstance(snapshot, dict), "unresolved_reference", "Selected delivery has no snapshot")
            need(validate_sealed_snapshot(self.s, snapshot) == selector["snapshot_digest"],
                 "stale_reference", "Delivery snapshot digest differs")
            produced_ref, _ = self.c.rt.verification_materials.pin_delivery_snapshot(
                actor, project, row, body,
                captured_from={"controller": "delivery", "operation": "assurance.pin.delivery_snapshot"})
            return {"pin": produced_ref["pin"], "ref": produced_ref, "created": True}
        elif kind == "output_artifact":
            # Output selectors name only the immutable nested Delivery/check/
            # receipt identities.  The six output fields are read from the
            # authoritative receipt and Delivery rows by the shared adapter.
            need(set(selector) == {"kind", "delivery", "check_id", "receipt", "output_id"},
                 "invalid_selector", "Output artifact selector keys differ")
            for field in ("check_id", "receipt", "output_id"):
                need(isinstance(selector[field], str) and bool(selector[field]) and
                     "\x00" not in selector[field],
                     "invalid_selector", "Output artifact selector identity is invalid", field)
            delivery_ref = validate_typed_ref(selector["delivery"], project=project,
                                               expected_kinds={"delivery_snapshot"})
            delivery_ref = self._identity(delivery_ref)
            self._resolve_locator(actor, delivery_ref, current=True)
            captured = capture_output_material(
                self.c, actor, project=project, delivery_ref=delivery_ref,
                check_id=selector["check_id"], receipt_id=selector["receipt"],
                output_id=selector["output_id"])
            material, created = self.store_material(
                actor, project, OUTPUT_MATERIAL_KIND, captured["payload"],
                captured["dependencies"],
                {"material": "output_artifact", "id": selector["output_id"],
                 "delivery": delivery_ref, "check_id": selector["check_id"],
                 "receipt": selector["receipt"]},
                {"controller": "delivery", "operation": "assurance.pin.output_artifact"})
            return {"pin": {"id": material["id"], "digest": material["digest"]},
                    "ref": captured["ref"], "created": created}
        elif kind == "actual_delivery_commit":
            need(set(selector) == {"kind", "delivery", "binding_digest", "snapshot_digest", "repository", "commit", "tree"},
                 "invalid_selector", "Actual delivery commit selector keys differ")
            row = self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (selector["delivery"], project))
            need(row is not None, "unresolved_reference", "Selected delivery is missing", selector["delivery"])
            body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
            need(isinstance(body, dict), "integrity_error", "Delivery body is malformed")
            need(row["digest"] == selector["binding_digest"], "stale_reference", "Delivery binding digest differs")
            snapshot = body.get("snapshot")
            need(isinstance(snapshot, dict) and validate_sealed_snapshot(self.s, snapshot) == selector["snapshot_digest"],
                 "stale_reference", "Delivery snapshot digest differs")
            result = (body.get("git") or {}).get(selector["repository"])
            need(isinstance(result, dict) and result.get("commit") == selector["commit"] and
                 result.get("tree") == selector["tree"],
                 "stale_reference", "Selected delivery commit differs")
            produced_ref, _ = self.c.rt.verification_materials.pin_actual_delivery_commit(
                actor, project, row, body, selector["repository"], result,
                captured_from={"controller": "delivery", "operation": "assurance.pin.actual_delivery_commit"})
            return {"pin": produced_ref["pin"], "ref": produced_ref, "created": True}
        else:
            raise Fault("unknown_reference", "No E1 material pin selector is implemented for this kind", kind)
        material, created = self.store_material(
            actor, project, material_kind, payload, [],
            {"kind": kind, "id": selector.get("task", selector.get("change"))},
            {"selector": selector})
        ref["pin"] = {"id": material["id"], "digest": material["digest"]}
        return {"pin": ref["pin"], "ref": ref, "created": created}

    def store_material(self, actor, project: str, material_kind: str, payload: dict[str, Any],
                       dependency_refs: list[dict[str, Any]], origin: dict[str, Any],
                       captured_from: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Internal E1 writer used by runtime/delivery pin adapters.

        Callers provide controller-generated observed payload and exact typed
        dependencies; this method creates the canonical material envelope,
        CAS leaf, and immutable material object atomically.  It is deliberately
        not exported as a generic body/object write route.
        """
        self._project(actor, project)
        need(isinstance(material_kind, str) and material_kind and "\x00" not in material_kind,
             "invalid_material", "Material kind is invalid")
        need(isinstance(payload, dict) and isinstance(dependency_refs, list) and isinstance(origin, dict) and isinstance(captured_from, dict),
             "invalid_material", "Material input is malformed")
        encoded_payload = canonical(payload)
        need(len(encoded_payload) <= MAX_OBJECT_BYTES, "material_too_large", "Material payload exceeds the bounded size")
        normalized_dependencies=[]
        for dependency in dependency_refs:
            normalized=validate_typed_ref(dependency, project=project)
            normalized_dependencies.append({k:v for k,v in normalized.items() if k != "identity_digest"})
        payload_blob=self.s.blob_put(encoded_payload)
        # The payload is written before the envelope, so nested child pointers
        # can be checked against the same store in this transaction boundary.
        material_cas_closure(self.s, payload_blob)
        body={"format":"daikibo.assurance-material.v1","material_kind":material_kind,
              "project":project,"origin":origin,"semantic_digest":digest(payload),
              "payload_blob":payload_blob,"dependency_refs":normalized_dependencies,
              "captured_from":captured_from}
        material_digest=digest(body)
        existing=self.s.one("SELECT * FROM assurance_objects WHERE project=? AND kind='material' AND digest=?",(project,material_digest))
        if existing:return self._decode_object(existing),False
        material=self.store_object(actor,project,"material",f"{material_kind}:{material_digest}",1,body)
        return material,True

    # ------------------------------------------------------------------
    # E2 proposal, packet and adoption boundary
    # ------------------------------------------------------------------
    @staticmethod
    def _identity(value: Any) -> Any:
        if isinstance(value, dict):
            # ``identity_digest`` and ``semantic_kind`` are resolver-derived
            # projections, never part of the exact persisted ref syntax.
            return {key: Assurance._identity(item) for key, item in value.items()
                    if key not in {"identity_digest", "semantic_kind"}}
        if isinstance(value, list):
            return [Assurance._identity(item) for item in value]
        return value

    @staticmethod
    def _delivery_identity(value: Any) -> Any:
        """Project a Delivery subject without selecting a material pin."""
        normalized = Assurance._identity(value)
        if isinstance(normalized, dict):
            normalized.pop("pin", None)
        return normalized

    def _object_ref(self, row: dict[str, Any]) -> dict[str, Any]:
        need(row["kind"] in {"scope", "obligations", "profile", "edge", "set"},
             "invalid_reference", "Only reviewable assurance objects have public typed refs")
        return {"kind": "assurance_object", "project": row["project"], "object": row["id"],
                "object_kind": row["kind"], "object_digest": row["digest"]}

    def _object_by_ref(self, ref: dict[str, Any], project: str, *, kinds: set[str] | None = None) -> dict[str, Any]:
        normalized = validate_typed_ref(self._identity(ref), project=project, expected_kinds={"assurance_object"})
        if kinds is not None:
            need(normalized["object_kind"] in kinds, "invalid_reference", "Assurance object kind is not allowed")
        row = self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind=?",
                         (normalized["object"], project, normalized["object_kind"]))
        need(row is not None, "unresolved_reference", "Assurance object reference is missing", normalized["object"])
        need(row["digest"] == normalized["object_digest"], "integrity_error", "Assurance object reference digest differs", normalized["object"])
        return self._decode_object(row)

    def _structural_object(self, project: str, row: dict[str, Any], actor: Actor) -> dict[str, Any]:
        """Validate and project one immutable proposal without changing state."""
        need(row.get("project") == project and row.get("kind") in
             {"scope", "profile", "edge", "set", "obligations"},
             "integrity_error", "Assurance structural object crosses its project or kind boundary",
             row.get("id"))
        need(isinstance(row.get("logical_id"), str) and bool(row["logical_id"]) and
             "\x00" not in row["logical_id"] and type(row.get("revision")) is int and row["revision"] > 0,
             "integrity_error", "Assurance structural object identity is malformed", row.get("id"))
        decoded = self._decode_object(row)
        body = decoded["body"]
        need(isinstance(body, dict) and body.get("project") == project,
             "integrity_error", "Assurance structural object body crosses its project boundary",
             row.get("id"))
        _object_contract(row["kind"], body)

        # _decode_object verifies the body digest.  The reference index is a
        # second immutable projection and must agree with the exact typed refs
        # in that body; otherwise a reader could silently lose a dependency.
        indexed = self._body_refs(project, row["kind"], body)
        stored = [
            {"purpose": item["purpose"], "kind": item["ref_kind"], "id": item["ref_id"],
             "revision": str(item["ref_revision"]), "digest": item["ref_digest"]}
            for item in decoded.get("refs", [])
        ]
        expected = [
            {"purpose": item["purpose"], "kind": item["kind"], "id": item["id"],
             "revision": str(item["revision"]), "digest": item["digest"]}
            for item in indexed
        ]
        need(stored == expected, "integrity_error", "Assurance structural reference index differs",
             row.get("id"))

        # Resolve each retained dependency through the existing read boundary.
        # current=False is deliberate: a historical proposal remains a
        # structural input, while currentness and adoption stay in their
        # existing gates.  The observer capability is scoped to this project
        # and cannot create pins, receipts, reviews, or events.
        for _path, raw in _walk_refs(body):
            normalized = validate_typed_ref(self._identity(raw), project=project)
            self._resolve_locator(actor, normalized, current=False)
        return {"kind": row["kind"], "logical_id": row["logical_id"], "body": body}

    def _structural_event_body(self, row: dict[str, Any], project: str) -> dict[str, Any]:
        need(row.get("project") == project and row.get("event_kind") in
             {"adopt", "withdraw", "supersede"},
             "integrity_error", "Assurance head event is malformed", row.get("id"))
        body = _json_field(row)
        need(isinstance(body, dict), "integrity_error", "Assurance head event body is malformed", row.get("id"))
        return body

    def _structural_head_projection(self, project: str, head: dict[str, Any],
                                     object_rows: dict[str, dict[str, Any]],
                                     object_projections: dict[str, dict[str, Any]],
                                     rows: dict[str, dict[str, Any]], actor: Actor) -> dict[str, Any]:
        """Validate one head and its full immutable event chain."""
        logical_id = head.get("logical_id")
        event_id = head.get("head_event")
        need(isinstance(logical_id, str) and logical_id and isinstance(event_id, str) and event_id,
             "integrity_error", "Assurance head identity is malformed", logical_id)
        event = rows.get(event_id)
        need(event is not None and event.get("project") == project,
             "integrity_error", "Assurance head points to a missing event", event_id)

        # The archive validator and the live append path share these chain
        # invariants.  Recheck them while reading so a corrupt head cannot be
        # turned into an empty progress projection.
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor = event
        while cursor is not None:
            cursor_id = cursor.get("id")
            need(isinstance(cursor_id, str) and cursor_id not in seen,
                 "integrity_error", "Assurance event chain contains a cycle", cursor_id)
            seen.add(cursor_id)
            need(cursor.get("project") == project and cursor.get("subject_id") in object_rows,
                 "integrity_error", "Assurance event subject is missing", cursor_id)
            subject_row = object_rows[cursor["subject_id"]]
            need(subject_row.get("logical_id") == logical_id and
                 subject_row.get("digest") == cursor.get("subject_digest"),
                 "integrity_error", "Assurance event subject identity differs", cursor_id)
            body = self._structural_event_body(cursor, project)
            previous_id = cursor.get("previous")
            need(cursor.get("expected_head") == previous_id,
                 "integrity_error", "Assurance event compare-and-swap record differs", cursor_id)
            if previous_id is None:
                previous = None
            else:
                previous = rows.get(previous_id)
                need(previous is not None and previous.get("project") == project,
                     "integrity_error", "Assurance event chain is dangling", previous_id)
                need(object_rows.get(previous.get("subject_id"), {}).get("logical_id") == logical_id,
                     "integrity_error", "Assurance event chain crosses logical identities", cursor_id)
            chain.append(cursor)

            subject_body = object_projections[cursor["subject_id"]]["body"]
            if subject_row.get("kind") == "profile" and _is_canonical_profile(subject_body.get("format")):
                previous_ref = None
                if previous is not None:
                    previous_subject = object_rows.get(previous.get("subject_id"))
                    need(previous_subject is not None and previous_subject.get("kind") == "profile" and
                         previous_subject.get("digest") == previous.get("subject_digest"),
                         "integrity_error", "Profile event predecessor subject differs", cursor_id)
                    previous_ref = {
                        "kind": "assurance_object", "project": project,
                        "object": previous_subject["id"], "object_kind": "profile",
                        "object_digest": previous_subject["digest"],
                    }
                _validate_profile_event_predecessor(cursor, body, subject_body,
                                                    previous_ref, code="integrity_error")
            cursor = previous

        logical_events = {row["id"] for row in rows.values()
                          if row.get("project") == project and
                          object_rows.get(row.get("subject_id"), {}).get("logical_id") == logical_id}
        need(set(seen) == logical_events,
             "integrity_error", "Assurance head omits an event in its logical history", logical_id)
        incoming = {row.get("previous") for row in rows.values()
                    if row.get("project") == project and
                    object_rows.get(row.get("subject_id"), {}).get("logical_id") == logical_id and
                    row.get("previous") is not None}
        need(event_id not in incoming, "integrity_error", "Assurance head is not the chain tip", logical_id)

        subject = object_projections[event["subject_id"]]
        return {
            "logical_id": logical_id,
            "event_kind": event["event_kind"],
            "subject": subject,
            "body": self._structural_event_body(event, project),
        }

    def structural_progress_projection(self, project: str) -> dict[str, Any]:
        """Return the deterministic Assurance input for supervisor progress.

        This is intentionally a read-only owner projection.  It includes
        immutable proposal meaning and the current CAS head meaning, while
        excluding packet/material bookkeeping and event/storage identities.
        """
        with self.s.lock:
            self.s.one("SELECT id FROM projects WHERE id=?", (project,), True)
            actor = Actor("supervisor-assurance-projection", "observer", project)
            object_rows = self.s.all(
                "SELECT * FROM assurance_objects WHERE project=? "
                "AND kind IN ('scope','profile','edge','set','obligations') "
                "ORDER BY kind,logical_id,revision,id", (project,))
            object_projections: list[dict[str, Any]] = []
            object_rows_by_id: dict[str, dict[str, Any]] = {}
            object_projections_by_id: dict[str, dict[str, Any]] = {}
            for row in object_rows:
                projection = self._structural_object(project, row, actor)
                object_rows_by_id[row["id"]] = row
                object_projections_by_id[row["id"]] = projection
                object_projections.append(projection)

            event_rows = self.s.all("SELECT * FROM assurance_events WHERE project=? ORDER BY id", (project,))
            events = {row["id"]: row for row in event_rows}
            need(len(events) == len(event_rows), "integrity_error", "Duplicate assurance event identity")
            for row in event_rows:
                need(row.get("subject_id") in object_rows_by_id and
                     object_projections_by_id[row["subject_id"]]["body"].get("project") == project,
                     "integrity_error", "Assurance event subject is missing", row.get("id"))

            head_rows = self.s.all(
                "SELECT project,logical_id,head_event FROM assurance_heads "
                "WHERE project=? ORDER BY logical_id", (project,))
            heads_by_key = {(row.get("project"), row.get("logical_id")): row for row in head_rows}
            need(len(heads_by_key) == len(head_rows), "integrity_error", "Duplicate assurance head")
            event_logicals = {
                (row.get("project"), object_rows_by_id[row["subject_id"]]["logical_id"])
                for row in event_rows
            }
            need(event_logicals <= set(heads_by_key),
                 "integrity_error", "Assurance event history has no head projection")
            head_projections = [
                self._structural_head_projection(project, row, object_rows_by_id,
                                                  object_projections_by_id, events, actor)
                for row in head_rows
            ]

            withdrawals: list[dict[str, Any]] = []
            packet_rows = self.s.all(
                "SELECT * FROM assurance_objects WHERE project=? AND kind='packet' "
                "ORDER BY logical_id,revision,id", (project,))
            for row in packet_rows:
                packet = self._decode_object(row)
                body = packet["body"]
                if body.get("review_kind") != "withdrawal":
                    continue
                _object_contract("packet", body)
                indexed = self._body_refs(project, "packet", body)
                stored = [
                    {"purpose": item["purpose"], "kind": item["ref_kind"], "id": item["ref_id"],
                     "revision": str(item["ref_revision"]), "digest": item["ref_digest"]}
                    for item in packet.get("refs", [])
                ]
                expected = [
                    {"purpose": item["purpose"], "kind": item["kind"], "id": item["id"],
                     "revision": str(item["revision"]), "digest": item["digest"]}
                    for item in indexed
                ]
                need(stored == expected, "integrity_error", "Withdrawal packet reference index differs", row.get("id"))
                withdrawal = body.get("withdrawal")
                need(isinstance(withdrawal, dict) and
                     withdrawal.get("format") == "assurance.withdrawal.v1" and
                     withdrawal.get("project") == project and
                     isinstance(withdrawal.get("subject_ref"), dict) and
                     isinstance(withdrawal.get("reason"), str) and bool(withdrawal["reason"]) and
                     isinstance(withdrawal.get("authority_refs"), list),
                     "integrity_error", "Withdrawal packet body is malformed", row.get("id"))
                subject_ref = validate_typed_ref(
                    self._identity(withdrawal["subject_ref"]), project=project,
                    expected_kinds={"assurance_object"})
                root_ref = validate_typed_ref(
                    self._identity(body.get("root_ref")), project=project,
                    expected_kinds={"assurance_object"})
                need(self._identity(root_ref) == self._identity(subject_ref),
                     "integrity_error", "Withdrawal packet root and subject differ", row.get("id"))
                need(subject_ref.get("object_kind") in {"scope", "obligations", "profile", "edge", "set"},
                     "integrity_error", "Withdrawal subject kind is not reviewable", row.get("id"))
                subject_row = self.s.one(
                    "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind=?",
                    (subject_ref["object"], project, subject_ref["object_kind"]), True)
                need(subject_row["digest"] == subject_ref["object_digest"],
                     "integrity_error", "Withdrawal subject digest differs", row.get("id"))
                for _path, raw in _walk_refs(withdrawal):
                    normalized = validate_typed_ref(self._identity(raw), project=project)
                    self._resolve_locator(actor, normalized, current=False)
                withdrawals.append(withdrawal)

            return {
                "format": "supervisor.assurance-structure.v1",
                "project": project,
                "objects": _canonical_unique(object_projections),
                "heads": _canonical_unique(head_projections),
                "withdrawal_proposals": _canonical_unique(withdrawals),
            }

    def _head_event(self, project: str, logical_id: str) -> dict[str, Any] | None:
        head = self.s.one("SELECT head_event FROM assurance_heads WHERE project=? AND logical_id=?", (project, logical_id))
        return self.s.one("SELECT * FROM assurance_events WHERE id=?", (head["head_event"],), True) if head else None

    def _object_is_current(self, row: dict[str, Any]) -> bool:
        event = self._head_event(row["project"], row["logical_id"])
        return bool(event and event["subject_id"] == row["id"] and event["subject_digest"] == row["digest"] and
                    event["event_kind"] == "adopt")

    def _set_criteria_requirements(self, relation: str, supplied: Any,
                                   contract_digest: str | None = None) -> list[str]:
        """Normalize a set's required criteria without accepting a bypass.

        ``criteria`` is an input declaration, not a caller supplied PASS
        map.  Every registry obligation and the common set obligations remain
        required; callers may only add a known criterion.  The value is
        deliberately restricted to a positive requirement marker so a
        ``False`` value cannot disable an obligation while looking like valid
        configuration.
        """
        need(isinstance(supplied, dict), "invalid_set", "Relation-set criteria must be an object")
        known = SET_UNIVERSAL_CRITERIA | set(registry_entry(relation, contract_digest=contract_digest)["set_checks"])
        for name, value in supplied.items():
            need(isinstance(name, str) and name in known, "invalid_criterion",
                 "Relation-set criterion is not declared by the registry", name)
            need(value is True or (
                type(value) is dict and set(value) == {"required"} and value.get("required") is True
            ),
                 "invalid_criterion", "Relation-set criteria declare requirements with true markers", name)
        return sorted(known | set(supplied))

    def _set_criteria_achieved(self, relation: str, edges: list[dict[str, Any]],
                               obligations: dict[str, Any], missing: list[str],
                               *, all_edges_current: bool,
                               meaning_review: bool = False,
                               independent_synthesis: bool = False,
                               contract_digest: str | None = None) -> dict[str, bool]:
        """Compute achieved observations separately from required criteria."""
        identities = [(edge["id"], edge["revision"], edge["digest"]) for edge in edges]
        empty_only = bool(obligations["body"].get("obligations")) and all(
            item.get("kind") == "empty_scope" for item in obligations["body"].get("obligations", [])
            if isinstance(item, dict)
        )
        all_obligations_covered = not missing or (empty_only and not edges)
        complete_empty_responsibilities = (
            relation == "implements" and obligations["body"].get("format") == OBLIGATIONS_V2
            and obligations["body"].get("enumeration_status") == "complete"
            and not edges and not missing
        )
        achieved: dict[str, bool] = {
            "all_edges_current": bool(all_edges_current),
            "all_obligations_covered": bool(all_obligations_covered),
            "no_duplicate_identity": len(identities) == len(set(identities)),
            "meaning_review": bool(meaning_review),
            "independent_synthesis": bool(independent_synthesis),
            "empty_scope": not bool(edges),
        }
        # The registry-specific checks are preserved in the immutable body.
        # Only the mechanical facts available at this layer are marked
        # achieved.  A relation-specific semantic check that has no finite
        # implementation here remains false and therefore blocks adoption;
        # it is never silently discarded.
        for name in registry_entry(relation, contract_digest=contract_digest)["set_checks"]:
            if name in achieved:
                continue
            if name == "no_self_edge":
                achieved[name] = all(
                    self._identity(edge["body"].get("source_ref")) !=
                    self._identity(edge["body"].get("target_ref"))
                    for edge in edges
                )
            elif name.startswith("all_"):
                achieved[name] = (bool(edges) or complete_empty_responsibilities) and bool(all_edges_current) and bool(all_obligations_covered)
            else:
                achieved[name] = False
        return achieved

    def _set_requirements_for_body(self, body: dict[str, Any]) -> list[str]:
        requirements = body.get("criteria_requirements")
        if requirements is None:
            # Old E2 set bodies did not retain the accepted requirements.  They
            # remain valid historical archive rows, but cannot be promoted to
            # a current proof by reconstructing requirements from an achieved
            # map whose semantics were not fixed at proposal time.
            raise Fault("legacy_unverified", "Relation-set criteria requirements are not retained")
        need(isinstance(requirements, list) and requirements == sorted(set(requirements)),
             "integrity_error", "Relation-set criteria requirements are malformed")
        need(set(requirements) <= SET_UNIVERSAL_CRITERIA | set(registry_entry(
            body["relation"], contract_digest=body.get("relation_contract_digest"))["set_checks"]),
             "invalid_criterion", "Relation-set criteria contains an undeclared requirement")
        if body.get("criteria_requirements_digest") is not None:
            need(body["criteria_requirements_digest"] == digest(requirements),
                 "integrity_error", "Relation-set criteria requirements digest differs")
        return list(requirements)

    def _ensure_external_current(self, actor, project: str, raw: dict[str, Any], *, visiting: set[str]) -> dict[str, Any]:
        normalized = validate_typed_ref(self._identity(raw), project=project)
        if normalized["kind"] == "assurance_object":
            row = self._object_by_ref(normalized, project,
                                      kinds={"scope", "obligations", "profile", "edge", "set"})
            self._ensure_object_current(actor, project, row, require_self=True, visiting=visiting)
            return {"mode": "assurance_object", "object": row, "current": True}
        resolved = self._resolve_locator(actor, normalized, current=True)
        need(resolved.get("current", True), "stale_reference", "Typed dependency is not current", normalized["kind"])
        return resolved

    def _ensure_object_current(self, actor, project: str, row: dict[str, Any], *,
                               require_self: bool = True,
                               allow_unadopted_dependencies: bool = False,
                               visiting: set[str] | None = None) -> None:
        """Re-evaluate an assurance object's semantic dependency closure.

        The immutable head is only one input to currentness.  This routine is
        shared by adoption gates, current reads and reports so an endpoint or
        set denominator update cannot be hidden by a still matching local
        adoption event.  ``allow_unadopted_dependencies`` is limited to the
        documented scope→obligations→profile bootstrap transaction.
        """
        need(row["project"] == project, "cross_project", "Assurance object belongs to another project")
        visiting = set() if visiting is None else visiting
        if row["id"] in visiting:
            raise Fault("proof_cycle", "Assurance currentness dependency graph contains a cycle", row["id"])
        if require_self:
            need(self._object_is_current(row), "stale_reference",
                 "Assurance object is not the adopted current head", row["id"])
        body = row["body"]
        # The E1 identity fixture shapes remain historical storage objects;
        # E2 semantic currentness is only claimed for the explicit v1 forms.
        if not (isinstance(body.get("format"), str) and body["format"].startswith("assurance.")):
            return
        visiting.add(row["id"])

        def dependency(raw: dict[str, Any], kinds: set[str], *, optional_adoption: bool = False) -> dict[str, Any]:
            dep = self._object_by_ref(raw, project, kinds=kinds)
            self._ensure_object_current(
                actor, project, dep,
                require_self=not optional_adoption,
                allow_unadopted_dependencies=optional_adoption,
                visiting=visiting,
            )
            return dep

        kind = row["kind"]
        if kind == "scope":
            for raw in body.get("roots", []):
                self._ensure_external_current(actor, project, raw, visiting=visiting)
            for item in body.get("exclusion_proposals", []):
                if isinstance(item, dict) and isinstance(item.get("target_ref"), dict):
                    self._ensure_external_current(actor, project, item["target_ref"], visiting=visiting)
                if isinstance(item, dict):
                    for raw in item.get("authority_refs", []):
                        self._ensure_external_current(actor, project, raw, visiting=visiting)
            for raw in body.get("authority_refs", []):
                self._ensure_external_current(actor, project, raw, visiting=visiting)
        elif kind == "obligations":
            scope = dependency(body["scope_ref"], {"scope"},
                               optional_adoption=allow_unadopted_dependencies)
            if isinstance(body.get("consumer_binding"), dict):
                derived, _records = self._consumer_c_obligation_material(
                    actor, project, body["consumer_binding"], current=True,
                )
            else:
                derived = self._derive_obligations_body(project, scope)
            need(digest(derived) == row["digest"], "stale_reference",
                 "Obligation derivation no longer matches the selected scope", row["id"])
        elif kind == "profile":
            if _is_canonical_profile(body.get("format")):
                # The stable program head and previous selection are semantic
                # selection inputs.  They are checked here on every current
                # read/adoption; the stage evaluator itself is intentionally
                # not part of E3 unit 1.
                self._validate_profile_records(actor, project, body, for_adoption=True)
            optional = allow_unadopted_dependencies
            scope = dependency(body["scope_ref"], {"scope"}, optional_adoption=optional)
            obligations = dependency(body["obligations_ref"], {"obligations"}, optional_adoption=optional)
            derived = self._derive_obligations_body(project, scope)
            need(digest(derived) == obligations["digest"], "stale_reference",
                 "Profile obligations no longer match its scope", row["id"])
            for binding in body.get("test_definition_bindings", []):
                if isinstance(binding, dict):
                    self._ensure_external_current(actor, project, binding["artifact_ref"], visiting=visiting)
                    self._ensure_external_current(actor, project, binding["check_ref"], visiting=visiting)
        elif kind == "edge":
            container = dependency(body["scope_ref"], {"scope", "profile"})
            self._validate_edge_semantics(actor, project, body["relation"],
                                          body["source_ref"], body["target_ref"],
                                          body.get("relation_contract_digest",
                                                   REGISTRY_V1_DIGEST))
            if (body["relation"] == "produced_by" and
                    semantic_kind(body["source_ref"]) == "artifact"):
                # A P output is allowed to remain a draft only through its
                # complete controller-owned production material.  All other
                # artifact endpoints retain the accepted-only resolver.
                self._resolve_artifact_production_material(
                    actor, project, body["source_ref"], body["target_ref"],
                )
            else:
                self._ensure_external_current(actor, project, body["source_ref"], visiting=visiting)
            self._ensure_external_current(actor, project, body["target_ref"], visiting=visiting)
            for raw in body.get("required_evidence_refs", []):
                self._ensure_external_current(actor, project, raw, visiting=visiting)
            for raw in body.get("authority_refs", []):
                self._ensure_external_current(actor, project, raw, visiting=visiting)
            scope = self._scope_from_ref(project, self._object_ref(container))
            obligation_row = self._obligations_for_scope(project, scope, container)
            need(obligation_row is not None, "stale_reference", "Edge denominator is missing", row["id"])
            obligation_body = self._decode_object(obligation_row)["body"]
            known = {item.get("id") for item in obligation_body.get("obligations", []) if isinstance(item, dict)}
            need(set(body.get("obligation_ids", [])) <= known, "stale_reference",
                 "Edge obligation is outside the current denominator", row["id"])
        elif kind == "set":
            container = dependency(body["scope_ref"], {"scope", "profile"})
            expected_row = self._object_by_ref(body["expected_obligations_ref"], project,
                                               kinds={"obligations"})
            obligations = dependency(
                body["expected_obligations_ref"], {"obligations"},
                optional_adoption=isinstance(expected_row["body"].get("consumer_binding"), dict),
            )
            self._ensure_external_current(actor, project, body["center_ref"], visiting=visiting)
            descriptor = body.get("selected_edge_manifest_ref") or {}
            descriptors = self._manifest_edge_descriptors(project, descriptor, {
                "center_ref": body["center_ref"], "relation": body["relation"],
                "direction": body["direction"], "scope_ref": body["scope_ref"],
            })
            edges = []
            for item in descriptors:
                edge_row = self.s.one(
                    "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='edge'",
                    (item.get("id"), project), True,
                )
                need(edge_row["digest"] == item.get("digest"), "stale_set",
                     "Set edge manifest contains a missing or changed edge", item.get("id"))
                edge = self._decode_object(edge_row)
                self._ensure_object_current(actor, project, edge, require_self=True, visiting=visiting)
                edges.append(edge)
            relation_contract_digest = body.get("relation_contract_digest")
            current_edges = self._latest_edges_for_set(
                actor, project, body["center_ref"], body["relation"], body["direction"], body["scope_ref"],
                relation_contract_digest,
            )
            identities = [(edge["id"], edge["revision"], edge["digest"]) for edge in edges]
            current_identities = [(edge["id"], edge["revision"], edge["digest"]) for edge in current_edges]
            need(identities == current_identities, "stale_set",
                 "Set denominator no longer matches the current edge inventory", row["id"])
            stream_digest = digest(identities)
            assignment = self._assignment_descriptors(
                project, body.get("coverage_assignment_ref") or {},
                body["expected_obligations_ref"], stream_digest,
            )
            expected_assignment = self._edge_assignment(
                obligations, edges, actor,
                center=body["center_ref"], relation=body["relation"],
                direction=body["direction"],
            )
            need(assignment == expected_assignment, "stale_set",
                 "Set coverage assignment no longer matches its edge denominator", row["id"])
            self._validate_partition_manifest(project, body.get("partition_manifest_ref") or {}, len(edges), stream_digest)
            missing = sorted(item for item, value in expected_assignment.items() if not value)
            criteria = self._set_criteria_achieved(
                body["relation"], edges, obligations, missing, all_edges_current=True,
                meaning_review=require_self, independent_synthesis=require_self,
                contract_digest=relation_contract_digest,
            )
            requirements = self._set_requirements_for_body(body)
            pending_review = {"meaning_review", "independent_synthesis"} if not require_self else set()
            need(all(criteria.get(name) is True for name in requirements if name not in pending_review),
                 "set_incomplete", "Adopted relation-set criteria are no longer achieved",
                 [name for name in requirements if name not in pending_review and not criteria.get(name)])
            for raw in body.get("required_evidence_refs", []):
                self._ensure_external_current(actor, project, raw, visiting=visiting)
        visiting.remove(row["id"])

    def _expected_head(self, project: str, logical_id: str, expected_head: str | None) -> None:
        current = self.s.one("SELECT head_event FROM assurance_heads WHERE project=? AND logical_id=?", (project, logical_id))
        actual = current["head_event"] if current else None
        need(expected_head == actual, "stale_head", "Assurance proposal head compare-and-swap failed",
             {"expected": expected_head, "actual": actual, "logical_id": logical_id})

    def _store_e2_object(self, actor, project: str, kind: str, logical_id: str,
                         body: dict[str, Any]) -> dict[str, Any]:
        encoded_digest = digest(body)
        existing = self.s.one("SELECT * FROM assurance_objects WHERE project=? AND kind=? AND digest=?",
                              (project, kind, encoded_digest))
        if existing:
            return self._decode_object(existing)
        latest = self.s.one("SELECT COALESCE(MAX(revision),0) AS revision FROM assurance_objects WHERE project=? AND kind=? AND logical_id=?",
                            (project, kind, logical_id))
        revision = int(latest["revision"]) + 1 if latest else 1
        return self.store_object(actor, project, kind, logical_id, revision, body)

    def _validate_authority_refs(self, actor, project: str, refs: Any) -> list[dict[str, Any]]:
        need(isinstance(refs, list), "invalid_reference", "Authority references must be a list")
        result=[]
        for value in refs:
            normalized=validate_typed_ref(value, project=project)
            # Authority is a reference to retained source/evidence, not a
            # free-form assertion.  Resolve the pinned identity now; current
            # adoption remains a later gate and is intentionally not inferred.
            self._resolve_proposal_endpoint(actor, project, normalized, current=False)
            result.append(self._identity(normalized))
        need(len({digest(item) for item in result}) == len(result), "duplicate_reference", "Duplicate authority reference")
        return result

    def _profile_scope_target_ids(self, project: str, scope_ref: dict[str, Any]) -> set[str]:
        """Return concrete artifact targets retained by a profile scope."""
        scope = self._object_by_ref(scope_ref, project, kinds={"scope"})
        targets: set[str] = set()
        for raw in scope["body"].get("roots", []):
            ref = validate_typed_ref(raw, project=project)
            if ref["kind"] == "artifact":
                targets.add(ref["artifact"])
            elif (ref["kind"] == "traceability_ref" and
                  ref["locator"].get("ref_type") == "artifact_ac"):
                targets.add(ref["locator"]["artifact"])
        return targets

    def _validate_profile_change_authority(self, actor, project: str, ref: dict[str, Any],
                                           target_ids: set[str]) -> None:
        resolved = self._resolve_proposal_endpoint(actor, project, ref, current=False)
        resolution = resolved.get("resolution") or {}
        payload = resolution.get("payload")
        need(resolution.get("mode") == "material" and isinstance(payload, dict) and
             set(payload) == {"change", "revision", "body"},
             "unsupported_authority", "Pinned change authority material is unsupported")
        row = self.s.one("SELECT * FROM changes WHERE id=? AND project=?",
                         (ref["change"], project), True)
        need(row is not None and row["revision"] == ref["revision"],
             "unsupported_authority", "Pinned change authority revision is unavailable")
        change_body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
        need(payload["change"] == ref["change"] and payload["revision"] == ref["revision"] and
             payload["body"] == change_body and digest(change_body) == ref["body_digest"],
             "integrity_error", "Pinned change authority body differs")
        # A change is usable as profile authority only after the existing
        # planning workflow has reached its reviewed/reconciling boundary.
        need(row["stage"] in {"reconciling", "ready_for_reimplementation"},
             "unsupported_authority", "Change authority is not in an adopted workflow state")
        affected = change_body.get("affected")
        evidence = change_body.get("evidence")
        baseline = change_body.get("baseline_refs")
        need(isinstance(affected, list) and affected and
             all(type(item) is str and item for item in affected) and
             len(affected) == len(set(affected)),
             "unsupported_authority", "Change authority targets are not retained")
        need(isinstance(evidence, list) and evidence and
             all(type(item) is str and item for item in evidence) and
             len(evidence) == len(set(evidence)),
             "unsupported_authority", "Change authority evidence is not retained")
        need(isinstance(baseline, list), "unsupported_authority",
             "Change authority baseline is not retained")
        baseline_by_id = {}
        for item in baseline:
            need(isinstance(item, dict) and set(item) == {"id", "revision", "digest"} and
                 type(item["id"]) is str and type(item["revision"]) is int and
                 _sha(item["digest"]), "unsupported_authority",
                 "Change authority baseline is malformed")
            need(item["id"] not in baseline_by_id, "unsupported_authority",
                 "Change authority baseline contains a duplicate target", item["id"])
            baseline_by_id[item["id"]] = item
        need(set(affected) == set(baseline_by_id), "unsupported_authority",
             "Change authority target baseline is incomplete")

        # ``baseline_refs`` is captured when the change is registered.  A
        # product change deliberately makes the mutable artifact row advance
        # later, so the baseline must be checked against the immutable history
        # row rather than against that current projection.  Keeping this
        # check separate from the result check below prevents a valid applied
        # change from invalidating its own authority while still rejecting a
        # forged baseline that was never retained.
        baseline_bodies = {}
        for artifact_id in affected:
            artifact = self.s.one("SELECT * FROM artifacts WHERE id=? AND project=?",
                                  (artifact_id, project))
            pinned = baseline_by_id[artifact_id]
            need(artifact is not None, "unsupported_authority",
                 "Change authority target is missing", artifact_id)
            baseline_row = self.s.one(
                "SELECT r.* FROM revisions r JOIN artifacts a ON a.id=r.artifact "
                "WHERE r.artifact=? AND r.revision=? AND a.project=?",
                (artifact_id, pinned["revision"], project),
            )
            need(baseline_row is not None and baseline_row["digest"] == pinned["digest"],
                 "unsupported_authority", "Change authority baseline history is missing or changed", artifact_id)
            baseline_body = parse_json(baseline_row["body"], limit=MAX_OBJECT_BYTES)
            need(isinstance(baseline_body, dict) and digest(baseline_body) == baseline_row["digest"],
                 "integrity_error", "Change authority baseline history digest differs", artifact_id)
            baseline_bodies[artifact_id] = baseline_body

        deltas = change_body.get("deltas", [])
        need(isinstance(deltas, list), "unsupported_authority",
             "Change authority deltas are not retained")
        delta_by_id = {}
        for delta in deltas:
            need(isinstance(delta, dict) and set(delta) <= {"artifact", "expected_revision", "body", "withdraw"} and
                 {"artifact", "expected_revision", "body"} <= set(delta),
                 "unsupported_authority", "Change authority delta is malformed")
            artifact_id = delta["artifact"]
            need(type(artifact_id) is str and artifact_id in baseline_by_id and artifact_id not in delta_by_id,
                 "unsupported_authority", "Change authority delta target is incomplete", artifact_id)
            need(type(delta["expected_revision"]) is int and
                 delta["expected_revision"] == baseline_by_id[artifact_id]["revision"],
                 "unsupported_authority", "Change authority delta baseline differs", artifact_id)
            need(isinstance(delta["body"], dict), "unsupported_authority",
                 "Change authority delta body is malformed", artifact_id)
            need(type(delta.get("withdraw", False)) is bool, "unsupported_authority",
                 "Change authority delta withdrawal marker is malformed", artifact_id)
            artifact = self.s.one("SELECT kind FROM artifacts WHERE id=? AND project=?",
                                  (artifact_id, project))
            need(artifact is not None, "unsupported_authority",
                 "Change authority delta target is missing", artifact_id)
            self.c.k.validate_body(artifact["kind"], delta["body"])
            delta_by_id[artifact_id] = delta

        # A ready-for-reimplementation change is the post-approval result of
        # _apply_change.  It must retain a delta so the expected applied
        # identity can be checked.  A reconciling change is still a valid
        # pre-application authority and therefore must remain on its captured
        # baseline until it is applied.
        if row["stage"] == "ready_for_reimplementation":
            need(delta_by_id, "unsupported_authority",
                 "Applied change authority has no retained result")
        impact = change_body.get("impact")
        need(isinstance(impact, dict) and impact.get("reachable_sets_complete") is True and
             isinstance(impact.get("artifacts"), list) and
             all(type(item) is str and item for item in impact["artifacts"]) and
             set(affected) <= set(impact["artifacts"]),
             "unsupported_authority", "Change authority impact is not retained")
        source_evidence = set()
        for evidence_id in evidence:
            source = self.s.one("SELECT id FROM sources WHERE id=? AND project=?", (evidence_id, project))
            if source:
                source_evidence.add(evidence_id)
                continue
            receipt = self.s.one("SELECT id FROM receipts WHERE id=? AND project=?", (evidence_id, project))
            if receipt:
                continue
            finding = self.s.one(
                "SELECT id FROM artifacts WHERE id=? AND project=? AND kind IN ('finding','risk','unknown')",
                (evidence_id, project),
            )
            need(finding is not None, "unsupported_authority",
                 "Change authority evidence endpoint is unsupported", evidence_id)
        declared_source = change_body.get("source")
        if declared_source is not None:
            need(type(declared_source) is str and declared_source and
                 self.s.one("SELECT id FROM sources WHERE id=? AND project=?", (declared_source, project)),
                 "unsupported_authority", "Change authority source relation is missing")
            source_evidence.add(declared_source)
        need(source_evidence, "unsupported_authority",
             "Change authority has no retained source relation")
        need(set(affected) & target_ids, "unsupported_authority",
             "Change authority does not target the selected scope")

        for artifact_id in affected:
            artifact = self.s.one("SELECT * FROM artifacts WHERE id=? AND project=?",
                                  (artifact_id, project))
            current_revision = self.s.one(
                "SELECT * FROM revisions WHERE artifact=? AND revision=?",
                (artifact_id, artifact["revision"]),
            )
            need(current_revision is not None and current_revision["digest"] == artifact["digest"],
                 "unsupported_authority", "Change authority current target history is missing", artifact_id)
            current_body = parse_json(current_revision["body"], limit=MAX_OBJECT_BYTES)
            need(isinstance(current_body, dict) and digest(current_body) == current_revision["digest"],
                 "integrity_error", "Change authority current target digest differs", artifact_id)
            delta = delta_by_id.get(artifact_id)
            if row["stage"] == "ready_for_reimplementation" and delta is not None:
                # _apply_change advances exactly one revision from the
                # captured expected revision.  An unrelated later revision,
                # or a current body that differs from the applied delta,
                # invalidates this authority while retaining its history.
                expected_status = "withdrawn" if delta.get("withdraw", False) else "accepted"
                need(artifact["revision"] == delta["expected_revision"] + 1 and
                     artifact["digest"] == digest(delta["body"]) and
                     artifact["status"] == expected_status and
                     current_revision["status"] == expected_status and
                     current_body == delta["body"],
                     "unsupported_authority", "Change authority applied target is no longer current", artifact_id)
            else:
                # Before application, every affected target must still be the
                # accepted baseline.  This preserves the existing
                # reconciling authority path while checking currentness
                # independently from historical baseline retention.
                pinned = baseline_by_id[artifact_id]
                need(artifact["revision"] == pinned["revision"] and
                     artifact["digest"] == pinned["digest"] and
                     artifact["status"] == "accepted" and
                     current_body == baseline_bodies[artifact_id],
                     "unsupported_authority", "Change authority target is no longer current", artifact_id)

    def _validate_profile_decision_authority(self, actor, project: str, ref: dict[str, Any],
                                             target_ids: set[str]) -> None:
        resolved = self._resolve_proposal_endpoint(actor, project, ref, current=False)
        need(resolved.get("kind") == "artifact" and resolved.get("artifact_kind") == "decision",
             "unsupported_authority", "Authority artifact is not an accepted decision")
        decision = resolved.get("body") or {}
        source_refs = decision.get("source_refs")
        if source_refs is None and isinstance(decision.get("source"), str):
            source_refs = [decision["source"]]
        target_refs = decision.get("target_refs", decision.get("refs"))
        need(isinstance(source_refs, list) and source_refs and
             all(type(item) is str and item for item in source_refs) and
             len(source_refs) == len(set(source_refs)),
             "unsupported_authority", "Decision authority source linkage is unsupported")
        for source_id in source_refs:
            source = self.s.one("SELECT * FROM sources WHERE id=? AND project=?",
                                (source_id, project))
            need(source is not None and digest(self.s.blob_get(source["blob"])) == source["blob"],
                 "unsupported_authority", "Decision authority source linkage is missing", source_id)
        need(isinstance(target_refs, list) and target_refs and
             all(type(item) is str and item for item in target_refs) and
             len(target_refs) == len(set(target_refs)),
             "unsupported_authority", "Decision authority target linkage is unsupported")
        for target_id in target_refs:
            target = self.s.one("SELECT id,project,status FROM artifacts WHERE id=? AND project=?",
                                (target_id, project))
            need(target is not None and target["status"] == "accepted",
                 "unsupported_authority", "Decision authority target is missing or not accepted", target_id)
        need(set(target_refs) & target_ids, "unsupported_authority",
             "Decision authority does not target the selected scope")

    def _validate_profile_authority_refs(self, actor, project: str, refs: Any,
                                         body: dict[str, Any],
                                         predecessor: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Resolve only the existing source/change/accepted-decision families."""
        need(isinstance(refs, list), "invalid_profile", "Profile authority_refs are invalid")
        target_ids = self._profile_scope_target_ids(project, body["scope_ref"])
        # A replacement's authority covers the transition from its immutable
        # predecessor to the immutable candidate.  On an idempotent replay the
        # canonical current profile is the candidate itself, so using only
        # ``current`` would drop the predecessor's targets and reject the same
        # already-authorized receipt.  The predecessor has already passed the
        # exact selection-head check in _validate_profile_v2_records; resolve
        # its retained scope here without treating it as a new current proof.
        if predecessor is not None:
            target_ids |= self._profile_scope_target_ids(project, predecessor["body"]["scope_ref"])
        result = []
        for raw in refs:
            normalized = validate_typed_ref(self._identity(raw), project=project)
            if normalized["kind"] == "source":
                self._resolve_locator(actor, normalized, current=False)
            elif normalized["kind"] == "change":
                self._validate_profile_change_authority(actor, project, normalized, target_ids)
            elif normalized["kind"] == "artifact":
                self._validate_profile_decision_authority(actor, project, normalized, target_ids)
            else:
                raise Fault("unsupported_authority", "Profile authority reference family is unsupported")
            result.append(self._identity(normalized))
        need(len({digest(item) for item in result}) == len(result), "duplicate_reference",
             "Duplicate profile authority reference")
        return result

    def _validate_selector(self, selector: Any) -> Any:
        # Scope selection is declarative data.  It intentionally has no SQL,
        # Python expression, wildcard evaluator or caller-controlled query.
        need(isinstance(selector, (dict, list)), "invalid_scope", "Selection rules must be finite declarative data")
        encoded=canonical(selector)
        need(len(encoded) <= MAX_OBJECT_BYTES, "invalid_scope", "Selection rules exceed the bounded size")
        if isinstance(selector, dict):
            allowed={"artifact_kinds", "relations", "stages", "include_roots", "exclude_roots", "include_status"}
            need(set(selector) <= allowed, "invalid_scope", "Selection rule contains an unsupported selector")
            for key,value in selector.items():
                need(isinstance(value,list) and len(value)<=1000 and all(isinstance(item,str) and item for item in value),
                     "invalid_scope", "Selection rule values must be finite string lists", key)
                need(len(value)==len(set(value)), "invalid_scope", "Selection rule contains duplicates", key)
        else:
            need(len(selector)<=1000 and all(isinstance(item,dict) for item in selector),
                 "invalid_scope", "Selection rule list is malformed")
        return self._identity(selector)

    def _scope_from_ref(self, project: str, ref: dict[str, Any]) -> dict[str, Any]:
        row=self._object_by_ref(ref, project, kinds={"scope", "profile"})
        if row["kind"] == "profile":
            body=row["body"]
            scope_ref=body.get("scope_ref")
            need(isinstance(scope_ref,dict), "unresolved_reference", "Profile does not retain its scope reference")
            return self._object_by_ref(scope_ref, project, kinds={"scope"})
        return row

    def _scope_ref_from_profile(self, project: str, ref: dict[str, Any]) -> dict[str, Any]:
        row=self._object_by_ref(ref, project, kinds={"scope", "profile"})
        if row["kind"] == "scope":
            return self._object_ref(row)
        return self._identity(row["body"]["scope_ref"])

    def _artifact_body(self, project: str, ref: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        ref=self._identity(ref)
        normalized=validate_typed_ref(ref, project=project, expected_kinds={"artifact"})
        row=self.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (normalized["artifact"], project))
        need(row is not None, "unresolved_reference", "Artifact endpoint is missing", normalized["artifact"])
        revision=self.s.one("SELECT * FROM revisions WHERE artifact=? AND revision=?", (normalized["artifact"], normalized["revision"]))
        need(revision is not None, "unresolved_reference", "Artifact endpoint revision is missing", normalized["revision"])
        body=parse_json(revision["body"], limit=MAX_OBJECT_BYTES)
        need(revision["digest"] == normalized["body_digest"] == digest(body), "integrity_error", "Artifact endpoint digest differs")
        need(row["status"] == "accepted", "unresolved_reference", "Artifact endpoint is not accepted")
        return row,body

    def execution_test_artifact_refs(self, actor, project: str,
                                      definition_ref: dict[str, Any]) -> list[dict[str, Any]]:
        """Derive execution artifact refs from adopted assurance profiles.

        Runtime and Delivery are controllers of execution material.  They do
        not accept a caller-supplied artifact list.  The only source for this
        list is a currently adopted profile whose exact check binding matches
        the immutable execution definition.  A profile with a stale external
        dependency is not silently treated as adopted.
        """
        self._project(actor, project, read=True)
        check = validate_typed_ref(self._identity(definition_ref), project=project,
                                   expected_kinds={"test_plan_check", "delivery_check"})
        def binding_key(value: dict[str, Any]) -> dict[str, Any]:
            """Compare the immutable definition while ignoring capture pins.

            A plan/snapshot pin records one controller capture.  A later
            controller execution may create another valid capture of the same
            mutable-row identity.  The binding still has to carry the exact
            task/delivery, revision/digest, check id and check digest; a pin
            is resolved independently by the adopted profile currentness
            walk.  This prevents a fresh capture ID from making a valid
            profile binding unusable without accepting an ID-only match.
            """
            result = self._identity(value)
            nested_key = "plan" if result.get("kind") == "test_plan_check" else "delivery"
            nested = result.get(nested_key)
            if isinstance(nested, dict):
                nested = dict(nested)
                nested.pop("pin", None)
                result[nested_key] = nested
            return result
        result: dict[str, dict[str, Any]] = {}
        rows = self.s.all(
            "SELECT * FROM assurance_objects WHERE project=? AND kind='profile' "
            "ORDER BY logical_id,revision,id", (project,))
        for row in rows:
            profile_ref = self._object_ref(row)
            try:
                resolved = self._resolve_locator(actor, profile_ref, current=True)
            except Fault as exc:
                # Proposed, withdrawn, or dependency-stale profiles do not
                # contribute controller execution bindings.  Integrity faults
                # are not a reason to continue with a weaker artifact set.
                if exc.code in {"stale_reference", "unresolved_reference", "missing_evidence",
                                "legacy_unverified"}:
                    continue
                raise
            profile = resolved.get("object", {}).get("body")
            need(isinstance(profile, dict), "integrity_error", "Adopted profile body is malformed")
            bindings = profile.get("test_definition_bindings", [])
            need(isinstance(bindings, list), "integrity_error", "Adopted profile test bindings are malformed")
            for binding in bindings:
                need(isinstance(binding, dict) and set(binding) == {"artifact_ref", "check_ref"},
                     "integrity_error", "Adopted profile test binding shape differs")
                bound_check = validate_typed_ref(self._identity(binding["check_ref"]), project=project,
                                                 expected_kinds={"test_plan_check", "delivery_check"})
                if binding_key(bound_check) != binding_key(check):
                    continue
                artifact_ref = validate_typed_ref(self._identity(binding["artifact_ref"]), project=project,
                                                  expected_kinds={"artifact"})
                artifact_row, artifact_body = self._artifact_body(project, artifact_ref)
                need(artifact_row.get("kind") == "test", "invalid_relation_endpoint",
                     "Profile execution artifact is not a test artifact")
                result[digest(self._identity(artifact_ref))] = self._identity(artifact_ref)
        return [result[key] for key in sorted(result)]

    def _resolve_proposal_endpoint(self, actor, project: str, ref: dict[str, Any], *, current: bool = False) -> dict[str, Any]:
        normalized=validate_typed_ref(self._identity(ref), project=project)
        if normalized["kind"] == "artifact":
            row,body=self._artifact_body(project, normalized)
            if current:
                need(row["revision"] == normalized["revision"] and row["digest"] == normalized["body_digest"],
                     "stale_reference", "Artifact endpoint is not current")
            return {"ref": self._identity(normalized), "kind":"artifact", "artifact_kind":row.get("kind"), "body":body}
        resolution=self._resolve_locator(actor, normalized, current=current)
        return {"ref":self._identity(normalized), "kind":semantic_kind(normalized), "resolution":resolution}

    def _resolve_produced_artifact_endpoint(self, project: str,
                                            ref: dict[str, Any], *,
                                            current: bool = False) -> dict[str, Any]:
        """Resolve a P output endpoint while it remains a Knowledge draft."""
        from .artifact_provenance import resolve_produced_artifact

        normalized = validate_typed_ref(self._identity(ref), project=project,
                                        expected_kinds={"artifact"})
        resolved = resolve_produced_artifact(
            self.s, normalized, project=project, code="unresolved_reference",
            current=current,
        )
        return {"ref": self._identity(normalized), "kind": "artifact",
                "artifact_kind": resolved["kind"], "body": resolved["body"],
                "status": resolved["status"]}

    def _resolve_artifact_production_material(self, actor, project: str,
                                               artifact_ref: dict[str, Any],
                                               task_ref: dict[str, Any]) -> dict[str, Any]:
        """Validate the complete P material closure for a produced-by edge."""
        from .artifact_provenance import (
            resolve_artifact_production_material,
            resolve_produced_artifact,
        )
        return resolve_artifact_production_material(
            self.s, project=project, artifact_ref=artifact_ref,
            task_ref=task_ref, context=self._candidate_context,
            resolve_artifact=lambda ref: resolve_produced_artifact(
                self.s, ref, project=project, code="integrity_error", current=True,
            ),
            blob_get=self.s.blob_get, code="integrity_error",
            missing_code="missing_evidence", current=True,
        )

    def _validate_scope_pair(self, scope, obligations, *, profile_format=None):
        v2 = scope["body"].get("format") == SCOPE_V2
        need((obligations["body"].get("format") == OBLIGATIONS_V2) == v2,
             "invalid_scope", "Scope and obligations versions differ")
        need(self._identity(obligations["body"].get("scope_ref")) == self._object_ref(scope),
             "invalid_scope", "Obligations bind another scope")
        if profile_format is not None:
            need((profile_format == PROFILE_V5_FORMAT) == v2, "invalid_profile", "Profile and scope contracts differ")

    def _obligations_for_scope(self, project, scope, container=None):
        rows = self.s.all("SELECT * FROM assurance_objects WHERE project=? AND kind='obligations' AND logical_id=? ORDER BY revision DESC", (project, "obligations:" + scope["id"]))
        if scope["body"].get("format") == SCOPE_V2:
            need(len(rows) == 1, "invalid_scope", "Scope v2 requires one exact obligation pair")
            decoded = self._decode_object(rows[0])
            self._validate_scope_pair(scope, decoded)
            if container is not None and container["kind"] == "profile":
                need(self._object_ref(decoded) == self._identity(container["body"]["obligations_ref"]),
                     "invalid_scope", "Profile does not select this exact obligation pair")
        return rows[0] if rows else None

    def _derive_obligations_body(self, project: str, scope: dict[str, Any]) -> dict[str, Any]:
        obligations=[]; input_refs=[]
        for raw in scope["body"].get("roots",[]):
            ref=self._identity(validate_typed_ref(raw, project=project)); input_refs.append(ref)
            if ref["kind"] == "artifact":
                _row,body=self._artifact_body(project,ref)
                acceptance=body.get("acceptance",[])
                need(isinstance(acceptance,list), "invalid_scope", "Artifact acceptance conditions are malformed")
                for index,value in enumerate(acceptance):
                    marker="obligation:"+digest([ref, index, value])
                    obligations.append({"id":marker,"kind":"artifact_acceptance","source_ref":ref,
                                       "pointer":f"/acceptance/{index}","value":value,"value_digest":digest(value)})
            elif ref["kind"] == "population":
                rows=self.s.all("SELECT * FROM traceability_items WHERE revision=? AND project=? ORDER BY id", (ref["revision"], project))
                for item in rows:
                    if item.get("leaf"):
                        item_body=parse_json(item["body"], limit=MAX_OBJECT_BYTES)
                        item_ref={"kind":"population_item","project":project,"population":ref,
                                  "item":item["id"],"item_digest":item["digest"]}
                        obligations.append({"id":"obligation:"+digest(item_ref),"kind":"population_leaf",
                                            "source_ref":item_ref,"value_digest":item["digest"],"value":item_body})
        if scope["body"].get("format") == SCOPE_V2:
            def resolve(ref):
                row, body = self._artifact_body(project, ref)
                need(row["revision"] == ref["revision"] and row["digest"] == ref["body_digest"],
                     "stale_reference", "Responsibility dependency is not current")
                return {**row, "body": body}
            for ref in list(input_refs):
                if ref["kind"] == "artifact":
                    artifact = resolve(ref)
                    records, dependencies = responsibility_records(ref, artifact["kind"], artifact["body"], resolver=resolve)
                    dependencies, sources = artifact_dependency_closure(ref, resolve,
                        lambda ident:self.s.one("SELECT * FROM sources WHERE id=? AND project=?", (ident, project)))
                    for source in sources:
                        self.s.blob_get(source["blob"])
                    obligations.extend(records)
                    input_refs.extend(dependencies)
            result = {"format": OBLIGATIONS_V2, "project": project, "scope_ref": self._object_ref(scope),
                      "derivation_version": "assurance-obligations-v2", "enumeration_status": "complete",
                      "input_refs": sorted({canonical(x):x for x in input_refs}.values(), key=canonical),
                      "obligations": sorted(obligations, key=lambda x:x["id"])}
            validate_v2_body("obligations", result)
            return result
        if not obligations:
            obligations=[{"id":"obligation:empty:"+scope["digest"],"kind":"empty_scope","source_ref":self._object_ref(scope),
                          "reason":"No machine-enumerable acceptance, check, or population leaf exists"}]
        obligations.sort(key=lambda item:item["id"])
        return {"format":"assurance.obligations.v1","project":project,"scope_ref":self._object_ref(scope),
                "derivation_version":"assurance-obligations-v1","input_refs":input_refs,"obligations":obligations}

    def _packet_marker(self, row: dict[str, Any]) -> str:
        return f"{row['kind']}:{row['id']}@{row['digest']}"

    def _make_packets(self, actor, project: str, root: dict[str, Any], review_kind: str,
                      leaves: list[str], required_roles: list[str], *, synthesis: bool = False,
                      required_coverage: list[str] | None = None,
                      synthesis_roles: list[str] | None = None) -> list[dict[str, Any]]:
        unique=sorted(set(leaves))
        if not unique:
            unique=[f"empty:{root['kind']}:{root['id']}@{root['digest']}"]
        stream_digest=digest(unique); packets=[]
        chunks=[unique[start:start+MAX_PAGE] for start in range(0,len(unique),MAX_PAGE)]
        for index,chunk in enumerate(chunks):
            body={"format":"assurance.review-packet.v1","project":project,
                  "root_ref":self._object_ref(root),"review_kind":review_kind,
                  "partition":{"kind":"leaf","index":index,"start":index*MAX_PAGE,
                               "end":index*MAX_PAGE+len(chunk),"count":len(chunk),"total":len(unique),
                               "stream_digest":stream_digest},"leaf_manifest":chunk,
                  "required_coverage":list(chunk),"required_roles":sorted(set(required_roles))}
            packet=self._store_e2_object(actor,project,"packet",f"packet:{root['id']}:{review_kind}:{index}",body)
            packets.append(packet)
        # A set's meaning is reviewed separately from its edge partitions.  A
        # synthesis packet may itself be partitioned, so large sets remain
        # bounded without materializing 100k identities in one body.
        if synthesis:
            children=[self._packet_marker(packet) for packet in packets]
            level=0
            while len(children)>MAX_PAGE:
                grouped=[children[start:start+MAX_PAGE] for start in range(0,len(children),MAX_PAGE)]
                next_level=[]
                for index,chunk in enumerate(grouped):
                    body={"format":"assurance.review-packet.v1","project":project,
                          "root_ref":self._object_ref(root),"review_kind":review_kind,
                          "partition":{"kind":"synthesis","level":level,"index":index,
                                       "count":len(chunk),"total":len(children),"stream_digest":digest(children)},
                          "leaf_manifest":chunk,"required_coverage":chunk,
                          "required_roles":sorted(set(synthesis_roles or ["impact"])),"children":True}
                    p=self._store_e2_object(actor,project,"packet",f"packet:{root['id']}:{review_kind}:s{level}-{index}",body)
                    packets.append(p);next_level.append(self._packet_marker(p))
                children=next_level;level+=1
            body={"format":"assurance.review-packet.v1","project":project,
                  "root_ref":self._object_ref(root),"review_kind":review_kind,
                  "partition":{"kind":"synthesis","level":level,"index":0,"count":len(children),
                               "total":len(children),"stream_digest":digest(children)},"leaf_manifest":children,
                  "required_coverage":list(required_coverage or [])+children,
                  "required_roles":sorted(set(synthesis_roles or ["impact"])),"children":True}
            packets.append(self._store_e2_object(actor,project,"packet",f"packet:{root['id']}:{review_kind}:synthesis",body))
        return packets

    def scope_propose(self, actor, project: str, body: dict[str, Any], expected_head: str | None = None) -> dict[str, Any]:
        with self.s.transaction():
            return self._scope_propose(actor, project, body, expected_head)

    def _scope_propose(self, actor, project: str, body: dict[str, Any], expected_head: str | None = None) -> dict[str, Any]:
        self._project(actor,project);need(isinstance(body,dict),"invalid_scope","Scope proposal must be an object")
        required={"roots","selection_rules","exclusion_proposals","authority_refs","discovery_unknowns"}
        v2 = body.get("format") == SCOPE_V2
        if v2: required = required | {"format"}
        need(set(body)==required,"invalid_scope","Scope proposal keys differ",sorted(set(body)^required))
        need(isinstance(body["roots"],list) and len(body["roots"])<=100000,"invalid_scope","Scope roots are invalid")
        roots=[]
        for raw in body["roots"]:
            ref=validate_typed_ref(raw,project=project,expected_kinds={"artifact","source","population"})
            self._resolve_proposal_endpoint(actor,project,ref,current=v2);roots.append(self._identity(ref))
        roots.sort(key=canonical)
        authority=self._validate_authority_refs(actor, project,body["authority_refs"])
        exclusions=[]
        need(isinstance(body["exclusion_proposals"],list),"invalid_scope","Exclusion proposals are invalid")
        for item in body["exclusion_proposals"]:
            need(isinstance(item,dict) and set(item)=={"target_ref","reason","authority_refs"},"invalid_scope","Exclusion proposal keys differ")
            target=validate_typed_ref(item["target_ref"],project=project)
            exclusions.append({"target_ref":self._identity(target),"reason":item["reason"],
                               "authority_refs":self._validate_authority_refs(actor, project,item["authority_refs"])})
        unknowns=body["discovery_unknowns"]
        need(isinstance(unknowns,list) and all(isinstance(item,dict) for item in unknowns),"invalid_scope","Discovery unknowns are invalid")
        normalized={"format":SCOPE_V2 if v2 else "assurance.scope.v1","project":project,"roots":roots,
                    "selection_rules":self._validate_selector(body["selection_rules"]),
                    "exclusion_proposals":exclusions,"authority_refs":authority,
                    "discovery_unknowns":self._identity(unknowns)}
        logical_id="scope:"+digest({key:normalized[key] for key in normalized if key not in {"format","project"}})
        if v2:
            validate_v2_body("scope", normalized)
            logical_id = "scope:" + digest(normalized)
        self._expected_head(project,logical_id,expected_head)
        if v2:
            preview = {"id":uid("AOBJ"), "kind":"scope", "project":project, "digest":digest(normalized), "body":normalized}
            derived = self._derive_obligations_body(project, preview)
            _object_contract("obligations", derived)
            need(len(canonical(derived)) <= MAX_OBJECT_BYTES, "object_too_large", "Scope responsibility closure exceeds bounded object size")
        scope=self._store_e2_object(actor,project,"scope",logical_id,normalized)
        obligations_body=self._derive_obligations_body(project,scope)
        obligations=self._store_e2_object(actor,project,"obligations","obligations:"+scope["id"],obligations_body)
        if v2:
            self._obligations_for_scope(project, scope)
        scope_packets=self._make_packets(actor,project,scope,"scope",[self._packet_marker(scope)], ["impact"])
        obligation_packets=self._make_packets(actor,project,obligations,"obligations",
                                               [item["id"] for item in obligations_body["obligations"]],["trace"])
        return {"scope":scope,"scope_ref":self._object_ref(scope),"obligations":obligations,
                "obligations_ref":self._object_ref(obligations),"packets":scope_packets+obligation_packets,
                "current":self._object_is_current(scope)}

    @staticmethod
    def _profile_logical_id(program: str) -> str:
        need(isinstance(program, str) and program and "\x00" not in program,
             "invalid_profile", "Profile program is invalid")
        return PROFILE_V2_LOGICAL_PREFIX + program

    def _program(self, project: str, program: str) -> dict[str, Any]:
        need(isinstance(program, str) and program, "invalid_profile", "Profile program is required")
        row = self.s.one("SELECT * FROM programs WHERE id=? AND project=?", (program, project), True)
        need(row is not None, "unresolved_reference", "Profile program is missing", program)
        row["body"] = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
        return row

    def _selection_event(self, project: str, program: str) -> dict[str, Any] | None:
        return self._head_event(project, self._profile_logical_id(program))

    def _selection_event_previous_ref(self, project: str, event: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve the exact profile identity named by an event predecessor."""
        previous_id = event.get("previous")
        if previous_id is None:
            return None
        previous_event = self.s.one("SELECT * FROM assurance_events WHERE id=? AND project=?",
                                    (previous_id, project), True)
        need(previous_event is not None, "integrity_error",
             "Profile event predecessor is missing", previous_id)
        previous_object = self.s.one(
            "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='profile'",
            (previous_event["subject_id"], project), True,
        )
        need(previous_object is not None and previous_object["digest"] == previous_event["subject_digest"],
             "integrity_error", "Profile event predecessor subject differs", previous_id)
        previous = self._decode_object(previous_object)
        need(previous["logical_id"] == self._profile_logical_id(previous["body"].get("program", "")),
             "integrity_error", "Profile event predecessor logical identity differs", previous_id)
        return self._object_ref(previous)

    def _selection_profile(self, project: str, program: str) -> dict[str, Any] | None:
        event = self._selection_event(project, program)
        if event is None:
            return None
        need(event["event_kind"] == "adopt", "integrity_error",
             "Canonical profile head is not an adoption event", program)
        row = self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='profile'",
                         (event["subject_id"], project), True)
        need(row is not None and row["digest"] == event["subject_digest"], "integrity_error",
             "Canonical profile head subject is missing or changed", program)
        decoded = self._decode_object(row)
        need(_is_canonical_profile(decoded["body"].get("format")) and
             decoded["body"].get("program") == program and
             decoded["logical_id"] == self._profile_logical_id(program),
             "integrity_error", "Canonical profile head has an invalid identity", program)
        return decoded

    def selected_profile(self, actor, project: str, program: str) -> dict[str, Any]:
        """Read the stable program selection without evaluating any E3 gate.

        A legacy profile is reported as migration_pending and a program with
        no profile as not_enabled.  Neither state creates a head or upgrades a
        v1 object.  The stage evaluator is intentionally absent from this
        unit, so even an adopted mandatory profile does not claim completion.
        """
        self._project(actor, project, read=True)
        self._program(project, program)
        event = self._selection_event(project, program)
        if event is None:
            legacy = False
            for row in self.s.all("SELECT body FROM assurance_objects WHERE project=? AND kind='profile'", (project,)):
                body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
                if body.get("format") == "assurance.profile.v1" and body.get("program") == program:
                    legacy = True
                    break
            return {"format": "daikibo.assurance-selection.v1", "program": program,
                    "profile_ref": None, "head_event": None,
                    "application_mode": None,
                    "profile_format": None,
                    "effective_relation_contract_digest": None,
                    "state": "migration_pending" if legacy else "not_enabled",
                    "status": "migration_pending" if legacy else "not_enabled",
                    "strong_complete": False, "not_enabled": True,
                    "migration_pending": legacy, "stage_evaluator": False}
        profile = self._selection_profile(project, program)
        mode = profile["body"]["application_mode"]
        return {"format": "daikibo.assurance-selection.v1", "program": program,
                "profile_ref": self._object_ref(profile), "head_event": event["id"],
                "application_mode": mode,
                "profile_format": profile["body"]["format"],
                "effective_relation_contract_digest": profile["body"].get(
                    "required_relation_contract_digest",
                    profile["body"].get("relation_contract_digest", REGISTRY_DIGEST),
                ),
                "state": "disabled" if mode == "disabled" else "selected",
                "status": "disabled" if mode == "disabled" else "selected",
                "strong_complete": False, "not_enabled": mode == "disabled",
                "migration_pending": False, "stage_evaluator": False}

    def _validate_profile_records(self, actor, project: str, body: dict[str, Any],
                                  *, for_adoption: bool = False) -> dict[str, Any] | None:
        """Resolve v2/v3 dependencies and enforce one canonical selection CAS."""
        profile_format = body.get("format")
        _validate_canonical_profile_wire(body)
        need(body["project"] == project, "cross_project", "Profile belongs to another project")
        self._program(project, body["program"])
        scope = self._object_by_ref(body["scope_ref"], project, kinds={"scope"})
        obligations = self._object_by_ref(body["obligations_ref"], project, kinds={"obligations"})
        self._validate_scope_pair(scope, obligations, profile_format=profile_format)
        if profile_format == PROFILE_V5_FORMAT:
            paired = self._obligations_for_scope(project, scope)
            need(paired is not None and paired["id"] == obligations["id"] and
                 paired["digest"] == obligations["digest"],
                 "invalid_scope", "Profile requires the unique exact obligation pair")
        need(obligations["logical_id"] == "obligations:" + scope["id"], "stale_reference",
             "Profile obligations do not belong to its scope")
        derived = self._derive_obligations_body(project, scope)
        need(digest(derived) == obligations["digest"], "stale_reference",
             "Profile obligations no longer match its scope")
        event = self._selection_event(project, body["program"])
        previous = body["previous_selection_ref"]
        current = None
        predecessor = None
        if event is None:
            need(previous is None, "stale_head",
                 "Initial canonical profile cannot name a previous selection")
        else:
            current = self._selection_profile(project, body["program"])
            expected = self._object_ref(current)
            expected_event_previous = self._selection_event_previous_ref(project, event)
            _validate_profile_event_predecessor(
                event, _json_field(event, "body"), current["body"], expected_event_previous,
                code="integrity_error",
            )
            # A lost response may replay the exact already-adopted object.
            # Its previous_selection_ref names the head that was current when
            # the event was first written, while the present head names the
            # same object.  Accept that one exact identity for idempotent
            # replay; a different body still has to name the present head.
            replay = (event.get("subject_digest") == digest(body) and
                      event.get("subject_id") == current["id"])
            if replay:
                # The shared event/profile predecessor check above has already
                # compared the replay body with the live event and its CAS
                # predecessor.  Keep the branch explicit for idempotence.
                need(self._identity(body) == self._identity(current["body"]),
                     "integrity_error", "Replayed profile body differs")
            else:
                need(previous is not None and self._identity(previous) == self._identity(expected),
                     "stale_head", "Profile previous_selection_ref does not match the canonical head",
                     {"expected": expected, "actual": previous})
        if previous is not None:
            predecessor = self._object_by_ref(previous, project, kinds={"profile"})
            need(_is_canonical_profile(predecessor["body"].get("format")) and
                 predecessor["body"].get("program") == body["program"] and
                 predecessor["logical_id"] == self._profile_logical_id(body["program"]),
                 "integrity_error", "Profile predecessor identity is invalid")
        self._validate_profile_authority_refs(
            actor, project, body["authority_refs"], body, predecessor,
        )
        if body["application_mode"] == "disabled":
            need(current is not None and previous is not None, "invalid_profile",
                 "A disabled profile must replace an adopted selection")
            need(body["authority_refs"], "invalid_profile",
                 "A disabled profile requires source-backed authority")
            need(self._identity(body["scope_ref"]) == self._identity(current["body"]["scope_ref"]) and
                 self._identity(body["obligations_ref"]) == self._identity(current["body"]["obligations_ref"]),
                 "invalid_profile", "A disabled profile must retain the selected scope and denominator")
        if current is not None:
            old = current["body"]
            semantic_keys = {
                "scope_ref", "obligations_ref", "application_mode", "stage_rules",
                "node_review_rules", "relation_selectors", "test_definition_bindings",
            }
            if profile_has_outputs(profile_format) or profile_has_outputs(old.get("format")):
                semantic_keys.add("required_relation_contract_digest")
            changed = any(self._identity(old.get(key)) != self._identity(body.get(key)) for key in semantic_keys)
            if changed:
                need(body["authority_refs"], "invalid_profile",
                     "A canonical profile replacement requires source-backed authority")
        return current

    def _validate_profile_v2_records(self, actor, project: str, body: dict[str, Any],
                                     *, for_adoption: bool = False) -> dict[str, Any] | None:
        """Compatibility wrapper for callers that explicitly request v2."""
        need(body.get("format") == PROFILE_V2_FORMAT, "invalid_profile",
             "The v2 profile resolver received a non-v2 body")
        return self._validate_profile_records(actor, project, body, for_adoption=for_adoption)

    def _profile_selection_gate(self, actor, project: str, root: dict[str, Any]) -> dict[str, Any]:
        body = root["body"]
        if not _is_canonical_profile(body.get("format")):
            return {}
        current = self._validate_profile_records(actor, project, body, for_adoption=True)
        event = self._selection_event(project, body["program"])
        return {"program": body["program"],
                "logical_id": root["logical_id"],
                "head_event": event["id"] if event else None,
                "previous_selection_ref": body["previous_selection_ref"],
                "application_mode": body["application_mode"],
                "current_profile_ref": self._object_ref(current) if current else None,
                "stage_evaluator": False}

    def profile_propose(self, actor, project: str, program: str | None, body: dict[str, Any],
                        expected_head: str | None = None) -> dict[str, Any]:
        with self.s.transaction(rollback_blobs=True):
            return self._profile_propose(actor, project, program, body, expected_head)

    def _profile_propose(self, actor, project: str, program: str | None, body: dict[str, Any],
                        expected_head: str | None = None) -> dict[str, Any]:
        self._project(actor,project);need(isinstance(body,dict),"invalid_profile","Profile proposal must be an object")
        profile_format = _profile_input_format(body)
        if _is_canonical_profile(profile_format):
            need(program is not None, "invalid_profile",
                 f"{profile_format} requires the program argument")
            _validate_canonical_profile_wire(body)
            need(body["project"] == project and body["program"] == program,
                 "cross_project" if body.get("project") != project else "invalid_profile",
                 f"{profile_format} program/project does not match the API arguments")
            scope = self._object_by_ref(body["scope_ref"], project, kinds={"scope"})
            obligations = self._object_by_ref(body["obligations_ref"], project, kinds={"obligations"})
            normalized_bindings = []
            for binding in body["test_definition_bindings"]:
                artifact = validate_typed_ref(binding["artifact_ref"], project=project,
                                               expected_kinds={"artifact"})
                artifact_row, _artifact_body_value = self._artifact_body(project, artifact)
                need(artifact_row.get("kind") == "test", "invalid_profile",
                     "Profile test_definition_bindings must point to test artifacts")
                check = validate_typed_ref(binding["check_ref"], project=project,
                                           expected_kinds={"test_plan_check", "delivery_check"})
                self._resolve_proposal_endpoint(actor, project, check, current=False)
                normalized_bindings.append({"artifact_ref": self._identity(artifact),
                                            "check_ref": self._identity(check)})
            normalized = {
                "format": profile_format, "project": project, "program": program,
                "scope_ref": self._object_ref(scope), "obligations_ref": self._object_ref(obligations),
                "previous_selection_ref": self._identity(body["previous_selection_ref"]),
                "application_mode": body["application_mode"],
                "stage_rules": self._identity(body["stage_rules"]),
                "node_review_rules": self._identity(body["node_review_rules"]),
                "relation_selectors": list(body["relation_selectors"]),
                "test_definition_bindings": normalized_bindings,
                "change_reason": body["change_reason"],
                "authority_refs": self._identity(body["authority_refs"]),
            }
            if profile_has_outputs(profile_format):
                normalized["required_relation_contract_digest"] = body["required_relation_contract_digest"]
            if profile_format == PROFILE_V5_FORMAT:
                normalized.update(required_scope_contract=SCOPE_V2, required_node_contract=NODE_V2)
            normalized["relation_selectors"] = sorted(set(normalized["relation_selectors"]))
            normalized["test_definition_bindings"] = sorted(normalized["test_definition_bindings"], key=canonical)
            normalized["authority_refs"] = sorted(normalized["authority_refs"], key=canonical)
            _validate_canonical_profile_wire(normalized)
            self._validate_profile_records(actor, project, normalized)
            logical_id = self._profile_logical_id(program)
            self._expected_head(project, logical_id, expected_head)
            profile = self._store_e2_object(actor, project, "profile", logical_id, normalized)
            packets = self._make_packets(actor, project, profile, "profile", [self._packet_marker(profile)], ["impact"])
            return {"profile": profile, "profile_ref": self._object_ref(profile),
                    "scope_ref": normalized["scope_ref"], "obligations_ref": normalized["obligations_ref"],
                    "packets": packets, "expected_head": expected_head,
                    "selection": self.selected_profile(actor, project, program),
                    "current": self._object_is_current(profile)}
        scope_ref=body.get("scope_ref");need(isinstance(scope_ref,dict),"invalid_profile","Profile scope_ref is required")
        scope=self._scope_from_ref(project,scope_ref);scope_ref=self._object_ref(scope)
        obligations=self._obligations_for_scope(project, scope)
        if obligations is None:
            obligations=self._store_e2_object(actor,project,"obligations","obligations:"+scope["id"],self._derive_obligations_body(project,scope))
        else: obligations=self._decode_object(obligations)
        need(scope["body"].get("format") != SCOPE_V2, "invalid_profile", "Legacy profile cannot use scope v2")
        stage_rules=body.get("stage_rules",{"plan":{},"task":{},"integration":{},"delivery":{}})
        need(isinstance(stage_rules,dict) and set(stage_rules)<= {"plan","task","integration","delivery"},"invalid_profile","Profile stage rules are invalid")
        relation_selectors=body.get("relation_selectors",body.get("relations",[]))
        need(isinstance(relation_selectors,list) and all(isinstance(item,str) and item for item in relation_selectors),"invalid_profile","Profile relation selectors are invalid")
        for relation in relation_selectors: registry_entry(relation)
        bindings=body.get("test_definition_bindings",[])
        need(isinstance(bindings,list),"invalid_profile","Test definition bindings are invalid")
        normalized_bindings=[]
        for binding in bindings:
            need(isinstance(binding,dict) and set(binding)=={"artifact_ref","check_ref"},"invalid_profile","Test definition binding keys differ")
            art=validate_typed_ref(binding["artifact_ref"],project=project,expected_kinds={"artifact"})
            art_row,art_body=self._artifact_body(project,art);need(art_row.get("kind")=="test","invalid_profile","Bound artifact is not a test artifact")
            check=validate_typed_ref(binding["check_ref"],project=project,expected_kinds={"test_plan_check","delivery_check"})
            normalized_bindings.append({"artifact_ref":self._identity(art),"check_ref":self._identity(check)})
        normalized={"format":"assurance.profile.v1","project":project,"scope_ref":scope_ref,
                    "obligations_ref":self._object_ref(obligations),"stage_rules":self._identity(stage_rules),
                    "relation_selectors":sorted(set(relation_selectors)),"test_definition_bindings":normalized_bindings}
        if program is not None:
            self.s.one("SELECT id FROM programs WHERE id=? AND project=?",(program,project),True);normalized["program"]=program
        logical_id="profile:"+digest({key:normalized[key] for key in normalized if key not in {"format","project"}})
        self._expected_head(project,logical_id,expected_head)
        profile=self._store_e2_object(actor,project,"profile",logical_id,normalized)
        packets=self._make_packets(actor,project,profile,"profile",[self._packet_marker(profile)],["impact"])
        return {"profile":profile,"profile_ref":self._object_ref(profile),"scope_ref":scope_ref,
                "obligations_ref":self._object_ref(obligations),"packets":packets,"current":self._object_is_current(profile)}

    def _validate_edge_semantics(self, actor, project: str, relation: str,
                                 source: dict[str, Any], target: dict[str, Any],
                                 contract_digest: str | None = None) -> None:
        source=self._identity(source);target=self._identity(target)
        selected_digest = REGISTRY_V1_DIGEST if contract_digest is None else contract_digest
        entry=validate_relation(relation,source,target,project=project,contract_digest=selected_digest)
        source_kind,target_kind=semantic_kind(entry[0]),semantic_kind(entry[1])
        source_info=(self._resolve_locator(actor, source, current=False)
                     if source_kind == "output_artifact" else
                     self._resolve_produced_artifact_endpoint(project, source)
                     if relation == "produced_by" and source_kind == "artifact" else
                     self._resolve_proposal_endpoint(actor,project,source,current=False))
        target_info=(self._resolve_locator(actor, target, current=False)
                     if target_kind == "delivery_check" else
                     self._resolve_proposal_endpoint(actor,project,target,current=False))
        # These checks use the real accepted artifact row, while keeping the
        # registry's endpoint grammar as the primary direction authority.
        if relation in {"realizes","implements"} and source_kind=="artifact" and target_kind=="artifact":
            need(source_info.get("artifact_kind") in {"design","component","interface"},"invalid_relation_endpoint","Relation source is not a design/component/interface")
            need(target_info.get("artifact_kind") in {"requirement","design","component","interface"},"invalid_relation_endpoint","Relation target is not a managed artifact")
        if relation=="decomposes" and source_kind==target_kind=="artifact":
            need(source_info.get("artifact_kind")=="requirement" and target_info.get("artifact_kind")=="requirement",
                 "invalid_relation_endpoint","decomposes requires requirement artifacts")
        if relation in {"verifies","exercises"} and source_kind=="artifact":
            need(source_info.get("artifact_kind")=="test","invalid_relation_endpoint","Test relation source must be a test artifact")
        if relation=="execution_of" and target_kind=="artifact":
            need(target_info.get("artifact_kind")=="test","invalid_relation_endpoint","execution_of target must be a test artifact")
        if relation=="assigned_to" and target_kind=="task_revision":
            self._resolve_locator(actor, self._identity(target), current=False)
        if relation=="produced_by" and target_kind=="task_revision":
            self._resolve_locator(actor, self._identity(target), current=False)
        if relation == "produced_by" and source_kind == "output_artifact" and target_kind == "delivery_check":
            need(source_info.get("mode") == "output_artifact" and target_info.get("mode") == "delivery_check",
                 "invalid_relation_endpoint", "Output producer endpoints are not resolvable")
            need(self._identity(source.get("check")) == self._identity(target) and
                 isinstance(target_info.get("content"), dict) and
                 source.get("output_id") in target_info["content"].get("produces", []),
                 "invalid_relation_endpoint", "Output producer check identity or declaration differs")
        if relation == "contains" and source_kind in {"delivery_snapshot", "actual_delivery_commit"} and target_kind == "output_artifact":
            membership = self.contains(actor, project, source, target)
            need(membership.get("contains") is True, "invalid_relation_endpoint",
                 "Output is not a member of the Delivery bundle")
        if relation=="extracted_from":
            need(target_kind in {"traceability_ref","source_span"},"invalid_relation_endpoint","extracted_from target must be a source span")

    def edge_propose(self, actor, project: str, body: dict[str, Any], expected_head: str | None = None) -> dict[str, Any]:
        with self.s.transaction(rollback_blobs=True):
            return self._edge_propose(actor, project, body, expected_head)

    def _edge_propose(self, actor, project: str, body: dict[str, Any], expected_head: str | None = None) -> dict[str, Any]:
        self._project(actor,project);need(isinstance(body,dict),"invalid_edge","Edge proposal must be an object")
        required={"source_ref","target_ref","relation","scope_ref","claim","obligation_ids","required_evidence_refs","authority_refs"}
        need(required <= set(body),"invalid_edge","Edge proposal is missing fields",sorted(required-set(body)))
        relation=body["relation"]
        contract_digest = body.get("relation_contract_digest", REGISTRY_V1_DIGEST)
        need(contract_digest in {REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST},
             "invalid_registry", "Edge relation registry digest is unknown")
        source=validate_typed_ref(body["source_ref"],project=project);target=validate_typed_ref(body["target_ref"],project=project)
        self._validate_edge_semantics(actor,project,relation,source,target,contract_digest)
        scope_ref=validate_typed_ref(body["scope_ref"],project=project,expected_kinds={"assurance_object"})
        scope_obj=self._object_by_ref(scope_ref,project,kinds={"scope","profile"})
        if scope_obj["kind"] == "profile":
            selectors=scope_obj["body"].get("relation_selectors", [])
            need(relation in selectors, "invalid_relation", "Relation is outside the selected profile", relation)
            if semantic_kind(target) == "artifact" and relation == "execution_of":
                bindings=scope_obj["body"].get("test_definition_bindings", [])
                need(any(self._identity(item.get("artifact_ref")) == self._identity(target)
                         for item in bindings if isinstance(item, dict)),
                     "invalid_relation_endpoint", "Execution test artifact is not bound by the selected profile")
        evidence=[]
        need(isinstance(body["required_evidence_refs"],list),"invalid_edge","Evidence refs are invalid")
        for ref in body["required_evidence_refs"]: evidence.append(self._identity(validate_typed_ref(ref,project=project)))
        authority=self._validate_authority_refs(actor, project,body["authority_refs"])
        obligations=body["obligation_ids"]
        need(isinstance(obligations,list) and all(isinstance(item,str) and item for item in obligations),"invalid_edge","Obligation IDs are invalid")
        need(len(obligations)==len(set(obligations)),"invalid_edge","Duplicate obligation IDs")
        scope_for_obligations=self._scope_from_ref(project,scope_ref)
        obligation_row=self._obligations_for_scope(project, scope_for_obligations, scope_obj)
        need(obligation_row is not None,"unresolved_reference","Edge scope has no derived obligations object")
        obligation_body=self._decode_object(obligation_row)["body"]
        known_obligations={item.get("id") for item in obligation_body.get("obligations",[]) if isinstance(item,dict)}
        need(set(obligations)<=known_obligations,"invalid_edge","Edge obligation is outside the selected denominator",
             sorted(set(obligations)-known_obligations))
        claim=body["claim"];need(isinstance(claim,str) and claim,"invalid_edge","Edge claim is required")
        normalized={"format":"assurance.edge.v1","project":project,"source_ref":self._identity(source),"target_ref":self._identity(target),
                    "relation":relation,"relation_contract_digest":contract_digest,"scope_ref":self._identity(scope_ref),
                    "claim":claim,"obligation_ids":sorted(obligations),"required_evidence_refs":evidence,"authority_refs":authority}
        if body.get("supersedes_ref") is not None:
            normalized["supersedes_ref"]=self._identity(validate_typed_ref(body["supersedes_ref"],project=project,expected_kinds={"assurance_object"}))
        logical_id=body.get("edge_id") or "edge:"+digest({key:normalized[key] for key in normalized if key not in {"format","project"}})
        need(isinstance(logical_id,str) and logical_id,"invalid_edge","Edge logical identity is invalid")
        self._expected_head(project,logical_id,expected_head)
        edge=self._store_e2_object(actor,project,"edge",logical_id,normalized)
        packets=self._make_packets(actor,project,edge,"edge",[self._packet_marker(edge)],["trace"])
        return {"edge":edge,"edge_ref":self._object_ref(edge),"packets":packets,"current":self._object_is_current(edge)}

    def _member_of(self, actor, project: str, container: dict[str, Any], member: dict[str, Any]) -> bool:
        """Return strict membership for a set container/member pair.

        Set collection uses the same finite membership relations exposed by
        ``assurance.contains``.  It never treats a matching display name as
        membership; the complete pinned identity and the relevant controller
        row are checked first.  The common direct case is intentionally cheap
        because large relation sets may inspect 100k immutable edges.
        """
        parent = self._identity(validate_typed_ref(self._identity(container), project=project))
        child = self._identity(validate_typed_ref(self._identity(member), project=project))
        if parent == child:
            return True
        pk, ck = semantic_kind(parent), semantic_kind(child)

        if pk == "source" and ck == "source_span":
            locator = child["locator"]
            if locator["source_id"] != parent["source"] or locator["blob_digest"] != parent["blob_digest"]:
                return False
            raw = self.s.blob_get(parent["blob_digest"])
            return 0 <= locator["byte_start"] <= locator["byte_end"] <= len(raw)

        if pk == "source_span" and ck == "source_span":
            left, right = parent["locator"], child["locator"]
            if left["source_id"] != right["source_id"] or left["blob_digest"] != right["blob_digest"]:
                return False
            # The resolver checks the source bytes and span hashes.  Calling
            # it here avoids accepting a range-only forged wrapper.
            outer = self._trace_refs.resolve(actor, project, left, require_current=False).get("content", {})
            inner = self._trace_refs.resolve(actor, project, right, require_current=False).get("content", {})
            return (outer.get("byte_start", -1) <= inner.get("byte_start", -1) and
                    inner.get("byte_end", -1) <= outer.get("byte_end", -1))

        if pk == "population" and ck == "population_item":
            child_population = child["population"]
            if self._identity(parent) != self._identity(child_population):
                return False
            row = self.s.one("SELECT id,digest,project FROM traceability_items WHERE revision=? AND id=? AND project=?",
                             (parent["revision"], child["item"], project))
            return bool(row and row["digest"] == child["item_digest"])

        if pk == "population_item" and ck == "population_item":
            parent_population = parent["population"]
            child_population = child["population"]
            if self._identity(parent_population) != self._identity(child_population):
                return False
            group = self.s.one("SELECT * FROM traceability_items WHERE revision=? AND id=? AND project=?",
                               (parent_population["revision"], parent["item"], project))
            leaf = self.s.one("SELECT id,digest,leaf FROM traceability_items WHERE revision=? AND id=? AND project=?",
                              (parent_population["revision"], child["item"], project))
            if not group or group["item_kind"] != "group" or not leaf or not leaf["leaf"]:
                return False
            body = parse_json(group["body"], limit=MAX_OBJECT_BYTES)
            members = body.get("atom_ids") or body.get("leaf_ids") or body.get("members") or body.get("item_ids")
            return isinstance(members, list) and len(members) == len(set(members)) and child["item"] in members

        if pk == "artifact" and ck == "artifact_ac":
            locator = child["locator"]
            if (locator["artifact"] != parent["artifact"] or locator["revision"] != parent["revision"] or
                    locator["body_digest"] != parent["body_digest"]):
                return False
            _row, body = self._artifact_body(project, parent)
            value: Any = body
            try:
                tokens = locator["ac_pointer"].split("/")
                need(tokens and tokens[0] == "", "invalid_membership", "Artifact AC pointer is malformed")
                for token in tokens[1:]:
                    token = token.replace("~1", "/").replace("~0", "~")
                    value = value[int(token)] if isinstance(value, list) else value[token]
            except (KeyError, IndexError, TypeError, ValueError):
                return False
            return digest(value) == locator["ac_digest"]

        if pk == "test_plan" and ck == "test_plan_check":
            if self._identity(parent) != self._identity(child.get("plan", {})):
                return False
            plan = self._resolve_locator(actor, parent, current=False)
            checks = (plan.get("payload") or {}).get("plan_body", {}).get("checks", [])
            return sum(1 for item in checks if isinstance(item, dict) and item.get("id") == child["check_id"] and
                       digest(item) == child["check_digest"]) == 1

        if pk == "delivery_snapshot" and ck == "delivery_check":
            if self._identity(parent) != self._identity(child.get("delivery", {})):
                return False
            delivery = self._resolve_locator(actor, parent, current=False)
            checks = (delivery.get("payload") or {}).get("checks", [])
            return sum(1 for item in checks if isinstance(item, dict) and item.get("id") == child["check_id"] and
                       digest(item) == child["check_digest"]) == 1

        if ck == "output_artifact" and pk in {"delivery_snapshot", "actual_delivery_commit"}:
            try:
                output_resolution = self._resolve_locator(actor, child, current=False)
                output = output_resolution.get("content") or {}
                output_ref = child
                if pk == "delivery_snapshot":
                    same_delivery = self._delivery_identity(output_ref.get("delivery")) == self._delivery_identity(parent)
                    need(same_delivery, "false_membership", "Output belongs to another Delivery snapshot")
                    return True
                actual = self._resolve_locator(actor, parent, current=False)
                actual_payload = actual.get("payload") or {}
                delivery_ref = actual_payload.get("delivery_snapshot_ref")
                same_delivery = self._delivery_identity(output_ref.get("delivery")) == self._delivery_identity(delivery_ref)
                need(same_delivery and output.get("repo") == parent.get("repository"),
                     "false_membership", "Output is not in the actual Delivery bundle")
                return True
            except Fault as exc:
                return False

        if pk == "candidate" and ck == "candidate_symbol":
            # Validate the generic container through the same historical
            # candidate core before checking symbol membership.  Comparing
            # five copied fields alone would let a corrupt/missing run,
            # receipt, snapshot or CAS pass through ``contains``.
            self._resolve_locator(actor, parent, current=False)
            locator = child["locator"]
            return all(locator.get(left) == parent.get(right) for left, right in (
                ("candidate", "candidate"), ("task", "task"), ("task_revision", "task_revision"),
                ("candidate_digest", "candidate_digest"), ("snapshot_digest", "snapshot_digest")))

        if pk == "actual_delivery_commit" and ck in {"git_file", "git_symbol"}:
            locator = child["locator"]
            if not all(locator.get(key) == parent.get(key) for key in ("repository", "object_format", "commit")):
                return False
            self._resolve_locator(actor, parent, current=False)
            self._trace_refs.resolve(actor, project, locator, require_current=False)
            payload = self._resolve_locator(actor, parent, current=False).get("payload") or {}
            snapshot_payload = self._resolve_locator(actor, parent["delivery"], current=False).get("payload") or {}
            manifest = validate_git_material_payload(self.s, payload, snapshot_payload.get("snapshot"))
            entries = {item["path"]: item for item in manifest["entry_manifest"] if item["kind"] != "tree"}
            entry = entries.get(locator.get("path"))
            return bool(entry and entry["mode"] == locator.get("mode") and entry["oid"] == locator.get("blob_oid"))

        if pk == "delivery_snapshot" and ck == "candidate":
            # A candidate is a member only when the pinned delivery snapshot
            # explicitly carries the same task candidate identity.  Do not
            # infer membership from a candidate ID appearing in free text.
            delivery = self._resolve_locator(actor, parent, current=False)
            payload = delivery.get("payload") or {}
            candidates = payload.get("candidates") or payload.get("candidate_refs") or []
            return any(self._identity(item) == child for item in candidates if isinstance(item, dict))
        return False

    def _relation_center_supported(self, relation: str, center: dict[str, Any],
                                   contract_digest: str | None = None) -> bool:
        entry = registry_entry(relation, contract_digest=contract_digest)
        kind = semantic_kind(center)
        if kind in entry["source_kinds"] or kind in entry["target_kinds"]:
            return True
        child_kinds = {
            "source": {"source_span"}, "source_span": {"source_span"},
            "population": {"population_item"}, "population_item": {"population_item"},
            "artifact": {"artifact_ac"}, "test_plan": {"test_plan_check"},
            "candidate": {"candidate_symbol"},
            "actual_delivery_commit": {"git_file", "git_symbol"},
            "delivery_snapshot": {"delivery_check", "candidate", "output_artifact"},
            "output_artifact": {"delivery_check"},
        }.get(kind, set())
        return bool(child_kinds & (set(entry["source_kinds"]) | set(entry["target_kinds"])))

    def _latest_edges_for_set(self, actor, project: str, center: dict[str, Any], relation: str,
                              direction: str, scope_ref: dict[str, Any],
                              contract_digest: str | None = None) -> list[dict[str, Any]]:
        rows=[]; center_identity=self._identity(center); scope_identity=self._identity(scope_ref)
        query="""SELECT row.* FROM assurance_objects row
                  JOIN (SELECT logical_id,MAX(revision) revision FROM assurance_objects
                        WHERE project=? AND kind='edge' GROUP BY logical_id) latest
                    ON latest.logical_id=row.logical_id AND latest.revision=row.revision
                  WHERE row.project=? AND row.kind='edge'
                  ORDER BY row.logical_id,row.revision,row.id"""
        for row in self.s.all(query,(project,project)):
            body=parse_json(row["body"],limit=MAX_OBJECT_BYTES)
            if (body.get("relation")!=relation or
                    body.get("relation_contract_digest") != (REGISTRY_V1_DIGEST if contract_digest is None else contract_digest) or
                    self._identity(body.get("scope_ref"))!=scope_identity):continue
            endpoint=body.get("source_ref") if direction=="outgoing" else body.get("target_ref")
            endpoint=self._identity(validate_typed_ref(endpoint, project=project))
            if endpoint != center_identity and not self._member_of(actor, project, center, endpoint):continue
            decoded=dict(row);decoded["body"]=body;decoded["refs"]=[]
            rows.append(decoded)
        rows.sort(key=lambda edge:(edge["id"],edge["revision"],edge["digest"]))
        return rows

    def _consumer_c_obligation_material(self, actor, project: str,
                                        binding: dict[str, Any], *, current: bool = False
                                        ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Read the controller-owned Delivery declaration denominator for C.

        Consumer-C edges deliberately do not use ``obligation_ids`` as their
        authority.  The set still needs an immutable expected-obligations
        object, however, so currentness and review alignment can bind the set
        to the same declaration identities as the sealed Unit 2a denominator.
        This helper reads only the pinned Delivery snapshot and its declared
        checks; output observations are not consulted.
        """
        need(isinstance(binding, dict), "invalid_set", "Consumer-C denominator binding is malformed")
        required = {"format", "project", "relation", "direction", "relation_contract_digest",
                    "center_ref", "scope_ref"}
        need(set(binding) == required, "invalid_set", "Consumer-C denominator binding keys differ")
        need(binding["format"] == "assurance.delivery-declared-output.v1" and
             binding["project"] == project and
             binding["relation_contract_digest"] == REGISTRY_V2_DIGEST and
             binding["relation"] in {"produced_by", "contains"} and
             binding["direction"] in {"incoming", "outgoing"},
             "invalid_registry", "Consumer-C denominator binding is not the v2 output registry")
        center = validate_typed_ref(binding["center_ref"], project=project)
        scope = validate_typed_ref(binding["scope_ref"], project=project,
                                   expected_kinds={"assurance_object"})
        need(scope["object_kind"] in {"scope", "profile"}, "invalid_set",
             "Consumer-C denominator scope is not an Assurance scope")
        if current:
            # The endpoint currentness check is part of the same resolver
            # boundary used by edge/set readers.  A retained historical C
            # object remains readable through resolve_pinned, while a current
            # adopted set cannot silently keep an obsolete Delivery pin.
            self._resolve_locator(actor, center, current=True)

        center_kind = semantic_kind(center)
        if center_kind == "actual_delivery_commit":
            snapshot = center.get("delivery")
        elif center_kind in {"delivery_snapshot", "output_artifact", "delivery_check"}:
            snapshot = center if center_kind == "delivery_snapshot" else center.get("delivery")
        else:
            raise Fault("invalid_set", "Consumer-C center has no Delivery declaration material", center_kind)
        snapshot = validate_typed_ref(self._identity(snapshot), project=project,
                                      expected_kinds={"delivery_snapshot"})
        snapshot = self._identity(snapshot)

        # Import lazily to keep the Assurance storage layer independent from
        # the public denominator collector's module initialization.
        from .assurance_denominators import _delivery_material

        unresolved: list[dict[str, Any]] = []
        material, _refs = _delivery_material(
            self.c, actor, project, "delivery", snapshot, unresolved,
            include_declared_outputs=True,
        )
        declared = material.get("declared_outputs", {}) if isinstance(material, dict) else {}
        status = declared.get("status") if isinstance(declared, dict) else "unverified"
        items = declared.get("items", []) if isinstance(declared, dict) else []
        if not isinstance(items, list):
            items = []

        # A center narrows the request population by the same typed owner
        # boundary as build_relation_request.  A snapshot selects the full
        # declaration inventory; each actual commit selects its repository
        # partition.  The common owner matcher keeps the actual partitions'
        # union equal to the snapshot population.
        selected: list[dict[str, Any]] = []
        center_identity = self._identity(center)
        for item in items:
            if not isinstance(item, dict):
                continue
            definition = item.get("definition")
            source_ref = item.get("source_ref")
            producer_ref = item.get("producer_ref")
            if not isinstance(definition, dict) or not isinstance(source_ref, dict) or not isinstance(producer_ref, dict):
                continue
            if (center_kind in {"delivery_snapshot", "actual_delivery_commit"} and
                    delivery_declaration_owner_matches(center, definition)):
                selected.append(item)
            elif center_kind == "output_artifact":
                if (definition.get("id") == center.get("output_id") and
                        self._delivery_identity(center.get("delivery")) == self._delivery_identity(source_ref)):
                    selected.append(item)
            elif center_kind == "delivery_check":
                if self._identity(producer_ref) == center_identity:
                    selected.append(item)

        records: dict[str, dict[str, Any]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for item in selected:
            identity = item.get("identity")
            if not isinstance(identity, dict):
                identity = {
                    "category": "delivery_declared_output",
                    "source_ref": self._identity(item["source_ref"]),
                    "pointer": item["pointer"],
                    "value_digest": item["value_digest"],
                }
            identity = self._identity(identity)
            obligation_id = "obligation:" + digest(identity)
            records[obligation_id] = {
                **identity,
                "id": obligation_id,
                "contributors": [],
                "introduced_at": "integration",
                "required_at": "delivery",
            }
            # Keep the controller's exact declaration pairing beside the
            # closed obligation record.  Edge assignment must be linear in
            # the declaration population; re-reading Delivery material for
            # every obligation would turn a bounded 100k population into a
            # quadratic resolver.
            metadata[obligation_id] = {
                "source_ref": self._identity(item["source_ref"]),
                "definition": self._identity(item["definition"]),
                "producer_ref": self._identity(item["producer_ref"]),
            }
        binding_identity = self._identity(binding)
        body = {
            "format": "assurance.obligations.v1", "project": project,
            "scope_ref": self._identity(scope),
            "derivation_version": "assurance-delivery-declared-output-v1",
            "input_refs": [self._identity(snapshot)],
            "consumer_binding": binding_identity,
            "declaration_status": status if isinstance(status, str) else "unverified",
            "obligations": [records[key] for key in sorted(records)],
        }
        return body, metadata

    def _consumer_c_edge_obligations(self, actor, project: str,
                                     obligations: dict[str, Any],
                                     edge: dict[str, Any],
                                     metadata: dict[str, dict[str, Any]] | None = None,
                                     index: dict[bytes, list[str]] | None = None,
                                     ) -> list[str]:
        binding = obligations["body"].get("consumer_binding")
        if not isinstance(binding, dict):
            return []
        if metadata is None:
            _body, metadata = self._consumer_c_obligation_material(
                actor, project, binding, current=False,
            )
        if index is None:
            index = self._consumer_c_edge_index(binding, metadata)
        edge_body = edge.get("body", {})
        source = edge_body.get("source_ref")
        target = edge_body.get("target_ref")
        if not isinstance(source, dict) or not isinstance(target, dict):
            return []
        source_kind, target_kind = semantic_kind(source), semantic_kind(target)
        relation = binding.get("relation")
        candidates: list[str]
        if relation == "produced_by" and edge_body.get("relation") == "produced_by":
            candidates = index.get(canonical([
                source.get("output_id"),
                self._delivery_identity(source.get("delivery")),
                self._identity(target),
            ]), [])
        elif relation == "contains" and edge_body.get("relation") == "contains":
            candidates = index.get(canonical([
                target.get("output_id"),
                self._delivery_identity(target.get("delivery")),
            ]), [])
        else:
            candidates = []
        matched: list[str] = []
        for obligation_id in candidates:
            item = metadata.get(obligation_id)
            if item is None:
                continue
            # The controller declaration resolver is the source of the
            # producer/definition pairing.  The helper returns that pairing
            # from the same pinned read that created the obligation IDs.
            definition = item.get("definition")
            source_ref = item.get("source_ref")
            if not isinstance(source_ref, dict):
                continue
            exact_definition = definition
            producer_ref = item.get("producer_ref")
            if (not isinstance(exact_definition, dict) or
                    not isinstance(producer_ref, dict)):
                continue
            center_ref = binding.get("center_ref")
            if relation == "contains" and isinstance(center_ref, dict) and \
                    semantic_kind(center_ref) == "actual_delivery_commit" and \
                    not delivery_declaration_owner_matches(center_ref, exact_definition):
                continue
            output_id = exact_definition.get("id")
            if edge_body.get("relation") == "produced_by":
                if (source_kind == "output_artifact" and target_kind == "delivery_check" and
                        source.get("output_id") == output_id and
                        self._delivery_identity(source.get("delivery")) == self._delivery_identity(source_ref) and
                        self._identity(source.get("check")) == self._identity(producer_ref) and
                        self._identity(target) == self._identity(producer_ref)):
                    matched.append(obligation_id)
            elif edge_body.get("relation") == "contains":
                parent_snapshot = source if source_kind == "delivery_snapshot" else source.get("delivery")
                if (target_kind == "output_artifact" and target.get("output_id") == output_id and
                        isinstance(parent_snapshot, dict) and
                        self._delivery_identity(parent_snapshot) == self._delivery_identity(source_ref) and
                        (source_kind != "actual_delivery_commit" or
                         delivery_declaration_owner_matches(source, exact_definition)) and
                        self._delivery_identity(target.get("delivery")) == self._delivery_identity(source_ref)):
                    matched.append(obligation_id)
        return sorted(set(matched))

    def _consumer_c_edge_index(self, binding: dict[str, Any],
                                metadata: dict[str, dict[str, Any]]) -> dict[bytes, list[str]]:
        """Index controller declarations by the v2 endpoint identity tuple."""
        index: dict[bytes, list[str]] = {}
        relation = binding.get("relation")
        for obligation_id, item in metadata.items():
            definition = item.get("definition")
            source_ref = item.get("source_ref")
            producer_ref = item.get("producer_ref")
            if not isinstance(definition, dict) or not isinstance(source_ref, dict):
                continue
            if relation == "produced_by":
                if not isinstance(producer_ref, dict):
                    continue
                key = canonical([
                    definition.get("id"),
                    self._delivery_identity(source_ref),
                    self._identity(producer_ref),
                ])
            elif relation == "contains":
                key = canonical([
                    definition.get("id"),
                    self._delivery_identity(source_ref),
                ])
            else:
                continue
            index.setdefault(key, []).append(obligation_id)
        return index

    def _set_owner_obligation_ids(self, obligations: dict[str, Any], center: dict[str, Any],
                                  relation: str, direction: str, actor=None) -> set[str]:
        """Select the immutable obligation population owned by one set center.

        A scope retains the complete denominator for the project.  A relation
        set centered on one Task is a local proof over that Task's contributor
        obligations, however, so it must not be forced to claim another
        repository's/Task's obligations.  The owner is read from the saved
        typed contributor contract; a caller cannot supply a subset or rename
        an owner.  Other relations retain the global set population until
        their registry-specific owner resolver is available.
        """
        body = obligations.get("body") if isinstance(obligations, dict) else None
        if not isinstance(body, dict):
            return set()
        items = body.get("obligations", [])
        if not isinstance(items, list):
            return set()
        selected = {item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
        try:
            center_kind = semantic_kind(center)
        except (KeyError, TypeError, AttributeError):
            return selected
        if body.get("format") == OBLIGATIONS_V2:
            # The saved pair retains the full union. Assignment is a relation/owner
            # projection, using the same closed Q category and owner contract as G.
            from .assurance_criteria import _relation_categories, _center_matches_obligation
            categories = _relation_categories(relation, REGISTRY_V2_DIGEST, center_ref=center)
            q_items = [x for x in items if x.get("category") in
                       {"artifact_responsibility", "artifact_structural_responsibility"}]
            if relation == "implements":
                return {x["id"] for x in q_items if x["category"] in categories and
                    _center_matches_obligation(self.c, actor, obligations["project"], relation, direction,
                        self._identity(center), x, registry_digest=REGISTRY_V2_DIGEST)}
            # Historical acceptance/population assignment semantics are retained;
            # responsibilities belong exclusively to their registry relation.
            items = [x for x in items if x not in q_items]
            selected = {x["id"] for x in items}
        if (direction == "incoming" and relation in {"assigned_to", "produced_by"}
                and center_kind == "task_revision"):
            # The wire shape alone is insufficient for ownership.  Resolve the
            # owner against the same project and, on the production path, the
            # retained current Task material before partitioning.  A malformed
            # or foreign center must keep the denominator visible so it cannot
            # turn an unresolved relation into a vacuous PASS.
            project = obligations.get("project")
            if body.get("project") != project or not isinstance(project, str) or not project:
                return selected
            try:
                center_ref = validate_typed_ref(
                    self._identity(center), project=project,
                    expected_kinds={"task_revision"},
                )
                if actor is not None:
                    scope_ref = body.get("scope_ref")
                    scope_ref = validate_typed_ref(
                        scope_ref, project=project,
                        expected_kinds={"assurance_object"},
                    )
                    need(scope_ref["object_kind"] in {"scope", "profile"},
                         "invalid_reference", "Owner obligations are outside an Assurance scope")
                    self._object_by_ref(scope_ref, project, kinds={"scope", "profile"})
                    self._resolve_locator(actor, center_ref, current=True)
            except (Fault, KeyError, TypeError, ValueError):
                return selected
            center_identity = self._identity(center_ref)
            # Narrowing is sound only when the *whole selected population* is
            # explicitly owner-typed.  Older/global obligations frequently
            # omit contributors; treating that omission as an empty owner
            # partition makes the denominator disappear and can turn a failed
            # edge into a vacuous pass.  Preserve the full denominator until
            # every item has a valid Task-revision owner list.
            owner_map: dict[str, list[Any]] = {}
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                contributors = item.get("contributors")
                if not isinstance(contributors, list) or not contributors:
                    return selected
                owners: list[Any] = []
                for contributor in contributors:
                    task_ref = contributor.get("task_ref") if isinstance(contributor, dict) else None
                    if not isinstance(task_ref, dict):
                        return selected
                    try:
                        normalized = validate_typed_ref(
                            self._identity(task_ref), project=project,
                            expected_kinds={"task_revision"},
                        )
                        if actor is not None:
                            self._resolve_locator(actor, normalized, current=True)
                    except (Fault, KeyError, TypeError, ValueError):
                        return selected
                    owners.append(self._identity(normalized))
                if not owners:
                    return selected
                owner_map[item["id"]] = owners
            selected = {
                obligation_id for obligation_id, owners in owner_map.items()
                if any(owner == center_identity for owner in owners)
            }
        return selected

    def _edge_assignment(self, obligations: dict[str, Any], edges: list[dict[str, Any]],
                         actor=None, *, center: dict[str, Any] | None = None,
                         relation: str | None = None,
                         direction: str | None = None) -> dict[str, list[dict[str, Any]]]:
        all_items = obligations["body"].get("obligations", [])
        owner_ids = ({item["id"] for item in all_items if isinstance(item, dict) and isinstance(item.get("id"), str)}
                     if center is None or relation is None or direction is None
                     else self._set_owner_obligation_ids(obligations, center, relation, direction, actor))
        assignments={item_id:[] for item_id in sorted(owner_ids)}
        consumer_metadata = None
        consumer_index = None
        if obligations["body"].get("consumer_binding"):
            _body, consumer_metadata = self._consumer_c_obligation_material(
                actor, obligations["project"], obligations["body"]["consumer_binding"],
                current=False,
            )
            consumer_index = self._consumer_c_edge_index(
                obligations["body"]["consumer_binding"], consumer_metadata,
            )
        for edge in edges:
            ebody=edge["body"]
            claimed = (self._consumer_c_edge_obligations(
                actor, obligations["project"], obligations, edge,
                consumer_metadata, consumer_index,
            ) if consumer_metadata is not None else ebody.get("obligation_ids", []))
            for obligation in claimed:
                if obligation in assignments:
                    assignments[obligation].append(self._object_ref(edge))
        for value in assignments.values():value.sort(key=lambda ref:(ref["object"],ref["object_digest"]))
        return assignments

    def set_propose(self, actor, project: str, body: dict[str, Any], expected_head: str | None = None) -> dict[str, Any]:
        with self.s.transaction(rollback_blobs=True):
            return self._set_propose(actor, project, body, expected_head)

    def _set_propose(self, actor, project: str, body: dict[str, Any], expected_head: str | None = None) -> dict[str, Any]:
        self._project(actor,project);need(isinstance(body,dict),"invalid_set","Relation-set proposal must be an object")
        for field in ("center_ref","relation","direction","scope_ref","criteria"):
            need(field in body,"invalid_set","Relation-set proposal is missing a field",field)
        center=validate_typed_ref(body["center_ref"],project=project)
        relation=body["relation"];direction=body["direction"]
        contract_digest = body.get("relation_contract_digest", REGISTRY_V1_DIGEST)
        need(contract_digest in {REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST},
             "invalid_registry", "Set relation registry digest is unknown")
        need(direction in {"outgoing","incoming"},"invalid_set","Relation-set direction is invalid")
        criteria_requirements = self._set_criteria_requirements(
            relation, body["criteria"], contract_digest)
        need(self._relation_center_supported(relation, center, contract_digest), "invalid_set",
             "Relation-set center is not a declared container for this relation", semantic_kind(center))
        scope_ref=validate_typed_ref(body["scope_ref"],project=project,expected_kinds={"assurance_object"})
        scope_obj=self._object_by_ref(scope_ref,project,kinds={"scope","profile"})
        if scope_obj["kind"] == "profile":
            need(relation in scope_obj["body"].get("relation_selectors", []),
                 "invalid_relation", "Relation is outside the selected profile", relation)
        canonical_scope=self._object_ref(scope_obj) if scope_obj["kind"]=="scope" else self._identity(scope_obj["body"]["scope_ref"])
        expected_ref=body.get("expected_obligations_ref")
        consumer_binding = None
        # Registry v2 extends the existing candidate/artifact/Task relations
        # with Delivery output endpoints.  Only those canonical Delivery
        # centers use the C declaration denominator; a valid v2 Task,
        # candidate, or Knowledge-artifact center stays on the pre-existing
        # scope obligations/assignment path.
        consumer_c_center = (
            contract_digest == REGISTRY_V2_DIGEST and
            relation in {"produced_by", "contains"} and
            semantic_kind(center) in {
                "delivery_snapshot", "actual_delivery_commit",
                "output_artifact", "delivery_check",
            }
        )
        if consumer_c_center:
            consumer_binding = {
                "format": "assurance.delivery-declared-output.v1", "project": project,
                "relation": relation, "direction": direction,
                "relation_contract_digest": REGISTRY_V2_DIGEST,
                "center_ref": self._identity(center),
                "scope_ref": self._identity(canonical_scope),
            }
            derived_body, _records = self._consumer_c_obligation_material(
                actor, project, consumer_binding, current=False,
            )
            if expected_ref is None:
                logical_id = "obligations:consumer-c:" + digest(consumer_binding)
                obligations = self._store_e2_object(
                    actor, project, "obligations", logical_id, derived_body,
                )
                expected_ref = self._object_ref(obligations)
            else:
                obligations = self._object_by_ref(expected_ref, project, kinds={"obligations"})
                need(obligations["body"].get("consumer_binding") == consumer_binding,
                     "invalid_set", "Consumer-C set obligations are bound to another relation")
                need(obligations["digest"] == digest(derived_body), "stale_reference",
                     "Consumer-C declaration denominator differs from the selected Delivery material")
        elif expected_ref is None:
            latest=self._obligations_for_scope(project, self._scope_from_ref(project,scope_ref), self._object_by_ref(scope_ref, project, kinds={"scope", "profile"}))
            need(latest is not None,"unresolved_reference","Relation-set obligations are missing")
            obligations=self._decode_object(latest);expected_ref=self._object_ref(obligations)
        else:
            obligations=self._object_by_ref(expected_ref,project,kinds={"obligations"})
            need(self._identity(obligations["body"].get("scope_ref"))==self._identity(canonical_scope),
                 "cross_scope", "Relation-set obligations belong to another scope")
        need(self._identity(obligations["body"].get("scope_ref"))==self._identity(canonical_scope),
             "cross_scope", "Relation-set obligations belong to another scope")
        selected_scope = self._scope_from_ref(project, scope_ref)
        if selected_scope["body"].get("format") == SCOPE_V2 and consumer_binding is None:
            paired = self._obligations_for_scope(project, selected_scope,
                self._object_by_ref(scope_ref, project, kinds={"scope", "profile"}))
            need(paired["id"] == obligations["id"], "invalid_set", "Set does not bind the selected exact v2 pair")
        edges=self._latest_edges_for_set(actor,project,center,relation,direction,scope_ref,contract_digest)
        # Edge objects are immutable and were endpoint-validated at proposal
        # time.  Re-read their canonical identities here; a set never trusts
        # a caller-supplied subset and does not repeat O(N) external row
        # lookups for a 100k-edge mechanical partition.
        # The manifest reader validates one canonical identity order.  The
        # discovery query is intentionally logical-id ordered, which is not
        # the same order once more than one immutable edge is present.  Sort
        # the actual rows before deriving every partition, assignment and
        # stream digest so producer and reader share one sequence.
        edges=sorted(edges,key=lambda edge:(edge["id"],edge["revision"],edge["digest"]))
        identities=[(edge["id"],edge["revision"],edge["digest"]) for edge in edges]
        stream_digest=digest(identities)
        partition_descriptors=[]
        for index,start in enumerate(range(0,len(edges),MAX_PAGE)):
            chunk=edges[start:start+MAX_PAGE]
            chunk_payload={"format":"assurance.edge-manifest-partition.v1","project":project,"index":index,
                           "stream_digest":stream_digest,"edges":[{"id":item["id"],"revision":item["revision"],"digest":item["digest"]} for item in chunk]}
            child,_=self.store_material(actor,project,"edge_manifest_partition",chunk_payload,
                                         [self._object_ref(item) for item in chunk],
                                         {"kind":"set_proposal","id":project},{"source":"assurance.set_propose"})
            child_body=child["body"]
            partition_descriptors.append({"id":child["id"],"digest":child["digest"],"payload_blob":child_body["payload_blob"],
                                         "index":index,"count":len(chunk)})
        manifest_payload={"format":"assurance.edge-manifest.v1","project":project,"scope_ref":scope_ref,
                          "center_ref":center,"relation":relation,"direction":direction,"count":len(identities),
                          "stream_digest":stream_digest,"partitions":partition_descriptors}
        manifest,_=self.store_material(actor,project,"edge_manifest",manifest_payload, [],
                                        {"kind":"set_proposal","id":project},{"source":"assurance.set_propose"})
        assignments=self._edge_assignment(
            obligations, edges, actor,
            center=center, relation=relation, direction=direction,
        )
        assignment_full={"format":"assurance.coverage-assignment.v1","project":project,"obligations_ref":expected_ref,
                         "assignments":assignments,"edge_stream_digest":stream_digest}
        if len(canonical(assignment_full)) <= MAX_OBJECT_BYTES and len(edges) <= MAX_PAGE:
            assignment_payload=assignment_full
            assignment_dependencies=[self._object_ref(edge) for edge in edges]+[expected_ref]
        else:
            assignment_parts=[]
            for part_index,(obligation_id,contributors) in enumerate(sorted(assignments.items())):
                for chunk_index,start in enumerate(range(0,len(contributors),MAX_PAGE)):
                    chunk=contributors[start:start+MAX_PAGE]
                    part_payload={"format":"assurance.coverage-assignment-partition.v1","project":project,
                                  "obligations_ref":expected_ref,"obligation_id":obligation_id,"contributors":chunk,
                                  "edge_stream_digest":stream_digest,"part_index":part_index,"chunk_index":chunk_index}
                    part,_=self.store_material(actor,project,"coverage_assignment_partition",part_payload,
                                                chunk+[expected_ref],{ "kind":"set_proposal","id":project},
                                                {"source":"assurance.set_propose"})
                    assignment_parts.append({"id":part["id"],"digest":part["digest"],"payload_blob":part["body"]["payload_blob"],
                                             "obligation_id":obligation_id,"part_index":part_index,
                                             "chunk_index":chunk_index,"count":len(chunk)})
            assignment_payload={"format":"assurance.coverage-assignment.v1","project":project,"obligations_ref":expected_ref,
                                "partitions":assignment_parts,"edge_stream_digest":stream_digest,"count":sum(len(v) for v in assignments.values())}
            assignment_dependencies=[expected_ref]
        assignment,_=self.store_material(actor,project,"coverage_assignment",assignment_payload,assignment_dependencies,
                                        {"kind":"set_proposal","id":project},{"source":"assurance.set_propose"})
        missing=sorted(item for item,value in assignments.items() if not value)
        criteria=self._set_criteria_achieved(
            relation, edges, obligations, missing,
            all_edges_current=all(self._object_is_current(edge) for edge in edges),
            meaning_review=False, independent_synthesis=False,
            contract_digest=contract_digest,
        )
        partition_payload={"format":"assurance.partition-manifest.v1","project":project,"root":"pending",
                           "count":len(identities),"stream_digest":stream_digest,"max_leaf":MAX_PAGE,
                           "partitions":[{"index":index,"start":start,"end":min(start+MAX_PAGE,len(identities)),"count":len(identities[start:start+MAX_PAGE])}
                                         for index,start in enumerate(range(0,len(identities),MAX_PAGE))]}
        partition,_=self.store_material(actor,project,"partition_manifest",partition_payload,[],
                                        {"kind":"set_proposal","id":project},{"source":"assurance.set_propose"})
        normalized={"format":"assurance.set.v1","project":project,"center_ref":self._identity(center),"relation":relation,
                    "direction":direction,"scope_ref":self._identity(scope_ref),"relation_contract_digest":contract_digest,
                    "expected_obligations_ref":self._identity(expected_ref),
                    "selected_edge_manifest_ref":{"id":manifest["id"],"digest":manifest["digest"]},
                    "coverage_assignment_ref":{"id":assignment["id"],"digest":assignment["digest"]},
                    "required_evidence_refs":[self._identity(validate_typed_ref(ref,project=project)) for ref in body.get("required_evidence_refs",[])],
                    "criteria":criteria,"criteria_requirements":criteria_requirements,
                    "criteria_requirements_digest":digest(criteria_requirements),
                    "partition_manifest_ref":{"id":partition["id"],"digest":partition["digest"]}}
        logical_id=body.get("set_id") or "set:"+digest({key:normalized[key] for key in normalized if key not in {"format","project"}})
        self._expected_head(project,logical_id,expected_head)
        set_row=self._store_e2_object(actor,project,"set",logical_id,normalized)
        edge_leaves=[self._packet_marker(edge) for edge in edges]
        synthesis_markers=[f"set:{set_row['id']}@{set_row['digest']}:criterion:{key}" for key in criteria_requirements]
        packets=self._make_packets(actor,project,set_row,"relation_set",edge_leaves,["trace"],synthesis=True,
                                   required_coverage=synthesis_markers,synthesis_roles=["impact"])
        return {"set":set_row,"set_ref":self._object_ref(set_row),"packets":packets,
                "manifest":manifest,"assignment":assignment,"partition":partition,"criteria":criteria,
                "criteria_requirements":criteria_requirements,
                "criteria_requirements_digest":digest(criteria_requirements),
                "obligations":obligations,"missing_obligations":missing,"current":self._object_is_current(set_row)}

    def _packet_rows(self, project: str, subject: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=?",(subject,project))
        if row and row["kind"]=="packet":
            packet=self._decode_object(row);root_ref=packet["body"].get("root_ref")
            root=self._object_by_ref(root_ref,project)
            return root,[packet]
        root=self._decode_object(row) if row else None
        need(root is not None and root["kind"] in {"scope","obligations","profile","edge","set"},"not_found","Unknown assurance review subject")
        packets=[]
        for item in self.s.all("SELECT * FROM assurance_objects WHERE project=? AND kind='packet' ORDER BY logical_id,revision,id",(project,)):
            body=parse_json(item["body"],limit=MAX_OBJECT_BYTES)
            ref=body.get("root_ref") if isinstance(body,dict) else None
            if isinstance(ref,dict) and ref.get("object")==root["id"] and ref.get("object_digest")==root["digest"]:
                packets.append(self._decode_object(item))
        return root,packets

    def _cursor_decode(self, cursor: str | None) -> dict[str, Any] | None:
        if cursor is None:return None
        need(isinstance(cursor,str) and cursor,"invalid_cursor","Cursor is invalid")
        try:
            import base64
            value=parse_json(base64.urlsafe_b64decode(cursor.encode()+b"="*((4-len(cursor)%4)%4)),limit=4096)
        except Exception as exc:
            raise Fault("invalid_cursor","Cursor is malformed") from exc
        need(isinstance(value,dict) and set(value)=={"snapshot","offset"} and type(value["offset"]) is int and value["offset"]>=0,"invalid_cursor","Cursor shape is invalid")
        return value

    def _cursor_encode(self, value: dict[str, Any]) -> str:
        import base64
        return base64.urlsafe_b64encode(canonical(value)).decode().rstrip("=")

    def review_subject(self, actor, project: str, subject: str, cursor: str | None = None, limit: int = MAX_PAGE) -> dict[str, Any]:
        self._project(actor,project,read=True);number(limit,"limit",1,MAX_PAGE,integer=True)
        root,packets=self._packet_rows(project,subject)
        snapshot=digest([(item["id"],item["digest"]) for item in packets])
        state=self._cursor_decode(cursor);offset=0
        if state:
            need(state["snapshot"]==snapshot,"stale_cursor","Review packet cursor is stale",{"restart":True});offset=state["offset"]
        page=packets[offset:offset+limit];next_offset=offset+len(page)
        next_cursor=self._cursor_encode({"snapshot":snapshot,"offset":next_offset}) if next_offset<len(packets) else None
        return {"format":"daikibo.assurance-review.v1","project":project,"root":root,
                "packets":page,"total":len(packets),"offset":offset,"next_cursor":next_cursor,
                "snapshot":snapshot}

    def review_subject_context(self, actor, subject: str, role: str, proposal: Any = None):
        packet=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND kind='packet'",(subject,),True)
        body=parse_json(packet["body"],limit=MAX_OBJECT_BYTES)
        need(role in set(body.get("required_roles",[])),"invalid_role","Assurance packet does not require this review role")
        root=self._object_by_ref(body["root_ref"],packet["project"])
        empty={"format":"snapshot.v1","repos":{},"digest":digest({"repos":{}})}
        context={"assurance_packet":body,"root":root,"required_coverage":body.get("required_coverage",[]),
                 "review_kind":body.get("review_kind"),"review_role":role}
        return packet["project"],packet["digest"],empty,context,None

    def _root_packets(self, project: str, root: dict[str, Any]) -> list[dict[str, Any]]:
        _root,packets=self._packet_rows(project,root["id"])
        return packets

    def _review_requirements(self, project: str, roots: list[dict[str, Any]]) -> list[tuple[dict[str, Any],str]]:
        requirements=[];seen=set()
        for root in roots:
            for packet in self._root_packets(project,root):
                body=packet["body"]
                for role in body.get("required_roles",[]):
                    key=(packet["id"],role)
                    if key not in seen:
                        seen.add(key);requirements.append((packet,role))
        requirements.sort(key=lambda item:(item[0]["id"],item[1]))
        return requirements

    def _require_reviews(self, project: str, roots: list[dict[str, Any]], review_refs: Any) -> list[dict[str, Any]]:
        need(isinstance(review_refs,list) and review_refs,"missing_review","All assurance review packets require explicit receipts")
        requirements=self._review_requirements(project,roots)
        by_key={(packet["id"],role):(packet,role) for packet,role in requirements}
        supplied={};normalized=[]
        for raw in review_refs:
            if isinstance(raw,str):
                raise Fault("invalid_review", "Review reference must name its packet and role")
            need(isinstance(raw,dict),"invalid_review","Review reference must be an object")
            packet_id=raw.get("packet",raw.get("packet_id"));receipt_id=raw.get("id",raw.get("receipt"));role=raw.get("role")
            need(isinstance(packet_id,str) and isinstance(receipt_id,str) and isinstance(role,str),"invalid_review","Review reference lacks packet, receipt or role")
            key=(packet_id,role);need(key in by_key,"invalid_review","Review reference is not required by this subject",key)
            need(key not in supplied,"duplicate_review","Duplicate assurance review reference",key)
            packet,required_role=by_key[key]
            receipt=self.c.g.require_review(receipt_id,packet_id,packet["digest"],{required_role})
            covered=receipt.get("result",{}).get("covered",[])
            need(isinstance(covered,list),"review_coverage","Review did not return coverage markers")
            required=set(packet["body"].get("required_coverage",[]))
            need(required <= set(covered),"review_coverage","Review omitted required assurance markers",
                 sorted(required-set(covered)))
            supplied[key]=receipt
            normalized.append({"id":receipt_id,"packet":packet_id,"role":role,"receipt_digest":digest(receipt),
                               "covered":sorted(set(covered))})
        missing=sorted(set(by_key)-set(supplied))
        need(not missing,"missing_review","Assurance review packets are incomplete",missing)
        return normalized

    def _typed_dependencies(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        result=[]
        for path,ref in _walk_refs(row["body"]):
            # A v2 profile retains the previous canonical selection as an
            # immutable historical identity.  It is a CAS predecessor, not a
            # dependency that must be adopted again or traversed as a proof
            # DAG child; the current head check handles its exact identity.
            if (row["kind"] == "profile" and
                    path.endswith("previous_selection_ref")):
                continue
            if ref.get("kind")=="assurance_object":result.append(ref)
        return result

    def _manifest_edge_descriptors(self, project: str, descriptor: dict[str, Any],
                                   expected: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        material=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='material'",(descriptor.get("id"),project),True)
        need(material["digest"]==descriptor.get("digest"),"integrity_error","Edge manifest digest differs")
        envelope=parse_json(material["body"],limit=MAX_OBJECT_BYTES)
        payload=parse_json(self.s.blob_get(envelope["payload_blob"]),limit=MAX_OBJECT_BYTES)
        need(payload.get("format")=="assurance.edge-manifest.v1" and payload.get("project")==project,
             "integrity_error","Edge manifest format or project differs")
        if expected is not None:
            for key in ("center_ref", "relation", "direction", "scope_ref"):
                if key in expected:
                    actual = self._identity(payload.get(key)) if isinstance(payload.get(key), dict) else payload.get(key)
                    wanted = self._identity(expected[key]) if isinstance(expected[key], dict) else expected[key]
                    need(actual == wanted, "stale_set", "Edge manifest selector differs", key)
        descriptors=[]
        if isinstance(payload.get("edges"),list):
            descriptors.extend(payload["edges"])
        else:
            partitions=payload.get("partitions",[])
            need(isinstance(partitions,list),"integrity_error","Edge manifest partitions are malformed")
            for part in sorted(partitions,key=lambda value:value.get("index",-1)):
                need(isinstance(part,dict) and isinstance(part.get("payload_blob"),str),"integrity_error","Edge manifest partition descriptor is malformed")
                part_payload=parse_json(self.s.blob_get(part["payload_blob"]),limit=MAX_OBJECT_BYTES)
                need(isinstance(part_payload.get("edges"),list),"integrity_error","Edge manifest partition body is malformed")
                descriptors.extend(part_payload["edges"])
        need(len(descriptors)==payload.get("count"),"integrity_error","Edge manifest count differs")
        identities=[(item.get("id"),item.get("revision"),item.get("digest")) for item in descriptors]
        need(identities==sorted(identities) and len(set(identities))==len(identities),"integrity_error","Edge manifest ordering or duplicate differs")
        need(digest(identities)==payload.get("stream_digest"),"integrity_error","Edge manifest stream digest differs")
        return descriptors

    def _assignment_descriptors(self, project: str, descriptor: dict[str, Any],
                                obligations_ref: dict[str, Any], stream_digest: str) -> dict[str, list[dict[str, Any]]]:
        material=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='material'",
                            (descriptor.get("id"),project),True)
        need(material["digest"]==descriptor.get("digest"),"integrity_error","Coverage assignment digest differs")
        envelope=parse_json(material["body"],limit=MAX_OBJECT_BYTES)
        payload=parse_json(self.s.blob_get(envelope["payload_blob"]),limit=MAX_OBJECT_BYTES)
        need(payload.get("format")=="assurance.coverage-assignment.v1" and payload.get("project")==project,
             "integrity_error","Coverage assignment format or project differs")
        need(self._identity(payload.get("obligations_ref"))==self._identity(obligations_ref),
             "stale_set","Coverage assignment obligations differ")
        need(payload.get("edge_stream_digest")==stream_digest,"stale_set","Coverage assignment edge stream differs")
        result: dict[str, list[dict[str, Any]]] = {}
        if isinstance(payload.get("assignments"),dict):
            need(set(payload)<= {"format","project","obligations_ref","assignments","edge_stream_digest"},
                 "integrity_error","Coverage assignment has unknown fields")
            for obligation_id, contributors in payload["assignments"].items():
                need(isinstance(obligation_id,str) and isinstance(contributors,list),
                     "integrity_error","Coverage assignment entry is malformed")
                result[obligation_id]=[self._identity(validate_typed_ref(item,project=project,expected_kinds={"assurance_object"}))
                                       for item in contributors]
        else:
            partitions=payload.get("partitions")
            need(isinstance(partitions,list),"integrity_error","Coverage assignment partitions are malformed")
            for part in sorted(partitions,key=lambda item:(item.get("obligation_id",""),item.get("chunk_index",-1))):
                need(isinstance(part,dict) and isinstance(part.get("payload_blob"),str),
                     "integrity_error","Coverage assignment partition descriptor is malformed")
                child=parse_json(self.s.blob_get(part["payload_blob"]),limit=MAX_OBJECT_BYTES)
                need(child.get("format")=="assurance.coverage-assignment-partition.v1" and
                     child.get("project")==project and child.get("obligations_ref") is not None and
                     self._identity(child["obligations_ref"])==self._identity(obligations_ref) and
                     child.get("edge_stream_digest")==stream_digest and
                     child.get("obligation_id")==part.get("obligation_id") and
                     child.get("chunk_index")==part.get("chunk_index"),
                     "integrity_error","Coverage assignment partition identity differs")
                contributors=child.get("contributors")
                need(isinstance(contributors,list) and len(contributors)==part.get("count"),
                     "integrity_error","Coverage assignment partition count differs")
                values=result.setdefault(child["obligation_id"],[])
                values.extend(self._identity(validate_typed_ref(item,project=project,expected_kinds={"assurance_object"}))
                              for item in contributors)
            for values in result.values():
                values.sort(key=lambda item:(item["object"],item["object_digest"]))
        for obligation_id, values in result.items():
            identities=[(item["object"],item["object_digest"]) for item in values]
            need(len(identities)==len(set(identities)) and identities==sorted(identities),
                 "integrity_error","Coverage assignment ordering or duplicates differ")
        return result

    def _validate_partition_manifest(self, project: str, descriptor: dict[str, Any],
                                     count: int, stream_digest: str) -> None:
        material=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='material'",
                            (descriptor.get("id"),project),True)
        need(material["digest"]==descriptor.get("digest"),"integrity_error","Partition manifest digest differs")
        envelope=parse_json(material["body"],limit=MAX_OBJECT_BYTES)
        payload=parse_json(self.s.blob_get(envelope["payload_blob"]),limit=MAX_OBJECT_BYTES)
        need(payload.get("format")=="assurance.partition-manifest.v1" and payload.get("project")==project,
             "integrity_error","Partition manifest format or project differs")
        need(payload.get("count")==count and payload.get("stream_digest")==stream_digest and
             payload.get("max_leaf")==MAX_PAGE,"stale_set","Partition manifest denominator differs")
        partitions=payload.get("partitions")
        need(isinstance(partitions,list),"integrity_error","Partition manifest partitions are malformed")
        expected=[]
        for index,start in enumerate(range(0,count,MAX_PAGE)):
            expected.append({"index":index,"start":start,"end":min(start+MAX_PAGE,count),
                             "count":min(start+MAX_PAGE,count)-start})
        need(partitions==expected,"integrity_error","Partition manifest boundaries differ")

    def _proof_dag(self, project: str, roots: list[dict[str, Any]]) -> None:
        visiting=set();visited=set()
        def visit(row):
            key=row["id"]
            if key in visiting:raise Fault("proof_cycle","Assurance proof dependency graph contains a cycle",key)
            if key in visited:return
            visiting.add(key)
            for raw in self._typed_dependencies(row):
                dep=self._object_by_ref(raw,project,kinds={"scope","obligations","profile","edge","set"})
                need(dep["id"]!=row["id"],"proof_cycle","Assurance object supports itself",row["id"])
                visit(dep)
            visiting.remove(key);visited.add(key)
        for root in roots:visit(root)

    def _adoption_roots(self, project: str, root: dict[str, Any]) -> list[dict[str, Any]]:
        result=[root];seen={root["id"]}
        def add(ref):
            if not isinstance(ref,dict) or ref.get("kind")!="assurance_object":return
            dep=self._object_by_ref(ref,project,kinds={"scope","obligations","profile","edge","set"})
            if dep["id"] not in seen:seen.add(dep["id"]);result.append(dep)
        body=root["body"]
        for ref in self._typed_dependencies(root):add(ref)
        # A set's immutable manifest contains edge identities as a material
        # payload.  Expand them into the independent edge review roots.
        if root["kind"]=="set":
            descriptor=body.get("selected_edge_manifest_ref") or {}
            for item in self._manifest_edge_descriptors(project,descriptor):
                edge=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='edge'",(item.get("id"),project))
                if edge and edge["digest"]==item.get("digest"):
                    decoded=self._decode_object(edge)
                    if decoded["id"] not in seen:seen.add(decoded["id"]);result.append(decoded)
        return result

    def _adoption_gate(self, actor, project: str, root: dict[str, Any],
                       *, review_refs: list[dict[str, Any]] | None = None,
                       allow_unadopted_dependencies: bool = False) -> dict[str, Any]:
        kind = root["kind"]
        body = root["body"]
        dependencies: list[dict[str, Any]] = []
        failures: list[str] = []
        selection_gate: dict[str, Any] = {}
        head = self._head_event(project, root["logical_id"])
        # Only the first profile bootstrap may defer dependency heads.  A
        # withdrawn/re-adopted profile still has to traverse adopted current
        # scope and obligations.
        bootstrap = bool(allow_unadopted_dependencies or
                         (kind == "profile" and head is None))
        self._ensure_object_current(
            actor, project, root, require_self=False,
            allow_unadopted_dependencies=bootstrap,
        )
        if kind == "profile" and _is_canonical_profile(body.get("format")):
            selection_gate = self._profile_selection_gate(actor, project, root)
        if kind == "scope":
            need(not body.get("discovery_unknowns"), "unknown_scope", "Scope has unresolved discovery unknowns")
            for item in body.get("exclusion_proposals", []):
                need(item.get("authority_refs"), "unresolved_exclusion", "Scope exclusion lacks authority")
        elif kind == "obligations":
            dependencies.append(self._object_by_ref(body["scope_ref"], project, kinds={"scope"}))
        elif kind == "profile":
            dependencies.extend([
                self._object_by_ref(body["scope_ref"], project, kinds={"scope"}),
                self._object_by_ref(body["obligations_ref"], project, kinds={"obligations"}),
            ])
        elif kind == "edge":
            dependencies.append(self._object_by_ref(body["scope_ref"], project, kinds={"scope", "profile"}))
        elif kind == "set":
            dependencies.extend([
                self._object_by_ref(body["scope_ref"], project, kinds={"scope", "profile"}),
                self._object_by_ref(body["expected_obligations_ref"], project, kinds={"obligations"}),
            ])
            descriptor = body.get("selected_edge_manifest_ref") or {}
            for item in self._manifest_edge_descriptors(project, descriptor):
                row = self.s.one(
                    "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='edge'",
                    (item.get("id"), project), True,
                )
                if row["digest"] == item.get("digest"):
                    dependencies.append(self._decode_object(row))

            # ``criteria`` in the immutable object is the proposal-time
            # achieved snapshot.  Adoption recomputes the current facts and
            # records the review-derived criteria separately in the event;
            # caller input cannot claim a PASS by setting a boolean.
            obligations = dependencies[1]
            descriptors = self._manifest_edge_descriptors(project, descriptor, {
                "center_ref": body["center_ref"], "relation": body["relation"],
                "direction": body["direction"], "scope_ref": body["scope_ref"],
            })
            edges = []
            for item in descriptors:
                row = self.s.one(
                    "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='edge'",
                    (item.get("id"), project), True,
                )
                edge = self._decode_object(row)
                need(set(edge["body"].get("obligation_ids", [])) <=
                     {value.get("id") for value in obligations["body"].get("obligations", [])
                      if isinstance(value, dict)},
                     "stale_set", "Set edge contains an obligation outside the selected denominator", edge["id"])
                edges.append(edge)
            assignments = self._edge_assignment(
                obligations, edges, actor,
                center=body["center_ref"], relation=body["relation"],
                direction=body["direction"],
            )
            missing = sorted(item for item, value in assignments.items() if not value)
            criteria = self._set_criteria_achieved(
                body["relation"], edges, obligations, missing,
                all_edges_current=True,
                meaning_review=review_refs is not None,
                independent_synthesis=review_refs is not None,
                contract_digest=body.get("relation_contract_digest"),
            )
            requirements = self._set_requirements_for_body(body)
            for name in requirements:
                # Receipt completeness/independence is checked by
                # _require_reviews.  Before receipts arrive these two
                # criteria remain pending rather than being mistaken for a
                # mechanical failure; after receipts they are true in the
                # event gate below.
                if review_refs is None and name in {"meaning_review", "independent_synthesis"}:
                    continue
                need(criteria.get(name) is True, "set_incomplete",
                     "Relation-set criterion is not achieved", name)
        self._proof_dag(project, [root] + dependencies)
        gate = {"root": self._object_ref(root), "kind": kind,
                "dependencies": [self._object_ref(row) for row in dependencies],
                "failures": failures}
        if selection_gate:
            gate["selection"] = selection_gate
        if kind == "set":
            gate["criteria"] = criteria
            gate["criteria_requirements"] = requirements
            gate["criteria_requirements_digest"] = digest(requirements)
        return gate

    def adopt(self, actor, project: str, subject: str, expected_digest: str, expected_head: str | None,
              review_refs: list[dict[str, Any]]) -> dict[str, Any]:
        self._project(actor, project)
        need(isinstance(expected_digest, str) and _sha(expected_digest),
             "invalid_adoption", "Expected digest is invalid")
        events = []
        # Semantic checks, receipt binding and the head CAS are all performed
        # under one writer transaction.  A dependency update cannot land
        # between currentness evaluation and event publication.
        with self.s.transaction():
            row = self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=?", (subject, project))
            if row and row["kind"] == "packet":
                body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
                root = self._object_by_ref(body["root_ref"], project)
            else:
                need(row is not None, "not_found", "Assurance adoption subject is missing")
                root = self._decode_object(row)
            need(root["digest"] == expected_digest,
                 "stale_reference", "Assurance adoption subject digest differs")
            roots = self._adoption_roots(project, root)
            existing = self._head_event(project, root["logical_id"])
            bootstrap = root["kind"] == "profile" and existing is None
            gate = self._adoption_gate(
                actor, project, root,
                allow_unadopted_dependencies=bootstrap,
            )
            reviews = self._require_reviews(project, roots, review_refs)
            if (existing and existing["event_kind"] == "adopt" and
                    existing["subject_id"] == root["id"] and
                    existing["subject_digest"] == root["digest"]):
                gate = self._adoption_gate(actor, project, root, review_refs=reviews)
                return {"status": "adopted", "id": existing["id"], "event": existing,
                        "subject": self._object_ref(root), "idempotent": True, "gate": gate}
            self._expected_head(project, root["logical_id"], expected_head)
            # Dependencies are appended first; a profile bootstrap therefore
            # retains separate review/event bodies inside one atomic history
            # transition.
            ordered = sorted(
                [item for item in roots if item["id"] != root["id"]],
                key=lambda item: {"scope": 0, "obligations": 1, "profile": 2, "edge": 3, "set": 4}.get(item["kind"], 5),
            )
            for dependency in ordered:
                if self._object_is_current(dependency):
                    continue
                dependency_gate = self._adoption_gate(actor, project, dependency)
                current = self._head_event(project, dependency["logical_id"])
                need(current is None, "stale_head",
                     "Dependent assurance object has a conflicting head", dependency["id"])
                events.append(self._append_storage_event(
                    actor, project, dependency["logical_id"], dependency["id"], dependency["digest"],
                    "adopt", None,
                    {"review_refs": reviews, "bootstrap_root": root["id"], "machine_gate": dependency_gate},
                ))
            # Re-evaluate after dependency heads have been appended.  For a
            # set this also proves the current membership and review criteria
            # immediately before its own head is advanced.
            gate = self._adoption_gate(actor, project, root, review_refs=reviews)
            events.append(self._append_storage_event(
                actor, project, root["logical_id"], root["id"], root["digest"],
                "adopt", expected_head,
                {"review_refs": reviews, "machine_gate": gate,
                 "registry_digest": root["body"].get(
                     "required_relation_contract_digest",
                     root["body"].get("relation_contract_digest", REGISTRY_DIGEST),
                 ),
                 **({"selection": gate["selection"]} if "selection" in gate else {})},
            ))
        return {"status":"adopted","id":events[-1]["id"],"event":events[-1],"events":events,
                "subject":self._object_ref(root),"idempotent":False,"gate":gate}

    def withdraw_propose(self, actor, project: str, subject: str, reason: str,
                         authority_refs: list[dict[str, Any]], expected_head: str | None = None) -> dict[str, Any]:
        self._project(actor,project)
        row=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=?",(subject,project))
        need(row is not None and row["kind"] in {"scope","obligations","profile","edge","set"},
             "not_found","Assurance withdrawal subject is missing",subject)
        root=self._decode_object(row)
        need(self._object_is_current(root),"stale_reference","Only an adopted current subject may be withdrawn")
        self._expected_head(project,root["logical_id"],expected_head)
        need(isinstance(reason,str) and reason,"invalid_withdrawal","Withdrawal reason is required")
        authorities=self._validate_authority_refs(actor, project,authority_refs)
        body={"format":"assurance.withdrawal.v1","project":project,"subject_ref":self._object_ref(root),"reason":reason,"authority_refs":authorities}
        packet=self._store_e2_object(actor,project,"packet","withdrawal:"+root["id"],
                                     {"format":"assurance.review-packet.v1","project":project,"root_ref":self._object_ref(root),
                                      "review_kind":"withdrawal","partition":{"kind":"leaf","index":0,"start":0,"end":1,"count":1,"total":1,"stream_digest":digest([reason])},
                                      "leaf_manifest":["withdrawal:"+root["id"]+"@"+root["digest"]],"required_coverage":["withdrawal:"+root["id"]+"@"+root["digest"]],"required_roles":["impact"],"withdrawal":body})
        return {"subject":self._object_ref(root),"packet":packet,"expected_head":expected_head,"withdrawal":body}

    def evaluate_stage(self, actor, project: str, program: str, stage: str,
                       task: dict[str, Any] | None = None,
                       delivery: dict[str, Any] | None = None,
                       checkpoint: str | None = None,
                       proposed_breakdown: str | None = None,
                       local_execution: str | None = None) -> dict[str, Any]:
        """Read the common Unit 3 evaluator through the Assurance boundary.

        The import is intentionally lazy: ``assurance_stage`` consumes the
        profile constants and the E1 resolver, while Assurance owns the
        profile storage itself.  Keeping this thin wrapper avoids a second
        evaluator and makes the public call site easy to reuse from later
        writer transactions.
        """
        from .assurance_stage import evaluate_stage
        return evaluate_stage(
            self.c, actor, project=project, program=program, stage=stage,
            task=task, delivery=delivery, checkpoint=checkpoint,
            proposed_breakdown=proposed_breakdown, local_execution=local_execution,
        )

    def report(self, actor, project: str, program: str | None = None, stage: str | None = None,
               cursor: str | None = None, limit: int = MAX_PAGE,
               checkpoint: str | None = None, task: dict[str, Any] | None = None,
               delivery: dict[str, Any] | None = None,
               proposed_breakdown: str | None = None,
               local_execution: str | None = None) -> dict[str, Any]:
        # A stage report is a read-only view over the same evaluator exposed
        # above.  The legacy object report remains byte-compatible when no
        # stage/context selector is supplied.
        if stage is not None and program is not None:
            from .assurance_stage import report_stage
            return report_stage(
                self.c, actor, project=project, program=program, stage=stage,
                checkpoint=checkpoint, task=task, delivery=delivery,
                proposed_breakdown=proposed_breakdown, local_execution=local_execution,
                cursor=cursor, limit=limit,
            )
        # A legacy project-wide object report may still carry its old stage
        # filter without a program.  It is explicitly not a Unit 3 evaluator
        # result; a stage report with selectors requires the canonical program
        # so the evaluator cannot silently choose a profile.
        need((stage is None or program is None) and checkpoint is None and task is None and delivery is None and
             proposed_breakdown is None and local_execution is None,
             "invalid_stage_context", "Stage selectors require a stage")
        self._project(actor,project,read=True);number(limit,"limit",1,MAX_PAGE,integer=True)
        selection = self.selected_profile(actor, project, program) if program is not None else None
        rows=[]
        for row in self.s.all("SELECT * FROM assurance_objects WHERE project=? AND kind IN ('scope','obligations','profile','edge','set') ORDER BY kind,logical_id,revision,id",(project,)):
            decoded=self._decode_object(row)
            newer=self.s.one("SELECT id FROM assurance_objects WHERE project=? AND kind=? AND logical_id=? AND revision>? ORDER BY revision DESC LIMIT 1",(project,row["kind"],row["logical_id"],row["revision"]))
            if newer:continue
            body=decoded["body"]
            if program is not None and body.get("program")!=program:continue
            if stage is not None and stage not in (body.get("stage_rules") or {}):continue
            adopted = self._object_is_current(decoded)
            current = False
            status = "unverified"
            if adopted:
                try:
                    self._ensure_object_current(actor, project, decoded, require_self=True)
                    current = True
                    status = "current"
                except Fault as exc:
                    # A retained adopted event remains history.  Its
                    # semantic status is derived from the failed dependency,
                    # so missing/altered external material is not shown as a
                    # current proof merely because the local head matches.
                    status = "stale" if exc.code in {
                        "stale_reference", "stale_set", "stale_evidence", "set_incomplete",
                    } else "unknown"
            elif self._head_event(project, row["logical_id"]):
                status = "stale"
            rows.append({"subject":self._object_ref(decoded),"kind":row["kind"],"status":status,
                         "current":current,"packets":len(self._root_packets(project,decoded))})
        snapshot=digest({"items": rows, "selection": selection});state=self._cursor_decode(cursor);offset=0
        if state:
            need(state["snapshot"]==snapshot,"stale_cursor","Assurance report cursor is stale",{"restart":True});offset=state["offset"]
        page=rows[offset:offset+limit];next_offset=offset+len(page)
        categories={name:sum(1 for row in rows if row["status"]==name) for name in ("missing","stale","unverified","unknown","failed","current")}
        return {"format":"daikibo.assurance-report.v1","project":project,"program":program,"stage":stage,
                "totals":{"all":len(rows),**categories},"items":page,"offset":offset,
                "next_cursor":self._cursor_encode({"snapshot":snapshot,"offset":next_offset}) if next_offset<len(rows) else None,
                "snapshot":snapshot, "selection": selection,
                "stage_evaluator": False}

    def contains(self, actor, project: str, container: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
        """Read-only strict membership check for the finite container pairs."""
        self._project(actor, project, read=True)
        parent = validate_typed_ref(container, project=project)
        child = validate_typed_ref(member, project=project)
        pk, ck = semantic_kind(parent), semantic_kind(child)
        result = {"format": "daikibo.assurance-membership.v1", "container": parent, "member": child,
                  "contains": False, "state": "unsupported_membership", "evidence": []}
        def identity(value: dict[str, Any]) -> dict[str, Any]:
            return {key: item for key, item in value.items() if key != "identity_digest"}

        def trace_result(value: dict[str, Any]) -> dict[str, Any]:
            need(value.get("kind") == "traceability_ref", "invalid_membership", "Traceability membership requires a wrapped ref")
            return self._trace_refs.resolve(actor, project, value["locator"], require_current=False)

        def population_row(value: dict[str, Any]) -> dict[str, Any]:
            need(value.get("kind") == "population", "invalid_membership", "Population container is required")
            row = self.s.one("SELECT * FROM traceability_revisions WHERE id=? AND project=?", (value["revision"], project))
            need(row is not None, "unknown_membership", "Traceability population is missing", value["revision"])
            need(row["digest"] == value["revision_digest"] and row["population_digest"] == value["population_digest"],
                 "stale_membership", "Traceability population digest differs")
            body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
            need(digest(body) == row["digest"], "integrity_error", "Traceability population body differs")
            return row

        def population_item(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            pop = value["population"]
            population_row(pop)
            row = self.s.one("SELECT * FROM traceability_items WHERE revision=? AND id=? AND project=?",
                             (pop["revision"], value["item"], project))
            need(row is not None, "unknown_membership", "Traceability population item is missing", value["item"])
            need(row["digest"] == value["item_digest"], "stale_membership", "Traceability item digest differs")
            body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
            need(digest(body) == row["digest"], "integrity_error", "Traceability item body differs")
            return row, body, pop

        if pk == "test_plan" and ck == "test_plan_check":
            same = identity(parent) == identity(child.get("plan", {}))
            result.update(contains=same, state="verified" if same else "false", evidence=["same_pinned_plan"] if same else [])
        elif pk == "delivery_snapshot" and ck == "delivery_check":
            try:
                same = identity(parent) == identity(child.get("delivery", {}))
                need(same, "false_membership", "Delivery check belongs to another pinned snapshot")
                delivery = self._resolve_locator(actor, parent, current=False)
                payload = delivery.get("payload") or {}
                checks = payload.get("checks", []) if isinstance(payload, dict) else []
                matches = [item for item in checks if isinstance(item, dict) and item.get("id") == child.get("check_id")]
                need(len(matches) == 1 and digest(matches[0]) == child.get("check_digest"),
                     "false_membership", "Delivery check is not retained by the pinned snapshot")
                result.update(contains=True, state="verified", evidence=["resolved_pinned_delivery_check"])
            except Fault as exc:
                result.update(state="unknown" if exc.code not in {"false_membership"} else "false",
                              evidence=[exc.code])
        elif ck == "output_artifact" and pk in {"delivery_snapshot", "actual_delivery_commit"}:
            try:
                output_resolution = self._resolve_locator(actor, child, current=False)
                output = output_resolution.get("content") or {}
                if pk == "delivery_snapshot":
                    need(self._delivery_identity(child.get("delivery", {})) == self._delivery_identity(parent),
                         "false_membership", "Output belongs to another Delivery snapshot")
                else:
                    actual = self._resolve_locator(actor, parent, current=False)
                    actual_payload = actual.get("payload") or {}
                    delivery_ref = actual_payload.get("delivery_snapshot_ref")
                    need(self._delivery_identity(child.get("delivery", {})) == self._delivery_identity(delivery_ref) and
                         output.get("repo") == parent.get("repository"),
                         "false_membership", "Output is not in the actual Delivery bundle")
                result.update(contains=True, state="verified", evidence=["delivery_build_output"],
                              membership="delivery_build_output", git_tree_inclusion=False)
            except Fault as exc:
                result.update(state="unknown" if exc.code != "false_membership" else "false",
                              evidence=[exc.code])
        elif pk == "assurance_object" and ck == "assurance_object":
            result.update(contains=parent.get("object") == child.get("object") and parent.get("object_digest") == child.get("object_digest"),
                          state="verified" if parent.get("object") == child.get("object") else "false")
        elif pk == "source" and ck == "source_span":
            try:
                loc = child["locator"]
                source = self.s.one("SELECT * FROM sources WHERE id=? AND project=?", (parent["source"], project))
                need(source is not None and source["blob"] == parent["blob_digest"], "stale_membership", "Source identity differs")
                span = trace_result(child)
                content = span.get("content") or {}
                same = (loc.get("source_id") == parent["source"] and loc.get("blob_digest") == parent["blob_digest"] and
                        content.get("blob") == parent["blob_digest"] and 0 <= content.get("byte_start", -1) <= content.get("byte_end", -1) <= len(self.s.blob_get(parent["blob_digest"])))
                result.update(contains=same, state="verified" if same else "false",
                              evidence=["resolved_source_container_and_range"] if same else [])
            except Fault as exc:
                result.update(state="unknown", evidence=[exc.code])
        elif pk == "source_span" and ck == "source_span":
            try:
                outer = trace_result(parent); inner = trace_result(child)
                po, co = outer.get("content") or {}, inner.get("content") or {}
                pl, cl = parent["locator"], child["locator"]
                same = (pl.get("source_id") == cl.get("source_id") and pl.get("blob_digest") == cl.get("blob_digest") and
                        po.get("byte_start") <= co.get("byte_start") and co.get("byte_end") <= po.get("byte_end"))
                result.update(contains=same, state="verified" if same else "false",
                              evidence=["resolved_source_ranges"] if same else [])
            except Fault as exc:
                result.update(state="unknown", evidence=[exc.code])
        elif pk == "population" and ck == "population_item":
            try:
                _, _, child_pop = population_item(child)
                same = identity(parent) == identity(child_pop)
                result.update(contains=same, state="verified" if same else "false",
                              evidence=["resolved_population_foreign_key"] if same else [])
            except Fault as exc:
                result.update(state="unknown", evidence=[exc.code])
        elif pk == "population_item" and ck == "population_item":
            try:
                parent_row, parent_body, parent_pop = population_item(parent)
                child_row, _, child_pop = population_item(child)
                same_population = identity(parent_pop) == identity(child_pop)
                members = parent_body.get("atom_ids") or parent_body.get("leaf_ids") or parent_body.get("members") or parent_body.get("item_ids")
                need(parent_row["item_kind"] == "group" and isinstance(members, list) and
                     all(isinstance(item, str) and item for item in members), "unsupported_membership", "Population group membership is not retained")
                need(len(members) == len(set(members)), "integrity_error", "Population group membership contains duplicates")
                leaves = self.s.all("SELECT id,leaf,item_kind FROM traceability_items WHERE revision=? AND id IN (%s)" % ",".join("?" * len(members)),
                                    tuple([parent_pop["revision"], *members])) if members else []
                leaf_map = {row["id"]: row for row in leaves}
                need(set(leaf_map) == set(members) and all(row["leaf"] == 1 for row in leaves),
                     "integrity_error", "Population group leaf membership is incomplete")
                same = same_population and child_row["id"] in set(members) and child_row["leaf"] == 1
                result.update(contains=same, state="verified" if same else "false",
                              evidence=["resolved_population_group_membership"] if same else [])
            except Fault as exc:
                result.update(state="unknown", evidence=[exc.code])
        elif pk == "artifact" and ck == "artifact_ac":
            try:
                loc = child["locator"]
                need(loc["artifact"] == parent["artifact"] and loc["revision"] == parent["revision"] and
                     loc["body_digest"] == parent["body_digest"], "stale_membership", "Artifact acceptance container differs")
                trace_result(child)
                result.update(contains=True, state="verified", evidence=["resolved_artifact_acceptance_pointer"])
            except Fault as exc:
                result.update(state="unknown", evidence=[exc.code])
        elif pk == "candidate" and ck == "candidate_symbol":
            try:
                # The parent is the generic E2 identity.  Resolve it first so
                # membership cannot become a checksum-only shortcut around
                # implementation provenance and historical Task definitions.
                self._resolve_locator(actor, parent, current=False)
                loc = child["locator"]
                same = all(loc.get(left) == parent.get(right) for left, right in (
                    ("candidate", "candidate"), ("task", "task"), ("task_revision", "task_revision"),
                    ("candidate_digest", "candidate_digest"), ("snapshot_digest", "snapshot_digest")))
                need(same, "stale_membership", "Candidate symbol belongs to another candidate identity")
                trace_result(child)
                result.update(contains=True, state="verified", evidence=["resolved_candidate_symbol_membership"])
            except Fault as exc:
                result.update(state="unknown", evidence=[exc.code])
        elif pk == "actual_delivery_commit" and ck in {"git_file", "git_symbol"}:
            try:
                parent_resolution = self._resolve_locator(actor, parent, current=False)
                payload = parent_resolution.get("payload") or {}
                locator = child.get("locator", {})
                need(locator.get("repository") == parent.get("repository") and
                     locator.get("object_format") == parent.get("object_format") and
                     locator.get("commit") == parent.get("commit"),
                     "false_membership", "Git reference belongs to another delivered commit")
                trace_result(child)
                manifest = validate_git_material_payload(
                    self.s, payload,
                    (self._resolve_locator(actor, parent["delivery"], current=False).get("payload") or {}).get("snapshot"))
                entries = {item["path"]: item for item in manifest["entry_manifest"] if item["kind"] != "tree"}
                entry = entries.get(locator.get("path"))
                need(entry is not None and entry["mode"] == locator.get("mode") and
                     entry["oid"] == locator.get("blob_oid"),
                     "false_membership", "Git reference is not in the actual delivery tree")
                result.update(contains=True, state="verified", evidence=["resolved_actual_delivery_tree_membership"])
            except Fault as exc:
                result.update(state="unknown" if exc.code not in {"false_membership"} else "false",
                              evidence=[exc.code])
        elif parent.get("kind") == "traceability_ref" and child.get("kind") == "traceability_ref":
            try:
                self.resolve_pinned(actor, parent); self.resolve_pinned(actor, child)
                p, c = parent["locator"], child["locator"]
                same_source = p.get("source_id") == c.get("source_id") and p.get("blob_digest") == c.get("blob_digest")
                contained = same_source and p.get("byte_start", 0) <= c.get("byte_start", 0) and c.get("byte_end", 0) <= p.get("byte_end", 0)
                result.update(contains=contained, state="verified" if contained else "false", evidence=["resolved_source_ranges"] if contained else [])
            except Fault as exc:
                result.update(state="unknown", evidence=[exc.code])
        return result

    def _append_storage_event(self, actor, project: str, logical_id: str, subject_id: str,
                              subject_digest: str, event_kind: str, expected_head: str | None,
                              body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append an E1 chain record.  It carries no review/adoption decision."""
        self._project(actor, project)
        need(event_kind in {"adopt", "withdraw", "supersede"}, "invalid_event", "Unknown assurance event kind")
        need(isinstance(subject_digest, str) and len(subject_digest) == 64 and _sha(subject_digest),
             "invalid_event", "Subject digest is not a SHA-256")
        body = {} if body is None else body
        need(isinstance(body, dict), "invalid_event", "Event body must be an object")
        encoded = canonical(body); need(len(encoded) <= MAX_OBJECT_BYTES, "event_too_large", "Event body is too large")
        with self.s.transaction():
            subject = self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=?", (subject_id, project), True)
            need(subject["digest"] == subject_digest, "integrity_error", "Event subject digest differs")
            need(subject["logical_id"] == logical_id, "invalid_event", "Event subject logical identity differs")
            current = self.s.one("SELECT head_event FROM assurance_heads WHERE project=? AND logical_id=?", (project, logical_id))
            current_head = current["head_event"] if current else None
            need(expected_head == current_head, "stale_head", "Assurance head compare-and-swap failed",
                 {"expected": expected_head, "actual": current_head})
            previous = current_head
            event_id = uid("AEVT")
            self.s.execute("INSERT INTO assurance_events(id,project,subject_id,subject_digest,event_kind,expected_head,previous,body,created) VALUES(?,?,?,?,?,?,?,?,?)",
                           (event_id, project, subject_id, subject_digest, event_kind, expected_head, previous, encoded.decode(), timestamp()))
            if current:
                self.s.execute("UPDATE assurance_heads SET head_event=? WHERE project=? AND logical_id=?", (event_id, project, logical_id))
            else:
                self.s.execute("INSERT INTO assurance_heads(project,logical_id,head_event) VALUES(?,?,?)", (project, logical_id, event_id))
        return dict(self.s.one("SELECT * FROM assurance_events WHERE id=?", (event_id,), True))

    def object_get(self, actor, project: str, object_id: str) -> dict[str, Any]:
        self._project(actor, project, read=True)
        row = self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=?", (object_id, project), True)
        return self._decode_object(row)

    def object_list(self, actor, project: str, kind: str | None = None,
                    limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self._project(actor, project, read=True)
        number(limit, "limit", 1, MAX_PAGE, integer=True); number(offset, "offset", 0, 10**12, integer=True)
        if kind is not None: need(kind in OBJECT_KINDS, "invalid_object_kind", "Unknown assurance object kind")
        where = "project=?"; args: list[Any] = [project]
        if kind is not None: where += " AND kind=?"; args.append(kind)
        total = self.s.one(f"SELECT count(*) AS n FROM assurance_objects WHERE {where}", tuple(args))["n"]
        rows = self.s.all(f"SELECT * FROM assurance_objects WHERE {where} ORDER BY kind,logical_id,revision,id LIMIT ? OFFSET ?",
                          tuple(args + [limit, offset]))
        return {"objects": [self._decode_object(row) for row in rows], "total": total,
                "offset": offset, "next_offset": offset + limit if offset + limit < total else None,
                "snapshot": self._stream_digest(project, kind)}

    def _stream_digest(self, project: str, kind: str | None = None) -> str:
        where = "project=?"; args: list[Any] = [project]
        if kind is not None: where += " AND kind=?"; args.append(kind)
        rows = self.s.all(f"SELECT id,kind,logical_id,revision,digest FROM assurance_objects WHERE {where} ORDER BY kind,logical_id,revision,id", tuple(args))
        return digest(rows)

    def refs(self, actor, project: str, ref_kind: str | None = None, ref_id: str | None = None,
             limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self._project(actor, project, read=True)
        number(limit, "limit", 1, MAX_PAGE, integer=True); number(offset, "offset", 0, 10**12, integer=True)
        where = "o.project=?"; args: list[Any] = [project]
        if ref_kind is not None: where += " AND r.ref_kind=?"; args.append(ref_kind)
        if ref_id is not None: where += " AND r.ref_id=?"; args.append(ref_id)
        total = self.s.one(f"SELECT count(*) AS n FROM assurance_refs r JOIN assurance_objects o ON o.id=r.object_id WHERE {where}", tuple(args))["n"]
        rows = self.s.all(f"SELECT r.*,o.kind AS object_kind,o.logical_id,o.revision AS object_revision,o.digest AS object_digest FROM assurance_refs r JOIN assurance_objects o ON o.id=r.object_id WHERE {where} ORDER BY r.ref_kind,r.ref_id,r.ref_revision,r.object_id,r.ordinal LIMIT ? OFFSET ?", tuple(args + [limit, offset]))
        return {"refs": rows, "total": total, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
                "snapshot": digest(rows if offset == 0 else self._stream_digest(project))}

    def history(self, actor, project: str, logical_id: str | None = None,
                limit: int = 100, offset: int = 0, cursor: str | None = None) -> dict[str, Any]:
        self._project(actor, project, read=True)
        number(limit, "limit", 1, MAX_PAGE, integer=True); number(offset, "offset", 0, 10**12, integer=True)
        cursor_state = self._cursor_decode(cursor) if cursor is not None else None
        if cursor_state is not None:
            offset = cursor_state["offset"]
        where = "project=?"; args: list[Any] = [project]
        if logical_id is not None: where += " AND logical_id=?"; args.append(logical_id)
        objects = self.s.all(f"SELECT * FROM assurance_objects WHERE {where} ORDER BY logical_id,revision,id LIMIT ? OFFSET ?", tuple(args + [limit, offset]))
        total = self.s.one(f"SELECT count(*) AS n FROM assurance_objects WHERE {where}", tuple(args))["n"]
        if logical_id is None:
            events = self.s.all("SELECT * FROM assurance_events WHERE project=? ORDER BY created,id LIMIT ? OFFSET ?", (project, limit, offset))
        else:
            events = self.s.all("SELECT e.* FROM assurance_events e JOIN assurance_objects o ON o.id=e.subject_id WHERE e.project=? AND o.logical_id=? ORDER BY e.created,e.id LIMIT ? OFFSET ?",
                                (project, logical_id, limit, offset))
        for row in objects: row["body"] = _json_field(row)
        for row in events: row["body"] = _json_field(row)
        heads = self.s.all("SELECT project,logical_id,head_event FROM assurance_heads WHERE project=? ORDER BY logical_id", (project,))
        receipts_where="project=?";receipt_args=[project]
        if logical_id is not None:
            receipts_where += " AND subject IN (SELECT id FROM assurance_objects WHERE project=? AND logical_id=?)"
            receipt_args.extend([project,logical_id])
        receipts=self.s.all(f"SELECT id,run,subject,role,binding,created FROM receipts WHERE {receipts_where} ORDER BY created,id LIMIT ? OFFSET ?",
                            tuple(receipt_args+[limit,offset]))
        event_hash=hashlib.sha256()
        for event in self.s.all("SELECT id,subject_id,subject_digest,event_kind,previous FROM assurance_events WHERE project=? ORDER BY created,id",(project,)):
            event_hash.update(canonical(event));event_hash.update(b"\n")
        current_snapshot=digest({"objects":self._stream_digest(project),"heads":heads,"events":event_hash.hexdigest()})
        if cursor_state is not None:
            need(cursor_state["snapshot"]==current_snapshot,"stale_cursor","Assurance history cursor is stale",{"restart":True})
        return {"format": "daikibo.assurance-history.v1", "objects": objects, "events": events,
                "heads": heads, "receipts": receipts, "total_objects": total, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
                "snapshot": current_snapshot,
                "next_cursor": self._cursor_encode({"snapshot":current_snapshot,"offset":offset+limit}) if offset+limit<total else None}

    def _execution_cas_refs(self, body: dict[str, Any]) -> list[dict[str, str]]:
        """Verify every explicitly named CAS leaf in an observed receipt.

        Receipt fields are immutable observations.  A missing stdout/report or
        build input/output blob therefore invalidates the observation instead
        of being treated as an optional display field.  The recursive walk is
        limited to the established ``blob``/``*_blob`` wire names so ordinary
        strings such as run IDs are never guessed to be CAS references.
        """
        refs: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(value: Any, path: str) -> None:
            need(_sha(value), "integrity_error", "Observed execution CAS reference is malformed", path)
            if value not in seen:
                self.s.blob_get(value)
                seen.add(value)
                refs.append({"purpose": path, "digest": value})

        def walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    child_path = f"{path}.{key}"
                    if key == "blob" or key.endswith("_blob"):
                        add(child, child_path)
                    walk(child, child_path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]")

        # ``input_digest`` is the prompt CAS identity but intentionally keeps
        # its historical field name, so it is checked explicitly.
        add(body.get("input_digest"), "receipt.input_digest")
        for key in ("stdout_blob", "stderr_blob"):
            add(body.get(key), f"receipt.{key}")
        walk(body.get("result"), "receipt.result")
        walk(body.get("partial_work"), "receipt.partial_work")
        walk(body.get("work_product"), "receipt.work_product")
        walk(body.get("recovery_artifacts"), "receipt.recovery_artifacts")
        return refs

    def _execution_material(self, actor, project: str, ref: dict[str, Any],
                            run_row: dict[str, Any], run_body: dict[str, Any],
                            observed: dict[str, Any], *, current: bool) -> dict[str, Any]:
        """Resolve and verify one controller-created execution material pin."""
        pin = run_body.get("verification_material")
        receipt_pin = observed.get("verification_material")
        if not isinstance(pin, dict) or set(pin) != {"id", "digest"}:
            raise Fault("legacy_unverified", "Observed execution has no immutable verification material", ref["receipt"])
        # Keep this early check so old callers retain the explicit integrity
        # diagnostic before the shared relation validator reads any material.
        need(receipt_pin == pin, "integrity_error", "Run and receipt verification material pins differ")
        coordinator = getattr(getattr(self.c, "rt", None), "verification_materials", None)
        need(coordinator is not None, "legacy_unverified", "Verification material read boundary is unavailable")
        material_row = coordinator.validate_stored_pin(actor, project, pin, run_id=run_row["id"])
        material_body = material_row.get("body")
        if isinstance(material_body, str):
            material_body = parse_json(material_body, limit=MAX_OBJECT_BYTES)

        def resolve_artifact(artifact_ref):
            return self._artifact_body(project, artifact_ref)

        relation = validate_execution_material_relation(
            project=project, ref=ref, run_row=run_row, run_body=run_body,
            observed=observed, material_row=material_row, material_body=material_body,
            blob_store=self.s, resolve_definition=lambda definition: self._resolve_locator(
                actor, definition, current=False),
            resolve_candidate=lambda candidate: resolve_candidate_identity(
                _material_identity(candidate), self._candidate_context),
            resolve_artifact=resolve_artifact,
        )
        relation["content_refs"] = self._execution_cas_refs(observed)
        return relation

    def _observed_execution_current(self, actor, project: str, ref: dict[str, Any],
                                    run_row: dict[str, Any], material: dict[str, Any]) -> bool:
        """Compute live currentness separately from historical identity."""
        if run_row.get("status") != "finished" or run_row.get("binding") != ref["run_binding"]:
            return False
        payload = material["payload"]
        subject = payload["execution_subject"]
        try:
            self._resolve_locator(actor, material["definition_ref"], current=True)
            if material.get("candidate_ref") is not None:
                self._resolve_locator(actor, material["candidate_ref"], current=True)
            if subject["kind"] == "task":
                task = self.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (subject["id"], project))
                if task is None or task.get("status") == "cancelled":
                    return False
                if self.c.g.task_binding(subject["id"], ensure_policy=False) != ref["run_binding"]:
                    return False
            expected_artifacts = self.execution_test_artifact_refs(actor, project, material["definition_ref"])
            if [self._identity(item) for item in expected_artifacts] != material["test_artifact_refs"]:
                return False
        except Fault as exc:
            if exc.code in {"stale_reference", "unresolved_reference", "missing_evidence",
                            "legacy_unverified", "not_found"}:
                return False
            raise
        return True

    def _resolve_locator(self, actor, ref: dict[str, Any], *, current: bool) -> dict[str, Any]:
        locator = ref.get("locator")
        if ref.get("kind") == "traceability_ref":
            result = self._trace_refs.resolve(actor, ref["project"], locator, require_current=current)
            return {"mode": "traceability", "result": result,
                    "dependencies": result.get("dependencies", []),
                    "content_refs": result.get("content", {}),
                    "current": result.get("current", True)}
        if ref.get("kind") == "artifact":
            row = self.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (ref["artifact"], ref["project"]))
            need(row is not None, "unresolved_reference", "Artifact is missing", ref["artifact"])
            revision = self.s.one("SELECT * FROM revisions WHERE artifact=? AND revision=?", (ref["artifact"], ref["revision"]))
            need(revision is not None, "unresolved_reference", "Artifact revision is missing", ref["revision"])
            need(revision.get("artifact") == row.get("id") and
                 revision.get("revision") == ref["revision"],
                 "integrity_error", "Artifact revision identity differs", ref["revision"])
            body = parse_json(revision["body"], limit=MAX_OBJECT_BYTES)
            need(revision["digest"] == ref["body_digest"] == digest(body), "integrity_error", "Artifact revision digest differs")
            need(isinstance(body, dict), "integrity_error", "Artifact body is malformed")
            # ``current=False`` is the immutable typed-reference read path.
            # It proves project, revision identity, body shape and digest, but
            # must not turn the mutable artifact status into a historical
            # existence condition.  Adoption/currentness remains explicit in
            # the current path below; a withdrawn or superseded row still
            # retains a valid historical body for owner projections.
            is_current = (row["revision"] == ref["revision"] and
                          row["digest"] == ref["body_digest"] and
                          row["status"] == "accepted")
            if current and not is_current:
                raise Fault("stale_reference", "Artifact reference is not current", ref["artifact"])
            return {"mode": "artifact", "content": {"artifact": row["id"], "revision": revision["revision"], "body": body}, "current": is_current}
        if ref.get("kind") == "source":
            row = self.s.one("SELECT * FROM sources WHERE id=? AND project=?", (ref["source"], ref["project"]))
            need(row is not None, "unresolved_reference", "Source is missing", ref["source"])
            need(row["blob"] == ref["blob_digest"], "integrity_error", "Source digest differs")
            raw = self.s.blob_get(ref["blob_digest"])
            need(digest(raw) == ref["blob_digest"], "integrity_error", "Source CAS digest differs")
            return {"mode": "source", "content": {"source": row["id"], "blob_digest": row["blob"], "characters": row["characters"]}, "current": True}
        if ref.get("kind") == "traceability_ref" and ref.get("semantic_kind") == "artifact_ac":
            # The wrapper resolver already validates the artifact/revision and
            # acceptance pointer.  Keep the resolved member identity explicit
            # so a relation cannot use an AC label as a free-form endpoint.
            result = self._trace_refs.resolve(actor, ref["project"], ref["locator"], require_current=current)
            return {"mode": "artifact_ac", "result": result,
                    "dependencies": result.get("dependencies", []),
                    "content_refs": result.get("content", {}),
                    "current": result.get("current", True)}
        if ref.get("kind") == "candidate":
            # The E2 generic candidate wire has no symbol/path fields.  Strip
            # the resolver's derived identity_digest before handing it to the
            # exact shared core; adding optional fields there would weaken the
            # generic contract and blur it with candidate_symbol.
            candidate_ref = {key: ref[key] for key in (
                "kind", "project", "candidate", "task", "task_revision",
                "candidate_digest", "snapshot_digest")}
            identity = resolve_candidate_identity(candidate_ref, self._candidate_context)
            candidate_row = self._candidate_context.row("candidate", candidate_ref["candidate"])
            task_row = self._candidate_context.row("task", candidate_ref["task"])
            need(isinstance(candidate_row, dict) and isinstance(task_row, dict),
                 "integrity_error", "Candidate context disappeared during resolution")

            # Historical identity and current use are separate questions.  A
            # retained candidate remains readable after replan, while current
            # use additionally requires the canonical Task candidate/revision,
            # epoch/status, and the normal Task freshness check.
            is_current = bool(
                task_row.get("status") != "cancelled"
                and task_row.get("revision") == candidate_ref["task_revision"]
                and task_row.get("candidate") == candidate_ref["candidate"]
                and candidate_row.get("epoch") == task_row.get("epoch")
            )
            try:
                current_failures = self.c.g.check_current(
                    candidate_ref["task"], ensure_policy=False)
            except Fault as exc:
                if exc.code in {"invalid_json", "invalid_policy", "invalid_input",
                                 "integrity_error", "not_found"}:
                    raise Fault("integrity_error", "Task currentness evidence is invalid",
                                candidate_ref["task"]) from exc
                raise
            if current_failures:
                is_current = False
            if current and not is_current:
                raise Fault("stale_reference", "Candidate is not the current task candidate",
                            {"task": candidate_ref["task"], "candidate": candidate_ref["candidate"],
                             "failures": current_failures})

            # Raw implementation receipt/environment data remains in the
            # private provenance context.  The public assurance response
            # exposes the exact identity and dependency/CAS closure instead of
            # re-publishing private execution material.
            return {
                "mode": "candidate",
                "content": {
                    "kind": "candidate",
                    "project": candidate_ref["project"],
                    "candidate": candidate_ref["candidate"],
                    "task": candidate_ref["task"],
                    "task_revision": candidate_ref["task_revision"],
                    "candidate_digest": candidate_ref["candidate_digest"],
                    "snapshot_digest": candidate_ref["snapshot_digest"],
                    "task_definition_digest": identity["task_definition_digest"],
                },
                "candidate_identity": identity,
                "dependencies": identity["dependency_refs"],
                "current": is_current,
            }
        if ref.get("kind") == "population":
            row=self.s.one("SELECT * FROM traceability_revisions WHERE id=? AND project=?",(ref["revision"],ref["project"]))
            need(row is not None,"unresolved_reference","Population revision is missing",ref["revision"])
            body=parse_json(row["body"],limit=MAX_OBJECT_BYTES)
            need(row["digest"]==ref["revision_digest"]==digest(body) and row["population_digest"]==ref["population_digest"],"integrity_error","Population revision identity differs")
            active=self.s.one("SELECT active_revision,active_digest FROM traceability_sets WHERE id=? AND project=?",(row["set_id"],ref["project"]))
            is_current=bool(active and active["active_revision"]==row["id"] and active["active_digest"]==row["digest"] and row["status"]=="active")
            if current and not is_current:raise Fault("stale_reference","Population revision is not active",ref["revision"])
            return {"mode":"population","content":body,"current":is_current}
        if ref.get("kind") == "population_item":
            pop=validate_typed_ref(ref["population"],project=ref["project"],expected_kinds={"population"})
            population=self._resolve_locator(actor,pop,current=current)
            row=self.s.one("SELECT * FROM traceability_items WHERE revision=? AND id=? AND project=?",(pop["revision"],ref["item"],ref["project"]))
            need(row is not None,"unresolved_reference","Population item is missing",ref["item"])
            body=parse_json(row["body"],limit=MAX_OBJECT_BYTES)
            need(row["digest"]==ref["item_digest"]==digest(body),"integrity_error","Population item identity differs")
            return {"mode":"population_item","content":body,"dependencies":[pop],"current":population.get("current",False) and row["status"]=="known"}
        if ref.get("kind") == "observed_result":
            row = self.s.one("SELECT * FROM receipts WHERE id=? AND project=?",
                             (ref["receipt"], ref["project"]), True)
            need(row is not None, "unresolved_reference", "Observed receipt is missing", ref["receipt"])
            need(row["run"] == ref["run"] and row["binding"] == ref["run_binding"],
                 "integrity_error", "Observed receipt binding differs")
            body = self.c.g.receipt(ref["receipt"])
            need(digest(body) == ref["receipt_digest"] and
                 body.get("snapshot") == ref["snapshot_digest"] and
                 digest(body.get("result", {})) == ref["result_digest"],
                 "integrity_error", "Observed result identity differs")
            run = self.s.one("SELECT * FROM runs WHERE id=? AND project=?",
                             (ref["run"], ref["project"]), True)
            need(run is not None, "unresolved_reference", "Observed execution run is missing", ref["run"])
            run_body = parse_json(run["body"], limit=MAX_OBJECT_BYTES)
            run_result = parse_json(run["result"], limit=MAX_OBJECT_BYTES) if run.get("result") is not None else None
            need(isinstance(run_body, dict) and isinstance(run_result, dict),
                 "legacy_unverified", "Observed execution has no complete run result")
            execution_record_consistency(run, run_body, run_result, row, body)
            material = self._execution_material(actor, ref["project"], ref, run, run_body, body,
                                                current=current)
            is_current = self._observed_execution_current(actor, ref["project"], ref, run, material)
            if current and not is_current:
                raise Fault("stale_reference", "Observed execution is not finished/current", ref["receipt"])
            return {"mode": "observed_result", "content": body, "current": is_current,
                    "payload": material["payload"], "material": material["material_body"],
                    "runtime_check": material.get("runtime_check"),
                    "material_pin": {"id": material["material_row"]["id"],
                                     "digest": material["material_row"]["digest"]},
                    "dependencies": material["dependencies"],
                    "content_refs": material["content_refs"],
                    "definition": material["definition_resolution"],
                    "candidate_identity": material.get("candidate_resolution"),
                    "membership": [{"relation": "execution_of", "target": material["definition_ref"],
                                    "state": "verified"}]}
        if ref.get("kind") == "proposal":
            table=ref["table"];need(table in {"task_revision_proposals","traceability_proposals","assurance_objects"},"invalid_reference","Proposal table is not supported")
            if table=="task_revision_proposals": row=self.s.one("SELECT * FROM task_revision_proposals WHERE id=? AND project=?",(ref["proposal"],ref["project"]))
            elif table=="traceability_proposals": row=self.s.one("SELECT * FROM traceability_proposals WHERE id=? AND project=?",(ref["proposal"],ref["project"]))
            else: row=self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=?",(ref["proposal"],ref["project"]))
            need(row is not None,"unresolved_reference","Proposal is missing",ref["proposal"])
            body=parse_json(row["body"],limit=MAX_OBJECT_BYTES)
            stored=row.get("digest") or digest(body)
            need(stored==ref["proposal_digest"]==digest(body),"integrity_error","Proposal identity differs")
            is_current=body.get("status") not in {"withdrawn","superseded","failed"}
            return {"mode":"proposal","content":body,"current":is_current}
        if ref.get("kind") == "delivery_check":
            delivery_ref=validate_typed_ref(ref["delivery"],project=ref["project"],expected_kinds={"delivery_snapshot"})
            delivery_resolution=self._resolve_locator(actor,delivery_ref,current=current)
            payload=delivery_resolution.get("payload") or {};checks=payload.get("checks",[]) if isinstance(payload,dict) else []
            matches=[check for check in checks if isinstance(check,dict) and check.get("id")==ref["check_id"]]
            need(len(matches)==1,"unresolved_reference","Delivery check is missing or ambiguous",ref["check_id"])
            need(digest(matches[0])==ref["check_digest"],"integrity_error","Delivery check digest differs")
            return {"mode":"delivery_check","content":matches[0],"dependencies":[delivery_ref],
                    "material_dependencies": delivery_resolution.get("dependencies", []),
                    "payload": payload, "current":delivery_resolution.get("current",False)}
        if ref.get("kind") == "assurance_object":
            row = self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind=?", (ref["object"], ref["project"], ref["object_kind"]))
            need(row is not None, "unresolved_reference", "Pinned assurance object is missing", ref["object"])
            need(row["digest"] == ref["object_digest"], "integrity_error", "Pinned assurance object digest differs")
            decoded = self._decode_object(row)
            head = self.s.one("SELECT head_event FROM assurance_heads WHERE project=? AND logical_id=?",
                              (ref["project"], row["logical_id"]))
            event = self.s.one("SELECT * FROM assurance_events WHERE id=?", (head["head_event"],)) if head else None
            is_current = bool(event and event["subject_id"] == row["id"] and
                              event["subject_digest"] == row["digest"] and event["event_kind"] == "adopt")
            if current and not is_current:
                raise Fault("stale_reference", "Assurance object is not the adopted current head", ref["object"])
            if current:
                # A matching head is only the local CAS projection.  Rewalk
                # the semantic dependency closure before exposing current.
                self._ensure_object_current(actor, ref["project"], decoded, require_self=True)
                is_current = True
            return {"mode": "assurance_object", "object": decoded, "current": is_current,
                    "authority": "adopted" if is_current else "unadopted"}
        if ref.get("kind") == "output_artifact":
            output_ref = validate_output_reference(self._identity(ref), project=ref["project"])
            candidates = []
            for row in self.s.all(
                    "SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
                    (ref["project"],)):
                material = self._decode_object(row)
                material_body = material["body"]
                if material_body.get("material_kind") != OUTPUT_MATERIAL_KIND:
                    continue
                payload_blob = material_body.get("payload_blob")
                need(isinstance(payload_blob, str) and _sha(payload_blob),
                     "integrity_error", "Delivery output material payload reference is malformed")
                payload = parse_json(self.s.blob_get(payload_blob), limit=MAX_OBJECT_BYTES)
                if not isinstance(payload, dict) or payload.get("output_identity_digest") != digest(output_ref):
                    continue
                candidates.append((material, payload))
            need(candidates, "missing_evidence", "Pinned Delivery output material is missing", ref.get("output_id"))
            semantic_payloads = {digest(payload) for _material, payload in candidates}
            need(len(semantic_payloads) == 1, "integrity_error",
                 "Multiple Delivery output materials disagree", ref.get("output_id"))
            material, payload = candidates[0]
            material_body = material["body"]
            relation = validate_output_material(
                output_ref, output_payload=payload,
                resolve_ref=lambda nested: self._resolve_locator(actor, nested, current=False),
                load_blob=self.s)
            stored_dependencies = material_body.get("dependency_refs")
            need(isinstance(stored_dependencies, list), "integrity_error",
                 "Delivery output material dependencies are malformed")
            normalized_stored = [validate_typed_ref(item, project=ref["project"])
                                 for item in stored_dependencies]
            need([self._identity(item) for item in normalized_stored] ==
                 [self._identity(item) for item in relation["dependencies"]],
                 "integrity_error", "Delivery output material dependencies differ")
            is_current = relation["current"]
            if current and not is_current:
                raise Fault("stale_reference", "Pinned Delivery output is not current", ref.get("output_id"))
            return {"mode": "output_artifact", "content": relation["output"],
                    "payload": payload, "object": material,
                    "dependencies": relation["dependencies"],
                    "content_refs": relation["content_refs"],
                    "membership": relation["membership"], "current": is_current}
        if ref.get("kind") in {"test_plan", "change", "delivery_snapshot", "actual_delivery_commit"}:
            pin = ref.get("pin")
            need(isinstance(pin, dict), "unresolved_reference", "Mutable reference requires a material pin", ref["kind"])
            row = self.s.one("SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='material'", (pin["id"], ref["project"]))
            need(row is not None and row["digest"] == pin["digest"], "unresolved_reference", "Material pin is missing or changed", pin.get("id"))
            material = self._decode_object(row)
            material_body = material["body"]
            need(set(material_body) == {"format", "material_kind", "project", "origin", "semantic_digest",
                                        "payload_blob", "dependency_refs", "captured_from"} and
                 material_body.get("format") == "daikibo.assurance-material.v1" and
                 material_body.get("project") == ref["project"],
                 "integrity_error", "Material envelope shape differs")
            need(isinstance(material_body.get("payload_blob"), str) and _sha(material_body["payload_blob"]),
                 "integrity_error", "Material payload CAS reference is malformed")
            payload = parse_json(self.s.blob_get(material_body["payload_blob"]), limit=MAX_OBJECT_BYTES)
            need(isinstance(payload, dict) and digest(payload) == material_body["semantic_digest"],
                 "integrity_error", "Material payload digest differs")
            material_cas_closure(self.s, material_body["payload_blob"])
            need(isinstance(material_body.get("dependency_refs"), list), "integrity_error", "Material dependencies are malformed")
            for dependency in material_body["dependency_refs"]:
                validate_typed_ref(dependency, project=ref["project"])
            need(material_body.get("material_kind") == ref["kind"], "integrity_error", "Material kind differs from reference")
            is_current = True
            material_dependencies = [validate_typed_ref(dependency, project=ref["project"])
                                     for dependency in material_body["dependency_refs"]]
            if ref["kind"] == "test_plan":
                need(payload.get("task") == ref["task"] and
                     payload.get("task_revision") == ref["task_revision"] and
                     payload.get("plan_digest") == ref["plan_digest"] and
                     isinstance(payload.get("plan_body"), dict) and
                     digest(payload["plan_body"]) == ref["plan_digest"],
                     "integrity_error", "Pinned test plan identity differs from material payload")
                task = self.s.one("SELECT revision FROM tasks WHERE id=? AND project=?", (ref["task"], ref["project"]))
                plan = self.s.one("SELECT digest FROM plans WHERE task=?", (ref["task"],))
                is_current = bool(task and plan and task["revision"] == ref["task_revision"] and plan["digest"] == ref["plan_digest"])
            elif ref["kind"] == "change":
                need(payload.get("change") == ref["change"] and payload.get("revision") == ref["revision"] and
                     isinstance(payload.get("body"), dict) and digest(payload["body"]) == ref["body_digest"],
                     "integrity_error", "Pinned change identity differs from material payload")
                change = self.s.one("SELECT revision,body FROM changes WHERE id=? AND project=?", (ref["change"], ref["project"]))
                is_current = bool(change and change["revision"] == ref["revision"] and
                                  digest(parse_json(change["body"], limit=MAX_OBJECT_BYTES)) == ref["body_digest"])
            elif ref["kind"] == "delivery_snapshot":
                need(set(payload) == {"delivery", "binding", "snapshot", "checks", "build_definitions",
                                      "target_environment", "applicability", "rollback"},
                     "integrity_error", "Pinned delivery snapshot payload shape differs")
                snapshot_digest = validate_sealed_snapshot(self.s, payload.get("snapshot"))
                payload_binding = payload.get("binding")
                need(isinstance(payload_binding, dict) and
                     payload.get("delivery") == ref["delivery"] and
                     digest(payload_binding) == ref["binding_digest"] and
                     payload_binding.get("snapshot") == snapshot_digest and
                     snapshot_digest == ref["snapshot_digest"],
                     "integrity_error", "Pinned delivery snapshot identity differs from material payload")
                need(not material_dependencies, "integrity_error", "Delivery snapshot has unexpected typed dependencies")
                delivery = self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?",
                                      (ref["delivery"], ref["project"]))
                if delivery:
                    delivery_body = parse_json(delivery["body"], limit=MAX_OBJECT_BYTES)
                    need(isinstance(delivery_body, dict), "integrity_error", "Stored Delivery body is not an object")
                    live_snapshot = delivery_body.get("snapshot") if isinstance(delivery_body, dict) else None
                    live_snapshot_digest = None
                    if isinstance(live_snapshot, dict):
                        try:
                            live_snapshot_digest = validate_sealed_snapshot(self.s, live_snapshot)
                        except Fault:
                            live_snapshot_digest = None
                    is_current = (delivery["digest"] == ref["binding_digest"] and
                                  isinstance(delivery_body.get("binding"), dict) and
                                  digest(delivery_body["binding"]) == ref["binding_digest"] and
                                  live_snapshot_digest == ref["snapshot_digest"] and
                                  payload.get("binding") == delivery_body.get("binding") and
                                  payload.get("snapshot") == live_snapshot)
                    if is_current and current:
                        try:
                            self.c.d.current(ref["delivery"])
                        except Fault as exc:
                            raise Fault("stale_reference", "Delivery snapshot is not current", ref["delivery"]) from exc
                else:
                    is_current = False
            elif ref["kind"] == "actual_delivery_commit":
                delivery_ref = ref.get("delivery")
                need(isinstance(delivery_ref, dict), "integrity_error", "Actual commit delivery reference is missing")
                binding_digest = delivery_ref["binding_digest"]
                snapshot_digest = delivery_ref["snapshot_digest"]
                need(set(payload) == {"delivery_snapshot_ref", "repository", "object_format", "commit", "tree", "ref",
                                      "commit_object_blob", "object_manifest_blob", "observed_result"},
                     "integrity_error", "Pinned delivery commit payload shape differs")
                pinned_snapshot_ref = validate_typed_ref(payload["delivery_snapshot_ref"], project=ref["project"],
                                                         expected_kinds={"delivery_snapshot"})
                need(self._identity(pinned_snapshot_ref) == self._identity(delivery_ref),
                     "integrity_error", "Pinned delivery commit snapshot identity differs")
                snapshot_resolution = self._resolve_locator(actor, pinned_snapshot_ref, current=False)
                snapshot_payload = snapshot_resolution.get("payload") or {}
                snapshot = snapshot_payload.get("snapshot") if isinstance(snapshot_payload, dict) else None
                need(isinstance(snapshot, dict), "missing_evidence", "Pinned delivery snapshot material is missing")
                need(pinned_snapshot_ref["binding_digest"] == binding_digest and
                     pinned_snapshot_ref["snapshot_digest"] == snapshot_digest and
                     payload.get("repository") == ref["repository"] and
                     payload.get("object_format") == ref["object_format"] and
                     payload.get("commit") == ref["commit"] and payload.get("tree") == ref["tree"],
                     "integrity_error", "Pinned delivery commit identity differs from material payload")
                validate_git_material_payload(self.s, payload, snapshot)
                need(len(material_dependencies) == 1 and
                     self._identity(material_dependencies[0]) == self._identity(delivery_ref),
                     "integrity_error", "Actual delivery commit dependency is not its snapshot")
                delivery = self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?",
                                      (delivery_ref["delivery"], ref["project"]))
                current_git = None
                live_snapshot_digest = None
                if delivery:
                    delivery_body = parse_json(delivery["body"], limit=MAX_OBJECT_BYTES)
                    need(isinstance(delivery_body, dict), "integrity_error", "Stored Delivery body is not an object")
                    current_git = (delivery_body.get("git") or {}).get(ref["repository"])
                    live_snapshot = delivery_body.get("snapshot") if isinstance(delivery_body, dict) else None
                    if isinstance(live_snapshot, dict):
                        try:
                            live_snapshot_digest = validate_sealed_snapshot(self.s, live_snapshot)
                        except Fault:
                            live_snapshot_digest = None
                is_current = bool(delivery and delivery["digest"] == binding_digest and
                                  isinstance(delivery_body.get("binding"), dict) and
                                  digest(delivery_body["binding"]) == binding_digest and
                                  live_snapshot_digest == snapshot_digest and
                                  isinstance(current_git, dict) and
                                  current_git.get("commit") == ref["commit"] and current_git.get("tree") == ref["tree"] and
                                  current_git.get("repository") == ref["repository"] and
                                  current_git.get("snapshot") == snapshot_digest and
                                  live_snapshot_digest == snapshot_digest)
                if is_current and current:
                    try:
                        self.c.d.current(delivery_ref["delivery"])
                    except Fault as exc:
                        raise Fault("stale_reference", "Actual delivery commit is not current", ref["repository"]) from exc
            if current and not is_current:
                raise Fault("stale_reference", "Pinned material is not current", ref["kind"])
            return {"mode": "material", "object": material, "payload": payload,
                    "dependencies": material_dependencies, "current": is_current}
        if ref.get("kind") == "test_plan_check":
            plan_ref = validate_typed_ref(ref["plan"], project=ref["project"])
            plan_resolution = self._resolve_locator(actor, plan_ref, current=current)
            payload = plan_resolution.get("payload") or {}
            plan_body = payload.get("plan_body") if isinstance(payload, dict) else None
            checks = plan_body.get("checks", []) if isinstance(plan_body, dict) else []
            matches = [check for check in checks if isinstance(check, dict) and check.get("id") == ref["check_id"]]
            need(len(matches) == 1, "unresolved_reference", "Pinned test plan check is missing or ambiguous", ref["check_id"])
            check = matches[0]
            need(digest(check) == ref["check_digest"], "integrity_error", "Pinned test plan check digest differs")
            return {"mode": "test_plan_check", "content": check, "dependencies": [plan_ref],
                    "material_dependencies": plan_resolution.get("dependencies", []),
                    "payload": payload, "current": plan_resolution.get("current", False)}
        if ref.get("kind") == "task_revision":
            row = self.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (ref["task"], ref["project"]))
            need(row is not None, "unresolved_reference", "Task is missing", ref["task"])
            body = parse_json(row["body"], limit=MAX_OBJECT_BYTES)
            from .task_revisions import task_definition_digest, validate_history_record
            actual = task_definition_digest(body)
            # Collect every retained definition for each Task revision before
            # comparing the requested digest.  A revision appears twice in a
            # normal chain (the preceding ``after`` and following ``before``
            # snapshot); equal definition bodies are one identity, while two
            # different bodies are an integrity failure even if the caller's
            # requested digest happens to match only one side.
            definitions: dict[int, dict[str, dict[str, Any]]] = {}

            def retain_definition(snapshot: dict[str, Any]) -> None:
                if (snapshot.get("id") != ref["task"]
                        or snapshot.get("project") != ref["project"]):
                    raise Fault("integrity_error", "Task revision history snapshot identity differs", ref["task"])
                revision = snapshot.get("revision")
                if type(revision) is not int or revision < 1:
                    raise Fault("integrity_error", "Task revision snapshot revision is malformed", ref["task"])
                definition_digest = task_definition_digest(snapshot.get("body"))
                versions = definitions.setdefault(revision, {})
                versions.setdefault(definition_digest, snapshot)
                if len(versions) > 1:
                    raise Fault("integrity_error", "Task revision history contains conflicting definition identities", ref["task"])

            current_snapshot = dict(row)
            current_snapshot["body"] = body
            retain_definition(current_snapshot)
            seen_to: dict[int, str] = {}
            histories = self.s.all("SELECT * FROM task_revision_history WHERE task=? AND project=? ORDER BY from_revision,to_revision,id", (ref["task"], ref["project"]))
            for history in histories:
                try:
                    hbody = parse_json(history["body"], limit=MAX_OBJECT_BYTES)
                except (Fault, TypeError, ValueError) as exc:
                    raise Fault("integrity_error", "Task revision history is malformed", ref["task"]) from exc
                history_row = dict(history)
                history_row["body"] = hbody
                # Apply-revision writes this exact immutable record shape. Do
                # not treat a checksum or an ID-only match as sufficient.
                try:
                    validate_history_record(history_row, ref["project"])
                except Fault as exc:
                    raise Fault("integrity_error", "Task revision history failed immutable validation", ref["task"]) from exc
                except (KeyError, TypeError, ValueError) as exc:
                    raise Fault("integrity_error", "Task revision history is malformed", ref["task"]) from exc
                to_revision = history_row["to_revision"]
                need(to_revision not in seen_to, "integrity_error",
                     "Task revision history contains a duplicate target revision", ref["task"])
                seen_to[to_revision] = history_row["id"]
                for side in ("before", "after"):
                    snapshot = hbody[side]
                    candidate = snapshot["task"]
                    retain_definition(candidate)

            is_current = row["revision"] == ref["revision"] and actual == ref["definition_digest"]
            requested = definitions.get(ref["revision"], {}).get(ref["definition_digest"])
            if is_current:
                return {"mode": "task_revision", "content": body, "current": True}
            if requested is not None:
                # Historical resolution is explicitly available through the
                # pinned/read path.  A currentness-required resolver must not
                # silently downgrade that same old identity to historical.
                if current:
                    raise Fault("stale_reference", "Task revision is not current", ref["revision"])
                return {"mode": "task_revision", "content": requested, "current": False}
            raise Fault("unresolved_reference", "Task revision material is not retained", ref["revision"])
        # The remaining variants require their typed material or the Unit B
        # execution/delivery adapters.  An ID alone is never treated as a
        # successful resolution.
        raise Fault("unresolved_reference", "This typed reference needs its retained material adapter", ref["kind"])

    def resolve_pinned(self, actor, ref: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_typed_ref(ref, project=ref.get("project") if isinstance(ref, dict) else None)
        self._project(actor, normalized["project"], read=True)
        resolved = self._resolve_locator(actor, normalized, current=False)
        return self._resolved(normalized, resolved, current_state="not_evaluated")

    def _resolved(self, ref: dict[str, Any], resolution: dict[str, Any], *, current_state: str,
                  reasons: list[str] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        canonical_ref=self._identity(ref)
        return {"format": "daikibo.assurance-resolved.v1", "canonical_ref": canonical_ref,
                "identity_digest": ref.get("identity_digest", digest(canonical_ref)),
                "semantic_kind": semantic_kind(ref),
                "dependency_refs": resolution.get("dependencies", []),
                "content_refs": resolution.get("content_refs", []),
                "current": {"state": current_state, "reasons": reasons or []},
                "authority": {"state": "not_evaluated", "reasons": ["E1 resolver does not perform review authority checks"]},
                "membership": resolution.get("membership", []),
                "resolution": resolution, "context": context}

    def resolve(self, actor, project: str, ref: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Public read resolver; it never creates a missing material pin."""
        self._project(actor, project, read=True)
        normalized = validate_typed_ref(ref, project=project)
        if context is None:
            resolution = self._resolve_locator(actor, normalized, current=False)
            return self._resolved(normalized, resolution, current_state="not_evaluated")
        need(isinstance(context, dict) and set(context) == {"profile", "stage", "task", "delivery"},
             "invalid_context", "Assurance resolve context keys differ")
        need(context["stage"] in {"plan", "task", "integration", "delivery"}, "invalid_context", "Assurance stage is invalid")
        profile = context["profile"]
        need(isinstance(profile, dict) and set(profile) == {"id", "digest"},
             "invalid_context", "Assurance profile selection is required")
        need(isinstance(profile["id"], str) and bool(profile["id"]), "invalid_context", "Profile id is invalid")
        need(isinstance(profile["digest"], str) and _sha(profile["digest"]), "invalid_context", "Profile digest is invalid")
        profile_ref = {"kind": "assurance_object", "project": project, "object": profile["id"],
                       "object_kind": "profile", "object_digest": profile["digest"]}
        profile_resolution = self._resolve_locator(actor, validate_typed_ref(profile_ref, project=project), current=True)
        profile_body = profile_resolution["object"]["body"]
        stage_rules = profile_body.get("stage_rules", {}) if isinstance(profile_body, dict) else {}
        if stage_rules:
            need(isinstance(stage_rules, dict) and context["stage"] in stage_rules,
                 "invalid_context", "Selected profile does not cover the requested stage")
        if context["stage"] == "task":
            need(isinstance(context["task"], dict), "invalid_context", "Task context is required")
            task_ref = validate_typed_ref(context["task"], project=project, expected_kinds={"task_revision"})
            self._resolve_locator(actor, task_ref, current=True)
        else:
            need(context["task"] is None, "invalid_context", "Task context is only valid at task stage")
        if context["stage"] in {"integration", "delivery"}:
            need(isinstance(context["delivery"], dict), "invalid_context", "Delivery context is required")
            delivery_ref = validate_typed_ref(context["delivery"], project=project,
                                              expected_kinds={"delivery_snapshot", "actual_delivery_commit"})
            self._resolve_locator(actor, delivery_ref, current=True)
        else:
            need(context["delivery"] is None, "invalid_context", "Delivery context is not valid at this stage")
        resolution = self._resolve_locator(actor, normalized, current=True)
        return self._resolved(normalized, resolution,
                              current_state="current" if resolution.get("current", True) else "stale",
                              context=context)

    def evaluate_current(self, actor, ref: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = validate_typed_ref(ref, project=ref.get("project") if isinstance(ref, dict) else None)
        self._project(actor, normalized["project"], read=True)
        try:
            resolved = self._resolve_locator(actor, normalized, current=True)
        except Fault as exc:
            if exc.code in {"stale_reference", "unresolved_reference", "missing_evidence", "integrity_error", "legacy_unverified"}:
                return self._resolved(normalized, {"error": exc.code}, current_state="stale" if exc.code == "stale_reference" else "unknown", reasons=[exc.code], context=context)
            raise
        return self._resolved(normalized, resolved, current_state="current" if resolved.get("current", True) else "stale", context=context)

    def cas_closure(self, project: str) -> set[str]:
        """Return all stored CAS digests reachable from assurance references."""
        result: set[str] = set()
        rows = self.s.all("SELECT ref_kind,ref_id,ref_digest FROM assurance_refs r JOIN assurance_objects o ON o.id=r.object_id WHERE o.project=?", (project,))
        for row in rows:
            value = row["ref_digest"]
            if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
                try:
                    self.s.blob_get(value)
                except Fault:
                    continue
                result.add(value)
        for row in self.s.all("SELECT body FROM assurance_objects WHERE project=? AND kind='material'", (project,)):
            body=parse_json(row["body"], limit=MAX_OBJECT_BYTES)
            value=body.get("payload_blob") if isinstance(body,dict) else None
            need(isinstance(value, str) and _sha(value), "integrity_error", "Material payload CAS reference is malformed")
            result.update(material_cas_closure(self.s, value))
        return result

    def archive_rows(self, project: str) -> dict[str, list[dict[str, Any]]]:
        queries={
            "assurance_objects":"SELECT * FROM assurance_objects WHERE project=? ORDER BY id",
            "assurance_events":"SELECT * FROM assurance_events WHERE project=? ORDER BY id",
            "assurance_heads":"SELECT * FROM assurance_heads WHERE project=? ORDER BY logical_id",
            "assurance_refs":"SELECT r.* FROM assurance_refs r JOIN assurance_objects o ON o.id=r.object_id WHERE o.project=? ORDER BY r.object_id,r.ordinal",
        }
        return {section: self.s.all(sql, (project,)) for section,sql in queries.items()}


def _sha(value: Any, name: str | None = None) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_assurance_rows(tables: dict[str, list[dict[str, Any]]], project: str,
                            external_tables: Any = None, blob_get: Any = None) -> None:
    """Validate assurance rows and, when supplied, their archived endpoints.

    The optional lookup is used by the standard archive inspector.  Keeping it
    optional preserves the small assurance-only validator used by callers that
    already have no external table set, while the full archive still rejects an
    E2 profile/edge containing a missing artifact or source endpoint.
    """
    objects = {row["id"]: row for row in tables.get("assurance_objects", [])}
    need(len(objects) == len(tables.get("assurance_objects", [])), "invalid_archive", "Duplicate assurance object")
    object_keys = set()
    for row in objects.values():
        need(row.get("project") == project and row.get("kind") in OBJECT_KINDS, "invalid_archive", "Assurance object crosses project or kind boundary")
        object_key = (row["project"], row["kind"], row["logical_id"], row["revision"])
        need(object_key not in object_keys, "invalid_archive", "Duplicate assurance object revision")
        object_keys.add(object_key)
        body = row.get("body"); body = parse_json(body, limit=MAX_OBJECT_BYTES) if isinstance(body, str) else body
        need(isinstance(body, dict) and body.get("project") == project and digest(body) == row.get("digest"), "invalid_archive", "Assurance object body/digest differs")
        _object_contract(row["kind"], body)
    def saved_body(row):
        body = row["body"]
        return parse_json(body) if isinstance(body, str) else body

    def saved_object(ref, kind):
        value = objects.get(ref.get("object")) if isinstance(ref, dict) else None
        need(value is not None and value["kind"] == kind and
             Assurance._identity(ref) == Assurance._object_ref(None, value),
             "invalid_archive", "Versioned assurance pair reference differs")
        return value

    for row in objects.values():
        body = saved_body(row)
        if row["kind"] == "profile" and body.get("format") in {PROFILE_V1_FORMAT, *CANONICAL_PROFILE_FORMATS}:
            scope = saved_object(body["scope_ref"], "scope")
            ob = saved_object(body["obligations_ref"], "obligations")
            v2 = saved_body(scope).get("format") == SCOPE_V2
            need((body["format"] == PROFILE_V5_FORMAT) == v2 and
                 (saved_body(ob).get("format") == OBLIGATIONS_V2) == v2,
                 "invalid_archive", "Profile and scope contracts differ")
            need(saved_body(ob).get("scope_ref") == body["scope_ref"], "invalid_archive", "Profile pair crosses scope")
        if body.get("format") == OBLIGATIONS_V2:
            scope = saved_object(body["scope_ref"], "scope")
            need(saved_body(scope).get("format") == SCOPE_V2 and row["logical_id"] == "obligations:"+scope["id"],
                 "invalid_archive", "Obligations v2 scope pair differs")
            siblings = [x for x in objects.values() if x["kind"] == "obligations" and x["logical_id"] == row["logical_id"]]
            need(len(siblings) == 1, "invalid_archive", "Scope v2 pair is ambiguous")
            need(external_tables is not None, "invalid_archive", "Responsibility history requires retained artifact closure")
    refs = tables.get("assurance_refs", [])
    seen = set()
    indexed_by_object: dict[str, set[tuple[Any, ...]]] = {object_id: set() for object_id in objects}
    for row in refs:
        key = (row["object_id"], row["ordinal"])
        need(key not in seen and row["object_id"] in objects, "invalid_archive", "Assurance reference index is dangling or duplicated")
        seen.add(key)
        need(row["ref_kind"] in TYPED_REF_KINDS and _sha(row["ref_digest"]), "invalid_archive", "Assurance reference identity is malformed")
        indexed_by_object[row["object_id"]].add((row["purpose"], row["ref_kind"], row["ref_id"], str(row["ref_revision"]), row["ref_digest"]))
    for object_id, object_row in objects.items():
        body = object_row["body"]; body = parse_json(body) if isinstance(body, str) else body
        found = set()
        for p, raw in _walk_refs(body):
            indexed = _indexed_reference(p, raw, project)
            found.add((indexed["purpose"], indexed["kind"], indexed["id"], indexed["revision"], indexed["digest"]))
        need(indexed_by_object[object_id] == found,
             "invalid_archive", "Assurance reference index differs from canonical body")
    if external_tables is None:
        # An assurance-only row check has no authority to certify an
        # observed execution.  Keep ordinary historical object validation
        # usable, but never report success for an observed-result dependency
        # when its run/receipt/material context was omitted.  A retained
        # delivery-output material is the one portable exception: its
        # immutable payload carries the output identity and the nested
        # observed ref, while the full archive inspector performs the
        # receipt/run/CAS closure check when those tables are available.
        for object_row in objects.values():
            body = object_row["body"]
            body = parse_json(body) if isinstance(body, str) else body
            material_body = body if object_row.get("kind") == "material" else None
            output_material = (isinstance(material_body, dict) and
                               material_body.get("material_kind") == OUTPUT_MATERIAL_KIND)
            for _path, raw in _walk_refs(body):
                if semantic_kind(raw) == "observed_result":
                    if output_material:
                        continue
                    need(False, "invalid_archive",
                         "Observed execution context is required for archive verification",
                         object_row.get("id"))

    if external_tables is not None:
        def archive_candidate_failure(kind: str, message: str,
                                      details: Any = None) -> None:
            # Historical inspection has no live authority.  Every shared
            # candidate provenance failure is an invalid archive.
            raise Fault("invalid_archive", message, details)

        def external(section: str, key: str) -> dict[str, Any] | None:
            if callable(external_tables):
                return external_tables(section, key)
            values = external_tables.get(section, {}) if isinstance(external_tables, dict) else {}
            return values.get(key) if isinstance(values, dict) else None

        def archive_pinned_context():
            """Build the same finite candidate context used by live reads.

            The standard chunked archive attaches its complete context
            projection to the endpoint callback.  A standalone caller may
            also pass that projection as a mapping.  A callback without the
            projection is deliberately insufficient: it can resolve a row by
            ID, but cannot prove Task history, implementation run/receipt,
            repository, and candidate snapshot CAS closure.
            """
            rows = getattr(external_tables, "context_rows", None)
            if rows is None and isinstance(external_tables, dict):
                rows = {}
                for section in ("tasks", "candidates", "runs", "receipts", "repos",
                                "task_revision_history"):
                    value = external_tables.get(section)
                    if isinstance(value, dict):
                        rows[section] = list(value.values())
                    elif isinstance(value, list):
                        rows[section] = list(value)
            if not isinstance(rows, dict):
                return None
            required = {"tasks", "candidates", "runs", "receipts", "repos",
                        "task_revision_history"}
            if not required <= set(rows) or any(not isinstance(rows[name], list)
                                                for name in required):
                return None
            try:
                from .traceability import _ArchivePinnedContext
                return _ArchivePinnedContext(rows, blob_get, project=project)
            except (ImportError, TypeError, ValueError):
                return None

        def legacy_identity_matches(raw: dict[str, Any]) -> bool:
            """Recognize a retained E1 identity fixture without certifying it as E2.

            Early assurance rows sometimes used an artifact-shaped locator for
            an immutable assurance object.  Preserve those rows only when the
            referenced object is actually archived under the same identity
            and digest.  A missing ID, or a digest mismatch, is still a hard
            archive failure; this is not a generic missing-endpoint bypass.
            """
            kind = semantic_kind(raw)
            if kind == "artifact":
                object_id = raw.get("artifact")
                expected_revision, expected_digest = raw.get("revision"), raw.get("body_digest")
            elif kind == "traceability_ref" and raw.get("locator", {}).get("ref_type") == "artifact_ac":
                locator = raw["locator"]
                object_id = locator.get("artifact")
                expected_revision, expected_digest = locator.get("revision"), locator.get("body_digest")
            else:
                return False
            target = objects.get(object_id)
            return bool(target and target.get("project") == project and
                        target.get("revision") == expected_revision and
                        target.get("digest") == expected_digest)

        def archived_artifact(raw: dict[str, Any]):
            """Resolve an execution artifact through archived canonical rows."""
            artifact = external("artifacts", raw["artifact"])
            need(artifact is not None and artifact.get("project") == project and
                 artifact.get("status") == "accepted",
                 "invalid_archive", "Execution artifact endpoint is missing or not accepted", raw["artifact"])
            revision = external("revisions", canonical([raw["artifact"], raw["revision"]]).decode())
            body = revision.get("body") if isinstance(revision, dict) else None
            if isinstance(body, str):
                body = parse_json(body, limit=MAX_OBJECT_BYTES)
            need(isinstance(revision, dict) and revision.get("artifact") == raw["artifact"] and
                 revision.get("revision") == raw["revision"] and
                 revision.get("digest") == raw["body_digest"] and
                 isinstance(body, dict) and digest(body) == raw["body_digest"],
                 "invalid_archive", "Execution artifact revision differs", raw["artifact"])
            return artifact, body

        def archived_produced_artifact(raw: dict[str, Any]):
            """Resolve a draft or accepted Knowledge artifact for Consumer-P.

            Ordinary assurance endpoints retain their accepted-only contract;
            an artifact-production material is the controller's immutable
            proof that this draft was created from the sealed candidate.
            """
            artifact = external("artifacts", raw["artifact"])
            need(artifact is not None and artifact.get("project") == project and
                 artifact.get("status") in {"draft", "accepted"},
                 "invalid_archive", "Produced artifact endpoint is missing or invalid", raw["artifact"])
            revision = external("revisions", canonical([raw["artifact"], raw["revision"]]).decode())
            body = revision.get("body") if isinstance(revision, dict) else None
            if isinstance(body, str):
                body = parse_json(body, limit=MAX_OBJECT_BYTES)
            need(isinstance(revision, dict) and revision.get("artifact") == raw["artifact"] and
                 revision.get("revision") == raw["revision"] and
                 revision.get("status") in {"draft", "accepted"} and
                 revision.get("digest") == raw["body_digest"] and
                 isinstance(body, dict) and digest(body) == raw["body_digest"],
                 "invalid_archive", "Produced artifact revision differs", raw["artifact"])
            return {"id": artifact["id"], "project": artifact["project"],
                    "kind": artifact["kind"], "revision": revision["revision"],
                    "status": revision["status"], "body": body,
                    "digest": revision["digest"]}

        def archived_candidate(raw: dict[str, Any]):
            """Resolve candidate identity from retained archive context."""
            context = archive_pinned_context()
            need(context is not None, "invalid_archive",
                 "Execution candidate provenance context is missing", raw["candidate"])
            try:
                return resolve_candidate_identity(
                    _material_identity(raw), context,
                    failure=archive_candidate_failure)
            except Fault as exc:
                if exc.code == "invalid_archive":
                    raise
                raise Fault("invalid_archive", "Execution candidate provenance is invalid",
                            str(exc)) from exc

        class _ArchiveDefinitionCAS:
            def blob_get(self, ident):
                if callable(blob_get):
                    value = blob_get(ident)
                elif isinstance(blob_get, dict):
                    value = blob_get.get(ident)
                else:
                    value = None
                need(isinstance(value, (bytes, bytearray)) and
                     digest(bytes(value)) == ident,
                     "invalid_archive",
                     "Execution definition CAS child is missing or changed", ident)
                return bytes(value)

        archive_cas = _ArchiveDefinitionCAS()

        def material_for(material_ref: dict[str, Any], expected_kind: str):
            """Read one archived immutable material envelope and its CAS."""
            pin = material_ref.get("pin")
            need(isinstance(pin, dict), "invalid_archive",
                 "Execution definition material pin is missing")
            material = external("assurance_objects", pin.get("id"))
            need(isinstance(material, dict) and material.get("project") == project and
                 material.get("kind") == "material" and
                 material.get("digest") == pin.get("digest"),
                 "invalid_archive",
                 "Execution definition material is missing or changed", pin.get("id"))
            body = material.get("body")
            if isinstance(body, str):
                body = parse_json(body, limit=MAX_OBJECT_BYTES)
            required = {"format", "material_kind", "project", "origin",
                        "semantic_digest", "payload_blob", "dependency_refs",
                        "captured_from"}
            need(isinstance(body, dict) and set(body) == required and
                 body.get("format") == MATERIAL_FORMAT and
                 body.get("material_kind") == expected_kind and
                 body.get("project") == project and
                 _sha(body.get("semantic_digest")) and
                 _sha(body.get("payload_blob")) and
                 digest(body) == material.get("digest"),
                 "invalid_archive", "Execution definition material envelope differs")
            material_cas_closure(archive_cas, body["payload_blob"])
            payload = parse_json(archive_cas.blob_get(body["payload_blob"]),
                                 limit=MAX_OBJECT_BYTES)
            need(isinstance(payload, dict) and
                 digest(payload) == body["semantic_digest"],
                 "invalid_archive", "Execution definition material payload differs")
            dependencies = body.get("dependency_refs")
            need(isinstance(dependencies, list), "invalid_archive",
                 "Execution definition material dependencies are malformed")
            normalized_dependencies = [
                validate_typed_ref(item, project=project)
                for item in dependencies
            ]
            return body, payload, normalized_dependencies

        def archived_definition(raw: dict[str, Any]):
            """Resolve a check through its immutable archived material."""
            definition = validate_typed_ref(
                _material_identity(raw), project=project,
                expected_kinds={"test_plan_check", "delivery_check"})

            if definition["kind"] == "test_plan_check":
                plan = validate_typed_ref(_material_identity(definition["plan"]),
                                          project=project, expected_kinds={"test_plan"})
                _body, payload, dependencies = material_for(plan, "test_plan")
                need(set(payload) == {"task", "task_revision", "plan_body", "plan_digest"} and
                     payload.get("task") == plan["task"] and
                     payload.get("task_revision") == plan["task_revision"] and
                     payload.get("plan_digest") == plan["plan_digest"] and
                     isinstance(payload.get("plan_body"), dict) and
                     digest(payload["plan_body"]) == plan["plan_digest"],
                     "invalid_archive", "Pinned test plan identity differs from material payload")
                task_dependencies = [item for item in dependencies
                                     if item.get("kind") == "task_revision"]
                need(len(dependencies) == 1 and len(task_dependencies) == 1 and
                     task_dependencies[0].get("project") == project and
                     task_dependencies[0].get("task") == plan["task"] and
                     task_dependencies[0].get("revision") == plan["task_revision"] and
                     _sha(task_dependencies[0].get("definition_digest")),
                     "invalid_archive", "Pinned test plan Task dependency differs")
                checks = payload["plan_body"].get("checks")
                need(isinstance(checks, list), "invalid_archive",
                     "Pinned test plan checks are malformed")
                matches = [check for check in checks
                           if isinstance(check, dict) and
                           check.get("id") == definition["check_id"]]
                need(len(matches) == 1 and
                     digest(matches[0]) == definition["check_digest"],
                     "invalid_archive", "Pinned test plan check identity differs")
                return {"mode": "test_plan_check", "content": matches[0],
                        "dependencies": [_material_identity(plan)],
                        "material_dependencies": dependencies,
                        "current": False, "payload": payload}

            delivery = validate_typed_ref(_material_identity(definition["delivery"]),
                                          project=project,
                                          expected_kinds={"delivery_snapshot"})
            _body, payload, dependencies = material_for(delivery, "delivery_snapshot")
            need(set(payload) == {"delivery", "binding", "snapshot", "checks",
                                  "build_definitions", "target_environment",
                                  "applicability", "rollback"},
                 "invalid_archive", "Pinned delivery material payload shape differs")
            snapshot_digest = validate_sealed_snapshot(archive_cas, payload.get("snapshot"))
            binding = payload.get("binding")
            need(isinstance(binding, dict) and payload.get("delivery") == delivery["delivery"] and
                 digest(binding) == delivery["binding_digest"] and
                 binding.get("snapshot") == snapshot_digest and
                 snapshot_digest == delivery["snapshot_digest"] and not dependencies,
                 "invalid_archive", "Pinned delivery snapshot identity differs")
            checks = payload.get("checks")
            need(isinstance(checks, list), "invalid_archive",
                 "Pinned delivery checks are malformed")
            matches = [check for check in checks
                       if isinstance(check, dict) and
                       check.get("id") == definition["check_id"]]
            need(len(matches) == 1 and
                 digest(matches[0]) == definition["check_digest"],
                 "invalid_archive", "Pinned delivery check identity differs")
            return {"mode": "delivery_check", "content": matches[0],
                    "dependencies": [_material_identity(delivery)],
                    "material_dependencies": dependencies,
                    "current": False, "payload": payload}

        def archived_delivery(raw: dict[str, Any]) -> dict[str, Any]:
            """Resolve the immutable Delivery snapshot used by an output."""
            delivery = validate_typed_ref(_material_identity(raw), project=project,
                                          expected_kinds={"delivery_snapshot"})
            _body, payload, dependencies = material_for(delivery, "delivery_snapshot")
            need(set(payload) == {"delivery", "binding", "snapshot", "checks",
                                  "build_definitions", "target_environment",
                                  "applicability", "rollback"},
                 "invalid_archive", "Pinned delivery output snapshot payload shape differs")
            snapshot_digest = validate_sealed_snapshot(archive_cas, payload.get("snapshot"))
            binding = payload.get("binding")
            need(isinstance(binding, dict) and payload.get("delivery") == delivery["delivery"] and
                 digest(binding) == delivery["binding_digest"] and
                 binding.get("snapshot") == snapshot_digest and
                 snapshot_digest == delivery["snapshot_digest"] and not dependencies,
                 "invalid_archive", "Pinned delivery output snapshot identity differs")
            return {"mode": "delivery_snapshot", "content": payload.get("snapshot"),
                    "payload": payload, "dependencies": [_material_identity(delivery)],
                    "current": False}

        def archived_observed(raw: dict[str, Any]) -> dict[str, Any]:
            """Resolve and verify one archived receipt/run/material triple."""
            observed_ref = validate_typed_ref(_material_identity(raw), project=project,
                                              expected_kinds={"observed_result"})
            receipt_row = external("receipts", observed_ref["receipt"])
            run_row = external("runs", observed_ref["run"])
            need(receipt_row is not None and run_row is not None,
                 "invalid_archive", "Output observed receipt or run is missing")
            need(receipt_row.get("project") == project and run_row.get("project") == project and
                 receipt_row.get("run") == observed_ref["run"] and
                 receipt_row.get("binding") == observed_ref["run_binding"],
                 "invalid_archive", "Output observed execution row identity differs")
            observed = receipt_row.get("body")
            if isinstance(observed, str):
                observed = parse_json(observed, limit=MAX_OBJECT_BYTES)
            run_body = run_row.get("body")
            if isinstance(run_body, str):
                run_body = parse_json(run_body, limit=MAX_OBJECT_BYTES)
            run_result = run_row.get("result")
            if isinstance(run_result, str):
                run_result = parse_json(run_result, limit=MAX_OBJECT_BYTES)
            need(isinstance(observed, dict) and isinstance(run_body, dict) and
                 isinstance(run_result, dict),
                 "invalid_archive", "Output observed execution record is incomplete")
            try:
                execution_record_consistency(run_row, run_body, run_result,
                                             receipt_row, observed)
            except Fault as exc:
                raise Fault("invalid_archive", "Output observed execution failed archive consistency",
                            str(exc)) from exc
            need(digest(observed) == observed_ref["receipt_digest"] and
                 observed.get("snapshot") == observed_ref["snapshot_digest"] and
                 digest(observed.get("result", {})) == observed_ref["result_digest"],
                 "invalid_archive", "Output observed result identity differs")
            pin = observed.get("verification_material")
            need(isinstance(pin, dict) and set(pin) == {"id", "digest"},
                 "invalid_archive", "Output observed execution material pin is missing")
            material = external("assurance_objects", pin["id"])
            need(material is not None and material.get("project") == project and
                 material.get("kind") == "material" and material.get("digest") == pin["digest"],
                 "invalid_archive", "Output observed execution material is missing")
            material_body = material.get("body")
            if isinstance(material_body, str):
                material_body = parse_json(material_body, limit=MAX_OBJECT_BYTES)
            need(blob_get is not None, "invalid_archive",
                 "Output observed execution CAS read boundary is unavailable")

            class _ArchiveCAS:
                def blob_get(self, ident):
                    if callable(blob_get):
                        value = blob_get(ident)
                    elif isinstance(blob_get, dict):
                        value = blob_get.get(ident)
                    else:
                        value = None
                    need(isinstance(value, (bytes, bytearray)) and digest(bytes(value)) == ident,
                         "invalid_archive", "Output observed execution CAS child is missing or changed", ident)
                    return bytes(value)

            try:
                relation = validate_execution_material_relation(
                    project=project, ref=observed_ref, run_row=run_row, run_body=run_body,
                    observed=observed, material_row=material, material_body=material_body,
                    blob_store=_ArchiveCAS(), resolve_definition=archived_definition,
                    resolve_candidate=archived_candidate, resolve_artifact=archived_artifact,
                    error_code="invalid_archive")
            except Fault as exc:
                if exc.code == "invalid_archive":
                    raise
                raise Fault("invalid_archive", "Output observed execution material is invalid",
                            str(exc)) from exc
            return {"mode": "observed_result", "content": observed,
                    "payload": relation["payload"], "material": material,
                    "runtime_check": relation.get("runtime_check"),
                    "current": False, "dependencies": relation.get("dependencies", [])}

        def archived_output_material(row: dict[str, Any]) -> None:
            """Run the live output validator against portable archive rows."""
            material_body = row.get("body")
            if isinstance(material_body, str):
                material_body = parse_json(material_body, limit=MAX_OBJECT_BYTES)
            required_envelope = {"format", "material_kind", "project", "origin",
                                 "semantic_digest", "payload_blob", "dependency_refs",
                                 "captured_from"}
            need(isinstance(material_body, dict) and set(material_body) == required_envelope and
                 material_body.get("format") == MATERIAL_FORMAT and
                 material_body.get("material_kind") == OUTPUT_MATERIAL_KIND and
                 material_body.get("project") == project and
                 _sha(material_body.get("semantic_digest")) and
                 _sha(material_body.get("payload_blob")) and
                 digest(material_body) == row.get("body_digest", row.get("digest")),
                 "invalid_archive", "Output material envelope differs")
            need(blob_get is not None, "invalid_archive",
                 "Output material CAS read boundary is unavailable")

            class _OutputArchiveCAS:
                def blob_get(self, ident):
                    if callable(blob_get):
                        value = blob_get(ident)
                    elif isinstance(blob_get, dict):
                        value = blob_get.get(ident)
                    else:
                        value = None
                    need(isinstance(value, (bytes, bytearray)) and digest(bytes(value)) == ident,
                         "invalid_archive", "Output material CAS child is missing or changed", ident)
                    return bytes(value)

            archive_cas = _OutputArchiveCAS()
            payload = parse_json(archive_cas.blob_get(material_body["payload_blob"]),
                                 limit=MAX_OBJECT_BYTES)
            need(isinstance(payload, dict) and digest(payload) == material_body["semantic_digest"],
                 "invalid_archive", "Output material payload identity differs")
            output = payload.get("output")
            need(isinstance(output, dict), "invalid_archive", "Output material has no output record")
            public_ref = {"kind": "output_artifact", "project": project,
                          "delivery": payload.get("delivery_ref"),
                          "check": payload.get("check_ref"),
                          "observed": payload.get("observed_ref"),
                          "output_id": output.get("id"),
                          "output_digest": digest(output)}

            def resolve_nested(nested):
                kind = semantic_kind(nested)
                if kind == "delivery_snapshot":
                    return archived_delivery(nested)
                if kind == "delivery_check":
                    return archived_definition(nested)
                if kind == "observed_result":
                    return archived_observed(nested)
                raise Fault("invalid_archive", "Output material has an unsupported dependency", kind)

            relation = validate_output_material(public_ref, output_payload=payload,
                                                resolve_ref=resolve_nested, load_blob=archive_cas)
            stored_dependencies = material_body.get("dependency_refs")
            need(isinstance(stored_dependencies, list), "invalid_archive",
                 "Output material dependencies are malformed")
            normalized_stored = [validate_typed_ref(item, project=project)
                                 for item in stored_dependencies]
            need([_material_identity(item) for item in normalized_stored] ==
                 [_material_identity(item) for item in relation["dependencies"]],
                 "invalid_archive", "Output material dependencies differ")

        def endpoint(raw: dict[str, Any], *, allow_legacy_identity: bool = False) -> None:
            kind = semantic_kind(raw)
            if kind == "artifact":
                artifact = external("artifacts", raw["artifact"])
                accepted = (artifact is not None and artifact.get("project") == project and
                            artifact.get("status") == "accepted")
                if accepted:
                    revision = external("revisions", canonical([raw["artifact"], raw["revision"]]).decode())
                    accepted = (revision is not None and revision.get("artifact") == raw["artifact"] and
                                revision.get("revision") == raw["revision"] and revision.get("digest") == raw["body_digest"] and
                                digest(revision.get("body")) == raw["body_digest"])
                if not accepted and allow_legacy_identity and legacy_identity_matches(raw):
                    return
                need(accepted, "invalid_archive", "Assurance artifact endpoint is missing or changed", raw["artifact"])
            elif kind == "source":
                source = external("sources", raw["source"])
                need(source is not None and source.get("project") == project and source.get("blob") == raw["blob_digest"],
                     "invalid_archive", "Assurance source endpoint is missing or changed", raw["source"])
            elif kind == "traceability_ref":
                locator = raw["locator"]
                if locator.get("ref_type") == "source_span":
                    source = external("sources", locator["source_id"])
                    need(source is not None and source.get("project") == project and source.get("blob") == locator["blob_digest"],
                         "invalid_archive", "Assurance source span endpoint is missing or changed", locator["source_id"])
                elif locator.get("ref_type") == "artifact_ac":
                    artifact = external("artifacts", locator["artifact"])
                    need(artifact is not None and artifact.get("project") == project and artifact.get("status") == "accepted",
                         "invalid_archive", "Assurance artifact AC endpoint is missing or not accepted", locator["artifact"])
                    revision = external("revisions", canonical([locator["artifact"], locator["revision"]]).decode())
                    need(revision is not None and revision.get("digest") == locator["body_digest"] and
                         digest(revision.get("body")) == locator["body_digest"],
                         "invalid_archive", "Assurance artifact AC revision differs", locator["artifact"])
            elif kind == "change":
                change = external("changes", raw["change"])
                need(change is not None and change.get("project") == project and
                     change.get("revision") == raw["revision"],
                     "invalid_archive", "Assurance change endpoint is missing or changed", raw["change"])
                change_body = change.get("body")
                change_body = parse_json(change_body, limit=MAX_OBJECT_BYTES) if isinstance(change_body, str) else change_body
                need(isinstance(change_body, dict) and digest(change_body) == raw["body_digest"],
                     "invalid_archive", "Assurance change body digest differs", raw["change"])
                pin = raw.get("pin")
                material = objects.get(pin.get("id")) if isinstance(pin, dict) else None
                need(material is not None and material.get("kind") == "material" and
                     material.get("project") == project and material.get("digest") == pin.get("digest"),
                     "invalid_archive", "Assurance change material pin is missing or changed", raw["change"])
                material_body = material.get("body")
                material_body = parse_json(material_body, limit=MAX_OBJECT_BYTES) if isinstance(material_body, str) else material_body
                need(isinstance(material_body, dict) and material_body.get("material_kind") == "change",
                     "invalid_archive", "Assurance change material kind differs", raw["change"])
            elif kind == "assurance_object":
                target = objects.get(raw["object"])
                need(target is not None and target.get("project") == project and
                     target.get("kind") == raw["object_kind"] and target.get("digest") == raw["object_digest"],
                     "invalid_archive", "Assurance object dependency is missing or changed", raw["object"])
            elif kind == "observed_result":
                receipt_row = external("receipts", raw["receipt"])
                run_row = external("runs", raw["run"])
                need(receipt_row is not None and run_row is not None,
                     "invalid_archive", "Observed execution receipt or run is missing")
                need(receipt_row.get("project") == project and run_row.get("project") == project and
                     receipt_row.get("run") == raw["run"] and
                     receipt_row.get("binding") == raw["run_binding"],
                     "invalid_archive", "Observed execution row identity differs")
                observed = receipt_row.get("body")
                if isinstance(observed, str):
                    observed = parse_json(observed, limit=MAX_OBJECT_BYTES)
                run_body = run_row.get("body")
                if isinstance(run_body, str):
                    run_body = parse_json(run_body, limit=MAX_OBJECT_BYTES)
                run_result = run_row.get("result")
                if isinstance(run_result, str):
                    run_result = parse_json(run_result, limit=MAX_OBJECT_BYTES)
                need(isinstance(observed, dict) and isinstance(run_body, dict) and isinstance(run_result, dict),
                     "invalid_archive", "Observed execution record is incomplete")
                try:
                    execution_record_consistency(run_row, run_body, run_result, receipt_row, observed)
                except Fault as exc:
                    raise Fault("invalid_archive", "Observed execution record failed archive consistency", str(exc)) from exc
                need(digest(observed) == raw["receipt_digest"] and
                     observed.get("snapshot") == raw["snapshot_digest"] and
                     digest(observed.get("result", {})) == raw["result_digest"],
                     "invalid_archive", "Observed result identity differs")
                pin = observed.get("verification_material")
                need(isinstance(pin, dict) and set(pin) == {"id", "digest"},
                     "invalid_archive", "Observed execution material pin is missing")
                material = external("assurance_objects", pin["id"])
                need(material is not None and material.get("project") == project and
                     material.get("kind") == "material" and material.get("digest") == pin["digest"],
                     "invalid_archive", "Observed execution material is missing")
                material_body = material.get("body")
                if isinstance(material_body, str):
                    material_body = parse_json(material_body, limit=MAX_OBJECT_BYTES)
                if blob_get is None:
                    raise Fault("invalid_archive", "Observed execution material CAS read boundary is unavailable")

                class _ArchiveCAS:
                    def blob_get(self, ident):
                        if callable(blob_get):
                            value = blob_get(ident)
                        elif isinstance(blob_get, dict):
                            value = blob_get.get(ident)
                        else:
                            value = None
                        need(isinstance(value, (bytes, bytearray)) and digest(bytes(value)) == ident,
                             "invalid_archive", "Observed execution CAS child is missing or changed", ident)
                        return bytes(value)

                try:
                    archive_cas = _ArchiveCAS()
                    validate_execution_material_relation(
                        project=project, ref=raw, run_row=run_row, run_body=run_body,
                        observed=observed, material_row=material, material_body=material_body,
                        blob_store=archive_cas, resolve_definition=archived_definition,
                        resolve_candidate=archived_candidate, resolve_artifact=archived_artifact,
                        error_code="invalid_archive",
                    )
                except Fault as exc:
                    if exc.code == "invalid_archive":
                        raise
                    raise Fault("invalid_archive", "Observed execution material relation is invalid", str(exc)) from exc

        # Re-derive saved v2 Q from retained revisions, never current heads.
        def historical_responsibility_artifact(ref):
            artifact, body = archived_artifact(ref)
            return {**artifact, "revision":ref["revision"], "digest":ref["body_digest"], "body":body}
        for row in objects.values():
            body = saved_body(row)
            if body.get("format") != OBLIGATIONS_V2:
                continue
            scope_body = saved_body(saved_object(body["scope_ref"], "scope"))
            expected, inputs = [], list(scope_body["roots"])
            for ref in scope_body["roots"]:
                if ref["kind"] == "artifact":
                    artifact = historical_responsibility_artifact(ref)
                    records, dependencies = responsibility_records(ref, artifact["kind"], artifact["body"],
                        resolver=historical_responsibility_artifact)
                    dependencies, sources = artifact_dependency_closure(ref, historical_responsibility_artifact,
                        lambda ident:external("sources",ident))
                    for source in sources:
                        raw_source = blob_get(source["blob"])
                        need(isinstance(raw_source,bytes) and digest(raw_source)==source["blob"],
                             "invalid_archive", "Responsibility source CAS differs")
                    expected.extend(records); inputs.extend(dependencies)
                    acceptance = artifact["body"].get("acceptance", [])
                    need(type(acceptance) is list, "invalid_archive", "Acceptance is malformed")
                    for index, value in enumerate(acceptance):
                        expected.append({"id":"obligation:"+digest([ref,index,value]), "kind":"artifact_acceptance",
                            "source_ref":ref, "pointer":f"/acceptance/{index}", "value":value, "value_digest":digest(value)})
                elif ref["kind"] == "population":
                    context_rows = getattr(external_tables, "context_rows", {})
                    rows = context_rows.get("traceability_items", [])
                    for item in rows:
                        if item.get("revision") != ref["revision"] or not item.get("leaf"): continue
                        item_body = item["body"] if isinstance(item["body"], dict) else parse_json(item["body"])
                        item_ref = {"kind":"population_item", "project":project, "population":ref,
                                    "item":item["id"], "item_digest":item["digest"]}
                        expected.append({"id":"obligation:"+digest(item_ref), "kind":"population_leaf",
                            "source_ref":item_ref, "value_digest":item["digest"], "value":item_body})
            need(body["obligations"] == sorted(expected, key=lambda x:x["id"]) and
                 body["input_refs"] == sorted({canonical(x):x for x in inputs}.values(), key=canonical),
                 "invalid_archive", "Saved responsibility derivation differs from retained revisions")
        from .domain_responsibility import validate_domain_history
        domain_context = getattr(external_tables, "context_rows", None)
        if domain_context is None and isinstance(external_tables, dict):
            domain_context = {k:list(v.values()) if isinstance(v,dict) else v for k,v in external_tables.items()}
        if domain_context is not None:
            validate_domain_history(domain_context, project, historical_responsibility_artifact,
                lambda ident:external("sources",ident), blob_get)
        # Every immutable assurance object may carry typed dependencies.  The
        # closure rule is intentionally independent of the object kind and
        # body format: a legacy scope/obligations/profile shape with an
        # explicit typed endpoint must resolve that endpoint too.  Opaque
        # historical bodies have no typed dependency to verify and remain
        # readable as history; they are never used as a reason to skip a
        # dependency that is actually declared.
        for object_row in objects.values():
            body = object_row["body"]; body = parse_json(body) if isinstance(body, str) else body
            if isinstance(body, dict):
                if (object_row.get("kind") == "material" and
                        body.get("material_kind") == OUTPUT_MATERIAL_KIND):
                    archived_output_material(object_row)
                # Consumer-P has a stronger artifact endpoint contract than
                # ordinary assurance references: a freshly produced draft is
                # valid only when the same shared candidate/manifest resolver
                # proves its immutable producer history.  Validate this
                # material as one closed unit before the generic endpoint
                # walker (which intentionally keeps accepted-only artifact
                # semantics for existing E2 objects).
                if (object_row.get("kind") == "material" and
                        body.get("material_kind") == "artifact_production"):
                    from .artifact_provenance import (
                        MATERIAL_FORMAT as ARTIFACT_PROVENANCE_FORMAT,
                        MATERIAL_KIND, observed_ref,
                        validate_artifact_production_material,
                    )
                    need(body.get("format") == ARTIFACT_PROVENANCE_FORMAT and
                         body.get("material_kind") == MATERIAL_KIND,
                         "invalid_archive", "Artifact production material envelope differs")
                    context = archive_pinned_context()
                    need(context is not None, "invalid_archive",
                         "Artifact production candidate context is missing")
                    need(blob_get is not None, "invalid_archive",
                         "Artifact production material CAS reader is unavailable")

                    def archive_cas_get(ident):
                        try:
                            raw = blob_get(ident) if callable(blob_get) else blob_get.get(ident)
                        except (Fault, KeyError, OSError, TypeError, ValueError) as exc:
                            raise Fault("invalid_archive", "Artifact production CAS child cannot be read", ident) from exc
                        need(isinstance(raw, (bytes, bytearray)) and digest(bytes(raw)) == ident,
                             "invalid_archive", "Artifact production CAS child is missing or changed", ident)
                        return bytes(raw)

                    payload_blob = body.get("payload_blob")
                    payload = parse_json(archive_cas_get(payload_blob), limit=MAX_OBJECT_BYTES)
                    need(isinstance(payload, dict) and digest(payload) == body.get("semantic_digest"),
                         "invalid_archive", "Artifact production payload digest differs")
                    checked = validate_artifact_production_material(
                        payload, context=context,
                        resolve_artifact=archived_produced_artifact,
                        blob_get=archive_cas_get, project=project,
                        code="invalid_archive")
                    expected_dependencies = [
                        checked["task_ref"], checked["candidate_ref"],
                        observed_ref(checked["state"]), checked["artifact_ref"],
                    ]
                    actual_dependencies = [
                        {key: value for key, value in validate_typed_ref(item, project=project).items()
                         if key not in {"identity_digest", "semantic_kind"}}
                        for item in body.get("dependency_refs", [])
                    ]
                    need(actual_dependencies == expected_dependencies,
                         "invalid_archive", "Artifact production material dependencies differ")
                    continue
                format_name = body.get("format")
                allow_legacy_identity = not (isinstance(format_name, str) and format_name.startswith("assurance."))
                if (object_row.get("kind") == "profile" and
                        _is_canonical_profile(format_name)):
                    for authority in body.get("authority_refs", []):
                        need(authority.get("kind") in {"source", "change", "artifact"},
                             "invalid_archive", "Profile authority reference family is unsupported")
                        if authority.get("kind") == "artifact":
                            artifact = external("artifacts", authority["artifact"])
                            need(artifact is not None and artifact.get("kind") == "decision",
                                 "invalid_archive", "Profile authority artifact is not a decision",
                                 authority.get("artifact"))
                for _path, raw in _walk_refs(body):
                    endpoint(raw, allow_legacy_identity=allow_legacy_identity)
    events = {row["id"]: row for row in tables.get("assurance_events", [])}
    for row in events.values():
        need(row.get("project") == project and row.get("subject_id") in objects, "invalid_archive", "Assurance event subject is missing")
        need(objects[row["subject_id"]]["digest"] == row["subject_digest"], "invalid_archive", "Assurance event subject digest differs")
        need(row["event_kind"] in {"adopt", "withdraw", "supersede"}, "invalid_archive", "Assurance event kind is invalid")
        body = row.get("body"); body = parse_json(body) if isinstance(body, str) else body
        need(isinstance(body, dict), "invalid_archive", "Assurance event body is invalid")
        object_row = objects[row["subject_id"]]
        object_body = object_row.get("body")
        object_body = parse_json(object_body, limit=MAX_OBJECT_BYTES) if isinstance(object_body, str) else object_body
        if object_row.get("kind") == "profile" and _is_canonical_profile(object_body.get("format")):
            previous_ref = None
            if row.get("previous") is not None:
                previous_event = events.get(row["previous"])
                need(previous_event is not None, "invalid_archive", "Profile event predecessor is missing")
                previous_object = objects.get(previous_event.get("subject_id"))
                need(previous_object is not None and previous_object.get("kind") == "profile" and
                     previous_object.get("digest") == previous_event.get("subject_digest") and
                     previous_object.get("logical_id") == object_row.get("logical_id"),
                     "invalid_archive", "Profile event predecessor subject differs")
                previous_ref = {
                    "kind": "assurance_object", "project": project,
                    "object": previous_object["id"], "object_kind": "profile",
                    "object_digest": previous_object["digest"],
                }
            _validate_profile_event_predecessor(
                row, body, object_body, previous_ref, code="invalid_archive",
            )
    head_rows = tables.get("assurance_heads", [])
    heads = {(r["project"], r["logical_id"]): r for r in head_rows}
    need(len(heads) == len(head_rows), "invalid_archive", "Duplicate assurance head")
    event_logicals = {(row["project"], objects[row["subject_id"]]["logical_id"]) for row in events.values()}
    need(event_logicals <= set(heads), "invalid_archive", "Assurance event history has no head projection")
    for key, row in heads.items():
        need(row["project"] == project and row["head_event"] in events, "invalid_archive", "Assurance head is dangling")
    for row in events.values():
        if row.get("previous") is not None:
            need(row["previous"] in events, "invalid_archive", "Assurance event chain is dangling")
            previous = events[row["previous"]]
            need(previous["project"] == row["project"] and
                 objects[previous["subject_id"]]["logical_id"] == objects[row["subject_id"]]["logical_id"],
                 "invalid_archive", "Assurance event chain crosses logical identities")
        expected = row.get("expected_head")
        if expected is not None: need(expected == row.get("previous"), "invalid_archive", "Assurance event compare-and-swap record differs")
    # A historical stream may contain events for multiple logical IDs; the
    # head projection still must point at the newest event in that chain.
    for (p, logical), head in heads.items():
        subject = objects[events[head["head_event"]]["subject_id"]]
        candidates = [row for row in objects.values() if row["project"] == p and row["logical_id"] == logical]
        need(candidates and subject["logical_id"] == logical, "invalid_archive", "Assurance head logical identity differs")
        incoming = {row.get("previous") for row in events.values() if row["project"] == p and
                    objects[row["subject_id"]]["logical_id"] == logical and row.get("previous") is not None}
        need(head["head_event"] not in incoming, "invalid_archive", "Assurance head is not the chain tip")
        seen_events = set(); cursor = head["head_event"]
        while cursor is not None:
            need(cursor not in seen_events, "invalid_archive", "Assurance event chain contains a cycle")
            seen_events.add(cursor); cursor = events[cursor].get("previous")
        chain_events = {row["id"] for row in events.values() if row["project"] == p and
                        objects[row["subject_id"]]["logical_id"] == logical}
        need(seen_events == chain_events, "invalid_archive", "Assurance head omits an event in its logical history")
