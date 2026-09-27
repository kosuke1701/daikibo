"""Controller-backed matching for Consumer-C Delivery declarations.

The denominator is made from the pinned Delivery ``build_definitions``.  This
module only matches those immutable declarations to later producer
observations and already-pinned ``output_artifact`` material.  It never turns
an observed output into a new denominator member and it never treats a
mechanical match as a semantic N/E/S review.

The generic matcher receives read-only declaration, producer and output
resolvers.  Live and portable archive callers can therefore use the same
cross-field checks while keeping their storage adapters separate.  The live
adapter below obtains all observations from controller rows; callers cannot
submit a receipt, output body, or producer owner as a replacement.
"""
from __future__ import annotations

from .assurance_profile_contract import profile_has_outputs

import copy
from collections.abc import Callable
from typing import Any

from .assurance_denominators import (
    DENOMINATOR_V4_FORMAT,
    DENOMINATOR_V3_FORMAT,
    DERIVATION_V5,
    DELIVERY_DECLARED_OUTPUT_CATEGORY,
    PROFILE_V3_FORMAT,
    _validate_denominator,
)
from .assurance_outputs import validate_output_record, validate_output_reference
from .assurance_relations import REGISTRY_V2_DIGEST, validate_typed_ref
from .build_outputs import validate_definition
from .common import Fault, canonical, digest, need
from .delivery_material_reader import DELIVERY_MATERIAL_EXTRACTOR, DELIVERY_REPOSITORY_EXTRACTOR


FORMAT = "daikibo.delivery-declared-output-match.v1"
_DERIVED_REF_FIELDS = {"identity_digest", "semantic_kind"}
_DECLARATION_KEYS = {"definition", "producer_ref"}
_OUTPUT_RESOLUTION_KEYS = {"output", "declared_definition", "membership", "current"}


def _identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _identity(item) for key, item in value.items()
                if key not in _DERIVED_REF_FIELDS}
    if isinstance(value, list):
        return [_identity(item) for item in value]
    return value


def _same(left: Any, right: Any) -> bool:
    return canonical(_identity(left)) == canonical(_identity(right))


def _fault(code: str, message: str, details: Any = None) -> Fault:
    return Fault(code, message, details)


def _diagnostic(code: str, *, obligation: str | None = None,
                output_id: str | None = None, producer: Any = None,
                detail: Any = None) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code}
    if obligation is not None:
        value["obligation_id"] = obligation
    if output_id is not None:
        value["output_id"] = output_id
    if producer is not None:
        value["producer"] = _identity(producer)
    if detail is not None:
        # Details are deliberately bounded to a digest.  Failure receipts can
        # contain private command/environment material and are not report data.
        value["detail_digest"] = digest(_identity(detail))
    return value


def _delivery_ref_semantic(value: Any) -> Any:
    """Compare Delivery anchors without accepting a different Delivery.

    Capture pins identify the resolver material, while ``delivery``, binding,
    and snapshot digests identify the saved Delivery meaning.  v4 readers use
    this same distinction for actual commit dependencies.  No other fields
    are projected away here.
    """
    if isinstance(value, dict):
        result = {key: _delivery_ref_semantic(item) for key, item in value.items()}
        if result.get("kind") in {"delivery_snapshot", "actual_delivery_commit"}:
            result.pop("pin", None)
        return result
    if isinstance(value, list):
        return [_delivery_ref_semantic(item) for item in value]
    return value


def delivery_declaration_owner_matches(center_ref: Any, definition: Any) -> bool:
    """Match one declared output to its typed Delivery center owner.

    A snapshot is the complete declaration population.  An actual commit is
    one repository partition of that same population, so only definitions
    naming the commit's repository belong to its Consumer-C request.  This is
    shared by denominator/request selection and the evaluator to keep the
    snapshot union and actual partitions identical.
    """
    if not isinstance(center_ref, dict) or not isinstance(definition, dict):
        return False
    center_kind = center_ref.get("kind")
    if center_kind == "delivery_snapshot":
        return True
    if center_kind != "actual_delivery_commit":
        return False
    repository = center_ref.get("repository")
    return isinstance(repository, str) and bool(repository) and definition.get("repo") == repository


