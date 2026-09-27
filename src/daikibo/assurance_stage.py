"""Read-only Unit 3 stage evaluation.

Unit 3 is the common *reader* for the E3 assurance contract.  It is kept in
its own module so that the workflow writers can later call the same code from
inside their transactions without making the public report a second gate
implementation.  Nothing in this module creates a pin, packet, receipt,
review, head, event, candidate, or workflow gate row.

The plan and Task relation consumers use the accepted controller-backed M/R
adapters in this module.  Integration and delivery connect the finite
produced_by/contains Consumer-C boundary through the same adapters; other
profile relations remain explicit capability boundaries.
"""
from __future__ import annotations

from .assurance_profile_contract import PROFILE_V4_FORMAT, PROFILE_V5_FORMAT, CANONICAL_PROFILE_FORMATS, profile_registry
from .assurance import _validate_canonical_profile_wire

from typing import Any, Iterable

from .assurance import (
    PROFILE_NODE_SELECTORS,
    PROFILE_STAGES,
    PROFILE_V2_FORMAT,
    PROFILE_V3_FORMAT,
    SET_UNIVERSAL_CRITERIA,
    _validate_profile_v2_wire,
    _validate_profile_v3_wire,
)
from .assurance_denominators import (
    DELIVERY_DECLARED_OUTPUT_CATEGORY,
    STAGES,
    _make_checkpoint_plan,
    collect_stage_context,
    derive_denominator,
    project_global_checkpoint,
    project_task,
    project_task_checkpoint,
    semantic_definition_projection,
)
from .assurance_node_reviews import build_node_requests, select_node_reviews
from .assurance_criteria import (
    _observed_definition_ref,
    _observed_execution,
    build_relation_request,
    build_review_assurance,
    evaluate_criteria,
)
from .assurance_relations import (
    REGISTRY_V1_DIGEST,
    REGISTRY_V2_DIGEST,
    registry_entry,
    validate_typed_ref,
)
from .common import Fault, canonical, digest, need, parse_json
from .observed_receipts import ordered_observed_receipts
from .task_revisions import task_definition_digest


CHECKPOINTS: dict[str, tuple[str, ...]] = {
    "plan": ("plan",),
    "task": ("ready", "claim", "execute", "candidate", "complete", "recheck"),
    "integration": ("certify", "commit_pre"),
    "delivery": ("finalize", "finish", "export"),
}
DEFAULT_CHECKPOINT = {
    "plan": "plan", "task": "complete", "integration": "certify", "delivery": "finalize",
}
PRECOMPLETION = {
    # Unit B's closure gate is a completion concern.  The candidate
    # checkpoint remains pre-completion even though candidate material itself
    # becomes a current execution obligation there.
    "task": frozenset({"ready", "claim", "execute", "candidate"}),
    "integration": frozenset({"certify", "commit_pre"}),
    "delivery": frozenset(),
}
EXECUTION_PRECOMPLETION = {
    "task": frozenset({"ready", "claim", "execute"}),
    "integration": frozenset(),
    "delivery": frozenset(),
}
TASK_FUTURE_EXECUTION_CODES = frozenset({
    "candidate_missing", "candidate_snapshot_missing", "implementation_run_missing",
})
INTEGRATION_FUTURE_EXECUTION_CODES = frozenset({
    "delivery_repository_missing", "delivery_repository_observation_missing",
})
STATUS_ORDER = ("failed", "stale", "missing", "unverified", "unknown", "unsupported")
REPORT_STATUSES = STATUS_ORDER + ("satisfied", "deferred", "not_applicable")
FORMAT = "daikibo.assurance-stage-result.v1"
REPORT_FORMAT = "daikibo.assurance-stage-report.v1"
VERSION = "unit3-readonly.v1"

# Unit 2b's checkpoint scheduler is deliberately a closed table.  The table
# describes when a relation population becomes evidence-bearing; it does not
# change the selected profile or the thirteen persisted registry contracts.
CHECKPOINT_RELATION_RULES = {
    "extracted_from": {"first": "ready", "producer": "accepted_source"},
    "decomposes": {"first": "ready", "producer": "accepted_requirement"},
    "realizes": {"first": "ready", "producer": "accepted_design"},
    "implements": {"first": "ready", "producer": "fixed_implementation_input"},
    "verifies": {"first": "ready", "producer": "task_definition_or_delivery_definition"},
    "exercises": {"first": "ready", "producer": "fixed_exercise_input"},
    "execution_of": {"first": "complete", "producer": "task_execution_observation"},
    "assigned_to": {"first": "ready", "producer": "saved_assignment"},
    "produced_by": {"first": "complete", "producer": "task_candidate_or_fixed_output"},
    "migrated_to": {"first": "complete", "producer": "task_migration_mapping"},
    "depends_on": {"first": "ready", "producer": "saved_dependency"},
    "affects": {"first": "ready", "producer": "saved_impact_inventory"},
    "contains": {"first": "integration", "producer": "delivery_snapshot_output"},
}
def _plain(value: Any) -> Any:
    """Remove resolver-only projections before identity/fingerprint work."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()
                if key not in {"identity_digest", "semantic_kind"}}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _copy(value: Any) -> Any:
    return parse_json(canonical(value))


def _status(status: str, reason: str, *, required: bool = True, **details: Any) -> dict[str, Any]:
    need(status in REPORT_STATUSES, "invalid_stage_result", "Unknown stage result status", status)
    result = {"status": status, "required": required, "reason": reason}
    for key, value in details.items():
        if value is not None:
            result[key] = _copy(value)
    return result


def _unresolved(code: str, reason: str, *, status: str = "unverified", **details: Any) -> dict[str, Any]:
    return _status(status, reason, code=code, **details)


def _aggregate(values: Iterable[dict[str, Any]], *, empty: str = "unverified") -> str:
    statuses = [item.get("status") for item in values if item.get("required", True)]
    if not statuses:
        return empty
    if all(value in {"satisfied", "not_applicable"} for value in statuses):
        return "satisfied"
    for value in STATUS_ORDER:
        if value in statuses:
            return value
    if "deferred" in statuses:
        return "deferred"
    return "unknown"


def _failure_items(component: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    status = result.get("status")
    if status in {"satisfied", "deferred", "not_applicable"} or not result.get("required", True):
        return []
    item = {"kind": component, "owner": result.get("owner", ""),
            "obligation_id": result.get("obligation_id", ""),
            "status": status, "reason": result.get("reason", status)}
    for key in ("code", "task", "program", "relation", "selector", "node_ref", "details"):
        if key in result:
            item[key] = _copy(result[key])
    return [item]


def _future_execution_code(stage: str, checkpoint: str, code: str) -> bool:
    """Whether an unresolved execution item belongs to a later checkpoint."""
    if stage == "task" and checkpoint in EXECUTION_PRECOMPLETION["task"]:
        return code in TASK_FUTURE_EXECUTION_CODES
    if stage == "integration" and checkpoint in CHECKPOINTS["integration"]:
        return code in INTEGRATION_FUTURE_EXECUTION_CODES
    return False


def _obligation_structural_item(context: dict[str, Any], obligation: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a Task structural declaration through the saved pointer."""
    pointer = obligation.get("pointer") if isinstance(obligation, dict) else None
    if type(pointer) is not str:
        return None
    parts = pointer.split("/")
    if (len(parts) != 6 or parts[0] != "" or parts[1] != "tasks" or
            parts[3] != "structural_obligations" or parts[4] not in
            {"required_outputs", "required_exercises"}):
        return None
    task = next((item for item in context.get("task_definitions", [])
                 if isinstance(item, dict) and item.get("id") == parts[2]), None)
    structural = task.get("structural_obligations") if isinstance(task, dict) else None
    if not isinstance(structural, dict):
        return None
    values = structural.get(parts[4], [])
    try:
        index = int(parts[5])
    except (TypeError, ValueError):
        return None
    if not isinstance(values, list) or index < 0 or index >= len(values):
        return None
    return values[index] if isinstance(values[index], dict) else None


def classify_relation_obligation(relation: str, *, stage: str, checkpoint: str,
                                 obligation: dict[str, Any],
                                 owner_refs: list[dict[str, Any]],
                                 context: dict[str, Any]) -> dict[str, Any]:
    """Classify one selected relation obligation without consulting status.

    This is the pure closed routing table used by the sealed request adapter.
    It receives only canonical denominator material and saved owner/producer
    declarations.  A missing observation therefore never becomes a reason to
    defer a fixed obligation; it remains ``unknown`` and blocks the request.
    """
    rule = CHECKPOINT_RELATION_RULES.get(relation)
    if (rule is None or stage not in STAGES or type(checkpoint) is not str or
            checkpoint not in CHECKPOINTS.get(stage, ())):
        return {"classification": "unknown", "first_required_checkpoint": "unknown",
                "producer_kind": "unknown", "reason": "closed_relation_or_checkpoint_unknown"}
    category = obligation.get("category")
    producer = rule["producer"]
    first = rule["first"]
    future = False
    reason = "saved_definition_is_required_at_checkpoint"

    # Relation-specific endpoint/producer rules.  These are based on retained
    # declaration identity and owner material, never on Task.status or on an
    # absent edge/evidence row.
    if relation == "verifies" and category == "delivery_check":
        future, first, producer, reason = True, "integration", "delivery_definition", "delivery_definition_belongs_to_later_stage"
    elif relation in {"produced_by", "contains"} and category == "delivery_declared_output":
        stage_order = {name: index for index, name in enumerate(STAGES)}
        # A saved Delivery snapshot/check/output is already a fixed relation
        # population at integration.  Only the later Git actual material may
        # remain future there; output relation review is required now and is
        # still required at Delivery.  Plan/Task retain the later-stage
        # deferral because the Delivery population is not yet present.
        future = stage_order.get(stage, -1) < stage_order["integration"]
        first, producer, reason = "integration", "delivery_snapshot_output", "delivery_output_relation_required_from_integration"
    elif relation == "execution_of" and category in {"required_check", "delivery_check"}:
        future = (stage in {"plan", "task"} and
                  not (stage == "task" and checkpoint in {"complete", "recheck"}))
        reason = "task_execution_observation_is_required_at_complete" if future else "task_execution_observation_checkpoint_reached"
    elif relation == "produced_by" and category == "required_output":
        declaration = _obligation_structural_item(context, obligation)
        if declaration is None:
            return {"classification": "unknown", "first_required_checkpoint": first,
                    "producer_kind": producer, "reason": "required_output_declaration_unresolved"}
        realization = declaration.get("realization_kind")
        if realization == "candidate_member":
            future = (stage in {"plan", "task"} and
                      not (stage == "task" and checkpoint in {"complete", "recheck"}))
            producer = "task_candidate"
            reason = "candidate_output_requires_completion_material" if future else "candidate_output_checkpoint_reached"
        elif realization == "artifact":
            # ``artifact_refs`` on a Task output are the declared source /
            # responsibility inputs.  They do not prove that the output
            # already exists.  A Task-revision owner therefore denotes a
            # future artifact-production result, while an artifact-owned
            # declaration remains a fixed current input.  Keep this decision
            # tied to the saved producer owner rather than status, candidate
            # presence, or a caller-provided artifact string.
            task_owners = [owner for owner in owner_refs
                           if isinstance(owner, dict) and
                           owner.get("kind") == "task_revision"]
            source_ref = obligation.get("source_ref")
            task_owned = bool(task_owners) and (
                not isinstance(source_ref, dict) or
                (source_ref.get("kind") == "task_revision" and
                 any(canonical(owner) == canonical(source_ref)
                     for owner in task_owners))
            )
            fixed_owners = [owner for owner in owner_refs
                            if isinstance(owner, dict) and owner.get("kind") in {
                                "artifact", "source", "output_artifact",
                            }]
            if task_owned:
                future = (stage in {"plan", "task"} and
                          not (stage == "task" and checkpoint in {"complete", "recheck"}))
                producer = "task_artifact"
                reason = ("task_artifact_output_requires_completion_material"
                          if future else "task_artifact_output_checkpoint_reached")
            elif fixed_owners and len(fixed_owners) == len(owner_refs):
                first = "ready"
                producer = "fixed_artifact_input"
                reason = "saved_owner_is_an_existing_fixed_artifact"
            else:
                return {"classification": "unknown", "first_required_checkpoint": first,
                        "producer_kind": "unknown", "reason": "artifact_producer_owner_unknown"}
        else:
            return {"classification": "unknown", "first_required_checkpoint": first,
                    "producer_kind": "unknown", "reason": "required_output_producer_kind_unknown"}
    elif relation == "migrated_to" and category == "population_leaf":
        future = (stage in {"plan", "task"} and
                  not (stage == "task" and checkpoint in {"complete", "recheck"}))
        producer = "task_migration_mapping"
        reason = "migration_target_is_a_later_task_result" if future else "migration_mapping_checkpoint_reached"
    elif relation == "exercises" and category == "required_exercise":
        # The structural contract names accepted artifact/path inputs.  It is
        # therefore current by default; an untyped future candidate has no
        # denominator identity and is rejected as an unknown producer rather
        # than being inferred from a missing observation.
        producer = "fixed_exercise_input"
        reason = "exercise_declaration_names_fixed_input"
    elif relation == "implements" and category in {
            "artifact_responsibility", "artifact_structural_responsibility"}:
        producer = "fixed_implementation_input"
        reason = "implementation_obligation_is_a_saved_fixed_input"
    elif relation in {"extracted_from", "decomposes", "realizes", "assigned_to", "affects"}:
        reason = "saved_definition_or_assignment_is_current"
    elif relation == "depends_on":
        reason = "saved_dependency_is_current"
    if future:
        return {"classification": "deferred_future", "first_required_checkpoint": first,
                "producer_kind": producer, "reason": reason}
    # For a Task checkpoint, relation definitions introduced at a later
    # workflow stage cannot be required now.  Keep those obligations in the
    # future partition even when their observation happens to exist.
    stage_order = {name: index for index, name in enumerate(STAGES)}
    if stage == "task" and first in stage_order and stage_order[first] > stage_order[stage]:
        return {"classification": "deferred_future", "first_required_checkpoint": first,
                "producer_kind": producer, "reason": "relation_first_required_stage_is_later"}
    return {"classification": "required_now", "first_required_checkpoint": first,
            "producer_kind": producer, "reason": reason}


