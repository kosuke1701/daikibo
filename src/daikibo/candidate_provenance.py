"""Shared, read-only provenance checks for retained implementation candidates.

The live resolver and both historical archive readers have different storage
backends, but a candidate pin has one identity contract.  This module keeps
that contract independent of SQL, signing keys, workflow transitions, and
currentness.  Adapters expose only the small :class:`PinnedContext` surface.
"""
from __future__ import annotations

import copy
import re
import stat
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

from .common import Fault, canonical, digest, parse_json, timestamp
from .execution_record import execution_record_consistency, validate_execution_record_consistency
from .governance import implementation_observation_success


PYTHON_ADAPTER = "python-ast-v1"
PYTHON_AST_V1_DIGEST = "d6568b82d89e8a96958b81a5895816751f54c3685d5d532e919df8b26b85b5e9"
SNAPSHOT_FORMAT = "snapshot.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Candidate identity is deliberately a smaller wire object than the old
# Python symbol reference.  E2 and future adapters can resolve this exact
# shape without inventing a path, language, or dummy AST member.
_GENERIC_CANDIDATE_FIELDS = frozenset({
    "kind", "project", "candidate", "task", "task_revision",
    "candidate_digest", "snapshot_digest",
})
_SYMBOL_CANDIDATE_FIELDS = frozenset({
    "ref_type", "candidate", "task", "task_revision", "candidate_digest",
    "snapshot_digest", "repository", "path", "sha256", "mode", "adapter",
    "adapter_digest", "qualified_name", "kind", "ordinal", "start_byte",
    "end_byte", "span_sha256", "signature_hash",
})

