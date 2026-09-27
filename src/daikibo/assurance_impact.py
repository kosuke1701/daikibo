"""Controller-derived Unit 2c-4 impact inventory.

The impact inventory is a read-only companion to Unit 2a's sealed stage
context.  A change keeps two different views of its affected set:

* ``baseline`` is the complete impact material captured with the change; and
* ``current`` is re-derived from the controller's registered graph at read
  time.

Review packets are checked as evidence for the inventory when a change names
them, but their leaf manifests are never used to manufacture the impact
population.  This distinction matters for packet pagination: a missing final
packet must remain an unresolved review defect even when the canonical graph
still enumerates all affected artifacts and Tasks.

This module intentionally does not add a public typed-reference kind or a
relation.  Existing ``artifact``, ``task_revision`` and ``change`` references
are used at the Unit 2a boundary.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable

from .assurance_relations import validate_typed_ref
from .common import Actor, Fault, canonical, digest, need, parse_json


IMPACT_FORMAT = "assurance.impact-inventory.v1"
IMPACT_CHANGE_FORMAT = "assurance.change-impact.v1"
IMPACT_DERIVATION_VERSION = "controller-impact.v1"
IMPACT_CATEGORY = "impacted_target"
IMPACT_CONSUMER = "assurance_denominators.derive_denominator"
MAX_IMPACT_TARGETS = 100_000

_SHA256 = set("0123456789abcdef")
_PACKET_TABLES = (
    "assurance_objects",
    "traceability_records",
    "breakdown_packets",
    "subplan_packets",
    "local_execution_packets",
    "execution_control_packets",
    "workstream_packets",
    "scope_return_packets",
)


class _SealedMapping(dict):
    """A private in-process seal for a controller-collected inventory."""

    __slots__ = ("_seal", "_token")

    def __init__(self, value: dict[str, Any], *, token: object) -> None:
        super().__init__(value)
        object.__setattr__(self, "_seal", canonical(dict(value)))
        object.__setattr__(self, "_token", token)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("sealed inventory metadata is immutable")
        object.__setattr__(self, name, value)

    def __deepcopy__(self, memo: dict[int, Any]) -> "_SealedMapping":
        copied = type(self).__new__(type(self))
        memo[id(self)] = copied
        dict.__init__(copied, copy.deepcopy(dict(self), memo))
        object.__setattr__(copied, "_seal", self._seal)
        object.__setattr__(copied, "_token", self._token)
        return copied


_INVENTORY_TOKEN = object()


def _copy_json(value: Any) -> Any:
    return parse_json(canonical(value))


def _invalid(message: str, details: Any = None) -> None:
    raise Fault("invalid_input", message, details)


def _integrity(message: str, details: Any = None) -> None:
    raise Fault("integrity_error", message, details)


def _string(value: Any, name: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value) or "\x00" in value:
        _invalid(f"{name} must be a {'possibly empty ' if empty else 'nonempty '}string")
    return value


def _sha(value: Any, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in _SHA256 for char in value):
        _invalid(f"{name} must be a lowercase SHA-256")
    return value


def _object(value: Any, required: Iterable[str], optional: Iterable[str] = (), *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid(f"{name} must be an object")
    required_set, optional_set = set(required), set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - required_set - optional_set)
    if missing:
        _invalid(f"{name} is missing fields", missing)
    if unknown:
        _invalid(f"{name} has unknown fields", unknown)
    return value


def _unresolved(code: str, *, reason: str, **details: Any) -> dict[str, Any]:
    value = {"code": code, "reason": reason}
    for key, item in details.items():
        if item is not None:
            value[key] = _copy_json(item)
    return value


def _body(row: dict[str, Any], *, name: str = "body") -> dict[str, Any]:
    try:
        value = parse_json(row["body"] if isinstance(row.get("body"), str) else row.get("body"))
    except (Fault, KeyError) as exc:
        raise Fault("integrity_error", f"{name} is not valid JSON", row.get("id")) from exc
    if not isinstance(value, dict):
        raise Fault("integrity_error", f"{name} is not an object", row.get("id"))
    return _copy_json(value)


def _ref(value: Any, project: str, expected: set[str], *, name: str) -> dict[str, Any]:
    try:
        validate_typed_ref(value, project=project, expected_kinds=expected)
    except Fault:
        raise
    return _copy_json(value)


def _change_ref(project: str, row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    body_digest = digest(body)
    if type(row.get("revision")) is not int or row["revision"] < 1:
        _integrity("Change revision is malformed", row.get("id"))
    ref = {
        "kind": "change", "project": project, "change": row["id"],
        "revision": row["revision"], "body_digest": body_digest,
        "pin": {"id": row["id"], "digest": body_digest},
    }
    return _ref(ref, project, {"change"}, name="change reference")


def _target_ref(control: Any, project: str, kind: str, ident: str,
                unresolved: list[dict[str, Any]], *, side: str,
                change: str) -> dict[str, Any] | None:
    if kind == "artifact":
        row = control.s.one("SELECT id,revision,digest,project FROM artifacts WHERE id=? AND project=?", (ident, project))
        if row is None:
            unresolved.append(_unresolved("impact_target_missing", reason="Impact artifact is absent from the controller project",
                                          change=change, side=side, target_kind=kind, target=ident))
            return None
        raw = {"kind": "artifact", "project": project, "artifact": ident,
               "revision": row["revision"], "body_digest": row["digest"]}
    else:
        row = control.s.one("SELECT id,revision,body,project FROM tasks WHERE id=? AND project=?", (ident, project))
        if row is None:
            unresolved.append(_unresolved("impact_target_missing", reason="Impact Task is absent from the controller project",
                                          change=change, side=side, target_kind=kind, target=ident))
            return None
        try:
            body = _body(row, name="impact Task body")
        except Fault as exc:
            unresolved.append(_unresolved("impact_target_integrity", reason=exc.code,
                                          change=change, side=side, target_kind=kind, target=ident))
            return None
        raw = {"kind": "task_revision", "project": project, "task": ident,
               "revision": row["revision"], "definition_digest": digest(body)}
    try:
        return _ref(raw, project, {raw["kind"]}, name="impact target reference")
    except Fault as exc:
        unresolved.append(_unresolved("impact_target_integrity", reason=exc.code,
                                      change=change, side=side, target_kind=kind, target=ident))
        return None


def _resolve_pinned_target(control: Any, ref: dict[str, Any], unresolved: list[dict[str, Any]], *,
                           side: str, change: str) -> None:
    """Resolve an impact pin through the shared assurance resolver.

    The impact material contains immutable artifact and Task revision pins.
    Comparing the pin's shape or digest with the saved change body is not
    enough: a fabricated revision can otherwise be mistaken for a legitimate
    historical baseline.  ``resolve_pinned`` deliberately permits retained
    history, while ``evaluate_current`` requires the current accepted/current
    identity for the re-derived side.
    """
    assurance = getattr(control, "assurance", None)
    resolver = getattr(assurance, "resolve_pinned", None)
    current_resolver = getattr(assurance, "evaluate_current", None)
    if not callable(resolver) or not callable(current_resolver):
        unresolved.append(_unresolved(
            "impact_target_resolution_unknown",
            reason="The shared typed impact resolver is unavailable",
            change=change, side=side, target=ref,
        ))
        return
    actor = Actor("unit2c4-impact", "owner")
    try:
        if side == "baseline":
            resolver(actor, ref)
        else:
            result = current_resolver(actor, ref)
            current = result.get("current") if isinstance(result, dict) else None
            state = current.get("state") if isinstance(current, dict) else None
            if state != "current":
                code = "stale_reference" if state == "stale" else "unresolved_reference"
                raise Fault(code, "Current impact pin did not resolve to the current target", ref)
    except Fault as exc:
        code = "impact_target_history_unknown" if side == "baseline" else "impact_target_current_unknown"
        unresolved.append(_unresolved(
            code,
            reason=exc.code,
            change=change, side=side, target=ref,
        ))


def _id_list(value: Any, name: str, unresolved: list[dict[str, Any]], *, change: str, side: str) -> list[str]:
    if not isinstance(value, list):
        unresolved.append(_unresolved("impact_enumeration_invalid", reason=f"{name} is not a list",
                                      change=change, side=side, field=name))
        return []
    if len(value) > MAX_IMPACT_TARGETS:
        unresolved.append(_unresolved("impact_enumeration_bounded", reason=f"{name} exceeds the bounded impact inventory",
                                      change=change, side=side, field=name, count=len(value)))
        return []
    if any(type(item) is not str or not item or "\x00" in item for item in value):
        unresolved.append(_unresolved("impact_target_identity_invalid", reason=f"{name} contains an invalid target ID",
                                      change=change, side=side, field=name))
        return []
    if len(set(value)) != len(value):
        unresolved.append(_unresolved("impact_target_duplicate", reason=f"{name} contains duplicate target IDs",
                                      change=change, side=side, field=name))
    return sorted(set(value))


def _snapshot(control: Any, project: str, impact: Any, unresolved: list[dict[str, Any]], *, side: str,
              change: str, require_refs: bool, baseline_refs: Any = None) -> dict[str, Any]:
    if type(impact) is not dict:
        unresolved.append(_unresolved("impact_material_missing", reason=f"{side} impact material is not an object",
                                      change=change, side=side))
        empty = {"artifacts": [], "tasks": [], "artifact_refs": [], "task_refs": [],
                 "reachable_sets_complete": False}
        return {**empty, "digest": digest(empty)}
    artifacts = _id_list(impact.get("artifacts"), "artifacts", unresolved, change=change, side=side)
    tasks = _id_list(impact.get("tasks"), "tasks", unresolved, change=change, side=side)
    if impact.get("reachable_sets_complete") is not True:
        unresolved.append(_unresolved("impact_enumeration_incomplete", reason="Impact material does not certify complete artifact/task sets",
                                      change=change, side=side))

    refs_by_kind: dict[str, list[dict[str, Any]]] = {"artifact": [], "task_revision": []}
    fields = (("artifact_refs", "artifact", "artifacts"), ("task_refs", "task_revision", "tasks"))
    for field, kind, ids_name in fields:
        raw_refs = impact.get(field)
        if not isinstance(raw_refs, list):
            if require_refs:
                unresolved.append(_unresolved("impact_target_identity_missing",
                                              reason=f"{side} impact material has no exact {field}",
                                              change=change, side=side, field=field))
            raw_refs = []
        seen = set()
        for raw in raw_refs:
            try:
                value = _ref(raw, project, {kind}, name=f"{side} {field} reference")
            except Fault as exc:
                unresolved.append(_unresolved("impact_target_identity_invalid", reason=exc.code,
                                              change=change, side=side, field=field))
                continue
            ident = value["artifact"] if kind == "artifact" else value["task"]
            if ident in seen:
                unresolved.append(_unresolved("impact_target_duplicate", reason=f"{side} target reference is duplicated",
                                              change=change, side=side, field=field, target=ident))
                continue
            seen.add(ident); refs_by_kind[kind].append(value)
        refs_by_kind[kind].sort(key=canonical)
        ref_ids = {value["artifact"] if kind == "artifact" else value["task"] for value in refs_by_kind[kind]}
        expected_ids = set(artifacts if ids_name == "artifacts" else tasks)
        if ref_ids != expected_ids:
            if require_refs or raw_refs:
                unresolved.append(_unresolved("impact_target_identity_mismatch",
                                              reason=f"{side} exact target refs do not match its complete ID set",
                                              change=change, side=side, field=field,
                                              expected=sorted(expected_ids), actual=sorted(ref_ids)))

    # Historical changes from before the rich snapshot may have only the
    # baseline_refs for their roots.  Keep those roots as exact history pins;
    # never silently substitute the current revision for a non-root target.
    if side == "baseline" and not refs_by_kind["artifact"]:
        if isinstance(baseline_refs, list):
            for raw in baseline_refs:
                try:
                    value = _ref(raw, project, {"artifact"}, name="baseline root reference")
                except Fault as exc:
                    unresolved.append(_unresolved("impact_target_identity_invalid", reason=exc.code,
                                                  change=change, side=side, field="baseline_refs"))
                    continue
                refs_by_kind["artifact"].append(value)
            refs_by_kind["artifact"].sort(key=canonical)
    # Every exact identity is resolved independently of the saved ID lists.
    # Baseline resolution accepts an immutable retained history record; the
    # current projection must resolve as current.  In particular, a
    # syntactically valid revision/digest pair which is absent from history is
    # unresolved rather than merely a semantic delta.
    for target in (*refs_by_kind["artifact"], *refs_by_kind["task_revision"]):
        _resolve_pinned_target(control, target, unresolved, side=side, change=change)
    snapshot = {
        "artifacts": artifacts, "tasks": tasks,
        "artifact_refs": refs_by_kind["artifact"], "task_refs": refs_by_kind["task_revision"],
        "reachable_sets_complete": impact.get("reachable_sets_complete") is True,
    }
    snapshot["digest"] = digest(snapshot)
    return snapshot


def _packet_manifest(body: dict[str, Any]) -> tuple[bool, Any, str | None]:
    """Find an explicitly declared packet set without inventing one."""
    impact = body.get("impact") if isinstance(body.get("impact"), dict) else {}
    for owner, key in ((impact, "review_packets"), (impact, "review_packet_manifest"),
                       (body, "review_packets"), (body, "review_packet_manifest"),
                       (body, "packet_manifest")):
        if key in owner:
            return True, owner[key], key
    return False, None, None


def _packet_row(control: Any, project: str, ident: str) -> dict[str, Any] | None:
    foreign = None
    for table in _PACKET_TABLES:
        try:
            row = control.s.one(f"SELECT * FROM {table} WHERE id=?", (ident,))
        except Exception:
            # A schema migration may not have installed an optional packet
            # table.  The declared packet remains unresolved below.
            row = None
        if row is not None:
            if row.get("project") not in (None, project):
                foreign = foreign or {"_foreign": True, "project": row.get("project"), "table": table}
                continue
            row["_table"] = table
            return row
    return foreign


def _coverage_targets(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    values = [*_copy_json(snapshot.get("artifact_refs", [])),
              *_copy_json(snapshot.get("task_refs", []))]
    return sorted(values, key=canonical)


def _unique_refs(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_identity: dict[bytes, dict[str, Any]] = {}
    for value in values:
        by_identity[canonical(value)] = value
    return [by_identity[key] for key in sorted(by_identity)]


def _material_target_refs(material: dict[str, Any], project: str) -> list[dict[str, Any]]:
    """Extract typed targets from a canonical breakdown material document."""
    if not isinstance(material, dict):
        raise Fault("integrity_error", "Breakdown material is not an object")
    result: list[dict[str, Any]] = []
    artifacts = material.get("artifacts")
    tasks = material.get("tasks")
    if not isinstance(artifacts, list) or not isinstance(tasks, list):
        # The structure packet intentionally has no target lists.  It is
        # validated by the caller, but contributes no coverage identities.
        if "units" in material and "current_scope" in material and "unit" not in material:
            return result
        raise Fault("integrity_error", "Breakdown material target collections are missing")
    for item in artifacts:
        if not isinstance(item, dict):
            raise Fault("integrity_error", "Breakdown artifact material is malformed")
        ident, revision, body_digest = item.get("id"), item.get("revision"), item.get("digest")
        if digest(item.get("body")) != body_digest:
            raise Fault("integrity_error", "Breakdown artifact material digest differs", ident)
        result.append(_ref({"kind": "artifact", "project": project, "artifact": ident,
                            "revision": revision, "body_digest": body_digest},
                           project, {"artifact"}, name="breakdown artifact coverage"))
    for item in tasks:
        if not isinstance(item, dict):
            raise Fault("integrity_error", "Breakdown Task material is malformed")
        ident, revision, task_body = item.get("id"), item.get("revision"), item.get("body")
        result.append(_ref({"kind": "task_revision", "project": project, "task": ident,
                            "revision": revision, "definition_digest": digest(task_body)},
                           project, {"task_revision"}, name="breakdown Task coverage"))
    return _unique_refs(result)


def _breakdown_packet_targets(control: Any, project: str, row: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Resolve one real breakdown packet and its complete canonical packet set.

    A row checksum and arbitrary leaf labels prove only storage integrity.  The
    Breakdowns packet accessor re-derives the material from the current
    breakdown, and the group walk below proves that every material binding and
    fragment is present before any target is counted as covered.
    """
    if row.get("_table") != "breakdown_packets":
        raise Fault("unresolved_reference", "Impact packet has no supported canonical accessor")
    packet_body = _body(row, name="breakdown packet")
    if packet_body.get("format") != "daikibo.breakdown-review.v1":
        raise Fault("unresolved_reference", "Impact packet format is not a canonical breakdown packet")
    actor = Actor("unit2c4-impact", "owner")
    checked = control.breakdowns.packet(actor, row["id"])
    breakdown_id = checked.get("breakdown")
    need(isinstance(breakdown_id, str) and breakdown_id, "unresolved_reference", "Breakdown packet has no canonical group")
    breakdown = control.breakdowns._row(actor, breakdown_id)
    breakdown_body = breakdown.get("body")
    bindings = breakdown_body.get("material_bindings") if isinstance(breakdown_body, dict) else None
    if not isinstance(bindings, dict) or not bindings:
        raise Fault("integrity_error", "Breakdown material bindings are missing")
    members = control.s.all(
        "SELECT p.id,p.digest,p.body,m.ordinal FROM breakdown_members m "
        "JOIN breakdown_packets p ON p.id=m.packet WHERE m.breakdown=? ORDER BY m.ordinal",
        (breakdown_id,),
    )
    if not members:
        raise Fault("unresolved_reference", "Breakdown packet group has no members")
    member_ids = {member.get("id") for member in members}
    if any(type(member.get("ordinal")) is not int or member["ordinal"] != index
           for index, member in enumerate(members)):
        raise Fault("integrity_error", "Breakdown packet group ordinals are not contiguous")
    by_unit: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        member = dict(member)
        member["_table"] = "breakdown_packets"
        resolved = control.breakdowns.packet(actor, member["id"])
        need(resolved.get("breakdown") == breakdown_id, "stale_reference",
             "Breakdown packet resolved to another canonical group", member["id"])
        body = _body(member, name="breakdown packet")
        unit = body.get("unit")
        if type(unit) is not str or not unit:
            raise Fault("integrity_error", "Breakdown packet unit is malformed")
        by_unit.setdefault(unit, []).append(body)
    if set(by_unit) != set(bindings):
        raise Fault("unresolved_reference", "Breakdown packet group does not cover every material binding")

    targets: list[dict[str, Any]] = []
    for unit, material_digest in bindings.items():
        parts = sorted(by_unit[unit], key=lambda item: (item.get("start", -1), item.get("end", -1)))
        cursor = 0
        fragments: list[str] = []
        for part in parts:
            start, end = part.get("start"), part.get("end")
            total = part.get("total_characters")
            if (type(start) is not int or type(end) is not int or type(total) is not int
                    or start != cursor or end < start or end > total
                    or part.get("material_digest") != material_digest
                    or type(part.get("serialized_fragment")) is not str):
                raise Fault("integrity_error", "Breakdown packet fragment does not match its material binding", unit)
            expected_marker = "PART-" + digest({
                "program": part.get("program"), "unit": unit,
                "material": material_digest, "start": start, "end": end,
            })
            if part.get("required_coverage") != [expected_marker]:
                raise Fault("integrity_error", "Breakdown packet required coverage is not canonical", unit)
            if "leaf_manifest" in part and part.get("leaf_manifest") != [expected_marker]:
                raise Fault("integrity_error", "Breakdown packet leaf manifest is not canonical", unit)
            cursor = end
            fragments.append(part["serialized_fragment"])
        if not parts or cursor != parts[-1].get("total_characters"):
            raise Fault("unresolved_reference", "Breakdown packet group has incomplete material fragments", unit)
        serialized = "".join(fragments)
        material = parse_json(serialized)
        if digest(material) != material_digest:
            raise Fault("integrity_error", "Breakdown packet material digest differs", unit)
        targets.extend(_material_target_refs(material, project))
    return _unique_refs(targets), member_ids


