"""Portable validation for execution-control history.

The execution-control tables are operational state in the live database.  This
module validates their exported, immutable projections only.  Reading an
archive never creates a claim, assessment, authorization, lease, or other live
admission state.

The validator deliberately keeps legacy ``tasks.attempts`` visible.  A task
which predates schema 13 has no reconstructed claim row unless the archive
contains durable evidence for that exact claim.  That absence is an explicit
unknown historical fact rather than an invented attempt or no-progress
judgment.
"""
from __future__ import annotations

import math

from .common import Fault, canonical, digest, need, parse_json


FORMAT = "daikibo.execution-control-history.v1"
# ``inconclusive`` is an observed review disposition, but it is deliberately
# not a row in the finalized assessment table.  It remains in the ordinary
# proposal/reviewer receipt history and can be followed by a later decisive
# review without consuming the unique per-attempt slot.
JUDGMENTS = {"progress", "no_progress"}
PROPOSAL_STATUSES = {"proposed", "applied", "withdrawn", "superseded"}
EVENT_KINDS = {"applied", "withdrawn", "superseded"}
# ``reserved`` is a short-lived collector state written after a claim and
# before the durable implementer run row is bound.  Its run/receipt columns
# are intentionally nullable, just like ``claimed``; the archive must retain
# this valid crash-window state rather than rejecting a checkpoint taken in
# between those two writes.
ATTEMPT_STATUSES = {"claimed", "reserved", "running", "succeeded", "failed", "unknown", "finished", "claim_only"}

SECTIONS = (
    "tasks",
    "execution_attempts",
    "attempt_assessments",
    "execution_control_proposals",
    "execution_control_packets",
    "execution_control_events",
    "execution_control_authorizations",
)


def _json(value, name):
    if isinstance(value, str):
        return parse_json(value)
    need(value is not None, "invalid_archive", f"{name} is missing")
    return value


def _body(row, name="body"):
    value = _json(row.get(name), name)
    need(isinstance(value, dict), "invalid_archive", f"{name} must be an object")
    return value


def _canonical(value, name):
    """Reject values which cannot be represented in the portable JSON form."""
    try:
        canonical(value)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Fault("invalid_archive", f"{name} is not canonical JSON") from exc


def _nonempty(value, message):
    need(isinstance(value, str) and bool(value.strip()), "invalid_archive", message)
    return value


def _positive_int(value, message, nullable=False):
    if nullable and value is None:
        return None
    need(type(value) is int and value > 0, "invalid_archive", message)
    return value


def _nonnegative_int(value, message, nullable=False):
    if nullable and value is None:
        return None
    need(type(value) is int and value >= 0, "invalid_archive", message)
    return value


def _duration(value, name, nullable=False):
    if value is None and nullable:
        return
    need(type(value) in (int, float) and not isinstance(value, bool),
         "invalid_archive", f"{name} must be a finite positive duration")
    number = float(value)
    need(math.isfinite(number) and number > 0, "invalid_archive",
         f"{name} must be a finite positive duration")


def _optional_equal(body, field, row_value, aliases=()):
    """If a projection repeats a column in its immutable body, bind it."""
    for key in (field, *aliases):
        if key in body:
            need(body[key] == row_value, "invalid_archive",
                 f"Execution-control {field} binding differs")


def _decode_evidence(value):
    if isinstance(value, str):
        value = parse_json(value)
    need(isinstance(value, list), "invalid_archive", "Execution-control evidence must be a list")
    _canonical(value, "Execution-control evidence")
    return value


def _proposal_material(body):
    """Return immutable material regardless of the envelope used by a producer."""
    material = body.get("material", body)
    need(isinstance(material, dict), "invalid_archive", "Execution-control proposal material is malformed")
    _canonical(material, "Execution-control proposal material")
    return material


def _rows(each, section):
    return list(each(section))


