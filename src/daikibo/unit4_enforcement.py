"""Read-only Unit 4 plan-adoption enforcement.

Unit 3 owns the stage evaluator.  Unit 4 writers only need a small shared
boundary that binds that evaluator to the immutable program origin and the
canonical profile selection.  Keeping this function here prevents Planning,
Breakdowns, and LocalExecutions from growing subtly different profile or
origin checks.

The helper never writes a gate, profile, review, or workflow row.  Callers
perform the mutation only after the returned snapshot has passed their
existing gate and their own transaction has reread the mutable row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .common import Fault, digest, need, parse_json
from .program_origins import resolve_program_origin


@dataclass(frozen=True)
class LocalClaimBinding:
    """Canonical local responsibility selected by Task admission for a claim."""

    task: str
    program: str
    proposal: str
    certification_id: str
    certification_digest: str
    material_digest: str


def task_admission_local_claim_binding(admission: dict[str, Any], *, task: str) -> LocalClaimBinding | None:
    """Project admitted local branches into the claim writer's internal contract.

    The legacy readiness route describes one route.  Task admission evaluates
    every canonical owner, so the writer must bind any branch that it marks
    ``local`` even when another branch is currently owned by a root.
    """
    need(isinstance(admission, dict) and admission.get("allowed") is True,
         "task_admission_blocked", "Local claim binding requires successful canonical Task admission")
    branches = admission.get("programs")
    need(isinstance(branches, list), "integrity_error",
         "Canonical Task admission did not retain its program branches")
    local_branches = [branch for branch in branches
                      if isinstance(branch, dict) and branch.get("route") == "local"]
    if not local_branches:
        return None
    # Current local authorization selects one proposal and therefore one
    # canonical program owner.  If that contract ever broadens, it needs an
    # explicit multi-claim transaction model instead of silently dropping an
    # owner here.
    need(len(local_branches) == 1, "integrity_error",
         "Task admission selected multiple local owners for one claim")
    selection = admission.get("local_selection")
    need(isinstance(selection, dict) and selection.get("state") == "resolved",
         "integrity_error", "Task admission local branch has no resolved selection")
    authorization = selection.get("authorization")
    proposal = selection.get("selector")
    program = selection.get("program")
    need(isinstance(authorization, dict) and authorization.get("allowed") is True,
         "integrity_error", "Task admission local branch has no current authorization")
    need(type(proposal) is str and bool(proposal) and "\x00" not in proposal and
         authorization.get("proposal") == proposal,
         "integrity_error", "Task admission local selector differs from its authorization")
    need(type(program) is str and bool(program) and "\x00" not in program,
         "integrity_error", "Task admission local selection has no canonical program")
    branch = local_branches[0]
    branch_local = branch.get("local")
    need(branch.get("program") == program and isinstance(branch_local, dict) and
         branch_local.get("selector") == proposal and
         branch_local.get("authorization") == authorization,
         "integrity_error", "Task admission local branch differs from its selected authorization")
    certification = authorization.get("certification")
    need(isinstance(certification, dict) and
         type(certification.get("id")) is str and bool(certification["id"]) and
         type(certification.get("digest")) is str and bool(certification["digest"]),
         "integrity_error", "Task admission local authorization has no canonical certification")
    material_digest = authorization.get("material_digest")
    need(type(material_digest) is str and bool(material_digest),
         "integrity_error", "Task admission local authorization has no material digest")
    return LocalClaimBinding(
        task=task, program=program, proposal=proposal,
        certification_id=certification["id"],
        certification_digest=certification["digest"],
        material_digest=material_digest,
    )


def _failure(code: str, message: str, **details: Any) -> dict[str, Any]:
    value = {"code": code, "reason": message, "status": "unknown"}
    value.update(details)
    return value


def _local_task_refs(control: Any, *, project: str, program: str,
                     local_execution: str,
                     proposed_breakdown: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Resolve a local proposal into canonical Task selectors.

    A local proposal is a selector for a composed root and a finite selected
    Task set.  The Unit3 task evaluator consumes each immutable Task reference
    directly; it does not receive the proposal as a substitute for the global
    program denominator.  This keeps an unselected analysis Task out of the
    local projection while preserving the evaluator's all-program membership
    check for every selected Task.
    """
    from .assurance_denominators import _task_ref
    from .assurance_stage import _local_execution_proposed_breakdown

    need(type(local_execution) is str and local_execution and "\x00" not in local_execution,
         "invalid_stage_context", "local_execution selector must be a canonical id")
    row = control.s.one(
        "SELECT * FROM local_execution_proposals WHERE id=?", (local_execution,),
    )
    need(row is not None, "missing_evidence", "Local execution proposal is not retained", local_execution)
    need(row["project"] == project and row["program"] == program,
         "cross_project", "Local execution selector belongs to another project/program", local_execution)
    body = parse_json(row["body"])
    need(isinstance(body, dict) and digest(body) == row["digest"],
         "integrity_error", "Local execution proposal content differs", local_execution)
    tasks = body.get("tasks")
    need(isinstance(tasks, list) and tasks and
         all(type(item) is str and item and "\x00" not in item for item in tasks),
         "missing_evidence", "Local execution proposal has no canonical selected Tasks", local_execution)
    need(len(tasks) == len(set(tasks)), "integrity_error",
         "Local execution proposal repeats a selected Task", local_execution)
    root = _local_execution_proposed_breakdown(control, project, program, local_execution)
    need(root is not None, "missing_evidence", "Local execution proposal has no composed root", local_execution)
    if proposed_breakdown is not None:
        need(proposed_breakdown == root, "invalid_reference",
             "Local execution proposal and proposed breakdown differ", local_execution)
    refs = []
    for task in tasks:
        task_row = control.s.one("SELECT * FROM tasks WHERE id=?", (task,))
        need(task_row is not None, "missing_evidence", "Selected local Task is not retained", task)
        need(task_row["project"] == project, "cross_project",
             "Selected local Task belongs to another project", task)
        refs.append(_task_ref(project, task_row))
    return root, refs