def _packet_inventory(control: Any, project: str, body: dict[str, Any], unresolved: list[dict[str, Any]], *,
                      change: str, baseline: dict[str, Any]) -> dict[str, Any]:
    declared, raw, field = _packet_manifest(body)
    if not declared:
        return {"declared": False, "status": "not_declared", "expected": [], "present": [],
                "missing": [], "unresolved": [], "rows_complete": None,
                "coverage": {"status": "not_declared", "expected": [], "actual": [],
                              "missing": [], "extra": [], "unknown": []},
                "complete": None, "digest": digest({"declared": False})}
    packet_errors: list[dict[str, Any]] = []
    coverage_errors: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        packet_errors.append(_unresolved("impact_packet_manifest_invalid", reason="Review packet manifest is not a list", change=change, field=field))
    else:
        for index, item in enumerate(raw):
            if type(item) is str:
                packet_errors.append(_unresolved("impact_packet_digest_missing", reason="Review packet manifest lacks a pinned digest", change=change, packet=item, index=index))
                continue
            if type(item) is not dict or set(item) - {"id", "digest", "ordinal"} or set(item) < {"id", "digest"}:
                packet_errors.append(_unresolved("impact_packet_manifest_invalid", reason="Review packet manifest entry is malformed", change=change, index=index))
                continue
            if type(item["id"]) is not str or not item["id"] or type(item["digest"]) is not str or len(item["digest"]) != 64:
                packet_errors.append(_unresolved("impact_packet_manifest_invalid", reason="Review packet identity is malformed", change=change, index=index))
                continue
            if "ordinal" in item and (type(item["ordinal"]) is not int or item["ordinal"] < 0):
                packet_errors.append(_unresolved("impact_packet_manifest_invalid", reason="Review packet ordinal is malformed", change=change, index=index))
                continue
            expected.append({key: item[key] for key in ("id", "digest", "ordinal") if key in item})
    if len({item["id"] for item in expected}) != len(expected):
        packet_errors.append(_unresolved("impact_packet_duplicate", reason="Review packet manifest contains duplicate identities", change=change))
    expected_count = body.get("packet_count", body.get("impact", {}).get("packet_count") if isinstance(body.get("impact"), dict) else None)
    if expected_count is not None and (type(expected_count) is not int or expected_count != len(expected)):
        packet_errors.append(_unresolved("impact_packet_count_mismatch", reason="Declared review packet count differs from the manifest", change=change, expected=expected_count, actual=len(expected)))
    present: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    actual_targets: list[dict[str, Any]] = []
    packet_groups: set[str] = set()
    for item in expected:
        row = _packet_row(control, project, item["id"])
        if row is None or row.get("_foreign"):
            missing.append(item)
            packet_errors.append(_unresolved("impact_review_packet_missing", reason="Declared review packet is absent", change=change, packet=item["id"]))
            continue
        actual_digest = row.get("digest")
        if actual_digest != item["digest"]:
            packet_errors.append(_unresolved("impact_review_packet_stale", reason="Declared review packet digest differs", change=change, packet=item["id"]))
            missing.append(item)
            continue
        try:
            packet_body = _body(row, name="review packet")
            if digest(packet_body) != actual_digest:
                packet_errors.append(_unresolved("impact_review_packet_integrity", reason="Review packet body digest differs", change=change, packet=item["id"]))
                missing.append(item)
                continue
            # Validate the packet's own bounded manifest where present.  Its
            # leaves are deliberately retained only as inspection material.
            malformed_manifest = False
            for manifest_field in ("leaf_manifest", "required_coverage"):
                if manifest_field not in packet_body:
                    continue
                manifest = packet_body[manifest_field]
                try:
                    markers = {canonical(value) for value in manifest} if isinstance(manifest, list) else set()
                except Fault:
                    markers = set()
                if (not isinstance(manifest, list) or len(manifest) > MAX_IMPACT_TARGETS
                        or len(markers) != len(manifest)):
                    malformed_manifest = True
                    break
            if malformed_manifest:
                packet_errors.append(_unresolved("impact_review_packet_integrity", reason="Review packet leaf/coverage manifest is malformed", change=change, packet=item["id"]))
                missing.append(item)
                continue
            try:
                targets, group = _breakdown_packet_targets(control, project, row)
                actual_targets.extend(targets)
                packet_groups.add(digest(sorted(group)))
            except Fault as exc:
                coverage_errors.append(_unresolved(
                    "impact_packet_accessor_unknown", reason=exc.code,
                    change=change, packet=item["id"], table=row.get("_table"),
                    details=exc.details,
                ))
        except Fault as exc:
            packet_errors.append(_unresolved("impact_review_packet_integrity", reason=exc.code, change=change, packet=item["id"]))
            missing.append(item)
            continue
        present.append(item)
    if expected and [item.get("ordinal") for item in expected if "ordinal" in item] and [item["ordinal"] for item in expected if "ordinal" in item] != list(range(len(expected))):
        packet_errors.append(_unresolved("impact_packet_ordinal_gap", reason="Review packet manifest ordinals are not contiguous", change=change))
    if not expected:
        packet_errors.append(_unresolved("impact_review_packet_missing", reason="An explicitly declared review packet set is empty", change=change))
    declared_ids = {item["id"] for item in expected}
    # A canonical breakdown accessor returns the complete member group.  If a
    # change names only one page of that group, the page itself is valid but
    # the declared review set is incomplete.
    if packet_groups:
        # ``packet_groups`` is a digest set to keep the result bounded.  The
        # membership check is repeated from each resolved row below so the
        # exact group IDs remain in the diagnostic details.
        for item in present:
            row = _packet_row(control, project, item["id"])
            if row is None or row.get("_foreign"):
                continue
            try:
                _, group = _breakdown_packet_targets(control, project, row)
            except Fault:
                continue
            undeclared = sorted(group - declared_ids)
            if undeclared:
                coverage_errors.append(_unresolved(
                    "impact_packet_manifest_incomplete",
                    reason="Canonical packet group contains undeclared members",
                    change=change, packet=item["id"], undeclared=undeclared,
                ))
    expected_targets = _coverage_targets(baseline)
    actual_targets = _unique_refs(actual_targets)
    if baseline.get("reachable_sets_complete") is not True or not expected_targets:
        coverage_errors.append(_unresolved(
            "impact_packet_coverage_unknown",
            reason="The baseline impact population is not a complete typed target set",
            change=change,
        ))
    expected_by_key = {canonical(value): value for value in expected_targets}
    actual_by_key = {canonical(value): value for value in actual_targets}
    missing_targets = [expected_by_key[key] for key in sorted(set(expected_by_key) - set(actual_by_key))]
    extra_targets = [actual_by_key[key] for key in sorted(set(actual_by_key) - set(expected_by_key))]
    if missing_targets:
        coverage_errors.append(_unresolved(
            "impact_packet_coverage_missing",
            reason="Canonical packet coverage omits baseline impact targets",
            change=change, missing=missing_targets,
        ))
    if extra_targets:
        coverage_errors.append(_unresolved(
            "impact_packet_coverage_extra",
            reason="Canonical packet coverage contains targets outside the baseline impact population",
            change=change, extra=extra_targets,
        ))
    rows_complete = bool(expected) and not packet_errors and len(present) == len(expected)
    coverage_status = "complete" if (rows_complete and not coverage_errors and expected_targets) else "unknown"
    coverage = {"status": coverage_status, "expected": expected_targets,
                "actual": actual_targets, "missing": missing_targets,
                "extra": extra_targets, "unknown": coverage_errors}
    complete = rows_complete and coverage_status == "complete"
    all_errors = [*packet_errors, *coverage_errors]
    unresolved.extend(all_errors)
    return {"declared": True, "status": "complete" if complete else "unknown", "expected": expected,
            "present": present, "missing": missing, "unresolved": all_errors,
            "rows_complete": rows_complete, "coverage": coverage,
            "complete": complete, "digest": digest({
                "declared": True, "expected": expected, "present": present,
                "missing": missing, "rows_complete": rows_complete,
                "coverage": coverage,
            })}