def _relation_schedule(context: dict[str, Any], denominator: dict[str, Any],
                       request: Any, *, stage: str, checkpoint: str) -> dict[str, Any]:
    """Build the relation population schedule from a sealed request."""
    by_id = {item.get("id"): item for item in denominator.get("obligations", [])}
    owners = {item.get("obligation_id"): item.get("owners", [])
              for item in request.get("owner_mapping", [])}
    relation = request.get("relation")
    entries: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for obligation_id in request.get("required_obligation_ids", []):
        obligation = by_id.get(obligation_id)
        owner_refs = owners.get(obligation_id, [])
        if obligation is None or not isinstance(owner_refs, list) or not owner_refs:
            unknown.append(_unresolved(
                "checkpoint_relation_owner_unknown",
                "Relation request obligation has no canonical saved owner",
                status="unverified", relation=relation, obligation_id=obligation_id,
            ))
            continue
        classified = classify_relation_obligation(
            relation, stage=stage, checkpoint=checkpoint,
            obligation=obligation, owner_refs=owner_refs, context=context,
        )
        if classified["classification"] == "unknown":
            unknown.append(_unresolved(
                "checkpoint_relation_classification_unknown",
                classified["reason"], status="unverified", relation=relation,
                obligation_id=obligation_id,
            ))
            continue
        entries.append({"obligation_id": obligation_id,
                        "classification": classified["classification"],
                        "first_required_checkpoint": classified["first_required_checkpoint"],
                        "producer_kind": classified["producer_kind"],
                        "reason": classified["reason"],
                        "owner_refs": _copy(owner_refs)})
    entries.sort(key=lambda item: item["obligation_id"])
    population = [item["obligation_id"] for item in entries]
    now = sorted(item["obligation_id"] for item in entries
                 if item["classification"] == "required_now")
    future = sorted(item["obligation_id"] for item in entries
                    if item["classification"] == "deferred_future")
    return {"population_ids": population, "required_now_ids": now,
            "deferred_future_ids": future, "schedule": entries,
            "unknown": unknown}


def _copy_unresolved(item: dict[str, Any]) -> dict[str, Any]:
    """Retain a controller diagnostic without carrying resolver internals."""
    return _unresolved(
        item.get("code", "context_unresolved"), item.get("reason", "context unresolved"),
        status="missing" if str(item.get("code", "")).endswith("missing") else "unverified",
        **{key: item.get(key) for key in
           ("task", "artifact", "source", "candidate", "repository", "run", "breakdown", "index")
           if key in item},
    )


def _ref(value: Any, project: str, expected: set[str] | None = None, *, name: str) -> dict[str, Any]:
    need(type(value) is dict, "invalid_stage_context", f"{name} must be a typed reference")
    return _plain(validate_typed_ref(value, project=project, expected_kinds=expected))


def _profile_registry(selection: dict[str, Any], body: dict[str, Any]) -> str:
    """Return the registry pinned by one validated canonical profile.

    The selected profile event is the authority for the profile identity and
    its reported effective registry.  A caller cannot choose a registry at
    the stage boundary: v2 is the immutable v1 relation wire and v3 is the
    output-aware v2 wire.  Keeping this check beside profile decoding makes
    every downstream reader consume one effective value.
    """
    profile_format = body.get("format")
    need(type(profile_format) is str and profile_format in CANONICAL_PROFILE_FORMATS,
         "invalid_profile", "Selected profile format is unsupported", profile_format)
    expected = profile_registry(profile_format)
    need(selection.get("profile_format") == profile_format,
         "integrity_error", "Selected profile format differs from its canonical selection")
    selected_registry = selection.get("effective_relation_contract_digest")
    need(type(selected_registry) is str and selected_registry == expected,
         "invalid_registry", "Selected profile effective registry is inconsistent")
    return expected


def _profile(control: Any, actor: Any, project: str, program: str) -> tuple[
    dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str | None
]:
    """Return selection, decoded profile, registry, and a read-only result."""
    selection = control.assurance.selected_profile(actor, project, program)
    profile_ref = selection.get("profile_ref")
    if profile_ref is None:
        return selection, None, None, None
    try:
        profile = control.assurance._object_by_ref(profile_ref, project, kinds={"profile"})
        _validate_canonical_profile_wire(profile["body"])
        need(profile["body"].get("program") == program and profile["body"].get("project") == project,
             "integrity_error", "Selected profile belongs to another program")
        effective_registry = _profile_registry(selection, profile["body"])
        # A selected profile is a current semantic dependency.  This is a
        # read-only check; _ensure_object_current performs no storage write.
        control.assurance._ensure_object_current(actor, project, profile, require_self=True)
        return selection, profile, None, effective_registry
    except Fault as exc:
        status = "stale" if exc.code in {"stale_reference", "stale_set", "stale_evidence", "set_incomplete"} else "unknown"
        return selection, None, _unresolved(exc.code, str(exc), status=status,
                                            profile_ref=profile_ref), None