def _local_task_evaluation(control: Any, actor: Any, *, project: str, program: str,
                           root: str, task_refs: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the selected local Tasks through the shared task reader.

    The readonly local primitive has already checked local packets and
    current authorization.  Calling the task evaluator here supplies the
    canonical current Task denominator and all-program membership without
    passing ``local_execution`` back into the local primitive.
    """
    assurance = control.assurance
    evaluations = [
        assurance.evaluate_stage(
            actor, project, program, "task", task=task_ref,
            checkpoint="ready", proposed_breakdown=root,
        )
        for task_ref in task_refs
    ]
    if len(evaluations) == 1:
        return evaluations[0]
    # Keep each immutable selector paired with the result produced for that
    # selector.  A comprehension-local ``task_ref`` cannot be reused while
    # aggregating failures below (and, on Python 3, is not defined there at
    # all).  The explicit pairing also prevents a failure from being reported
    # against a neighbouring selected Task when the projection contains more
    # than one Task.
    paired = list(zip(task_refs, evaluations))
    need(len(paired) == len(task_refs) == len(evaluations),
         "integrity_error", "Selected Task references and evaluations differ")
    failures = []
    for task_ref, item in paired:
        failures.extend([
            {**failure, "task": task_ref["task"]}
            for failure in item.get("failures", [])
        ])
    semantic = digest({
        "format": "daikibo.unit4-local-task-evaluations.v1",
        "tasks": task_refs,
        "evaluations": [item.get("semantic_fingerprint") for item in evaluations],
    })
    return {
        **evaluations[0],
        "stage": "task", "checkpoint": "ready",
        "task_evaluations": evaluations,
        "failures": failures,
        "deferred_future": [
            future for item in evaluations for future in item.get("deferred_future", [])
        ],
        "assurance_allow": all(item.get("assurance_allow") is True for item in evaluations),
        "strong_complete": all(item.get("strong_complete") is True for item in evaluations),
        "status": ("satisfied" if all(item.get("status") == "satisfied" for item in evaluations)
                   else "deferred" if all(item.get("status") in {"satisfied", "deferred"}
                                          for item in evaluations)
                   else "unknown"),
        "semantic_fingerprint": semantic,
        "report_snapshot": digest({
            "semantic_fingerprint": semantic,
            "reports": [item.get("report_snapshot") for item in evaluations],
            "failures": failures,
        }),
    }


TASK_ADMISSION_CHECKPOINTS = frozenset({"ready", "claim", "complete", "recheck"})
TASK_ADMISSION_FORMAT = "daikibo.unit4-task-admission.v1"


def _task_admission_task(control: Any, actor: Any, task: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the current Task row and derive its immutable evaluator selector."""
    from .assurance_denominators import _task_ref

    need(type(task) is str and task and "\x00" not in task,
         "invalid_task", "Task selector must be a canonical id")
    row = control.s.one("SELECT * FROM tasks WHERE id=?", (task,), True)
    control.k.project(actor, row["project"])
    return row, _task_ref(row["project"], row)


def _task_admission_local_proposals(control: Any, project: str, task: str) -> list[dict[str, Any]]:
    """Return retained local proposals that explicitly contain ``task``.

    This small read is also used when the local service is not composed.  A
    missing service is an acceptable empty route only when the canonical store
    proves that no local proposal exists; an existing proposal must not be
    turned into an empty population by a partial composition.
    """
    matches: list[dict[str, Any]] = []
    for row in control.s.all(
            "SELECT * FROM local_execution_proposals WHERE project=? ORDER BY created,id",
            (project,)):
        try:
            body = parse_json(row["body"])
        except Fault:
            # A malformed row cannot be selected by a valid provider, but it
            # still belongs to this project's local population.  Keep it in
            # the diagnostic population so the caller cannot infer absence.
            matches.append(row)
            continue
        if isinstance(body, dict) and task in body.get("tasks", []):
            matches.append(row)
    return matches


def _task_admission_known_local_programs(control: Any, project: str, task: str,
                                         selector: str | None = None) -> list[str]:
    """Retain only canonical program owners already proven by a proposal row."""
    if selector is None:
        rows = _task_admission_local_proposals(control, project, task)
    else:
        rows = control.s.all(
            "SELECT * FROM local_execution_proposals WHERE id=?", (selector,),
        )
    programs: set[str] = set()
    for proposal in rows:
        if proposal.get("project") != project:
            continue
        if selector is None:
            try:
                body = parse_json(proposal["body"])
            except Fault:
                continue
            if not isinstance(body, dict) or task not in body.get("tasks", []):
                continue
        program = proposal.get("program")
        if (type(program) is str and program and
                control.s.one(
                    "SELECT id FROM programs WHERE id=? AND project=?",
                    (program, project),
                ) is not None):
            programs.add(program)
    return sorted(programs)


def _task_admission_local_selection(control: Any, actor: Any, row: dict[str, Any],
                                    checkpoint: str) -> dict[str, Any]:
    """Resolve only the current local selector relevant to this checkpoint.

    Before a claim exists, the readonly authorization primitive chooses the
    current certified proposal.  Complete/recheck are bound to the current
    epoch's durable claim instead.  A failed choice can still expose its
    proposal; the caller retains that owner in the program union so a stale
    local branch cannot disappear from the AND evaluation.
    """
    local = getattr(control, "local_executions", None)
    result: dict[str, Any] = {
        "state": "absent", "selector": None, "program": None,
        "programs": [],
        "authorization": None, "claim": None, "failures": [],
    }
    project = row["project"]
    task = row["id"]
    if local is None:
        if _task_admission_local_proposals(control, project, task):
            result["state"] = "invalid"
            result["programs"] = _task_admission_known_local_programs(
                control, project, task,
            )
            if len(result["programs"]) == 1:
                result["program"] = result["programs"][0]
            result["failures"].append(_failure(
                "admission_dependency_unavailable",
                "Canonical local admission provider is unavailable",
            ))
        return result
    selector: str | None = None
    try:
        pinned = None
        if checkpoint in {"complete", "recheck"}:
            claim = local.claimed(task, row["epoch"])
            result["claim"] = (
                {key: claim.get(key) for key in ("id", "digest", "task", "epoch", "created")}
                if claim is not None else None
            )
            if claim is None:
                authorization = local.current_authorization_readonly(
                    actor, task, checkpoint,
                )
                result["authorization"] = authorization
                selector = (authorization.get("proposal")
                            if isinstance(authorization, dict) else None)
                if selector is None and not _task_admission_local_proposals(control, project, task):
                    return result
                if selector is None:
                    result["state"] = "invalid"
                    result["programs"] = _task_admission_known_local_programs(
                        control, project, task,
                    )
                    if len(result["programs"]) == 1:
                        result["program"] = result["programs"][0]
                    result["failures"].append(_failure(
                        "local_claim_missing",
                        "Current local proposal has no durable Task claim",
                    ))
                result["selector"] = selector
            else:
                # Decode enough of a corrupt claim to retain its known
                # proposal owner in the diagnostic population.  The digest,
                # Task, epoch, and certification checks below still decide
                # validity; no unverified field is used as authorization.
                claim_body = parse_json(claim["body"])
                if isinstance(claim_body, dict):
                    selector = claim_body.get("proposal")
                result["selector"] = selector
                need(isinstance(claim_body, dict) and digest(claim_body) == claim["digest"],
                     "integrity_error", "Current local claim content differs", claim["id"])
                need(claim_body.get("task") == task and claim_body.get("epoch") == row["epoch"],
                     "stale_reference", "Current local claim is bound to another Task epoch", claim["id"])
                pinned = claim_body.get("certified_event")
                need(type(selector) is str and selector and "\x00" not in selector,
                     "invalid_local_selector", "Current local claim has no canonical proposal selector", claim["id"])
                need(isinstance(pinned, dict) and
                     type(pinned.get("id")) is str and type(pinned.get("digest")) is str,
                     "integrity_error", "Current local claim has no sealed certification reference", claim["id"])
        else:
            authorization = local.current_authorization_readonly(
                actor, task, checkpoint,
            )
            result["authorization"] = authorization
            selector = (authorization.get("proposal")
                        if isinstance(authorization, dict) else None)
            if selector is None and not _task_admission_local_proposals(control, project, task):
                return result
            if selector is None:
                result["state"] = "invalid"
                result["programs"] = _task_admission_known_local_programs(
                    control, project, task,
                )
                if len(result["programs"]) == 1:
                    result["program"] = result["programs"][0]
                result["failures"].append(_failure(
                    "local_selection_unavailable",
                    "Retained local proposals have no current selector",
                ))
        authorization = local.current_authorization_readonly(
            actor, task, checkpoint, pinned,
        ) if selector is not None and not (checkpoint in {"complete", "recheck"} and result["claim"] is None) else result.get("authorization")
        if isinstance(authorization, dict):
            authorized_selector = authorization.get("proposal")
            if selector is not None and authorized_selector not in {None, selector}:
                raise Fault(
                    "stale_reference",
                    "Current local authorization selected a different proposal than the claim",
                    {"claim": selector, "current": authorized_selector},
                )
            selector = authorized_selector or selector
            result["authorization"] = authorization
        result["selector"] = selector
    except Fault as exc:
        result["state"] = "invalid"
        result["programs"] = _task_admission_known_local_programs(
            control, project, task, selector,
        )
        if len(result["programs"]) == 1:
            result["program"] = result["programs"][0]
        result["failures"].append(_failure(
            exc.code, str(exc), error=exc.as_dict(), status="unknown",
        ))

    selector = result.get("selector")
    if selector is None:
        if result["state"] != "invalid":
            result["state"] = "absent"
        return result
    if type(selector) is not str or not selector or "\x00" in selector:
        result["state"] = "invalid"
        result["failures"].append(_failure(
            "invalid_local_selector", "Local authorization did not retain a canonical proposal selector",
            selector=selector,
        ))
        return result
    proposal = control.s.one(
        "SELECT * FROM local_execution_proposals WHERE id=?", (selector,),
    )
    if proposal is None:
        result["state"] = "invalid"
        result["failures"].append(_failure(
            "local_execution_missing", "Selected local execution proposal is not retained",
            local_execution=selector,
        ))
        return result
    if proposal["project"] != row["project"]:
        result["state"] = "invalid"
        result["failures"].append(_failure(
            "cross_project", "Selected local execution proposal belongs to another project",
            local_execution=selector,
        ))
        return result
    try:
        proposal_body = parse_json(proposal["body"])
        need(isinstance(proposal_body, dict) and digest(proposal_body) == proposal["digest"],
             "integrity_error", "Selected local execution proposal content differs", selector)
        need(row["id"] in proposal_body.get("tasks", []),
             "local_execution_task_mismatch", "Selected local execution proposal does not include the Task",
             selector)
    except Fault as exc:
        result["state"] = "invalid"
        result["failures"].append(_failure(
            exc.code, str(exc), error=exc.as_dict(), local_execution=selector,
        ))
        return result
    program = proposal["program"]
    if type(program) is not str or not program:
        result["state"] = "invalid"
        result["failures"].append(_failure(
            "local_execution_owner_missing", "Selected local proposal has no canonical program owner",
            local_execution=selector, program=program,
        ))
        return result
    result["program"] = program
    result["programs"] = [program]
    if control.s.one(
            "SELECT id FROM programs WHERE id=? AND project=?", (program, row["project"])) is None:
        result["state"] = "invalid"
        result["failures"].append(_failure(
            "local_execution_owner_missing", "Selected local proposal has no canonical program owner",
            local_execution=selector, program=program,
        ))
        return result
    if result["state"] != "invalid":
        result["state"] = "resolved"
    return result


def _task_admission_selection_ref(selection: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(selection, dict):
        return None
    return {key: selection.get(key) for key in (
        "program", "profile_ref", "head_event", "application_mode", "profile_format",
        "effective_relation_contract_digest", "state", "status",
    )}


def _task_admission_disabled_authority(control: Any, actor: Any, project: str,
                                       selection: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the source-backed current disable selection without a stage read."""
    assurance = getattr(control, "assurance", None)
    try:
        profile_ref = selection.get("profile_ref")
        need(isinstance(profile_ref, dict) and isinstance(profile_ref.get("object"), str),
             "profile_invalid", "Disabled selection has no canonical profile object")
        profile = assurance.object_get(actor, project, profile_ref["object"])
        body = profile.get("body")
        need(isinstance(body, dict) and body.get("application_mode") == "disabled",
             "profile_invalid", "Current disabled selection body differs")
        refs = body.get("authority_refs")
        need(isinstance(refs, list) and refs,
             "disabled_authority_missing", "Disabled selection has no source-backed authority")
        validator = getattr(assurance, "_validate_profile_records", None)
        need(callable(validator), "assurance_unavailable", "Profile authority validator is unavailable")
        validator(actor, project, body, for_adoption=True)
    except Fault as exc:
        return _failure(exc.code, str(exc), error=exc.as_dict(), status="unknown")
    return None


def _task_admission_root_current(control: Any, actor: Any, task: str, program: str) -> tuple[bool, list[dict[str, Any]]]:
    governance = getattr(control, "g", None)
    if governance is None and hasattr(control, "root_execution_current_for_program_readonly"):
        # An explicitly bound standalone Governance is itself the canonical
        # reader root.  This preserves the same Store-backed currentness
        # proof without manufacturing a Control or treating missing services
        # as an empty allow population.
        governance = control
    if governance is None or not hasattr(governance, "root_execution_current_for_program_readonly"):
        return False, [_failure(
            "root_currentness_unavailable",
            "Canonical root currentness reader is unavailable",
        )]
    try:
        return bool(governance.root_execution_current_for_program_readonly(actor, task, program)), []
    except Fault as exc:
        return False, [_failure(exc.code, str(exc), error=exc.as_dict())]


def _inspect_task_admission(control: Any, actor: Any, *, task: str,
                            checkpoint: str) -> dict[str, Any]:
    row, task_ref = _task_admission_task(control, actor, task)
    project = row["project"]
    from .assurance_stage import (
        _active_task_programs,
        _evaluate_stage,
        _local_task_programs,
    )

    membership_reader = getattr(control, "traceability", None)
    active_programs = _active_task_programs(control, project, task)
    membership_failure = None
    if membership_reader is None or not callable(getattr(membership_reader, "_programs_for_task", None)):
        # A missing canonical reader is not equivalent to an empty active
        # Breakdown.  Only the latter is a legitimate no-membership result.
        active_rows = control.s.all(
            "SELECT body FROM breakdowns WHERE project=? AND status='active'",
            (project,),
        )
        has_assignment = False
        for active_row in active_rows:
            try:
                active_body = parse_json(active_row["body"])
            except Fault:
                has_assignment = True
                break
            if any(task in unit.get("tasks", []) for unit in active_body.get("units", [])):
                has_assignment = True
                break
        if has_assignment:
            membership_failure = _failure(
                "admission_dependency_unavailable",
                "Canonical active membership reader is unavailable",
            )
    local = _task_admission_local_selection(control, actor, row, checkpoint)
    # Use the same canonical local-owner resolver as the private candidate
    # path.  A failed current authorization still retains its proposal owner
    # in this union so a stale branch cannot disappear from the AND result.
    local_programs = _local_task_programs(
        control, project, task, local.get("selector"),
    )
    local_programs.extend(
        item for item in local.get("programs", []) if isinstance(item, str)
    )
    if local.get("program") and local["program"] not in local_programs:
        local_programs.append(local["program"])
    local_programs = sorted(set(local_programs))
    programs = sorted(set(active_programs) | set(local_programs))
    assurance = getattr(control, "assurance", None)
    root_readings: dict[str, tuple[bool, list[dict[str, Any]]]] = {
        program: _task_admission_root_current(control, actor, task, program)
        for program in active_programs
    }
    branches: list[dict[str, Any]] = []
    mandatory_programs: list[str] = []

    for program in programs:
        root_current, root_failures = root_readings.get(program, (False, []))
        local_owner = program == local.get("program")
        claim_bound = local.get("claim") is not None
        if local_owner and local.get("state") in {"resolved", "invalid"}:
            # A current epoch claim fixes the local responsibility through
            # complete/recheck.  Before a claim, a current adopted root owns
            # the route; an older/withdrawn local proposal does not block that
            # explicit root takeover.  An integrity-invalid local selection
            # remains a global failure even when root is selected.
            route = ("local" if claim_bound or not (program in active_programs and root_current)
                     else "root")
        elif program in active_programs:
            route = "root"
        elif local_owner:
            route = "local"
        else:
            route = "unowned"
        branch: dict[str, Any] = {
            "program": program, "origin": None, "selection": None,
            "route": route,
            "root": {"active": program in active_programs, "current": root_current,
                      "failures": root_failures},
            "local": {
                "selector": local.get("selector") if program == local.get("program") else None,
                "authorization": local.get("authorization") if program == local.get("program") else None,
            },
            "evaluation": None, "allowed": False, "strong_complete": False,
            "failures": [], "required": True,
        }
        if program == local.get("program"):
            if local.get("state") == "invalid" or route == "local":
                branch["failures"].extend(local.get("failures", []))
        if route == "root" and program in active_programs:
            # A root branch is authoritative only with a current root proof.
            # Preserve the concrete reader fault where one exists and add a
            # stable denial when the canonical root is not current.  Phase
            # labels do not transfer this responsibility to Unit3: a root
            # route needs current root proof at every Task checkpoint, while
            # a valid local route may carry the same Task explicitly.
            if root_failures:
                branch["failures"].extend(root_failures)
            elif not root_current:
                branch["failures"].append(_failure(
                    "root_execution_not_current",
                    "Canonical root execution is not current for this Task",
                ))
        try:
            branch["origin"] = resolve_program_origin(
                control.s, project=project, program=program,
            )
        except Fault as exc:
            branch["failures"].append(_failure(exc.code, str(exc), error=exc.as_dict()))
            branches.append(branch)
            continue

        if assurance is None or not hasattr(assurance, "selected_profile"):
            if branch["origin"]["policy"] == "legacy-preserved":
                branch["required"] = False
                branch["allowed"] = not branch["failures"]
                branch["reason"] = "legacy_unselected_profile_route"
            else:
                branch["failures"].append(_failure(
                    "assurance_unavailable", "Canonical assurance service is unavailable",
                ))
            branches.append(branch)
            continue
        try:
            selection = assurance.selected_profile(actor, project, program)
            branch["selection"] = _task_admission_selection_ref(selection)
        except Fault as exc:
            branch["failures"].append(_failure(exc.code, str(exc), error=exc.as_dict()))
            branches.append(branch)
            continue

        profile_selected = selection.get("profile_ref") is not None
        if not profile_selected:
            if branch["origin"]["policy"] == "legacy-preserved":
                branch["required"] = False
                branch["allowed"] = not branch["failures"]
                branch["reason"] = "legacy_unselected_profile_route"
            else:
                branch["failures"].append(_failure(
                    "canonical_profile_required",
                    "A current canonical assurance profile is required for Task admission",
                    selection_state=selection.get("state"),
                ))
                branch["reason"] = "canonical_profile_required"
            branches.append(branch)
            continue

        mode = selection.get("application_mode")
        if mode == "disabled":
            authority_failure = _task_admission_disabled_authority(
                control, actor, project, selection,
            )
            if authority_failure is not None:
                branch["failures"].append(authority_failure)
            if branch["route"] == "root" and branch["root"]["current"] and not branch["failures"]:
                branch["allowed"] = True
                branch["reason"] = "disabled_adopted_root_route"
            else:
                if not branch["failures"]:
                    branch["failures"].append(_failure(
                        "disabled_profile_not_admissible",
                        "A disabled profile cannot authorize a new root or local Task route",
                    ))
                branch["reason"] = "disabled_profile_not_admissible"
            branches.append(branch)
            continue

        if mode != "mandatory":
            branch["failures"].append(_failure(
                "mandatory_profile_required",
                "Task admission requires the current canonical mandatory profile",
                application_mode=mode,
            ))
            branches.append(branch)
            continue
        mandatory_programs.append(program)
        branches.append(branch)

    evaluations: dict[str, dict[str, Any]] = {}
    if mandatory_programs:
        local_route_program = None
        if (local.get("state") in {"resolved", "invalid"} and
                local.get("program") in mandatory_programs):
            local_route_program = next(
                (branch["program"] for branch in branches
                 if branch["program"] == local.get("program") and branch["route"] == "local"),
                None,
            )
        try:
            evaluation = _evaluate_stage(
                control, actor, project=project, program=mandatory_programs[0], stage="task",
                task=task_ref, checkpoint=checkpoint,
                local_execution=(local.get("selector") if local_route_program is not None else None),
                _active_memberships=True, _membership_programs=mandatory_programs,
            )
            if isinstance(evaluation.get("program_evaluations"), list):
                evaluations = {
                    item.get("program"): item
                    for item in evaluation["program_evaluations"]
                    if isinstance(item, dict) and isinstance(item.get("program"), str)
                }
            else:
                evaluations[evaluation.get("program", mandatory_programs[0])] = evaluation
        except Fault as exc:
            failure = _failure(exc.code, str(exc), error=exc.as_dict())
            evaluations = {
                program: {"program": program, "assurance_allow": False,
                          "strong_complete": False, "failures": [failure]}
                for program in mandatory_programs
            }

    for branch in branches:
        if branch["program"] not in mandatory_programs:
            continue
        evaluation = evaluations.get(branch["program"])
        branch["evaluation"] = evaluation
        if evaluation is None:
            branch["failures"].append(_failure(
                "stage_evaluation_missing", "Task stage evaluation did not return this canonical program",
            ))
            continue
        branch["failures"].extend(evaluation.get("failures", []))
        branch["allowed"] = evaluation.get("assurance_allow") is True and not branch["failures"]
        branch["strong_complete"] = evaluation.get("strong_complete") is True and not branch["failures"]
        branch["reason"] = "task_stage_satisfied" if branch["allowed"] else "task_stage_blocked"

    failures: list[dict[str, Any]] = []
    if membership_failure is not None:
        failures.append({"program": None, **membership_failure})
    if local.get("state") == "invalid" and not local.get("program"):
        failures.extend({"program": None, **failure} for failure in local.get("failures", []))
    for branch in branches:
        for failure in branch.get("failures", []):
            if isinstance(failure, dict):
                failures.append({"program": branch["program"], **failure})
            else:
                failures.append({"program": branch["program"], "code": "invalid_failure",
                                 "reason": str(failure), "status": "unknown"})
    resolution_success = membership_failure is None and local.get("state") != "invalid"
    allowed = resolution_success and (not programs or all(branch.get("allowed") is True for branch in branches))
    strong = bool(branches) and allowed and all(
        branch.get("strong_complete") is True for branch in branches
    )
    semantic = digest({
        "format": TASK_ADMISSION_FORMAT, "task": task_ref, "checkpoint": checkpoint,
        "programs": [{
            "program": branch["program"], "origin": branch["origin"],
            "selection": branch["selection"], "route": branch["route"],
            "root": branch["root"],
            "local_selector": branch["local"].get("selector"),
            "evaluation": ((branch.get("evaluation") or {}).get("semantic_fingerprint")),
        } for branch in branches],
    })
    proof = {
        "task": task_ref, "checkpoint": checkpoint, "canonical_programs": programs,
        "programs": branches, "local_selection": local,
        "failures": failures,
    }
    return {
        "format": TASK_ADMISSION_FORMAT, "task": task_ref, "checkpoint": checkpoint,
        "allowed": allowed, "strong_complete": strong,
        "canonical_programs": programs, "programs": branches,
        "local_selection": local, "failures": failures,
        "reason": ("task_admission_not_applicable" if not programs else
                    "task_admission_satisfied" if allowed else "task_admission_blocked"),
        "semantic_fingerprint": semantic,
        "report_snapshot": digest(proof),
        "proof_digest": digest({"semantic_fingerprint": semantic, "report_snapshot": digest(proof)}),
    }


def inspect_task_admission(control: Any, actor: Any, *, task: str,
                           checkpoint: str) -> dict[str, Any]:
    """Read the current canonical Task admission projection.

    This is deliberately an internal, fixed-checkpoint reader.  It emits no
    gate, claim, candidate, profile, CAS, or workflow row; writer callers
    combine ``allowed`` with their existing checks inside their transaction.
    """
    need(checkpoint in TASK_ADMISSION_CHECKPOINTS,
         "invalid_checkpoint", "Task admission checkpoint is unsupported", checkpoint)
    with control.s.transaction():
        return _inspect_task_admission(control, actor, task=task, checkpoint=checkpoint)


def inspect_plan_gate(control: Any, actor: Any, *, project: str, program: str,
                      proposed_breakdown: str | None = None,
                      local_execution: str | None = None) -> dict[str, Any]:
    """Return the current Unit 4-P plan-adoption proof.

    A migrated legacy program with no canonical profile retains its historical
    unselected route.  Every new ``program.begin`` origin, and every legacy
    program that *does* have a selected profile, requires a current canonical
    mandatory profile and a passing Unit 3 ``plan`` evaluation.  The profile
    is always resolved from the current head; no caller-supplied profile or
    previous report is accepted.

    ``proposed_breakdown`` is an ID selector only.  It is used for a proposed
    root during activation/local certification and is validated by the stage
    context collector itself.
    """
    origin = resolve_program_origin(control.s, project=project, program=program)
    assurance = getattr(control, "assurance", None)
    if assurance is None or not hasattr(assurance, "selected_profile"):
        if origin["policy"] == "legacy-preserved":
            return {
                "format": "daikibo.unit4-plan-gate.v1",
                "allowed": True,
                "required": False,
                "reason": "legacy_unselected_profile_route",
                "origin": {key: origin[key] for key in
                            ("program", "project", "policy", "origin", "digest")},
                "selection": None,
                "stage": "plan", "checkpoint": "plan",
                "evaluation": None, "semantic_fingerprint": None,
                "report_snapshot": None, "failures": [],
            }
        return {
            "format": "daikibo.unit4-plan-gate.v1", "allowed": False,
            "required": True, "reason": "assurance_unavailable",
            "origin": {key: origin[key] for key in
                        ("program", "project", "policy", "origin", "digest")},
            "selection": None, "stage": "plan", "checkpoint": "plan",
            "evaluation": None, "semantic_fingerprint": None,
            "report_snapshot": None,
            "failures": [_failure("assurance_unavailable", "Canonical assurance service is unavailable")],
        }

    selection = assurance.selected_profile(actor, project, program)
    origin_ref = {key: origin[key] for key in
                  ("program", "project", "policy", "origin", "digest")}
    selection_ref = {
        key: selection.get(key)
        for key in ("program", "profile_ref", "head_event", "application_mode",
                    "profile_format", "effective_relation_contract_digest",
                    "state", "status")
    }
    profile_selected = selection.get("profile_ref") is not None
    # Historical unselected programs remain compatible.  A selected legacy
    # profile is no longer an old-route exemption: it has to satisfy the same
    # current mandatory proof as a new program.
    if origin["policy"] == "legacy-preserved" and not profile_selected:
        return {
            "format": "daikibo.unit4-plan-gate.v1", "allowed": True,
            "required": False, "reason": "legacy_unselected_profile_route",
            "origin": origin_ref, "selection": selection_ref,
            "stage": "plan", "checkpoint": "plan", "evaluation": None,
            "semantic_fingerprint": None, "report_snapshot": None,
            "failures": [],
        }

    if not profile_selected:
        failures = [_failure(
            "canonical_profile_required",
            "A current canonical assurance profile is required for plan adoption",
            selection_state=selection.get("state"),
        )]
        return {
            "format": "daikibo.unit4-plan-gate.v1", "allowed": False,
            "required": True, "reason": "canonical_profile_required",
            "origin": origin_ref, "selection": selection_ref,
            "stage": "plan", "checkpoint": "plan", "evaluation": None,
            "semantic_fingerprint": None, "report_snapshot": None,
            "failures": failures,
        }

    if selection.get("application_mode") != "mandatory":
        failures = [_failure(
            "mandatory_profile_required",
            "Plan adoption requires the current canonical mandatory profile",
            application_mode=selection.get("application_mode"),
        )]
        return {
            "format": "daikibo.unit4-plan-gate.v1", "allowed": False,
            "required": True, "reason": "mandatory_profile_required",
            "origin": origin_ref, "selection": selection_ref,
            "stage": "plan", "checkpoint": "plan", "evaluation": None,
            "semantic_fingerprint": None, "report_snapshot": None,
            "failures": failures,
        }

    try:
        if local_execution is None:
            evaluation = assurance.evaluate_stage(
                actor, project, program, "plan", checkpoint="plan",
                proposed_breakdown=proposed_breakdown,
            )
            gate_stage, gate_checkpoint = "plan", "plan"
        else:
            # The local proposal selects a finite Task projection.  The
            # task-stage reader enforces current test-plan evidence and every
            # canonical program membership for each selected Task; it does not
            # pull unrelated Tasks from the program-wide plan denominator.
            root, task_refs = _local_task_refs(
                control, project=project, program=program,
                local_execution=local_execution,
                proposed_breakdown=proposed_breakdown,
            )
            evaluation = _local_task_evaluation(
                control, actor, project=project, program=program,
                root=root, task_refs=task_refs,
            )
            gate_stage, gate_checkpoint = "task", "ready"
    except Fault as exc:
        failure = _failure("stage_evaluation_failed", str(exc), error=exc.as_dict())
        return {
            "format": "daikibo.unit4-plan-gate.v1", "allowed": False,
            "required": True, "reason": "stage_evaluation_failed",
            "origin": origin_ref, "selection": selection_ref,
            "stage": "task" if local_execution is not None else "plan",
            "checkpoint": "ready" if local_execution is not None else "plan",
            "local_execution": local_execution,
            "evaluation": None,
            "semantic_fingerprint": None, "report_snapshot": None,
            "failures": [failure],
        }

    allowed = evaluation.get("assurance_allow") is True
    failures = list(evaluation.get("failures", []))
    if not allowed and not failures:
        failures = [_failure(
            "stage_assurance_blocked",
            "Current canonical plan evaluation did not authorize adoption",
            status=evaluation.get("status", "unknown"),
        )]
    return {
        "format": "daikibo.unit4-plan-gate.v1", "allowed": allowed,
        "required": True,
        "reason": "stage_plan_satisfied" if allowed else "stage_assurance_blocked",
        "origin": origin_ref, "selection": selection_ref,
        "stage": gate_stage, "checkpoint": gate_checkpoint,
        "local_execution": local_execution,
        "evaluation": evaluation,
        "semantic_fingerprint": evaluation.get("semantic_fingerprint"),
        "report_snapshot": evaluation.get("report_snapshot"),
        "failures": failures,
        "proof_digest": digest({
            "origin": origin_ref, "selection": selection_ref,
            "stage": gate_stage, "checkpoint": gate_checkpoint,
            "local_execution": local_execution,
            "semantic_fingerprint": evaluation.get("semantic_fingerprint"),
            "report_snapshot": evaluation.get("report_snapshot"),
        }),
    }


def require_plan_gate(control: Any, actor: Any, *, project: str, program: str,
                      proposed_breakdown: str | None = None,
                      local_execution: str | None = None) -> dict[str, Any]:
    """Raise at a writer boundary unless the current Unit 4-P proof passes."""
    result = inspect_plan_gate(
        control, actor, project=project, program=program,
        proposed_breakdown=proposed_breakdown,
        local_execution=local_execution,
    )
    if result["allowed"] is not True:
        raise Fault("stage_assurance_blocked",
                    "Current origin, canonical mandatory selection, and plan evaluation did not authorize this mutation",
                    result)
    return result


__all__ = ["inspect_plan_gate", "require_plan_gate", "inspect_task_admission"]