def _require_delivery_denominator(denominator: Any) -> dict[str, Any]:
    """Require one sealed, fully selected v3 or v4 output denominator.

    v4 is a material-reader wire, rather than a version string accepted by
    the old matcher.  Its derivation, reader extractors, anchor population,
    and exact declared-output capability are checked before any callback can
    be consulted.  This keeps an older or partially assembled denominator
    from being interpreted as the new Delivery contract.
    """
    value = _validate_denominator(denominator)
    if value["format"] not in {DENOMINATOR_V3_FORMAT, DENOMINATOR_V4_FORMAT}:
        raise _fault(
            "unsupported",
            "Delivery declared-output matching requires denominator.v3 or denominator.v4",
            value.get("format"),
        )
    if value.get("stage") not in {"integration", "delivery"}:
        raise _fault("unsupported", "Delivery declared-output matching requires integration or delivery stage")
    extractor_versions = value.get("extractor_versions")
    if not isinstance(extractor_versions, dict) or \
            extractor_versions.get(DELIVERY_DECLARED_OUTPUT_CATEGORY) != "delivery-declared-output.v1":
        raise _fault("unsupported", "Delivery declared-output extractor is not selected")
    selection = value.get("capabilities", {}).get("selection")
    if (not isinstance(selection, dict) or
            not profile_has_outputs(selection.get("profile_format")) or
            selection.get("effective_relation_contract_digest") != REGISTRY_V2_DIGEST):
        raise _fault("unsupported", "Delivery declared-output matching requires selected profile.v3 and registry.v2")
    if value["format"] == DENOMINATOR_V3_FORMAT:
        return value

    if value.get("derivation_version") != DERIVATION_V5:
        raise _fault("unsupported", "Delivery v4 matching requires denominator derivation.v5")
    if (extractor_versions.get("delivery_material") != DELIVERY_MATERIAL_EXTRACTOR or
            extractor_versions.get("delivery_repository") != DELIVERY_REPOSITORY_EXTRACTOR):
        raise _fault("unsupported", "Delivery v4 matching requires the material and repository readers")

    capabilities = value.get("capabilities")
    derivation = capabilities.get("derivation") if isinstance(capabilities, dict) else None
    if (not isinstance(derivation, dict) or derivation.get("supported") is not True or
            derivation.get("version") != DERIVATION_V5):
        raise _fault("unsupported", "Delivery v4 denominator derivation capability is not sealed")
    extractor_capabilities = capabilities.get("extractors") if isinstance(capabilities, dict) else None
    declared_capability = (extractor_capabilities.get(DELIVERY_DECLARED_OUTPUT_CATEGORY)
                           if isinstance(extractor_capabilities, dict) else None)
    if (not isinstance(declared_capability, dict) or
            declared_capability.get("supported") is not True or
            declared_capability.get("version") != "delivery-declared-output.v1"):
        raise _fault("unsupported", "Delivery v4 declared-output capability is not sealed")

    refs = value.get("input_refs")
    if not isinstance(refs, list):
        raise _fault("integrity_error", "Delivery v4 denominator input references are not a list")
    snapshots = [ref for ref in refs if isinstance(ref, dict) and ref.get("kind") == "delivery_snapshot"]
    if len(snapshots) != 1:
        raise _fault("integrity_error", "Delivery v4 denominator must have one snapshot anchor")
    anchor = snapshots[0]
    checks = [ref for ref in refs if isinstance(ref, dict) and ref.get("kind") == "delivery_check"]
    for check in checks:
        if not _same(check.get("delivery"), anchor):
            raise _fault("integrity_error", "Delivery v4 check input is bound to another snapshot")
    for actual in (ref for ref in refs
                   if isinstance(ref, dict) and ref.get("kind") == "actual_delivery_commit"):
        if _delivery_ref_semantic(actual.get("delivery")) != _delivery_ref_semantic(anchor):
            raise _fault("integrity_error", "Delivery v4 actual input is bound to another snapshot")

    input_keys = {canonical(_identity(ref)) for ref in refs}
    declared = _declared_obligations(value)
    if declared and not checks:
        raise _fault("integrity_error", "Delivery v4 declared outputs have no sealed producer checks")
    for obligation in declared:
        source = obligation.get("source_ref")
        if not isinstance(source, dict) or not _same(source, anchor):
            raise _fault("integrity_error", "Delivery v4 declared output is bound to another snapshot")
        if canonical(_identity(source)) not in input_keys:
            raise _fault("integrity_error", "Delivery v4 declared output source is absent from inputs")
    return value


