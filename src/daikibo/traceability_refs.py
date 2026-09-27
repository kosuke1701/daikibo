"""Strict, read-only resolution of the five Unit B traceability references.

This module is intentionally a small boundary component.  It reads the
immutable rows and CAS objects produced by Unit A and the existing workflow;
it does not publish a revision, adopt a candidate, evaluate a completion
gate, or fill a missing pin from a repository working tree.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import re
import stat
from pathlib import PurePosixPath
from typing import Any, Iterable

from .common import Fault, canonical, digest, parse_json
from .candidate_provenance import (
    PinnedContext,
    PYTHON_ADAPTER,
    PYTHON_AST_V1_DIGEST,
    SNAPSHOT_FORMAT,
    candidate_task_revisions,
    resolve_candidate_pin,
)


RESOLVED_FORMAT = "traceability.resolved-ref.v1"
TRACEABILITY_FORMAT = "daikibo.traceability.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEX = re.compile(r"^[0-9a-f]+$")


def _invalid(message: str, details: Any = None) -> None:
    raise Fault("invalid_reference", message, details)


def _unknown(message: str, details: Any = None) -> None:
    raise Fault("unknown_reference", message, details)


def _cross(message: str, details: Any = None) -> None:
    raise Fault("cross_project", message, details)


def _stale(message: str, details: Any = None) -> None:
    raise Fault("stale_reference", message, details)


def _ambiguous(message: str, details: Any = None) -> None:
    raise Fault("ambiguous_reference", message, details)


def _unresolved(message: str, details: Any = None) -> None:
    raise Fault("unresolved_reference", message, details)


def _integrity(message: str, details: Any = None) -> None:
    raise Fault("integrity_error", message, details)


def _object(value: Any, required: Iterable[str], optional: Iterable[str] = (), *, name: str = "reference") -> dict[str, Any]:
    if type(value) is not dict:
        _invalid(f"{name} must be an object")
    required_set, optional_set = set(required), set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - required_set - optional_set)
    if missing:
        _invalid(f"{name} is missing fields", missing)
    if unknown:
        _invalid(f"{name} has unknown fields", unknown)
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _invalid(f"{name} must be a {'possibly empty ' if allow_empty else ''}string")
    if "\x00" in value:
        _invalid(f"{name} contains NUL")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        _invalid(f"{name} is below its minimum", {"minimum": minimum, "value": value})
    if maximum is not None and value > maximum:
        _invalid(f"{name} is above its maximum", {"maximum": maximum, "value": value})
    return value


def _sha(value: Any, name: str = "digest") -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        _invalid(f"{name} must be a lowercase SHA-256 digest")
    return value


def _oid(value: Any, object_format: str, name: str = "oid") -> str:
    if object_format not in {"sha1", "sha256"}:
        _invalid("object_format must be sha1 or sha256")
    length = 40 if object_format == "sha1" else 64
    if type(value) is not str or len(value) != length or not _HEX.fullmatch(value) or value.lower() != value:
        _invalid(f"{name} must be a complete lowercase {object_format} OID")
    return value


def _stored_sha(value: Any, name: str = "digest") -> str:
    try:
        return _sha(value, name)
    except Fault as exc:
        _integrity(f"Stored {name} is malformed", exc.details)


def _stored_oid(value: Any, object_format: str, name: str = "oid") -> str:
    try:
        return _oid(value, object_format, name)
    except Fault as exc:
        _integrity(f"Stored {name} is malformed", exc.details)


def _path(value: Any, name: str = "path") -> str:
    if type(value) is not str or not value or "\x00" in value or "\\" in value:
        _invalid(f"{name} must be a nonempty repository-relative POSIX path")
    try:
        parsed = PurePosixPath(value)
    except (TypeError, ValueError):
        _invalid(f"{name} is not a POSIX path")
    if parsed.is_absolute() or value in {".", ".."} or any(part in {"", ".", ".."} for part in parsed.parts):
        _invalid(f"{name} must be normalized and repository-relative")
    if str(parsed) != value or ".git" in parsed.parts or any(part.startswith(".daikibo-control") for part in parsed.parts):
        _invalid(f"{name} must be a normalized repository-relative path")
    return value


def _interval(start: Any, end: Any, total: int, *, name: str = "interval") -> tuple[int, int]:
    # bool is deliberately excluded: it is an int subclass but never a byte
    # coordinate in the reference contract.
    if type(start) is not int or type(end) is not int:
        _invalid(f"{name} endpoints must be integers")
    if not 0 <= start < end <= total:
        _invalid(f"{name} must be a nonempty half-open interval", {"start": start, "end": end, "bytes": total})
    return start, end


def _same(value: Any, expected: Any, message: str, *, code: str = "integrity_error") -> None:
    if value != expected:
        raise Fault(code, message, {"expected": expected, "actual": value})


def _json_row(row: dict[str, Any], field: str = "body", *, limit: int = 256 * 1024 * 1024) -> dict[str, Any]:
    raw = row.get(field)
    if not isinstance(raw, str):
        _integrity(f"Stored {field} is not JSON", row.get("id"))
    try:
        body = parse_json(raw, limit=limit)
    except Fault as exc:
        _integrity(f"Stored {field} is not valid JSON", row.get("id"))
    if type(body) is not dict:
        _integrity(f"Stored {field} is not an object", row.get("id"))
    return body


def _record_body(store, row: dict[str, Any]) -> dict[str, Any]:
    body = _json_row(row)
    _same(digest(body), row.get("digest"), "Traceability record digest differs")
    return body


def _sort_dependencies(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for value in values:
        if type(value) is not dict:
            _integrity("Dependency descriptor is not an object")
        kind, ident = value.get("kind"), value.get("id")
        if type(kind) is not str or type(ident) is not str:
            _integrity("Dependency descriptor lacks kind or id")
        revision = value.get("revision")
        key = (kind, ident, "" if revision is None else str(revision))
        previous = unique.get(key)
        if previous is not None and canonical(previous) != canonical(value):
            _integrity("Duplicate dependency has conflicting identity", value)
        unique[key] = value
    return [unique[key] for key in sorted(unique, key=lambda item: item)]


def _content_descriptor(blob: str, start: int, end: int, raw: bytes) -> dict[str, Any]:
    selected = raw[start:end]
    return {"blob": blob, "byte_start": start, "byte_end": end,
            "sha256": digest(selected), "bytes": len(selected)}


def _scope_matches(path: str, scope: dict[str, Any]) -> bool:
    roots = scope.get("roots")
    include = scope.get("include")
    if type(roots) is not list or type(include) is not list:
        _integrity("Pinned Git scope roots or include filters are malformed")
    if any(type(value) is not str for value in roots + include):
        _integrity("Pinned Git scope filters contain a non-string value")
    in_root = any(not root or path == root or path.startswith(root + "/") for root in roots)
    if not in_root:
        return False
    return not include or any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern) for pattern in include)


def _git_oid_from_raw(raw: bytes, object_format: str) -> str:
    algorithm = "sha1" if object_format == "sha1" else "sha256"
    return hashlib.new(algorithm, raw).hexdigest()


def _git_raw(raw: bytes, expected_type: str | None = None) -> tuple[str, bytes]:
    separator = raw.find(b"\0")
    if separator <= 0:
        _integrity("Git object header is malformed")
    try:
        header = raw[:separator].decode("ascii").split(" ")
    except UnicodeDecodeError:
        _integrity("Git object header is not ASCII")
    if len(header) != 2 or header[0] not in {"commit", "tree", "blob", "tag"}:
        _integrity("Git object header has an unknown type")
    if not header[1].isdigit():
        _integrity("Git object header has an invalid size")
    try:
        size = int(header[1])
    except ValueError:
        _integrity("Git object header has an invalid size")
    payload = raw[separator + 1:]
    if size < 0 or len(payload) != size:
        _integrity("Git object payload length differs from its header")
    if expected_type is not None and header[0] != expected_type:
        _integrity("Git object type differs", {"expected": expected_type, "actual": header[0]})
    return header[0], payload


def _tree_entries(raw: bytes, object_format: str) -> list[tuple[int, str, str, str]]:
    _kind, payload = _git_raw(raw, "tree")
    oid_bytes = 20 if object_format == "sha1" else 32
    entries: list[tuple[int, str, str, str]] = []
    offset = 0
    names: set[str] = set()
    while offset < len(payload):
        mode_end = payload.find(b" ", offset)
        if mode_end <= offset:
            _integrity("Git tree entry mode is malformed")
        name_end = payload.find(b"\0", mode_end + 1)
        if name_end <= mode_end or name_end + 1 + oid_bytes > len(payload):
            _integrity("Git tree entry is truncated")
        try:
            mode = int(payload[offset:mode_end], 8)
            name = payload[mode_end + 1:name_end].decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            _integrity("Git tree entry name or mode is malformed")
        if not name or "/" in name or name in {".", ".."} or name in names:
            _integrity("Git tree contains an invalid or duplicate entry name", name)
        names.add(name)
        oid = payload[name_end + 1:name_end + 1 + oid_bytes].hex()
        kind = "tree" if stat.S_ISDIR(mode) else "commit" if mode == 0o160000 else "blob"
        entries.append((mode, kind, oid, name))
        offset = name_end + 1 + oid_bytes
    if offset != len(payload):
        _integrity("Git tree has trailing bytes")
    return entries


def _line_starts(raw: bytes) -> list[int]:
    starts = [0]
    for index, value in enumerate(raw):
        if value == 10 and index + 1 < len(raw):
            starts.append(index + 1)
    return starts


def _ast_byte_offset(raw: bytes, starts: list[int], line: int, column: int) -> int:
    if type(line) is not int or type(column) is not int or line < 1 or line > len(starts):
        _integrity("AST span line is outside the source")
    line_start = starts[line - 1]
    line_end = raw.find(b"\n", line_start)
    if line_end < 0:
        line_end = len(raw)
    segment = raw[line_start:line_end]
    if line == 1 and segment.startswith(b"\xef\xbb\xbf"):
        line_start += 3
        segment = segment[3:]
    if column < 0 or column > len(segment):
        _integrity("AST span column is outside the source")
    return line_start + column


def _node_span(raw: bytes, starts: list[int], node: ast.AST) -> tuple[int, int]:
    values = tuple(getattr(node, key, None) for key in ("lineno", "end_lineno", "col_offset", "end_col_offset"))
    if not all(type(value) is int for value in values):
        _integrity("AST definition has no complete source span")
    start = _ast_byte_offset(raw, starts, values[0], values[2])
    end = _ast_byte_offset(raw, starts, values[1], values[3])
    if not 0 <= start <= end <= len(raw):
        _integrity("AST definition span exceeds source bytes")
    return start, end


class _DefinitionVisitor(ast.NodeVisitor):
    """Frozen copy of Unit A's python-ast-v1 definition walk.

    It is kept local deliberately: resolving a historical ref must not import
    a mutable private helper from a later Unit A working tree.
    """

    def __init__(self, raw: bytes, starts: list[int]):
        self.raw, self.starts = raw, starts
        self.stack: list[str] = []
        self.definitions: list[dict[str, Any]] = []
        self._ordinals: dict[tuple[str, str], int] = {}

    def _visit_definition(self, node: ast.AST, kind: str, name: str) -> None:
        parent = ".".join(self.stack) or None
        key = (parent or "", name)
        ordinal = self._ordinals.get(key, 0)
        self._ordinals[key] = ordinal + 1
        start, end = _node_span(self.raw, self.starts, node)
        decorators = getattr(node, "decorator_list", []) or []
        if decorators:
            decorator_start, _ = _node_span(self.raw, self.starts, decorators[0])
            line_start = self.starts[getattr(decorators[0], "lineno") - 1]
            marker = self.raw.rfind(b"@", line_start, decorator_start + 1)
            if marker >= line_start and self.raw[marker + 1:decorator_start].strip() == b"":
                decorator_start = marker
            start = min(start, decorator_start)
        self.definitions.append({
            "kind": kind, "name": name, "qualified_name": ".".join(self.stack + [name]),
            "parent": parent, "ordinal": ordinal, "start": start, "end": end,
            "signature_hash": digest(ast.dump(node, include_attributes=False)),
        })
        self.stack.append(name)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_definition(node, "function", node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_definition(node, "async_function", node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_definition(node, "class", node.name)


def _python_definitions(raw: bytes, path: str) -> list[dict[str, Any]]:
    try:
        raw.decode("utf-8-sig")
        tree = ast.parse(raw, filename=path, type_comments=True)
        starts = _line_starts(raw)
        visitor = _DefinitionVisitor(raw, starts)
        visitor.visit(tree)
    except (UnicodeDecodeError, SyntaxError, ValueError, TypeError, RecursionError) as exc:
        _unresolved("Python source cannot be parsed by python-ast-v1", {"path": path, "error": str(exc)[:500]})
    return sorted(visitor.definitions, key=lambda value: (value["start"], -value["end"], value["qualified_name"], value["ordinal"]))


def _find_definition(raw: bytes, path: str, *, qualified_name: str, kind: str, ordinal: int,
                     start: int, end: int, span_hash: str, signature_hash: str) -> dict[str, Any]:
    definitions = _python_definitions(raw, path)
    matches = [item for item in definitions
               if item["qualified_name"] == qualified_name and item["kind"] == kind
               and item["ordinal"] == ordinal and item["start"] == start and item["end"] == end
               and digest(raw[start:end]) == span_hash and item["signature_hash"] == signature_hash]
    if not matches:
        _unresolved("Python symbol does not match the pinned source bytes", {"path": path, "qualified_name": qualified_name})
    if len(matches) != 1:
        _ambiguous("Python symbol resolves to multiple definitions", {"path": path, "qualified_name": qualified_name})
    return matches[0]


class _LivePinnedContext(PinnedContext):
    """Fixed live-store adapter for the shared candidate provenance helper."""

    _QUERIES = {
        "candidate": "SELECT * FROM candidates WHERE id=?",
        "task": "SELECT * FROM tasks WHERE id=?",
        "run": "SELECT * FROM runs WHERE id=?",
        "receipt": "SELECT * FROM receipts WHERE id=?",
        "repository": "SELECT * FROM repos WHERE id=?",
    }

    def __init__(self, resolver: "TraceabilityRefResolver"):
        self.resolver = resolver

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for key in ("body", "result"):
            if isinstance(value.get(key), str):
                value[key] = parse_json(value[key])
        if isinstance(value.get("body"), dict) and value.get("body_digest") is None:
            value["body_digest"] = digest(value["body"])
        if value.get("result") is not None and value.get("result_digest") is None:
            value["result_digest"] = digest(value["result"])
        return value

    def row(self, kind: str, ident: str) -> dict[str, Any] | None:
        query = self._QUERIES.get(kind)
        if query is None:
            raise Fault("invalid_reference", "Unknown pinned context kind", kind)
        row = self.resolver.s.one(query, (ident,))
        return self._decode(row) if row is not None else None

    def task_history(self, task: str) -> Iterable[dict[str, Any]]:
        return [self._decode(row) for row in self.resolver.s.all(
            "SELECT * FROM task_revision_history WHERE task=? ORDER BY to_revision,id", (task,))]

    def blob(self, sha256: str) -> bytes | None:
        return self.resolver.s.blob_get(sha256)

    def receipt_body(self, receipt: str) -> dict[str, Any] | None:
        try:
            return self.resolver.c.g.receipt(receipt)
        except Fault as exc:
            if exc.code in {"not_found", "missing_evidence"}:
                return None
            raise


def _live_candidate_failure(kind: str, message: str, details: Any = None) -> None:
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


class TraceabilityRefResolver:
    """Resolve immutable structural references without changing controller state."""

    def __init__(self, control):
        self.c = control
        self.s = control.s

    def resolve(self, actor, project, ref, *, require_current: bool = True) -> dict[str, Any]:
        if type(require_current) is not bool:
            _invalid("require_current must be a boolean")
        # This is intentionally the first resolver operation.  It establishes
        # the caller's project scope before any reference-shaped input is read.
        self.c.k.project(actor, project)
        if type(ref) is not dict or type(ref.get("ref_type")) is not str:
            _invalid("ref must be a tagged object with ref_type")
        ref_type = ref["ref_type"]
        if ref_type not in {"git_file", "git_symbol", "candidate_symbol", "source_span", "artifact_ac"}:
            _invalid("Unsupported reference type", ref_type)
        with self.s.transaction():
            if ref_type == "git_file":
                self._schema_git(ref, symbol=False)
                return self._resolve_git(project, ref, symbol=False)
            if ref_type == "git_symbol":
                self._schema_git(ref, symbol=True)
                return self._resolve_git(project, ref, symbol=True)
            if ref_type == "candidate_symbol":
                self._schema_candidate(ref)
                return self._resolve_candidate(actor, project, ref, require_current)
            if ref_type == "source_span":
                self._schema_source(ref)
                return self._resolve_source(project, ref)
            self._schema_artifact(ref)
            return self._resolve_artifact(actor, project, ref, require_current)

    # ---------- strict input schemas ----------
    @staticmethod
    def _schema_git(ref: dict[str, Any], *, symbol: bool) -> None:
        # A missing pin is a resolvable input error (unresolved_reference), so
        # the two pin fields are the only common fields accepted as absent.
        required = {"ref_type", "repository", "object_format", "commit", "path", "blob_oid", "sha256", "mode"}
        if symbol:
            required |= {"adapter", "adapter_digest", "qualified_name", "kind", "ordinal", "start_byte",
                         "end_byte", "span_sha256", "signature_hash"}
        _object(ref, required, {"pin_revision", "pin_revision_digest"}, name="git reference")
        if ref["ref_type"] != ("git_symbol" if symbol else "git_file"):
            _invalid("Reference type does not match its schema")

    @staticmethod
    def _schema_candidate(ref: dict[str, Any]) -> None:
        _object(ref, {"ref_type", "candidate", "task", "task_revision", "candidate_digest", "snapshot_digest",
                      "repository", "path", "sha256", "mode", "adapter", "adapter_digest", "qualified_name",
                      "kind", "ordinal", "start_byte", "end_byte", "span_sha256", "signature_hash"}, name="candidate reference")
        if ref["ref_type"] != "candidate_symbol":
            _invalid("Reference type does not match its schema")

    @staticmethod
    def _schema_source(ref: dict[str, Any]) -> None:
        _object(ref, {"ref_type", "source_id", "blob_digest", "byte_start", "byte_end", "unicode_start",
                      "unicode_end", "span_hash"}, name="source span reference")
        if ref["ref_type"] != "source_span":
            _invalid("Reference type does not match its schema")

    @staticmethod
    def _schema_artifact(ref: dict[str, Any]) -> None:
        _object(ref, {"ref_type", "artifact", "revision", "body_digest", "ac_pointer", "ac_digest"}, {"ac_id"}, name="artifact AC reference")
        if ref["ref_type"] != "artifact_ac":
            _invalid("Reference type does not match its schema")

    # ---------- common result and CAS helpers ----------
    @staticmethod
    def _result(ref: dict[str, Any], dependencies: Iterable[dict[str, Any]], content: dict[str, Any] | None = None,
                *, current: bool | None = None) -> dict[str, Any]:
        result = {"format": RESOLVED_FORMAT, "ref_type": ref["ref_type"], "canonical_ref": dict(ref),
                  "identity_digest": digest(ref), "dependencies": _sort_dependencies(dependencies),
                  "evidence_claim": "structural_identity_only"}
        if content is not None:
            result["content"] = content
        if current is not None:
            result["current"] = current
        return result

    def _cas(self, ref: str, *, missing_code: str = "unresolved_reference") -> bytes:
        _sha(ref, "CAS reference")
        try:
            raw = self.s.blob_get(ref)
        except Fault as exc:
            if exc.code in {"missing_evidence", "artifact_requires_stream", "not_found"}:
                raise Fault(missing_code, "Required CAS object is missing", ref) from exc
            raise Fault("integrity_error", "Required CAS object could not be read", ref) from exc
        if digest(raw) != ref:
            _integrity("CAS object digest differs", ref)
        return raw

    @staticmethod
    def _cas_dep(ref: str, *, role: str | None = None) -> dict[str, Any]:
        value = {"kind": "cas", "id": ref, "revision": None, "digest": ref}
        if role is not None:
            value["role"] = role
        return value

    # ---------- TREV / TREC / Git pin ----------
    def _trace_revision(self, project: str, revision_id: Any, revision_digest: Any) -> dict[str, Any]:
        _string(revision_id, "pin_revision")
        _sha(revision_digest, "pin_revision_digest")
        row = self.s.one("SELECT * FROM traceability_revisions WHERE id=?", (revision_id,))
        if row is None:
            _unknown("Pinned traceability revision does not exist", revision_id)
        if row["project"] != project:
            _cross("Pinned traceability revision belongs to another project", revision_id)
        if row["digest"] != revision_digest:
            _stale("Pinned traceability revision digest differs", revision_id)
        if row["status"] not in {"ready", "active", "superseded"}:
            _stale("Pinned traceability revision is not a completed pin", {"revision": revision_id, "status": row["status"]})
        body = _json_row(row)
        if digest(body) != row["digest"]:
            _integrity("Traceability revision digest differs", revision_id)
        if body.get("format") != TRACEABILITY_FORMAT or body.get("revision") != revision_id:
            _integrity("Traceability revision identity differs", revision_id)
        if body.get("project") != project or body.get("set_id") != row["set_id"] or body.get("kind") != "code":
            _integrity("Traceability revision scope differs", revision_id)
        scope = body.get("scope")
        if type(scope) is not dict or scope.get("kind") != "code":
            _unresolved("Pinned revision is not a Git code population", revision_id)
        repository = scope.get("repository")
        _string(repository, "repository")
        repo = self.s.one("SELECT * FROM repos WHERE id=?", (repository,))
        if repo is None:
            _unknown("Pinned repository does not exist", repository)
        if repo["project"] != project:
            _cross("Pinned repository belongs to another project", repository)
        if scope.get("repository_name") != repo["name"]:
            _integrity("Pinned repository name differs", repository)
        object_format = scope.get("object_format")
        if object_format not in {"sha1", "sha256"}:
            _integrity("Pinned Git object format is invalid", object_format)
        if type(scope.get("empty_scope")) is not bool:
            _integrity("Pinned Git empty_scope flag is malformed", repository)
        commit = _stored_oid(scope.get("commit"), object_format, "scope.commit")
        compact = body.get("git_pin")
        if type(compact) is not dict or set(compact) != {"object_format", "commit", "commit_blob", "tree", "tree_blob"}:
            _integrity("TREV git_pin must be the compact five-field projection", revision_id)
        _same(commit, compact["commit"], "Revision scope commit differs")
        _same(object_format, compact["object_format"], "Revision scope object format differs")
        _stored_oid(compact["commit"], object_format, "git_pin.commit")
        _stored_oid(compact["tree"], object_format, "git_pin.tree")
        _stored_sha(compact["commit_blob"], "git_pin.commit_blob")
        _stored_sha(compact["tree_blob"], "git_pin.tree_blob")

        extracted_rows = self.s.all("SELECT * FROM traceability_records WHERE revision=? AND kind='extracted' ORDER BY id", (revision_id,))
        if not extracted_rows:
            _unknown("Completed extracted traceability record is missing", revision_id)
        extracted: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for record in extracted_rows:
            if record["project"] != project:
                _cross("Extracted traceability record belongs to another project", record["id"])
            if record.get("revision") != revision_id:
                _integrity("Extracted traceability record revision differs", record["id"])
            record_body = _record_body(self.s, record)
            if record_body.get("format") not in {None, TRACEABILITY_FORMAT}:
                _integrity("Extracted traceability record format differs", record["id"])
            if (record_body.get("revision") != revision_id
                    or record_body.get("revision_digest") != row["digest"]
                    or record_body.get("population_digest") != row["population_digest"]):
                _integrity("Extracted traceability record does not bind the TREV", record["id"])
            if record_body.get("status") != "ready":
                _stale("Extracted traceability record is not ready", record["id"])
            extracted.append((record, record_body))
        proposal_ids = {item[0].get("proposal") for item in extracted}
        if not all(isinstance(value, str) and value for value in proposal_ids):
            _integrity("Extracted record has no proposal identity")
        if len(proposal_ids) != 1:
            _ambiguous("TREV is bound to multiple extracted proposals", sorted(proposal_ids))
        proposal_id = next(iter(proposal_ids))
        extracted_record, extracted_body = extracted[0]
        if any(canonical(value[1]) != canonical(extracted_body) for value in extracted[1:]):
            _ambiguous("TREV has conflicting extracted records", [value[0]["id"] for value in extracted])
        proposal_row = self.s.one("SELECT * FROM traceability_proposals WHERE id=?", (proposal_id,))
        if proposal_row is None:
            _unknown("Traceability proposal for TREV is missing", proposal_id)
        if proposal_row["project"] != project:
            _cross("Traceability proposal belongs to another project", proposal_id)
        # A completed immutable extraction remains a valid pin after the
        # normal Unit B lifecycle records adoption on its source proposal.
        # Staging/failed/proposed/withdrawn are not completed pin authority;
        # unknown values are rejected rather than treated as adopted.
        if proposal_row["status"] not in {"ready", "adopted"}:
            _stale("Traceability proposal is not a completed source proposal", proposal_id)
        proposal_body = _json_row(proposal_row)
        if digest(proposal_body) != proposal_row["digest"]:
            _integrity("Traceability proposal digest differs", proposal_id)
        if proposal_body.get("id") != proposal_id or proposal_row["set_id"] != row["set_id"]:
            _integrity("Traceability proposal identity differs", proposal_id)
        if proposal_body.get("project") != project or proposal_body.get("set_id") != row["set_id"]:
            _integrity("Traceability proposal scope differs", proposal_id)
        if proposal_body.get("scope") != scope:
            _integrity("Traceability proposal scope does not match TREV", proposal_id)

        checkpoints = self.s.all("SELECT * FROM traceability_records WHERE proposal=? AND kind='extraction_checkpoint' ORDER BY created,id", (proposal_id,))
        complete: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for record in checkpoints:
            if record["project"] != project:
                _cross("Extraction checkpoint belongs to another project", record["id"])
            if record.get("revision") is not None:
                _integrity("Extraction checkpoint row.revision must be NULL", record["id"])
            checkpoint = _record_body(self.s, record)
            if checkpoint.get("format") != TRACEABILITY_FORMAT:
                _integrity("Extraction checkpoint format differs", record["id"])
            if checkpoint.get("proposal") != proposal_id or checkpoint.get("revision_id") != revision_id:
                _integrity("Extraction checkpoint identity differs", record["id"])
            if checkpoint.get("revision_number") != row["revision"]:
                _integrity("Extraction checkpoint revision number differs", record["id"])
            if checkpoint.get("stage") == "git_pin" and checkpoint.get("key") == "__git_pin_complete__" and checkpoint.get("complete") is True and checkpoint.get("pin_complete") is True:
                if type(checkpoint.get("git_pin")) is not dict or type(checkpoint.get("entry_manifest")) is not list:
                    _integrity("Complete Git checkpoint lacks its pin or manifest", record["id"])
                pins = checkpoint.get("pins")
                if type(pins) is not list or any(type(value) is not str or not _SHA256.fullmatch(value) for value in pins):
                    _integrity("Complete Git checkpoint pins are malformed", record["id"])
                complete.append((record, checkpoint))
        if not complete:
            _unresolved("Complete Git extraction checkpoint is missing", revision_id)
        fingerprints = {digest({"git_pin": value[1]["git_pin"], "entry_manifest": value[1]["entry_manifest"]}) for value in complete}
        if len(fingerprints) != 1:
            _ambiguous("Completed Git checkpoints disagree", [value[0]["id"] for value in complete])
        checkpoint_record, checkpoint_body = complete[0]
        full_pin = checkpoint_body["git_pin"]
        if set(full_pin) != {"object_format", "commit", "commit_blob", "tree", "tree_blob", "trees"}:
            _integrity("Complete checkpoint Git pin has an unexpected shape", checkpoint_record["id"])
        for key in ("object_format", "commit", "commit_blob", "tree", "tree_blob"):
            _same(full_pin[key], compact[key], "Complete checkpoint compact Git pin differs",)
        trees = full_pin.get("trees")
        if type(trees) is not list or not trees:
            _unresolved("Complete Git checkpoint has no durable tree closure", revision_id)
        for tree in trees:
            _object(tree, {"oid", "blob"}, name="complete Git tree closure row")
            _stored_oid(tree["oid"], object_format, "tree.oid")
            _stored_sha(tree["blob"], "tree.blob")
        marker_pins = checkpoint_body.get("pins")
        rev_pins = body.get("pins")
        if type(marker_pins) is not list or type(rev_pins) is not list:
            _integrity("Git checkpoint pins are malformed", revision_id)
        for pins in (marker_pins, rev_pins, extracted_body.get("pins", [])):
            if type(pins) is not list or any(type(value) is not str or not _SHA256.fullmatch(value) for value in pins):
                _integrity("Git checkpoint contains an invalid CAS pin", revision_id)
        if not set(marker_pins) <= set(rev_pins) or not set(extracted_body.get("pins", [])) <= set(rev_pins):
            _integrity("TREV pins do not contain complete checkpoint pins", revision_id)
        if any(not set(value[1]["pins"]) <= set(rev_pins) for value in complete):
            _integrity("A complete Git checkpoint is not covered by the TREV pins", revision_id)
        inventory = body.get("inventory")
        manifest = checkpoint_body.get("entry_manifest")
        if type(inventory) is not list or type(manifest) is not list:
            _integrity("Pinned Git inventory or entry manifest is malformed", revision_id)
        try:
            self._validate_manifest(manifest, object_format)
            self._validate_inventory(inventory, object_format)
        except Fault as exc:
            if exc.code == "integrity_error":
                raise
            raise Fault("integrity_error", "Pinned Git inventory is malformed", exc.details) from exc
        manifest_paths = {entry["path"] for entry in manifest}
        inventory_paths = {entry["path"] for entry in inventory}
        if manifest_paths != inventory_paths:
            _integrity("Git inventory and entry manifest path sets differ", revision_id)
        for entry in manifest:
            if not _scope_matches(entry["path"], scope):
                _integrity("Git entry manifest path is outside the pinned scope", entry["path"])
        needed_pins = {compact["commit_blob"], compact["tree_blob"]}
        needed_pins.update(entry["blob"] for entry in trees)
        needed_pins.update(entry["sha256"] for entry in inventory if entry.get("sha256") is not None)
        needed_pins.update(entry["git_object_blob"] for entry in inventory if entry.get("git_object_blob") is not None)
        if not needed_pins <= set(rev_pins):
            _integrity("TREV pins do not contain every referenced Git CAS object", sorted(needed_pins - set(rev_pins)))
        if any(not needed_pins <= set(value[1]["pins"]) for value in complete):
            _integrity("Complete Git checkpoint pins do not cover the Git closure", revision_id)
        return {"row": row, "body": body, "scope": scope, "repo": repo, "repository": repository,
                "object_format": object_format, "compact": compact, "full_pin": full_pin,
                "manifest": manifest, "inventory": inventory, "proposal": proposal_row,
                "proposal_body": proposal_body, "extracted": extracted_record, "extracted_body": extracted_body,
                "checkpoint": checkpoint_record, "checkpoint_body": checkpoint_body}

    @staticmethod
    def _validate_manifest(manifest: list[Any], object_format: str) -> None:
        paths: set[str] = set()
        for entry in manifest:
            _object(entry, {"mode", "kind", "oid", "path"}, name="Git entry manifest row")
            path = _path(entry["path"], "manifest.path")
            if path in paths:
                _ambiguous("Git entry manifest contains a duplicate path", path)
            paths.add(path)
            _integer(entry["mode"], "manifest.mode", minimum=0)
            if entry["kind"] not in {"blob", "tree", "commit"}:
                _integrity("Git entry manifest kind is invalid", entry)
            _stored_oid(entry["oid"], object_format, "manifest.oid")

    @staticmethod
    def _validate_inventory(inventory: list[Any], object_format: str) -> None:
        paths: set[str] = set()
        for entry in inventory:
            if type(entry) is not dict:
                _integrity("Git inventory row is not an object", entry)
            kind = entry.get("type")
            # Unit A stores regular blobs (including symlink payload blobs)
            # with both content and raw Git-object CAS leaves.  Gitlinks and
            # other non-blob tree entries are durable identity rows only and
            # intentionally omit git_object_blob; an unrelated such entry
            # must not poison regular-file resolution.
            required = {"path", "type", "mode", "blob_oid", "sha256", "bytes"}
            optional = {"git_object_blob"} if kind != "blob" else set()
            if kind == "blob":
                required.add("git_object_blob")
            _object(entry, required, optional, name="Git inventory row")
            path = _path(entry["path"], "inventory.path")
            if path in paths:
                _ambiguous("Git inventory contains a duplicate path", path)
            paths.add(path)
            if kind not in {"blob", "commit", "tree", "symlink", "source"}:
                _integrity("Git inventory type is invalid", entry)
            _integer(entry["mode"], "inventory.mode", minimum=0) if entry["mode"] is not None else None
            if entry["blob_oid"] is not None:
                _stored_oid(entry["blob_oid"], object_format, "inventory.blob_oid")
            if entry["sha256"] is not None:
                _stored_sha(entry["sha256"], "inventory.sha256")
            if entry.get("git_object_blob") is not None:
                _stored_sha(entry["git_object_blob"], "inventory.git_object_blob")
            _integer(entry["bytes"], "inventory.bytes", minimum=0)

    def _git_closure(self, ctx: dict[str, Any], path: str) -> dict[str, Any]:
        object_format = ctx["object_format"]
        pin = ctx["full_pin"]
        raw_refs: list[str] = []
        commit_raw = self._cas(pin["commit_blob"])
        raw_refs.append(pin["commit_blob"])
        if _git_oid_from_raw(commit_raw, object_format) != pin["commit"]:
            _integrity("Pinned commit CAS does not hash to its Git OID", pin["commit"])
        _kind, commit_payload = _git_raw(commit_raw, "commit")
        tree_match = re.search(rb"(?m)^tree ([0-9a-f]+)$", commit_payload)
        if tree_match is None:
            _integrity("Pinned commit has no root tree", pin["commit"])
        root_tree = tree_match.group(1).decode("ascii")
        _same(root_tree, pin["tree"], "Pinned commit root tree differs")
        _stored_oid(root_tree, object_format, "commit.tree")

        tree_map: dict[str, tuple[str, bytes]] = {}
        for entry in pin["trees"]:
            try:
                _object(entry, {"oid", "blob"}, name="Git tree closure row")
            except Fault as exc:
                _integrity("Pinned Git tree closure row is malformed", exc.details)
            oid = _stored_oid(entry["oid"], object_format, "tree.oid")
            blob = _stored_sha(entry["blob"], "tree.blob")
            if oid in tree_map:
                _integrity("Pinned tree closure contains a duplicate tree OID", oid)
            raw = self._cas(blob)
            if _git_oid_from_raw(raw, object_format) != oid:
                _integrity("Pinned tree CAS does not hash to its Git OID", oid)
            _git_raw(raw, "tree")
            tree_map[oid] = (blob, raw)
            raw_refs.append(blob)
        if root_tree not in tree_map:
            _unresolved("Pinned root tree is absent from the complete CAS closure", root_tree)
        if pin["tree_blob"] != tree_map[root_tree][0]:
            _integrity("Pinned compact root tree CAS differs")

        # A Git tree object may occur at several path prefixes (for example
        # identical ``a/`` and ``b/`` subtrees).  Cache its parsed bytes by
        # OID, but walk every occurrence.  ``reachable_oids`` is only the
        # closure accounting set; it must never suppress a path walk.
        reachable_oids: set[str] = set()
        parsed_trees: dict[str, list[tuple[int, str, str, str]]] = {}
        selected: dict[str, tuple[int, str, str]] = {}

        def walk(oid: str, prefix: str, ancestry: tuple[str, ...] = ()) -> None:
            if oid in ancestry:
                _integrity("Pinned Git tree closure contains an ancestry cycle", oid)
            if oid not in tree_map:
                _unresolved("Pinned tree closure is missing a child tree", oid)
            reachable_oids.add(oid)
            entries = parsed_trees.get(oid)
            if entries is None:
                entries = _tree_entries(tree_map[oid][1], object_format)
                parsed_trees[oid] = entries
            next_ancestry = (*ancestry, oid)
            for mode, kind, child_oid, name in entries:
                child_path = f"{prefix}/{name}" if prefix else name
                if kind == "tree":
                    walk(child_oid, child_path, next_ancestry)
                else:
                    selected[child_path] = (mode, kind, child_oid)

        if not ctx["scope"].get("empty_scope"):
            walk(root_tree, "")
            if reachable_oids != set(tree_map):
                _integrity("Pinned tree closure contains unreachable or omitted trees", {"reachable": sorted(reachable_oids), "pinned": sorted(tree_map)})
        else:
            if ctx["manifest"] or ctx["inventory"]:
                _unresolved("An empty Git scope has no selected file entries", path)
            if set(tree_map) != {root_tree}:
                _integrity("An empty Git scope must pin only its root tree", path)
            reachable_oids.add(root_tree)

        manifest_rows = {entry["path"]: entry for entry in ctx["manifest"]}
        if path not in manifest_rows:
            _unknown("Path is not a selected immutable Git entry", path)
        manifest = manifest_rows[path]
        if not ctx["scope"].get("empty_scope"):
            actual = selected.get(path)
            if actual is None:
                _integrity("Manifest path is not present in the pinned Git tree", path)
            if (manifest["mode"], manifest["kind"], manifest["oid"]) != actual:
                _integrity("Manifest path differs from the pinned Git tree", path)
        inventory_rows = [entry for entry in ctx["inventory"] if entry["path"] == path]
        if len(inventory_rows) == 0:
            _unknown("Path has no selected Git inventory row", path)
        if len(inventory_rows) != 1:
            _ambiguous("Path has multiple selected Git inventory rows", path)
        inventory = inventory_rows[0]
        if inventory["type"] != manifest["kind"] or inventory["mode"] != manifest["mode"] or inventory["blob_oid"] != manifest["oid"]:
            _integrity("Git inventory differs from the immutable entry manifest", path)
        if manifest["kind"] != "blob" or inventory["sha256"] is None or inventory["git_object_blob"] is None:
            _unresolved("The selected Git entry is not a regular file with a pinned blob", path)
        mode = inventory["mode"]
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            _unresolved("Symlinks and non-regular Git entries are not resolvable file refs", path)
        raw_content = self._cas(inventory["sha256"])
        raw_refs.append(inventory["sha256"])
        if len(raw_content) != inventory["bytes"]:
            _integrity("Pinned file byte count differs", path)
        raw_object = self._cas(inventory["git_object_blob"])
        raw_refs.append(inventory["git_object_blob"])
        if _git_oid_from_raw(raw_object, object_format) != inventory["blob_oid"]:
            _integrity("Pinned Git blob object does not hash to its entry OID", path)
        _kind, payload = _git_raw(raw_object, "blob")
        if payload != raw_content:
            _integrity("Pinned Git blob payload differs from its content CAS", path)
        if _git_oid_from_raw(f"blob {len(raw_content)}\0".encode() + raw_content, object_format) != inventory["blob_oid"]:
            _integrity("Pinned content bytes differ from the entry Git OID", path)
        return {"raw": raw_content, "inventory": inventory, "manifest": manifest, "raw_refs": raw_refs}

    def _git_dependencies(self, ctx: dict[str, Any], raw_refs: Iterable[str], *, adapter: str | None = None,
                          adapter_digest: str | None = None) -> list[dict[str, Any]]:
        dependencies: list[dict[str, Any]] = [
            {"kind": "traceability_revision", "id": ctx["row"]["id"], "revision": ctx["row"]["revision"], "digest": ctx["row"]["digest"]},
            {"kind": "traceability_record", "id": ctx["extracted"]["id"], "revision": ctx["extracted"].get("revision"), "digest": ctx["extracted"]["digest"], "record_kind": "extracted"},
            {"kind": "traceability_record", "id": ctx["checkpoint"]["id"], "revision": ctx["checkpoint"].get("revision"), "digest": ctx["checkpoint"]["digest"], "record_kind": "extraction_checkpoint"},
        ]
        dependencies.extend(self._cas_dep(value) for value in sorted(set(raw_refs)))
        if adapter is not None:
            dependencies.append({"kind": "adapter", "id": adapter, "revision": "v1", "digest": adapter_digest})
        return dependencies

    def _resolve_git(self, project: str, ref: dict[str, Any], *, symbol: bool) -> dict[str, Any]:
        _string(ref["repository"], "repository")
        object_format = ref["object_format"]
        if object_format not in {"sha1", "sha256"}:
            _invalid("object_format must be sha1 or sha256")
        commit = _oid(ref["commit"], object_format, "commit")
        path = _path(ref["path"])
        blob_oid = _oid(ref["blob_oid"], object_format, "blob_oid")
        sha256 = _sha(ref["sha256"], "sha256")
        mode = _integer(ref["mode"], "mode", minimum=0)
        if "pin_revision" not in ref or "pin_revision_digest" not in ref:
            _unresolved("A completed immutable pin is required for Git references", ref.get("path"))
        _sha(ref["pin_revision_digest"], "pin_revision_digest")
        ctx = self._trace_revision(project, ref["pin_revision"], ref["pin_revision_digest"])
        _same(ctx["repository"], ref["repository"], "Reference repository differs from the pinned scope", code="stale_reference")
        _same(ctx["object_format"], object_format, "Reference object format differs from the pinned scope", code="stale_reference")
        _same(ctx["compact"]["commit"], commit, "Reference commit differs from the pinned scope", code="stale_reference")
        closure = self._git_closure(ctx, path)
        inventory = closure["inventory"]
        if inventory["blob_oid"] != blob_oid or inventory["sha256"] != sha256 or inventory["mode"] != mode:
            _stale("Reference file identity differs from the pinned inventory", path)
        raw = closure["raw"]
        dependencies = self._git_dependencies(ctx, closure["raw_refs"])
        if not symbol:
            content = _content_descriptor(sha256, 0, len(raw), raw)
            return self._result(ref, dependencies, content)

        _string(ref["adapter"], "adapter")
        if ref["adapter"] != PYTHON_ADAPTER:
            _unresolved("Only python-ast-v1 symbols are resolvable", ref["adapter"])
        if not path.endswith((".py", ".pyi")):
            _unresolved("python-ast-v1 requires a Python source path", path)
        adapter_digest = _sha(ref["adapter_digest"], "adapter_digest")
        if adapter_digest != PYTHON_AST_V1_DIGEST:
            _stale("Symbol adapter digest is not the frozen python-ast-v1 implementation", adapter_digest)
        contract = ctx["body"].get("adapter_contract")
        if (type(contract) is not dict or contract.get("id") != PYTHON_ADAPTER
                or contract.get("version") != "v1"
                or contract.get("implementation_digest") != adapter_digest):
            _stale("Symbol adapter digest differs from the pinned revision", ref["adapter_digest"])
        qualified_name = _string(ref["qualified_name"], "qualified_name")
        kind = _string(ref["kind"], "kind")
        if kind not in {"function", "async_function", "class"}:
            _invalid("Unsupported Python symbol kind", kind)
        ordinal = _integer(ref["ordinal"], "ordinal", minimum=0)
        start, end = _interval(ref["start_byte"], ref["end_byte"], len(raw), name="symbol span")
        span_hash = _sha(ref["span_sha256"], "span_sha256")
        signature_hash = _sha(ref["signature_hash"], "signature_hash")
        _find_definition(raw, path, qualified_name=qualified_name, kind=kind, ordinal=ordinal,
                         start=start, end=end, span_hash=span_hash, signature_hash=signature_hash)
        dependencies = self._git_dependencies(ctx, closure["raw_refs"], adapter=ref["adapter"], adapter_digest=adapter_digest)
        return self._result(ref, dependencies, _content_descriptor(sha256, start, end, raw))

    # ---------- candidate symbol ----------
    def _resolve_candidate(self, actor, project: str, ref: dict[str, Any], require_current: bool) -> dict[str, Any]:
        candidate_id = _string(ref["candidate"], "candidate")
        task_id = _string(ref["task"], "task")
        task_revision = _integer(ref["task_revision"], "task_revision", minimum=1)
        candidate_digest = _sha(ref["candidate_digest"], "candidate_digest")
        snapshot_digest = _sha(ref["snapshot_digest"], "snapshot_digest")
        repository = _string(ref["repository"], "repository")
        path = _path(ref["path"])
        sha256 = _sha(ref["sha256"], "sha256")
        mode = _integer(ref["mode"], "mode", minimum=0)
        adapter = _string(ref["adapter"], "adapter")
        if adapter != PYTHON_ADAPTER:
            _unresolved("Only python-ast-v1 candidate symbols are resolvable", adapter)
        adapter_digest = _sha(ref["adapter_digest"], "adapter_digest")
        if adapter_digest != PYTHON_AST_V1_DIGEST:
            _stale("Candidate adapter digest is not the frozen python-ast-v1 implementation", adapter_digest)
        qualified_name = _string(ref["qualified_name"], "qualified_name")
        kind = _string(ref["kind"], "kind")
        if kind not in {"function", "async_function", "class"}:
            _invalid("Unsupported Python symbol kind", kind)
        ordinal = _integer(ref["ordinal"], "ordinal", minimum=0)
        candidate_start = ref["start_byte"]
        candidate_end = ref["end_byte"]
        if type(candidate_start) is not int or type(candidate_end) is not int:
            _invalid("Candidate symbol span endpoints must be integers")
        signature_hash = _sha(ref["signature_hash"], "signature_hash")
        span_hash = _sha(ref["span_sha256"], "span_sha256")

        # Actor/task authorization remains a live workflow concern.  The
        # immutable candidate identity and all implementation provenance are
        # resolved by the shared read-only context below.
        try:
            self.c.w.task(actor, task_id)
        except Fault as exc:
            if exc.code == "not_found":
                raise Fault("unknown_reference", "Task does not exist", task_id) from exc
            if exc.code in {"invalid_json", "invalid_input", "integrity_error"}:
                raise Fault("integrity_error", "Task projection is invalid", task_id) from exc
            raise

        context = _LivePinnedContext(self)
        resolved = resolve_candidate_pin(project, ref, context, failure=_live_candidate_failure)
        candidate_row = context.row("candidate", candidate_id)
        task_row = context.row("task", task_id)
        if candidate_row is None or task_row is None:  # guarded by the helper
            _unknown("Candidate or Task disappeared during resolution", candidate_id)
        raw = self._cas(sha256)

        # Currentness is an input-freshness check only.  It deliberately does
        # not call evaluate_task or any completion/adoption gate.
        current = (task_row["status"] != "cancelled" and task_row["revision"] == task_revision
                   and task_row["candidate"] == candidate_id and candidate_row["epoch"] == task_row["epoch"])
        try:
            current_failures = self.c.g.check_current(task_id, ensure_policy=False)
        except Fault as exc:
            if exc.code in {"invalid_json", "invalid_policy", "invalid_input", "integrity_error", "not_found"}:
                _integrity("Task currentness evidence is invalid", task_id)
            raise
        if current_failures:
            current = False
        if not current and require_current:
            _stale("Candidate is not current for the requested task revision", {"task": task_id, "candidate": candidate_id, "failures": current_failures})
        _find_definition(raw, path, qualified_name=qualified_name, kind=kind, ordinal=ordinal,
                         start=candidate_start, end=candidate_end, span_hash=span_hash, signature_hash=signature_hash)
        dependencies = list(resolved["dependency_refs"])
        dependencies.append({"kind": "adapter", "id": adapter, "revision": "v1", "digest": adapter_digest})
        return self._result(ref, dependencies, resolved["content"], current=current)

    def _candidate_revisions(self, task: str, candidate: str) -> dict[int, str]:
        context = _LivePinnedContext(self)
        row = context.row("task", task)
        if row is None:
            _unknown("Task does not exist", task)
        try:
            return candidate_task_revisions(row, context.task_history(task), candidate,
                                            failure=_live_candidate_failure)
        except Fault:
            raise

    # ---------- source spans ----------
    def _resolve_source(self, project: str, ref: dict[str, Any]) -> dict[str, Any]:
        if ref["source_id"] is None:
            _unresolved("A registered source is required for source spans", None)
        source_id = _string(ref["source_id"], "source_id")
        blob_digest = _sha(ref["blob_digest"], "blob_digest")
        span_hash = _sha(ref["span_hash"], "span_hash")
        source = self.s.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if source is None:
            _unknown("Source does not exist", source_id)
        if source["project"] != project:
            _cross("Source belongs to another project", source_id)
        stored_blob = _stored_sha(source["blob"], "source.blob")
        if stored_blob != blob_digest:
            _stale("Source blob digest differs", source_id)
        raw = self._cas(stored_blob)
        if type(source["characters"]) is not int or source["characters"] < 0:
            _integrity("Source character count is invalid", source_id)
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            _unresolved("Source span requires valid UTF-8 source bytes", source_id)
        if len(decoded) != source["characters"]:
            _integrity("Source character count differs from its blob", source_id)
        start, end = _interval(ref["byte_start"], ref["byte_end"], len(raw), name="source span")
        try:
            prefix_start = raw[:start].decode("utf-8")
            selected = raw[start:end].decode("utf-8")
            prefix_end = raw[:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            _invalid("Source span endpoints must be strict UTF-8 boundaries", {"start": start, "end": end})
        unicode_start = _integer(ref["unicode_start"], "unicode_start", minimum=0)
        unicode_end = _integer(ref["unicode_end"], "unicode_end", minimum=unicode_start)
        _same(unicode_start, len(prefix_start), "Source Unicode start differs", code="stale_reference")
        _same(unicode_end, len(prefix_end), "Source Unicode end differs", code="stale_reference")
        _same(span_hash, digest(raw[start:end]), "Source span digest differs", code="stale_reference")
        dependencies = [{"kind": "source", "id": source_id, "revision": None, "digest": blob_digest,
                         "project": project, "blob_digest": blob_digest}, self._cas_dep(blob_digest)]
        return self._result(ref, dependencies, _content_descriptor(blob_digest, start, end, raw))

    # ---------- artifact acceptance criteria ----------
    @staticmethod
    def _pointer(pointer: Any) -> list[str]:
        if type(pointer) is not str:
            _invalid("ac_pointer must be an RFC 6901 string")
        if pointer == "":
            return []
        if not pointer.startswith("/"):
            _invalid("ac_pointer must start with /")
        tokens: list[str] = []
        for token in pointer[1:].split("/"):
            value = []
            index = 0
            while index < len(token):
                if token[index] != "~":
                    value.append(token[index]); index += 1; continue
                if index + 1 >= len(token) or token[index + 1] not in "01":
                    _invalid("ac_pointer contains an invalid RFC 6901 escape")
                value.append("/" if token[index + 1] == "1" else "~")
                index += 2
            tokens.append("".join(value))
        return tokens

    def _resolve_artifact(self, actor, project: str, ref: dict[str, Any], require_current: bool) -> dict[str, Any]:
        artifact_id = _string(ref["artifact"], "artifact")
        revision = _integer(ref["revision"], "revision", minimum=1)
        body_digest = _sha(ref["body_digest"], "body_digest")
        ac_digest = _sha(ref["ac_digest"], "ac_digest")
        tokens = self._pointer(ref["ac_pointer"])
        if len(tokens) != 2 or tokens[0] != "acceptance" or not tokens[1].isdigit() or (tokens[1] != "0" and tokens[1].startswith("0")):
            _unresolved("ac_pointer is outside the existing artifact acceptance container", ref["ac_pointer"])
        current = self.s.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if current is None:
            _unknown("Artifact does not exist", artifact_id)
        if current["project"] != project:
            _cross("Artifact belongs to another project", artifact_id)
        current_body = _json_row(current)
        if digest(current_body) != current["digest"]:
            _integrity("Current artifact body digest differs", artifact_id)
        historical = self.s.one("SELECT * FROM revisions WHERE artifact=? AND revision=?", (artifact_id, revision))
        if historical is None:
            _unknown("Artifact revision does not exist", {"artifact": artifact_id, "revision": revision})
        if historical["digest"] != body_digest:
            _stale("Artifact revision digest differs", artifact_id)
        body = _json_row(historical)
        if digest(body) != historical["digest"]:
            _integrity("Artifact revision body digest differs", artifact_id)
        # Run the public projection validator as a read-only semantic/schema
        # check, while using the historical SQL row below for status identity.
        try:
            projected = self.c.k.artifact(actor, artifact_id, revision)
        except Fault as exc:
            if exc.code in {"not_found", "missing_evidence"}:
                raise Fault("unresolved_reference", "Artifact projection is unavailable", artifact_id) from exc
            raise Fault("integrity_error", "Artifact projection is invalid", artifact_id) from exc
        if projected.get("project") != project or projected.get("revision") != revision or projected.get("digest") != body_digest:
            _integrity("Artifact projection identity differs", artifact_id)
        acceptance = body.get("acceptance")
        if type(acceptance) is not list:
            _unresolved("Artifact has no existing acceptance container", artifact_id)
        index = int(tokens[1])
        if index >= len(acceptance):
            _unknown("Acceptance pointer does not exist in the artifact revision", ref["ac_pointer"])
        value = acceptance[index]
        if type(ref.get("ac_id")) is str:
            if (type(value) is dict and value.get("id") != ref["ac_id"]) or (type(value) is str and value != ref["ac_id"]):
                _stale("ac_id does not identify the pointed acceptance criterion", ref["ac_id"])
        elif "ac_id" in ref:
            _invalid("ac_id must be a string when supplied")
        _same(ac_digest, digest(value), "Acceptance criterion digest differs", code="stale_reference")
        is_current = (current["revision"] == revision and current["digest"] == body_digest and current["status"] == "accepted")
        # A non-current resolve is an immutable history read.  The revision
        # row/body/digest and acceptance pointer above establish its exact
        # identity; a later withdrawal must not erase that retained shape.
        # The current resolver still rejects every non-accepted/non-current
        # artifact through the explicit gate below.
        if require_current and not is_current:
            _stale("Artifact acceptance criterion is not the current accepted revision", artifact_id)
        dependencies = [
            {"kind": "artifact", "id": artifact_id, "revision": revision, "digest": body_digest, "project": project,
             "body_digest": body_digest},
            {"kind": "artifact_ac", "id": artifact_id, "revision": revision, "digest": body_digest,
             "project": project, "pointer": ref["ac_pointer"], "ac_digest": ac_digest,
             "body_digest": body_digest},
        ]
        return self._result(ref, dependencies, current=is_current)


__all__ = ["TraceabilityRefResolver", "RESOLVED_FORMAT"]