def _task_row(control: Any, actor: Any, project: str, task_ref: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve one exact Task revision without accepting a raw task body."""
    row = control.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (task_ref.get("task"), project))
    if row is None:
        return None, _unresolved("task_missing", "Task revision is not retained", status="missing",
                                 task=task_ref)
    try:
        body = parse_json(row["body"])
        current = {"kind": "task_revision", "project": project, "task": row["id"],
                   "revision": row["revision"], "definition_digest": task_definition_digest(body)}
        if current["revision"] != task_ref.get("revision") or current["definition_digest"] != task_ref.get("definition_digest"):
            return row, _unresolved("task_revision_stale", "Task selector is not the current retained revision",
                                    status="stale", task=task_ref, current=current)
        return row, _status("satisfied", "task_revision_current")
    except Fault as exc:
        return row, _unresolved(exc.code, str(exc), status="unknown", task=task_ref)
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        return row, _unresolved("task_body_invalid", str(exc), status="unknown", task=task_ref)


def _task_programs(control: Any, project: str, task_id: str) -> list[str]:
    """Enumerate the public Task program view used by stage reads.

    The workflow field remains a compatibility projection for the public
    evaluator.  Private candidate admission uses ``_active_task_programs``
    below so that an optional workflow value cannot create an admission
    membership by itself.
    """
    programs: set[str] = set()
    trace = getattr(control, "traceability", None)
    if trace is not None and hasattr(trace, "_programs_for_task"):
        programs.update(trace._programs_for_task(project, task_id))
    for row in control.s.all("SELECT body FROM tasks WHERE id=? AND project=?", (task_id, project)):
        body = parse_json(row["body"])
        value = body.get("workflow_id")
        if isinstance(value, str) and value:
            programs.add(value)
    return sorted(programs)


def _active_task_programs(control: Any, project: str, task_id: str) -> list[str]:
    """Return only active Breakdown memberships for private admission.

    A Task's optional ``workflow_id`` is caller data and is useful to legacy
    read paths, but it is not an authority for the private candidate gate.
    Runtime admission must start from the controller's retained active
    Breakdown assignments.  The ordinary evaluator then expands that seed to
    every canonical membership and applies its existing all-program AND.
    """
    trace = getattr(control, "traceability", None)
    if trace is None or not hasattr(trace, "_programs_for_task"):
        return []
    return sorted(trace._programs_for_task(project, task_id))


def _local_task_programs(control: Any, project: str, task_id: str,
                         local_execution: str | None) -> list[str]:
    """Resolve a canonical local proposal's program membership.

    A local proposal is a second canonical ownership path for a Task.  The
    selector is taken from the sealed current claim and its program is read
    from the immutable proposal row.  Current certification and reviews are
    rechecked by the stage component through the shared read-only
    authorization primitive.  A stale, withdrawn, foreign, or otherwise
    unverified proposal remains in the membership union so the selected
    profile cannot be bypassed; the stage result preserves its concrete
    failure.
    """
    if not isinstance(local_execution, str) or not local_execution:
        return []
    row = control.s.one(
        "SELECT project,program FROM local_execution_proposals WHERE id=?",
        (local_execution,),
    )
    if row is None or row["project"] != project:
        return []
    program = row["program"]
    if not isinstance(program, str) or not program:
        return []
    # The immutable proposal row is the owner of this selector.  Confirm that
    # the owner is still a project program before it can enter the all-program
    # stage evaluation.
    if control.s.one(
        "SELECT id FROM programs WHERE id=? AND project=?",
        (program, project),
    ) is None:
        return []
    # Retain the canonical proposal owner in the membership union even when a
    # later readonly authorization read reports stale/withdrawn material.  The
    # stage component performs that read again and preserves the concrete
    # authorization failure; dropping this branch here would bypass a selected
    # profile when no active Breakdown owns the Task.
    return [program]


def _membership(control: Any, actor: Any, project: str, program: str,
                stage: str, task_ref: dict[str, Any] | None, *,
                active_only: bool = False,
                program_override: list[str] | None = None,
                ) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    if task_ref is None:
        return {"format": "daikibo.assurance-membership.v1", "state": "program",
                "programs": [program], "requested_program": program,
                "task": None, "all_programs_evaluated": [program]}, None, [program]
    row, task_state = _task_row(control, actor, project, task_ref)
    task_id = task_ref.get("task")
    if isinstance(task_id, str):
        if program_override is not None:
            programs = sorted({item for item in program_override
                               if isinstance(item, str) and item})
        else:
            programs = (_active_task_programs(control, project, task_id)
                        if active_only else _task_programs(control, project, task_id))
    else:
        programs = []
    state = "satisfied"
    failures: list[dict[str, Any]] = []
    if task_state.get("status") != "satisfied":
        state = task_state["status"]
        failures.append(task_state)
    if not programs:
        state = "unknown"
        failures.append(_unresolved("task_program_membership_missing",
                                    "Task has no canonical active program membership", status="unknown", task=task_ref))
    elif program not in programs:
        state = "unknown"
        failures.append(_unresolved("task_program_membership_mismatch",
                                    "Task is not assigned to the requested program", status="unknown",
                                    task=task_ref, requested_program=program, programs=programs))
    membership = {
        "format": "daikibo.assurance-membership.v1", "state": state,
        "programs": programs, "requested_program": program, "task": task_ref,
        "all_programs_evaluated": programs if program in programs else [program],
        "failures": failures,
    }
    return membership, row, (programs if program in programs else [program])


def _local_execution_proposed_breakdown(control: Any, project: str, program: str,
                                        selector: str | None) -> str | None:
    """Resolve a saved local proposal's composed root, if one exists."""
    if selector is None:
        return None
    row = control.s.one("SELECT * FROM local_execution_proposals WHERE id=?", (selector,))
    if row is None:
        return None
    need(row["project"] == project and row["program"] == program,
         "cross_project", "Local execution selector belongs to another project/program", selector)
    body = parse_json(row["body"])
    need(isinstance(body, dict) and digest(body) == row["digest"],
         "integrity_error", "Local execution proposal content differs", selector)
    subplan = body.get("subplan")
    need(isinstance(subplan, str) and subplan, "missing_evidence",
         "Local execution proposal has no composed subplan", selector)
    composition = control.s.one(
        "SELECT * FROM subplan_compositions WHERE subplan=? ORDER BY created DESC,id DESC LIMIT 1",
        (subplan,),
    )
    if composition is None:
        raise Fault("missing_evidence", "Local execution proposal composition is missing", selector)
    composition_body = parse_json(composition["body"])
    need(isinstance(composition_body, dict) and digest(composition_body) == composition["digest"],
         "integrity_error", "Local execution composition content differs", composition["id"])
    need(composition_body.get("subplan") == subplan and
         composition_body.get("breakdown") == composition["breakdown"],
         "integrity_error", "Local execution composition binding differs", composition["id"])
    subplan_row = control.s.one("SELECT project,program,digest FROM subplans WHERE id=?", (subplan,))
    need(subplan_row is not None and subplan_row["project"] == project and
         subplan_row["program"] == program and
         composition_body.get("subplan_digest") == subplan_row["digest"],
         "stale_reference", "Local execution subplan is not current", subplan)
    breakdown = control.s.one("SELECT * FROM breakdowns WHERE id=?", (composition["breakdown"],))
    if breakdown is None:
        raise Fault("missing_evidence", "Local execution composition root is missing", composition["breakdown"])
    need(breakdown["project"] == project and breakdown["program"] == program,
         "cross_project", "Local execution composition root belongs elsewhere", breakdown["id"])
    root_body = parse_json(breakdown["body"])
    need(digest(root_body) == breakdown["digest"],
         "integrity_error", "Local execution composition root differs", breakdown["id"])
    need(breakdown["status"] in {"proposed", "active"},
         "stale_reference", "Local execution composition root is not selectable", breakdown["id"])
    return breakdown["id"]


def _local_execution_state(control: Any, actor: Any, project: str, program: str,
                           stage: str, checkpoint: str,
                           task_ref: dict[str, Any] | None,
                           selector: str | None) -> dict[str, Any]:
    if selector is None:
        return _status("not_applicable", "local_execution_not_selected", required=False)
    need(type(selector) is str and selector and "\x00" not in selector,
         "invalid_stage_context", "local_execution selector must be a canonical id")
    row = control.s.one("SELECT * FROM local_execution_proposals WHERE id=?", (selector,))
    if row is None:
        return _unresolved("local_execution_missing", "Local execution proposal is not retained", status="missing",
                           local_execution=selector)
    if row["project"] != project or row["program"] != program:
        raise Fault("cross_project", "Local execution selector belongs to another project/program", selector)
    body = parse_json(row["body"])
    need(isinstance(body, dict) and digest(body) == row["digest"],
         "integrity_error", "Local execution proposal content differs", selector)
    tasks = body.get("tasks", []) if isinstance(body, dict) else []
    if task_ref is not None and task_ref.get("task") not in tasks:
        return _unresolved("local_execution_task_mismatch", "Local execution proposal does not include the selected Task",
                           status="unknown", local_execution=selector, task=task_ref)
    local = getattr(control, "local_executions", None)
    if local is None:
        return _unresolved("local_execution_unavailable", "Local execution authority is not attached", status="unsupported",
                           local_execution=selector)
    try:
        # This is the readonly local qualification primitive.  Unit4 writer
        # enforcement is a caller-owned wrapper and must not be re-entered
        # from this read, otherwise local certification would recurse through
        # the stage evaluator.
        audit = local._audit(actor, selector, True, readonly=True, enforce_plan=False)
    except Fault as exc:
        return _unresolved("local_execution_audit_failed", str(exc), status="unverified",
                           local_execution=selector)
    if audit.get("current") is not True:
        return _unresolved("local_execution_not_current", "Saved local execution proposal is not current",
                           status="stale", local_execution=selector,
                           details={"failures": audit.get("failures", [])})
    record = control.s.one("SELECT * FROM local_execution_records WHERE proposal=? AND kind='certified' ORDER BY created DESC,id DESC LIMIT 1", (selector,))
    if record is None:
        return _status("unverified", "local_execution_not_certified", local_execution=selector)
    certified = parse_json(record["body"])
    need(isinstance(certified, dict) and digest(certified) == record["digest"],
         "integrity_error", "Local execution certification content differs", record["id"])
    if (certified.get("proposal") != selector or
            certified.get("proposal_digest") != row["digest"] or
            certified.get("material_digest") != body.get("material_digest") or
            certified.get("current") is not True):
        return _unresolved("local_execution_certification_stale",
                           "Certification does not bind the current proposal material", status="stale",
                           local_execution=selector, record=record["id"])
    if task_ref is not None:
        try:
            # ``stage`` is the business context (plan/task/integration/delivery),
            # while the local authority primitive accepts the finite checkpoint
            # vocabulary.  Preserve that boundary at the shared readonly read;
            # passing ``task`` here would turn a valid candidate into an
            # authorization-shape failure.
            authorization = local.current_authorization_readonly(
                actor, task_ref["task"], checkpoint,
            )
        except Fault as exc:
            return _unresolved("local_execution_authorization_failed", str(exc), status="unverified",
                               local_execution=selector, task=task_ref)
        if (authorization is None or authorization.get("allowed") is not True or
                authorization.get("proposal") != selector or
                authorization.get("certification", {}).get("id") != record["id"]):
            return _unresolved("local_execution_authorization_not_current",
                               "Current Task authorization does not bind this local proposal",
                               status="stale", local_execution=selector, task=task_ref,
                               details={"authorization": authorization})
    return _status("satisfied", "local_execution_authorized", local_execution=selector,
                   record={"id": record["id"], "digest": record["digest"]},
                   audit_digest=digest(audit))


def _program_scoped_selector(control: Any, project: str, program: str,
                             selector: str | None, table: str) -> str | None:
    """Apply a caller selector only to its owning program in a multi-program read.

    A Task can be a member of more than one canonical program.  A local
    proposal or Breakdown selector names one program's material; reusing it
    for every AND branch would either cross-bind the denominator or turn a
    missing global branch into the local branch's result.  The owning branch
    consumes the selector, while other branches resolve their own active root
    (or retain a concrete missing result).  Foreign-project and unknown IDs
    remain visible to the normal validator instead of being silently ignored.
    """
    if selector is None:
        return None
    row = control.s.one(f"SELECT project,program FROM {table} WHERE id=?", (selector,))
    if row is None or row["project"] != project or row["program"] == program:
        return selector
    return None


def _node_refs(context: dict[str, Any], denominator: dict[str, Any], selector: str,
               task_ref: dict[str, Any] | None) -> list[dict[str, Any]]:
    refs: dict[bytes, dict[str, Any]] = {}
    # The selector names the population.  The review role is a separate
    # projection (for example ``component`` is reviewed by the minimum
    # ``design`` role, while its subject remains a component artifact).
    artifact_kinds = {
        "domain": {"domain"}, "accepted_domain": {"domain"},
        "requirement": {"requirement"}, "accepted_requirement": {"requirement"},
        "design": {"design"}, "accepted_design": {"design"},
        "component": {"component"}, "accepted_component": {"component"},
        "interface": {"interface"}, "accepted_interface": {"interface"},
        "test_artifact": {"test"}, "accepted_test_artifact": {"test"},
    }
    if selector in artifact_kinds:
        values = [item for item in context.get("artifacts", []) if item.get("kind") in artifact_kinds[selector]]
        for item in values:
            if isinstance(item.get("ref"), dict):
                refs[canonical(_plain(item["ref"]))] = _plain(item["ref"])
    elif selector in {"test_plan", "fixed_test_plan"}:
        for item in context.get("task_definitions", []):
            if task_ref is not None and item.get("task_ref") != task_ref:
                continue
            value = item.get("task_ref")
            if isinstance(value, dict):
                refs[canonical(_plain(value))] = _plain(value)
    return [refs[key] for key in sorted(refs)]


def _node_component(control: Any, actor: Any, project: str, profile_body: dict[str, Any],
                    stage: str, context: dict[str, Any], denominator: dict[str, Any],
                    task_ref: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]], Any | None]:
    rule_by_id = {item["id"]: item for item in profile_body["node_review_rules"]}
    stage_rule = profile_body["stage_rules"][stage]
    requests: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    declared_roles: list[dict[str, Any]] = []
    for rule_id in stage_rule["node_rules"]:
        rule = rule_by_id[rule_id]
        selector = rule["selector"]
        declared_roles.append({"rule": rule_id, "selector": selector, "roles": list(rule["roles"])})
        refs = _node_refs(context, denominator, selector, task_ref)
        if not refs:
            diagnostics.append(_unresolved("node_population_missing",
                                           "Node rule has no controller-derived subject", status="missing",
                                           selector=selector, rule=rule_id))
        else:
            requests.extend({"selector": selector, "node_ref": ref, "roles": list(rule["roles"])} for ref in refs)
    if not requests:
        status = _aggregate(diagnostics, empty="missing")
        return {"format": "daikibo.assurance-node-result.v1", "status": status,
                "required": True, "reason": "node_population_missing" if diagnostics else "node_rules_empty",
                "requests": [], "items": [], "diagnostics": diagnostics,
                "declared_roles": declared_roles}, diagnostics, None
    try:
        request_bundle = build_node_requests(control, actor, project=project, selectors=requests,
            contract=profile_body.get("required_node_contract", "assurance.node-contract.v1"))
        selected = select_node_reviews(control, actor, node_requests=request_bundle)
        statuses: list[dict[str, Any]] = []
        for item in selected:
            for role, value in item.get("roles", {}).items():
                statuses.append({**value, "selector": item.get("selector"), "node_ref": item.get("node_ref")})
        diagnostics.extend(statuses)
        status = _aggregate(diagnostics, empty="unverified")
        return {"format": "daikibo.assurance-node-result.v1", "status": status,
                "required": True, "reason": "node_reviews_evaluated", "requests": list(request_bundle),
                "items": selected, "diagnostics": diagnostics, "declared_roles": declared_roles}, diagnostics, selected
    except Fault as exc:
        diagnostic = _unresolved(exc.code, str(exc), status="unverified")
        result = {"format": "daikibo.assurance-node-result.v1", "status": diagnostic["status"],
                  "required": True, "reason": diagnostic["reason"],
                  "code": diagnostic.get("code"), "requests": requests, "items": [],
                  "diagnostics": [diagnostic], "declared_roles": declared_roles}
        return result, [diagnostic], None


