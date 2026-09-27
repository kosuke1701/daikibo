"""Portable observed execution context shared by direct and chunked exports.

The context is historical evidence only.  It is a finite projection of the
five runtime tables used by candidate provenance plus a verified CAS reader;
it never opens a controller home, repository, or network endpoint while an
archive is being inspected.
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Iterable

from .candidate_provenance import PinnedContext
from .common import Fault, canonical, digest, need, parse_json


CONTEXT_FORMAT = "daikibo.observed-context.v1"
CONTEXT_SECTIONS = ("tasks", "candidates", "runs", "receipts", "repos")
TRACEABILITY_SECTIONS = (
    "traceability_sets", "traceability_revisions", "traceability_items",
    "traceability_proposals", "traceability_decisions", "traceability_mappings",
    "traceability_bindings", "traceability_records",
)
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
CONTEXT_WIRE_FIELDS = frozenset({
    "format", "project", "historical_only", "runtime_restore_supported",
    "fresh_review_or_test_evidence", "tables", "blobs", "digest",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def context_queries() -> dict[str, str]:
    """Return the canonical project-scoped context queries.

    Candidates have no project column.  The owner Task join is therefore part
    of the query and the projected project column is part of the portable row.
    """
    return {
        "tasks": "SELECT * FROM tasks WHERE project=? ORDER BY id",
        "candidates": (
            "SELECT c.*, t.project AS project FROM candidates c "
            "JOIN tasks t ON t.id=c.task WHERE t.project=? ORDER BY c.id"
        ),
        "runs": "SELECT * FROM runs WHERE project=? ORDER BY id",
        "receipts": "SELECT * FROM receipts WHERE project=? ORDER BY id",
        "repos": "SELECT * FROM repos WHERE project=? ORDER BY id",
    }


def candidate_context_row(section: str, row: dict[str, Any]) -> dict[str, Any]:
    """Use the established provenance row normalization in one place."""
    # Import lazily: traceability imports this module through archive helpers
    # in some installed lanes, so importing it at module load would cycle.
    from .traceability import _candidate_context_row
    return _candidate_context_row(section, row)


def project_context_rows(store: Any, project: str) -> dict[str, list[dict[str, Any]]]:
    """Read the complete fixed five-section context for one project."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for section, sql in context_queries().items():
        rows[section] = [candidate_context_row(section, row)
                         for row in store.all(sql, (project,))]
    return rows


def _row_key(section: str, row: dict[str, Any]) -> str | None:
    value = row.get("id")
    if section == "repos":
        # Repositories use their ordinary primary key just like the other
        # context tables.  Keep this branch explicit for schema readers.
        value = row.get("id")
    return value if isinstance(value, str) and value else None