def _require_live_v4_anchor_current(control: Any, actor: Any,
                                    denominator: dict[str, Any]) -> None:
    """Bind live v4 matching to the current saved Delivery anchor.

    The denominator retains historical refs for restart and archive reads.
    A live global proof still needs the selected Delivery head to be current;
    ``resolve_pinned`` alone intentionally reports historical material without
    making that claim.
    """
    anchor = next(
        (ref for ref in denominator["input_refs"]
         if isinstance(ref, dict) and ref.get("kind") == "delivery_snapshot"),
        None,
    )
    if anchor is None:
        raise _fault("integrity_error", "Delivery v4 denominator has no live snapshot anchor")
    assurance = getattr(control, "assurance", None)
    evaluate_current = getattr(assurance, "evaluate_current", None)
    if not callable(evaluate_current):
        raise _fault("unsupported", "Delivery v4 live currentness resolver is unavailable")
    try:
        result = evaluate_current(actor, anchor)
    except Fault as exc:
        raise _fault("stale_reference", "Delivery v4 snapshot anchor is not current", exc.as_dict()) from exc
    current = result.get("current") if isinstance(result, dict) else None
    if not isinstance(current, dict) or current.get("state") != "current":
        raise _fault("stale_reference", "Delivery v4 snapshot anchor is not current", current)


# Keep the old private name for callers in retained diagnostics.  It now
# denotes the shared v3/v4 contract gate, rather than a v3-only allowlist.
_require_v3 = _require_delivery_denominator