def _target_from_proposal(proposal_body, material, task, attempts):
    target_info = proposal_body.get("target_attempt")
    if target_info is None:
        target_info = material.get("target_attempt")
    if target_info is not None:
        need(isinstance(target_info, dict), "invalid_archive", "Execution-control target attempt is malformed")
        target_epoch = target_info.get("epoch", target_info.get("attempt_epoch"))
        target_ordinal = target_info.get("ordinal", target_info.get("attempt_ordinal"))
    else:
        target_epoch = material.get("target_attempt_epoch", proposal_body.get("target_attempt_epoch"))
        target_ordinal = material.get("target_attempt_ordinal", proposal_body.get("target_attempt_ordinal"))
    _nonnegative_int(target_epoch, "Execution-control target epoch is invalid")
    _positive_int(target_ordinal, "Execution-control target ordinal is invalid", nullable=True)

    target = attempts.get((task, target_epoch))
    if target is not None:
        # Nullable ordinals are reserved for reconstructed legacy history;
        # every retained schema-13 execution_attempts row has an exact ordinal.
        need(target_ordinal == target.get("attempt_ordinal"),
             "invalid_archive", "Execution-control proposal target ordinal differs")
        for info in (target_info, material.get("target_attempt")):
            if not isinstance(info, dict):
                continue
            for field, aliases in (("task_revision", ("revision",)),
                                   ("task_binding", ("binding",)),
                                   ("implementer_run", ("run",)),
                                   ("implementer_receipt", ("receipt",))):
                for key in (field, *aliases):
                    if key in info:
                        need(info[key] == target.get(field), "invalid_archive",
                             "Execution-control proposal target identity differs")
        return target

    # A schema-13 migration does not manufacture execution_attempts rows for
    # dev17 history.  Such a target is portable only when the immutable
    # proposal explicitly marks it as legacy and retains run/claim evidence.
    legacy = bool((target_info or {}).get("legacy")) or bool(material.get("legacy_history"))
    need(legacy, "invalid_archive", "Execution-control proposal targets a missing attempt")
    run = (target_info or {}).get("run", (target_info or {}).get("implementer_run"))
    receipt = (target_info or {}).get("receipt", (target_info or {}).get("implementer_receipt"))
    claim_only = bool((target_info or {}).get("claim_only"))
    need(claim_only or (isinstance(run, str) and bool(run.strip()) and
                        isinstance(receipt, str) and bool(receipt.strip())),
         "invalid_archive", "Legacy execution-control target has no durable evidence")
    return {
        "task": task, "attempt_epoch": target_epoch,
        "attempt_ordinal": target_ordinal,
        "task_revision": (target_info or {}).get("revision", (target_info or {}).get("task_revision")),
        "task_binding": (target_info or {}).get("binding", (target_info or {}).get("task_binding")),
        "implementer_run": run, "implementer_receipt": receipt,
        "legacy": True, "claim_only": claim_only,
    }