def _relation_center_refs(context: dict[str, Any], center: str,
                          task_ref: dict[str, Any] | None, *,
                          profile_format: str | None = None) -> list[dict[str, Any]]:
    """Return the finite typed population named by one profile center.

    The profile contains center *selectors*, never endpoint IDs.  This
    projection therefore stays controller-derived and bounded by the same
    context used for the denominator.  In particular, a missing selector is
    reported as a missing owner; an edge/set row cannot create a population.
    """
    values: list[dict[str, Any]] = []
    if center == "source_roots":
        values = [item.get("ref") for item in context.get("source_inputs", [])
                  if isinstance(item, dict)]
    elif center == "requirements":
        values = [item.get("ref") for item in context.get("artifacts", [])
                  if isinstance(item, dict) and item.get("kind") == "requirement"]
    elif center == "design_artifacts":
        values = [item.get("ref") for item in context.get("artifacts", [])
                  if isinstance(item, dict) and item.get("kind") in
                  {"design", "component", "interface", "domain"}]
    elif center == "realization_sources":
        need(profile_format in {PROFILE_V4_FORMAT, PROFILE_V5_FORMAT}, "invalid_profile",
             "realization_sources requires an explicit v4 profile")
        values = [item.get("ref") for item in context.get("artifacts", [])
                  if isinstance(item, dict) and item.get("kind") in
                  {"design", "component", "interface"}]
    elif center == "assigned_tasks":
        values = [item.get("task_ref") for item in context.get("task_definitions", [])
                  if isinstance(item, dict)]
        if task_ref is not None:
            values = [value for value in values if isinstance(value, dict) and value == task_ref]
    elif center == "test_definitions":
        for item in context.get("task_definitions", []):
            if not isinstance(item, dict) or (task_ref is not None and item.get("task_ref") != task_ref):
                continue
            plan = item.get("plan")
            if isinstance(plan, dict):
                values.extend(plan.get("check_refs", []))
    elif center == "selected_outputs":
        for item in context.get("task_definitions", []):
            if not isinstance(item, dict) or (task_ref is not None and item.get("task_ref") != task_ref):
                continue
            candidate = item.get("candidate")
            if isinstance(candidate, dict):
                values.append(candidate.get("ref"))
            structural = item.get("structural_obligations")
            if isinstance(structural, dict):
                for collection in ("required_outputs", "required_exercises"):
                    for entry in structural.get(collection, []):
                        if isinstance(entry, dict):
                            values.extend(entry.get("artifact_refs", []))
    elif center == "populations":
        values = [item.get("ref") for item in context.get("unit_b", {}).get("leaves", [])
                  if isinstance(item, dict)]
    elif center in {"delivery_snapshots", "actual_commits"}:
        material = context.get("delivery_material")
        if isinstance(material, dict):
            reader = material.get("reader")
            if isinstance(reader, dict):
                centers = reader.get("centers", {})
                values = centers.get(center, []) if isinstance(centers, dict) else []
            else:
                # A Delivery center is authoritative only when the accepted
                # reader supplied the typed snapshot/actual population.  A
                # raw selector or legacy material projection cannot create a
                # relation center.
                values = []

    refs: dict[bytes, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            normalized = _plain(value)
            refs[canonical(normalized)] = normalized
        except (TypeError, ValueError):
            # The collector's validator will retain a malformed material
            # diagnostic.  It must not become an arbitrary relation endpoint.
            continue
    return [refs[key] for key in sorted(refs)]


def _matching_relation_set(control: Any, project: str, *, relation: str,
                           direction: str, center_ref: dict[str, Any],
                           scope_ref: dict[str, Any], registry_digest: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the adopted current set for one exact controller request."""
    assurance = control.assurance
    matches: list[dict[str, Any]] = []
    for row in control.s.all(
            "SELECT * FROM assurance_objects WHERE project=? AND kind='set' ORDER BY logical_id,revision,id",
            (project,)):
        decoded = assurance._decode_object(row)
        body = decoded.get("body", {})
        if (body.get("relation") != relation or body.get("direction") != direction or
                body.get("relation_contract_digest") != registry_digest or
                _plain(body.get("center_ref")) != _plain(center_ref) or
                _plain(body.get("scope_ref")) != _plain(scope_ref)):
            continue
        matches.append(decoded)
    current = [row for row in matches if assurance._object_is_current(row)]
    if len(current) > 1:
        return None, _unresolved(
            "relation_set_ambiguous", "More than one adopted current relation set matches the request",
            status="unverified", relation=relation, direction=direction,
            center_ref=center_ref, scope_ref=scope_ref,
        )
    if not current:
        return None, _unresolved(
            "relation_set_missing", "No adopted current relation set matches the controller population",
            status="missing", relation=relation, direction=direction,
            center_ref=center_ref, scope_ref=scope_ref,
        )
    return current[0], None


def _relation_fault_status(code: str) -> str:
    if code in {"stale_reference", "stale_set", "stale_evidence", "set_incomplete"}:
        return "stale"
    if code in {"unresolved_reference", "missing_evidence", "not_found", "relation_set_missing"}:
        return "missing"
    if code in {"invalid_registry", "unsupported"}:
        return "unsupported"
    return "unverified"


def _relation_component(control: Any, actor: Any, project: str, profile_body: dict[str, Any],
                        stage: str, checkpoint: str, context: dict[str, Any], denominator: dict[str, Any],
                        task_ref: dict[str, Any] | None,
                        selected_profile_ref: dict[str, Any],
                        node_reviews: Any | None,
                        registry_digest: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Consume accepted M/R request, set-review, and criteria boundaries.

    Integration and Delivery connect only the accepted C output relations.
    Other registry relations remain explicit unsupported capabilities when a
    profile asks for them; they are never silently dropped or made optional.
    """
    rules = profile_body["stage_rules"][stage]["relation_sets"]
    connected = {"produced_by", "contains"}
    unsupported_rules = []
    if stage in {"integration", "delivery"}:
        unsupported_rules = [rule for rule in rules if rule.get("relation") not in connected]

    items: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    deferred_future: list[dict[str, Any]] = []
    for rule in unsupported_rules:
        diagnostic = _unresolved(
            "relation_consumer_unsupported",
            "This stage relation is outside the finite Consumer-C connection",
            status="unsupported", relation=rule["relation"], direction=rule["direction"],
            centers=rule["centers"],
        )
        items.append(diagnostic)
        diagnostics.append(diagnostic)
    # Relation sets are scoped to the selected profile identity.  The profile
    # body also contains its underlying scope identity, which is used by the
    # accepted request adapter to derive obligations, but set adoption binds
    # the exact selected profile object.
    scope_ref = _plain(selected_profile_ref)
    active_rules = [rule for rule in rules if rule not in unsupported_rules]
    for rule in active_rules:
        relation = rule["relation"]
        direction = rule["direction"]
        requirements = sorted(SET_UNIVERSAL_CRITERIA | set(
            registry_entry(relation, contract_digest=registry_digest)["set_checks"]
        ))
        for center_name in rule["centers"]:
            centers = _relation_center_refs(context, center_name, task_ref,
                                            profile_format=profile_body.get("format"))
            if not centers:
                item = _unresolved(
                    "relation_center_missing", "Profile relation center has no controller-derived owner",
                    status="missing", relation=relation, direction=direction, selector=center_name,
                )
                item.update({"center": center_name})
                items.append(item); diagnostics.append(item)
                continue
            for center_ref in centers:
                base = {"relation": relation, "direction": direction, "center": center_name,
                        "center_ref": center_ref, "registry_digest": registry_digest}
                try:
                    full_projection = (project_task(denominator, task_ref)
                                       if task_ref is not None else None)
                    full_request = build_relation_request(
                        control, actor, context=context, denominator=denominator,
                        relation=relation, center_ref=center_ref, direction=direction,
                        scope_ref=scope_ref, registry_digest=registry_digest,
                        projection=full_projection,
                    )
                    schedule = _relation_schedule(
                        context, denominator, full_request,
                        stage=stage, checkpoint=checkpoint,
                    )
                    if schedule["unknown"]:
                        diagnostics.extend(schedule["unknown"])
                        item = {**base, "status": "unverified", "required": True,
                                "reason": "checkpoint_relation_classification_unknown",
                                "request": dict(full_request),
                                "schedule": schedule}
                        items.append(item)
                        continue
                    request = full_request
                    if (task_ref is not None and schedule["population_ids"] and
                            schedule["required_now_ids"] != schedule["population_ids"]):
                        checkpoint_plan = _make_checkpoint_plan(
                            stage=stage, checkpoint=checkpoint, relation=relation,
                            direction=direction, center_ref=center_ref,
                            schedule=schedule,
                        )
                        checkpoint_projection = project_task_checkpoint(
                            denominator, task_ref, checkpoint=checkpoint,
                            # checkpoint is the existing Task checkpoint;
                            # it is metadata on this read-only projection only.
                            relation=relation, direction=direction,
                            center_ref=center_ref, _plan=checkpoint_plan,
                        )
                        request = build_relation_request(
                            control, actor, context=context, denominator=denominator,
                            relation=relation, center_ref=center_ref, direction=direction,
                            scope_ref=scope_ref, registry_digest=registry_digest,
                            checkpoint=checkpoint,
                            projection=checkpoint_projection,
                        )
                    elif schedule["deferred_future_ids"]:
                        checkpoint_plan = _make_checkpoint_plan(
                            stage=stage, checkpoint=checkpoint, relation=relation,
                            direction=direction, center_ref=center_ref,
                            schedule=schedule,
                        )
                        checkpoint_projection = project_global_checkpoint(
                            denominator, checkpoint=checkpoint,
                            relation=relation, direction=direction,
                            center_ref=center_ref, _plan=checkpoint_plan,
                        )
                        request = build_relation_request(
                            control, actor, context=context, denominator=denominator,
                            relation=relation, center_ref=center_ref, direction=direction,
                            scope_ref=scope_ref, registry_digest=registry_digest,
                            checkpoint=checkpoint,
                            projection=checkpoint_projection,
                        )
                    set_row, set_diagnostic = _matching_relation_set(
                        control, project, relation=relation, direction=direction,
                        center_ref=center_ref, scope_ref=scope_ref,
                        registry_digest=registry_digest,
                    )
                    if schedule["deferred_future_ids"] and not schedule["required_now_ids"] and set_diagnostic is not None:
                        # A future producer does not need an adopted current
                        # set at this checkpoint.  Preserve the missing set
                        # diagnostic inside the deferred schedule; it becomes
                        # a required current failure at complete.
                        item = {**base, "status": "deferred", "required": True,
                                "reason": "relation_obligations_deferred",
                                "request": dict(request), "schedule": schedule,
                                "set_diagnostic": set_diagnostic,
                                "capabilities": {"checkpoint_projection": True,
                                                  "set_required_at": "complete"}}
                        items.append(item)
                        deferred_future.extend({
                            "component": "relation", **base,
                            "obligation_id": obligation_id,
                            "status": "deferred", "required": True,
                            "reason": next(entry["reason"] for entry in schedule["schedule"]
                                            if entry["obligation_id"] == obligation_id),
                        } for obligation_id in schedule["deferred_future_ids"])
                        continue
                    if set_diagnostic is not None:
                        item = {**set_diagnostic, **base, "request": dict(request),
                                "schedule": schedule}
                        items.append(item); diagnostics.append(item)
                        continue
                    set_ref = control.assurance._object_ref(set_row)
                    if schedule["deferred_future_ids"] and not schedule["required_now_ids"]:
                        # The adopted set is still read through the sealed
                        # request boundary, but no future evidence is turned
                        # into a current criteria result.
                        relation_reviews = build_review_assurance(
                            control, actor, relation_request=request, set_ref=set_ref,
                        )
                        item = {**base, "status": "deferred", "required": True,
                                "reason": "relation_obligations_deferred",
                                "set_ref": set_ref, "request": dict(request),
                                "relation_reviews": dict(relation_reviews),
                                "schedule": schedule,
                                "capabilities": {"checkpoint_projection": True}}
                        items.append(item)
                        deferred_future.extend({
                            "component": "relation", **base,
                            "obligation_id": obligation_id,
                            "status": "deferred", "required": True,
                            "reason": next(entry["reason"] for entry in schedule["schedule"]
                                            if entry["obligation_id"] == obligation_id),
                        } for obligation_id in schedule["deferred_future_ids"])
                        continue
                    relation_reviews = build_review_assurance(
                        control, actor, relation_request=request, set_ref=set_ref,
                    )
                    edge_rows = [control.assurance._object_by_ref(
                        edge_ref, project, kinds={"edge"})
                        for edge_ref in relation_reviews["edge_refs"]]
                    if node_reviews is None:
                        # An empty sealed bundle is still a controller result;
                        # evaluate_criteria will report N as unverified rather
                        # than accepting a JSON-shaped substitute.
                        empty_requests = build_node_requests(
                            control, actor, project=project, selectors=[],
                        )
                        relation_node_reviews = select_node_reviews(
                            control, actor, node_requests=empty_requests,
                        )
                    else:
                        relation_node_reviews = node_reviews
                    evaluated = evaluate_criteria(
                        relation=relation, requirements=requirements,
                        denominator=denominator, edges=edge_rows,
                        validated_reviews=relation_node_reviews,
                        relation_request=request,
                        relation_reviews=relation_reviews,
                    )
                    item = {**base, "status": evaluated["status"], "required": True,
                            "reason": "relation_criteria_evaluated", "set_ref": set_ref,
                            "request": dict(request), "relation_reviews": dict(relation_reviews),
                            "criteria": evaluated["criteria"],
                            "schedule": schedule,
                            "capabilities": evaluated.get("capabilities", {})}
                    if schedule["deferred_future_ids"]:
                        item["status"] = "deferred" if item["status"] == "satisfied" else item["status"]
                        deferred_future.extend({
                            "component": "relation", **base,
                            "obligation_id": obligation_id,
                            "status": "deferred", "required": True,
                            "reason": next(entry["reason"] for entry in schedule["schedule"]
                                            if entry["obligation_id"] == obligation_id),
                        } for obligation_id in schedule["deferred_future_ids"])
                    items.append(item)
                    for criterion, result in evaluated["criteria"].items():
                        status = result.get("status", "unverified")
                        if status in {"satisfied", "not_applicable"}:
                            continue
                        diagnostic = _unresolved(
                            f"relation_criterion_{criterion}",
                            f"Relation criterion {criterion} is {status}", status=status,
                            relation=relation, direction=direction, center_ref=center_ref,
                            selector=center_name, criterion=criterion,
                        )
                        diagnostics.append(diagnostic)
                except Fault as exc:
                    status = _relation_fault_status(exc.code)
                    diagnostic = _unresolved(
                        exc.code, str(exc), status=status, **base,
                    )
                    items.append(diagnostic); diagnostics.append(diagnostic)

    status = _aggregate(items, empty="missing")
    supported = not unsupported_rules
    reason = ("consumer_c_connected_produced_by_contains"
              if stage in {"integration", "delivery"}
              else "consumer_mr_connected_plan_task")
    return {"format": "daikibo.assurance-relation-result.v1", "status": status,
            "required": True, "reason": "relation_consumer_evaluated", "registry_digest": registry_digest,
            "items": items, "diagnostics": diagnostics,
            "deferred_future": deferred_future,
            "capability": {"supported": supported, "reason": reason,
                            "connected_relations": sorted(connected) if stage in {"integration", "delivery"} else "all_profile_relations",
                            "unsupported_relations": sorted({rule["relation"] for rule in unsupported_rules}),
                            "registry_digest": registry_digest}}, diagnostics, deferred_future


def _execution_definition_key(ref: dict[str, Any]) -> dict[str, Any]:
    """Compare a frozen check while allowing independent material captures."""
    value = _plain(ref)
    if isinstance(value, dict):
        if value.get("kind") == "test_plan_check":
            plan = value.get("plan")
            if isinstance(plan, dict):
                plan.pop("pin", None)
        elif value.get("kind") == "delivery_check":
            # Delivery.verify captures the same immutable snapshot more than
            # once across re-verification.  Its material pin is capture
            # provenance, not Delivery meaning.  Keep the typed snapshot,
            # binding, digest, and check digest exact while comparing captures
            # semantically; an arbitrary ref or a digest-only shortcut still
            # fails the resolver checks below.
            delivery = value.get("delivery")
            if isinstance(delivery, dict):
                delivery.pop("pin", None)
    return value


def _execution_receipt_ref(control: Any, project: str, task_ref: dict[str, Any],
                           check_ref: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Select one exact current formal-check receipt by its immutable fields."""
    role = "test:" + check_ref.get("check_id", "")
    try:
        binding = control.g.task_binding(task_ref["task"], ensure_policy=False)
    except Fault as exc:
        return None, _unresolved(
            exc.code, str(exc), status="unverified", task=task_ref, check=check_ref,
        )
    rows = []
    for row in control.s.all(
            "SELECT * FROM receipts WHERE project=? AND subject=? AND role=? AND binding=? ORDER BY id",
            (project, task_ref["task"], role, binding),
    ):
        rows.append(row)
    if not rows:
        return None, _unresolved(
            "execution_check_missing", "No retained observed result matches the frozen Task check",
            status="missing", task=task_ref, check=check_ref,
        )
    try:
        ordered = ordered_observed_receipts(
            control, project=project, subject=task_ref["task"],
            role=role, binding=binding,
            receipt_ids=[row["id"] for row in rows],
        )
    except Fault as exc:
        return None, _unresolved(
            "execution_check_invalid", str(exc),
            status="unverified", task=task_ref, check=check_ref,
        )
    if not ordered:
        return None, _unresolved(
            "execution_check_missing", "No retained observed result matches the frozen Task check",
            status="missing", task=task_ref, check=check_ref,
        )
    selected = ordered[-1]
    row, body = selected["row"], selected["body"]
    observed_ref = {
        "kind": "observed_result", "project": project, "receipt": row["id"],
        "run": row["run"], "receipt_digest": digest(body),
        "run_binding": row["binding"], "snapshot_digest": body.get("snapshot"),
        "result_digest": digest(body.get("result", {})),
    }
    try:
        return _plain(validate_typed_ref(observed_ref, project=project,
                                         expected_kinds={"observed_result"})), None
    except Fault as exc:
        return None, _unresolved(exc.code, str(exc), status="unverified", task=task_ref, check=check_ref)


def _delivery_execution_receipt_ref(
        control: Any, project: str, delivery_material: dict[str, Any],
        check_ref: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Select the latest exact Delivery-check observation.

    Delivery checks have their own subject/role family.  In particular, a
    Task ``test:<id>`` receipt, another Delivery, or an old Delivery binding
    is never a candidate.  ``ordered_observed_receipts`` verifies the receipt,
    run, MAC and durable ``events.seq`` link before this reader chooses the
    last observation.
    """
    snapshot_ref = delivery_material.get("snapshot_ref")
    delivery_id = delivery_material.get("delivery_id")
    if not isinstance(snapshot_ref, dict) or not isinstance(delivery_id, str) or not delivery_id:
        return None, _unresolved(
            "delivery_material_unresolved",
            "Delivery execution needs the reader's canonical snapshot and Delivery identity",
            status="unverified", check=check_ref,
        )
    binding = snapshot_ref.get("binding_digest")
    if not isinstance(binding, str) or not binding:
        return None, _unresolved(
            "delivery_material_unresolved", "Delivery execution binding is missing",
            status="unverified", delivery=delivery_id, check=check_ref,
        )
    check_id = check_ref.get("check_id")
    role = "delivery:" + check_id if isinstance(check_id, str) else "delivery:"
    rows = control.s.all(
        "SELECT * FROM receipts WHERE project=? AND subject=? AND role=? AND binding=? ORDER BY id",
        (project, delivery_id, role, binding),
    )
    if not rows:
        return None, _unresolved(
            "execution_check_missing", "No retained observed result matches the frozen Delivery check",
            status="missing", delivery=delivery_id, check=check_ref,
        )
    try:
        ordered = ordered_observed_receipts(
            control, project=project, subject=delivery_id, role=role,
            binding=binding, receipt_ids=[row["id"] for row in rows],
        )
    except Fault as exc:
        return None, _unresolved(
            "execution_check_invalid", str(exc), status="unverified",
            delivery=delivery_id, check=check_ref,
        )
    if not ordered:
        return None, _unresolved(
            "execution_check_missing", "No retained observed result matches the frozen Delivery check",
            status="missing", delivery=delivery_id, check=check_ref,
        )
    selected = ordered[-1]
    row, body = selected["row"], selected["body"]
    observed_ref = {
        "kind": "observed_result", "project": project, "receipt": row["id"],
        "run": row["run"], "receipt_digest": digest(body),
        "run_binding": row["binding"], "snapshot_digest": body.get("snapshot"),
        "result_digest": digest(body.get("result", {})),
    }
    try:
        return _plain(validate_typed_ref(
            observed_ref, project=project, expected_kinds={"observed_result"},
        )), None
    except Fault as exc:
        return None, _unresolved(
            exc.code, str(exc), status="unverified", delivery=delivery_id, check=check_ref,
        )


def _resolve_execution_check(control: Any, actor: Any, project: str,
                             task_ref: dict[str, Any], check_ref: dict[str, Any]) -> dict[str, Any]:
    """Resolve definition, observed record, and result through shared E1/E3 readers."""
    base = {"task": task_ref, "check": check_ref}
    try:
        definition = control.assurance.resolve_pinned(actor, check_ref)
        definition_resolution = definition.get("resolution", {})
        if definition_resolution.get("current") is not True:
            return _unresolved("execution_definition_stale", "Frozen Task check is not current",
                                status="stale", **base)
        observed_ref, missing = _execution_receipt_ref(control, project, task_ref, check_ref)
        if missing is not None:
            return missing
        observed_resolution = control.assurance.resolve_pinned(actor, observed_ref)
        resolved = observed_resolution.get("resolution", {})
        if resolved.get("current") is not True:
            return _unresolved("execution_observation_stale", "Observed Task check is not current",
                                status="stale", observed=observed_ref, **base)
        passed, reason, observed = _observed_execution(control, actor, observed_ref)
        if observed is None:
            return _unresolved(reason or "observed_result_unresolved",
                               "Observed Task check could not be resolved", status="unverified",
                               observed=observed_ref, **base)
        material_ref, runtime_check, material_reason = _observed_definition_ref(
            control, actor, observed_ref, observed,
        )
        if material_ref is None:
            return _unresolved(material_reason or "verification_definition_unresolved",
                               "Observed Task check has no resolved frozen definition",
                               status="unverified", observed=observed_ref, **base)
        if (_execution_definition_key(material_ref) != _execution_definition_key(check_ref) or
                not isinstance(runtime_check, dict) or
                runtime_check.get("id") != check_ref.get("check_id") or
                observed.get("check_id") != runtime_check.get("id") or
                observed.get("check_digest") != digest(runtime_check)):
            return _unresolved(
                "execution_definition_identity_mismatch",
                "Observed Task check definition differs from the frozen check",
                status="unverified", observed=observed_ref, definition=material_ref, **base,
            )
        if passed is False:
            return _unresolved("execution_check_failed", "Observed Task check reported failure",
                                status="failed", observed=observed_ref, **base)
        if passed is not True:
            return _unresolved(reason or "execution_check_unresolved",
                               "Observed Task check result is unresolved", status="unverified",
                               observed=observed_ref, **base)
        return _status("satisfied", "execution_check_current_and_passed", task=task_ref,
                       check=check_ref, observed=observed_ref,
                       definition=material_ref)
    except Fault as exc:
        return _unresolved(exc.code, str(exc), status=_relation_fault_status(exc.code), **base)


def _resolve_delivery_check(control: Any, actor: Any, project: str,
                            delivery_material: dict[str, Any],
                            check_ref: dict[str, Any]) -> dict[str, Any]:
    """Resolve one Delivery check through the shared observed-material path."""
    delivery_id = delivery_material.get("delivery_id")
    base = {"delivery": delivery_id, "check": check_ref}
    try:
        reader = delivery_material.get("reader")
        need(isinstance(reader, dict), "delivery_material_unresolved",
             "Delivery execution requires the canonical material reader")
        snapshot_ref = delivery_material.get("snapshot_ref")
        need(isinstance(snapshot_ref, dict), "delivery_material_unresolved",
             "Delivery execution has no canonical snapshot anchor")
        # The reader owns the complete check population.  A hand-built check
        # or a check from a different snapshot is not a member merely because
        # its typed wire validates.
        members = reader.get("check_refs")
        need(isinstance(members, list), "delivery_checks_invalid",
             "Delivery reader check population is not a list")
        need(any(_execution_definition_key(item) == _execution_definition_key(check_ref)
                 for item in members), "delivery_check_source_invalid",
             "Delivery check is outside the reader-owned snapshot population")
        need(_execution_definition_key(check_ref.get("delivery")) ==
             _execution_definition_key(snapshot_ref),
             "delivery_check_source_invalid",
             "Delivery check does not belong to the reader snapshot anchor")
        definition_resolution = control.assurance.resolve_pinned(actor, check_ref)
        definition_current = definition_resolution.get("resolution", {}).get("current")
        if definition_current is not True:
            return _unresolved(
                "execution_definition_stale", "Frozen Delivery check is not current",
                status="stale", **base,
            )
        observed_ref, missing = _delivery_execution_receipt_ref(
            control, project, delivery_material, check_ref,
        )
        if missing is not None:
            return missing
        observed_resolution = control.assurance.resolve_pinned(actor, observed_ref)
        observed_current = observed_resolution.get("resolution", {}).get("current")
        if observed_current is not True:
            return _unresolved(
                "execution_observation_stale", "Observed Delivery check is not current",
                status="stale", observed=observed_ref, **base,
            )
        passed, reason, observed = _observed_execution(control, actor, observed_ref)
        if observed is None:
            return _unresolved(
                reason or "observed_result_unresolved",
                "Observed Delivery check could not be resolved", status="unverified",
                observed=observed_ref, **base,
            )
        material_ref, runtime_check, material_reason = _observed_definition_ref(
            control, actor, observed_ref, observed,
        )
        if material_ref is None:
            return _unresolved(
                material_reason or "verification_definition_unresolved",
                "Observed Delivery check has no resolved frozen definition",
                status="unverified", observed=observed_ref, **base,
            )
        material_resolution = control.assurance.resolve_pinned(actor, material_ref)
        material_current = material_resolution.get("resolution", {}).get("current")
        if material_current is not True:
            return _unresolved(
                "execution_definition_stale", "Observed Delivery definition is not current",
                status="stale", observed=observed_ref, definition=material_ref, **base,
            )
        if (_execution_definition_key(material_ref) != _execution_definition_key(check_ref) or
                material_ref.get("kind") != "delivery_check" or
                material_ref.get("check_id") != check_ref.get("check_id") or
                material_ref.get("check_digest") != check_ref.get("check_digest") or
                not isinstance(runtime_check, dict) or
                runtime_check.get("id") != check_ref.get("check_id") or
                observed.get("check_id") != runtime_check.get("id") or
                observed.get("check_digest") != digest(runtime_check)):
            return _unresolved(
                "execution_definition_identity_mismatch",
                "Observed Delivery check definition differs from the frozen check",
                status="unverified", observed=observed_ref, definition=material_ref, **base,
            )
        if passed is False:
            return _unresolved(
                "execution_check_failed", "Observed Delivery check reported failure",
                status="failed", observed=observed_ref, **base,
            )
        if passed is not True:
            return _unresolved(
                reason or "execution_check_unresolved",
                "Observed Delivery check result is unresolved", status="unverified",
                observed=observed_ref, **base,
            )
        return _status(
            "satisfied", "delivery_execution_check_current_and_passed",
            delivery=delivery_id, check=check_ref, observed=observed_ref,
            definition=material_ref,
        )
    except Fault as exc:
        return _unresolved(
            exc.code, str(exc), status=_relation_fault_status(exc.code), **base,
        )


def _candidate_execution_state(control: Any, actor: Any, task_value: dict[str, Any],
                               preadoption_identity: Any | None = None) -> dict[str, Any]:
    """Resolve candidate evidence at the ordinary or private boundary.

    The public reader still requires the retained candidate reference.  The
    Runtime pre-adoption boundary is the one exception: it supplies the
    controller/actor sealed identity returned by the readonly provenance
    resolver while the candidate row is intentionally absent.  Keep this
    branch here, beside the ordinary resolver, so both paths consume the same
    Task context and no caller-authored candidate-shaped dictionary can enter
    the public evaluator.
    """
    candidate = task_value.get("candidate")
    ref = candidate.get("ref") if isinstance(candidate, dict) else None
    task_ref = task_value.get("task_ref")
    if preadoption_identity is not None:
        from . import candidate_provenance
        try:
            identity = candidate_provenance._verify_preadoption_identity(
                control, actor, preadoption_identity,
            )
            expected = {
                "project": task_ref.get("project") if isinstance(task_ref, dict) else None,
                "task": task_ref.get("task") if isinstance(task_ref, dict) else None,
                "task_revision": task_ref.get("revision") if isinstance(task_ref, dict) else None,
                "task_definition_digest": task_ref.get("definition_digest") if isinstance(task_ref, dict) else None,
            }
            if any(identity.get(key) != value for key, value in expected.items()):
                return _unresolved(
                    "preadoption_identity_mismatch",
                    "Pre-adoption identity does not bind the evaluated Task revision",
                    status="stale", task=task_ref,
                )
            return _status(
                "satisfied", "preadoption_identity_current", task=task_ref,
                provenance={key: identity.get(key) for key in
                            ("run", "receipt", "epoch", "output_snapshot_digest", "changes_digest")},
            )
        except Fault as exc:
            return _unresolved(
                exc.code, str(exc), status=_relation_fault_status(exc.code), task=task_ref,
            )
    if not isinstance(ref, dict):
        return _unresolved("candidate_missing", "Task candidate evidence is not retained",
                           status="missing", task=task_ref)
    try:
        resolved = control.assurance.resolve_pinned(actor, ref)
        inner = resolved.get("resolution", {})
        if inner.get("current") is not True:
            return _unresolved("candidate_stale", "Task candidate is not current",
                               status="stale", task=task_ref, candidate=ref)
        return _status("satisfied", "candidate_current", task=task_ref, candidate=ref)
    except Fault as exc:
        return _unresolved(exc.code, str(exc), status=_relation_fault_status(exc.code),
                           task=task_ref, candidate=ref)


def _task_inventory_item_matches(item: dict[str, Any], task_ref: dict[str, Any] | None) -> bool:
    """Keep task scoped inventory aligned with the evaluated Task revision."""
    if task_ref is None:
        return True
    item_task = item.get("task")
    if isinstance(item_task, dict):
        return item_task.get("task") == task_ref.get("task")
    if isinstance(item_task, str):
        return item_task == task_ref.get("task")
    return True


def _execution_component(control: Any, actor: Any, project: str, stage: str, checkpoint: str,
                         context: dict[str, Any], denominator: dict[str, Any],
                         task_ref: dict[str, Any] | None, profile_body: dict[str, Any],
                         *, preadoption_identity: Any | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    future: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    mode = profile_body["stage_rules"][stage]["execution_results"]
    if mode == "none":
        return _status("satisfied", "execution_not_required_at_plan", required=False), diagnostics, future
    task_values = context.get("task_definitions", [])
    if task_ref is not None:
        task_values = [item for item in task_values if item.get("task_ref") == task_ref]
    relevant_codes = {
        "test_plan_missing", "test_plan_material_missing", "test_plan_material_unsupported",
        "test_plan_material_stale", "test_plan_material_invalid", "test_plan_material_ambiguous",
        "task_test_plan_unpinned", "required_checks_unpinned",
        "candidate_missing", "candidate_snapshot_missing", "implementation_run_missing",
        "test_plan_checks_invalid", "test_check_invalid", "delivery_material_missing",
        "delivery_material_unsupported", "delivery_material_unresolved", "delivery_payload_missing",
        "delivery_checks_invalid", "delivery_actual_material_invalid",
        "delivery_actual_material_missing", "delivery_actual_material_ambiguous",
        "delivery_actual_snapshot_mismatch", "delivery_repository_set_mismatch",
        "delivery_check_source_invalid", "delivery_check_invalid",
        "delivery_repository_missing", "delivery_repository_observation_missing",
    }
    seen: set[bytes] = set()
    for item in context.get("unresolved", []) + denominator.get("unresolved", []):
        code = item.get("code")
        if code not in relevant_codes:
            continue
        # The context and denominator retain the complete selected local
        # population, while a complete/recheck admission is for one current
        # Task revision.  _execution_component already narrows
        # ``task_values`` to that selector; apply the same boundary to
        # unresolved execution leaves so a later selected Task cannot block
        # the current Task before its own dependency-ordered checkpoint.
        if not _task_inventory_item_matches(item, task_ref):
            continue
        marker = canonical(item)
        if marker in seen:
            continue
        seen.add(marker)
        if (_future_execution_code(stage, checkpoint, code) or
                (preadoption_identity is not None and stage == "task" and
                 checkpoint == "candidate" and code in TASK_FUTURE_EXECUTION_CODES)):
            future.append({"component": "execution", "stage": stage, "checkpoint": checkpoint,
                           "code": code, "reason": item.get("reason", code),
                           **{key: _copy(item[key]) for key in
                              ("task", "candidate", "repository", "run") if key in item}})
            continue
        diagnostic = _copy_unresolved(item)
        diagnostics.append(diagnostic); items.append(diagnostic)

    # ready/claim/execute are intentionally future-facing.  Keep current
    # plan/assignment deficits required while deferring candidate and formal
    # check observations until their declared checkpoints.
    if checkpoint in EXECUTION_PRECOMPLETION.get(stage, ()):
        for task_value in task_values:
            plan = task_value.get("plan") if isinstance(task_value, dict) else None
            for check_ref in (plan or {}).get("check_refs", []) if isinstance(plan, dict) else []:
                future.append({"component": "execution", "stage": stage, "checkpoint": checkpoint,
                               "code": "execution_check_deferred", "reason": "formal check runs after Task execution",
                               "task": _copy(task_value.get("task_ref")), "check": _copy(check_ref)})
        future.append({"component": "execution", "stage": stage, "checkpoint": checkpoint,
                       "reason": "future_execution_results_not_required_before_completion"})
        return _status("deferred", "future_execution_results_deferred", required=False,
                       execution_results=mode, current_requirements=diagnostics or None), diagnostics, future

    # Candidate is a current adoption boundary, while its post-candidate
    # formal checks remain future until complete/recheck.
    if stage == "task" and checkpoint == "candidate":
        for task_value in task_values:
            candidate_state = _candidate_execution_state(
                control, actor, task_value, preadoption_identity,
            )
            if candidate_state.get("status") not in {"satisfied", "not_applicable"}:
                diagnostics.append(candidate_state); items.append(candidate_state)
            else:
                items.append(candidate_state)
            plan = task_value.get("plan") if isinstance(task_value, dict) else None
            for check_ref in (plan or {}).get("check_refs", []) if isinstance(plan, dict) else []:
                future.append({"component": "execution", "stage": stage, "checkpoint": checkpoint,
                               "code": "execution_check_deferred", "reason": "formal check runs after candidate adoption",
                               "task": _copy(task_value.get("task_ref")), "check": _copy(check_ref)})
        if diagnostics:
            status = _aggregate(diagnostics, empty="missing")
            return {"format": "daikibo.assurance-execution-result.v1", "status": status,
                    "required": True, "reason": "candidate_evidence_evaluated", "mode": mode,
                    "items": items}, diagnostics, future
        return _status("deferred", "future_execution_results_deferred", required=False,
                       execution_results=mode, items=items), diagnostics, future

    # Complete/recheck consume every frozen formal check independently.  A
    # relation review cannot substitute for this observed definition/run
    # resolver.
    for task_value in task_values:
        candidate_state = _candidate_execution_state(control, actor, task_value)
        if candidate_state.get("status") not in {"satisfied", "not_applicable"}:
            diagnostics.append(candidate_state); items.append(candidate_state)
        else:
            items.append(candidate_state)
        plan = task_value.get("plan") if isinstance(task_value, dict) else None
        check_refs = (plan or {}).get("check_refs", []) if isinstance(plan, dict) else []
        if not check_refs:
            if plan is not None:
                empty = _unresolved("execution_checks_missing", "Frozen Task plan has no formal check evidence",
                                    status="missing", task=task_value.get("task_ref"))
                diagnostics.append(empty); items.append(empty)
            continue
        for check_ref in check_refs:
            check_state = _resolve_execution_check(control, actor, project,
                                                   task_value["task_ref"], check_ref)
            items.append(check_state)
            if check_state.get("status") not in {"satisfied", "not_applicable"}:
                diagnostics.append(check_state)

    # Delivery.verify is the sole producer of these observations.  Consume
    # every check from the reader-owned snapshot population, including
    # output-less checks and checks blocked by a failed producer.  The exact
    # Delivery family is selected inside _resolve_delivery_check; no Task
    # binding or successful-output count can satisfy this branch.
    if stage in {"integration", "delivery"}:
        delivery_material = context.get("delivery_material")
        if isinstance(delivery_material, dict) and isinstance(delivery_material.get("reader"), dict):
            check_refs = delivery_material.get("check_refs")
            if not isinstance(check_refs, list):
                diagnostic = _unresolved(
                    "delivery_checks_invalid", "Delivery reader check population is not a list",
                    status="unverified",
                )
                diagnostics.append(diagnostic); items.append(diagnostic)
            else:
                for check_ref in check_refs:
                    check_state = _resolve_delivery_check(
                        control, actor, project, delivery_material, check_ref,
                    )
                    items.append(check_state)
                    if check_state.get("status") not in {"satisfied", "not_applicable"}:
                        diagnostics.append(check_state)
    status = _aggregate(diagnostics, empty="satisfied")
    return {"format": "daikibo.assurance-execution-result.v1", "status": status,
            "required": True, "reason": "execution_results_evaluated", "mode": mode,
            "items": items}, diagnostics, future


def _unit_b_component(control: Any, actor: Any, project: str, program: str, stage: str,
                      checkpoint: str, task_ref: dict[str, Any] | None,
                      delivery_id: str | None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    future: list[dict[str, Any]] = []
    if stage == "plan":
        gate_name = "planning_gate"
    elif stage == "task" and checkpoint in {"complete", "recheck"}:
        gate_name = "task_closure_gate"
    elif stage == "integration":
        gate_name = "integrated_closure_gate"
    elif stage == "delivery":
        gate_name = "delivered_closure_gate"
    else:
        future.append({"component": "unit_b", "stage": stage, "checkpoint": checkpoint,
                       "reason": "completion_gate_deferred_at_precompletion_checkpoint"})
        return _status("deferred", "unit_b_completion_deferred", required=False), [], future
    trace = getattr(control, "traceability", None)
    if trace is None or not hasattr(trace, gate_name):
        result = _unresolved("unit_b_gate_unavailable", "Existing Unit B gate is not attached to this controller", status="unsupported")
        return result, [result], future
    try:
        if gate_name == "planning_gate":
            gate = trace.planning_gate(project, program, actor)
        elif gate_name == "task_closure_gate":
            gate = trace.task_closure_gate(project, task_ref["task"], actor)
        else:
            need(isinstance(delivery_id, str) and delivery_id,
                 "invalid_stage_context", "Validated Delivery ID is required for Unit B delivery gates")
            if gate_name == "integrated_closure_gate":
                gate = trace.integrated_closure_gate(project, delivery_id, actor)
            else:
                gate = trace.delivered_closure_gate(project, delivery_id, actor)
        failures = [_unresolved("unit_b_gate_failure", value, status="failed") for value in gate.get("failures", [])]
        if failures:
            return {"format": "daikibo.assurance-unit-b-result.v1", "status": "failed", "required": True,
                    "reason": "unit_b_gate_failure", "allowed": False, "gate": gate,
                    "items": failures}, failures, future
        if gate.get("mandatory"):
            if gate.get("allowed") is not True:
                failure = _unresolved("unit_b_gate_not_allowed",
                                      "Existing Unit B gate did not authorize this checkpoint", status="failed",
                                      gate_allowed=gate.get("allowed"))
                return {"format": "daikibo.assurance-unit-b-result.v1", "status": "failed", "required": True,
                        "reason": "unit_b_gate_not_allowed", "allowed": False, "gate": gate,
                        "items": [failure]}, [failure], future
            return {"format": "daikibo.assurance-unit-b-result.v1", "status": "satisfied", "required": True,
                    "reason": "unit_b_gate_satisfied", "allowed": bool(gate.get("allowed")), "gate": gate,
                    "items": []}, [], future
        return {"format": "daikibo.assurance-unit-b-result.v1", "status": "not_applicable", "required": False,
                "reason": "no_mandatory_unit_b_binding", "allowed": True, "gate": gate,
                "items": []}, [], future
    except Fault as exc:
        result = _unresolved(exc.code, str(exc), status="unknown")
        result.update({"format": "daikibo.assurance-unit-b-result.v1", "allowed": False})
        return result, [result], future


def _context_failure(selection: dict[str, Any], project: str, program: str, stage: str,
                     checkpoint: str, reason: dict[str, Any], *, membership: dict[str, Any]) -> dict[str, Any]:
    status = reason.get("status", "unknown")
    selection_exact = {"program": program, "profile_ref": selection.get("profile_ref"),
                       "head_event": selection.get("head_event")}
    semantic = digest({"selection": {"program": program, "profile_ref": selection.get("profile_ref")},
                       "stage": stage, "checkpoint": checkpoint, "membership": membership,
                       "reason": {key: value for key, value in reason.items() if key not in {"status"}}})
    failure = {"kind": "selection", **reason}
    return {"format": FORMAT, "version": VERSION, "project": project, "program": program,
            "selection": selection_exact, "selection_state": selection.get("state"),
            "stage": stage, "checkpoint": checkpoint, "membership": membership,
            "global_denominator": None, "local_denominator": None,
            "nodes": _status(status, reason.get("reason", "selection_unavailable")),
            "relations": _status("unsupported", "selection_unavailable"),
            "execution": _status("deferred", "selection_unavailable", required=False),
            "unit_b": _status("deferred", "selection_unavailable", required=False),
            "deferred_future": [], "capabilities": {"stage_evaluator": {"supported": True, "version": VERSION},
                         "system_enforcement": False}, "failures": [failure],
            "status": status, "assurance_allow": False, "strong_complete": False,
            "system_enforcement": False, "semantic_fingerprint": semantic,
            "report_snapshot": digest({"semantic_fingerprint": semantic, "failures": [failure]})}


def _evaluate_one(control: Any, actor: Any, *, project: str, program: str, stage: str,
                  checkpoint: str, task_ref: dict[str, Any] | None,
                  delivery_ref: dict[str, Any] | None, proposed_breakdown: str | None,
                  local_execution: str | None,
                  preadoption_identity: Any | None = None,
                  active_memberships: bool = False,
                  membership_programs: list[str] | None = None) -> dict[str, Any]:
    membership, _task, _resolved_memberships = _membership(
        control, actor, project, program, stage, task_ref,
        active_only=active_memberships,
        program_override=membership_programs,
    )
    selection, profile, profile_error, registry_digest = _profile(control, actor, project, program)
    if profile_error is not None:
        return _context_failure(selection, project, program, stage, checkpoint, profile_error, membership=membership)
    if profile is None:
        state = selection.get("state", "not_enabled")
        reason = "legacy_profile_migration_pending" if state == "migration_pending" else "canonical_profile_not_selected"
        failure = _unresolved(reason, reason, status="unknown" if state == "migration_pending" else "missing")
        return _context_failure(selection, project, program, stage, checkpoint, failure, membership=membership)
    if profile["body"].get("application_mode") == "disabled":
        failure = _unresolved("profile_disabled", "Canonical assurance profile is disabled", status="unsupported")
        return _context_failure(selection, project, program, stage, checkpoint, failure, membership=membership)
    try:
        effective_breakdown = proposed_breakdown
        local_breakdown = _local_execution_proposed_breakdown(control, project, program, local_execution)
        if local_breakdown is not None:
            if effective_breakdown is not None and effective_breakdown != local_breakdown:
                raise Fault("invalid_reference", "Local execution proposal and proposed breakdown differ", {
                    "local_execution": local_execution, "expected": local_breakdown,
                    "actual": effective_breakdown,
                })
            effective_breakdown = local_breakdown
        context = collect_stage_context(control, actor, project=project, program=program, stage=stage,
                                        proposed_breakdown=effective_breakdown, task=task_ref,
                                        delivery=delivery_ref)
        denominator = derive_denominator(context)
        local = project_task(denominator, task_ref) if task_ref is not None else None
    except Fault as exc:
        # Selector shape and ownership are caller errors; saved material
        # deficits are diagnostic results so a report remains inspectable.
        if exc.code in {"invalid_stage_context", "cross_project", "invalid_reference", "invalid_input"}:
            raise
        reason = _unresolved(exc.code, str(exc), status="unknown")
        return _context_failure(selection, project, program, stage, checkpoint, reason, membership=membership)

    local_execution_result = _local_execution_state(
        control, actor, project, program, stage, checkpoint, task_ref,
        local_execution,
    )
    node_result, node_items, node_reviews = _node_component(
        control, actor, project, profile["body"], stage, context, denominator, task_ref,
    )
    relation_result, relation_items, deferred_relation = _relation_component(
        control, actor, project, profile["body"], stage, checkpoint, context, denominator,
        task_ref, selection["profile_ref"], node_reviews, registry_digest,
    )
    execution_result, execution_items, deferred_execution = _execution_component(
        control, actor, project, stage, checkpoint, context, denominator, task_ref,
        profile["body"], preadoption_identity=preadoption_identity,
    )
    delivery_material = context.get("delivery_material")
    delivery_id = delivery_material.get("delivery_id") if isinstance(delivery_material, dict) else None
    unit_b_result, unit_b_items, deferred_unit_b = _unit_b_component(
        control, actor, project, program, stage, checkpoint, task_ref, delivery_id)
    deferred = [*deferred_relation, *deferred_execution, *deferred_unit_b]

    # Unresolved material is preserved one-for-one.  It is never converted
    # into a PASS merely because a component happened to have no rows.
    # The collector retains future material in its unresolved inventory.  At
    # a precompletion checkpoint those entries are represented by the
    # execution component's ``deferred_future`` and must not be reintroduced
    # as current failures.  The same candidate remains required at candidate,
    # complete, and recheck.
    context_items = [
        _copy_unresolved(item) for item in context.get("unresolved", [])
        if _task_inventory_item_matches(item, task_ref) and
           not (_future_execution_code(stage, checkpoint, item.get("code", "")) or
                (preadoption_identity is not None and stage == "task" and
                 checkpoint == "candidate" and item.get("code", "") in TASK_FUTURE_EXECUTION_CODES))
    ]
    denominator_items = [
        _copy_unresolved(item) for item in denominator.get("unresolved", [])
        if _task_inventory_item_matches(item, task_ref) and
           not (_future_execution_code(stage, checkpoint, item.get("code", "")) or
                (preadoption_identity is not None and stage == "task" and
                 checkpoint == "candidate" and item.get("code", "") in TASK_FUTURE_EXECUTION_CODES))
    ]
    failures: list[dict[str, Any]] = []
    failures.extend(_failure_items("membership", item) for item in membership.get("failures", []))
    failures = [item for group in failures for item in (group if isinstance(group, list) else [group])]
    for name, result, items in (("node", node_result, node_items), ("relation", relation_result, relation_items),
                                ("execution", execution_result, execution_items), ("unit_b", unit_b_result, unit_b_items),
                                ("local_execution", local_execution_result, [local_execution_result]),
                                ("context", {"status": _aggregate(context_items, empty="satisfied"), "required": bool(context_items), "reason": "context_inventory"}, context_items),
                                ("denominator", {"status": _aggregate(denominator_items, empty="satisfied"), "required": bool(denominator_items), "reason": "denominator_inventory"}, denominator_items)):
        # Component summaries are useful to callers, but a report must not
        # duplicate every leaf once as a summary and again as a leaf.  Keep
        # leaf diagnostics when available; otherwise retain one component
        # failure with its aggregate status.
        if items:
            for item in items:
                if item.get("status") not in {"satisfied", "deferred", "not_applicable"} and item.get("required", True):
                    failures.append({"kind": name, **{key: value for key, value in item.items() if key in {"status", "reason", "code", "task", "artifact", "source", "candidate", "repository", "relation", "selector", "roles", "check", "center_ref", "criterion", "observed", "definition"}}})
        else:
            failures.extend(_failure_items(name, result))

    semantic_payload = {
        "selection": {"program": program, "profile_ref": selection.get("profile_ref")},
        "profile": {key: profile["body"].get(key) for key in
                     ("scope_ref", "obligations_ref", "application_mode", "stage_rules",
                      "node_review_rules", "relation_selectors", "test_definition_bindings")},
        "stage": stage, "checkpoint": checkpoint,
        # Context retains actual material authority/proof refs, but a capture
        # envelope is not Task/plan meaning. Keep semantic identity on the
        # shared definition projection used by the denominator.
        "context_inputs": semantic_definition_projection(context.get("input_refs", [])),
        "global_denominator": {"digest": denominator.get("digest"), "count": denominator.get("count"),
                               "obligations": [item.get("id") for item in denominator.get("obligations", [])]},
        "local_denominator": {"digest": local.get("digest"), "obligation_ids": local.get("obligation_ids", [])} if local else None,
        "membership": {key: membership.get(key) for key in ("programs", "requested_program", "task")},
        "registry_digest": registry_digest,
    }
    semantic_fingerprint = digest(semantic_payload)
    proof_payload = {
        "selection": selection, "semantic_fingerprint": semantic_fingerprint,
        # Denominator meaning intentionally projects capture pins and
        # diagnostics away.  The proof snapshot retains the complete reader
        # inventory so candidate changes, currentness observations, and
        # unresolved material cannot be confused with a prior report.
        "delivery_material": context.get("delivery_material"),
        "nodes": node_result, "relations": relation_result, "execution": execution_result,
        "unit_b": unit_b_result, "local_execution": local_execution_result,
        "failures": failures, "deferred_future": deferred,
    }
    status_values = [node_result, relation_result, execution_result, unit_b_result, local_execution_result]
    required_status = _aggregate(status_values, empty="satisfied")
    # A relation population whose owner is explicitly scheduled for a later
    # checkpoint remains visible and deferred, but it cannot block admission
    # at an earlier checkpoint when every required-now item is satisfied.
    # Keep the ordinary required status and future list intact; only the
    # current admission projection treats a deferred-only relation as
    # non-required.  A nonempty deferred inventory and an empty diagnostic
    # list are both required so a missing/current relation is never converted
    # into a blanket future allowance.  ``strong_complete`` below continues
    # to remain false while any future proof is present.
    admission_values = list(status_values)
    if (stage == "plan" and checkpoint == "plan" and
            relation_result.get("status") == "deferred" and
            relation_result.get("deferred_future") and
            not relation_result.get("diagnostics")) or (
            stage == "task" and checkpoint in PRECOMPLETION["task"] and
            relation_result.get("status") == "deferred" and
            relation_result.get("deferred_future") and
            not relation_result.get("diagnostics")):
        admission_values[1] = {**relation_result, "required": False}
    admission_status = _aggregate(admission_values, empty="satisfied")
    # Program-level evaluations intentionally have no Task membership row;
    # ``program`` is their valid canonical scope.  Task evaluations require an
    # exact satisfied membership, including every program the Task belongs to.
    membership_allowed = membership.get("state") in ({"program", "satisfied"} if task_ref is None else {"satisfied"})
    assurance_allow = admission_status == "satisfied" and not failures and membership_allowed
    # Future execution material and pending integration/delivery producers
    # remain explicit boundaries; plan/Task M/R uses the connected reader.
    strong = bool(assurance_allow and not deferred and profile["body"].get("application_mode") == "mandatory")
    return {
        "format": FORMAT, "version": VERSION, "project": project, "program": program,
        "selection": {"program": program, "profile_ref": selection.get("profile_ref"),
                      "head_event": selection.get("head_event")},
        "selection_state": selection.get("state"), "stage": stage, "checkpoint": checkpoint,
        "membership": membership,
        "global_denominator": {"format": denominator.get("format"), "digest": denominator.get("digest"),
                                "count": denominator.get("count"), "input_digest": denominator.get("input_digest")},
        "local_denominator": _copy(local) if local is not None else None,
        "nodes": node_result, "relations": relation_result, "execution": execution_result,
        "unit_b": unit_b_result, "local_execution": local_execution_result,
        "deferred_future": deferred, "capabilities": {
            "stage_evaluator": {"supported": True, "version": VERSION, "read_only": True},
            "relation_consumer": {
                "supported": relation_result.get("capability", {}).get("supported", False),
                "reason": relation_result.get("capability", {}).get("reason", "relation_consumer_unavailable"),
                "connected_relations": relation_result.get("capability", {}).get("connected_relations", []),
                "unsupported_relations": relation_result.get("capability", {}).get("unsupported_relations", []),
                "registry_digest": registry_digest,
            },
            "delivery_output_consumer": {
                "supported": stage in {"integration", "delivery"} and
                relation_result.get("capability", {}).get("supported", False),
                "reason": ("consumer_c_connected_produced_by_contains"
                           if stage in {"integration", "delivery"} and
                           relation_result.get("capability", {}).get("supported", False)
                           else "consumer_c_finite_relation_boundary"),
            },
            "system_enforcement": False,
            "unit4_unit5": {"supported": False, "reason": "writer_entrypoints_are_later_unit"},
        },
        "failures": failures,
        "status": required_status if not failures else _aggregate([
            {"status": item.get("status", "unknown"), "required": True} for item in failures
        ], empty=required_status),
        "assurance_allow": assurance_allow,
        "strong_complete": strong,
        "system_enforcement": False,
        "semantic_fingerprint": semantic_fingerprint,
        "report_snapshot": digest(proof_payload),
    }


def _evaluate_stage(control: Any, actor: Any, *, project: str, program: str, stage: str,
                    task: dict[str, Any] | None = None, delivery: dict[str, Any] | None = None,
                    checkpoint: str | None = None, proposed_breakdown: str | None = None,
                    local_execution: str | None = None,
                    _preadoption_identity: Any | None = None,
                    _active_memberships: bool = False,
                    _membership_programs: list[str] | None = None) -> dict[str, Any]:
    """Evaluate one stage from canonical controller records without writes."""
    need(type(project) is str and project and "\x00" not in project,
         "invalid_stage_context", "project is invalid")
    need(type(program) is str and program and "\x00" not in program,
         "invalid_stage_context", "program is invalid")
    need(stage in STAGES, "invalid_stage", "stage must be one of plan/task/integration/delivery", stage)
    if _preadoption_identity is not None:
        need(stage == "task", "invalid_stage_context",
             "Pre-adoption identity is only valid for the Task stage")
    if checkpoint is None:
        checkpoint = DEFAULT_CHECKPOINT[stage]
    need(type(checkpoint) is str and checkpoint in CHECKPOINTS[stage],
         "invalid_checkpoint", "checkpoint is not valid for this stage", checkpoint)
    if stage == "task":
        task = _ref(task, project, {"task_revision"}, name="task")
    else:
        need(task is None, "invalid_stage_context", "task selector is only valid for task stage")
    if stage in {"integration", "delivery"}:
        delivery = _ref(delivery, project, {"delivery_snapshot", "actual_delivery_commit"}, name="delivery")
    else:
        need(delivery is None, "invalid_stage_context", "delivery selector is not valid for plan/task stage")
    if proposed_breakdown is not None:
        need(type(proposed_breakdown) is str and proposed_breakdown and "\x00" not in proposed_breakdown,
             "invalid_stage_context", "proposed_breakdown selector must be a canonical id")
    if local_execution is not None:
        need(type(local_execution) is str and local_execution and "\x00" not in local_execution,
             "invalid_stage_context", "local_execution selector must be a canonical id")
    control.k.project(actor, project)
    control.s.one("SELECT id FROM programs WHERE id=? AND project=?", (program, project), True)

    membership_programs = [program]
    if task is not None:
        _membership_result, _row, memberships = _membership(
            control, actor, project, program, stage, task,
            active_only=_active_memberships,
            program_override=_membership_programs,
        )
        if program in memberships and len(memberships) > 1:
            membership_programs = memberships
    evaluations = []
    for item in membership_programs:
        # A single selector may be valid for the requested program while a
        # second mandatory membership needs its own canonical root.  Keep the
        # selector on its owning branch and let the other branch read its
        # active root or report the missing dependency.
        branch_breakdown = proposed_breakdown
        branch_local_execution = local_execution
        if len(membership_programs) > 1:
            branch_breakdown = _program_scoped_selector(
                control, project, item, proposed_breakdown, "breakdowns",
            )
            branch_local_execution = _program_scoped_selector(
                control, project, item, local_execution, "local_execution_proposals",
            )
        evaluations.append(_evaluate_one(
            control, actor, project=project, program=item, stage=stage,
            checkpoint=checkpoint, task_ref=task, delivery_ref=delivery,
            proposed_breakdown=branch_breakdown,
            local_execution=branch_local_execution,
            preadoption_identity=_preadoption_identity,
            active_memberships=_active_memberships,
            membership_programs=_membership_programs,
        ))
    if len(evaluations) == 1:
        return evaluations[0]
    first = evaluations[0]
    failures: list[dict[str, Any]] = []
    for item in evaluations:
        failures.extend([{**failure, "program": item["program"]} for failure in item.get("failures", [])])
    semantic = digest({"program_evaluations": [item["semantic_fingerprint"] for item in evaluations],
                       "task": task, "stage": stage, "checkpoint": checkpoint})
    assurance_allow = all(item.get("assurance_allow", False) for item in evaluations)
    strong = all(item.get("strong_complete", False) for item in evaluations)
    return {**first, "program": program, "selection": first.get("selection"),
            "membership": {**first.get("membership", {}), "all_programs_evaluated": membership_programs},
            "program_evaluations": evaluations, "failures": failures,
            "assurance_allow": assurance_allow, "strong_complete": strong,
            "status": _aggregate([{"status": item.get("status"), "required": True} for item in evaluations]),
            "semantic_fingerprint": semantic,
            "report_snapshot": digest({"semantic_fingerprint": semantic,
                                        "evaluations": [item.get("report_snapshot") for item in evaluations],
                                        "failures": failures})}


def _evaluate_pre_adoption(control: Any, actor: Any, identity: Any) -> dict[str, Any] | None:
    """Run the private Task candidate checkpoint from Runtime's sealed identity.

    This is deliberately the only stage entry that accepts a pre-adoption
    identity.  It is called inside Runtime's existing adoption transaction,
    after the durable observation resolver and before the Task UPDATE.  A
    project without a selected E3 profile retains the legacy optional-stage
    behavior; once any canonical profile is selected for the Task's program
    membership, the ordinary evaluator owns the admission decision.
    """
    from . import candidate_provenance

    sealed = candidate_provenance._verify_preadoption_identity(control, actor, identity)
    project = sealed.get("project")
    task_id = sealed.get("task")
    task_row = control.s.one(
        "SELECT body FROM tasks WHERE id=? AND project=?", (task_id, project), True,
    )
    need(task_row is not None, "stale_run", "Pre-adoption Task is no longer retained", task_id)
    local_execution = None
    local = getattr(control, "local_executions", None)
    claim_reader = getattr(getattr(control, "g", None), "local_executions", None)
    if local is not None and claim_reader is not None:
        claim = claim_reader.claimed(task_id, sealed.get("epoch"))
        if claim is not None:
            local_execution = claim.get("proposal")
    # ``workflow_id`` is optional Task input.  Admission starts from the
    # canonical active Breakdown memberships and the current certified local
    # proposal/claim owner, then evaluates that complete union with the
    # existing all-program AND.  A caller-supplied workflow cannot create a
    # private membership by itself.
    programs = sorted(set(_active_task_programs(control, project, task_id)))
    programs.extend(_local_task_programs(
        control, project, task_id, local_execution,
    ))
    programs = sorted(set(programs))
    if not programs:
        return None
    # Stage enforcement is opt-in through the canonical profile selection.  A
    # secondary membership still participates in the evaluator once any member
    # is selected; _evaluate_stage reports a missing/invalid branch instead of
    # allowing the caller to narrow scope.
    selected = [
        control.assurance.selected_profile(actor, project, program)
        for program in programs
    ]
    if not any(item.get("profile_ref") is not None for item in selected):
        return None

    task_ref = {
        "kind": "task_revision", "project": project, "task": task_id,
        "revision": sealed.get("task_revision"),
        "definition_digest": sealed.get("task_definition_digest"),
    }
    result = _evaluate_stage(
        control, actor, project=project, program=programs[0], stage="task",
        task=task_ref, checkpoint="candidate", local_execution=local_execution,
        _preadoption_identity=identity, _active_memberships=True,
        _membership_programs=programs,
    )
    if result.get("assurance_allow") is not True:
        raise Fault(
            "stage_assurance_blocked",
            "Task candidate checkpoint is not currently admissible",
            {"project": project, "task": task_id, "result": result},
        )
    return result


def evaluate_stage(control: Any, actor: Any, *, project: str, program: str, stage: str,
                   task: dict[str, Any] | None = None, delivery: dict[str, Any] | None = None,
                   checkpoint: str | None = None, proposed_breakdown: str | None = None,
                   local_execution: str | None = None) -> dict[str, Any]:
    """Evaluate one complete Unit 3 snapshot in a serialized read view.

    The lower-level context collector uses nested savepoints.  The outer
    transaction keeps selection, membership, denominator, node, relation,
    execution, and Unit B reads on one controller snapshot.
    """
    with control.s.transaction():
        return _evaluate_stage(control, actor, project=project, program=program, stage=stage,
                               task=task, delivery=delivery, checkpoint=checkpoint,
                               proposed_breakdown=proposed_breakdown,
                               local_execution=local_execution)


def report_stage(control: Any, actor: Any, *, project: str, program: str,
                 stage: str, checkpoint: str | None = None,
                 task: dict[str, Any] | None = None, delivery: dict[str, Any] | None = None,
                 proposed_breakdown: str | None = None, local_execution: str | None = None,
                 cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Build a bounded report from a complete evaluator snapshot."""
    need(type(limit) is int and 1 <= limit <= 500, "invalid_range", "Stage report limit is invalid")
    result = evaluate_stage(control, actor, project=project, program=program, stage=stage,
                            checkpoint=checkpoint, task=task, delivery=delivery,
                            proposed_breakdown=proposed_breakdown, local_execution=local_execution)
    items: list[dict[str, Any]] = []
    for failure in result.get("failures", []):
        item = {"program": failure.get("program", program), "kind": failure.get("kind", "stage"),
                "owner": failure.get("owner", ""), "obligation_id": failure.get("obligation_id", ""),
                "reason": failure.get("reason", failure.get("status", "unknown")),
                "status": failure.get("status", "unknown")}
        for key in ("code", "task", "relation", "selector", "node_ref", "program"):
            if key in failure:
                item[key] = _copy(failure[key])
        items.append(item)
    items.sort(key=lambda item: (item.get("program", ""), item.get("kind", ""), item.get("owner", ""),
                                 item.get("obligation_id", ""), item.get("reason", ""), item.get("status", "")))
    snapshot = result["report_snapshot"]
    offset = 0
    if cursor is not None:
        need(isinstance(cursor, str) and cursor, "invalid_cursor", "Stage report cursor is invalid")
        try:
            state = control.assurance._cursor_decode(cursor)
        except Exception as exc:
            raise Fault("invalid_cursor", "Stage report cursor is malformed") from exc
        need(state is not None and state.get("snapshot") == snapshot,
             "stale_cursor", "Stage report cursor is stale", {"restart": True})
        offset = state["offset"]
    page = items[offset:offset + limit]
    next_offset = offset + len(page)
    next_cursor = control.assurance._cursor_encode({"snapshot": snapshot, "offset": next_offset}) if next_offset < len(items) else None
    totals = {name: sum(1 for item in items if item.get("status") == name) for name in REPORT_STATUSES}
    totals["all"] = len(items)
    return {"format": REPORT_FORMAT, "project": project, "program": program, "stage": stage,
            "checkpoint": result["checkpoint"], "selection": result["selection"],
            "result": result, "items": page, "total": len(items), "offset": offset,
            "totals": totals, "next_cursor": next_cursor, "report_snapshot": snapshot,
            "semantic_fingerprint": result["semantic_fingerprint"],
            "stage_evaluator": True, "system_enforcement": False}


__all__ = ["CHECKPOINTS", "DEFAULT_CHECKPOINT", "evaluate_stage", "report_stage", "VERSION"]