def _declared_obligations(denominator: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [item for item in denominator["obligations"]
            if item.get("category") == DELIVERY_DECLARED_OUTPUT_CATEGORY]
    rows.sort(key=lambda item: item["id"])
    return rows


def _validate_declaration(value: Any, *, obligation: dict[str, Any],
                          project: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(value) is not dict or set(value) != _DECLARATION_KEYS:
        raise _fault("integrity_error", "Delivery declaration resolver returned an invalid shape")
    definition = value["definition"]
    if type(definition) is not dict or set(definition) != {"id", "repo", "path"}:
        raise _fault("integrity_error", "Delivery declaration definition is not exact")
    validate_definition(definition)
    if digest(definition) != obligation.get("value_digest"):
        raise _fault("integrity_error", "Delivery declaration digest differs from denominator")
    producer = validate_typed_ref(value["producer_ref"], project=project,
                                  expected_kinds={"delivery_check"})
    source = validate_typed_ref(obligation["source_ref"], project=project,
                                expected_kinds={"delivery_snapshot"})
    if not _same(producer.get("delivery"), source):
        raise _fault("integrity_error", "Delivery producer is bound to another snapshot")
    return copy.deepcopy(definition), _identity(producer)


def _validate_observation(value: Any, *, definition: dict[str, Any],
                          producer: dict[str, Any], project: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise _fault("integrity_error", "Delivery producer resolver returned an invalid shape")
    status = value.get("status")
    if status not in {"observed", "failed", "missing", "unverified"}:
        raise _fault("integrity_error", "Delivery producer resolver returned an unknown status")
    result: dict[str, Any] = {"status": status}
    if "diagnostics" in value:
        diagnostics = value["diagnostics"]
        if type(diagnostics) is not list:
            raise _fault("integrity_error", "Delivery producer diagnostics are not a list")
        result["diagnostics"] = copy.deepcopy(diagnostics[:20])
    if status == "observed":
        ref = validate_output_reference(value.get("output_ref"), project=project)
        if not _same(ref.get("delivery"), producer.get("delivery")):
            raise _fault("integrity_error", "Observed output belongs to another Delivery")
        if not _same(ref.get("check"), producer):
            raise _fault("integrity_error", "Observed output producer differs from declaration")
        if ref.get("output_id") != definition["id"]:
            raise _fault("integrity_error", "Observed output ID differs from declaration")
        result["output_ref"] = _identity(ref)
    return result


def _validate_output_resolution(value: Any, *, output_ref: dict[str, Any],
                                definition: dict[str, Any],
                                require_current: bool) -> dict[str, Any]:
    if type(value) is not dict or not _OUTPUT_RESOLUTION_KEYS <= set(value):
        raise _fault("integrity_error", "Output resolver returned an incomplete resolution")
    if type(value.get("current")) is not bool:
        raise _fault("integrity_error", "Output resolver omitted currentness")
    if require_current and value["current"] is not True:
        raise _fault("stale_reference", "Output material is historical, not current")
    output = validate_output_record(value["output"], name="resolved output")
    if output["id"] != definition["id"] or \
            {key: output[key] for key in ("id", "repo", "path")} != definition:
        raise _fault("integrity_error", "Resolved output differs from declaration")
    if output_ref["output_digest"] != digest(output):
        raise _fault("integrity_error", "Resolved output digest differs from reference")
    declared = value["declared_definition"]
    if type(declared) is not dict or declared != definition:
        raise _fault("integrity_error", "Resolved producer declaration differs")
    membership = value["membership"]
    if type(membership) is not list:
        raise _fault("integrity_error", "Output resolver membership is not a list")
    exact = [item for item in membership
             if isinstance(item, dict) and item.get("relation") == "delivery_build_output"
             and item.get("state") == "verified"
             and _same(item.get("container"), output_ref["delivery"])
             and _same(item.get("member"), output_ref)]
    if len(exact) != 1:
        raise _fault("integrity_error", "Resolved output lacks exact Delivery membership")
    return {"output": output, "declared_definition": copy.deepcopy(declared),
            "membership": copy.deepcopy(exact), "current": value["current"]}


def match_delivery_declared_outputs(
    denominator: Any, *,
    resolve_declaration: Callable[[dict[str, Any], str], dict[str, Any]],
    resolve_producer: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    resolve_output: Callable[[dict[str, Any]], dict[str, Any]],
    require_current: bool = True,
) -> dict[str, Any]:
    """Match every v3 Delivery declaration to authoritative execution material.

    The three callbacks are read boundaries, not payload suppliers.  A live
    controller and an archive adapter both resolve the same declaration,
    producer receipt and output material from their retained rows/CAS.  The
    returned ``mechanical_state`` is intentionally separate from
    ``semantic_status``; even an exact output/member match is only eligible
    for the later relation/review stages.
    """
    denominator = _require_v3(denominator)
    project = denominator["project"]
    obligations = _declared_obligations(denominator)
    rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    if not obligations:
        return {
            "format": FORMAT, "project": project,
            "registry_digest": REGISTRY_V2_DIGEST,
            "denominator_digest": denominator["digest"],
            "obligations": [], "mechanical_state": "unverified",
            "semantic_status": "unverified",
            "diagnostics": [_diagnostic("empty_denominator_requires_review")],
            "capabilities": {"producer": True, "output_material": True,
                             "membership": True, "semantic_review": False},
        }
    for obligation in obligations:
        oid = obligation["id"]
        output_id = None
        row: dict[str, Any] = {"obligation_id": oid, "mechanical_state": "unverified",
                               "diagnostics": []}
        try:
            resolved_declaration = resolve_declaration(obligation["source_ref"], obligation["pointer"])
            definition, producer = _validate_declaration(
                resolved_declaration, obligation=obligation, project=project,
            )
            output_id = definition["id"]
            row.update({"output_id": output_id, "definition": definition,
                        "producer_ref": producer})
            observed = _validate_observation(
                resolve_producer(producer, definition), definition=definition,
                producer=producer, project=project,
            )
            row["producer_observation"] = observed
            if observed.get("diagnostics"):
                row["diagnostics"].extend(copy.deepcopy(observed["diagnostics"][:20]))
            if observed["status"] == "failed":
                row["mechanical_state"] = "failed"
                row["diagnostics"].append(_diagnostic(
                    "producer_execution_failed", obligation=oid,
                    output_id=output_id, producer=producer,
                ))
            elif observed["status"] in {"missing", "unverified"}:
                row["mechanical_state"] = observed["status"]
                row["diagnostics"].append(_diagnostic(
                    "producer_" + observed["status"], obligation=oid,
                    output_id=output_id, producer=producer,
                ))
            else:
                output_ref = observed["output_ref"]
                try:
                    resolved_output = resolve_output(output_ref)
                    checked = _validate_output_resolution(
                        resolved_output, output_ref=output_ref,
                        definition=definition, require_current=require_current,
                    )
                    row.update({"output_ref": output_ref,
                                "membership": checked["membership"],
                                "mechanical_state": "eligible"})
                except Fault as exc:
                    row["mechanical_state"] = "stale" if exc.code == "stale_reference" else "unverified"
                    row["diagnostics"].append(_diagnostic(
                        "output_material_" + exc.code, obligation=oid,
                        output_id=output_id, producer=producer,
                    ))
        except Fault as exc:
            row["mechanical_state"] = "unverified"
            row["diagnostics"].append(_diagnostic(
                "declaration_" + exc.code, obligation=oid,
                output_id=output_id, detail=exc.as_dict(),
            ))
        diagnostics.extend(row["diagnostics"])
        rows.append(row)

    states = [row["mechanical_state"] for row in rows]
    if any(state == "failed" for state in states):
        mechanical = "failed"
    elif any(state == "missing" for state in states):
        mechanical = "missing"
    elif any(state in {"stale", "unverified"} for state in states):
        mechanical = "unverified"
    else:
        mechanical = "eligible"
    return {
        "format": FORMAT, "project": project,
        "registry_digest": REGISTRY_V2_DIGEST,
        "denominator_digest": denominator["digest"],
        "obligations": rows,
        "mechanical_state": mechanical,
        "semantic_status": "unverified",
        "diagnostics": diagnostics[:1000],
        "capabilities": {"producer": True, "output_material": True,
                         "membership": True, "semantic_review": False},
    }


def _live_declaration_resolver(control: Any, actor: Any, project: str) -> Callable[[dict[str, Any], str], dict[str, Any]]:
    assurance = getattr(control, "assurance", None)
    need(assurance is not None, "unsupported", "Assurance controller is unavailable")

    def resolve(source_ref: dict[str, Any], pointer: str) -> dict[str, Any]:
        source = validate_typed_ref(source_ref, project=project,
                                    expected_kinds={"delivery_snapshot"})
        resolved = assurance._resolve_locator(actor, _identity(source), current=False)
        payload = resolved.get("payload")
        need(isinstance(payload, dict), "missing_evidence", "Pinned Delivery payload is unavailable")
        definitions = payload.get("build_definitions")
        checks = payload.get("checks")
        need(isinstance(definitions, list) and isinstance(checks, list),
             "integrity_error", "Pinned Delivery declaration inventory is malformed")
        prefix = "/build_definitions/"
        need(isinstance(pointer, str) and pointer.startswith(prefix) and pointer[len(prefix):].isdigit(),
             "integrity_error", "Delivery declaration pointer is malformed")
        index = int(pointer[len(prefix):])
        need(index < len(definitions), "missing_evidence", "Pinned Delivery declaration is missing")
        definition = definitions[index]
        validate_definition(definition)
        producers = [check for check in checks
                     if isinstance(check, dict) and isinstance(check.get("produces"), list)
                     and definition["id"] in check["produces"]]
        need(len(producers) == 1, "integrity_error", "Delivery declaration producer is not unique")
        producer = control.rt.verification_materials.delivery_check_ref(
            project, _identity(source), producers[0],
        )
        return {"definition": {key: definition[key] for key in ("id", "repo", "path")},
                "producer_ref": producer}
    return resolve


def _receipt_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    result = receipt.get("result")
    return {
        "receipt": receipt.get("id"), "run": receipt.get("run"),
        "exit_code": receipt.get("exit_code"),
        "timed_out": bool(receipt.get("timed_out")),
        "cancelled": bool(receipt.get("cancelled")),
        "output_overflow": bool(receipt.get("output_overflow")),
        "input_mutated": bool(receipt.get("input_mutated")),
        "passed": result.get("passed") if isinstance(result, dict) else None,
        "failure_digest": digest(receipt.get("failure")) if receipt.get("failure") is not None else None,
    }


def _observed_ref(project: str, receipt: dict[str, Any]) -> dict[str, Any]:
    """Build the immutable observed-result identity for one receipt.

    The receipt is only an input to the shared assurance resolver.  Its
    digest, run binding, snapshot and result digest are all part of the
    identity that must be checked before a producer status is interpreted.
    """
    need(
        isinstance(receipt.get("id"), str) and bool(receipt["id"]),
        "producer_receipt_malformed", "Producer receipt has no id",
    )
    need(
        isinstance(receipt.get("run"), str) and bool(receipt["run"]),
        "producer_receipt_malformed", "Producer receipt has no run",
    )
    need(
        isinstance(receipt.get("binding"), str) and bool(receipt["binding"]),
        "producer_receipt_malformed", "Producer receipt has no binding",
    )
    need(
        isinstance(receipt.get("snapshot"), str) and bool(receipt["snapshot"]),
        "producer_receipt_malformed", "Producer receipt has no snapshot",
    )
    return {
        "kind": "observed_result", "project": project,
        "receipt": receipt["id"], "run": receipt["run"],
        "receipt_digest": digest(receipt), "run_binding": receipt["binding"],
        "snapshot_digest": receipt["snapshot"],
        "result_digest": digest(receipt.get("result", {})),
    }


def _live_producer_resolver(control: Any, actor: Any, project: str) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    assurance = getattr(control, "assurance", None)
    need(assurance is not None, "unsupported", "Assurance controller is unavailable")

    def resolve(producer_ref: dict[str, Any], definition: dict[str, Any]) -> dict[str, Any]:
        producer = validate_typed_ref(producer_ref, project=project,
                                      expected_kinds={"delivery_check"})
        assurance._resolve_locator(actor, _identity(producer), current=False)
        delivery_ref = producer["delivery"]
        delivery_id = delivery_ref["delivery"]
        _row, body = control.d.current(delivery_id)
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            return {"status": "unverified", "diagnostics": [{"code": "delivery_results_missing"}]}
        matches = [item for item in results if isinstance(item, dict) and item.get("check") == producer["check_id"]]
        if not matches:
            return {"status": "missing", "diagnostics": [{"code": "producer_observation_missing"}]}
        if len(matches) != 1:
            return {"status": "unverified", "diagnostics": [{"code": "producer_observation_ambiguous"}]}
        result = matches[0]
        receipt_id = result.get("receipt")
        if not isinstance(receipt_id, str) or not receipt_id:
            return {"status": "unverified", "diagnostics": [{"code": "producer_receipt_missing"}]}
        try:
            receipt = control.g.receipt(receipt_id)
        except Fault:
            return {"status": "unverified", "diagnostics": [{"code": "producer_receipt_unavailable"}]}

        # Resolve the complete observed execution before looking at any
        # result/failure flag.  A Delivery result row names a check, but its
        # receipt pointer can be stale, foreign, or missing one of the
        # immutable execution-material CAS leaves.  Interpreting exit=1 (or
        # timeout/cancel/overflow/input mutation) before this relation is
        # checked would attribute another execution's failure to this
        # producer.  The shared resolver accepts genuine failed executions;
        # it only rejects an incoherent observed record as unverified.
        try:
            observed_ref = _observed_ref(project, receipt)
            validate_typed_ref(observed_ref, project=project,
                               expected_kinds={"observed_result"})
            observed_resolution = assurance._resolve_locator(
                actor, _identity(observed_ref), current=False,
            )
            material_payload = observed_resolution.get("payload")
            need(isinstance(material_payload, dict),
                 "producer_observed_material_missing",
                 "Observed execution material payload is unavailable")
            material_definition = material_payload.get("definition_ref")
            need(isinstance(material_definition, dict) and
                 _same(material_definition, producer),
                 "producer_observed_definition_mismatch",
                 "Observed execution material belongs to another producer")
            execution_subject = material_payload.get("execution_subject")
            need(isinstance(execution_subject, dict) and
                 execution_subject.get("kind") == "delivery" and
                 execution_subject.get("id") == delivery_id,
                 "producer_observed_subject_mismatch",
                 "Observed execution subject belongs to another Delivery")
        except Fault as exc:
            summary = _receipt_summary(receipt)
            return {"status": "unverified", "diagnostics": [{
                "code": exc.code or "producer_observed_unverified",
                "receipt": summary,
            }]}
        summary = _receipt_summary(receipt)
        result_passed = result.get("passed")
        if type(result_passed) is not bool or type(summary["passed"]) is not bool:
            return {"status": "unverified", "diagnostics": [{"code": "producer_result_status_missing",
                                                                "receipt": summary}]}
        if result_passed is not summary["passed"]:
            return {"status": "unverified", "diagnostics": [{
                "code": "producer_result_identity_mismatch", "receipt": summary,
            }]}
        if result_passed is False or summary["passed"] is False:
            return {"status": "failed", "diagnostics": [{"code": "producer_execution_failed", "receipt": summary}]}
        if summary["exit_code"] is None:
            return {"status": "unverified", "diagnostics": [{"code": "producer_exit_status_missing",
                                                                "receipt": summary}]}
        if summary["exit_code"] != 0 or receipt.get("failure") is not None or any(
                summary[field] for field in ("timed_out", "cancelled", "output_overflow", "input_mutated")):
            return {"status": "failed", "diagnostics": [{"code": "producer_execution_failed", "receipt": summary}]}
        receipt_result = receipt.get("result")
        outputs = receipt_result.get("build_outputs") if isinstance(receipt_result, dict) else None
        if not isinstance(outputs, list):
            return {"status": "missing", "diagnostics": [{"code": "producer_output_missing", "receipt": summary}]}
        selected = [item for item in outputs if isinstance(item, dict) and item.get("id") == definition["id"]]
        if len(selected) != 1:
            return {"status": "missing" if not selected else "unverified",
                    "diagnostics": [{"code": "producer_output_missing" if not selected else "producer_output_ambiguous",
                                     "receipt": summary}]}
        output = validate_output_record(selected[0], name="producer output")
        output_ref = {
            "kind": "output_artifact", "project": project,
            "delivery": _identity(delivery_ref), "check": _identity(producer),
            "observed": observed_ref, "output_id": output["id"],
            "output_digest": digest(output),
        }
        validate_output_reference(output_ref, project=project)
        return {"status": "observed", "output_ref": output_ref,
                "diagnostics": [{"code": "producer_observed", "receipt": summary}]}
    return resolve


def match_live_delivery_declared_outputs(control: Any, actor: Any, denominator: Any,
                                         *, require_current: bool = True) -> dict[str, Any]:
    """Run the declared-output matcher against one live Control read view."""
    denominator_value = _require_v3(denominator)
    project = denominator_value["project"]
    resolver = _live_declaration_resolver(control, actor, project)
    producer = _live_producer_resolver(control, actor, project)
    assurance = getattr(control, "assurance", None)
    need(assurance is not None, "unsupported", "Assurance controller is unavailable")
    if require_current and denominator_value["format"] == DENOMINATOR_V4_FORMAT:
        _require_live_v4_anchor_current(control, actor, denominator_value)

    def output(ref: dict[str, Any]) -> dict[str, Any]:
        resolved = assurance.resolve_pinned(actor, ref)
        resolution = resolved.get("resolution") if isinstance(resolved, dict) else None
        need(isinstance(resolution, dict), "missing_evidence", "Output pin resolution is unavailable")
        payload = resolution.get("payload")
        need(isinstance(payload, dict), "missing_evidence", "Output pin payload is unavailable")
        return {"output": resolution.get("content"),
                "declared_definition": payload.get("declared_definition"),
                "membership": resolution.get("membership"),
                "current": resolution.get("current")}

    return match_delivery_declared_outputs(
        denominator_value, resolve_declaration=resolver,
        resolve_producer=producer, resolve_output=output,
        require_current=require_current,
    )


__all__ = [
    "FORMAT", "match_delivery_declared_outputs",
    "match_live_delivery_declared_outputs",
]