def validate_execution_controls(get, each, project):
    """Validate schema-13 execution history in a portable archive.

    ``get(section, key)`` and ``each(section, ref=None)`` are the same small
    access protocol used by the other history validators.  They are
    intentionally read-only and may be backed by the archive inspector's
    temporary SQLite index.
    """

    task_rows = _rows(each, "tasks")
    tasks = {}
    task_ids = set()
    for row in task_rows:
        ident = row.get("id")
        need(isinstance(ident, str) and ident and ident not in task_ids,
             "invalid_archive", "Duplicate or missing execution task")
        task_ids.add(ident)
        need(row.get("project") == project, "invalid_archive", "Execution task belongs to another project")
        body = _body(row)
        _canonical(body, "Execution task body")
        _positive_int(row.get("revision"), "Execution task revision is invalid")
        _nonnegative_int(row.get("epoch"), "Execution task epoch is invalid")
        _nonnegative_int(row.get("attempts"), "Legacy task attempt telemetry is invalid")
        _nonnegative_int(row.get("no_progress_count", 0), "Cached no-progress count is invalid")
        tasks[ident] = row

    attempt_rows = _rows(each, "execution_attempts")
    attempts = {}
    attempt_ids = set()
    ordinals = {}
    for row in attempt_rows:
        ident = row.get("id")
        task = row.get("task")
        epoch = row.get("attempt_epoch")
        ordinal = row.get("attempt_ordinal")
        key = (task, epoch)
        need(isinstance(ident, str) and ident and ident not in attempt_ids,
             "invalid_archive", "Duplicate execution attempt")
        attempt_ids.add(ident)
        need(task in tasks and row.get("project") == project == tasks[task].get("project"),
             "invalid_archive", "Execution attempt has a missing or cross-project Task")
        _nonnegative_int(epoch, "Execution attempt epoch is invalid")
        _positive_int(ordinal, "Execution attempt ordinal is invalid")
        _positive_int(row.get("task_revision"), "Execution attempt Task revision is invalid")
        _nonempty(row.get("task_binding"), "Execution attempt binding is missing")
        need(row.get("status") in ATTEMPT_STATUSES, "invalid_archive", "Execution attempt status is invalid")
        body = _body(row)
        need(digest(body) == row.get("digest"), "invalid_archive", "Execution attempt body digest differs")
        _optional_equal(body, "task", task)
        _optional_equal(body, "project", project)
        _optional_equal(body, "attempt_epoch", epoch, ("epoch",))
        _optional_equal(body, "attempt_ordinal", ordinal, ("ordinal",))
        _optional_equal(body, "task_revision", row["task_revision"], ("revision",))
        _optional_equal(body, "task_binding", row["task_binding"], ("binding",))
        need(key not in attempts, "invalid_archive", "Duplicate Task attempt epoch")
        need(ordinal not in ordinals.setdefault(task, set()),
             "invalid_archive", "Duplicate Task attempt ordinal")
        ordinals[task].add(ordinal)
        # ``attempts`` is monotonic legacy telemetry.  It must never be lower
        # than a retained claim ordinal, while older unknown attempts need not
        # have a synthetic execution_attempts row.
        need(ordinal <= tasks[task]["attempts"],
             "invalid_archive", "Execution attempt exceeds legacy Task telemetry")
        run = row.get("implementer_run")
        receipt = row.get("implementer_receipt")
        need((run is None) == (receipt is None), "invalid_archive",
             "Execution attempt run and receipt must be paired")
        if run is not None:
            _nonempty(run, "Execution attempt run is empty")
            _nonempty(receipt, "Execution attempt receipt is empty")
        attempts[key] = row

    assessment_rows = _rows(each, "attempt_assessments")
    assessments = {}
    assessment_ids = {}
    assessment_id_set = set()
    no_progress = {task: 0 for task in tasks}
    for row in assessment_rows:
        task = row.get("task")
        epoch = row.get("attempt_epoch")
        key = (task, epoch)
        need(task in tasks and row.get("project") == project,
             "invalid_archive", "Assessment has a missing or cross-project Task")
        assessment_id = row.get("id")
        need(isinstance(assessment_id, str) and assessment_id and assessment_id not in assessment_id_set,
             "invalid_archive", "Duplicate or missing attempt assessment")
        assessment_id_set.add(assessment_id)
        _nonnegative_int(epoch, "Assessment attempt epoch is invalid")
        need(key not in assessments, "invalid_archive", "Duplicate attempt assessment")
        attempt = attempts.get(key)
        ordinal = row.get("attempt_ordinal")
        revision = row.get("task_revision")
        _positive_int(ordinal, "Assessment attempt ordinal is invalid", nullable=True)
        _positive_int(revision, "Assessment Task revision is invalid", nullable=True)
        binding = _nonempty(row.get("task_binding"), "Assessment Task binding is missing")
        if attempt is not None:
            need(ordinal == attempt.get("attempt_ordinal") and
                 revision == attempt.get("task_revision") and
                 binding == attempt.get("task_binding"),
                 "invalid_archive", "Assessment Task revision/epoch binding differs")
        body = _body(row)
        need(digest(body) == row.get("digest"), "invalid_archive", "Assessment body digest differs")
        for field, value, aliases in (
            ("task", task, ()), ("project", project, ()),
            ("attempt_epoch", epoch, ("epoch",)),
            ("attempt_ordinal", ordinal, ("ordinal",)),
            ("task_revision", revision, ()),
            ("task_binding", binding, ("binding",)),
        ):
            _optional_equal(body, field, value, aliases)
        need(row.get("judgment") in JUDGMENTS,
             "invalid_archive", "Unknown attempt assessment judgment")
        _nonempty(row.get("rationale"), "Attempt assessment rationale is missing")
        evidence = _decode_evidence(row.get("evidence"))
        _optional_equal(body, "evidence", evidence)
        # A finalized assessment cannot be invented for a claim-only or lost
        # run.  Inconclusive review receipts are not rows here, so they never
        # contribute to the cached counter below.
        _nonempty(row.get("implementer_run"), "Assessment implementer run is missing")
        _nonempty(row.get("implementer_receipt"), "Assessment implementer receipt is missing")
        if attempt is not None:
            need(row["implementer_run"] == attempt.get("implementer_run") and
                 row["implementer_receipt"] == attempt.get("implementer_receipt"),
                 "invalid_archive", "Assessment implementer evidence differs")
        _nonempty(row.get("reviewer_run"), "Assessment reviewer run is missing")
        _nonempty(row.get("reviewer_receipt"), "Assessment reviewer receipt is missing")
        need(row["reviewer_run"] != row["implementer_run"],
             "invalid_archive", "Assessment reviewer is not independent")
        _nonempty(row.get("proposal"), "Attempt assessment proposal is missing")
        _nonempty(row.get("proposal_digest"), "Attempt assessment proposal digest is missing")
        assessment_ids[assessment_id] = row
        assessments[key] = row
        if row["judgment"] == "no_progress":
            no_progress[task] += 1

    for task, row in tasks.items():
        need(row.get("no_progress_count", 0) == no_progress[task],
             "invalid_archive", "Cached no-progress count differs from assessment history")

    proposal_rows = _rows(each, "execution_control_proposals")
    proposals = {}
    proposal_targets = {}
    for row in proposal_rows:
        ident = row.get("id")
        task = row.get("task")
        need(isinstance(ident, str) and ident and ident not in proposals,
             "invalid_archive", "Duplicate execution-control proposal")
        need(row.get("project") == project and task in tasks,
             "invalid_archive", "Execution-control proposal has a missing or cross-project Task")
        need(row.get("status") in PROPOSAL_STATUSES,
             "invalid_archive", "Unknown execution-control proposal state")
        _positive_int(row.get("task_revision"), "Execution-control proposal revision is invalid")
        _nonempty(row.get("binding"), "Execution-control proposal binding is missing")
        body = _body(row)
        need(digest(body) == row.get("digest"),
             "invalid_archive", "Execution-control proposal body digest differs")
        need(row["binding"] == digest({"proposal": ident, "body": body}),
             "invalid_archive", "Execution-control proposal binding differs")
        material = _proposal_material(body)
        if isinstance(material.get("task"), dict):
            need(material["task"].get("id") == task and material["task"].get("project") == project,
                 "invalid_archive", "Execution-control material Task identity differs")
            _optional_equal(material["task"], "revision", row["task_revision"])
        else:
            _optional_equal(material, "task", task)
        _optional_equal(material, "project", project)
        _optional_equal(material, "task_revision", row["task_revision"])
        _optional_equal(body, "task", task)
        _optional_equal(body, "project", project)
        _optional_equal(body, "task_revision", row["task_revision"])
        if "material" in body and "material_digest" in body:
            need(body["material_digest"] == digest(material),
                 "invalid_archive", "Execution-control material digest differs")

        request = body.get("request", body)
        need(isinstance(request, dict), "invalid_archive", "Execution-control proposal request is malformed")
        control_type = body.get("control_type", request.get("control_type"))
        need(control_type in {"assessment", "recovery", "timeout"},
             "invalid_archive", "Unknown execution-control proposal type")
        if "control_type" in body and "control_type" in request:
            need(body["control_type"] == request["control_type"],
                 "invalid_archive", "Execution-control proposal type differs")
        target = _target_from_proposal(body, material, task, attempts)
        proposal_targets[ident] = target
        requested = request.get("requested_seconds", material.get("requested_seconds", body.get("requested_seconds")))
        if control_type == "timeout":
            _duration(requested, "requested_seconds")
        else:
            need(requested is None, "invalid_archive", "Non-timeout proposal requests a duration")
        old_effective = request.get("old_effective_seconds", material.get("old_effective_seconds", body.get("old_effective_seconds")))
        _duration(old_effective, "old_effective_seconds", nullable=True)
        for field in ("cause_analysis", "intended_next_action"):
            _nonempty(request.get(field, material.get(field)), f"Execution-control {field} is missing")
        for field in ("experiment_estimate", "scope"):
            value = request.get(field, material.get(field))
            need((isinstance(value, dict) or (isinstance(value, str) and bool(value.strip()))),
                 "invalid_archive", f"Execution-control {field} is missing")
            _canonical(value, f"Execution-control {field}")
        _decode_evidence(request.get("evidence", material.get("evidence")))
        if control_type == "recovery":
            action = request.get("recovery_action", material.get("recovery_action"))
            _nonempty(action, "Recovery proposal action is missing")
        proposals[ident] = row

    packet_rows = _rows(each, "execution_control_packets")
    packets = {ident: [] for ident in proposals}
    for row in packet_rows:
        proposal = row.get("proposal")
        need(row.get("project") == project and proposal in proposals,
             "invalid_archive", "Execution-control packet refers to another proposal/project")
        body = _body(row)
        need(digest(body) == row.get("digest"),
             "invalid_archive", "Execution-control packet digest differs")
        ordinal = row.get("ordinal")
        _nonnegative_int(ordinal, "Execution-control packet ordinal is invalid")
        _optional_equal(body, "id", row.get("id"))
        _optional_equal(body, "proposal", proposal)
        _optional_equal(body, "project", project)
        _optional_equal(body, "ordinal", ordinal)
        fragment = body.get("serialized_fragment")
        if fragment is not None:
            need(isinstance(fragment, str), "invalid_archive", "Execution-control packet fragment is malformed")
            start, end = body.get("start"), body.get("end")
            _nonnegative_int(start, "Execution-control packet start is invalid")
            need(type(end) is int and end == start + len(fragment) and end > start,
                 "invalid_archive", "Execution-control packet range is invalid")
            _positive_int(body.get("total_characters"), "Execution-control packet total is invalid")
        else:
            # Schema 13 stores bounded material objects in each packet.  The
            # current producer emits one object packet (start/end/total are
            # retained as pagination metadata); serialized-fragment packets
            # remain readable for early archive producers.
            need(isinstance(body.get("material"), dict),
                 "invalid_archive", "Execution-control packet material is missing")
            _nonnegative_int(body.get("start", 0), "Execution-control packet start is invalid")
            end = body.get("end", 1)
            need(type(end) is int and end > body.get("start", 0),
                 "invalid_archive", "Execution-control packet range is invalid")
            _positive_int(body.get("total", end), "Execution-control packet total is invalid")
            _canonical(body["material"], "Execution-control packet material")
        _nonempty(body.get("material_digest"), "Execution-control packet material digest is missing")
        if body.get("proposal_digest") is not None:
            _nonempty(body.get("proposal_digest"), "Execution-control packet proposal digest is empty")
        packets[proposal].append(row)

    for ident, proposal in proposals.items():
        proposal_body = _body(proposal)
        material = _proposal_material(proposal_body)
        rows = sorted(packets[ident], key=lambda r: (r.get("ordinal"), r.get("id")))
        need(rows, "invalid_archive", "Execution-control packet history is incomplete")
        for ordinal, row in enumerate(rows):
            packet = _body(row)
            need(row.get("ordinal") == ordinal and packet.get("ordinal", ordinal) == ordinal,
                 "invalid_archive", "Execution-control packet order has a gap")
            if packet.get("proposal_digest") is not None:
                need(packet["proposal_digest"] == proposal.get("digest"),
                     "invalid_archive", "Execution-control packet proposal digest differs")
        manifest = material.get("packet_manifest", proposal_body.get("packet_manifest"))
        if manifest is not None:
            need(isinstance(manifest, list), "invalid_archive", "Execution-control packet manifest is malformed")
            need([{"id": r.get("id"), "digest": r.get("digest")} for r in rows] == manifest,
                 "invalid_archive", "Execution-control packet manifest differs")

        fragments = [(_body(row)).get("serialized_fragment") for row in rows]
        if all(fragment is not None for fragment in fragments):
            cursor = 0
            total = None
            data = []
            for row, fragment in zip(rows, fragments):
                packet = _body(row)
                need(packet.get("start") == cursor,
                     "invalid_archive", "Execution-control packet ranges are discontinuous")
                total = packet.get("total_characters") if total is None else total
                need(packet.get("total_characters") == total,
                     "invalid_archive", "Execution-control packet totals differ")
                need(packet.get("material_digest") == proposal_body.get("material_digest", material.get("material_digest")),
                     "invalid_archive", "Execution-control packet material differs")
                cursor = packet["end"]
                data.append(fragment)
            expected = proposal_body.get("material_digest", material.get("material_digest"))
            need(cursor == total and digest("".join(data).encode()) == expected,
                 "invalid_archive", "Execution-control packet material is truncated")
            parsed = parse_json("".join(data))
            need(isinstance(parsed, dict), "invalid_archive", "Execution-control packet material is malformed")
        else:
            need(all(fragment is None for fragment in fragments),
                 "invalid_archive", "Execution-control packet encoding differs")
            expected = proposal_body.get("material_digest", digest(material))
            for row in rows:
                packet = _body(row)
                need(packet.get("material_digest") == expected and digest(packet["material"]) == expected,
                     "invalid_archive", "Execution-control packet material digest differs")
                # The current packet is an exact immutable projection of the
                # proposal material.  Comparing canonical objects catches a
                # swapped packet whose digest was recomputed independently.
                need(packet["material"] == material,
                     "invalid_archive", "Execution-control packet material differs")

    event_rows = _rows(each, "execution_control_events")
    events = {}
    events_by_proposal = {}
    for row in event_rows:
        ident = row.get("id")
        proposal = row.get("proposal")
        need(isinstance(ident, str) and ident and ident not in events,
             "invalid_archive", "Duplicate execution-control event")
        need(row.get("project") == project and proposal in proposals,
             "invalid_archive", "Execution-control event refers to another project/proposal")
        need(row.get("kind") in EVENT_KINDS, "invalid_archive", "Unknown execution-control event")
        body = _body(row)
        need(digest(body) == row.get("digest"),
             "invalid_archive", "Execution-control event digest differs")
        _optional_equal(body, "proposal", proposal)
        _optional_equal(body, "project", project)
        _optional_equal(body, "kind", row["kind"])
        events[ident] = row
        events_by_proposal.setdefault(proposal, []).append(row)

    authorization_rows = _rows(each, "execution_control_authorizations")
    # Archives are validated by identity, not by the order in which a caller
    # happened to materialize rows.  Validate per-task control revisions in
    # their canonical order so a harmless row reorder cannot look like a
    # revision regression.
    authorization_rows.sort(key=lambda row: (
        row.get("task") or "",
        row.get("control_revision") if type(row.get("control_revision")) is int else -1,
        row.get("id") or "",
    ))
    authorizations = {}
    auth_by_proposal = {}
    control_revisions = {}
    for row in authorization_rows:
        ident = row.get("id")
        proposal_id = row.get("proposal")
        need(isinstance(ident, str) and ident and ident not in authorizations,
             "invalid_archive", "Duplicate execution-control authorization")
        proposal = proposals.get(proposal_id)
        need(proposal is not None and row.get("project") == project == proposal.get("project"),
             "invalid_archive", "Authorization refers to another project/proposal")
        need(proposal.get("status") == "applied", "invalid_archive", "Authorization has no applied proposal")
        need(row.get("task") == proposal.get("task") and
             row.get("task_revision") == proposal.get("task_revision"),
             "invalid_archive", "Authorization Task revision binding differs")
        _positive_int(row.get("control_revision"), "Authorization control revision is invalid")
        task = row.get("task")
        previous = control_revisions.get(task, 0)
        need(row["control_revision"] > previous, "invalid_archive", "Authorization control revision is not monotonic")
        control_revisions[task] = row["control_revision"]
        need(row.get("proposal_digest") == proposal.get("digest"),
             "invalid_archive", "Authorization proposal digest differs")
        body = _body(row)
        need(digest(body) == row.get("digest"),
             "invalid_archive", "Authorization body digest differs")
        _optional_equal(body, "proposal", proposal_id)
        _optional_equal(body, "task", task)
        _optional_equal(body, "task_revision", row.get("task_revision"))
        _optional_equal(body, "control_revision", row.get("control_revision"))
        _optional_equal(body, "requested_seconds", row.get("requested_seconds"))
        _optional_equal(body, "effective_seconds", row.get("effective_seconds"))
        _optional_equal(body, "assessment", row.get("assessment"))
        _nonempty(row.get("reviewer_run"), "Authorization reviewer run is missing")
        _nonempty(row.get("reviewer_receipt"), "Authorization reviewer receipt is missing")
        assessment = row.get("assessment")
        need(assessment is None or assessment in JUDGMENTS,
             "invalid_archive", "Authorization assessment is invalid")
        target = proposal_targets[proposal_id]
        observed = bool(target.get("implementer_run") and target.get("implementer_receipt"))
        if observed:
            need(row["reviewer_run"] != target.get("implementer_run"),
                 "invalid_archive", "Authorization reviewer is not independent")
        control_type = body.get("control_type")
        if control_type is None:
            control_type = "timeout" if row.get("requested_seconds") is not None else "recovery"
        need(control_type in {"timeout", "recovery"},
             "invalid_archive", "Authorization control type is invalid")
        if control_type == "timeout":
            _duration(row.get("requested_seconds"), "authorization requested_seconds")
            _duration(row.get("effective_seconds"), "authorization effective_seconds")
            need(observed, "invalid_archive", "Timeout authorization lacks an observed attempt")
            need(assessment in JUDGMENTS, "invalid_archive", "Timeout authorization assessment is missing")
        else:
            need(row.get("requested_seconds") is None and row.get("effective_seconds") is None,
                 "invalid_archive", "Recovery authorization carries a duration")
            if not observed:
                need(assessment is None, "invalid_archive", "Claim-only recovery has an assessment")
        if "control_type" in body:
            need(body["control_type"] == control_type, "invalid_archive", "Authorization control type differs")
        assessment_id = body.get("assessment_id")
        if assessment_id is not None:
            need(assessment_id in assessment_ids and assessment_ids[assessment_id].get("proposal") == proposal_id,
                 "invalid_archive", "Authorization assessment is dangling")
            need(assessment_ids[assessment_id].get("judgment") == assessment,
                 "invalid_archive", "Authorization assessment differs")
        elif assessment is not None:
            need(False, "invalid_archive", "Authorization assessment identity is missing")
        authorizations[ident] = row
        auth_by_proposal.setdefault(proposal_id, []).append(row)

    # Cross-link finalized assessments to their proposal and exact target.
    for key, row in assessments.items():
        proposal_id = row.get("proposal")
        proposal = proposals.get(proposal_id)
        need(proposal is not None, "invalid_archive", "Assessment refers to a missing proposal")
        need(row.get("proposal_digest") == proposal.get("digest"),
             "invalid_archive", "Assessment proposal digest differs")
        target = proposal_targets[proposal_id]
        need(target.get("attempt_epoch") == row.get("attempt_epoch"),
             "invalid_archive", "Assessment proposal epoch differs")
        if target.get("attempt_ordinal") is not None and row.get("attempt_ordinal") is not None:
            need(target.get("attempt_ordinal") == row.get("attempt_ordinal"),
                 "invalid_archive", "Assessment proposal ordinal differs")
        need(proposal.get("task") == row.get("task"),
             "invalid_archive", "Assessment proposal Task differs")

    # Status transitions are append-only.  Assessment-only applied decisions
    # legitimately have no authorization; timeout/recovery authorizations are
    # checked above.  Inconclusive reviews leave proposals pending and never
    # consume a finalized assessment slot or authorization.
    for ident, proposal in proposals.items():
        kinds = [row["kind"] for row in events_by_proposal.get(ident, [])]
        need(len(kinds) == len(set(kinds)),
             "invalid_archive", "Duplicate execution-control status transition")
        status = proposal["status"]
        if status == "applied":
            need("applied" in kinds, "invalid_archive", "Applied proposal lost applied event")
            result = proposal.get("result")
            if isinstance(result, str):
                result = _json(result, "Execution-control proposal result")
            need(result is None or isinstance(result, dict),
                 "invalid_archive", "Execution-control proposal result is malformed")
            if isinstance(result, dict):
                if result.get("authorization") is not None:
                    need(result["authorization"] in authorizations and
                         authorizations[result["authorization"]].get("proposal") == ident,
                         "invalid_archive", "Proposal result authorization is dangling")
                if result.get("assessment") is not None:
                    need(result["assessment"] in assessment_ids and
                         assessment_ids[result["assessment"]].get("proposal") == ident,
                         "invalid_archive", "Proposal result assessment is dangling")
                if result.get("event") is not None:
                    need(result["event"] in events and events[result["event"]].get("proposal") == ident and
                         events[result["event"]].get("kind") == "applied",
                         "invalid_archive", "Proposal result event is dangling")
            applied = next(row for row in events_by_proposal[ident] if row["kind"] == "applied")
            result = _body(applied).get("result")
            if isinstance(result, dict):
                auth_id = result.get("authorization")
                assessment_id = result.get("assessment")
                if auth_id is not None:
                    need(auth_id in authorizations and authorizations[auth_id].get("proposal") == ident,
                         "invalid_archive", "Applied event authorization is dangling")
                if assessment_id is not None:
                    need(assessment_id in assessment_ids and assessment_ids[assessment_id].get("proposal") == ident,
                         "invalid_archive", "Applied event assessment is dangling")
        elif status in {"withdrawn", "superseded"}:
            need(status in kinds, "invalid_archive", "Closed proposal lost its status event")
        else:
            need(not kinds, "invalid_archive", "Proposed execution-control status is inconsistent")

    # Legacy rows with no execution_attempts representation remain unknown.
    # A legacy assessment with an explicit ordinal is durable evidence for one
    # of those attempts; a NULL ordinal remains unknown and is never guessed.
    legacy_unknown = 0
    for task, row in tasks.items():
        known = set(ordinals.get(task, set()))
        known.update(value.get("attempt_ordinal") for (candidate, _), value in assessments.items()
                     if candidate == task and value.get("attempt_ordinal") is not None)
        legacy_unknown += max(0, row["attempts"] - len(known))

    return {
        "tasks": len(tasks),
        "execution_attempts": len(attempt_rows),
        "attempt_assessments": len(assessment_rows),
        "execution_control_proposals": len(proposal_rows),
        "execution_control_packets": len(packet_rows),
        "execution_control_events": len(event_rows),
        "execution_control_authorizations": len(authorization_rows),
        "legacy_unknown_attempts": legacy_unknown,
        "fresh_live_authorization": False,
    }


# Names used by earlier history validators and by callers that prefer the
# singular feature name.  Keeping both costs nothing and makes the archive
# contract easier to consume from standalone tooling.
validate_history = validate_execution_controls
validate_execution_control_history = validate_execution_controls