def _authority_check(control: Any, project: str, body: dict[str, Any], unresolved: list[dict[str, Any]], *, change: str) -> None:
    evidence = body.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(type(item) is not str or not item for item in evidence):
        unresolved.append(_unresolved("impact_authority_missing", reason="Change has no complete evidence authority list", change=change))
    else:
        for ident in evidence:
            exists = control.s.one("SELECT id FROM sources WHERE id=? AND project=?", (ident, project))
            exists = exists or control.s.one("SELECT id FROM receipts WHERE id=? AND project=?", (ident, project))
            exists = exists or control.s.one("SELECT id FROM artifacts WHERE id=? AND project=? AND kind IN ('finding','risk','unknown')", (ident, project))
            if exists is None:
                unresolved.append(_unresolved("impact_authority_missing", reason="Change evidence endpoint is absent", change=change, evidence=ident))
    authority_refs = body.get("authority_refs")
    if authority_refs is not None:
        if not isinstance(authority_refs, list):
            unresolved.append(_unresolved("impact_authority_invalid", reason="Change authority_refs is not a list", change=change))
        else:
            for raw in authority_refs:
                try:
                    _ref(raw, project, {"source", "receipt", "artifact", "change", "proposal", "assurance_object", "material"}, name="change authority reference")
                except Fault as exc:
                    unresolved.append(_unresolved("impact_authority_invalid", reason=exc.code, change=change))


