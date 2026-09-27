"""Exact non-Git Delivery output references and immutable output material.

This module is deliberately independent from :mod:`daikibo.assurance`.  The
live controller and the portable archive adapter both provide the same
read-only ``resolve_ref``/``load_blob`` boundary and therefore share this
validator instead of maintaining two output interpretations.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable, Mapping

from .assurance_relations import validate_typed_ref
from .common import digest, need, relative_path, text

OUTPUT_FIELDS = ("id", "repo", "path", "blob", "bytes", "mode")
OUTPUT_MATERIAL_FORMAT = "daikibo.delivery-output-material.v1"
OUTPUT_MATERIAL_KIND = "delivery_output"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_MODES = {0o644, 0o755}


def _sha(value: Any, name: str) -> None:
    need(isinstance(value, str) and _SHA256.fullmatch(value),
         "invalid_output", f"{name} must be a lowercase SHA-256")


def _identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _identity(item) for key, item in value.items()
                if key not in {"identity_digest", "semantic_kind"}}
    if isinstance(value, list):
        return [_identity(item) for item in value]
    return value


def validate_output_record(value: Any, *, name: str = "output") -> dict[str, Any]:
    """Validate one producer output using the exact six-field wire."""
    need(isinstance(value, dict) and set(value) == set(OUTPUT_FIELDS),
         "invalid_output", f"{name} must contain exactly the six output fields")
    text(value["id"], f"{name}.id", 200)
    text(value["repo"], f"{name}.repo", 200)
    relative_path(value["path"])
    need(value["path"].startswith(".daikibo-build/"),
         "invalid_output", f"{name}.path is outside the Delivery output area")
    _sha(value["blob"], f"{name}.blob")
    need(type(value["bytes"]) is int and value["bytes"] >= 0,
         "invalid_output", f"{name}.bytes must be a non-negative integer")
    need(type(value["mode"]) is int and value["mode"] in _OUTPUT_MODES,
         "invalid_output", f"{name}.mode must be 0644 or 0755")
    return copy.deepcopy(value)


def validate_output_reference(ref: Any, *, project: str | None = None) -> dict[str, Any]:
    """Validate and return the exact public output-artifact reference."""
    need(isinstance(ref, dict), "invalid_reference", "Output artifact reference must be an object")
    required = {"kind", "project", "delivery", "check", "observed", "output_id", "output_digest"}
    need(set(ref) == required, "invalid_reference", "Output artifact reference keys differ")
    need(ref.get("kind") == "output_artifact", "invalid_reference", "Reference is not an output artifact")
    need(isinstance(ref.get("project"), str) and bool(ref["project"]) and "\x00" not in ref["project"],
         "invalid_reference", "Output artifact project is invalid")
    if project is not None:
        need(ref["project"] == project, "cross_project", "Output artifact belongs to another project")
    text(ref.get("output_id"), "output_id", 200)
    _sha(ref["output_digest"], "output_digest")
    delivery = validate_typed_ref(ref["delivery"], project=ref["project"], expected_kinds={"delivery_snapshot"})
    check = validate_typed_ref(ref["check"], project=ref["project"], expected_kinds={"delivery_check"})
    observed = validate_typed_ref(ref["observed"], project=ref["project"], expected_kinds={"observed_result"})
    # Compare persisted identities, excluding resolver-only projections.
    need(_identity(check) ["delivery"] == _identity(delivery),
         "invalid_reference", "Output check is bound to another delivery snapshot")
    normalized = {"kind": "output_artifact", "project": ref["project"],
                  "delivery": _identity(delivery), "check": _identity(check),
                  "observed": _identity(observed), "output_id": ref["output_id"],
                  "output_digest": ref["output_digest"]}
    return normalized


def _load_blob(loader: Any, ident: str) -> bytes:
    if callable(getattr(loader, "blob_get", None)):
        raw = loader.blob_get(ident)
    elif callable(loader):
        raw = loader(ident)
    elif isinstance(loader, Mapping):
        raw = loader.get(ident)
    else:
        raw = None
    need(isinstance(raw, (bytes, bytearray)), "missing_evidence", "Output CAS blob is unavailable", ident)
    raw = bytes(raw)
    need(digest(raw) == ident, "integrity_error", "Output CAS blob digest differs", ident)
    return raw


def _require_resolution(result: Any, kind: str) -> dict[str, Any]:
    need(isinstance(result, dict), "unresolved_reference", f"{kind} resolver returned no identity")
    need(type(result.get("current")) is bool, "integrity_error", f"{kind} resolver omitted currentness")
    return result


def _single(items: Any, predicate: Callable[[Any], bool], message: str) -> Any:
    need(isinstance(items, list), "integrity_error", message)
    matches = [item for item in items if predicate(item)]
    need(len(matches) == 1, "integrity_error", message)
    return matches[0]


def validate_output_material(ref: Any, *, output_payload: Any,
                             resolve_ref: Callable[[dict[str, Any]], dict[str, Any]],
                             load_blob: Any) -> dict[str, Any]:
    """Validate fixed Delivery output material through shared read boundaries.

    ``resolve_ref`` must resolve the three nested typed references to their
    canonical retained content.  It may be backed by live SQLite or an archive
    mapping; this function never calls a mutation or a fallback callback.
    """
    ref = validate_output_reference(ref, project=ref.get("project") if isinstance(ref, dict) else None)
    need(isinstance(output_payload, dict), "integrity_error", "Delivery output material payload is malformed")
    required_payload = {"format", "project", "delivery_ref", "check_ref", "observed_ref",
                        "output_identity_digest", "output", "delivery_result",
                        "delivery_output", "declared_definition"}
    need(set(output_payload) == required_payload and output_payload.get("format") == OUTPUT_MATERIAL_FORMAT,
         "integrity_error", "Delivery output material payload keys differ")
    need(output_payload["project"] == ref["project"], "cross_project", "Output material project differs")
    delivery_ref = validate_output_reference({**ref, "delivery": output_payload["delivery_ref"],
                                              "check": ref["check"], "observed": ref["observed"]},
                                             project=ref["project"])["delivery"]
    check_ref = validate_typed_ref(output_payload["check_ref"], project=ref["project"], expected_kinds={"delivery_check"})
    observed_ref = validate_typed_ref(output_payload["observed_ref"], project=ref["project"], expected_kinds={"observed_result"})
    need(_identity(delivery_ref) == _identity(ref["delivery"]) and
         _identity(check_ref) == _identity(ref["check"]) and
         _identity(observed_ref) == _identity(ref["observed"]),
         "integrity_error", "Output material nested reference differs from its public reference")

    delivery = _require_resolution(resolve_ref(delivery_ref), "delivery")
    check = _require_resolution(resolve_ref(check_ref), "check")
    observed = _require_resolution(resolve_ref(observed_ref), "observed")
    delivery_payload = delivery.get("payload")
    need(isinstance(delivery_payload, dict), "missing_evidence", "Pinned Delivery snapshot payload is unavailable")
    checks = delivery_payload.get("checks")
    definitions = delivery_payload.get("build_definitions")
    need(isinstance(checks, list) and isinstance(definitions, list),
         "integrity_error", "Pinned Delivery snapshot lacks checks or definitions")
    check_body = check.get("content")
    need(isinstance(check_body, dict) and check_body.get("id") == check_ref["check_id"] and
         digest(check_body) == check_ref["check_digest"],
         "integrity_error", "Pinned Delivery check identity differs")
    need(_identity(check_ref.get("delivery")) == _identity(delivery_ref),
         "integrity_error", "Delivery check does not belong to the pinned snapshot")
    snapshot = _single(checks, lambda item: isinstance(item, dict) and item.get("id") == check_ref["check_id"] and
                       digest(item) == check_ref["check_digest"], "Delivery check is not retained by its snapshot")
    need(snapshot == check_body, "integrity_error", "Delivery check resolver returned another body")
    receipt = observed.get("content")
    need(isinstance(receipt, dict), "missing_evidence", "Observed Delivery receipt is unavailable")
    need(receipt.get("id") == observed_ref["receipt"] and receipt.get("run") == observed_ref["run"] and
         receipt.get("binding") == observed_ref["run_binding"] and
         digest(receipt) == observed_ref["receipt_digest"] and
         digest(receipt.get("result", {})) == observed_ref["result_digest"],
         "integrity_error", "Observed receipt identity differs")
    observed_payload = observed.get("payload")
    need(isinstance(observed_payload, dict), "missing_evidence", "Observed execution material payload is unavailable")
    need(_identity(observed_payload.get("definition_ref")) == _identity(check_ref),
         "integrity_error", "Observed execution uses another Delivery check")
    subject = observed_payload.get("execution_subject")
    need(isinstance(subject, dict) and subject.get("kind") == "delivery" and
         subject.get("id") == delivery_ref["delivery"] and
         subject.get("binding") == delivery_ref["binding_digest"],
         "integrity_error", "Observed execution subject is not this Delivery")

    output = validate_output_record(output_payload["output"], name="output")
    need(ref["output_id"] == output["id"], "integrity_error",
         "Output artifact reference names another output record")
    sealed = delivery_payload.get("snapshot")
    need(isinstance(sealed, dict) and isinstance(sealed.get("repos"), dict) and
         output["repo"] in sealed["repos"],
         "integrity_error", "Output repository is absent from the pinned Delivery snapshot")
    delivery_output = output_payload["delivery_output"]
    need(isinstance(delivery_output, dict) and set(delivery_output) == set(OUTPUT_FIELDS) | {"producer_receipt"},
         "integrity_error", "Delivery output record shape differs")
    normalized_delivery_output = validate_output_record(
        {key: delivery_output[key] for key in OUTPUT_FIELDS}, name="delivery_output")
    need(delivery_output["producer_receipt"] == observed_ref["receipt"],
         "integrity_error", "Delivery output producer receipt differs")
    need(normalized_delivery_output == output,
         "integrity_error", "Output and Delivery output records differ")
    result = receipt.get("result")
    need(isinstance(result, dict), "integrity_error", "Observed receipt result is malformed")
    result_outputs = result.get("build_outputs")
    result_output = _single(result_outputs,
                            lambda item: isinstance(item, dict) and item.get("id") == output["id"],
                            "Observed receipt does not contain exactly one selected output")
    validate_output_record(result_output, name="receipt.result.build_outputs")
    need(result_output == output, "integrity_error", "Receipt output differs from retained output")
    runtime_check = observed.get("runtime_check")
    need(isinstance(runtime_check, dict) and runtime_check.get("id") == check_ref["check_id"],
         "missing_evidence", "Adjusted Delivery check material is unavailable")
    adjusted_inputs = runtime_check.get("build_inputs")
    result_inputs = result.get("build_inputs")
    need(isinstance(adjusted_inputs, list) and isinstance(result_inputs, list) and
         adjusted_inputs == result_inputs,
         "integrity_error", "Adjusted Delivery inputs differ from the observed result")
    for input_item in adjusted_inputs:
        need(isinstance(input_item, dict) and
             set(input_item) == set(OUTPUT_FIELDS) | {"producer_receipt"},
             "integrity_error", "Adjusted Delivery input record shape differs")
        validate_output_record({key: input_item[key] for key in OUTPUT_FIELDS},
                               name="delivery_input")
        text(input_item["producer_receipt"], "delivery_input.producer_receipt", 200)
    need(isinstance(result.get("passed"), bool) and result.get("passed") is True and
         receipt.get("exit_code") == 0 and not any(receipt.get(name) for name in
                                                    ("timed_out", "cancelled", "output_overflow", "input_mutated")),
         "failed_output", "A failed Delivery execution cannot produce a successful output artifact")

    delivery_result = output_payload["delivery_result"]
    need(isinstance(delivery_result, dict) and
         set(delivery_result) == {"check", "category", "receipt", "passed", "verification_material"},
         "integrity_error", "Delivery result shape differs")
    need(delivery_result.get("check") == check_ref["check_id"] and
         delivery_result.get("receipt") == observed_ref["receipt"] and
         delivery_result.get("passed") is True,
         "integrity_error", "Delivery result does not identify the producer receipt")
    need(delivery_result.get("verification_material") == receipt.get("verification_material"),
         "integrity_error", "Delivery result material differs from producer receipt")

    declared = output_payload["declared_definition"]
    need(isinstance(declared, dict) and set(declared) == {"id", "repo", "path"},
         "integrity_error", "Declared build definition shape differs")
    for key in ("id", "repo", "path"):
        need(declared[key] == output[key], "integrity_error", "Declared build definition differs from output", key)
    matching_def = _single(definitions, lambda item: isinstance(item, dict) and
                           set(item) == {"id", "repo", "path"} and item.get("id") == output["id"],
                           "Delivery definition is missing or ambiguous")
    need({key: matching_def[key] for key in ("id", "repo", "path")} == declared,
         "integrity_error", "Delivery definition differs from retained output")
    adjusted_outputs = runtime_check.get("build_outputs")
    adjusted_definition = _single(
        adjusted_outputs,
        lambda item: isinstance(item, dict) and
        set(item) == {"id", "repo", "path"} and item.get("id") == output["id"],
        "Adjusted Delivery output definition is missing or ambiguous")
    need(adjusted_definition == declared,
         "integrity_error", "Adjusted Delivery output definition differs")
    need(output["id"] in check_body.get("produces", []),
         "integrity_error", "Selected output is not declared by its producer check")
    other_producers = [item for item in checks if isinstance(item, dict) and output["id"] in item.get("produces", [])]
    need(len(other_producers) == 1 and other_producers[0].get("id") == check_body.get("id"),
         "integrity_error", "Selected output has multiple or foreign producer checks")

    raw = _load_blob(load_blob, output["blob"])
    need(len(raw) == output["bytes"], "integrity_error", "Output CAS length differs")
    need(digest(raw) == output["blob"], "integrity_error", "Output CAS digest differs")
    expected_digest = digest(output)
    need(ref["output_digest"] == expected_digest and
         output_payload["output_identity_digest"] == digest(ref),
         "integrity_error", "Output identity digest differs")

    dependencies = [delivery_ref, check_ref, observed_ref]
    current = delivery["current"] and check["current"] and observed["current"]
    return {"output": output, "dependencies": dependencies,
            "content_refs": [{"purpose": "delivery_build_output.blob", "digest": output["blob"]}],
            "membership": [{"relation": "delivery_build_output", "container": delivery_ref,
                            "member": ref, "state": "verified"}],
            "current": current, "delivery": delivery, "check": check,
            "observed": observed, "delivery_result": copy.deepcopy(delivery_result),
            "declared_definition": copy.deepcopy(declared)}


def resolve_output_artifact(ref: Any, *, resolve_ref: Callable[[dict[str, Any]], dict[str, Any]],
                           load_blob: Any, load_delivery_record: Any) -> dict[str, Any]:
    """Resolve one retained output through the live/archive read adapters.

    The delivery-record callback returns the immutable ``delivery_output``
    material payload (or a wrapper with that payload under ``payload``).  It
    cannot supply output bytes or a replacement definition: the common
    validator still derives every cross-field identity and reads the output
    CAS through ``load_blob``.
    """
    need(callable(resolve_ref), "missing_evidence", "Output reference resolver is unavailable")
    need(callable(load_delivery_record), "missing_evidence", "Output material read boundary is unavailable")
    record = load_delivery_record(validate_output_reference(ref, project=ref.get("project")
                                                            if isinstance(ref, dict) else None))
    if isinstance(record, dict) and set(record) == {"payload"}:
        record = record["payload"]
    need(isinstance(record, dict) and record.get("format") == OUTPUT_MATERIAL_FORMAT,
         "missing_evidence", "Delivery output material payload is unavailable")
    return validate_output_material(ref, output_payload=record,
                                    resolve_ref=resolve_ref, load_blob=load_blob)


def capture_output_material(control: Any, actor: Any, *, project: str,
                            delivery_ref: dict[str, Any], check_id: str,
                            receipt_id: str, output_id: str) -> dict[str, Any]:
    """Capture producer output from authoritative live Delivery rows.

    This is an adapter, not a payload upload API: every result and six-field
    output is read from the controller's Delivery/receipt records.
    """
    assurance = getattr(control, "assurance", None)
    need(assurance is not None, "missing_evidence", "Assurance output capture boundary is unavailable")
    delivery_ref = _identity(validate_typed_ref(delivery_ref, project=project, expected_kinds={"delivery_snapshot"}))
    delivery_resolution = assurance._resolve_locator(actor, delivery_ref, current=True)
    delivery_payload = delivery_resolution.get("payload")
    need(isinstance(delivery_payload, dict), "missing_evidence", "Current Delivery snapshot is unavailable")
    checks = delivery_payload.get("checks")
    check = _single(checks, lambda item: isinstance(item, dict) and item.get("id") == check_id,
                    "Selected Delivery check is missing or ambiguous")
    check_ref = control.rt.verification_materials.delivery_check_ref(project, delivery_ref, check)
    receipt = control.g.receipt(receipt_id)
    need(receipt.get("subject") == delivery_ref["delivery"] if "subject" in receipt else True,
         "integrity_error", "Receipt subject is not the selected Delivery")
    observed_ref = {"kind": "observed_result", "project": project,
                    "receipt": receipt["id"], "run": receipt["run"],
                    "receipt_digest": digest(receipt), "run_binding": receipt["binding"],
                    "snapshot_digest": receipt["snapshot"],
                    "result_digest": digest(receipt.get("result", {}))}
    validate_typed_ref(observed_ref, project=project)
    observed_resolution = assurance._resolve_locator(actor, observed_ref, current=True)
    result = receipt.get("result")
    need(isinstance(result, dict), "integrity_error", "Selected receipt result is malformed")
    outputs = result.get("build_outputs")
    item = _single(outputs, lambda value: isinstance(value, dict) and value.get("id") == output_id,
                   "Selected Delivery output is missing or ambiguous")
    output = validate_output_record(item, name="receipt.result.build_outputs")
    current_row = control.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery_ref["delivery"], project), True)
    need(current_row is not None, "unresolved_reference", "Selected Delivery is missing")
    body = control.d.current(delivery_ref["delivery"])[1]
    results = body.get("results")
    delivery_result = _single(results, lambda value: isinstance(value, dict) and value.get("check") == check_id,
                              "Delivery result is missing or ambiguous")
    need(delivery_result.get("receipt") == receipt_id and delivery_result.get("passed") is True,
         "failed_output", "Selected Delivery check did not produce a successful result")
    available = body.get("build_outputs")
    delivered = _single([available.get(output_id)] if isinstance(available, dict) else None,
                        lambda value: isinstance(value, dict),
                        "Delivery build output is missing")
    delivery_output = validate_output_record({key: delivered[key] for key in OUTPUT_FIELDS}, name="Delivery build output")
    need(delivered.get("producer_receipt") == receipt_id and delivery_output == output,
         "integrity_error", "Delivery build output differs from producer receipt")
    definitions = delivery_payload.get("build_definitions")
    definition = _single(definitions, lambda value: isinstance(value, dict) and value.get("id") == output_id,
                         "Selected build definition is missing or ambiguous")
    declared = {key: definition[key] for key in ("id", "repo", "path")}
    ref = {"kind": "output_artifact", "project": project, "delivery": delivery_ref,
           "check": check_ref, "observed": observed_ref, "output_id": output_id,
           "output_digest": digest(output)}
    ref = validate_output_reference(ref, project=project)
    payload = {"format": OUTPUT_MATERIAL_FORMAT, "project": project,
               "delivery_ref": delivery_ref, "check_ref": check_ref,
               "observed_ref": observed_ref, "output_identity_digest": digest(ref),
               "output": output, "delivery_result": copy.deepcopy(delivery_result),
               "delivery_output": {**output, "producer_receipt": receipt_id},
               "declared_definition": declared}
    # Verify the exact payload before handing it to Assurance.store_material.
    validate_output_material(
        ref, output_payload=payload,
        resolve_ref=lambda nested: assurance._resolve_locator(actor, nested, current=False),
        load_blob=control.s,
    )
    return {"ref": ref, "payload": payload,
            "dependencies": [delivery_ref, check_ref, observed_ref]}