def validate_context_rows(rows: Any, project: str, *, code: str = "invalid_archive") -> dict[str, list[dict[str, Any]]]:
    """Validate and copy the fixed, project-scoped row population."""
    need(isinstance(rows, dict) and set(rows) == set(CONTEXT_SECTIONS), code,
         "Observed context tables must contain exactly the fixed sections")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for section in CONTEXT_SECTIONS:
        values = rows[section]
        need(isinstance(values, list), code,
             "Observed context section is not a list", section)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in values:
            need(isinstance(row, dict), code,
                 "Observed context row is not an object", section)
            value = dict(row)
            ident = _row_key(section, value)
            need(ident is not None and ident not in seen, code,
                 "Observed context row identifier is missing or duplicated",
                 {"section": section, "id": value.get("id")})
            need(value.get("project") == project, code,
                 "Observed context row crosses the project boundary", {
                     "section": section, "id": ident,
                 })
            seen.add(ident)
            # A row must remain canonical JSON.  This also rejects sqlite
            # values and accidental binary/secret handles before the wire is
            # hashed or encoded.
            try:
                canonical(value)
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise Fault(code, "Observed context row is not canonical JSON",
                            {"section": section, "id": ident}) from exc
            if section in {"tasks", "candidates", "runs", "receipts"}:
                body = value.get("body")
                if isinstance(body, str):
                    body = parse_json(body, limit=MAX_SNAPSHOT_BYTES)
                if isinstance(body, dict):
                    need(value.get("body_digest") == digest(body), code,
                         "Observed context body checksum differs", ident)
                run_result = value.get("result")
                if isinstance(run_result, str):
                    run_result = parse_json(run_result, limit=MAX_SNAPSHOT_BYTES)
                if section == "runs" and run_result is not None:
                    need(value.get("result_digest") == digest(run_result), code,
                         "Observed context run result checksum differs", ident)
            result.append(value)
        normalized[section] = result

    task_ids = {row["id"] for row in normalized["tasks"]}
    runs_by_id = {row["id"]: row for row in normalized["runs"]}
    run_ids = set(runs_by_id)
    for row in normalized["candidates"]:
        need(row.get("task") in task_ids, code,
             "Candidate context row references a missing Task", row.get("id"))
        run_id = row.get("implementation_run")
        need(run_id in run_ids, code,
             "Candidate context row references a missing implementation run",
             {"candidate": row.get("id"), "run": run_id})
        need(runs_by_id[run_id].get("task") == row.get("task"), code,
             "Candidate context implementation run belongs to another Task",
             {"candidate": row.get("id"), "run": run_id})
    for row in normalized["runs"]:
        task = row.get("task")
        if task is not None:
            need(task in task_ids, code,
                 "Run context row references a missing Task", row.get("id"))
    receipt_runs: set[str] = set()
    for row in normalized["receipts"]:
        run = row.get("run")
        if run is not None:
            need(run in run_ids, code,
                 "Receipt context row references a missing run", row.get("id"))
            need(run not in receipt_runs, code,
                 "Observed context has multiple receipts for one run", run)
            receipt_runs.add(run)
    # This is intentionally a typed relationship check, not a generic scan of
    # arbitrary 64-hex values.  Candidate body references are checked by the
    # shared provenance resolver when an assurance object actually uses them.
    return normalized


def _strict_sha(value: Any, name: str, *, code: str) -> str:
    need(isinstance(value, str) and _SHA256.fullmatch(value) is not None,
         code, f"{name} is not a lowercase SHA-256", value)
    return value


