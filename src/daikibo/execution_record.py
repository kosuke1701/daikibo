"""Pure consistency checks for a stored subprocess execution record.

The helper deliberately does not know about candidates, Tasks, archives, or
success policy.  It can therefore be shared by observed-result and
traceability consumers without giving either consumer authority to infer or
rewrite execution state.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from .common import Fault, canonical, digest, parse_json


Failure = Callable[[str, str, Any], None]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_FIELDS = frozenset({
    "code", "retryable", "source", "message", "retry_after_seconds",
    "technical_impossibility", "reported_codes", "reported_statuses", "details",
})
_FAILURE_REQUIRED_FIELDS = frozenset({
    "code", "retryable", "source", "message", "retry_after_seconds",
    "technical_impossibility",
})


def _reject(failure: Failure | None, kind: str, message: str, details: Any = None) -> None:
    if failure is not None:
        failure(kind, message, details)
    codes = {
        "invalid": "invalid_reference",
        "integrity": "integrity_error",
    }
    raise Fault(codes.get(kind, "integrity_error"), message, details)


def _expect(condition: Any, failure: Failure | None, kind: str, message: str,
            details: Any = None) -> None:
    if not condition:
        _reject(failure, kind, message, details)


def _sha(value: Any, name: str, failure: Failure | None = None) -> str:
    _expect(isinstance(value, str) and _SHA256.fullmatch(value), failure, "invalid",
            f"{name} must be a lowercase SHA-256 digest", value)
    return value


def _validate_failure_record(value: Any, failure: Failure | None = None) -> None:
    """Validate the actual execution_errors producer shape.

    A complete failure object is a valid failed observation, not a successful
    candidate.  Success policy stays with the caller of this module.
    """
    if value is None:
        return
    _expect(type(value) is dict, failure, "integrity",
            "Execution failure must be null or a producer failure object")
    _expect(_FAILURE_REQUIRED_FIELDS <= set(value) <= _FAILURE_FIELDS, failure, "integrity",
            "Execution failure object has an unexpected schema", value)
    _expect(isinstance(value.get("code"), str) and bool(value["code"]), failure,
            "integrity", "Execution failure code is malformed")
    _expect(type(value.get("retryable")) is bool, failure, "integrity",
            "Execution failure retryable flag is malformed")
    _expect(isinstance(value.get("source"), str) and bool(value["source"]), failure,
            "integrity", "Execution failure source is malformed")
    _expect(isinstance(value.get("message"), str) and len(value["message"]) <= 2000,
            failure, "integrity", "Execution failure message is malformed")
    retry_after = value.get("retry_after_seconds")
    _expect(retry_after is None or (type(retry_after) in (int, float)
                                    and math.isfinite(float(retry_after))
                                    and retry_after >= 0), failure, "integrity",
            "Execution failure retry delay is malformed")
    _expect(type(value.get("technical_impossibility")) is bool, failure, "integrity",
            "Execution failure technical flag is malformed")
    if "reported_codes" in value:
        codes = value["reported_codes"]
        _expect(isinstance(codes, list) and len(codes) <= 30
                and all(isinstance(item, str) and bool(item) for item in codes),
                failure, "integrity", "Execution failure reported codes are malformed")
    if "reported_statuses" in value:
        statuses = value["reported_statuses"]
        _expect(isinstance(statuses, list) and len(statuses) <= 30
                and all(type(item) is int for item in statuses),
                failure, "integrity", "Execution failure reported statuses are malformed")
    if "details" in value:
        try:
            canonical(value["details"])
        except (TypeError, ValueError, OverflowError, RecursionError):
            _reject(failure, "integrity", "Execution failure details are not JSON data")


def validate_execution_record_consistency(run_body: dict[str, Any], run_result: dict[str, Any],
                                          receipt_body: dict[str, Any],
                                          failure: Failure | None = None) -> None:
    """Compare immutable run and receipt execution identity.

    A real failed observation remains valid here.  Callers that require a
    successful implementation must reject its non-null ``failure`` or failed
    result after this identity check.
    """
    _expect(type(run_body) is dict and type(run_result) is dict and type(receipt_body) is dict,
            failure, "integrity", "Execution record consistency inputs are malformed")
    _expect({"argv", "snapshot", "input_digest", "environment", "simulated"} <= set(run_body),
            failure, "integrity", "Implementation run body omits execution identity")
    run_snapshot = _sha(run_body.get("snapshot"), "run.body.snapshot", failure)
    run_input = _sha(run_body.get("input_digest"), "run.body.input_digest", failure)
    argv = run_body.get("argv")
    _expect(isinstance(argv, list) and bool(argv)
            and all(isinstance(value, str) and bool(value) and "\x00" not in value for value in argv),
            failure, "integrity", "Implementation run argv is malformed")
    _expect(type(run_body.get("environment")) is dict and type(run_body.get("simulated")) is bool,
            failure, "integrity", "Implementation run execution metadata is malformed")
    required = {"snapshot", "input_digest", "command_digest", "result", "failure",
                "exit_code", "timed_out", "cancelled", "output_overflow", "environment",
                "stdout_blob", "stderr_blob", "process_started", "simulated"}
    _expect(required <= set(receipt_body), failure, "integrity",
            "Implementation receipt omits execution identity")
    receipt_snapshot = _sha(receipt_body.get("snapshot"), "receipt.snapshot", failure)
    receipt_input = _sha(receipt_body.get("input_digest"), "receipt.input_digest", failure)
    command_digest = _sha(receipt_body.get("command_digest"), "receipt.command_digest", failure)
    _expect(run_snapshot == receipt_snapshot, failure, "integrity",
            "Run and receipt input snapshots differ")
    _expect(run_input == receipt_input, failure, "integrity",
            "Run and receipt input digests differ")
    _expect(digest(argv) == command_digest, failure, "integrity",
            "Run argv differs from receipt command digest")
    _expect(type(receipt_body.get("environment")) is dict
            and receipt_body.get("environment") == run_body.get("environment"), failure,
            "integrity", "Run and receipt environment metadata differ")
    _expect(type(receipt_body.get("simulated")) is bool
            and receipt_body.get("simulated") == run_body.get("simulated"), failure,
            "integrity", "Run and receipt simulation flags differ")
    for key in ("timed_out", "cancelled", "output_overflow"):
        _expect(type(receipt_body.get(key)) is bool, failure, "integrity",
                f"Receipt {key} flag is malformed")
    _expect(type(receipt_body.get("exit_code")) is int
            and not isinstance(receipt_body.get("exit_code"), bool), failure,
            "integrity", "Receipt exit code is malformed")
    _expect(type(receipt_body.get("process_started")) is bool, failure, "integrity",
            "Receipt process_started flag is malformed")
    _validate_failure_record(receipt_body.get("failure"), failure)
    observed_result = receipt_body.get("result")
    _expect(type(observed_result) is dict, failure, "integrity",
            "Candidate implementation receipt result is malformed")
    try:
        same_result = canonical(run_result) == canonical(observed_result)
    except (TypeError, ValueError, OverflowError, RecursionError):
        _reject(failure, "integrity", "Execution results are not JSON data")
    _expect(same_result, failure, "integrity", "Run and receipt results differ")

@dataclass(frozen=True)
class ExecutionRecord:
    """A verified relation between one run row and its observed receipt."""

    run: dict[str, Any]
    receipt: dict[str, Any]
    result: dict[str, Any]


def _same_stored_json(raw: Any, value: Any, name: str) -> None:
    """Compare a parsed value with the immutable representation supplied by a row."""
    _expect(raw is not None, None, "integrity", f"{name} is missing")
    try:
        parsed = parse_json(raw) if isinstance(raw, (str, bytes)) else raw
        same = canonical(parsed) == canonical(value)
    except (Fault, TypeError, ValueError, OverflowError, RecursionError) as exc:
        _reject(None, "integrity", f"{name} is not valid JSON", str(exc))
    _expect(same, None, "integrity", f"{name} differs from its parsed execution record")


def _identity_text(record: dict[str, Any], field: str, label: str) -> str:
    """Require a producer identity field instead of treating absent as null."""
    _expect(field in record, None, "integrity", f"{label}.{field} is missing")
    value = record[field]
    _expect(type(value) is str and bool(value) and "\x00" not in value, None,
            "integrity", f"{label}.{field} must be a nonempty string")
    return value


def _identity_task(record: dict[str, Any], label: str) -> str | None:
    """Require the task key while retaining legitimate taskless observations."""
    _expect("task" in record, None, "integrity", f"{label}.task is missing")
    value = record["task"]
    _expect(value is None or (type(value) is str and bool(value) and "\x00" not in value),
            None, "integrity", f"{label}.task must be null or a nonempty string")
    return value


def _identity_epoch(record: dict[str, Any], task: str | None, label: str) -> int | None:
    """Validate the nullable task epoch without accepting bool or negatives."""
    _expect("epoch" in record, None, "integrity", f"{label}.epoch is missing")
    value = record["epoch"]
    if value is None:
        _expect(task is None, None, "integrity",
                f"{label}.epoch is null for a task-bound execution")
        return None
    _expect(type(value) is int and value >= 0, None, "integrity",
            f"{label}.epoch must be a nonnegative integer")
    _expect(task is not None, None, "integrity",
            f"{label}.epoch cannot bind a taskless execution")
    return value


def execution_record_consistency(run_row: dict[str, Any], run_body: dict[str, Any],
                                 run_result: dict[str, Any], receipt_row: dict[str, Any],
                                 observed_body: dict[str, Any]) -> ExecutionRecord:
    """Validate row identity and body identity without judging execution success.

    Live callers verify the receipt signature and blob references before this
    pure check; archive callers verify their stored checksums first.  The
    returned failed observation is still an observation.  Consumers decide
    whether its result satisfies their own success gate.
    """
    _expect(type(run_row) is dict and type(run_body) is dict and type(run_result) is dict
            and type(receipt_row) is dict and type(observed_body) is dict,
            None, "integrity", "Execution record consistency inputs are malformed")

    run_id = _identity_text(run_row, "id", "run row")
    run_project = _identity_text(run_row, "project", "run row")
    run_subject = _identity_text(run_row, "subject", "run row")
    run_role = _identity_text(run_row, "role", "run row")
    run_binding = _identity_text(run_row, "binding", "run row")
    run_task = _identity_task(run_row, "run row")
    run_epoch = _identity_epoch(run_row, run_task, "run row")

    receipt_id = _identity_text(receipt_row, "id", "receipt row")
    receipt_run = _identity_text(receipt_row, "run", "receipt row")
    receipt_project = _identity_text(receipt_row, "project", "receipt row")
    receipt_subject = _identity_text(receipt_row, "subject", "receipt row")
    receipt_role = _identity_text(receipt_row, "role", "receipt row")
    receipt_binding = _identity_text(receipt_row, "binding", "receipt row")

    observed_id = _identity_text(observed_body, "id", "receipt body")
    observed_run = _identity_text(observed_body, "run", "receipt body")
    observed_project = _identity_text(observed_body, "project", "receipt body")
    observed_subject = _identity_text(observed_body, "subject", "receipt body")
    observed_role = _identity_text(observed_body, "role", "receipt body")
    observed_binding = _identity_text(observed_body, "binding", "receipt body")
    observed_task = _identity_task(observed_body, "receipt body")
    observed_epoch = _identity_epoch(observed_body, observed_task, "receipt body")

    _same_stored_json(run_row.get("body"), run_body, "run body")
    _same_stored_json(run_row.get("result"), run_result, "run result")
    _same_stored_json(receipt_row.get("body"), observed_body, "receipt body")

    _expect("status" in run_row and run_row["status"] == "finished", None, "integrity",
            "Execution run is not finished")
    _expect(receipt_run == run_id == observed_run, None, "integrity",
            "Run and receipt references differ")
    for field in ("project", "subject", "role", "binding"):
        values = {
            "project": (run_project, receipt_project, observed_project),
            "subject": (run_subject, receipt_subject, observed_subject),
            "role": (run_role, receipt_role, observed_role),
            "binding": (run_binding, receipt_binding, observed_binding),
        }[field]
        _expect(values[0] == values[1] == values[2],
                None, "integrity", f"Execution {field} identity differs")
    _expect(run_task == observed_task, None, "integrity",
            "Execution task identity differs")
    _expect(run_epoch == observed_epoch, None, "integrity",
            "Execution epoch differs between run and receipt")
    _expect(receipt_id == observed_id, None, "integrity",
            "Receipt ID differs from its observed body")

    validate_execution_record_consistency(run_body, run_result, observed_body)
    return ExecutionRecord(run=run_row, receipt=observed_body,
                           result=observed_body["result"])


__all__ = ("ExecutionRecord", "execution_record_consistency",
           "validate_execution_record_consistency")