class PinnedContext:
    """Finite read-only access to the records needed by a candidate pin.

    Implementations deliberately expose fixed semantic kinds instead of SQL
    or arbitrary table names.  A context is a snapshot: methods must not
    publish, refresh, or infer a missing record.
    """

    KINDS = frozenset({"candidate", "task", "run", "receipt", "repository"})

    def row(self, kind: str, ident: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def task_history(self, task: str) -> Iterable[dict[str, Any]]:
        raise NotImplementedError

    def blob(self, sha256: str) -> bytes | None:
        raise NotImplementedError

    def receipt_body(self, receipt: str) -> dict[str, Any] | None:
        raise NotImplementedError


# This sentinel is intentionally private to the controller-side factory.  A
# JSON/dict-shaped value cannot carry it, so the pre-adoption resolver cannot
# be turned into a caller-authored evidence path by copying the wire payload.
_PREADOPTION_ORIGIN = object()


def _json_copy(value: Any, name: str) -> Any:
    try:
        return parse_json(canonical(value))
    except (Fault, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Fault("invalid_preadoption", f"{name} is not canonical JSON") from exc


class _PreAdoptionObservation:
    """Runtime-only, sealed material immediately before candidate adoption.

    The object is deliberately not a public report or a candidate reference.
    It preserves the controller and caller identities alongside a canonical
    copy of the actual collector material; the readonly adapter verifies the
    seal again before it reads the store.
    """

    __slots__ = ("_control", "_actor", "_origin", "_payload", "_seal")

    def __init__(self, payload: dict[str, Any], *, control: Any, actor: Any,
                 _origin: object | None = None) -> None:
        if _origin is not _PREADOPTION_ORIGIN:
            raise Fault("invalid_preadoption", "pre-adoption observations can only come from Runtime")
        if type(payload) is not dict:
            raise Fault("invalid_preadoption", "pre-adoption observation payload must be an object")
        copied = _json_copy(payload, "pre-adoption observation")
        object.__setattr__(self, "_control", control)
        object.__setattr__(self, "_actor", actor)
        object.__setattr__(self, "_origin", _PREADOPTION_ORIGIN)
        object.__setattr__(self, "_payload", copied)
        object.__setattr__(self, "_seal", canonical(copied))

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("pre-adoption observation metadata is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__slots__:
            raise AttributeError("pre-adoption observation metadata is immutable")
        object.__delattr__(self, name)

    @property
    def payload(self) -> dict[str, Any]:
        # Expose the sealed material only through this private convenience
        # view.  Mutating it is detectable because the adapter recomputes the
        # canonical seal before reading any store row.
        return self._payload

    def __deepcopy__(self, memo: dict[int, Any]) -> "_PreAdoptionObservation":
        copied = type(self).__new__(type(self))
        memo[id(self)] = copied
        object.__setattr__(copied, "_control", self._control)
        object.__setattr__(copied, "_actor", self._actor)
        object.__setattr__(copied, "_origin", self._origin)
        object.__setattr__(copied, "_payload", copy.deepcopy(self._payload, memo))
        object.__setattr__(copied, "_seal", self._seal)
        return copied


class _PreAdoptionIdentity(dict):
    """Immutable-looking mapping returned by the private readonly adapter."""

    __slots__ = ("_control", "_actor", "_origin", "_seal")

    def __init__(self, value: dict[str, Any], *, control: Any, actor: Any) -> None:
        value = _json_copy(value, "pre-adoption identity")
        dict.__init__(self, value)
        object.__setattr__(self, "_control", control)
        object.__setattr__(self, "_actor", actor)
        object.__setattr__(self, "_origin", _PREADOPTION_ORIGIN)
        object.__setattr__(self, "_seal", canonical(dict(self)))

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("pre-adoption identity metadata is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__slots__:
            raise AttributeError("pre-adoption identity metadata is immutable")
        object.__delattr__(self, name)

    def __deepcopy__(self, memo: dict[int, Any]) -> "_PreAdoptionIdentity":
        copied = type(self).__new__(type(self))
        memo[id(self)] = copied
        dict.__init__(copied, copy.deepcopy(dict(self), memo))
        object.__setattr__(copied, "_control", self._control)
        object.__setattr__(copied, "_actor", self._actor)
        object.__setattr__(copied, "_origin", self._origin)
        object.__setattr__(copied, "_seal", self._seal)
        return copied


Failure = Callable[[str, str, Any], None]
CandidateRef = dict[str, Any]
CandidateIdentity = dict[str, Any]


def _reject(failure: Failure | None, kind: str, message: str, details: Any = None) -> None:
    """Raise through an adapter while retaining a shared semantic reason."""
    if failure is not None:
        failure(kind, message, details)
    codes = {
        "invalid": "invalid_reference",
        "missing": "unknown_reference",
        "cross_project": "cross_project",
        "stale": "stale_reference",
        "ambiguous": "ambiguous_reference",
        "unresolved": "unresolved_reference",
        "integrity": "integrity_error",
    }
    raise Fault(codes.get(kind, "integrity_error"), message, details)


def _expect(condition: Any, failure: Failure | None, kind: str, message: str,
            details: Any = None) -> None:
    if not condition:
        _reject(failure, kind, message, details)


def _body(row: dict[str, Any], field: str = "body", failure: Failure | None = None) -> dict[str, Any]:
    value = row.get(field)
    if isinstance(value, str):
        try:
            value = parse_json(value)
        except Fault as exc:
            _reject(failure, "integrity", f"Stored {field} is not valid JSON", row.get("id"))
    _expect(isinstance(value, dict), failure, "integrity",
            f"Stored {field} is not an object", row.get("id"))
    return value


def _sha(value: Any, name: str, failure: Failure | None = None) -> str:
    _expect(isinstance(value, str) and _SHA256.fullmatch(value), failure, "invalid",
            f"{name} must be a lowercase SHA-256 digest", value)
    return value


def _text(value: Any, name: str, failure: Failure | None = None) -> str:
    _expect(isinstance(value, str) and bool(value) and "\x00" not in value,
            failure, "invalid", f"{name} must be a nonempty string", value)
    return value


def _exact_fields(value: Any, fields: frozenset[str], failure: Failure | None,
                  name: str) -> dict[str, Any]:
    _expect(type(value) is dict, failure, "invalid", f"{name} must be an object")
    _expect(set(value) == set(fields), failure, "invalid",
            f"{name} fields are not the exact frozen shape", {
                "missing": sorted(set(fields) - set(value)),
                "unknown": sorted(set(value) - set(fields)),
            })
    return value



def _row(context: PinnedContext, kind: str, ident: str, failure: Failure | None = None) -> dict[str, Any]:
    _expect(kind in PinnedContext.KINDS, failure, "invalid", "Unknown pinned context kind", kind)
    try:
        value = context.row(kind, ident)
    except Fault as exc:
        if exc.code in {"not_found", "missing_evidence", "unknown_reference"}:
            _reject(failure, "missing", f"Pinned {kind} is missing", ident)
        _reject(failure, "integrity", f"Pinned {kind} could not be read", ident)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        _reject(failure, "integrity", f"Pinned {kind} could not be read", ident)
    _expect(isinstance(value, dict), failure, "missing", f"Pinned {kind} is missing", ident)
    return value


def _read_blob(context: PinnedContext, reference: str, failure: Failure | None = None) -> bytes:
    try:
        value = context.blob(reference)
    except Fault as exc:
        if exc.code in {"not_found", "missing_evidence", "unknown_reference"}:
            _reject(failure, "missing", "Required candidate CAS object is missing", reference)
        _reject(failure, "integrity", "Required candidate CAS object could not be read", reference)
    except (KeyError, OSError, TypeError, ValueError):
        _reject(failure, "integrity", "Required candidate CAS object could not be read", reference)
    _expect(isinstance(value, bytes), failure, "missing", "Required candidate CAS object is missing", reference)
    _expect(digest(value) == reference, failure, "integrity", "Candidate CAS object digest differs", reference)
    return value


def _path(value: Any, failure: Failure | None = None) -> str:
    _expect(isinstance(value, str) and bool(value) and "\x00" not in value and "\\" not in value,
            failure, "invalid", "Candidate path must be a repository-relative POSIX path", value)
    try:
        parsed = PurePosixPath(value)
    except (TypeError, ValueError):
        _reject(failure, "invalid", "Candidate path is not a POSIX path", value)
    _expect(not parsed.is_absolute() and value not in {".", ".."}
            and all(part not in {"", ".", ".."} for part in parsed.parts)
            and str(parsed) == value and ".git" not in parsed.parts
            and all(not part.startswith(".daikibo-control") for part in parsed.parts),
            failure, "invalid", "Candidate path must be normalized and repository-relative", value)
    return value


def _decode_result(row: dict[str, Any], failure: Failure | None = None) -> Any:
    value = row.get("result")
    if isinstance(value, str):
        try:
            value = parse_json(value)
        except Fault:
            _reject(failure, "integrity", "Stored run result is not valid JSON", row.get("id"))
    return value


def _check_row_checksum(row: dict[str, Any], body: dict[str, Any], field: str,
                        checksum: str, failure: Failure | None = None) -> None:
    expected = row.get(checksum)
    if expected is None:
        return
    _expect(isinstance(expected, str) and expected == digest(body), failure, "integrity",
            f"Stored {field} checksum differs", row.get("id"))


def _receipt_blob_refs(body: dict[str, Any], failure: Failure | None = None) -> list[str]:
    """Return durable receipt leaves without traversing private environment data."""
    refs: list[str] = []

    def add(value: Any, name: str) -> None:
        if value is None:
            return
        _expect(isinstance(value, str) and _SHA256.fullmatch(value), failure, "integrity",
                f"Receipt {name} is not a SHA-256 CAS reference", value)
        if value not in refs:
            refs.append(value)

    for key in ("input_digest", "input_blob", "stdout_blob", "stderr_blob"):
        add(body.get(key), key)
    result = body.get("result")
    if isinstance(result, dict):
        add(result.get("report_blob"), "result.report_blob")
        outputs = result.get("build_outputs", [])
        inputs = result.get("build_inputs", [])
        _expect(isinstance(outputs, list), failure, "integrity",
                "Receipt result build_outputs is malformed")
        _expect(isinstance(inputs, list), failure, "integrity",
                "Receipt result build_inputs is malformed")
        for output in outputs + inputs:
            _expect(isinstance(output, dict), failure, "integrity",
                    "Receipt build output is malformed")
            _expect("blob" in output, failure, "integrity",
                    "Receipt build output lacks its CAS reference")
            add(output.get("blob"), "build output blob")
    for product in (body.get("work_product"), body.get("partial_work")):
        if isinstance(product, dict):
            _expect("snapshot_blob" in product and "changes_blob" in product, failure, "integrity",
                    "Receipt work product CAS references are incomplete")
            add(product.get("snapshot_blob"), "work product snapshot_blob")
            add(product.get("changes_blob"), "work product changes_blob")
    return refs


def _safe_link_target(path: str, target: Any, failure: Failure | None = None) -> None:
    """Check a collector symlink target without following it.

    ``Snapshots.scan`` permits a relative target when the resolved link stays
    within the repository.  Reproduce that lexical boundary in the portable
    validator; no filesystem is consulted and no target is materialized.
    """
    _expect(isinstance(target, str) and bool(target) and "\x00" not in target
            and "\\" not in target and not PurePosixPath(target).is_absolute(),
            failure, "integrity", "Snapshot symlink target is unsafe", path)
    parts: list[str] = list(PurePosixPath(path).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            _expect(bool(parts), failure, "integrity",
                    "Snapshot symlink target escapes its repository", path)
            parts.pop()
        else:
            parts.append(part)


def _validate_snapshot(snapshot: Any, task_body: dict[str, Any], project: str,
                       context: PinnedContext, failure: Failure | None = None) -> dict[str, Any]:
    """Validate every entry of a runtime ``snapshot.v1`` and its CAS closure."""
    _expect(type(snapshot) is dict, failure, "integrity", "Candidate snapshot is malformed")
    _expect(set(snapshot) == {"format", "repos", "digest"}, failure, "integrity",
            "Candidate snapshot fields are malformed")
    snapshot_digest = _sha(snapshot.get("digest"), "snapshot.digest", failure)
    _expect(snapshot.get("format") == SNAPSHOT_FORMAT, failure, "integrity",
            "Candidate snapshot format is unsupported")
    unsigned = {key: value for key, value in snapshot.items() if key != "digest"}
    _expect(digest(unsigned) == snapshot_digest, failure, "integrity",
            "Candidate snapshot digest differs")

    declared = task_body.get("repos")
    _expect(isinstance(declared, list) and bool(declared)
            and all(isinstance(value, str) and bool(value) and "\x00" not in value for value in declared)
            and len(set(declared)) == len(declared), failure, "integrity",
            "Task repository scope is malformed")
    snapshot_repos = snapshot.get("repos")
    _expect(type(snapshot_repos) is dict and set(snapshot_repos) == set(declared), failure,
            "integrity", "Candidate snapshot repository scope differs")
    repositories: dict[str, dict[str, Any]] = {}
    for repository, repo_snapshot in snapshot_repos.items():
        _text(repository, "snapshot repository", failure)
        _expect(type(repo_snapshot) is dict
                and set(repo_snapshot) == {"name", "head", "files", "bytes", "unknown"},
                failure, "integrity", "Candidate snapshot repository is malformed", repository)
        name = _text(repo_snapshot.get("name"), "snapshot repository name", failure)
        head = repo_snapshot.get("head")
        _expect(head is None or (isinstance(head, str) and bool(re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head))),
                failure, "integrity", "Candidate snapshot repository head is malformed", repository)
        files = repo_snapshot.get("files")
        _expect(type(files) is dict, failure, "integrity",
                "Candidate snapshot repository files are malformed", repository)
        total = repo_snapshot.get("bytes")
        _expect(type(total) is int and total >= 0, failure, "integrity",
                "Candidate snapshot repository byte count is malformed", repository)
        unknown = repo_snapshot.get("unknown")
        _expect(isinstance(unknown, list), failure, "integrity",
                "Candidate snapshot unknown entries are malformed", repository)
        calculated = 0
        for path, entry in files.items():
            path = _path(path, failure)
            _expect(type(entry) is dict and isinstance(entry.get("kind"), str), failure,
                    "integrity", "Candidate snapshot file entry is malformed", path)
            kind = entry.get("kind")
            if kind == "file":
                _expect(set(entry) == {"kind", "blob", "mode", "size"}, failure,
                        "integrity", "Candidate snapshot regular file entry is malformed", path)
                blob = _sha(entry.get("blob"), "snapshot file blob", failure)
                mode = entry.get("mode")
                _expect(type(mode) is int and stat.S_ISREG(mode) and not stat.S_ISLNK(mode),
                        failure, "integrity", "Candidate snapshot file mode is malformed", path)
                size = entry.get("size")
                _expect(type(size) is int and size >= 0, failure, "integrity",
                        "Candidate snapshot file size is malformed", path)
                raw = _read_blob(context, blob, failure)
                _expect(len(raw) == size, failure, "integrity",
                        "Candidate snapshot file size differs from its CAS object", path)
                calculated += size
            elif kind == "symlink":
                _expect(set(entry) == {"kind", "target", "mode"}, failure,
                        "integrity", "Candidate snapshot symlink entry is malformed", path)
                _expect(entry.get("mode") == 0o120000, failure, "integrity",
                        "Candidate snapshot symlink mode is malformed", path)
                _safe_link_target(path, entry.get("target"), failure)
            else:
                _reject(failure, "integrity", "Candidate snapshot contains an unsupported file kind", {
                    "path": path, "kind": kind})
        _expect(calculated == total, failure, "integrity",
                "Candidate snapshot repository byte count differs", repository)
        repository_row = _row(context, "repository", repository, failure)
        _expect(repository_row.get("project") == project, failure, "cross_project",
                "Candidate snapshot repository belongs to another project", repository)
        _expect(repository_row.get("name") == name, failure, "integrity",
                "Candidate snapshot repository identity differs", repository)
        repositories[repository] = repository_row
    return {"digest": snapshot_digest, "repos": snapshot_repos, "rows": repositories}


def _validate_work_products(observed: dict[str, Any], candidate_snapshot: dict[str, Any],
                            changes: list[Any], context: PinnedContext,
                            failure: Failure | None = None) -> None:
    """Bind sealed candidate output to the observer's durable work product."""
    products = []
    for name in ("work_product", "partial_work"):
        value = observed.get(name)
        if value is None:
            continue
        _expect(type(value) is dict, failure, "integrity", f"Receipt {name} is malformed")
        products.append((name, value))
    if changes:
        _expect(isinstance(observed.get("work_product"), dict), failure, "integrity",
                "Successful candidate lacks its durable work product")
    for name, product in products:
        _expect(set({"snapshot_blob", "changes_blob", "changed_files", "adopted",
                     "requires_reassessment", "recorded_before_adoption"}) <= set(product),
                failure, "integrity", f"Receipt {name} fields are incomplete")
        snapshot_blob = _sha(product.get("snapshot_blob"), f"{name}.snapshot_blob", failure)
        changes_blob = _sha(product.get("changes_blob"), f"{name}.changes_blob", failure)
        snapshot_raw = _read_blob(context, snapshot_blob, failure)
        changes_raw = _read_blob(context, changes_blob, failure)
        try:
            saved_snapshot = parse_json(snapshot_raw)
            saved_changes = parse_json(changes_raw)
        except Fault:
            _reject(failure, "integrity", f"Receipt {name} work product is not valid JSON")
        _expect(saved_snapshot == candidate_snapshot, failure, "integrity",
                f"Receipt {name} snapshot differs from candidate output")
        _expect(saved_changes == changes, failure, "integrity",
                f"Receipt {name} changes differ from candidate output")
        _expect(type(product.get("changed_files")) is int and product["changed_files"] == len(changes),
                failure, "integrity", f"Receipt {name} changed-file count differs")
        for field in ("adopted", "requires_reassessment", "recorded_before_adoption"):
            _expect(type(product.get(field)) is bool, failure, "integrity",
                    f"Receipt {name} {field} flag is malformed")
        _expect(product.get("adopted") is False, failure, "integrity",
                f"Receipt {name} was marked adopted before the candidate boundary")
        _expect(product.get("recorded_before_adoption") is True, failure, "integrity",
                f"Receipt {name} was not recorded before the candidate boundary")
        _expect(product.get("requires_reassessment") is True, failure, "integrity",
                f"Receipt {name} does not retain its reassessment requirement")


def _resolve_execution_observation_state(
    *, project: str, task_id: str, task_revision: int, epoch: int,
    run_id: str, receipt_id: str,
    context: PinnedContext, failure: Failure | None = None,
    allow_historical_producer_revision: bool = False,
) -> dict[str, Any]:
    """Resolve one stored implementation observation without a candidate row.

    Candidate identity and pre-adoption identity use this same row/body/CAS
    closure.  Candidate-specific Task history ownership remains in the caller;
    this helper only proves that the retained implementation observation is the
    exact finished run and receipt named by its consumer.
    """
    run = _row(context, "run", run_id, failure)
    _expect(run.get("project") == project and run.get("task") == task_id
            and run.get("subject") == task_id and run.get("role") == "implementer"
            and run.get("epoch") == epoch, failure, "integrity",
            "Implementation run identity differs", run_id)
    _expect(run.get("status") == "finished", failure, "stale",
            "Implementation run is not finished", run_id)
    run_body = _body(run, failure=failure)
    _expect({"argv", "snapshot", "input_digest", "environment"} <= set(run_body), failure,
            "integrity", "Implementation run body omits execution identity", run_id)
    binding = _text(run.get("binding"), "run binding", failure)
    _check_row_checksum(run, run_body, "run body", "body_digest", failure)
    run_result = _decode_result(run, failure)
    _expect(type(run_result) is dict, failure, "integrity",
            "Stored run result is not an object", run_id)
    if run.get("result_digest") is not None:
        _expect(isinstance(run.get("result_digest"), str)
                and digest(run_result) == run.get("result_digest"), failure,
                "integrity", "Stored run result checksum differs", run_id)

    receipt = _row(context, "receipt", receipt_id, failure)
    _expect(receipt.get("run") == run_id and receipt.get("project") == project
            and receipt.get("subject") == task_id and receipt.get("role") == "implementer"
            and receipt.get("binding") == binding, failure, "integrity",
            "Implementation receipt identity differs", receipt_id)
    try:
        observed = context.receipt_body(receipt_id)
    except Fault as exc:
        if exc.code in {"not_found", "missing_evidence", "unknown_reference"}:
            _reject(failure, "missing", "Implementation evidence is unavailable", receipt_id)
        _reject(failure, "integrity", "Implementation evidence is invalid", receipt_id)
    _expect(isinstance(observed, dict), failure, "missing",
            "Implementation evidence is unavailable", receipt_id)
    _check_row_checksum(receipt, observed, "receipt body", "body_digest", failure)
    for key, expected in (("id", receipt_id), ("run", run_id), ("project", project),
                          ("task", task_id), ("subject", task_id), ("role", "implementer"),
                          ("binding", binding), ("epoch", epoch)):
        _expect(observed.get(key) == expected, failure, "integrity",
                "Implementation receipt identity differs", receipt_id)
    execution_record_consistency(run, run_body, run_result, receipt, observed)
    _expect(observed.get("snapshot") == run_body.get("snapshot")
            and observed.get("input_digest") == run_body.get("input_digest"), failure,
            "integrity", "Implementation input identity differs", run_id)

    state = {"project": project, "task_id": task_id, "candidate_epoch": epoch,
             "task_revision": task_revision, "run_id": run_id, "receipt_id": receipt_id,
             "run": run, "run_body": run_body, "receipt": receipt,
             "observed": observed}
    # artifact_provenance is the existing producer-metadata checker.  Keep
    # this import local because artifact_provenance itself consumes the
    # candidate resolver.
    metadata_state = state
    if allow_historical_producer_revision:
        control_metadata = run_body.get("execution_control")
        metadata_revision = (control_metadata.get("task_revision")
                             if isinstance(control_metadata, dict)
                             else task_revision)
        metadata_state = {**state, "task_revision": metadata_revision}
    try:
        from .artifact_provenance import _run_metadata
        producer = _run_metadata(metadata_state, code="integrity_error")
    except Fault as exc:
        _reject(failure, "integrity", "Implementation producer metadata is invalid", {
            "run": run_id, "reason": exc.message})
    state["producer_metadata"] = producer
    for blob in _receipt_blob_refs(observed, failure):
        _read_blob(context, blob, failure)
    return state


def _prompt_context(state: dict[str, Any], context: PinnedContext,
                    failure: Failure | None = None) -> dict[str, Any] | None:
    """Read the collector-owned prompt CAS when available.

    Historical observations may predate the optional input blob.  New Runtime
    observations retain it so the pre-adoption and post-candidate projections
    can prove the frozen Task and test-plan context without trusting a caller
    supplied plan digest.
    """
    run_body = state["run_body"]
    reference = run_body.get("input_blob")
    if reference is None:
        return None
    reference = _sha(reference, "run.body.input_blob", failure)
    raw = _read_blob(context, reference, failure)
    _expect(digest(raw) == reference, failure, "integrity",
            "Implementation input prompt CAS differs", state["run_id"])
    try:
        prompt = parse_json(raw)
    except Fault:
        _reject(failure, "integrity", "Implementation input prompt is not JSON", state["run_id"])
    _expect(type(prompt) is dict, failure, "integrity",
            "Implementation input prompt is not an object", state["run_id"])
    _expect(digest(prompt) == run_body.get("input_digest"), failure, "integrity",
            "Implementation input prompt digest differs", state["run_id"])
    observed = state["observed"]
    _expect(observed.get("input_blob") == reference, failure, "integrity",
            "Receipt input prompt reference differs", state["receipt_id"])
    return prompt


def _snapshot_dependencies(snapshot_info: dict[str, Any], observed: dict[str, Any],
                           failure: Failure | None = None) -> list[dict[str, Any]]:
    """Return the common CAS/repository projection used before and after INSERT."""
    values: list[dict[str, Any]] = []
    for repository, repo in sorted(snapshot_info["rows"].items()):
        values.append({"kind": "repository", "id": repository, "revision": None,
                       "digest": digest({"id": repository, "project": repo.get("project"),
                                          "name": repo.get("name")})})
        for entry in snapshot_info["repos"][repository]["files"].values():
            if entry.get("kind") == "file":
                values.append({"kind": "cas", "id": entry["blob"], "revision": None,
                               "digest": entry["blob"]})
    for reference in _receipt_blob_refs(observed, failure):
        values.append({"kind": "cas", "id": reference, "revision": None,
                       "digest": reference})
    unique = {(item["kind"], item["id"], item["revision"]): item for item in values}
    return [unique[key] for key in sorted(unique)]


_PREADOPTION_FIELDS = frozenset({
    "project", "task", "task_revision", "task_definition_digest", "epoch",
    "lease_owner", "binding", "local_claim", "task_start", "plan", "snapshot", "after",
    "changes", "prompt", "run_id", "receipt_id", "run_row", "receipt_row",
    "observed",
})


def _normalized_store_row(row: dict[str, Any]) -> dict[str, Any]:
    """Match the live pinned context's JSON row normalization."""
    value = dict(row)
    for key in ("body", "result"):
        if isinstance(value.get(key), str):
            value[key] = parse_json(value[key])
    if isinstance(value.get("body"), dict) and value.get("body_digest") is None:
        value["body_digest"] = digest(value["body"])
    if value.get("result") is not None and value.get("result_digest") is None:
        value["result_digest"] = digest(value["result"])
    return value


def _make_preadoption_observation(control: Any, actor: Any,
                                  payload: dict[str, Any]) -> _PreAdoptionObservation:
    """Capture actual Runtime material into the private handoff object.

    This factory is intentionally underscored and is called only from
    Runtime._execute_body.  It reads the already durable run/receipt rows so a
    caller cannot supply a different execution identity alongside the in-memory
    after/changes values.
    """
    if not isinstance(payload, dict):
        raise Fault("invalid_preadoption", "Runtime pre-adoption material must be an object")
    required = {"project", "task", "task_revision", "epoch", "lease_owner", "binding", "local_claim",
                "task_start", "plan", "snapshot", "after", "changes", "prompt",
                "observed"}
    if not required <= set(payload):
        raise Fault("invalid_preadoption", "Runtime pre-adoption material is incomplete",
                    {"missing": sorted(required - set(payload))})
    observed = payload["observed"]
    if not isinstance(observed, dict):
        raise Fault("invalid_preadoption", "Runtime observation is malformed")
    run_id, receipt_id = observed.get("run"), observed.get("id")
    if not (isinstance(run_id, str) and run_id and isinstance(receipt_id, str) and receipt_id):
        raise Fault("invalid_preadoption", "Runtime observation lacks run or receipt identity")
    run_row = control.s.one("SELECT * FROM runs WHERE id=?", (run_id,), True)
    receipt_row = control.s.one("SELECT * FROM receipts WHERE id=?", (receipt_id,), True)
    if run_row is None or receipt_row is None:
        raise Fault("invalid_preadoption", "Runtime observation rows are not durable")
    actual = dict(payload)
    actual["run_id"] = run_id
    actual["receipt_id"] = receipt_id
    actual["run_row"] = _normalized_store_row(run_row)
    actual["receipt_row"] = _normalized_store_row(receipt_row)
    # The signed reader is the only authority for receipt content.  Retain its
    # exact result in the sealed copy for the adapter's replacement check.
    actual["observed"] = control.g.receipt(receipt_id)
    actual["task_definition_digest"] = digest(payload["task_start"]["body"])
    actual["task_revision"] = payload["task_start"]["revision"]
    actual["epoch"] = payload["task_start"]["epoch"]
    actual["project"] = payload["task_start"]["project"]
    actual["task"] = payload["task_start"]["id"]
    actual["lease_owner"] = payload["task_start"]["lease_owner"]
    return _PreAdoptionObservation(actual, control=control, actor=actor,
                                   _origin=_PREADOPTION_ORIGIN)


def _verify_preadoption_origin(control: Any, actor: Any,
                               observation: Any) -> dict[str, Any]:
    if (type(observation) is not _PreAdoptionObservation
            or observation._control is not control
            or observation._actor is not actor
            or observation._origin is not _PREADOPTION_ORIGIN):
        raise Fault("invalid_preadoption", "Pre-adoption material must come from this Runtime controller")
    try:
        sealed = canonical(observation._payload)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Fault("invalid_preadoption", "Pre-adoption material is not canonical JSON") from exc
    if set(observation._payload) != _PREADOPTION_FIELDS:
        raise Fault("invalid_preadoption", "Pre-adoption material has an unexpected shape")
    if sealed != observation._seal:
        raise Fault("integrity_error", "Pre-adoption material was modified after collection")
    return observation._payload


def _verify_preadoption_identity(control: Any, actor: Any,
                                 identity: Any) -> dict[str, Any]:
    if (type(identity) is not _PreAdoptionIdentity
            or identity._control is not control
            or identity._actor is not actor
            or identity._origin is not _PREADOPTION_ORIGIN):
        raise Fault("invalid_preadoption", "Pre-adoption identity was not produced by this controller")
    if canonical(dict(identity)) != identity._seal:
        raise Fault("integrity_error", "Pre-adoption identity was modified after validation")
    return dict(identity)


def _live_pinned_context(control: Any) -> PinnedContext:
    # Import lazily to keep the shared pure resolver independent of the live
    # traceability composition root and to avoid an import cycle.
    from .traceability_refs import TraceabilityRefResolver, _LivePinnedContext
    return _LivePinnedContext(TraceabilityRefResolver(control))


def _task_project_row(control: Any, project: str) -> dict[str, Any]:
    row = control.s.one("SELECT paused FROM projects WHERE id=?", (project,), True)
    _expect(isinstance(row, dict), None, "missing", "Implementation project is unavailable", project)
    return row


def _preadoption_projection(state: dict[str, Any], *, output_snapshot: dict[str, Any],
                            changes: list[Any], plan_digest: str | None,
                            input_snapshot_digest: str | None,
                            task_definition_digest: str | None = None) -> dict[str, Any]:
    """Project immutable identity common to pre-adoption and candidate state."""
    metadata = state["producer_metadata"]
    return {
        "project": state["project"], "task": state["task_id"],
        "task_revision": state["task_revision"],
        "task_definition_digest": task_definition_digest,
        "epoch": state["candidate_epoch"], "run": state["run_id"],
        "run_digest": digest(state["run_body"]), "receipt": state["receipt_id"],
        "receipt_digest": digest(state["observed"]),
        "input_snapshot_digest": input_snapshot_digest,
        "plan_digest": plan_digest, "binding": state["run"].get("binding"),
        "output_snapshot_digest": output_snapshot["digest"],
        "changes_digest": digest(changes),
        "producer_actor": metadata["producer_actor"],
        "cas_dependencies": _snapshot_dependencies(output_snapshot, state["observed"]),
    }


def _preadoption_identity_from_payload(control: Any, actor: Any,
                                       payload: dict[str, Any], context: PinnedContext,
                                       *, failure: Failure | None = None) -> _PreAdoptionIdentity:
    project, task_id = payload["project"], payload["task"]
    task_revision, epoch = payload["task_revision"], payload["epoch"]
    task_start = payload["task_start"]
    _expect(type(task_start) is dict, failure, "integrity", "Pre-adoption Task snapshot is malformed")
    task = _row(context, "task", task_id, failure)
    _expect(task.get("id") == task_id and task.get("project") == project,
            failure, "cross_project", "Pre-adoption Task project identity differs", task_id)
    task_body = _body(task, failure=failure)
    _expect(task.get("revision") == task_revision
            and digest(task_body) == payload["task_definition_digest"]
            and task_body == task_start.get("body"), failure, "stale",
            "Task definition changed before pre-adoption", task_id)
    _expect(task.get("status") == "running" and task.get("candidate") is None
            and task.get("epoch") == epoch and not task.get("paused"), failure,
            "stale", "Task is not at the running pre-adoption boundary", task_id)
    _expect(task.get("lease_owner") == payload["lease_owner"], failure, "stale",
            "Task lease owner changed before pre-adoption", task_id)
    project_row = _task_project_row(control, project)
    _expect(not project_row.get("paused"), failure, "stale",
            "Project is paused before pre-adoption", project)

    plan = payload["plan"]
    _expect(type(plan) is dict and set(plan) == {"body", "digest"}
            and type(plan["body"]) is dict and digest(plan["body"]) == plan["digest"],
            failure, "integrity", "Frozen test plan material is malformed", task_id)
    plan_row = control.s.one("SELECT body,digest FROM plans WHERE task=?", (task_id,), True)
    _expect(plan_row is not None, failure, "missing", "Frozen test plan is unavailable", task_id)
    plan_body = parse_json(plan_row["body"])
    _expect(plan_row["digest"] == digest(plan_body) == plan["digest"]
            and plan_body == plan["body"], failure, "stale",
            "Frozen test plan changed before pre-adoption", task_id)

    current_binding = control.g.task_binding(task_id, ensure_policy=False)
    _expect(current_binding == payload["binding"], failure, "stale",
            "Task binding changed before pre-adoption", task_id)

    run_id, receipt_id = payload["run_id"], payload["receipt_id"]
    state = _resolve_execution_observation_state(
        project=project, task_id=task_id, task_revision=task_revision,
        epoch=epoch, run_id=run_id, receipt_id=receipt_id,
        context=context, failure=failure)
    _expect(state["run"] == payload["run_row"]
            and state["receipt"] == payload["receipt_row"]
            and state["observed"] == payload["observed"], failure, "integrity",
            "Stored execution rows differ from the Runtime handoff")
    _expect(state["observed"].get("binding") == payload["binding"], failure,
            "integrity", "Observed implementation binding differs")
    _expect(implementation_observation_success(state["observed"], control.g.mode),
            failure, "integrity", "Implementation observation is not successful in this mode")
    _expect(state["producer_metadata"]["producer_actor"] == payload["lease_owner"],
            failure, "integrity", "Implementation producer differs from the starting lease owner")
    _expect(state["producer_metadata"]["task_revision"] == task_revision
            and state["producer_metadata"]["epoch"] == epoch, failure, "integrity",
            "Implementation producer Task identity differs")

    prompt = _prompt_context(state, context, failure)
    _expect(prompt is not None and prompt == payload["prompt"], failure, "integrity",
            "Implementation prompt differs from the Runtime handoff")
    _expect(prompt.get("task") == task_body
            and prompt.get("test_plan") == {"body": plan_body, "digest": plan["digest"]},
            failure, "stale", "Implementation prompt is bound to a different Task or plan")

    input_snapshot = payload["snapshot"]
    after = payload["after"]
    changes = payload["changes"]
    input_info = _validate_snapshot(input_snapshot, task_body, project, context, failure)
    output_info = _validate_snapshot(after, task_body, project, context, failure)
    _expect(state["run_body"].get("snapshot") == input_info["digest"]
            and state["observed"].get("snapshot") == input_info["digest"], failure,
            "integrity", "Implementation input snapshot differs")
    _expect(isinstance(changes, list), failure, "integrity", "Implementation changes are malformed")
    _validate_work_products(state["observed"], after, changes, context, failure)
    _expect(output_info["digest"] == after.get("digest"), failure, "integrity",
            "Implementation output snapshot identity differs")

    # This read-only check deliberately does not extend the lease.  The writer
    # retains its final conditional UPDATE after this function returns.
    now = timestamp()
    _expect(task.get("lease_until") is not None and task.get("lease_until") > now,
            failure, "stale", "Implementation lease expired before pre-adoption")
    claimed = getattr(getattr(control.g, "local_executions", None), "claimed", None)
    current_claim = claimed(task_id, epoch) if claimed is not None else None
    expected_claim = payload.get("local_claim")
    if expected_claim is None:
        _expect(current_claim is None, failure, "stale",
                "A local execution claim appeared before pre-adoption", task_id)
    else:
        _expect(current_claim is not None
                and {"id": current_claim.get("id"), "digest": current_claim.get("digest")} == expected_claim,
                failure, "stale", "Local execution claim changed before pre-adoption", task_id)
        claim_body = parse_json(current_claim["body"])
        # Candidate provenance is a sealed read boundary.  Resolve the current
        # local authority through Governance's read primitive so this path
        # cannot re-enter a stage-enforcing readiness wrapper while a writer is
        # still before candidate adoption.
        readiness = control.g.execution_readiness_readonly(
            actor, task_id, "candidate", claim_body.get("certified_event"))
        _expect(readiness.get("allowed") is True, failure, "stale",
                "Local execution authorization changed before pre-adoption", task_id)

    projection = _preadoption_projection(
        state, output_snapshot=output_info, changes=changes,
        plan_digest=plan["digest"], input_snapshot_digest=input_info["digest"],
        task_definition_digest=payload["task_definition_digest"])
    return _PreAdoptionIdentity(projection, control=control, actor=actor)


def resolve_preadoption_observation(control: Any, actor: Any,
                                    observation: Any) -> _PreAdoptionIdentity:
    """Read-only Runtime boundary for the sealed pre-adoption observation."""
    payload = _verify_preadoption_origin(control, actor, observation)
    context = _live_pinned_context(control)
    return _preadoption_identity_from_payload(control, actor, payload, context)


def _candidate_task_definitions(task_row: dict[str, Any], history_rows: Iterable[dict[str, Any]],
                                candidate_id: str, failure: Failure | None = None) -> dict[int, dict[str, Any]]:
    """Validate every retained Task definition, then select ``candidate_id``.

    The chain is validated from the first retained immutable record.  A
    migration may intentionally retain only a contiguous suffix beginning at
    revision ``>1``; no earlier history is synthesized and an internal gap is
    still rejected.  Each selected revision retains its exact definition body
    as well as its digest so historical snapshot scope is validated against
    the historical definition.  All current and before/after bodies are
    compared by revision before candidate ownership is used for the result.
    """
    _expect(isinstance(task_row, dict), failure, "integrity", "Task context row is malformed")
    task_id = _text(task_row.get("id"), "task", failure)
    project = task_row.get("project")
    _expect(isinstance(project, str) and bool(project), failure, "integrity",
            "Task project identity is malformed", task_id)
    task_body = _body(task_row, failure=failure)
    if task_row.get("body_digest") is not None:
        _check_row_checksum(task_row, task_body, "Task body", "body_digest", failure)
    current_revision = task_row.get("revision")
    _expect(type(current_revision) is int and current_revision > 0, failure, "integrity",
            "Task revision is malformed", task_id)

    selected = [dict(row) for row in history_rows if isinstance(row, dict) and row.get("task") == task_id]
    selected.sort(key=lambda row: (
        row.get("to_revision") if type(row.get("to_revision")) is int else -1,
        row.get("id") if isinstance(row.get("id"), str) else ""))
    expected_from = None
    all_definitions: dict[int, dict[str, Any]] = {}
    candidate_revisions: set[int] = set()

    def collect_definition(side: dict[str, Any], source: str) -> None:
        """Collect the immutable definition before applying candidate selection.

        Candidate ownership is telemetry about a Task snapshot.  It must not
        decide which same-revision definitions are checked for consistency:
        adjacent history rows and the current row can carry ``candidate=None``
        while still proving the same definition revision.  Compare the
        canonical definition body for every side first, then retain only the
        revisions owned by the requested candidate below.
        """
        revision = side.get("revision")
        _expect(type(revision) is int and revision > 0, failure,
                "integrity", "Task revision definition revision is malformed", task_id)
        definition = side.get("body")
        _expect(isinstance(definition, dict), failure, "integrity",
                "Task revision history definition is malformed", task_id)
        identity = digest(definition)
        previous = all_definitions.get(revision)
        _expect(previous is None or previous["digest"] == identity, failure, "ambiguous",
                "Task revision history has conflicting definitions", {
                    "task": task_id, "revision": revision, "source": source})
        if previous is None:
            all_definitions[revision] = {"body": definition, "digest": identity}
        if side.get("candidate") == candidate_id:
            candidate_revisions.add(revision)

    # The existing immutable history validator is the source of the detailed
    # before/after snapshot rules.  Map its archive-specific Fault to the
    # adapter's semantic failure channel so live and archive use one chain.
    from .task_revisions import validate_history_record
    for record in selected:
        _text(record.get("id"), "task revision history id", failure)
        body = _body(record, failure=failure)
        normalized = dict(record); normalized["body"] = body
        try:
            validate_history_record(normalized, project)
        except (Fault, KeyError, TypeError, ValueError) as exc:
            _reject(failure, "integrity", "Task revision history is malformed", {
                "task": task_id, "history": record.get("id"),
                "reason": exc.message if isinstance(exc, Fault) else type(exc).__name__})
        from_revision = record.get("from_revision")
        _expect(type(from_revision) is int and from_revision >= 1, failure, "integrity",
                "Task revision history start revision is malformed", task_id)
        if expected_from is None:
            # The first retained record is the migration boundary.  Records
            # before it are unavailable evidence, not a reason to fabricate
            # synthetic definitions.
            expected_from = from_revision
        _expect(from_revision == expected_from, failure, "integrity",
                "Task revision history has an internal missing revision", task_id)
        expected_from = record.get("to_revision")
        for side_name in ("before", "after"):
            side = body.get(side_name, {}).get("task") if isinstance(body.get(side_name), dict) else None
            _expect(isinstance(side, dict), failure, "integrity",
                    "Task revision history task snapshot is malformed", task_id)
            _expect(side.get("id") == task_id and side.get("project") == project
                    and type(side.get("revision")) is int,
                    failure, "integrity", "Task revision history task identity differs", task_id)
            collect_definition(side, f"history:{record.get('id')}:{side_name}")

    # The current row is also part of the all-definition consistency check,
    # even when its candidate field is None after a later definition change.
    current_side = dict(task_row)
    current_side["body"] = task_body
    collect_definition(current_side, "current")

    definitions = {revision: all_definitions[revision]
                   for revision in sorted(candidate_revisions)}

    if task_row.get("candidate") != candidate_id:
        _expect(bool(selected), failure, "stale", "Candidate requires retained Task revision history",
                {"task": task_id, "candidate": candidate_id})
    if selected:
        _expect(expected_from == current_revision, failure, "integrity",
                "Task revision history does not reach the current revision", task_id)
    return definitions


def candidate_task_revisions(task_row: dict[str, Any], history_rows: Iterable[dict[str, Any]],
                             candidate_id: str, failure: Failure | None = None) -> dict[int, str]:
    """Return the retained candidate revision-to-digest projection.

    Keep this public compatibility API digest-only; the identity resolver uses
    the private body-preserving helper when it must validate an old snapshot.
    """
    definitions = _candidate_task_definitions(task_row, history_rows, candidate_id, failure)
    return {revision: value["digest"] for revision, value in definitions.items()}


def _generic_ref_fields(ref: dict[str, Any], failure: Failure | None = None) -> dict[str, Any]:
    _exact_fields(ref, _GENERIC_CANDIDATE_FIELDS, failure, "Candidate identity reference")
    _expect(ref.get("kind") == "candidate", failure, "invalid",
            "Candidate identity kind is malformed")
    project = _text(ref.get("project"), "project", failure)
    candidate_id = _text(ref.get("candidate"), "candidate", failure)
    task_id = _text(ref.get("task"), "task", failure)
    task_revision = ref.get("task_revision")
    _expect(type(task_revision) is int and task_revision > 0, failure, "invalid",
            "Candidate task revision is malformed", task_revision)
    candidate_digest = _sha(ref.get("candidate_digest"), "candidate_digest", failure)
    snapshot_digest = _sha(ref.get("snapshot_digest"), "snapshot_digest", failure)
    return {"project": project, "candidate_id": candidate_id, "task_id": task_id,
            "task_revision": task_revision, "candidate_digest": candidate_digest,
            "snapshot_digest": snapshot_digest}


def _resolve_candidate_identity_state(ref: dict[str, Any], context: PinnedContext,
                                     failure: Failure | None = None) -> dict[str, Any]:
    fields = _generic_ref_fields(ref, failure)
    project = fields["project"]
    candidate_id = fields["candidate_id"]
    task_id = fields["task_id"]
    task_revision = fields["task_revision"]
    candidate_digest = fields["candidate_digest"]
    snapshot_digest = fields["snapshot_digest"]

    candidate = _row(context, "candidate", candidate_id, failure)
    task = _row(context, "task", task_id, failure)
    if candidate.get("project") is not None:
        _expect(candidate.get("project") == project, failure, "cross_project",
                "Candidate belongs to another project", candidate_id)
    _expect(task.get("project") == project, failure, "cross_project",
            "Task belongs to another project", task_id)
    _expect(candidate.get("task") == task_id, failure, "stale",
            "Candidate is attached to another task", candidate_id)
    candidate_body = _body(candidate, failure=failure)
    _expect({"snapshot", "changes", "findings", "implementation_receipt"} <= set(candidate_body)
            <= {"snapshot", "changes", "findings", "implementation_receipt", "execution_authorization"},
            failure, "integrity", "Candidate body fields are malformed", candidate_id)
    candidate_epoch = candidate.get("epoch")
    _expect(type(candidate_epoch) is int and candidate_epoch >= 0, failure, "integrity",
            "Candidate epoch is malformed", candidate_id)
    stored_candidate_digest = candidate.get("digest")
    _expect(isinstance(stored_candidate_digest, str) and digest(candidate_body) == stored_candidate_digest
            and stored_candidate_digest == candidate_digest, failure, "integrity",
            "Candidate body digest differs", candidate_id)

    run_id = candidate.get("implementation_run")
    _expect(isinstance(run_id, str) and bool(run_id), failure, "integrity",
            "Candidate has no implementation run", candidate_id)
    receipt_id = candidate_body.get("implementation_receipt")
    _expect(isinstance(receipt_id, str) and bool(receipt_id), failure, "integrity",
            "Candidate has no implementation receipt", candidate_id)
    evidence = _resolve_execution_observation_state(
        project=project, task_id=task_id, task_revision=task_revision,
        epoch=candidate_epoch, run_id=run_id, receipt_id=receipt_id,
        context=context, failure=failure,
        allow_historical_producer_revision=True)
    run = evidence["run"]
    run_body = evidence["run_body"]
    receipt = evidence["receipt"]
    observed = evidence["observed"]
    _expect(implementation_observation_success(observed, None), failure, "integrity",
            "Candidate is bound to a failed implementation receipt", candidate_id)

    task_body = _body(task, failure=failure)
    definitions = _candidate_task_definitions(task, context.task_history(task_id), candidate_id, failure)
    selected_definition = definitions.get(task_revision)
    _expect(isinstance(selected_definition, dict), failure, "stale",
            "Candidate does not have the requested task revision history", {
            "candidate": candidate_id, "revision": task_revision})
    selected_task_body = selected_definition["body"]
    snapshot = candidate_body.get("snapshot")
    changes = candidate_body.get("changes")
    findings = candidate_body.get("findings")
    _expect(isinstance(changes, list) and isinstance(findings, list), failure, "integrity",
            "Candidate body lacks the runtime candidate fields", candidate_id)
    snapshot_info = _validate_snapshot(snapshot, selected_task_body, project, context, failure)
    _expect(snapshot_info["digest"] == snapshot_digest, failure, "integrity",
            "Candidate snapshot identity differs", candidate_id)
    _validate_work_products(observed, snapshot, changes, context, failure)

    return {**fields, "candidate": candidate, "task": task, "candidate_body": candidate_body,
            "candidate_epoch": candidate_epoch, "run": run, "run_body": run_body,
            "run_id": run_id, "receipt": receipt, "observed": observed,
            "producer_metadata": evidence["producer_metadata"],
            "receipt_id": receipt_id, "task_body": task_body, "snapshot": snapshot_info,
            "selected_task_body": selected_task_body, "snapshot_body": snapshot,
            "changes": changes, "findings": findings,
            "task_definition_digest": selected_definition["digest"]}


def _identity_dependencies(state: dict[str, Any]) -> list[dict[str, Any]]:
    dependencies = [
        {"kind": "task", "id": state["task_id"], "revision": state["task_revision"],
         "digest": state["task_definition_digest"]},
        {"kind": "candidate", "id": state["candidate_id"], "revision": state["candidate_epoch"],
         "digest": state["candidate_digest"]},
        {"kind": "implementation_run", "id": state["run_id"], "revision": state["candidate_epoch"],
         "digest": digest(state["run_body"])},
        {"kind": "implementation_receipt", "id": state["receipt_id"], "revision": state["candidate_epoch"],
         "digest": digest(state["observed"])},
        {"kind": "snapshot", "id": state["candidate_id"], "revision": state["task_revision"],
         "digest": state["snapshot_digest"]},
    ]
    for repository, repo in sorted(state["snapshot"]["rows"].items()):
        dependencies.append({"kind": "repository", "id": repository, "revision": None,
                             "digest": digest({"id": repository, "project": state["project"],
                                                "name": repo.get("name")})})
        for entry in state["snapshot"]["repos"][repository]["files"].values():
            if entry.get("kind") == "file":
                dependencies.append({"kind": "cas", "id": entry["blob"], "revision": None,
                                     "digest": entry["blob"]})
    return dependencies


def resolve_candidate_identity(ref: CandidateRef, context: PinnedContext,
                               failure: Failure | None = None) -> CandidateIdentity:
    """Resolve the exact generic CandidateRef used by the E2 assurance core."""
    state = _resolve_candidate_identity_state(ref, context, failure)
    return {"canonical_ref": dict(ref), "kind": "candidate", "project": state["project"],
            "candidate": state["candidate_id"], "task": state["task_id"],
            "task_revision": state["task_revision"],
            "candidate_digest": state["candidate_digest"],
            "snapshot_digest": state["snapshot_digest"],
            "task_definition_digest": state["task_definition_digest"],
            "dependency_refs": _identity_dependencies(state)}


def resolve_candidate_pin(project: str, ref: dict[str, Any], context: PinnedContext,
                          failure: Failure | None = None) -> dict[str, Any]:
    """Resolve the strict legacy ``candidate_symbol`` wrapper over generic identity."""
    _expect(isinstance(project, str) and bool(project), failure, "invalid",
            "Project identity is malformed")
    _exact_fields(ref, _SYMBOL_CANDIDATE_FIELDS, failure, "Candidate symbol reference")
    _expect(ref.get("ref_type") == "candidate_symbol", failure, "invalid",
            "Candidate reference is malformed")
    repository = _text(ref.get("repository"), "repository", failure)
    path = _path(ref.get("path"), failure)
    sha256 = _sha(ref.get("sha256"), "sha256", failure)
    mode = ref.get("mode")
    _expect(type(mode) is int and mode >= 0, failure, "invalid", "Candidate mode is malformed", mode)
    _expect(ref.get("adapter") == PYTHON_ADAPTER, failure, "unresolved",
            "Only python-ast-v1 candidate symbols are resolvable", ref.get("adapter"))
    _expect(ref.get("adapter_digest") == PYTHON_AST_V1_DIGEST, failure, "stale",
            "Candidate adapter digest is not the frozen python-ast-v1 implementation",
            ref.get("adapter_digest"))
    _text(ref.get("qualified_name"), "qualified_name", failure)
    kind = _text(ref.get("kind"), "kind", failure)
    _expect(kind in {"function", "async_function", "class"}, failure, "invalid",
            "Unsupported Python symbol kind", kind)
    ordinal = ref.get("ordinal")
    _expect(type(ordinal) is int and ordinal >= 0, failure, "invalid",
            "Candidate ordinal is malformed", ordinal)
    start, end = ref.get("start_byte"), ref.get("end_byte")
    _expect(type(start) is int and type(end) is int and start >= 0 and start < end,
            failure, "invalid", "Candidate symbol span endpoints are malformed")
    signature_hash = _sha(ref.get("signature_hash"), "signature_hash", failure)
    span_hash = _sha(ref.get("span_sha256"), "span_sha256", failure)

    generic_ref = {"kind": "candidate", "project": project,
                   "candidate": ref["candidate"], "task": ref["task"],
                   "task_revision": ref["task_revision"],
                   "candidate_digest": ref["candidate_digest"],
                   "snapshot_digest": ref["snapshot_digest"]}
    state = _resolve_candidate_identity_state(generic_ref, context, failure)
    _expect(repository in state["snapshot"]["repos"], failure, "stale",
            "Candidate repository is outside the task repository scope", repository)
    repo_snapshot = state["snapshot"]["repos"][repository]
    _expect(path in repo_snapshot["files"], failure, "missing",
            "Candidate snapshot does not contain the requested path", path)
    entry = repo_snapshot["files"][path]
    _expect(entry.get("kind") == "file" and entry.get("blob") == sha256
            and entry.get("mode") == mode, failure, "stale",
            "Candidate reference file identity differs from the snapshot", path)
    raw = _read_blob(context, sha256, failure)
    _expect(stat.S_ISREG(mode) and not stat.S_ISLNK(mode), failure, "unresolved",
            "Candidate symlink or non-regular entry is not a symbol", path)
    _expect(path.endswith((".py", ".pyi")), failure, "unresolved",
            "python-ast-v1 requires a Python source path", path)
    _expect(end <= len(raw) and digest(raw[start:end]) == span_hash, failure, "integrity",
            "Candidate symbol span differs from its snapshot bytes", path)
    content = {"blob": sha256, "byte_start": start, "byte_end": end,
               "sha256": digest(raw[start:end]), "bytes": end - start}
    dependencies = _identity_dependencies(state)
    dependencies.append({"kind": "adapter", "id": PYTHON_ADAPTER, "revision": "v1",
                         "digest": PYTHON_AST_V1_DIGEST})
    return {"canonical_ref": dict(ref), "task_definition_digest": state["task_definition_digest"],
            "dependency_refs": dependencies, "content": content}