def _decode_blob_descriptor(row: Any, *, code: str = "invalid_archive") -> tuple[str, bytes]:
    need(isinstance(row, dict) and set(row) == {"sha256", "bytes", "encoding", "data"},
         code, "Observed context CAS descriptor has an unexpected shape")
    ident = _strict_sha(row.get("sha256"), "Observed context CAS digest", code=code)
    size = row.get("bytes")
    need(type(size) is int and 0 <= size <= MAX_SNAPSHOT_BYTES, code,
         "Observed context CAS byte count is malformed", ident)
    need(row.get("encoding") == "base64", code,
         "Observed context CAS encoding is unsupported", ident)
    encoded = row.get("data")
    need(isinstance(encoded, str), code,
         "Observed context CAS data is malformed", ident)
    try:
        encoded_size = len(encoded.encode("ascii"))
    except UnicodeEncodeError as exc:
        raise Fault(code, "Observed context CAS data is not ASCII", ident) from exc
    expected_encoded_size = 4 * ((size + 2) // 3)
    need(encoded_size == expected_encoded_size, code,
         "Observed context CAS base64 length differs from its byte count", {
             "sha256": ident, "bytes": size,
             "encoded_bytes": encoded_size, "expected_encoded_bytes": expected_encoded_size,
         })
    # Validate both alphabet/padding and canonical representation.  This
    # rejects alternate spellings and whitespace while keeping the wire
    # deterministic.
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise Fault(code, "Observed context CAS data is not strict base64", ident) from exc
    need(base64.b64encode(raw).decode("ascii") == encoded, code,
         "Observed context CAS base64 is not canonical", ident)
    need(len(raw) == size and digest(raw) == ident, code,
         "Observed context CAS bytes do not match their digest", ident)
    return ident, raw


class PortableObservedContext(PinnedContext):
    """Finite reader used by direct and chunked historical validators."""

    _SECTIONS = {
        "candidate": "candidates",
        "task": "tasks",
        "run": "runs",
        "receipt": "receipts",
        "repository": "repos",
    }

    def __init__(self, rows: dict[str, list[dict[str, Any]]], blob_reader: Any,
                 *, project: str | None = None,
                 extra_rows: dict[str, list[dict[str, Any]]] | None = None,
                 code: str = "invalid_archive") -> None:
        if project is None:
            project = next((row.get("project") for values in rows.values()
                            for row in values if isinstance(row, dict)
                            and isinstance(row.get("project"), str)), None)
        need(isinstance(project, str) and project, code,
             "Observed context project is missing")
        self.project = project
        self.code = code
        self.rows = validate_context_rows(rows, project, code=code)
        self.blob_reader = blob_reader
        self.blob_ids = set(blob_reader) if isinstance(blob_reader, dict) else set()
        self.context_rows: dict[str, list[dict[str, Any]]] = {
            section: list(values) for section, values in self.rows.items()
        }
        if extra_rows:
            for section, values in extra_rows.items():
                need(isinstance(section, str) and section not in self.context_rows,
                     code, "Observed context has a duplicate external section", section)
                need(isinstance(values, list), code,
                     "Observed context external section is not a list", section)
                copied = []
                for value in values:
                    need(isinstance(value, dict), code,
                         "Observed context external row is malformed", section)
                    row = dict(value)
                    try:
                        canonical(row)
                    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                        raise Fault(code, "Observed context external row is not canonical JSON",
                                    section) from exc
                    if section == "revisions":
                        need("project" not in row or row.get("project") == project, code,
                             "Observed context external row crosses the project boundary",
                             {"section": section, "id": row.get("id")})
                    else:
                        need(row.get("project") == project, code,
                             "Observed context external row crosses the project boundary",
                             {"section": section, "id": row.get("id")})
                    copied.append(row)
                self.context_rows[section] = copied
        self._indexes: dict[str, dict[str, dict[str, Any]]] = {}
        for section, values in self.context_rows.items():
            index: dict[str, dict[str, Any]] = {}
            for value in values:
                need(isinstance(value, dict), code,
                     "Observed context external row is malformed", section)
                ident = value.get("id")
                if section == "revisions" and ident is None:
                    ident = canonical([value.get("artifact"), value.get("revision")]).decode()
                need(isinstance(ident, str) and ident not in index, code,
                     "Observed context external row is missing or duplicated", section)
                if value.get("project") is not None:
                    need(value.get("project") == project, code,
                         "Observed context external row crosses the project boundary", ident)
                index[ident] = value
            self._indexes[section] = index
        for row in self.context_rows.get("revisions", []):
            artifact = self._indexes.get("artifacts", {}).get(row.get("artifact"))
            need(artifact is not None and artifact.get("project") == project, code,
                 "Observed revision context references a missing or foreign artifact",
                 row.get("artifact"))

    @classmethod
    def from_wire(cls, wire: Any, *, project: str,
                  extra_rows: dict[str, list[dict[str, Any]]] | None = None,
                  code: str = "invalid_archive") -> "PortableObservedContext":
        need(isinstance(wire, dict) and set(wire) == CONTEXT_WIRE_FIELDS, code,
             "Observed context envelope is malformed")
        need(wire.get("format") == CONTEXT_FORMAT and wire.get("project") == project,
             code, "Observed context format or project differs")
        need(wire.get("historical_only") is True and
             wire.get("runtime_restore_supported") is False and
             wire.get("fresh_review_or_test_evidence") is False, code,
             "Observed context has an invalid historical-only declaration")
        rows = validate_context_rows(wire.get("tables"), project, code=code)
        blobs = wire.get("blobs")
        need(isinstance(blobs, list), code, "Observed context CAS list is malformed")
        unsigned = dict(wire)
        unsigned.pop("digest")
        need(wire.get("digest") == digest(unsigned), code,
             "Observed context digest differs")
        decoded: dict[str, bytes] = {}
        descriptor_ids = []
        seen = set()
        encoded_total = 0
        decoded_total = 0
        previous = None
        for item in blobs:
            need(isinstance(item, dict), code, "Observed context CAS descriptor is malformed")
            # Check metadata and the cumulative encoded/decoded budgets before
            # allocating decoded blob bytes.  The outer specification limit is
            # an additional bound, not a substitute for these checks.
            need(set(item) == {"sha256", "bytes", "encoding", "data"}, code,
                 "Observed context CAS descriptor has an unexpected shape")
            ident = _strict_sha(item.get("sha256"), "Observed context CAS digest", code=code)
            size = item.get("bytes")
            need(type(size) is int and size >= 0, code,
                 "Observed context CAS byte count is malformed", ident)
            need(item.get("encoding") == "base64" and isinstance(item.get("data"), str), code,
                 "Observed context CAS encoding or data is malformed", ident)
            try:
                encoded_size = len(item["data"].encode("ascii"))
            except UnicodeEncodeError as exc:
                raise Fault(code, "Observed context CAS data is not ASCII", ident) from exc
            expected_encoded_size = 4 * ((size + 2) // 3)
            if size > MAX_SNAPSHOT_BYTES:
                raise Fault("snapshot_too_large", "Observed context CAS object exceeds the specification limit", {
                    "kind": "single_blob_bytes", "required": size,
                    "limit": MAX_SNAPSHOT_BYTES,
                })
            need(encoded_size == expected_encoded_size, code,
                 "Observed context CAS base64 length differs from its byte count", {
                     "sha256": ident, "bytes": size,
                     "encoded_bytes": encoded_size, "expected_encoded_bytes": expected_encoded_size,
                 })
            need(ident not in seen, code,
                 "Observed context CAS digest is duplicated", ident)
            need(previous is None or previous < ident, code,
                 "Observed context CAS descriptors are not sorted", ident)
            next_encoded = encoded_total + encoded_size
            next_decoded = decoded_total + size
            if next_encoded > MAX_SNAPSHOT_BYTES:
                raise Fault("snapshot_too_large", "Observed context encoded CAS exceeds the specification limit", {
                    "kind": "encoded_blob_bytes", "required": next_encoded,
                    "limit": MAX_SNAPSHOT_BYTES,
                })
            if next_decoded > MAX_SNAPSHOT_BYTES:
                raise Fault("snapshot_too_large", "Observed context decoded CAS exceeds the specification limit", {
                    "kind": "decoded_blob_bytes", "required": next_decoded,
                    "limit": MAX_SNAPSHOT_BYTES,
                })
            descriptor_ids.append(ident)
            seen.add(ident)
            previous = ident
            encoded_total = next_encoded
            decoded_total = next_decoded
        # Preflight every descriptor and the complete encoded/decoded budget
        # before decoding even the first object. This avoids partial large
        # allocations when a later member makes the closure over capacity.
        for item, ident in zip(blobs, descriptor_ids):
            ident2, raw = _decode_blob_descriptor(item, code=code)
            need(ident2 == ident, code, "Observed context CAS digest changed during validation")
            decoded[ident] = raw
        return cls(rows, decoded, project=project, extra_rows=extra_rows, code=code)

    def row(self, kind: str, ident: str) -> dict[str, Any] | None:
        section = self._SECTIONS.get(kind)
        if section is None:
            raise Fault(self.code, "Unknown pinned context kind", kind)
        value = self._indexes.get(section, {}).get(ident)
        return dict(value) if value is not None else None

    def task_history(self, task: str) -> Iterable[dict[str, Any]]:
        return [dict(row) for row in self.context_rows.get("task_revision_history", [])
                if row.get("task") == task]

    def blob(self, sha256: str) -> bytes | None:
        _strict_sha(sha256, "Observed context CAS reference", code=self.code)
        try:
            value = self.blob_reader(sha256) if callable(self.blob_reader) else self.blob_reader.get(sha256)
        except (Fault, KeyError, OSError, TypeError, ValueError) as exc:
            raise Fault(self.code, "Observed context CAS object cannot be read", sha256) from exc
        if value is None:
            return None
        need(isinstance(value, (bytes, bytearray)) and digest(bytes(value)) == sha256,
             self.code, "Observed context CAS object digest differs", sha256)
        return bytes(value)

    def blob_get(self, ident: str) -> bytes:
        value = self.blob(ident)
        need(value is not None, self.code, "Observed context CAS object is missing", ident)
        return value

    def receipt_body(self, receipt: str) -> dict[str, Any] | None:
        row = self.row("receipt", receipt)
        if row is None:
            return None
        body = row.get("body")
        if isinstance(body, str):
            body = parse_json(body, limit=MAX_SNAPSHOT_BYTES)
        need(isinstance(body, dict), self.code, "Observed receipt body is malformed", receipt)
        if row.get("body_digest") is not None:
            need(row["body_digest"] == digest(body), self.code,
                 "Observed receipt body checksum differs", receipt)
        return body

    def __call__(self, section: str, key: str) -> dict[str, Any] | None:
        return self._indexes.get(section, {}).get(key)


def row_cas_refs(section: str, row: dict[str, Any], blob_reader: Any) -> set[str]:
    """Enumerate one row's typed CAS closure for every portable format.

    Chunked records use the resulting refs for their raw-object descriptors;
    direct specifications use the same rules to build and verify their inline
    CAS table.  Ordinary body digests are never scanned as CAS references.
    """
    from .traceability import _trace_blob_refs
    if section in (*CONTEXT_SECTIONS, *TRACEABILITY_SECTIONS, "sources", "documents"):
        return set(_trace_blob_refs(row))
    if section == "assurance_objects":
        if row.get("kind") != "material":
            return set()
        body = _json_body(row.get("body"))
        root = body.get("payload_blob") if isinstance(body, dict) else None
        if isinstance(root, str) and _SHA256.fullmatch(root):
            from .assurance import material_cas_closure
            return set(material_cas_closure(blob_reader, root))
        return set()
    if section == "assurance_refs":
        ref = row.get("ref_digest")
        if not isinstance(ref, str) or not _SHA256.fullmatch(ref):
            return set()
        if isinstance(blob_reader, dict) and ref not in blob_reader:
            return set()
        blob_ids = getattr(blob_reader, "blob_ids", None)
        if isinstance(blob_ids, set) and ref not in blob_ids:
            return set()
        try:
            value = (blob_reader.blob_get(ref) if hasattr(blob_reader, "blob_get")
                     else blob_reader(ref) if callable(blob_reader)
                     else blob_reader.get(ref))
        except Fault as exc:
            if exc.code in {"missing_evidence", "not_found"}:
                return set()
            raise
        except KeyError:
            return set()
        if isinstance(value, (bytes, bytearray)) and digest(bytes(value)) == ref:
            return {ref}
        if value is not None:
            need(False, "invalid_archive", "Assurance reference CAS bytes differ from their digest", ref)
        return set()
    return set()


def _json_body(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return parse_json(value, limit=MAX_SNAPSHOT_BYTES)
        except Fault:
            return value
    return value


def required_cas_refs(spec: dict[str, Any], rows: dict[str, list[dict[str, Any]]],
                      blob_reader: Any) -> set[str]:
    """Return the typed CAS closure roots for a direct or chunked projection."""
    refs: set[str] = set()
    for section, values in rows.items():
        if section in CONTEXT_SECTIONS:
            for row in values:
                refs.update(row_cas_refs(section, row, blob_reader))
    for section, values in (spec.get("traceability_history", {}) or {}).items():
        if section in TRACEABILITY_SECTIONS and isinstance(values, list):
            for row in values:
                refs.update(row_cas_refs(section, row, blob_reader))
    if spec.get("format") == "daikibo.spec.v7":
        refs.update(row["blob"] for row in spec.get("sources", []))
    history = spec.get("assurance_history") or {}
    for section in ("assurance_objects", "assurance_refs"):
        values = history.get(section, [])
        if isinstance(values, list):
            for row in values:
                if isinstance(row, dict):
                    refs.update(row_cas_refs(section, row, blob_reader))
    return refs


def _source_blob_size(store: Any, ident: str) -> int:
    """Read a CAS object's size from store metadata before materializing it."""
    if isinstance(store, dict):
        value = store.get(ident)
        need(isinstance(value, (bytes, bytearray)), "missing_evidence",
             "Portable CAS object is missing", ident)
        return len(value)
    blob_path = getattr(store, "blob_path", None)
    if callable(blob_path):
        try:
            return blob_path(ident).stat().st_size
        except Fault as exc:
            if exc.code != "artifact_requires_stream":
                raise
            from .backup_artifacts import open_artifact_session
            with open_artifact_session(store, ident) as session:
                return session.size
    getter = getattr(store, "blob_get", None)
    if callable(getter):
        return len(getter(ident))
    if callable(store):
        return len(store(ident))
    raise Fault("invalid_archive", "Portable CAS source has no bounded size reader", ident)


class _BudgetedBlobReader:
    """Prevent material-graph discovery from reading over the wire budget."""

    def __init__(self, store: Any, limit: int | None = None):
        self.store = store
        self.limit = MAX_SNAPSHOT_BYTES if limit is None else limit
        self.sizes: dict[str, int] = {}
        self.seen: set[str] = set()
        self.total = 0

    def blob_get(self, ident: str) -> bytes:
        size = self.sizes.get(ident)
        if size is None:
            size = _source_blob_size(self.store, ident)
            self.sizes[ident] = size
        if ident not in self.seen and self.total + size > self.limit:
            raise Fault("snapshot_too_large", "Observed context CAS closure exceeds the specification limit", {
                "kind": "decoded_blob_bytes", "required": self.total + size,
                "limit": self.limit,
            })
        raw = (self.store.blob_get(ident) if hasattr(self.store, "blob_get")
               else self.store(ident) if callable(self.store) else self.store.get(ident))
        need(isinstance(raw, (bytes, bytearray)) and len(raw) == size and digest(bytes(raw)) == ident,
             "integrity_error", "Portable CAS source bytes differ from their digest", ident)
        if ident not in self.seen:
            self.seen.add(ident)
            self.total += len(raw)
        return bytes(raw)


def make_observed_context(store: Any, project: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Build the v1 observed context without publishing any new CAS object."""
    rows = project_context_rows(store, project)
    # The context reader is also used to prove the exact row shape while the
    # source transaction is still open.  Extra rows remain in the existing
    # specification and are not duplicated in the five-key wire section.
    validate_context_rows(rows, project)
    bounded = _BudgetedBlobReader(store)
    refs = required_cas_refs(spec, rows, bounded)
    sized = []
    encoded_total = 0
    decoded_total = 0
    for ident in sorted(refs):
        size = _source_blob_size(store, ident)
        encoded_size = 4 * ((size + 2) // 3)
        next_encoded = encoded_total + encoded_size
        next_decoded = decoded_total + size
        if size > MAX_SNAPSHOT_BYTES:
            raise Fault("snapshot_too_large", "Observed context CAS object exceeds the specification limit", {
                "kind": "single_blob_bytes", "required": size,
                "limit": MAX_SNAPSHOT_BYTES,
            })
        if next_encoded > MAX_SNAPSHOT_BYTES:
            raise Fault("snapshot_too_large", "Observed context encoded CAS exceeds the specification limit", {
                "kind": "encoded_blob_bytes", "required": next_encoded,
                "limit": MAX_SNAPSHOT_BYTES,
            })
        if next_decoded > MAX_SNAPSHOT_BYTES:
            raise Fault("snapshot_too_large", "Observed context decoded CAS exceeds the specification limit", {
                "kind": "decoded_blob_bytes", "required": next_decoded,
                "limit": MAX_SNAPSHOT_BYTES,
            })
        sized.append((ident, size))
        encoded_total = next_encoded
        decoded_total = next_decoded
    blobs = []
    for ident, expected_size in sized:
        raw = store.blob_get(ident)
        need(len(raw) == expected_size and digest(raw) == ident,
             "integrity_error", "Portable CAS source bytes differ from their digest", ident)
        encoded = base64.b64encode(raw).decode("ascii")
        blobs.append({
            "sha256": ident,
            "bytes": len(raw),
            "encoding": "base64",
            "data": encoded,
        })
    context = {
        "format": CONTEXT_FORMAT,
        "project": project,
        "historical_only": True,
        "runtime_restore_supported": False,
        "fresh_review_or_test_evidence": False,
        "tables": rows,
        "blobs": blobs,
    }
    context["digest"] = digest(context)
    return context