def _target_map(snapshot: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    result = {}
    field = "artifact_refs" if kind == "artifact" else "task_refs"
    id_field = "artifact" if kind == "artifact" else "task"
    for ref in snapshot.get(field, []):
        result[ref[id_field]] = ref
    return result


def _delta(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    result = {"added": [], "removed": [], "changed": []}
    for kind, field in (("artifact", "artifacts"), ("task_revision", "tasks")):
        before = set(baseline.get(field, [])); after = set(current.get(field, []))
        result["added"].extend({"kind": kind, "id": ident} for ident in sorted(after - before))
        result["removed"].extend({"kind": kind, "id": ident} for ident in sorted(before - after))
        before_refs, after_refs = _target_map(baseline, kind), _target_map(current, kind)
        for ident in sorted(before & after):
            if before_refs.get(ident) != after_refs.get(ident):
                result["changed"].append({"kind": kind, "id": ident,
                                          "baseline": before_refs.get(ident), "current": after_refs.get(ident)})
    for key in ("added", "removed", "changed"):
        result[key].sort(key=canonical)
    result["digest"] = digest(result)
    return result


def _seal(value: dict[str, Any]) -> _SealedMapping:
    return _SealedMapping(_copy_json(value), token=_INVENTORY_TOKEN)


def _verify_seal(value: Any, *, require_seal: bool) -> dict[str, Any]:
    if require_seal:
        if not isinstance(value, _SealedMapping) or value._token is not _INVENTORY_TOKEN:
            _invalid("impact inventory must come from the controller collector")
        if value._seal != canonical(dict(value)):
            raise Fault("denominator_input_mismatch", "Impact inventory was modified after controller enumeration")
    if not isinstance(value, dict):
        _invalid("impact inventory must be an object")
    return value


def collect_impact_inventory(control: Any, actor: Any, *, project: str, program: str) -> dict[str, Any]:
    """Read every stored project change and derive its current impact set.

    The caller normally invokes this inside Unit 2a's transaction.  The
    helper is read-only and writes no receipts, packets, or workflow rows.
    """
    control.k.project(actor, project)
    changes = []
    unresolved: list[dict[str, Any]] = []
    for row in control.s.all("SELECT * FROM changes WHERE project=? ORDER BY id", (project,)):
        change_unresolved: list[dict[str, Any]] = []
        try:
            body = _body(row, name="change body")
        except Fault as exc:
            body = {}
            change_unresolved.append(_unresolved("impact_change_integrity", reason=exc.code, change=row.get("id")))
        change_id = row.get("id")
        if type(change_id) is not str or not change_id:
            change_unresolved.append(_unresolved("impact_change_integrity", reason="Change ID is malformed"))
            continue
        try:
            change_ref = _change_ref(project, row, body)
        except Fault as exc:
            # A typed change pin is still the only safe fallback source for
            # retained target obligations.  A malformed row remains unknown.
            change_ref = {"kind": "change", "project": project, "change": change_id,
                          "revision": row.get("revision", 1), "body_digest": digest(body),
                          "pin": {"id": change_id, "digest": digest(body)}}
            change_unresolved.append(_unresolved("impact_change_integrity", reason=exc.code, change=change_id))
        if body.get("program") != program:
            change_unresolved.append(_unresolved("impact_program_mismatch", reason="Change material is not bound to the selected program", change=change_id, expected=program, actual=body.get("program")))
        if body.get("revision") is not None and body.get("revision") != row.get("revision"):
            change_unresolved.append(_unresolved("impact_revision_mismatch", reason="Change body revision differs from its controller revision", change=change_id))
        _authority_check(control, project, body, change_unresolved, change=change_id)
        affected = body.get("affected")
        if not isinstance(affected, list) or not affected or any(type(item) is not str or not item for item in affected) or len(set(affected)) != len(affected):
            change_unresolved.append(_unresolved("impact_roots_invalid", reason="Change affected roots are not a unique nonempty list", change=change_id))
            affected = []
        baseline_material = body.get("impact")
        baseline = _snapshot(control, project, baseline_material, change_unresolved, side="baseline", change=change_id,
                             require_refs=True, baseline_refs=body.get("baseline_refs"))
        current = {"artifacts": [], "tasks": [], "artifact_refs": [], "task_refs": [],
                   "reachable_sets_complete": False}
        current["digest"] = digest(current)
        if affected:
            try:
                current_raw = control.k.impact(Actor("unit2c4-impact", "owner"), project, affected)
                current = _snapshot(control, project, current_raw, change_unresolved, side="current", change=change_id, require_refs=True)
            except Fault as exc:
                change_unresolved.append(_unresolved("impact_current_unknown", reason=exc.code,
                                                     change=change_id, roots=affected))
        packet_check = _packet_inventory(control, project, body, change_unresolved, change=change_id,
                                        baseline=baseline)
        delta = _delta(baseline, current)
        status = "unknown" if change_unresolved else "stale" if delta["added"] or delta["removed"] or delta["changed"] else "current"
        change_value = {
            "format": IMPACT_CHANGE_FORMAT, "project": project, "program": program,
            "change_ref": change_ref, "change_revision": row.get("revision"),
            "change_body_digest": digest(body), "affected": sorted(affected),
            "baseline": baseline, "current": current, "delta": delta,
            "review_packets": packet_check, "status": status,
            "unresolved": sorted({canonical(item): item for item in change_unresolved}.values(), key=canonical),
        }
        change_value["digest"] = digest(change_value)
        changes.append(change_value)
        unresolved.extend(change_unresolved)
    changes.sort(key=lambda item: item["change_ref"]["change"])
    body = {
        "format": IMPACT_FORMAT, "project": project, "program": program,
        "derivation_version": IMPACT_DERIVATION_VERSION, "changes": changes,
        "unresolved": sorted({canonical(item): item for item in unresolved}.values(), key=canonical),
        "capabilities": {"supported": True, "category": IMPACT_CATEGORY,
                         "consumer": IMPACT_CONSUMER, "baseline_complete": True,
                         "current_rederived": True, "packet_sets_inspected": True,
                         "packet_leaves_generate_denominator": False,
                         "telemetry_excluded": True},
    }
    body["digest"] = digest(body)
    return _seal(body)


def validate_impact_inventory(inventory: Any, project: str, program: str, *, require_seal: bool = True) -> dict[str, Any]:
    """Validate an inventory handed to the pure Unit 2a derivation."""
    inventory = _verify_seal(inventory, require_seal=require_seal)
    _object(inventory, {"format", "project", "program", "derivation_version", "changes", "unresolved", "capabilities", "digest"}, name="impact inventory")
    if inventory["format"] != IMPACT_FORMAT or inventory["derivation_version"] != IMPACT_DERIVATION_VERSION:
        _invalid("impact inventory format or derivation version differs")
    _string(inventory["project"], "impact.project"); _string(inventory["program"], "impact.program")
    if inventory["project"] != project:
        raise Fault("cross_project", "Impact inventory belongs to another project")
    if inventory["program"] != program:
        raise Fault("stale_reference", "Impact inventory belongs to another program")
    if not isinstance(inventory["changes"], list) or not isinstance(inventory["unresolved"], list) or not isinstance(inventory["capabilities"], dict):
        _integrity("Impact inventory collection fields are malformed")
    if inventory["capabilities"].get("consumer") != IMPACT_CONSUMER or inventory["capabilities"].get("category") != IMPACT_CATEGORY:
        _integrity("Impact inventory consumer handoff differs")
    seen = set()
    for change in inventory["changes"]:
        _object(change, {"format", "project", "program", "change_ref", "change_revision", "change_body_digest", "affected", "baseline", "current", "delta", "review_packets", "status", "unresolved", "digest"}, name="change impact")
        if change["format"] != IMPACT_CHANGE_FORMAT or change["project"] != project or change["program"] != program:
            _invalid("change impact identity differs")
        ref = _ref(change["change_ref"], project, {"change"}, name="change impact reference")
        if ref["change"] in seen:
            _integrity("Duplicate change impact identity", ref["change"])
        seen.add(ref["change"])
        if change["change_revision"] != ref["revision"] or change["change_body_digest"] != ref["body_digest"]:
            _integrity("Change impact pin differs", ref["change"])
        _sha(change["change_body_digest"], "change_body_digest")
        calculated_change = dict(change)
        calculated_change.pop("digest")
        if digest(calculated_change) != change["digest"]:
            _integrity("Change impact digest differs", ref["change"])
        if not isinstance(change["affected"], list) or change["affected"] != sorted(set(change["affected"])):
            _integrity("Change impact affected roots are not canonical", ref["change"])
        if not isinstance(change["unresolved"], list):
            _integrity("Change impact unresolved material is malformed", ref["change"])
        unresolved_codes = {item.get("code") for item in change["unresolved"] if isinstance(item, dict)}
        for side in ("baseline", "current"):
            snapshot = change[side]
            _object(snapshot, {"artifacts", "tasks", "artifact_refs", "task_refs", "reachable_sets_complete", "digest"}, name=f"{side} impact snapshot")
            if not isinstance(snapshot["artifacts"], list) or not isinstance(snapshot["tasks"], list) or not isinstance(snapshot["artifact_refs"], list) or not isinstance(snapshot["task_refs"], list):
                _integrity("Impact snapshot collections are malformed", ref["change"])
            if type(snapshot["reachable_sets_complete"]) is not bool:
                _integrity("Impact snapshot completeness is malformed", ref["change"])
            for field in ("artifacts", "tasks"):
                values = snapshot[field]
                if any(type(item) is not str or not item for item in values) or values != sorted(set(values)):
                    _integrity("Impact snapshot target IDs are not canonical", ref["change"])
            for target in snapshot["artifact_refs"]:
                _ref(target, project, {"artifact"}, name="impact artifact target")
            for target in snapshot["task_refs"]:
                _ref(target, project, {"task_revision"}, name="impact Task target")
            artifact_ids = {target["artifact"] for target in snapshot["artifact_refs"]}
            task_ids = {target["task"] for target in snapshot["task_refs"]}
            exact = (artifact_ids == set(snapshot["artifacts"]) and
                     task_ids == set(snapshot["tasks"]))
            if not exact and change["status"] != "unknown":
                _integrity("Impact snapshot target identities are incomplete", ref["change"])
            if not exact and change["status"] == "unknown" and not unresolved_codes.intersection({
                    "impact_target_identity_missing", "impact_target_identity_mismatch",
                    "impact_target_identity_invalid", "impact_enumeration_invalid",
                    "impact_enumeration_incomplete", "impact_target_duplicate",
                    "impact_target_missing", "impact_target_integrity", "impact_current_unknown"}):
                _integrity("Unknown impact snapshot lacks an identity resolution reason", ref["change"])
            calculated = dict(snapshot); calculated.pop("digest")
            if digest(calculated) != snapshot["digest"]:
                _integrity("Impact snapshot digest differs", ref["change"])
        _object(change["delta"], {"added", "removed", "changed", "digest"}, name="impact delta")
        if digest({key: change["delta"][key] for key in ("added", "removed", "changed")}) != change["delta"]["digest"]:
            _integrity("Impact delta digest differs", ref["change"])
        if _delta(change["baseline"], change["current"]) != change["delta"]:
            _integrity("Impact delta does not match baseline/current snapshots", ref["change"])
        _object(change["review_packets"], {"declared", "status", "expected", "present", "missing",
                                            "unresolved", "rows_complete", "coverage", "complete", "digest"},
                name="impact packet check")
        if change["review_packets"]["declared"] is False:
            calculated_packets = {"declared": False}
        else:
            coverage = change["review_packets"]["coverage"]
            _object(coverage, {"status", "expected", "actual", "missing", "extra", "unknown"},
                    name="impact packet coverage")
            if coverage["status"] not in {"complete", "unknown", "not_declared"}:
                _invalid("Impact packet coverage status is invalid")
            if not all(isinstance(coverage[field], list)
                       for field in ("expected", "actual", "missing", "extra", "unknown")):
                _integrity("Impact packet coverage collections are malformed", ref["change"])
            if type(change["review_packets"]["rows_complete"]) is not bool:
                _integrity("Impact packet row completeness is malformed", ref["change"])
            for field in ("expected", "actual", "missing", "extra"):
                values = coverage[field]
                for target in values:
                    if not isinstance(target, dict):
                        _integrity("Impact packet coverage target is malformed", ref["change"])
                    kind = target.get("kind")
                    if kind not in {"artifact", "task_revision"}:
                        _integrity("Impact packet coverage target kind is unsupported", ref["change"])
                    _ref(target, project, {kind}, name="impact packet coverage target")
                canonical_values = sorted(values, key=canonical)
                if values != canonical_values or len({canonical(value) for value in values}) != len(values):
                    _integrity("Impact packet coverage targets are not canonical", ref["change"])
            if any(not isinstance(item, dict) for item in coverage["unknown"]):
                _integrity("Impact packet coverage diagnostics are malformed", ref["change"])
            calculated_packets = {key: change["review_packets"][key]
                                  for key in ("declared", "expected", "present", "missing", "rows_complete", "coverage")}
        if digest(calculated_packets) != change["review_packets"]["digest"]:
            _integrity("Impact packet check digest differs", ref["change"])
        if change["review_packets"]["declared"] is False:
            if (change["review_packets"]["status"] != "not_declared"
                    or change["review_packets"]["complete"] is not None
                    or change["review_packets"]["rows_complete"] is not None
                    or change["review_packets"]["coverage"]["status"] != "not_declared"):
                _integrity("Undeclared impact packet state is inconsistent", ref["change"])
        elif change["review_packets"]["status"] not in {"complete", "unknown"}:
            _integrity("Declared impact packet state is invalid", ref["change"])
        elif change["review_packets"]["complete"] is True and (
                change["review_packets"]["rows_complete"] is not True
                or change["review_packets"]["coverage"]["status"] != "complete"):
            _integrity("Impact packet strong completeness lacks canonical coverage", ref["change"])
        elif change["review_packets"]["coverage"]["status"] == "complete" and (
                change["review_packets"]["complete"] is not True
                or change["review_packets"]["coverage"]["missing"]
                or change["review_packets"]["coverage"]["extra"]
                or change["review_packets"]["coverage"]["unknown"]
                or change["review_packets"]["coverage"]["expected"]
                   != change["review_packets"]["coverage"]["actual"]):
            _integrity("Impact packet coverage claims complete without exact target equality", ref["change"])
        if change["status"] not in {"current", "stale", "unknown"}:
            _invalid("Impact change status is invalid")
    calculated = dict(inventory); calculated.pop("digest")
    if digest(calculated) != inventory["digest"]:
        _integrity("Impact inventory digest differs")
    return _copy_json(inventory)


def impact_input_refs(inventory: Any, project: str) -> list[dict[str, Any]]:
    """Return all typed semantic refs consumed by Unit 2a's input set."""
    supplied_program = inventory.get("program") if isinstance(inventory, dict) else None
    inventory = validate_impact_inventory(inventory, project, supplied_program, require_seal=False)
    values: list[dict[str, Any]] = []
    for change in inventory["changes"]:
        values.append(change["change_ref"])
        for side in ("baseline", "current"):
            values.extend(change[side]["artifact_refs"])
            values.extend(change[side]["task_refs"])
    unique = {canonical(value): value for value in values}
    return [unique[key] for key in sorted(unique)]


def impact_obligations(inventory: Any, project: str) -> list[dict[str, Any]]:
    """Create Unit 2a obligations from the union of baseline/current targets.

    Each target is represented once in the denominator.  The value digest
    contains both pinned views, so a replan or revision changes identity while
    the inventory still preserves the historical and current rows separately.
    """
    supplied_program = inventory.get("program") if isinstance(inventory, dict) else None
    inventory = validate_impact_inventory(inventory, project, supplied_program, require_seal=False)
    result: list[dict[str, Any]] = []
    for change in inventory["changes"]:
        baseline, current = change["baseline"], change["current"]
        for kind, field, ref_field, id_field in (("artifact", "artifacts", "artifact_refs", "artifact"),
                                                  ("task_revision", "tasks", "task_refs", "task")):
            before = {ref[id_field]: ref for ref in baseline[ref_field]}
            after = {ref[id_field]: ref for ref in current[ref_field]}
            ids = set(baseline[field]) | set(current[field]) | set(before) | set(after)
            for ident in sorted(ids):
                baseline_ref = before.get(ident)
                current_ref = after.get(ident)
                source_ref = current_ref or baseline_ref or change["change_ref"]
                identity = {"baseline": baseline_ref, "current": current_ref,
                            "target_kind": kind, "target": ident}
                pointer = f"/changes/{change['change_ref']['change']}/impact/{kind}/{ident}"
                value_digest = digest(identity)
                contributor = []
                task_ref = current_ref or baseline_ref
                if kind == "task_revision" and task_ref is not None:
                    contributor = [{"task_ref": task_ref, "assignment_refs": [{
                        "kind": "change_impact", "change": change["change_ref"]["change"],
                        "change_digest": change["change_body_digest"], "target": ident,
                    }]}]
                result.append({"id": "obligation:" + digest({"category": IMPACT_CATEGORY,
                                                               "source_ref": source_ref,
                                                               "pointer": pointer,
                                                               "value_digest": value_digest}),
                               "category": IMPACT_CATEGORY, "source_ref": source_ref,
                               "pointer": pointer, "value_digest": value_digest,
                               "contributors": contributor, "introduced_at": "plan",
                               "required_at": "plan"})
    result.sort(key=lambda item: item["id"])
    return result


# Explicit aliases make the producer/consumer handoff discoverable without
# adding another public workflow surface.
derive_impact_obligations = impact_obligations
