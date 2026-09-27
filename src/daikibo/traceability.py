"""Immutable source populations for the dev28 traceability foundation.

Unit A deliberately stops at mechanical population extraction and durable
history.  It records proposals, decisions and mappings as typed, immutable
history, but does not turn a proposal into an adopted scope or a successful
closure.  The latter requires Unit B's existing review and workflow gates.

The module has no third party dependency.  Python extraction uses the stdlib
AST and Git is read through argument-array subprocess calls against the
registered repository at one full commit.  All bytes needed to read a
population later are copied into the controller CAS before the revision is
published.
"""
from __future__ import annotations

import ast
import base64
import binascii
import fnmatch
import hashlib
import json
import os
import re
import stat
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .common import Actor, Fault, canonical, digest, need, obj, parse_json, relative_path, strings, text, timestamp, uid
from .candidate_provenance import resolve_candidate_pin
from .portable_context import PortableObservedContext, candidate_context_row
from .traceability_refs import PYTHON_AST_V1_DIGEST


# Kept beside the migration so a schema-13 database and a fresh database use
# byte-for-byte equivalent table/trigger definitions.
SCHEMA = r"""
CREATE TABLE IF NOT EXISTS traceability_sets (
 id TEXT PRIMARY KEY,
 project TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('population','code','document')),
 active_revision TEXT REFERENCES traceability_revisions(id), active_digest TEXT, created REAL NOT NULL,
 UNIQUE(project,name)
);
CREATE TABLE IF NOT EXISTS traceability_revisions (
 id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES traceability_sets(id),
 project TEXT NOT NULL REFERENCES projects(id), revision INTEGER NOT NULL CHECK(revision>0),
 status TEXT NOT NULL CHECK(status IN ('staging','failed','ready','active','superseded')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 population_digest TEXT NOT NULL, adapter TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(set_id,revision), UNIQUE(set_id,digest)
);
CREATE INDEX IF NOT EXISTS traceability_revisions_project ON traceability_revisions(project,set_id,revision);
CREATE TABLE IF NOT EXISTS traceability_items (
 id TEXT PRIMARY KEY, revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 item_kind TEXT NOT NULL CHECK(item_kind IN ('file','atom','symbol','line','group')),
 path TEXT, status TEXT NOT NULL CHECK(status IN ('known','unknown','tombstone')),
 start_byte INTEGER NOT NULL CHECK(start_byte>=0), end_byte INTEGER NOT NULL CHECK(end_byte>=start_byte),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 leaf INTEGER NOT NULL DEFAULT 0 CHECK(leaf IN (0,1)),
 UNIQUE(revision,ordinal), UNIQUE(revision,digest)
);
CREATE INDEX IF NOT EXISTS traceability_items_page ON traceability_items(revision,ordinal,id);
CREATE INDEX IF NOT EXISTS traceability_items_path ON traceability_items(revision,path,ordinal);
CREATE INDEX IF NOT EXISTS traceability_items_leaf ON traceability_items(revision,leaf,status);
CREATE TABLE IF NOT EXISTS traceability_proposals (
 id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES traceability_sets(id),
 project TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('population','code','document','decision','mapping','scope')),
 status TEXT NOT NULL CHECK(status IN ('proposed','staging','ready','failed','adopted','withdrawn')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 expected_active TEXT, semantic_material_digest TEXT NOT NULL,
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS traceability_proposals_project ON traceability_proposals(project,set_id,created,id);
CREATE TABLE IF NOT EXISTS traceability_decisions (
 id TEXT PRIMARY KEY, revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','stale')), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traceability_mappings (
 id TEXT PRIMARY KEY, revision TEXT NOT NULL REFERENCES traceability_revisions(id),
 project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','accepted','stale')), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traceability_bindings (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 revision TEXT NOT NULL REFERENCES traceability_revisions(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','mandatory','stale','withdrawn')), created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traceability_records (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 revision TEXT REFERENCES traceability_revisions(id), proposal TEXT REFERENCES traceability_proposals(id),
 kind TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS traceability_records_project ON traceability_records(project,created,id);
CREATE INDEX IF NOT EXISTS traceability_review_packets_page
 ON traceability_records(project,proposal,kind,json_extract(body,'$.packet_index'))
 WHERE kind='review_packet';
CREATE INDEX IF NOT EXISTS traceability_delivered_mapping_lookup
 ON traceability_records(project,kind,json_extract(body,'$.delivery'),json_extract(body,'$.mapping_id'))
 WHERE kind='delivered_mapping';
CREATE TRIGGER IF NOT EXISTS traceability_sets_immutable BEFORE UPDATE OF project,name,kind,created ON traceability_sets BEGIN SELECT RAISE(ABORT,'immutable traceability set'); END;
CREATE TRIGGER IF NOT EXISTS traceability_sets_no_delete BEFORE DELETE ON traceability_sets BEGIN SELECT RAISE(ABORT,'retain traceability sets'); END;
CREATE TRIGGER IF NOT EXISTS traceability_revisions_immutable BEFORE UPDATE OF set_id,project,revision,body,digest,population_digest,adapter,created ON traceability_revisions BEGIN SELECT RAISE(ABORT,'immutable traceability revision'); END;
CREATE TRIGGER IF NOT EXISTS traceability_revisions_no_delete BEFORE DELETE ON traceability_revisions BEGIN SELECT RAISE(ABORT,'retain traceability revisions'); END;
CREATE TRIGGER IF NOT EXISTS traceability_items_immutable BEFORE UPDATE ON traceability_items BEGIN SELECT RAISE(ABORT,'immutable traceability item'); END;
CREATE TRIGGER IF NOT EXISTS traceability_items_no_delete BEFORE DELETE ON traceability_items BEGIN SELECT RAISE(ABORT,'retain traceability items'); END;
CREATE TRIGGER IF NOT EXISTS traceability_proposals_immutable BEFORE UPDATE OF set_id,project,kind,body,digest,expected_active,semantic_material_digest,created ON traceability_proposals BEGIN SELECT RAISE(ABORT,'immutable traceability proposal'); END;
CREATE TRIGGER IF NOT EXISTS traceability_proposals_no_delete BEFORE DELETE ON traceability_proposals BEGIN SELECT RAISE(ABORT,'retain traceability proposals'); END;
CREATE TRIGGER IF NOT EXISTS traceability_decisions_immutable BEFORE UPDATE ON traceability_decisions BEGIN SELECT RAISE(ABORT,'immutable traceability decision'); END;
CREATE TRIGGER IF NOT EXISTS traceability_decisions_no_delete BEFORE DELETE ON traceability_decisions BEGIN SELECT RAISE(ABORT,'retain traceability decisions'); END;
CREATE TRIGGER IF NOT EXISTS traceability_mappings_immutable BEFORE UPDATE ON traceability_mappings BEGIN SELECT RAISE(ABORT,'immutable traceability mapping'); END;
CREATE TRIGGER IF NOT EXISTS traceability_mappings_no_delete BEFORE DELETE ON traceability_mappings BEGIN SELECT RAISE(ABORT,'retain traceability mappings'); END;
CREATE TRIGGER IF NOT EXISTS traceability_bindings_immutable BEFORE UPDATE ON traceability_bindings BEGIN SELECT RAISE(ABORT,'immutable traceability binding'); END;
CREATE TRIGGER IF NOT EXISTS traceability_bindings_no_delete BEFORE DELETE ON traceability_bindings BEGIN SELECT RAISE(ABORT,'retain traceability bindings'); END;
CREATE TRIGGER IF NOT EXISTS traceability_records_immutable BEFORE UPDATE ON traceability_records BEGIN SELECT RAISE(ABORT,'immutable traceability record'); END;
CREATE TRIGGER IF NOT EXISTS traceability_records_no_delete BEFORE DELETE ON traceability_records BEGIN SELECT RAISE(ABORT,'retain traceability records'); END;
"""

TRACEABILITY_FORMAT = "daikibo.traceability.v1"
ARCHIVE_FORMAT = "daikibo.traceability-archive.v10"
HISTORY_VERSION = 10
MAX_PAGE = 500
MAX_PAGE_BYTES = 1 * 1024 * 1024
MAX_ITEM_BYTES = 64 * 1024 * 1024
MAX_REVISION_BODY_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_PAYLOAD_BYTES = 256 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX = re.compile(r"^[0-9a-f]+$")
_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$|^[0-9a-f]{64}$")
# The AST collector's identity is part of every immutable population pin.  It
# must remain stable when this module grows new lifecycle and archive code;
# using the whole mutable module digest would make old symbol references
# unverifiable after an otherwise compatible Unit B release.
# Keep archive/ref validation and the live resolver on one adapter digest
# authority.  Candidate and Git-symbol refs must never drift between the two
# code paths as this module evolves.
_PYTHON_AST_V1_DIGEST = PYTHON_AST_V1_DIGEST


def _read_transaction(function):
    """Keep page/count/snapshot reads on one SQLite snapshot."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self.s.transaction():
            return function(self, *args, **kwargs)
    return wrapped


def _adapter_contract(adapter: str) -> dict[str, Any]:
    if adapter in {"python-ast-v1", "python.ast.v1", "python"}:
        implementation_digest = _PYTHON_AST_V1_DIGEST
    else:
        try:
            implementation_digest = digest(Path(__file__).read_bytes())
        except OSError:
            implementation_digest = None
    return {"id": adapter, "version": adapter.rsplit("-", 1)[-1],
            "implementation_digest": implementation_digest,
            "cpython_grammar_version": f"{sys.version_info.major}.{sys.version_info.minor}"}


def _sha256_oid(raw: bytes, object_format: str) -> str:
    """Return the Git object ID for raw object bytes."""
    algorithm = "sha256" if object_format == "sha256" else "sha1"
    return hashlib.new(algorithm, raw).hexdigest()


def _git_env(repo: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 60) -> bytes:
    need(all(isinstance(a, str) and "\x00" not in a for a in args), "invalid_git_argument", "Git arguments must be strings")
    command = list(args)
    # Roots are inventory paths, not Git pathspec programs.  Keep wildcard
    # expansion available only to the explicit include filter handled by our
    # own matcher.
    if command and command[0] == "ls-tree":
        command.insert(0, "--literal-pathspecs")
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-C", str(repo), *command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(repo),
        timeout=timeout,
    )
    if check and result.returncode:
        raise Fault("git_failed", "Git operation failed", {"argv": command, "stderr": result.stderr.decode(errors="replace")[-4000:]})
    return result.stdout


def _git_oid(repo: Path, value: str, kind: str = "commit") -> str:
    need(isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)), "invalid_git_commit", "Expected a complete hexadecimal Git object ID")
    result = _git(repo, "cat-file", "-t", value, check=False)
    need(result.decode().strip() == kind, "invalid_git_commit", f"Pinned Git object is not a {kind}")
    return value


def _git_object(repo: Path, oid: str, object_format: str, expected_type: str | None = None) -> bytes:
    """Read one Git object including its type/size header and verify its OID."""
    # ``cat-file --batch`` gives the raw payload without a shell and works for
    # both SHA-1 and SHA-256 repositories.  The one-object invocation keeps
    # memory bounded by the already explicit 32 MiB source-file limit.
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-C", str(repo), "cat-file", "--batch"],
        input=(oid + "\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_git_env(repo), timeout=60,
    )
    need(result.returncode == 0, "git_failed", "Git object read failed", result.stderr.decode(errors="replace")[-4000:])
    header_end = result.stdout.find(b"\n")
    need(header_end > 0, "git_failed", "Git object response is malformed")
    header = result.stdout[:header_end].decode("ascii", errors="replace").split()
    need(len(header) == 3 and header[0] == oid and header[1] != "missing", "git_object_missing", "Pinned Git object is missing", oid)
    typ, size = header[1], int(header[2])
    payload_start = header_end + 1
    payload = result.stdout[payload_start:payload_start + size]
    need(len(payload) == size, "git_failed", "Git object payload was truncated", oid)
    need(expected_type is None or typ == expected_type, "git_object_type", "Pinned Git object type differs", {"oid": oid, "expected": expected_type, "actual": typ})
    raw = f"{typ} {size}\0".encode() + payload
    need(_sha256_oid(raw, object_format) == oid, "git_object_hash", "Git object hash differs from the pinned OID", oid)
    return raw


def _git_object_payload(raw: bytes, expected_type: str | None = None) -> tuple[str, bytes]:
    """Split a raw Git object that was already verified against its OID."""
    header_end = raw.find(b"\0")
    need(header_end > 0, "git_object_corrupt", "Pinned Git object header is malformed")
    header = raw[:header_end].decode("ascii", errors="replace").split()
    need(len(header) == 2 and (expected_type is None or header[0] == expected_type),
         "git_object_type", "Pinned Git object type differs")
    payload = raw[header_end + 1:]
    need(len(payload) == int(header[1]), "git_object_corrupt", "Pinned Git object size differs")
    return header[0], payload


def _tree_entries(raw: bytes, object_format: str) -> list[tuple[int, str, str, str]]:
    """Decode one Git tree payload into mode, kind, oid, and basename entries."""
    _kind, payload = _git_object_payload(raw, "tree")
    oid_bytes = 20 if object_format == "sha1" else 32
    entries: list[tuple[int, str, str, str]] = []
    offset = 0
    while offset < len(payload):
        mode_end = payload.find(b" ", offset)
        name_end = payload.find(b"\0", mode_end + 1)
        need(mode_end > offset and name_end > mode_end and name_end + 1 + oid_bytes <= len(payload),
             "invalid_git_inventory", "Pinned Git tree entry is malformed")
        mode = int(payload[offset:mode_end], 8)
        name = payload[mode_end + 1:name_end].decode("utf-8")
        oid = payload[name_end + 1:name_end + 1 + oid_bytes].hex()
        kind = "tree" if stat.S_ISDIR(mode) else "commit" if mode == 0o160000 else "blob"
        entries.append((mode, kind, oid, name))
        offset = name_end + 1 + oid_bytes
    return entries


def _safe_roots(value: Any) -> list[str]:
    if value is None:
        return [""]
    if isinstance(value, str):
        value = [value]
    need(isinstance(value, list) and len(value) <= 10000, "invalid_input", "Invalid roots")
    need(all(isinstance(item, str) and "\x00" not in item and len(item) <= 4096 for item in value), "invalid_input", "Invalid roots")
    out = []
    for item in value:
        if item in {"", "."}:
            item = ""
        else:
            item = relative_path(item).strip("/")
        if item not in out:
            out.append(item)
    return sorted(out)


def _patterns(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    strings(value, "include", maximum=10000, item_maximum=4096)
    return sorted(value)


def _matches(path: str, roots: list[str], include: list[str]) -> bool:
    in_root = any(not root or path == root or path.startswith(root + "/") for root in roots)
    if not in_root:
        return False
    return not include or any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern) for pattern in include)


def _line_starts(raw: bytes) -> list[int]:
    starts = [0]
    for index, value in enumerate(raw):
        # A terminating newline closes the current physical line.  It must
        # not manufacture an extra zero-byte line at EOF; consecutive
        # newlines still retain the start of each real empty line.
        if value == 10 and index + 1 < len(raw):
            starts.append(index + 1)
    return starts


def _ast_byte_offset(raw: bytes, starts: list[int], line: int, column: int) -> int:
    need(type(line) is int and line >= 1 and line <= len(starts), "parse_error", "AST line is outside the raw source")
    # CPython's AST columns are UTF-8 byte columns.  This is why this mapping
    # deliberately does not use Python string indices for source spans.
    line_start = starts[line - 1]
    line_end = raw.find(b"\n", line_start)
    if line_end < 0:
        line_end = len(raw)
    segment = raw[line_start:line_end]
    if line == 1 and segment.startswith(b"\xef\xbb\xbf"):
        line_start += 3
        segment = segment[3:]
    need(0 <= column <= len(segment), "parse_error", "AST column is outside the raw source")
    return line_start + column


def _unicode_position(raw: bytes, byte_offset: int, bom: bool) -> int:
    """Return canonical source character coordinates for a raw byte offset.

    ``Knowledge.source`` stores ``len(content)`` and ``source.read`` decodes
    with ordinary UTF-8, so a leading U+FEFF is part of that coordinate
    system.  Keep it here; callers that need a display coordinate can derive
    one separately while byte offsets remain the lossless authority.
    """
    prefix = raw[:byte_offset]
    try:
        return len(prefix.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Fault("invalid_encoding", "Source span is not valid UTF-8") from exc


def _unicode_offset_map(raw: bytes, offsets: Iterable[int]) -> dict[int, int | None]:
    """Map a bounded set of raw UTF-8 byte boundaries in one linear pass.

    Calling ``raw[:offset].decode()`` for every atom made a large flat Python
    file quadratic even after the AST interval sweep was linearithmic.  Span
    boundaries are known up front, so count UTF-8 leading bytes once and
    answer all source-span lookups from this compact map.  U+FEFF is counted
    as the canonical source character just like ``Knowledge.source``.
    """
    wanted = sorted(set(int(value) for value in offsets))
    need(all(0 <= value <= len(raw) for value in wanted), "invalid_range", "UTF-8 span boundary is outside the source")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return {value: None for value in wanted}
    result: dict[int, int] = {}
    cursor = 0
    wanted_index = 0
    for index, value in enumerate(raw):
        while wanted_index < len(wanted) and wanted[wanted_index] == index:
            result[index] = cursor
            wanted_index += 1
        # UTF-8 continuation bytes do not begin a Unicode scalar.
        if value & 0xC0 != 0x80:
            cursor += 1
    while wanted_index < len(wanted) and wanted[wanted_index] == len(raw):
        result[len(raw)] = cursor
        wanted_index += 1
    return result


def _node_span(raw: bytes, starts: list[int], node: ast.AST) -> tuple[int, int]:
    line = getattr(node, "lineno", None)
    end_line = getattr(node, "end_lineno", None)
    col = getattr(node, "col_offset", None)
    end_col = getattr(node, "end_col_offset", None)
    need(all(type(value) is int for value in (line, end_line, col, end_col)), "parse_error", "AST node has no complete source span")
    start = _ast_byte_offset(raw, starts, line, col)
    end = _ast_byte_offset(raw, starts, end_line, end_col)
    need(0 <= start <= end <= len(raw), "parse_error", "AST span exceeds raw source")
    return start, end


class _DefinitionVisitor(ast.NodeVisitor):
    def __init__(self, raw: bytes, starts: list[int]):
        self.raw, self.starts = raw, starts
        self.stack: list[str] = []
        self.definitions: list[dict[str, Any]] = []
        self._ordinals: dict[tuple[str, str], int] = {}

    def _visit_definition(self, node: ast.AST, kind: str, name: str):
        parent = ".".join(self.stack) or None
        key = (parent or "", name)
        ordinal = self._ordinals.get(key, 0)
        self._ordinals[key] = ordinal + 1
        start, end = _node_span(self.raw, self.starts, node)
        decorators = getattr(node, "decorator_list", []) or []
        if decorators:
            # AST positions begin at the decorator expression after ``@``.
            # Recover the marker from the same physical line so the symbol
            # span owns the complete decorator bytes.
            dstart, _ = _node_span(self.raw, self.starts, decorators[0])
            line_start = self.starts[getattr(decorators[0], "lineno") - 1]
            marker = self.raw.rfind(b"@", line_start, dstart + 1)
            if marker >= line_start and self.raw[marker + 1:dstart].strip() == b"":
                dstart = marker
            start = min(start, dstart)
        qualified = ".".join(self.stack + [name])
        entry = {
            "kind": kind, "name": name, "qualified_name": qualified,
            "parent": parent, "ordinal": ordinal, "start": start, "end": end,
            "signature_hash": digest(ast.dump(node, include_attributes=False)),
            "ast_hash": digest(ast.dump(node, include_attributes=True)),
        }
        self.definitions.append(entry)
        self.stack.append(name)
        # Generic traversal is intentional: definitions inside if/try/loops
        # and lambda/default expressions remain visible to the population.
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.stack.pop()

    def visit_FunctionDef(self, node):  # noqa: N802
        self._visit_definition(node, "function", node.name)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._visit_definition(node, "async_function", node.name)

    def visit_ClassDef(self, node):  # noqa: N802
        self._visit_definition(node, "class", node.name)


def _source_span(source_id: str | None, blob: str, raw: bytes, start: int, end: int, bom: bool,
                 unicode_positions: dict[int, int | None] | None = None) -> dict[str, Any]:
    try:
        if unicode_positions is None:
            unicode_start = _unicode_position(raw, start, bom)
            unicode_end = _unicode_position(raw, end, bom)
        else:
            unicode_start = unicode_positions.get(start)
            unicode_end = unicode_positions.get(end)
            need(unicode_start is not None and unicode_end is not None, "invalid_encoding", "Source span is not valid UTF-8")
    except Fault:
        # Invalid UTF-8 remains a retained unknown byte span.  The byte
        # coordinates and hash are still exact; Unicode coordinates are
        # intentionally unavailable rather than guessed.
        unicode_start = unicode_end = None
    return {
        "ref_type": "source_span",
        "source_id": source_id,
        "blob_digest": blob,
        "unicode_start": unicode_start,
        "unicode_end": unicode_end,
        "byte_start": start,
        "byte_end": end,
        "span_hash": digest(raw[start:end]),
    }


def _item(ident: str, ordinal: int, kind: str, path: str | None, status: str, start: int, end: int, body: dict[str, Any]) -> dict[str, Any]:
    encoded = canonical(body)
    return {"id": ident, "ordinal": ordinal, "item_kind": kind, "path": path, "status": status,
            "start_byte": start, "end_byte": end, "body": body, "digest": digest(encoded)}


def _code_file_items(set_id: str, revision_no: int, path: str, raw: bytes, blob: str, blob_oid: str | None, mode: int, source_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a file item, non-overlapping byte atoms, and definition groups."""
    file_key = [set_id, revision_no, path, blob]
    file_id = "ITEM-" + digest(file_key + ["file"])[:40]
    bom = raw.startswith(b"\xef\xbb\xbf")
    unicode_positions = _unicode_offset_map(raw, {0, len(raw)}) if raw else {}
    base = {"type": "file", "path": path, "blob_oid": blob_oid, "blob_digest": blob,
            "bytes": len(raw), "mode": mode, "language": "python" if path.endswith((".py", ".pyi")) else None,
            "leaf": False, "source_span": _source_span(source_id, blob, raw, 0, len(raw), bom, unicode_positions) if raw else None}
    file_item = _item(file_id, 0, "file", path, "known", 0, len(raw), base)
    if not raw:
        # An empty file is an explicit zero-byte target.  It has no interval
        # atom and is distinct from a parse/adapter unknown.
        file_item["body"] = {**base, "leaf": True, "empty": True}
        file_item["digest"] = digest(file_item["body"])
        return [file_item], {"file": file_item, "leaf_count": 1, "unknown_count": 0, "atom_ids": [file_id]}
    if not path.endswith((".py", ".pyi")):
        file_item["status"] = "unknown"
        file_item["body"] = {**base, "leaf": True, "unknown_reason": "unsupported_adapter"}
        file_item["digest"] = digest(file_item["body"])
        return [file_item], {"file": file_item, "leaf_count": 1, "unknown_count": 1, "atom_ids": [file_id]}
    try:
        raw.decode("utf-8-sig")
        tree = ast.parse(raw, filename=path, type_comments=True)
        starts = _line_starts(raw)
        visitor = _DefinitionVisitor(raw, starts)
        visitor.visit(tree)
    except (UnicodeDecodeError, SyntaxError, Fault, ValueError, TypeError, RecursionError) as exc:
        file_item["status"] = "unknown"
        file_item["body"] = {**base, "leaf": False, "unknown_reason": "parseerror" if isinstance(exc, SyntaxError) else "invalid_utf8", "error": str(exc)[:500]}
        file_item["digest"] = digest(file_item["body"])
        atom_id = "ITEM-" + digest(file_key + ["unknown", 0, len(raw)])[:40]
        atom = _item(atom_id, 1, "atom", path, "unknown", 0, len(raw), {
            "type": "byte_atom", "leaf": True, "path": path, "byte_start": 0, "byte_end": len(raw),
            "text_digest": digest(raw), "unknown_reason": file_item["body"]["unknown_reason"],
            "source_span": _source_span(source_id, blob, raw, 0, len(raw), bom, unicode_positions),
        })
        return [file_item, atom], {"file": file_item, "leaf_count": 1, "unknown_count": 1, "atom_ids": [atom_id]}

    definitions = sorted(visitor.definitions, key=lambda value: (value["start"], -value["end"], value["qualified_name"], value["ordinal"]))
    boundaries = {0, len(raw)}
    for definition in definitions:
        boundaries.update((definition["start"], definition["end"]))
    points = sorted(boundaries)
    unicode_positions = _unicode_offset_map(raw, points)
    # Sweep boundaries once.  Searching all definitions for every segment
    # made a flat generated file quadratic.  The heap's smallest live
    # interval is the nearest definition owner; ended entries are discarded
    # lazily as they reach the heap top.
    import heapq
    starts_by_definition = sorted(
        enumerate(definitions),
        key=lambda pair: (pair[1]["start"], -pair[1]["end"], pair[0]),
    )
    active: list[tuple[int, int, int, str, dict[str, Any]]] = []
    next_definition = 0
    segments: list[dict[str, Any]] = []
    for start, end in zip(points, points[1:]):
        if end <= start:
            continue
        while next_definition < len(starts_by_definition) and starts_by_definition[next_definition][1]["start"] <= start:
            index, entry = starts_by_definition[next_definition]
            heapq.heappush(active, (entry["end"] - entry["start"], entry["start"], index, entry["qualified_name"], entry))
            next_definition += 1
        while active and active[0][4]["end"] <= start:
            heapq.heappop(active)
        owner = active[0][4] if active else None
        segments.append({"start": start, "end": end, "owner": owner})

    groups: dict[str, dict[str, Any]] = {}
    for definition in definitions:
        gid = "ITEM-" + digest(file_key + ["symbol", definition["qualified_name"], definition["kind"], definition["ordinal"], definition["start"], definition["end"]])[:40]
        definition["id"] = gid
        groups[gid] = {**definition, "id": gid, "atom_ids": []}
    # Record direct nesting with one stack pass.  Parent groups receive child
    # atoms below, while the atom denominator itself stays disjoint.
    stack: list[dict[str, Any]] = []
    for definition in definitions:
        while stack and stack[-1]["end"] <= definition["start"]:
            stack.pop()
        definition["parent_definition"] = stack[-1] if stack else None
        stack.append(definition)
    atoms: list[dict[str, Any]] = []
    for segment in segments:
        owner = segment["owner"]
        owner_id = owner["id"] if owner else None
        atom_id = "ITEM-" + digest(file_key + ["atom", segment["start"], segment["end"], owner_id])[:40]
        atom_body = {
            "type": "byte_atom", "leaf": True, "path": path,
            "byte_start": segment["start"], "byte_end": segment["end"],
            "text_digest": digest(raw[segment["start"]:segment["end"]]),
            "owner_symbol": owner_id,
            "source_span": _source_span(source_id, blob, raw, segment["start"], segment["end"], bom, unicode_positions),
        }
        atom = _item(atom_id, 0, "atom", path, "known", segment["start"], segment["end"], atom_body)
        atoms.append(atom)
        if owner_id:
            groups[owner_id]["atom_ids"].append(atom_id)
    # Parent groups are unions, not additional denominator leaves.  The
    # explicitly stored atom set makes class-child and nested definitions
    # inspectable without double-counting the child's bytes.
    # Children start at or after their parent.  Reverse start/size order
    # guarantees child unions are complete before their parent is propagated.
    for definition in sorted(definitions, key=lambda value: (value["start"], value["end"] - value["start"]), reverse=True):
        group = groups[definition["id"]]
        group["atom_ids"] = sorted(set(group["atom_ids"]))
        parent = definition.get("parent_definition")
        if parent is not None:
            groups[parent["id"]]["atom_ids"].extend(group["atom_ids"])
    group_items: list[dict[str, Any]] = []
    ordinal = 1 + len(atoms)
    for definition in definitions:
        body = {"type": "symbol_group", "leaf": False, "path": path, "symbol_id": definition["id"],
                "qualified_name": definition["qualified_name"], "name": definition["name"],
                "kind": definition["kind"], "ordinal": definition["ordinal"], "parent": definition["parent"],
                "byte_start": definition["start"], "byte_end": definition["end"],
                "signature_hash": definition["signature_hash"], "ast_hash": definition["ast_hash"],
                "atom_ids": groups[definition["id"]]["atom_ids"],
                "source_span": _source_span(source_id, blob, raw, definition["start"], definition["end"], bom, unicode_positions)}
        group_items.append(_item(definition["id"], ordinal, "symbol", path, "known", definition["start"], definition["end"], body))
        ordinal += 1
    # File item precedes atoms; atoms sort by physical range.  This order is
    # stable for same-name overloads and makes independent reassembly simple.
    atoms.sort(key=lambda value: (value["start_byte"], value["end_byte"], value["id"]))
    for index, atom in enumerate(atoms, 1):
        atom["ordinal"] = index
    result = [file_item, *atoms, *group_items]
    return result, {"file": file_item, "leaf_count": len(atoms), "unknown_count": 0, "atom_ids": [item["id"] for item in atoms]}


def partition_python(raw: bytes, path: str = "source.py", source_id: str | None = None, blob: str | None = None) -> dict[str, Any]:
    """Public deterministic AST/byte partition helper used by focused tests."""
    blob = blob or digest(raw)
    items, stats = _code_file_items("PARTITION", 1, path, raw, blob, None, 0o100644, source_id)
    return {"items": items, "leaf_ids": stats["atom_ids"], "leaf_count": stats["leaf_count"], "raw_digest": blob}


def _document_line_items(set_id: str, revision_no: int, raw: bytes, blob: str, source_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    unicode_positions = _unicode_offset_map(raw, {0, len(raw)}) if raw else {}
    try:
        raw.decode("utf-8-sig")
        valid_utf8 = True
    except UnicodeDecodeError:
        valid_utf8 = False
    file_id = "ITEM-" + digest([set_id, revision_no, "document", blob, "file"])[:40]
    file_body = {"type": "document", "leaf": False, "bytes": len(raw), "blob_digest": blob,
                 "line_count": 0, "source_span": _source_span(source_id, blob, raw, 0, len(raw), bom, unicode_positions) if raw else None}
    file_item = _item(file_id, 0, "file", None, "known", 0, len(raw), file_body)
    if not raw:
        file_item["body"] = {**file_body, "leaf": True, "empty": True}
        file_item["digest"] = digest(file_item["body"])
        return [file_item], {"leaf_count": 1, "unknown_count": 0}
    starts = _line_starts(raw)
    line_boundaries = set(starts) | {len(raw)}
    for line_start in starts:
        newline_end = raw.find(b"\n", line_start)
        line_boundaries.add(len(raw) if newline_end < 0 else newline_end + 1)
    unicode_positions = _unicode_offset_map(raw, line_boundaries)
    items = [file_item]
    for line_no, start in enumerate(starts, 1):
        newline_end = raw.find(b"\n", start)
        end = len(raw) if newline_end < 0 else newline_end + 1
        # BOM is retained in the raw range and is part of the canonical source
        # character coordinates used by Knowledge.source/source.read.
        try:
            unicode_start = unicode_positions.get(start)
            unicode_end = unicode_positions.get(end)
            need(unicode_start is not None and unicode_end is not None, "invalid_encoding", "Source line is not valid UTF-8")
        except Fault:
            # Invalid UTF-8 has exact byte coordinates but no trustworthy
            # Unicode coordinate.  Do not carry a guessed cursor into the
            # immutable source span.
            unicode_start = unicode_end = None
        line_id = "ITEM-" + digest([set_id, revision_no, "line", line_no, start, end, digest(raw[start:end])])[:40]
        body = {"type": "physical_line", "leaf": True, "line": line_no,
                "byte_start": start, "byte_end": end, "unicode_start": unicode_start,
                "unicode_end": unicode_end, "text_digest": digest(raw[start:end]),
                "source_span": _source_span(source_id, blob, raw, start, end, bom, unicode_positions)
                if unicode_start is not None and unicode_end is not None else None}
        if not valid_utf8:
            body["unknown_reason"] = "invalid_utf8"
        items.append(_item(line_id, line_no, "line", None, "known" if valid_utf8 else "unknown", start, end, body))
    file_item["body"]["line_count"] = len(items) - 1
    file_item["digest"] = digest(file_item["body"])
    return items, {"leaf_count": len(items) - 1, "unknown_count": 0 if valid_utf8 else len(items) - 1}


def partition_document(raw: bytes, source_id: str | None = None, blob: str | None = None) -> dict[str, Any]:
    blob = blob or digest(raw)
    items, stats = _document_line_items("PARTITION", 1, raw, blob, source_id)
    return {"items": items, "leaf_ids": [item["id"] for item in items if item["body"].get("leaf")], "leaf_count": stats["leaf_count"], "raw_digest": blob}


def _cursor_encode(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(canonical(value)).decode("ascii").rstrip("=")


def _cursor_decode(value: str) -> dict[str, Any]:
    need(isinstance(value, str) and len(value) <= 16384, "invalid_cursor", "Cursor is malformed")
    try:
        padded = value + "=" * (-len(value) % 4)
        result = parse_json(base64.urlsafe_b64decode(padded.encode("ascii")), limit=65536)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise Fault("invalid_cursor", "Cursor is malformed") from exc
    need(isinstance(result, dict) and result.get("format") == "traceability.cursor.v1", "invalid_cursor", "Cursor format is unsupported")
    return result


def _page_limit(value: Any) -> int:
    need(type(value) is int and 1 <= value <= MAX_PAGE, "invalid_range", f"limit must be between 1 and {MAX_PAGE}")
    return value


def _scan_hashes(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if _HEX64.fullmatch(value):
            found.add(value)
        else:
            found.update(re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", value))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_scan_hashes(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_scan_hashes(item))
    return found


def _trace_blob_refs(value: Any, key: str | None = None) -> set[str]:
    """Collect CAS refs while ignoring ordinary record/body digests.

    Every producer uses one of the explicit blob-shaped keys below.  Scanning
    every 64-hex string would incorrectly treat immutable row digests as
    missing CAS leaves and would make archives depend on implementation
    details of unrelated history tables.
    """
    refs: set[str] = set()
    # Runtime provenance uses named leaves for observed output, report and
    # retained work-product bytes.  These are explicit CAS fields; ordinary
    # digests and private environment values are intentionally not followed.
    blob_keys = {"blob", "blob_digest", "raw_digest", "commit_blob", "tree_blob", "source_blob", "pins", "sha256",
                 "input_digest",
                 "stdout_blob", "stderr_blob", "report_blob", "snapshot_blob", "changes_blob", "manifest_blob"}
    if isinstance(value, str):
        if key in blob_keys and _HEX64.fullmatch(value):
            refs.add(value)
    elif isinstance(value, dict):
        for child_key, child in value.items():
            refs.update(_trace_blob_refs(child, child_key))
    elif isinstance(value, list):
        for child in value:
            refs.update(_trace_blob_refs(child, key))
    return refs


def _row_json(row: dict[str, Any], body_keys: Iterable[str] = ("body", "result")) -> dict[str, Any]:
    result = dict(row)
    for key in body_keys:
        if key in result and isinstance(result[key], str):
            try:
                result[key] = parse_json(result[key])
            except Fault:
                pass
    return result


def _candidate_context_row(section: str, row: dict[str, Any]) -> dict[str, Any]:
    """Add immutable derived checksums to portable candidate context rows."""
    result = _row_json(row)
    if section in {"tasks", "candidates", "runs", "receipts"} and isinstance(result.get("body"), dict):
        result["body_digest"] = digest(result["body"])
    if section == "runs" and result.get("result") is not None:
        result["result_digest"] = digest(result["result"])
    return result


def _bounded_history_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("body", "result"):
        if key not in result or not isinstance(result[key], str):
            continue
        try:
            decoded = parse_json(result[key], limit=MAX_REVISION_BODY_BYTES)
        except Fault:
            # Preserve the fact that an old/corrupt record could not be
            # decoded without returning an unbounded raw JSON string.
            result[key] = {"detail": f"traceability://record/{row.get('id')}/{key}", "decoding": "range_read_required"}
            result[f"{key}_truncated_fields"] = [key]
            continue
        bounded, truncated = _bounded_metadata(decoded)
        result[key] = bounded
        if truncated:
            result[f"{key}_truncated_fields"] = truncated
    return result


class Traceability:
    def __init__(self, control):
        self.c = control
        self.s = control.s

    # ---------- readonly structural progress ----------
    @_read_transaction
    def structural_progress_projection(self, project: str) -> dict[str, Any]:
        """Return the immutable traceability meaning used by Supervisor.

        Traceability has several append-only storage identities (TPROP/TREV,
        TDEC/TMAP/TBIND and TREC).  They are useful for history and CAS
        validation, but a repeated proposal of the same logical input must
        not make Supervisor progress merely because a new generated ID was
        allocated.  This reader validates every row in one transaction and
        replaces only the generated self/companion links with deterministic
        typed identities.  External references, revision populations and
        actual delivery observations remain exact.
        """
        self.s.one("SELECT id FROM projects WHERE id=?", (project,), True)
        tables = {
            "sets": self.s.all("SELECT * FROM traceability_sets WHERE project=? ORDER BY id", (project,)),
            "revisions": self.s.all("SELECT * FROM traceability_revisions WHERE project=? ORDER BY set_id,revision,id", (project,)),
            "items": self.s.all("SELECT * FROM traceability_items WHERE project=? ORDER BY revision,ordinal,id", (project,)),
            "proposals": self.s.all("SELECT * FROM traceability_proposals WHERE project=? ORDER BY set_id,created,id", (project,)),
            "decisions": self.s.all("SELECT * FROM traceability_decisions WHERE project=? ORDER BY revision,created,id", (project,)),
            "mappings": self.s.all("SELECT * FROM traceability_mappings WHERE project=? ORDER BY revision,created,id", (project,)),
            "bindings": self.s.all("SELECT * FROM traceability_bindings WHERE project=? ORDER BY revision,created,id", (project,)),
            "records": self.s.all("SELECT * FROM traceability_records WHERE project=? ORDER BY created,id", (project,)),
        }
        valid_status = {
            "proposals": {"proposed", "staging", "ready", "failed", "adopted", "withdrawn"},
            "revisions": {"staging", "failed", "ready", "active", "superseded"},
            "decisions": {"proposed", "accepted", "rejected", "stale"},
            "mappings": {"proposed", "accepted", "stale"},
            "bindings": {"pending", "mandatory", "stale", "withdrawn"},
        }
        valid_kinds = {"population", "code", "document", "decision", "mapping", "scope"}
        known_record_kinds = {
            "proposal", "extracted", "extraction_checkpoint", "extraction_failed",
            "review_packet", "decision_proposed", "mapping_proposed", "scope_proposed",
            "population_adopted", "decision_adopted", "mapping_adopted", "binding_adopted",
            "closure_proposed", "closure_adopted", "delivered_mapping", "withdrawn",
        }

        def decode(row: dict[str, Any], table: str) -> dict[str, Any]:
            raw = row.get("body")
            need(isinstance(raw, str), "integrity_error", "Traceability body is not stored JSON", {"table": table, "id": row.get("id")})
            body = parse_json(raw, limit=MAX_REVISION_BODY_BYTES)
            need(isinstance(body, dict), "integrity_error", "Traceability body is not an object", {"table": table, "id": row.get("id")})
            need(digest(body) == row.get("digest"), "integrity_error", "Traceability body digest differs", {"table": table, "id": row.get("id")})
            return body

        def cas(value: Any, owner: Any) -> None:
            need(isinstance(value, str) and _HEX64.fullmatch(value), "integrity_error", "Traceability CAS reference is malformed", owner)
            self.s.blob_get(value)

        set_rows = tables["sets"]
        set_by_id: dict[str, dict[str, Any]] = {}
        for row in set_rows:
            need(row.get("project") == project and row.get("kind") in {"population", "code", "document"}
                 and isinstance(row.get("name"), str) and row["name"],
                 "integrity_error", "Traceability set identity is malformed", row.get("id"))
            need(row["id"] not in set_by_id, "integrity_error", "Duplicate traceability set identity", row.get("id"))
            set_by_id[row["id"]] = row

        def set_identity(set_id: str) -> dict[str, Any]:
            row = set_by_id.get(set_id)
            need(row is not None, "integrity_error", "Traceability set reference is missing", set_id)
            return {"project": project, "name": row["name"], "kind": row["kind"]}

        revision_by_id: dict[str, dict[str, Any]] = {}
        revision_body_by_id: dict[str, dict[str, Any]] = {}
        for row in tables["revisions"]:
            need(row.get("project") == project and row.get("set_id") in set_by_id
                 and row.get("status") in valid_status["revisions"]
                 and type(row.get("revision")) is int and row["revision"] > 0,
                 "integrity_error", "Traceability revision row is malformed", row.get("id"))
            body = decode(row, "traceability_revisions")
            need(body.get("format") == TRACEABILITY_FORMAT and body.get("revision") == row["id"]
                 and body.get("set_id") == row["set_id"] and body.get("project") == project
                 and body.get("kind") == set_by_id[row["set_id"]]["kind"]
                 and isinstance(body.get("scope"), dict),
                 "integrity_error", "Traceability revision identity differs", row.get("id"))
            pins = body.get("pins")
            need(isinstance(pins, list) and all(isinstance(pin, str) for pin in pins)
                 and len(pins) == len(set(pins)),
                 "integrity_error", "Traceability revision CAS pin list is malformed", row.get("id"))
            for pin in pins:
                cas(pin, row["id"])
            inventory = body.get("inventory")
            need(isinstance(inventory, list), "integrity_error", "Traceability revision inventory is malformed", row.get("id"))
            for entry in inventory:
                need(isinstance(entry, dict), "integrity_error", "Traceability inventory entry is malformed", row["id"])
                for key in ("sha256", "git_object_blob"):
                    if entry.get(key) is not None:
                        cas(entry[key], {"revision": row["id"], "field": key})
            git_pin = body.get("git_pin")
            if git_pin is not None:
                need(isinstance(git_pin, dict), "integrity_error", "Traceability Git pin is malformed", row["id"])
                for key in ("commit_blob", "tree_blob"):
                    if git_pin.get(key) is not None:
                        cas(git_pin[key], {"revision": row["id"], "field": key})
                trees = git_pin.get("trees", [])
                need(isinstance(trees, list), "integrity_error", "Traceability Git tree pin list is malformed", row["id"])
                for tree in trees:
                    need(isinstance(tree, dict), "integrity_error", "Traceability Git tree pin is malformed", row["id"])
                    if tree.get("blob") is not None:
                        cas(tree["blob"], {"revision": row["id"], "field": "trees.blob"})
            revision_by_id[row["id"]] = row
            revision_body_by_id[row["id"]] = body

        def revision_identity(revision_id: str | None) -> dict[str, Any] | None:
            if revision_id is None:
                return None
            row = revision_by_id.get(revision_id)
            need(row is not None, "integrity_error", "Traceability revision reference is missing", revision_id)
            need(isinstance(row.get("digest"), str) and _HEX64.fullmatch(row["digest"])
                 and isinstance(row.get("population_digest"), str) and _HEX64.fullmatch(row["population_digest"]),
                 "integrity_error", "Traceability revision digest identity is malformed", revision_id)
            return {"project": project, "set": set_identity(row["set_id"]),
                    "revision": row["revision"], "digest": row["digest"],
                    "population_digest": row["population_digest"]}

        item_by_id: dict[str, dict[str, Any]] = {}
        item_body_by_id: dict[str, dict[str, Any]] = {}
        items_by_revision: dict[str, list[dict[str, Any]]] = {key: [] for key in revision_by_id}
        for row in tables["items"]:
            need(row.get("project") == project and row.get("revision") in revision_by_id
                 and row.get("item_kind") in {"file", "atom", "symbol", "line", "group"}
                 and row.get("status") in {"known", "unknown", "tombstone"}
                 and (row.get("path") is None or isinstance(row.get("path"), str)) and isinstance(row.get("digest"), str)
                 and _HEX64.fullmatch(row["digest"])
                 and type(row.get("start_byte")) is int and row["start_byte"] >= 0
                 and type(row.get("end_byte")) is int and row["end_byte"] >= row["start_byte"]
                 and type(row.get("ordinal")) is int and row["ordinal"] >= 0
                 and type(row.get("leaf")) is int and row["leaf"] in {0, 1},
                 "integrity_error", "Traceability item row is malformed", row.get("id"))
            body = decode(row, "traceability_items")
            need(type(body.get("leaf")) is bool and int(body["leaf"]) == row["leaf"],
                 "integrity_error", "Traceability item leaf flag differs", row.get("id"))
            for ref in _trace_blob_refs(body):
                cas(ref, row["id"])
            item_by_id[row["id"]] = row
            item_body_by_id[row["id"]] = body
            items_by_revision[row["revision"]].append(row)
        for revision_id, rows in items_by_revision.items():
            ordinals = [row["ordinal"] for row in rows]
            need(len(ordinals) == len(set(ordinals)), "integrity_error", "Traceability population has duplicate ordinals", revision_id)
            rows.sort(key=lambda value: (value["ordinal"], value["id"]))
        for row in tables["items"]:
            body = item_body_by_id[row["id"]]
            for field in ("parent", "owner_symbol", "symbol_id"):
                ref = body.get(field)
                if ref is not None:
                    need(ref in item_by_id and item_by_id[ref]["revision"] == row["revision"],
                         "integrity_error", "Traceability item parent reference is foreign", row["id"])
            atom_ids = body.get("atom_ids")
            if atom_ids is not None:
                need(isinstance(atom_ids, list), "integrity_error", "Traceability item atom references are malformed", row["id"])
                for ref in atom_ids:
                    need(ref in item_by_id and item_by_id[ref]["revision"] == row["revision"],
                         "integrity_error", "Traceability item atom reference is foreign", row["id"])

        proposal_by_id: dict[str, dict[str, Any]] = {}
        proposal_body_by_id: dict[str, dict[str, Any]] = {}
        for row in tables["proposals"]:
            need(row.get("project") == project and row.get("set_id") in set_by_id
                 and row.get("kind") in valid_kinds and row.get("status") in valid_status["proposals"],
                 "integrity_error", "Traceability proposal row is malformed", row.get("id"))
            need(isinstance(row.get("digest"), str) and _HEX64.fullmatch(row["digest"])
                 and isinstance(row.get("semantic_material_digest"), str)
                 and _HEX64.fullmatch(row["semantic_material_digest"]),
                 "integrity_error", "Traceability proposal digest identity is malformed", row.get("id"))
            body = decode(row, "traceability_proposals")
            need(body.get("format") == TRACEABILITY_FORMAT and body.get("id") == row["id"]
                 and body.get("project") == project and body.get("set_id") == row["set_id"]
                 and body.get("kind") == row["kind"],
                 "integrity_error", "Traceability proposal identity differs", row.get("id"))
            expected_active = row.get("expected_active")
            if isinstance(expected_active, str):
                expected_active = parse_json(expected_active, limit=MAX_REVISION_BODY_BYTES)
            need(expected_active == body.get("expected_active"), "integrity_error", "Traceability expected-active identity differs", row.get("id"))
            need(row.get("semantic_material_digest") == digest({"kind": body.get("kind"), "scope": body.get("scope"), "adapter": body.get("adapter")}),
                 "integrity_error", "Traceability proposal semantic digest differs", row.get("id"))
            result = row.get("result")
            if isinstance(result, str):
                result = parse_json(result, limit=MAX_REVISION_BODY_BYTES)
            if result is not None:
                need(isinstance(result, dict), "integrity_error", "Traceability proposal result is not an object", row["id"])
                for ref in _trace_blob_refs(result):
                    cas(ref, row["id"])
            proposal_by_id[row["id"]] = row
            proposal_body_by_id[row["id"]] = body

        def proposal_identity(proposal_id: str | None) -> dict[str, Any] | None:
            if proposal_id is None:
                return None
            row = proposal_by_id.get(proposal_id)
            need(row is not None, "integrity_error", "Traceability proposal reference is missing", proposal_id)
            body = proposal_body_by_id[proposal_id]
            return {"table": "traceability_proposals", "kind": row["kind"],
                    "set": set_identity(row["set_id"]), "material_digest": row["semantic_material_digest"],
                    "body_digest": digest(normalize_body(body, "traceability_proposals", row))}

        def item_identity(item_id: str) -> dict[str, Any]:
            row = item_by_id.get(item_id)
            need(row is not None, "integrity_error", "Traceability item reference is missing", item_id)
            return {"revision": revision_identity(row["revision"]), "ordinal": row["ordinal"],
                    "item_kind": row["item_kind"], "path": row["path"], "digest": row["digest"]}

        def subject_identity(table: str, ident: str) -> dict[str, Any]:
            if table == "traceability_proposals":
                return proposal_identity(ident)
            table_key = {"traceability_decisions": "decisions", "traceability_mappings": "mappings",
                         "traceability_bindings": "bindings", "traceability_records": "records",
                         "traceability_revisions": "revisions", "traceability_items": "items"}.get(table)
            need(table_key is not None, "integrity_error", "Unknown traceability subject table", table)
            source = {"decisions": tables["decisions"], "mappings": tables["mappings"],
                      "bindings": tables["bindings"], "records": tables["records"],
                      "revisions": tables["revisions"], "items": tables["items"]}[table_key]
            row = next((value for value in source if value.get("id") == ident), None)
            need(row is not None, "integrity_error", "Traceability subject reference is missing", {"table": table, "id": ident})
            if table == "traceability_revisions":
                return {"table": table, "identity": revision_identity(ident)}
            if table == "traceability_items":
                return {"table": table, "identity": item_identity(ident)}
            body = decoded_rows[table][ident]
            return {"table": table, "revision": revision_identity(row.get("revision")),
                    "kind": body.get("kind"),
                    # Companion identities are derived from their normalized
                    # body.  The raw digest includes the generated TDEC/
                    # TMAP/TBIND ID and therefore cannot be a duplicate key.
                    "body_digest": digest(normalize_body(body, table, row))}

        decoded_rows: dict[str, dict[str, dict[str, Any]]] = {
            "traceability_decisions": {}, "traceability_mappings": {}, "traceability_bindings": {},
            "traceability_records": {},
        }
        for key in decoded_rows:
            table_name = key
            for row in tables[{"traceability_decisions": "decisions", "traceability_mappings": "mappings",
                               "traceability_bindings": "bindings", "traceability_records": "records"}[key]]:
                decoded_rows[key][row["id"]] = decode(row, table_name)

        def normalize_ref(value: Any) -> Any:
            if not isinstance(value, dict) or value.get("ref_type") is not None:
                return normalize_value(value)
            table = value.get("table")
            ident = value.get("id")
            if isinstance(table, str) and isinstance(ident, str) and table.startswith("traceability_"):
                need(table in tables_by_table,
                     "integrity_error", "Unknown Traceability subject table", table)
                need(ident in tables_by_table[table],
                     "integrity_error", "Traceability subject reference is missing", value)
                need(value.get("digest") == tables_by_table[table][ident].get("digest"),
                     "integrity_error", "Traceability subject digest differs", value)
                return subject_identity(table, ident)
            return normalize_value(value)

        tables_by_table = {
            "traceability_sets": {row["id"]: row for row in set_rows},
            "traceability_revisions": {row["id"]: row for row in tables["revisions"]},
            "traceability_items": item_by_id,
            "traceability_proposals": proposal_by_id,
            "traceability_decisions": {row["id"]: row for row in tables["decisions"]},
            "traceability_mappings": {row["id"]: row for row in tables["mappings"]},
            "traceability_bindings": {row["id"]: row for row in tables["bindings"]},
            "traceability_records": {row["id"]: row for row in tables["records"]},
        }

        def normalize_value(value: Any, key: str | None = None) -> Any:
            if isinstance(value, dict):
                structural_wrapper = (
                    value.get("typed_ref") is not None
                    or value.get("type") in {"file", "byte_atom", "symbol_group"}
                    or (value.get("ref_type") == "source_span" and value.get("source_id") is None)
                    or value.get("ref_type") == "git_atom"
                )
                if value.get("ref_type") is not None and not structural_wrapper:
                    return json.loads(canonical(value))
                return {name: normalize_value(child, name) for name, child in value.items()}
            if isinstance(value, list):
                return [normalize_value(child, key) for child in value]
            return value

        def normalize_generated(value: Any, key: str | None = None) -> Any:
            """Normalize a generated graph field, including its known children."""
            if isinstance(value, dict):
                structural_wrapper = (
                    value.get("typed_ref") is not None
                    or value.get("type") in {"file", "byte_atom", "symbol_group"}
                    or (value.get("ref_type") == "source_span" and value.get("source_id") is None)
                    or value.get("ref_type") == "git_atom"
                )
                if value.get("ref_type") is not None and not structural_wrapper:
                    return json.loads(canonical(value))
                if key in {"subject_ref", "root_subject", "closure_ref", "decision_ref"} and value.get("table"):
                    if isinstance(value.get("id"), str) and value["id"].startswith("$"):
                        return json.loads(canonical(value))
                    return normalize_ref(value)
                return {name: normalize_generated(child, name) for name, child in value.items()}
            if isinstance(value, list):
                if key in {"leaf_ids", "required_leaf_ids", "atom_ids"}:
                    return [item_identity(child) if isinstance(child, str) and child in item_by_id else child for child in value]
                return [normalize_generated(child, key) for child in value]
            if isinstance(value, str):
                if key in {"set_id"} and value in set_by_id:
                    return set_identity(value)
                if key in {"revision", "revision_id", "pin_revision"} and value in revision_by_id:
                    return revision_identity(value)
                if key in {"proposal", "proposal_id"} and value in proposal_by_id:
                    # A body-level proposal link is the generated companion
                    # role.  The enclosing projection carries the full
                    # proposal identity where an external relationship is
                    # intended.
                    return "$proposal"
                if key == "decision_id" and value in tables_by_table["traceability_decisions"]:
                    return "$decision"
                if key == "mapping_id" and value in tables_by_table["traceability_mappings"]:
                    return "$mapping"
                if key == "binding_id" and value in tables_by_table["traceability_bindings"]:
                    return "$binding"
                if key in {"item", "parent", "symbol_id", "owner_symbol"} and value in item_by_id:
                    return item_identity(value)
            return value

        def normalize_top_level(value: dict[str, Any], generated_fields: set[str]) -> dict[str, Any]:
            return {name: normalize_generated(child, name) if name in generated_fields
                    else normalize_value(child, name)
                    for name, child in value.items()}

        def normalize_body(body: dict[str, Any], table: str, row: dict[str, Any]) -> dict[str, Any]:
            value = json.loads(canonical(body))
            if table == "traceability_revisions":
                value["revision"] = revision_identity(row["id"])
                value["set_id"] = set_identity(row["set_id"])
            elif table == "traceability_proposals":
                value["id"] = "$proposal"
                value["set_id"] = set_identity(row["set_id"])
            elif table in {"traceability_decisions", "traceability_mappings", "traceability_bindings"}:
                value["id"] = "$" + {"traceability_decisions": "decision", "traceability_mappings": "mapping", "traceability_bindings": "binding"}[table]
            elif table == "traceability_records":
                if value.get("closure_id") == row.get("id"):
                    value["closure_id"] = "$record"
                for key in ("subject_ref", "root_subject", "closure_ref"):
                    ref = value.get(key)
                    if isinstance(ref, dict) and ref.get("table") == "traceability_records" and ref.get("id") == row.get("id"):
                        value[key] = {"table": "traceability_records", "id": "$record"}
            # These are the known nested generated relationships in the
            # companion formats.  Arbitrary user dictionaries (including
            # ``extra``) are intentionally left byte-for-byte semantic so a
            # user field named ``proposal_id`` or ``item`` is never mistaken
            # for a generated graph edge.
            if table == "traceability_decisions" and isinstance(value.get("decisions"), list):
                for entry in value["decisions"]:
                    if isinstance(entry, dict) and isinstance(entry.get("item"), str) and entry["item"] in item_by_id:
                        entry["item"] = item_identity(entry["item"])
            if table == "traceability_mappings" and isinstance(value.get("mappings"), list):
                for edge in value["mappings"]:
                    if not isinstance(edge, dict):
                        continue
                    if isinstance(edge.get("leaf_ids"), list):
                        edge["leaf_ids"] = [item_identity(leaf) if isinstance(leaf, str) and leaf in item_by_id else leaf
                                             for leaf in edge["leaf_ids"]]
                    # decision_ref is an external dependency of a mapping;
                    # its exact TDEC id/digest distinguishes two otherwise
                    # similar mapping operations and is retained verbatim.
            generated_fields = {
                "proposal", "proposal_id", "revision", "revision_id", "pin_revision", "set_id",
                "required_leaf_ids", "leaf_ids", "atom_ids", "parent", "owner_symbol", "symbol_id",
                "item", "subject_ref", "root_subject", "closure_ref", "decision_ref",
                "decision_id", "mapping_id", "binding_id",
            }
            return normalize_top_level(value, generated_fields)

        # Validate the companion rows and every external typed reference while
        # their immutable FK and population identities are available.
        observer = Actor("traceability-structural-progress", "observer", project)
        from .traceability_refs import TraceabilityRefResolver
        resolver = TraceabilityRefResolver(self.c)

        blob_keys = {"blob", "blob_digest", "raw_digest", "commit_blob", "tree_blob", "source_blob", "pins",
                     "sha256", "input_digest", "stdout_blob", "stderr_blob", "report_blob", "snapshot_blob",
                     "changes_blob", "manifest_blob"}

        def validate_blob_fields(value: Any, owner: Any, key: str | None = None) -> None:
            if key == "pins":
                need(isinstance(value, list), "integrity_error", "Traceability CAS pin collection is malformed", owner)
                for child in value:
                    cas(child, {"owner": owner, "field": key})
                return
            if key in blob_keys:
                cas(value, {"owner": owner, "field": key})
                return
            if isinstance(value, dict):
                for child_key, child in value.items():
                    # Proposal ``extra`` is caller-owned meaning.  Its keys
                    # deliberately do not acquire CAS or typed-reference
                    # semantics merely by matching a storage field name.
                    if child_key == "extra":
                        continue
                    validate_blob_fields(child, owner, child_key)
            elif isinstance(value, list):
                for child in value:
                    validate_blob_fields(child, owner, key)

        def validate_external(value: Any, owner: Any) -> None:
            if isinstance(value, dict):
                ref_type = value.get("ref_type")
                # Extracted item bodies carry an adapter-owned wrapper around
                # their historical source span (and a legacy ``git_atom``
                # kind).  It is structural population data, not a public
                # target ref: validating it through the public resolver would
                # incorrectly require pin_revision fields that the adapter
                # never stored.  The wrapper, its complete body digest, and
                # every named CAS leaf are still validated below.
                internal_wrapper = (
                    value.get("typed_ref") is not None
                    or value.get("type") in {"file", "byte_atom", "symbol_group"}
                    or (ref_type == "source_span" and value.get("source_id") is None)
                    or ref_type == "git_atom"
                )
                if ref_type is not None and not internal_wrapper:
                    resolver.resolve(observer, project, value, require_current=False)
                    return
                for child_key, child in value.items():
                    # ``extra`` is an opaque proposal payload.  Only the
                    # format-owned fields below are eligible for typed
                    # resolution; arbitrary user payloads retain meaning.
                    if child_key == "extra":
                        continue
                    validate_external(child, owner)
            elif isinstance(value, list):
                for child in value:
                    validate_external(child, owner)

        for row in tables["revisions"]:
            validate_external(revision_body_by_id[row["id"]], row["id"])
            validate_blob_fields(revision_body_by_id[row["id"]], row["id"])
        for row in tables["items"]:
            validate_external(item_body_by_id[row["id"]], row["id"])
            validate_blob_fields(item_body_by_id[row["id"]], row["id"])
        for row in tables["proposals"]:
            validate_external(proposal_body_by_id[row["id"]], row["id"])
            validate_blob_fields(proposal_body_by_id[row["id"]], row["id"])
            result = row.get("result")
            if isinstance(result, str):
                result = parse_json(result, limit=MAX_REVISION_BODY_BYTES)
            if result is not None:
                validate_blob_fields(result, row["id"])

        def validate_delivered_history(row: dict[str, Any], body: dict[str, Any]) -> None:
            """Check a delivered observation without asking whether it is current."""
            mapping_id = body.get("mapping_id")
            delivery_id = body.get("delivery")
            mapping = tables_by_table["traceability_mappings"].get(mapping_id)
            need(mapping is not None and mapping["project"] == project,
                 "integrity_error", "Delivered mapping source is missing", row["id"])
            need(row.get("revision") == mapping.get("revision"),
                 "integrity_error", "Delivered mapping revision differs", row["id"])
            need(body.get("subject_ref") == {"table": "traceability_mappings", "id": mapping_id,
                                              "digest": mapping["digest"]},
                 "integrity_error", "Delivered mapping subject differs", row["id"])
            delivery = self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery_id, project))
            need(delivery is not None, "integrity_error", "Delivered mapping delivery is missing", row["id"])
            delivery_body = parse_json(delivery["body"], limit=MAX_REVISION_BODY_BYTES)
            need(isinstance(delivery_body, dict) and isinstance(delivery_body.get("binding"), dict)
                 and digest(delivery_body["binding"]) == delivery["digest"],
                 "integrity_error", "Delivered mapping delivery digest differs", delivery_id)
            need(body.get("delivery_digest") == delivery["digest"],
                 "integrity_error", "Delivered mapping delivery identity differs", row["id"])
            # Delivery rows retain a mutable operational body as checks and
            # commits are observed.  The delivered record owns the historical
            # commit_refs/actual destination; only the frozen snapshot
            # identity is compared here, so an old observation remains
            # readable after a later delivery attempt.
            snapshot = delivery_body.get("snapshot")
            need(isinstance(snapshot, dict),
                 "integrity_error", "Delivered mapping snapshot is malformed", delivery_id)
            need(isinstance(body.get("commit_refs"), dict)
                 and body.get("snapshot_digest") == snapshot.get("digest"),
                 "integrity_error", "Delivered mapping snapshot identity differs", row["id"])
            actual = body.get("actual")
            need(isinstance(actual, list) and actual,
                 "integrity_error", "Delivered mapping observation is empty", row["id"])
            material = {"mapping_id": mapping_id, "mapping_digest": mapping["digest"],
                        "delivery": delivery_id, "delivery_digest": delivery["digest"],
                        "snapshot_digest": body.get("snapshot_digest"),
                        "commit_refs": body.get("commit_refs"), "actual": actual}
            need(body.get("material_digest") == digest(material),
                 "integrity_error", "Delivered mapping material digest differs", row["id"])

        for table_name, key in (("traceability_decisions", "decisions"), ("traceability_mappings", "mappings"), ("traceability_bindings", "bindings")):
            for row in tables[key]:
                body = decoded_rows[table_name][row["id"]]
                need(row.get("revision") in revision_by_id and body.get("project") == project
                     and body.get("format") == TRACEABILITY_FORMAT
                     and body.get("id") == row["id"] and body.get("revision") == row["revision"],
                     "integrity_error", "Traceability companion identity differs", row["id"])
                proposal_id = body.get("proposal")
                prop = proposal_by_id.get(proposal_id)
                need(prop is not None and prop["kind"] == {"traceability_decisions": "decision", "traceability_mappings": "mapping", "traceability_bindings": "scope"}[table_name],
                     "integrity_error", "Traceability companion proposal differs", row["id"])
                if table_name == "traceability_decisions":
                    need(isinstance(body.get("decisions"), list) and isinstance(body.get("required_leaf_ids"), list), "integrity_error", "Decision body is malformed", row["id"])
                elif table_name == "traceability_mappings":
                    need(isinstance(body.get("mappings"), list) and isinstance(body.get("required_leaf_ids"), list), "integrity_error", "Mapping body is malformed", row["id"])
                    for edge in body["mappings"]:
                        need(isinstance(edge, dict) and isinstance(edge.get("decision_ref"), dict), "integrity_error", "Mapping decision reference is malformed", row["id"])
                        ref = edge["decision_ref"]
                        need(ref.get("table") == "traceability_decisions" and ref.get("id") in decoded_rows["traceability_decisions"]
                             and ref.get("digest") == tables_by_table["traceability_decisions"][ref["id"]]["digest"], "integrity_error", "Mapping decision reference differs", row["id"])
                else:
                    need(body.get("kind") == "scope_binding" and isinstance(body.get("scope_requirement"), dict), "integrity_error", "Scope binding body is malformed", row["id"])
                for leaf in body.get("required_leaf_ids", []):
                    need(leaf in item_by_id and item_by_id[leaf]["revision"] == row["revision"], "integrity_error", "Traceability companion leaf is foreign", row["id"])
                validate_external(body, row["id"])
                validate_blob_fields(body, row["id"])

        # A typed proposal and its companion are one generated operation.  A
        # body that merely names a plausible role while the companion is
        # absent is corrupt history, not an empty logical proposal.
        for row in tables["proposals"]:
            body = proposal_body_by_id[row["id"]]
            if row["kind"] == "mapping" and body.get("adapter") == "traceability-closure-v1":
                # A closure proposal deliberately uses the mapping kind so
                # it shares the mapping denominator, but its immutable
                # companion is the closure_proposed TREC.  It has no TMAP;
                # distinguish it by its controller-owned adapter/stage
                # format rather than treating every mapping as a TMAP pair.
                need(body.get("format") == TRACEABILITY_FORMAT
                     and body.get("closure_stage") in {"task", "integrated", "delivered"}
                     and isinstance(body.get("mapping_ids"), list)
                     and all(isinstance(mapping_id, str) for mapping_id in body["mapping_ids"])
                     and len(body["mapping_ids"]) == len(set(body["mapping_ids"])),
                     "integrity_error", "Traceability closure proposal shape is malformed", row["id"])
                closure_matches = []
                for closure_row in tables["records"]:
                    if closure_row.get("kind") != "closure_proposed" or closure_row.get("proposal") != row["id"]:
                        continue
                    closure_body = decoded_rows["traceability_records"][closure_row["id"]]
                    closure_matches.append((closure_row, closure_body))
                need(len(closure_matches) == 1,
                     "integrity_error", "Traceability closure proposal companion is missing or ambiguous", row["id"])
                closure_row, closure_body = closure_matches[0]
                need(closure_body.get("format") in {None, "traceability.record.v1", TRACEABILITY_FORMAT}
                     and closure_body.get("kind") == "closure_proposed"
                     and closure_body.get("project") == project
                     and closure_body.get("proposal") == row["id"]
                     and closure_body.get("closure_id") == closure_row["id"]
                     and closure_body.get("subject_ref") == {"table": "traceability_proposals",
                                                               "id": row["id"], "digest": row["digest"]}
                     and closure_body.get("stage") == body.get("closure_stage")
                     and closure_body.get("mapping_ids") == body.get("mapping_ids")
                     and isinstance(closure_body.get("required_leaf_ids"), list)
                     and all(isinstance(leaf_id, str) for leaf_id in closure_body["required_leaf_ids"]),
                     "integrity_error", "Traceability closure companion identity differs", row["id"])
                closure_revision = body.get("revision")
                need(isinstance(closure_revision, str)
                     and closure_row.get("revision") == closure_revision
                     and all(leaf_id in item_by_id and item_by_id[leaf_id].get("revision") == closure_revision
                             for leaf_id in closure_body["required_leaf_ids"])
                     and all(mapping_id in tables_by_table["traceability_mappings"]
                             and tables_by_table["traceability_mappings"][mapping_id].get("revision") == closure_revision
                             for mapping_id in body["mapping_ids"]),
                     "integrity_error", "Traceability closure mapping population differs", row["id"])
                continue
            companion_key = {"decision": "decision_id", "mapping": "mapping_id", "scope": "binding_id"}.get(row["kind"])
            if companion_key is None:
                continue
            companion_id = body.get(companion_key)
            companion_table = {"decision_id": "traceability_decisions", "mapping_id": "traceability_mappings",
                               "binding_id": "traceability_bindings"}[companion_key]
            companion = tables_by_table[companion_table].get(companion_id)
            need(companion is not None, "integrity_error", "Traceability proposal companion is missing", row["id"])
            companion_body = decoded_rows[companion_table][companion_id]
            need(companion_body.get("proposal") == row["id"], "integrity_error", "Traceability proposal companion link differs", row["id"])

        # Validate set active pointers after revision rows are known.
        for row in set_rows:
            active = row.get("active_revision")
            if active is None:
                need(row.get("active_digest") is None, "integrity_error", "Traceability active digest has no revision", row["id"])
            else:
                target = revision_by_id.get(active)
                need(target is not None and target["set_id"] == row["id"] and row.get("active_digest") == target["digest"],
                     "integrity_error", "Traceability active revision differs", row["id"])

        # A ready/active/superseded revision must retain one matching extracted
        # record and its complete immutable population.  This keeps an absent
        # record/CAS failure visible instead of projecting an empty population.
        extracted_by_revision: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for row in tables["records"]:
            body = decoded_rows["traceability_records"][row["id"]]
            record_kind = row.get("kind")
            need(record_kind in known_record_kinds,
                 "integrity_error", "Unknown traceability record kind", row.get("id"))
            # The first proposal, resumable checkpoint, and extraction-failed
            # records predate the full traceability.record.v1 envelope.  Their
            # immutable row kind and proposal FK are still checked, while the
            # semantic records must carry the complete project/kind envelope.
            if record_kind not in {"proposal", "extraction_checkpoint", "extraction_failed", "review_packet"}:
                need(body.get("format") in {None, "traceability.record.v1", TRACEABILITY_FORMAT}
                     and body.get("kind") == record_kind and body.get("project") == project,
                     "integrity_error", "Traceability record kind or body differs", row.get("id"))
            if body.get("proposal") is not None:
                need(body.get("proposal") == row.get("proposal") and body.get("proposal") in proposal_by_id,
                     "integrity_error", "Traceability record proposal differs", row.get("id"))
            if body.get("revision") is not None:
                need(body.get("revision") == row.get("revision"),
                     "integrity_error", "Traceability record revision differs", row.get("id"))
            if row.get("revision") is not None:
                need(row["revision"] in revision_by_id, "integrity_error", "Traceability record revision is missing", row["id"])
            if row.get("proposal") is not None:
                need(row["proposal"] in proposal_by_id, "integrity_error", "Traceability record proposal is missing", row["id"])
            if row["kind"] == "extracted":
                extracted_by_revision.setdefault(row.get("revision"), []).append((row, body))
            if row["kind"] == "review_packet":
                need(body.get("format") == "traceability.review-packet.v1"
                     and body.get("kind") == "review_packet"
                     and body.get("proposal") in proposal_by_id
                     and isinstance(body.get("proposal_digest"), str)
                     and body.get("proposal_digest") == proposal_by_id[body["proposal"]]["digest"]
                     and body.get("role") in {"trace", "impact"}
                     and type(body.get("packet_index")) is int and body["packet_index"] >= 0
                     and type(body.get("packet_count")) is int and body["packet_count"] > 0
                     and isinstance(body.get("leaf_ids"), list)
                     and isinstance(body.get("required_coverage"), list)
                     and body.get("binding") == self._packet_binding(body),
                     "integrity_error", "Traceability review packet shape or binding differs", row["id"])
            if row["kind"] in {"decision_proposed", "mapping_proposed", "scope_proposed",
                                "population_adopted", "decision_adopted", "mapping_adopted",
                                "binding_adopted", "closure_proposed", "closure_adopted", "withdrawn"}:
                need(isinstance(body.get("subject_ref"), dict),
                     "integrity_error", "Traceability semantic record has no subject reference", row["id"])
            if row["kind"] == "closure_proposed":
                need(body.get("closure_id") == row["id"] and isinstance(body.get("mapping_ids"), list),
                     "integrity_error", "Traceability closure proposal identity differs", row["id"])
                for mapping_id in body["mapping_ids"]:
                    need(mapping_id in tables_by_table["traceability_mappings"],
                         "integrity_error", "Traceability closure mapping is missing", row["id"])
            if row["kind"] == "closure_adopted":
                ref = body.get("closure_ref")
                need(isinstance(ref, dict) and ref.get("table") == "traceability_records"
                     and ref.get("id") in tables_by_table["traceability_records"],
                     "integrity_error", "Traceability closure adoption reference is missing", row["id"])
            if row["kind"] == "delivered_mapping":
                validate_delivered_history(row, body)
            validate_external(body, row["id"])
            validate_blob_fields(body, row["id"])
            for ref in _trace_blob_refs(body):
                cas(ref, row["id"])
        for revision_id, row in revision_by_id.items():
            if row["status"] in {"ready", "active", "superseded"}:
                matches = extracted_by_revision.get(revision_id, [])
                need(len(matches) == 1, "integrity_error", "Completed traceability revision has no unique extracted record", revision_id)
                extracted_body = matches[0][1]
                need(extracted_body.get("revision") == revision_id and extracted_body.get("revision_digest") == row["digest"]
                     and extracted_body.get("population_digest") == row["population_digest"],
                     "integrity_error", "Extracted traceability record differs", revision_id)
                rows = items_by_revision[revision_id]
                item_digest = _sequence_digest(item["digest"] for item in rows)
                leaf_ids = [item["id"] for item in rows if item["leaf"]]
                body = revision_body_by_id[revision_id]
                need(body.get("item_digest") == item_digest and body.get("leaf_ids_digest") == digest(sorted(leaf_ids)),
                     "integrity_error", "Traceability revision population differs", revision_id)
                proposal_id = matches[0][0].get("proposal")
                prop = proposal_by_id.get(proposal_id)
                need(prop is not None, "integrity_error", "Extracted revision proposal is missing", revision_id)
                expected_population = digest({"proposal": prop["digest"], "items": item_digest,
                                               "leaf_count": len(leaf_ids),
                                               "unknown_count": sum(1 for item in rows if item["leaf"] and item["status"] == "unknown")})
                need(row["population_digest"] == expected_population, "integrity_error", "Traceability population digest differs", revision_id)

        def normalized_result(row: dict[str, Any]) -> Any:
            raw = row.get("result")
            if raw is None:
                return None
            result = parse_json(raw, limit=MAX_REVISION_BODY_BYTES) if isinstance(raw, str) else raw
            need(isinstance(result, dict), "integrity_error", "Traceability proposal result is malformed", row["id"])
            keep = {key: result[key] for key in (
                "status", "revision", "revision_digest", "revision_number", "population_digest", "active",
                "counts", "error", "pins", "completed", "partial", "inventory", "items", "git_pin",
                "entry_manifest", "historical_only") if key in result}
            return normalize_generated(keep)

        def unique(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            found: dict[bytes, dict[str, Any]] = {}
            for value in values:
                found[canonical(value)] = value
            return [found[key] for key in sorted(found)]

        status_order = {
            "proposed": 0, "pending": 0, "staging": 1, "ready": 2,
            "accepted": 3, "mandatory": 3, "adopted": 4, "active": 4,
            "superseded": 4, "failed": 5, "rejected": 5, "stale": 5,
            "withdrawn": 6,
        }
        initial_states = {"proposed", "pending"}

        def collapse_states(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Collapse duplicate generated proposals/companions by meaning.

            A second initial proposal is an append-only history fact, but it
            does not itself add structural progress.  Once either duplicate
            moves through staging, adoption, failure, or withdrawal, the
            non-initial state set is retained on that one logical entry.
            Results remain a list only when distinct formal revisions or
            failure observations actually differ.
            """
            groups: dict[bytes, dict[str, Any]] = {}
            states: dict[bytes, set[str]] = {}
            results: dict[bytes, list[Any]] = {}
            for value in values:
                base = {key: child for key, child in value.items()
                        if key not in {"status", "statuses", "result", "results"}}
                key = canonical(base)
                groups.setdefault(key, base)
                states.setdefault(key, set()).update(value.get("statuses") or [value.get("status")])
                if "result" in value and value.get("result") is not None:
                    result_key = canonical(value["result"])
                    if all(canonical(item) != result_key for item in results.setdefault(key, [])):
                        results[key].append(value["result"])
            output = []
            for key in sorted(groups):
                value = dict(groups[key])
                observed = {state for state in states[key] if isinstance(state, str) and state}
                # A repeated initial proposal is an immutable history row, not
                # a new semantic state.  Once a logical subject has reached a
                # later state, retaining ``proposed``/``pending`` in the
                # projected status set would make an otherwise identical
                # re-proposal look like progress.  Keep every non-initial
                # state (including distinct failures), while an all-initial
                # group gets one stable representation.
                non_initial = observed - initial_states
                if non_initial:
                    observed = non_initial
                elif observed & initial_states:
                    observed = {"pending" if "pending" in observed else "proposed"}
                else:
                    observed = {"unknown"}
                observed = sorted(observed)
                value["status"] = max(observed, key=lambda item: (status_order.get(item, -1), item))
                value["statuses"] = observed
                if key in results:
                    ordered = sorted(results[key], key=canonical)
                    value["results"] = ordered
                    value["result"] = ordered[0] if len(ordered) == 1 else ordered
                output.append(value)
            return output

        projections = {key: [] for key in ("sets", "revisions", "items", "proposals", "decisions", "mappings", "bindings", "records")}
        companion_states: dict[tuple[str, str], set[str]] = {}
        for record in tables["records"]:
            body = decoded_rows["traceability_records"][record["id"]]
            ref = body.get("subject_ref")
            if not isinstance(ref, dict) or not isinstance(ref.get("table"), str) or not isinstance(ref.get("id"), str):
                continue
            state = {"decision_adopted": ("accepted",), "mapping_adopted": ("accepted",),
                     "binding_adopted": ("mandatory",), "withdrawn": ()}.get(record["kind"])
            if state is None:
                continue
            table = ref["table"]
            if table not in {"traceability_decisions", "traceability_mappings", "traceability_bindings"}:
                continue
            if record["kind"] == "withdrawn":
                state = ("withdrawn",) if table == "traceability_bindings" else ("stale",)
            companion_states.setdefault((table, ref["id"]), set()).update(state)

        def companion_statuses(table: str, row: dict[str, Any]) -> list[str]:
            return sorted({row["status"], *companion_states.get((table, row["id"]), set())})

        for row in set_rows:
            projections["sets"].append({"project": project, "name": row["name"], "kind": row["kind"],
                                         "active_revision": revision_identity(row.get("active_revision")),
                                         "active_digest": row.get("active_digest")})
        for row in tables["revisions"]:
            projections["revisions"].append({"identity": revision_identity(row["id"]), "status": row["status"],
                                              "body": normalize_body(revision_body_by_id[row["id"]], "traceability_revisions", row)})
        for row in tables["items"]:
            projections["items"].append({"identity": item_identity(row["id"]), "status": row["status"],
                                          "start_byte": row["start_byte"], "end_byte": row["end_byte"],
                                          "body": normalize_body(item_body_by_id[row["id"]], "traceability_items", row)})
        for row in tables["proposals"]:
            projections["proposals"].append({"set": set_identity(row["set_id"]), "project": project, "kind": row["kind"],
                                              "status": row["status"], "body": normalize_body(proposal_body_by_id[row["id"]], "traceability_proposals", row),
                                              "expected_active": normalize_value(parse_json(row["expected_active"]) if isinstance(row.get("expected_active"), str) else row.get("expected_active")),
                                              "semantic_material_digest": row["semantic_material_digest"], "result": normalized_result(row)})
        for key, table_name in (("decisions", "traceability_decisions"), ("mappings", "traceability_mappings"), ("bindings", "traceability_bindings")):
            for row in tables[key]:
                body = decoded_rows[table_name][row["id"]]
                projections[key].append({"revision": revision_identity(row["revision"]), "status": row["status"],
                                         "statuses": companion_statuses(table_name, row),
                                         "body": normalize_body(body, table_name, row)})
        semantic_records = {"population_adopted", "decision_adopted", "mapping_adopted", "binding_adopted",
                            "closure_proposed", "closure_adopted", "delivered_mapping", "withdrawn"}
        for row in tables["records"]:
            if row["kind"] not in semantic_records | {"extraction_checkpoint", "extraction_failed"}:
                continue
            body = decoded_rows["traceability_records"][row["id"]]
            if row["kind"] in {"population_adopted", "decision_adopted", "mapping_adopted", "binding_adopted", "closure_adopted"}:
                body = {key: value for key, value in body.items() if key not in {"review_refs", "head_before"}}
            elif row["kind"] in {"extraction_checkpoint", "extraction_failed"}:
                # Worker sequence numbers are operational bookkeeping.  The
                # checkpoint's completed/partial population, pins, inventory,
                # and the failure code/details remain structural evidence.
                body = {key: value for key, value in body.items() if key not in {"checkpoint_count"}}
            projections["records"].append({"project": project, "revision": revision_identity(row.get("revision")),
                                            "proposal": proposal_identity(row.get("proposal")), "kind": row["kind"],
                                            "body": normalize_body(body, "traceability_records", row)})
        projections["proposals"] = collapse_states(projections["proposals"])
        for key in ("decisions", "mappings", "bindings"):
            projections[key] = collapse_states(projections[key])
        projections["sets"] = unique(projections["sets"])
        projections["revisions"] = unique(projections["revisions"])
        projections["items"] = unique(projections["items"])
        projections["records"] = unique(projections["records"])
        return {"format": "daikibo.traceability-structural-progress.v1", "project": project,
                **projections}

    # ---------- Unit B identity/evidence helpers ----------
    #
    # Unit A deliberately stores the eight traceability relations as
    # immutable rows.  Unit B adds meaning by appending records to the same
    # history; it never rewrites a TDEC/TMAP/TBIND row to make an adoption
    # appear.  Keep the small wire-format helpers together so the dedicated
    # archive validator, the public reader, and the mutation paths use the
    # same identity rules.
    _B_TABLES = {
        "traceability_proposals", "traceability_decisions", "traceability_mappings",
        "traceability_bindings", "traceability_records", "traceability_revisions",
    }

    @staticmethod
    def _subject_ref(table: str, row: dict[str, Any]) -> dict[str, str]:
        need(table in Traceability._B_TABLES, "invalid_reference", "Unknown traceability subject table")
        ident = row.get("id")
        stored = row.get("digest")
        need(isinstance(ident, str) and isinstance(stored, str) and _HEX64.fullmatch(stored),
             "integrity_error", "Traceability subject has no valid immutable digest")
        return {"table": table, "id": ident, "digest": stored}

    def _row_subject_ref(self, table: str, ident: str, project: str | None = None) -> dict[str, str]:
        need(table in self._B_TABLES, "invalid_reference", "Unknown traceability subject table")
        row = self.s.one(f"SELECT * FROM {table} WHERE id=?", (ident,), True)
        if project is not None:
            need(row.get("project") == project, "cross_project", "Traceability reference belongs to another project")
        return self._subject_ref(table, row)

    def _typed_resolve(self, actor, project: str, ref: Any, *, require_current: bool = True) -> dict[str, Any]:
        """Resolve a public typed ref through the separately reviewed resolver.

        The resolver is intentionally an integration dependency.  Keeping the
        import lazy lets Unit A databases open while the independent resolver
        is reviewed, but B never substitutes path/name matching or a fake
        successful result when that component is unavailable.
        """
        try:
            from .traceability_refs import TraceabilityRefResolver
        except ImportError as exc:
            raise Fault("unresolved_reference", "Typed traceability reference resolver is unavailable") from exc
        return TraceabilityRefResolver(self.c).resolve(actor, project, ref, require_current=require_current)

    def _latest_record_id(self, project: str) -> str | None:
        row = self.s.one("SELECT id FROM traceability_records WHERE project=? ORDER BY created DESC,id DESC LIMIT 1", (project,))
        return row["id"] if row else None

    def _check_head(self, project: str, expected_head_record: str | None) -> str | None:
        current = self._latest_record_id(project)
        if expected_head_record is not None:
            need(isinstance(expected_head_record, str), "invalid_reference", "expected_head_record must be a record ID")
            need(expected_head_record == current, "write_conflict", "Traceability history head changed", {"expected": expected_head_record, "actual": current})
        return current

    def _record_body(self, kind: str, project: str, revision: str | None, proposal: str | None,
                     subject_ref: dict[str, Any] | None = None, **fields) -> dict[str, Any]:
        body = {"format": "traceability.record.v1", "kind": kind, "project": project,
                "revision": revision, "proposal": proposal, **fields}
        if subject_ref is not None:
            body["subject_ref"] = subject_ref
        return body

    def _append_record(self, project: str, revision: str | None, proposal: str | None,
                       kind: str, body: dict[str, Any]) -> str:
        """Append an immutable record; caller owns the surrounding CAS transaction."""
        ident = uid("TREC")
        encoded = canonical(body).decode()
        self.s.execute("INSERT INTO traceability_records VALUES(?,?,?,?,?,?,?,?)",
                       (ident, project, revision, proposal, kind, encoded, digest(body), timestamp()))
        return ident

    def _records_for_subject(self, project: str, table: str, ident: str, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.s.all("SELECT * FROM traceability_records WHERE project=? ORDER BY created,id", (project,))
        result = []
        for row in rows:
            if kind is not None and row["kind"] != kind:
                continue
            try:
                body = parse_json(row["body"], limit=MAX_REVISION_BODY_BYTES)
            except Fault:
                continue
            ref = body.get("subject_ref")
            if isinstance(ref, dict) and ref.get("table") == table and ref.get("id") == ident:
                need(digest(body) == row["digest"], "integrity_error", "Traceability record digest differs", row["id"])
                row["body"] = body
                result.append(row)
        return result

    def _effective_status(self, table: str, ident: str, project: str | None = None) -> str:
        row = self.s.one(f"SELECT * FROM {table} WHERE id=?", (ident,), True)
        if project is not None:
            need(row["project"] == project, "cross_project", "Traceability row belongs to another project")
        stored = row.get("status")
        if table == "traceability_proposals":
            return stored
        accepted = {
            "traceability_decisions": "decision_adopted",
            "traceability_mappings": "mapping_adopted",
            "traceability_bindings": "binding_adopted",
        }.get(table)
        if accepted:
            records = self._records_for_subject(row["project"], table, ident)
            if any(record["kind"] == "withdrawn" for record in records):
                return "withdrawn" if table == "traceability_bindings" else "stale"
            if any(record["kind"] == accepted for record in records):
                return {"traceability_decisions": "accepted", "traceability_mappings": "accepted",
                        "traceability_bindings": "mandatory"}[table]
        return stored

    def _proposal_for_subject(self, actor, project: str, subject: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Return (TPROP id, TPROP row, typed subject row) for B dispatch."""
        self._project(actor, project)
        prop = self.s.one("SELECT * FROM traceability_proposals WHERE id=? AND project=?", (subject, project))
        if prop:
            return prop["id"], _row_json(prop), {"table": "traceability_proposals", **_row_json(prop)}
        for table in ("traceability_decisions", "traceability_mappings", "traceability_bindings"):
            row = self.s.one(f"SELECT * FROM {table} WHERE id=? AND project=?", (subject, project))
            if row:
                decoded = _row_json(row)
                body = decoded["body"]
                proposal_id = body.get("proposal")
                need(isinstance(proposal_id, str), "missing_proposal", f"{table} has no immutable TPROP correspondence", subject)
                prop = self.s.one("SELECT * FROM traceability_proposals WHERE id=? AND project=?", (proposal_id, project), True)
                prop = _row_json(prop)
                need(digest(prop["body"]) == prop["digest"], "integrity_error", "Traceability proposal digest differs")
                return prop["id"], prop, {"table": table, **decoded}
        record = self.s.one("SELECT * FROM traceability_records WHERE id=? AND project=?", (subject, project))
        if record:
            decoded = _row_json(record)
            if decoded["kind"] in {"closure_proposed", "closure_adopted"}:
                proposal_id = decoded["proposal"]
                need(isinstance(proposal_id, str), "missing_proposal", "Closure has no TPROP correspondence")
                prop = self.s.one("SELECT * FROM traceability_proposals WHERE id=? AND project=?", (proposal_id, project), True)
                return prop["id"], _row_json(prop), {"table": "traceability_records", **decoded}
        raise Fault("not_found", "Unknown traceability proposal or subject")

    def _proposal_revision(self, proposal: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        body = proposal.get("body", {})
        result = proposal.get("result")
        if isinstance(result, dict):
            revision_id = result.get("revision")
        else:
            revision_id = body.get("revision") or body.get("scope", {}).get("revision")
        if not revision_id:
            return None, None
        row = self.s.one("SELECT * FROM traceability_revisions WHERE id=?", (revision_id,))
        return revision_id, row

    def _decision_body(self, row: dict[str, Any]) -> dict[str, Any]:
        body = row.get("body")
        if isinstance(body, str): body = parse_json(body, limit=MAX_REVISION_BODY_BYTES)
        need(isinstance(body, dict) and body.get("kind") == "decision", "invalid_decision", "Decision body is malformed")
        need(digest(body) == row.get("digest"), "integrity_error", "Traceability decision digest differs")
        return body

    def _mapping_body(self, row: dict[str, Any]) -> dict[str, Any]:
        body = row.get("body")
        if isinstance(body, str): body = parse_json(body, limit=MAX_REVISION_BODY_BYTES)
        need(isinstance(body, dict) and body.get("kind") == "mapping", "invalid_mapping", "Mapping body is malformed")
        need(digest(body) == row.get("digest"), "integrity_error", "Traceability mapping digest differs")
        return body

    def _binding_body(self, row: dict[str, Any]) -> dict[str, Any]:
        body = row.get("body")
        if isinstance(body, str): body = parse_json(body, limit=MAX_REVISION_BODY_BYTES)
        need(isinstance(body, dict) and body.get("kind") == "scope_binding", "invalid_scope", "Scope binding body is malformed")
        need(digest(body) == row.get("digest"), "integrity_error", "Traceability binding digest differs")
        return body

    # ---------- validation and proposal ----------
    def _project(self, actor, project):
        self.c.k.project(actor, project)
        actor.require("owner", "agent", "worker", "reviewer", "observer", project=project)

    def _source_descriptor(self, project: str, source: Any = None, blob: Any = None) -> dict[str, Any]:
        source_id = None
        raw_digest = None
        if isinstance(source, dict):
            source_id = source.get("id") or source.get("source")
            raw_digest = source.get("blob") or source.get("digest")
        elif isinstance(source, str):
            source_id = source
            raw_digest = source if _HEX64.fullmatch(source) else None
        if source_id:
            row = self.s.one("SELECT * FROM sources WHERE id=? AND project=?", (source_id, project))
            if row:
                need(raw_digest is None or raw_digest == row["blob"], "stale_source", "Source digest differs")
                return {"source_id": source_id, "blob": row["blob"], "bytes": self.s.blob_get(row["blob"]).__len__(), "locator": row["locator"]}
            document = self.s.one("SELECT * FROM documents WHERE id=? AND project=?", (source_id, project))
            if document:
                body = parse_json(document["body"])
                raw_digest = body.get("raw_digest")
                need(isinstance(raw_digest, str) and _HEX64.fullmatch(raw_digest), "invalid_source", "Document does not contain a valid raw blob digest")
                data = self.s.blob_get(raw_digest)
                return {"source_id": source_id, "blob": raw_digest, "bytes": len(data), "locator": body.get("locator")}
            if raw_digest and _HEX64.fullmatch(raw_digest):
                data = self.s.blob_get(raw_digest)
                return {"source_id": None, "blob": raw_digest, "bytes": len(data), "locator": "cas"}
            raise Fault("not_found", "Source or document does not exist")
        raw_digest = blob or raw_digest
        need(isinstance(raw_digest, str) and _HEX64.fullmatch(raw_digest), "invalid_source", "A registered source blob is required")
        data = self.s.blob_get(raw_digest)
        return {"source_id": None, "blob": raw_digest, "bytes": len(data), "locator": "cas"}

    def _normal_scope(self, project: str, kind: str, scope: Any, repository=None, commit=None, roots=None, include=None, source=None, blob=None) -> dict[str, Any]:
        if scope is None:
            scope = {}
        if isinstance(scope, str):
            scope = {"source": scope} if kind in {"document", "source"} else {"repository": scope}
        obj(scope, optional=("repository", "repo", "commit", "roots", "include", "source", "blob", "name", "kind", "path", "media_type"), name="scope")
        scope = dict(scope)
        for key, value in (("repository", repository), ("commit", commit), ("roots", roots), ("include", include), ("source", source), ("blob", blob)):
            if value is not None:
                scope.setdefault(key, value)
        if kind in {"document", "source"} or scope.get("source") is not None or scope.get("blob") is not None:
            source_desc = self._source_descriptor(project, scope.get("source"), scope.get("blob"))
            return {"kind": "document", "source": source_desc, "roots": [], "include": []}
        repo_id = scope.get("repository") or scope.get("repo")
        need(isinstance(repo_id, str), "invalid_scope", "Code population needs a registered repository")
        repo = self.s.one("SELECT * FROM repos WHERE id=? AND project=?", (repo_id, project), True)
        pinned_commit = scope.get("commit")
        need(isinstance(pinned_commit, str), "invalid_scope", "Code population needs a complete Git commit OID")
        _git_oid(Path(repo["path"]), pinned_commit, "commit")
        object_format = _git(Path(repo["path"]), "rev-parse", "--show-object-format").decode().strip()
        need(object_format in {"sha1", "sha256"}, "unsupported_git", "Unsupported Git object format")
        selected_roots = _safe_roots(scope.get("roots")) if "roots" in scope else [""]
        return {"kind": "code", "repository": repo_id, "repository_name": repo["name"], "commit": pinned_commit,
                "object_format": object_format, "roots": selected_roots, "empty_scope": "roots" in scope and not selected_roots,
                "include": _patterns(scope.get("include"))}

    def propose(self, actor, project, scope=None, adapter="python-ast-v1", expected_active=None, kind="population", name=None, roots=None, include=None, repository=None, commit=None, source=None, blob=None, **extra):
        """Record a validated immutable extraction proposal.

        Proposal creation validates the fixed input identity but does not
        extract or adopt it.  Call :meth:`extract` to create a durable ready
        revision.
        """
        self._project(actor, project)
        actor.require("owner", "agent", "worker", project=project)
        need(kind in {"population", "code", "document", "source"}, "invalid_kind", "Unit A accepts code or document populations")
        normalized_kind = "document" if kind in {"document", "source"} else "code"
        text(adapter, "adapter", 200)
        normalized = self._normal_scope(project, normalized_kind, scope, repository, commit, roots, include, source, blob)
        if normalized_kind == "code":
            need(adapter in {"python-ast-v1", "python.ast.v1", "python"}, "unsupported_adapter", "Only the Python AST adapter is implemented in Unit A")
        else:
            need(adapter in {"utf8-lines-v1", "utf8.lines.v1", "document", "python-ast-v1"}, "unsupported_adapter", "Only the UTF-8 line adapter is implemented in Unit A")
        set_name = name or extra.get("set_name") or f"traceability-{normalized_kind}"
        text(set_name, "set name", 200)
        set_row = self.s.one("SELECT * FROM traceability_sets WHERE project=? AND name=?", (project, set_name))
        ident = uid("TPROP")
        if set_row is None:
            set_id = uid("TSET")
            with self.s.transaction():
                self.s.execute("INSERT INTO traceability_sets VALUES(?,?,?,?,?,?,?)", (set_id, project, set_name, normalized_kind, None, None, timestamp()))
        else:
            set_id = set_row["id"]
        body = {"format": TRACEABILITY_FORMAT, "id": ident, "project": project, "set_id": set_id,
                "kind": normalized_kind, "scope": normalized, "adapter": adapter,
                "adapter_contract": _adapter_contract(adapter),
                "expected_active": expected_active, "extra": extra}
        body_digest = digest(body)
        semantic = digest({"kind": normalized_kind, "scope": normalized, "adapter": adapter})
        with self.s.transaction():
            self.s.execute("INSERT INTO traceability_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (ident, set_id, project, normalized_kind, "proposed", canonical(body).decode(), body_digest,
                            canonical(expected_active).decode() if expected_active is not None else None, semantic, None, timestamp()))
            record_body = {"proposal": ident, "digest": body_digest, "status": "proposed"}
            self.s.execute("INSERT INTO traceability_records VALUES(?,?,?,?,?,?,?,?)",
                           (uid("TREC"), project, None, ident, "proposal", canonical(record_body).decode(), digest(record_body), timestamp()))
            self.c.sec.event(project, "traceability_proposed", actor.id, {"proposal": ident, "digest": body_digest, "kind": normalized_kind})
        return {"id": ident, "proposal": ident, "set_id": set_id, "project": project, "kind": normalized_kind,
                "status": "proposed", "digest": body_digest, "semantic_material_digest": semantic,
                "scope": normalized, "next_action": "traceability.extract"}

    def _proposal(self, actor, proposal: str, mutate: bool = False) -> dict[str, Any]:
        row = self.s.one("SELECT * FROM traceability_proposals WHERE id=?", (proposal,), True)
        self._project(actor, row["project"])
        if mutate:
            actor.require("owner", "agent", "worker", project=row["project"])
        row = _row_json(row)
        need(digest(row["body"]) == row["digest"], "integrity_error", "Traceability proposal digest differs")
        return row

    def _staging_state(self, proposal: str) -> dict[str, Any]:
        """Rebuild durable extraction progress from immutable checkpoint rows.

        A process can disappear after a CAS write or between two files.  The
        proposal status/result is mutable operational state, while each
        checkpoint is an append-only traceability record.  Replaying these
        records gives the next extraction attempt its completed files,
        inventories, item bodies, and CAS pin closure without trusting an
        in-memory list from the lost process.
        """
        rows = self.s.all(
            "SELECT body,digest FROM traceability_records WHERE proposal=? AND kind='extraction_checkpoint' ORDER BY created,id",
            (proposal,),
        )
        completed: dict[str, dict[str, Any]] = {}
        partial: dict[str, dict[str, Any]] = {}
        pins: set[str] = set()
        revision_id = None
        revision_no = None
        git_pin: dict[str, Any] | None = None
        entry_manifest: list[dict[str, Any]] | None = None
        pin_complete = False
        for row in rows:
            body = parse_json(row["body"], limit=MAX_REVISION_BODY_BYTES)
            need(digest(body) == row["digest"], "integrity_error", "Extraction checkpoint digest differs")
            pins.update(value for value in body.get("pins", []) if isinstance(value, str) and _HEX64.fullmatch(value))
            revision_id = revision_id or body.get("revision_id")
            revision_no = revision_no or body.get("revision_number")
            if isinstance(body.get("git_pin"), dict):
                git_pin = body["git_pin"]
            if isinstance(body.get("entry_manifest"), list):
                entry_manifest = body["entry_manifest"]
            if body.get("pin_complete") is True:
                pin_complete = True
            key = body.get("key")
            if not isinstance(key, str):
                continue
            if body.get("complete"):
                completed[key] = body
            elif body.get("inventory"):
                partial[key] = body
        items: list[dict[str, Any]] = []
        inventory: list[dict[str, Any]] = []
        counts = {"files": 0, "bytes": 0, "known": 0, "unknown": 0}
        for body in completed.values():
            items.extend(body.get("items", []))
            inventory.extend(body.get("inventory", []))
            for key in counts:
                counts[key] += int(body.get("count_delta", {}).get(key, 0) or 0)
        return {"completed": completed, "partial": partial, "pins": sorted(pins),
                "items": items, "inventory": inventory, "counts": counts,
                "revision_id": revision_id, "revision_number": revision_no,
                "checkpoint_count": len(rows), "git_pin": git_pin,
                "entry_manifest": entry_manifest, "pin_complete": pin_complete}

    def _stage_checkpoint(self, proposal: dict[str, Any], revision_id: str, revision_no: int,
                          body: dict[str, Any], pins: Iterable[str]) -> None:
        """Commit one resumable extraction checkpoint and its CAS roots."""
        checkpoint = {"format": TRACEABILITY_FORMAT, "proposal": proposal["id"],
                      "revision_id": revision_id, "revision_number": revision_no,
                      **body, "pins": sorted(set(value for value in pins if isinstance(value, str)))}
        encoded = canonical(checkpoint).decode()
        with self.s.transaction():
            self.s.execute(
                "INSERT INTO traceability_records VALUES(?,?,?,?,?,?,?,?)",
                (uid("TREC"), proposal["project"], None, proposal["id"], "extraction_checkpoint", encoded,
                 digest(checkpoint), timestamp()),
            )
            # Keep the latest operational status visible to a restarted job;
            # the immutable checkpoint rows remain the source of truth.
            current = self._staging_state(proposal["id"])
            result = {"status": "staging", "revision": revision_id, "revision_number": revision_no,
                      "pins": current["pins"], "completed_count": len(current["completed"]),
                      "checkpoint_count": current["checkpoint_count"], "last_key": checkpoint.get("key"),
                      "counts": current["counts"], "historical_only": True}
            self.s.execute("UPDATE traceability_proposals SET status='staging',result=? WHERE id=?",
                           (canonical(result).decode(), proposal["id"]))

    def extract(self, actor, proposal, expected_digest=None, **options):
        """Extract one proposal into immutable CAS-backed staging history.

        The public Control route submits this operation as a durable job.  The
        direct method is also useful to local tests and performs the same
        checkpointed operation synchronously.
        """
        row = self._proposal(actor, proposal, mutate=True)
        need(expected_digest is None or expected_digest == row["digest"], "stale_proposal", "Proposal digest differs")
        need(row["status"] in {"proposed", "failed", "staging"}, "invalid_state", "Proposal is not extractable in its current state")
        staging = self._staging_state(proposal)
        with self.s.transaction():
            self.s.execute("UPDATE traceability_proposals SET status='staging',result=? WHERE id=?",
                           (canonical({"status": "staging", "revision": staging.get("revision_id"),
                                       "revision_number": staging.get("revision_number"),
                                       "pins": staging["pins"], "completed_count": len(staging["completed"]),
                                       "checkpoint_count": staging["checkpoint_count"], "historical_only": True}).decode(), proposal))
        revision_no = staging.get("revision_number") or int(self.s.one("SELECT COALESCE(max(revision),0)+1 AS n FROM traceability_revisions WHERE set_id=?", (row["set_id"],))["n"])
        revision_id = staging.get("revision_id") or uid("TREV")
        pinned: list[str] = list(staging["pins"])
        try:
            scope = row["body"]["scope"]
            if scope["kind"] == "code":
                result = self._extract_git(row, revision_id, revision_no, scope, pinned, staging)
            else:
                result = self._extract_document(row, revision_id, revision_no, scope, pinned, staging)
            items = result.pop("items")
            inventory = result.pop("inventory", [])
            leaf_ids = [item["id"] for item in items if item["body"].get("leaf")]
            unknown_count = sum(1 for item in items if item["status"] == "unknown" and item["body"].get("leaf"))
            item_digest = _sequence_digest(item["digest"] for item in items)
            population_digest = digest({"proposal": row["digest"], "items": item_digest, "leaf_count": len(leaf_ids), "unknown_count": unknown_count})
            git_counts = result.get("counts", {})
            revision_body = {"format": TRACEABILITY_FORMAT, "revision": revision_id, "set_id": row["set_id"],
                             "project": row["project"], "kind": scope["kind"], "scope": scope,
                             "adapter": row["body"]["adapter"], "adapter_contract": row["body"].get("adapter_contract", _adapter_contract(row["body"]["adapter"])),
                             "pins": sorted(set(pinned)), "inventory": inventory,
                             "git_pin": ({"object_format": scope.get("object_format"), "commit": scope.get("commit"),
                                          "commit_blob": git_counts.get("commit_blob"), "tree": git_counts.get("tree"),
                                          "tree_blob": git_counts.get("tree_blob")} if scope["kind"] == "code" else None),
                             "counts": {**result["counts"], "items": len(items), "leaf": len(leaf_ids), "unknown": unknown_count},
                             "item_digest": item_digest, "leaf_ids_digest": digest(sorted(leaf_ids)),
                             "historical_only": True, "active": False}
            revision_digest = digest(revision_body)
            with self.s.transaction():
                self.s.execute("INSERT INTO traceability_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (revision_id, row["set_id"], row["project"], revision_no, "ready", canonical(revision_body).decode(), revision_digest,
                                population_digest, row["body"]["adapter"], timestamp()))
                for ordinal, item in enumerate(items):
                    item["ordinal"] = ordinal
                    self.s.execute("INSERT INTO traceability_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (item["id"], revision_id, row["project"], ordinal, item["item_kind"], item["path"], item["status"],
                                    item["start_byte"], item["end_byte"], canonical(item["body"]).decode(), item["digest"],
                                    int(bool(item["body"].get("leaf")))))
                result = {"revision": revision_id, "revision_digest": revision_digest, "population_digest": population_digest,
                          "status": "ready", "counts": revision_body["counts"], "pins": sorted(set(pinned)), "active": False}
                self.s.execute("UPDATE traceability_proposals SET status='ready',result=? WHERE id=?", (canonical(result).decode(), proposal))
                record_body = result
                extracted_record = self._append_record(row["project"], revision_id, proposal, "extracted",
                                                       {**record_body, "format": TRACEABILITY_FORMAT,
                                                        "kind": "extracted", "project": row["project"],
                                                        "revision": revision_id, "proposal": proposal})
                # Review packets are materialized as immutable records while
                # the ready revision is published.  review_subject is a
                # read-only lookup and therefore cannot silently create a
                # different packet split on each request.
                self._ensure_review_packets(row["project"], proposal, revision_id,
                                            ["item:" + value for value in leaf_ids]
                                            or ["empty_scope:" + revision_id],
                                            role="trace", dependency_refs=[
                                                self._row_subject_ref("traceability_proposals", proposal),
                                                self._row_subject_ref("traceability_revisions", revision_id),
                                            ], root_subject=self._row_subject_ref("traceability_proposals", proposal),
                                            material_digest=digest({"proposal": row["digest"],
                                                                     "revision": revision_digest,
                                                                     "population": population_digest,
                                                                     "leaves": sorted(leaf_ids)}))
                self.c.sec.event(row["project"], "traceability_extracted", actor.id, {"proposal": proposal, "revision": revision_id, "digest": revision_digest})
            return result
        except BaseException as exc:
            details = exc.as_dict() if isinstance(exc, Fault) else {"type": type(exc).__name__, "message": str(exc)[:2000]}
            retained = self._staging_state(proposal)
            failure = {"proposal": proposal, "status": "failed", "error": details,
                       "pins": sorted(set(pinned) | set(retained["pins"])),
                       "completed_count": len(retained["completed"]),
                       "checkpoint_count": retained["checkpoint_count"],
                       "counts": retained["counts"], "historical_only": True}
            with self.s.transaction():
                self.s.execute("UPDATE traceability_proposals SET status='failed',result=? WHERE id=?", (canonical(failure).decode(), proposal))
                self.s.execute("INSERT INTO traceability_records VALUES(?,?,?,?,?,?,?,?)",
                               (uid("TREC"), row["project"], None, proposal, "extraction_failed", canonical(failure).decode(), digest(failure), timestamp()))
            if isinstance(exc, Fault):
                raise
            raise Fault("extraction_failed", "Traceability extraction failed", details) from exc

    def _find_pinned_git_object(self, pins: Iterable[str], oid: str, object_format: str,
                                expected_type: str) -> tuple[str, bytes] | None:
        """Find and verify a Git object in the durable CAS pin set."""
        for ref in pins:
            if not isinstance(ref, str) or not _HEX64.fullmatch(ref):
                continue
            try:
                raw = self.s.blob_get(ref)
                kind, _payload = _git_object_payload(raw)
            except (Fault, ValueError):
                continue
            if kind == expected_type and _sha256_oid(raw, object_format) == oid:
                return ref, raw
        return None

    def _recover_git_pin(self, proposal: dict[str, Any], scope: dict[str, Any],
                         pins: list[str], staging: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Rebuild the selected Git inventory solely from pinned CAS objects.

        This path deliberately does not inspect the registered repository.  A
        missing tree/blob is an explicit incomplete-input result; it is never
        converted into an empty or successful population.
        """
        object_format = scope["object_format"]
        commit = scope["commit"]
        found_commit = self._find_pinned_git_object(pins, commit, object_format, "commit")
        need(found_commit is not None, "missing_input", "Pinned Git commit object is incomplete; extraction remains unfinished", {"missing": ["commit:" + commit]})
        commit_blob, commit_raw = found_commit
        _kind, commit_payload = _git_object_payload(commit_raw, "commit")
        tree_match = re.search(rb"(?m)^tree ([0-9a-f]{40,64})$", commit_payload)
        need(tree_match is not None, "missing_input", "Pinned Git commit has no recoverable tree", commit)
        tree_oid = tree_match.group(1).decode("ascii")
        tree_found = self._find_pinned_git_object(pins, tree_oid, object_format, "tree")
        need(tree_found is not None, "missing_input", "Pinned Git root tree object is incomplete", {"missing": ["tree:" + tree_oid]})
        tree_blob, _tree_raw = tree_found
        tree_refs: dict[str, str] = {}
        entries: list[dict[str, Any]] = []
        missing: list[str] = []

        def walk(oid: str, prefix: str) -> None:
            found = self._find_pinned_git_object(pins, oid, object_format, "tree")
            if found is None:
                missing.append("tree:" + oid)
                return
            ref, raw = found
            tree_refs[oid] = ref
            for mode, kind, child_oid, name in _tree_entries(raw, object_format):
                path = f"{prefix}/{name}" if prefix else name
                if kind == "tree":
                    walk(child_oid, path)
                elif _matches(path, scope["roots"], scope["include"]):
                    entries.append({"mode": mode, "kind": kind, "oid": child_oid, "path": path})

        if not scope.get("empty_scope"):
            walk(tree_oid, "")
        need(not missing, "missing_input", "Pinned Git tree closure is incomplete; extraction remains unfinished", {"missing": sorted(set(missing))})
        entries.sort(key=lambda value: value["path"])
        pin = {"object_format": object_format, "commit": commit, "commit_blob": commit_blob,
               "tree": tree_oid, "tree_blob": tree_blob,
               "trees": [{"oid": oid, "blob": tree_refs[oid]} for oid in sorted(tree_refs)]}
        return pin, entries

    def _validate_git_pin(self, scope: dict[str, Any], git_pin: dict[str, Any],
                          entries: list[dict[str, Any]], pins: list[str]) -> None:
        """Validate the complete durable Git object closure before reuse.

        A staged entry checkpoint is sufficient to avoid re-reading the source
        repository, but it must not make a missing tree pin look complete.  A
        resumed population therefore checks the commit, every recorded tree,
        and the selected manifest against CAS.  No repository fallback is
        permitted on this path.
        """
        object_format = scope["object_format"]
        commit = scope["commit"]
        commit_blob = git_pin.get("commit_blob")
        need(isinstance(commit_blob, str) and _HEX64.fullmatch(commit_blob),
             "missing_input", "Pinned Git commit reference is incomplete; extraction remains unfinished", commit)
        try:
            commit_raw = self.s.blob_get(commit_blob)
            need(_sha256_oid(commit_raw, object_format) == commit, "integrity_error",
                 "Pinned commit CAS digest differs", commit)
            _git_object_payload(commit_raw, "commit")
        except (Fault, ValueError) as exc:
            raise Fault("missing_input", "Pinned Git commit CAS is unavailable; extraction remains unfinished", commit) from exc
        trees = git_pin.get("trees")
        need(isinstance(trees, list) and trees, "missing_input",
             "Pinned Git tree closure is incomplete; extraction remains unfinished", commit)
        tree_oids = set()
        for tree in trees:
            need(isinstance(tree, dict) and isinstance(tree.get("oid"), str)
                 and isinstance(tree.get("blob"), str) and _HEX64.fullmatch(tree["blob"]),
                 "missing_input", "Pinned Git tree reference is malformed; extraction remains unfinished", tree)
            oid = tree["oid"]
            try:
                raw_tree = self.s.blob_get(tree["blob"])
                need(_sha256_oid(raw_tree, object_format) == oid, "integrity_error",
                     "Pinned tree CAS digest differs", oid)
                _git_object_payload(raw_tree, "tree")
            except (Fault, ValueError) as exc:
                raise Fault("missing_input", "Pinned Git tree CAS is unavailable; extraction remains unfinished", oid) from exc
            tree_oids.add(oid)
        need(git_pin.get("tree") in tree_oids, "missing_input",
             "Pinned Git root tree is absent from the durable closure", git_pin.get("tree"))
        need(isinstance(entries, list), "missing_input",
             "Pinned Git entry manifest is incomplete; extraction remains unfinished", commit)

    def _extract_git(self, proposal: dict[str, Any], revision_id: str, revision_no: int,
                     scope: dict[str, Any], pinned: list[str], staging: dict[str, Any] | None = None) -> dict[str, Any]:
        staging = staging or {"completed": {}, "partial": {}, "pins": [], "items": [], "inventory": [],
                              "counts": {"files": 0, "bytes": 0, "known": 0, "unknown": 0}}
        repo = self.s.one("SELECT * FROM repos WHERE id=? AND project=?", (scope["repository"], proposal["project"]), True)
        root = Path(repo["path"])
        git_pin = staging.get("git_pin")
        entries = staging.get("entry_manifest")

        if not isinstance(git_pin, dict) or entries is None:
            # A new extraction pins the commit, every tree needed to enumerate
            # the selected roots, and the complete selected entry manifest in
            # append-only checkpoints before publishing any revision.
            try:
                need(root.is_dir(), "missing_input", "Registered Git repository is unavailable while pinning extraction inputs", str(root))
                commit = _git_oid(root, scope["commit"], "commit")
                commit_raw = _git_object(root, commit, scope["object_format"], "commit")
                commit_blob = self.s.blob_put(commit_raw)
                if commit_blob not in pinned:
                    pinned.append(commit_blob)
                _kind, commit_payload = _git_object_payload(commit_raw, "commit")
                tree_match = re.search(rb"(?m)^tree ([0-9a-f]{40,64})$", commit_payload)
                need(tree_match is not None, "invalid_git_inventory", "Git commit has no tree")
                tree_oid = tree_match.group(1).decode("ascii")
                tree_refs: dict[str, str] = {}
                entries = []

                def pin_tree(oid: str, prefix: str) -> None:
                    raw_tree = _git_object(root, oid, scope["object_format"], "tree")
                    tree_blob_ref = self.s.blob_put(raw_tree)
                    if tree_blob_ref not in pinned:
                        pinned.append(tree_blob_ref)
                    tree_refs[oid] = tree_blob_ref
                    if scope.get("empty_scope"):
                        return
                    for mode, kind, child_oid, name in _tree_entries(raw_tree, scope["object_format"]):
                        path = f"{prefix}/{name}" if prefix else name
                        if kind == "tree":
                            pin_tree(child_oid, path)
                        elif _matches(path, scope["roots"], scope["include"]):
                            entries.append({"mode": mode, "kind": kind, "oid": child_oid, "path": path})

                pin_tree(tree_oid, "")
                entries.sort(key=lambda value: value["path"])
                git_pin = {"object_format": scope["object_format"], "commit": commit,
                           "commit_blob": commit_blob, "tree": tree_oid,
                           "tree_blob": tree_refs[tree_oid],
                           "trees": [{"oid": oid, "blob": tree_refs[oid]} for oid in sorted(tree_refs)]}
            except Fault:
                # If a worker died after enough checkpointed CAS writes, the
                # repository may already be gone.  Recovery is attempted from
                # pins and otherwise reports missing-input explicitly.
                git_pin, entries = self._recover_git_pin(proposal, scope, pinned, staging)
            self._stage_checkpoint(proposal, revision_id, revision_no,
                                   {"stage": "git_inventory", "key": "__git_inventory__", "complete": False,
                                    "git_pin": git_pin, "entry_manifest": entries}, pinned)
        else:
            # Validate the durable marker before using its manifest.  This is
            # the branch used after a process exit and original Git removal.
            need(git_pin.get("commit") == scope["commit"] and git_pin.get("object_format") == scope["object_format"],
                 "staging_conflict", "Staged Git pin does not match the proposal scope")
            self._validate_git_pin(scope, git_pin, entries, pinned)

        # Every selected blob is pinned before any ready revision can exist.
        # A complete prior entry checkpoint is sufficient for the one-file
        # crash window, while the manifest prevents a partial multi-file
        # population from being mistaken for a complete one.
        prior_inventory = {row.get("path"): dict(row) for row in staging.get("inventory", []) if isinstance(row, dict)}
        # A worker may exit immediately after the blob checkpoint and before
        # the corresponding entry checkpoint.  Partial checkpoint inventory
        # is still durable CAS evidence and must be reused; it is not itself
        # enough to publish a revision until every manifest entry is present.
        for checkpoint in staging.get("partial", {}).values():
            for row in checkpoint.get("inventory", []):
                if isinstance(row, dict) and isinstance(row.get("path"), str):
                    prior_inventory[row["path"]] = dict(row)
        missing: list[str] = []
        for entry in entries:
            path, kind, mode, oid = entry["path"], entry["kind"], entry["mode"], entry["oid"]
            if kind != "blob":
                prior_inventory.setdefault(path, {"path": path, "type": kind, "mode": mode, "blob_oid": oid,
                                                   "sha256": None, "bytes": 0})
                continue
            current = prior_inventory.get(path, {})
            content_ref = current.get("sha256")
            object_ref = current.get("git_object_blob")
            payload = None
            if isinstance(content_ref, str) and _HEX64.fullmatch(content_ref):
                try:
                    payload = self.s.blob_get(content_ref)
                    need(digest(payload) == content_ref, "integrity_error", "Pinned source blob digest differs", path)
                except Fault:
                    payload = None
            if payload is None:
                if not root.is_dir():
                    missing.append("blob:" + path)
                    continue
                try:
                    object_raw = _git_object(root, oid, scope["object_format"], "blob")
                except Fault:
                    missing.append("blob:" + path)
                    continue
                _kind, payload = _git_object_payload(object_raw, "blob")
                need(len(payload) <= MAX_ITEM_BYTES, "source_too_large", "Selected Git blob exceeds the Unit A bound", path)
                content_ref = self.s.blob_put(payload)
                if content_ref not in pinned:
                    pinned.append(content_ref)
                object_ref = self.s.blob_put(object_raw)
                if object_ref not in pinned:
                    pinned.append(object_ref)
                current = {"path": path, "type": kind, "mode": mode, "blob_oid": oid,
                           "sha256": content_ref, "git_object_blob": object_ref, "bytes": len(payload)}
                prior_inventory[path] = current
                self._stage_checkpoint(proposal, revision_id, revision_no,
                                       {"stage": "git_blob", "key": "blob:" + path, "complete": False,
                                        "inventory": [current]}, pinned)
            else:
                need(len(payload) <= MAX_ITEM_BYTES, "source_too_large", "Selected Git blob exceeds the Unit A bound", path)
                object_raw = None
                if isinstance(object_ref, str) and _HEX64.fullmatch(object_ref):
                    try:
                        object_raw = self.s.blob_get(object_ref)
                        need(_sha256_oid(object_raw, scope["object_format"]) == oid, "integrity_error", "Pinned Git blob object differs", path)
                        _git_object_payload(object_raw, "blob")
                    except (Fault, ValueError):
                        object_raw = None
                if object_raw is None:
                    object_raw = f"blob {len(payload)}\0".encode() + payload
                    need(_sha256_oid(object_raw, scope["object_format"]) == oid, "integrity_error", "Pinned source bytes do not match Git blob OID", path)
                    object_ref = self.s.blob_put(object_raw)
                    if object_ref not in pinned:
                        pinned.append(object_ref)
                    current = {"path": path, "type": kind, "mode": mode, "blob_oid": oid,
                               "sha256": content_ref, "git_object_blob": object_ref, "bytes": len(payload)}
                    prior_inventory[path] = current
                    self._stage_checkpoint(proposal, revision_id, revision_no,
                                           {"stage": "git_blob", "key": "blob:" + path, "complete": False,
                                            "inventory": [current]}, pinned)
            need(isinstance(prior_inventory[path].get("git_object_blob"), str), "missing_input", "Pinned Git blob object is incomplete", path)
        need(not missing, "missing_input", "Pinned Git source closure is incomplete; extraction remains unfinished", {"missing": sorted(missing)})
        self._stage_checkpoint(proposal, revision_id, revision_no,
                               {"stage": "git_pin", "key": "__git_pin_complete__", "complete": True,
                                "git_pin": git_pin, "entry_manifest": entries, "pin_complete": True}, pinned)

        items: list[dict[str, Any]] = list(staging.get("items", []))
        inventory: list[dict[str, Any]] = list(prior_inventory.values())
        prior_counts = staging.get("counts", {})
        tree_count = len(git_pin.get("trees", [])) or 1
        counts = {"files": int(prior_counts.get("files", 0)), "bytes": int(prior_counts.get("bytes", 0)),
                  "known": int(prior_counts.get("known", 0)), "unknown": int(prior_counts.get("unknown", 0)),
                  "trees": tree_count}
        completed = staging.get("completed", {})
        for entry in sorted(entries, key=lambda value: value["path"]):
            path, kind, mode, oid = entry["path"], entry["kind"], entry["mode"], entry["oid"]
            if path in completed:
                continue
            entry_inventory = prior_inventory[path]
            if kind != "blob":
                ident = "ITEM-" + digest([proposal["set_id"], revision_no, path, kind, oid])[:40]
                body = {"type": "git_entry", "leaf": True, "path": path, "git_kind": kind, "git_oid": oid,
                        "mode": mode, "unknown_reason": "submodule" if kind == "commit" else "unsupported_git_entry"}
                item = _item(ident, 0, "file", path, "unknown", 0, 0, body)
                entry_inventory.update({"type": kind, "mode": mode, "blob_oid": oid, "sha256": None, "bytes": 0})
                delta = {"files": 1, "bytes": 0, "known": 0, "unknown": 1}
                items.append(item)
                for key, value in delta.items(): counts[key] += value
                self._stage_checkpoint(proposal, revision_id, revision_no,
                                       {"stage": "entry", "key": path, "complete": True, "items": [item],
                                        "inventory": [entry_inventory], "count_delta": delta}, pinned)
                continue
            payload = self.s.blob_get(entry_inventory["sha256"])
            blob = entry_inventory["sha256"]
            if stat.S_ISLNK(mode):
                symlink_id = "ITEM-" + digest([proposal["set_id"], revision_no, path, "symlink", oid])[:40]
                symlink_body = {"type": "git_entry", "leaf": True, "path": path, "git_kind": "symlink",
                                "ref_type": "git_file", "project": proposal["project"], "repository": scope["repository"],
                                "commit": git_pin["commit"], "object_format": scope["object_format"], "git_oid": oid,
                                "blob_oid": oid, "blob_digest": blob, "sha256": blob, "mode": mode,
                                "target": payload.decode("utf-8", errors="replace"), "unknown_reason": "symlink"}
                subitems = [_item(symlink_id, 0, "file", path, "unknown", 0, len(payload), symlink_body)]
            else:
                subitems, _stats = _code_file_items(proposal["set_id"], revision_no, path, payload, blob, oid, mode)
                for subitem in subitems:
                    body = subitem["body"]
                    body.update({"project": proposal["project"], "repository": scope["repository"], "commit": git_pin["commit"],
                                 "object_format": scope["object_format"], "adapter_version": "python-ast-v1",
                                 "blob_oid": oid, "sha256": blob})
                    if subitem["item_kind"] == "file":
                        body["ref_type"] = "git_file"
                        body["typed_ref"] = {"type": "git_file", "repository": scope["repository"], "project": proposal["project"],
                                             "commit": git_pin["commit"], "object_format": scope["object_format"], "path": path,
                                             "blob_oid": oid, "sha256": blob}
                    elif subitem["item_kind"] == "symbol":
                        body["ref_type"] = "git_symbol"
                        body["signaturehash"] = body.get("signature_hash")
                        body["typed_ref"] = {"type": "git_symbol", "repository": scope["repository"], "project": proposal["project"],
                                             "commit": git_pin["commit"], "object_format": scope["object_format"], "path": path,
                                             "language": "python", "adapter_version": "python-ast-v1", "blob_oid": oid,
                                             "sha256": blob, "qualified_name": body.get("qualified_name"), "kind": body.get("kind"),
                                             "ordinal": body.get("ordinal"), "byte_start": body.get("byte_start"),
                                             "byte_end": body.get("byte_end"), "span_hash": body.get("source_span", {}).get("span_hash"),
                                             "signature_hash": body.get("signature_hash")}
                    else:
                        body["ref_type"] = "git_atom"
                        body["typed_ref"] = {"type": "git_atom", "repository": scope["repository"], "project": proposal["project"],
                                             "commit": git_pin["commit"], "object_format": scope["object_format"], "path": path,
                                             "language": "python", "adapter_version": "python-ast-v1", "blob_oid": oid,
                                             "sha256": blob, "byte_start": body.get("byte_start"), "byte_end": body.get("byte_end"),
                                             "span_hash": body.get("source_span", {}).get("span_hash")}
                    subitem["digest"] = digest(body)
            delta = {"files": 1, "bytes": len(payload), "known": sum(item["status"] == "known" for item in subitems),
                     "unknown": sum(item["status"] == "unknown" for item in subitems)}
            items.extend(subitems)
            for key, value in delta.items(): counts[key] += value
            self._stage_checkpoint(proposal, revision_id, revision_no,
                                   {"stage": "entry", "key": path, "complete": True, "items": subitems,
                                    "inventory": [entry_inventory], "count_delta": delta}, pinned)
        return {"items": items, "inventory": inventory,
                "counts": {**counts, "empty_selection": not entries, "commit": git_pin["commit"], "tree": git_pin["tree"],
                           "commit_blob": git_pin["commit_blob"], "tree_blob": git_pin["tree_blob"],
                           "empty_scope": bool(scope.get("empty_scope"))}}

    def _extract_document(self, proposal: dict[str, Any], revision_id: str, revision_no: int,
                          scope: dict[str, Any], pinned: list[str], staging: dict[str, Any] | None = None) -> dict[str, Any]:
        staging = staging or {"completed": {}, "partial": {}, "pins": [], "items": [], "inventory": [],
                              "counts": {"files": 0, "bytes": 0, "known": 0, "unknown": 0}}
        source = scope["source"]
        prior = staging.get("completed", {}).get("__document__")
        if prior:
            return {"items": prior.get("items", []), "inventory": prior.get("inventory", []),
                    "counts": {**prior.get("count_delta", {}), "blob": source["blob"]}}
        raw = self.s.blob_get(source["blob"])
        need(len(raw) <= MAX_ITEM_BYTES, "source_too_large", "Selected document exceeds the Unit A bound")
        blob = self.s.blob_put(raw)
        if blob not in pinned:
            pinned.append(blob)
        entry_inventory = {"path": None, "type": "source", "mode": None, "blob_oid": None, "sha256": blob, "bytes": len(raw)}
        self._stage_checkpoint(proposal, revision_id, revision_no,
                               {"stage": "blob", "key": "__document__", "complete": False,
                                "inventory": [entry_inventory]}, pinned)
        items, stats = _document_line_items(proposal["set_id"], revision_no, raw, blob, source.get("source_id"))
        delta = {"files": 1, "bytes": len(raw), "lines": stats["leaf_count"],
                 "known": len(items) - 1 - stats["unknown_count"], "unknown": stats["unknown_count"]}
        self._stage_checkpoint(proposal, revision_id, revision_no,
                               {"stage": "entry", "key": "__document__", "complete": True,
                                "items": items, "inventory": [entry_inventory], "count_delta": delta}, pinned)
        return {"items": items, "inventory": [entry_inventory], "counts": {**delta, "blob": blob}}

    # ---------- bounded reads ----------
    def _revision(self, actor, revision: str, *, include_body: bool = True) -> dict[str, Any]:
        columns = "*" if include_body else "id,set_id,project,revision,status,digest,population_digest,adapter,created"
        row = self.s.one(f"SELECT {columns} FROM traceability_revisions WHERE id=?", (revision,), True)
        self._project(actor, row["project"])
        if include_body:
            raw_body = row["body"]
            body = parse_json(raw_body, limit=MAX_REVISION_BODY_BYTES)
            need(digest(body) == row["digest"], "integrity_error", "Traceability revision digest differs")
            row["body"] = body
        return row

    def _revision_stamp(self, row: dict[str, Any]) -> str:
        return digest({"revision": row["id"], "digest": row["digest"], "population": row["population_digest"], "status": row["status"]})

    def _check_cursor(self, cursor: str | None, expected: dict[str, Any]) -> dict[str, Any] | None:
        if cursor is None:
            return None
        value = _cursor_decode(cursor)
        for key in ("revision", "stamp", "query", "sort"):
            need(value.get(key) == expected.get(key), "stale_cursor", "Cursor is bound to a different traceability read snapshot", {"restart": True, "expected": expected, "cursor": value})
        return value

    @_read_transaction
    def get(self, actor, revision):
        row = self._revision(actor, revision)
        counts = row["body"].get("counts", {})
        body, truncated = _bounded_metadata(row["body"])
        result = {"revision": row["id"], "set_id": row["set_id"], "project": row["project"], "revision_number": row["revision"],
                  "status": row["status"], "digest": row["digest"], "population_digest": row["population_digest"],
                  "adapter": row["adapter"], "body": body, "body_truncated_fields": truncated,
                  "detail": f"traceability://revision/{row['id']}", "counts": counts,
                  "active": row["status"] == "active", "historical_only": row["status"] != "active"}
        _bounded_result(result)
        return result

    @_read_transaction
    def list(self, actor, project, set_id=None, kind=None, limit=100, cursor=None, offset=0):
        self._project(actor, project);limit = _page_limit(limit)
        need(set_id is None or isinstance(set_id, str), "invalid_input", "set_id must be a string")
        query = {"project": project, "set_id": set_id, "kind": kind}
        rows = self.s.all("""SELECT id,set_id,project,revision,status,digest,population_digest,adapter,
                                  json_extract(body,'$.kind') AS kind_label,
                                  json_extract(body,'$.counts') AS counts_json
                           FROM traceability_revisions
                           WHERE project=? AND (? IS NULL OR set_id=?)
                           ORDER BY set_id,revision,id""", (project, set_id, set_id))
        if kind is not None:
            rows = [row for row in rows if row.get("kind_label") == kind]
        stamp = digest([{row["id"]: {"digest": row["digest"], "population_digest": row["population_digest"], "status": row["status"]}} for row in rows])
        expected = {"revision": "list:" + project, "stamp": stamp, "query": query, "sort": "set_id,revision,id"}
        decoded = self._check_cursor(cursor, expected)
        if decoded is None:
            start = int(offset) if type(offset) is int and offset >= 0 else 0
            page_rows = rows[start:start + limit + 1]
        else:
            key = tuple(decoded.get("lastkey", ["", 0, ""]))
            page_rows = [row for row in rows if (row["set_id"], row["revision"], row["id"]) > key][:limit + 1]
        values = []
        for row in page_rows[:limit]:
            counts = parse_json(row["counts_json"]) if isinstance(row.get("counts_json"), str) else (row.get("counts_json") or {})
            values.append({"id": row["id"], "set_id": row["set_id"], "project": row["project"], "revision": row["revision"], "status": row["status"], "digest": row["digest"], "population_digest": row["population_digest"], "adapter": row["adapter"], "counts": counts})
        next_cursor = None
        if len(page_rows) > limit:
            last = page_rows[limit - 1]
            next_cursor = _cursor_encode({"format": "traceability.cursor.v1", **expected, "lastkey": [last["set_id"], last["revision"], last["id"]]})
        result = {"revisions": values, "items": values, "total": len(rows), "remaining": max(0, len(rows) - (len(rows) if next_cursor is None else (rows.index(page_rows[limit - 1]) + 1))), "next_cursor": next_cursor, "snapshot": stamp}
        _bounded_result(result)
        return result

    @_read_transaction
    def items(self, actor, revision, limit=100, cursor=None, offset=0, path=None, item_kind=None, status=None):
        # The immutable revision metadata is enough for a page stamp.  Avoid
        # parsing its potentially multi-megabyte inventory on every 100k-item
        # page; get() is the bounded metadata endpoint that reads the body.
        row = self._revision(actor, revision, include_body=False);limit = _page_limit(limit)
        query = {"path": path, "item_kind": item_kind, "status": status}
        where = ["revision=?"]
        filter_args: list[Any] = [revision]
        if path is not None: where.append("path=?"); filter_args.append(path)
        if item_kind is not None: where.append("item_kind=?"); filter_args.append(item_kind)
        if status is not None: where.append("status=?"); filter_args.append(status)
        where_sql = " AND ".join(where)
        # Counts and the bounded page are separate indexed queries.  In
        # particular, this never loads all item bodies for a 100k-item page.
        total = self.s.one(f"SELECT count(*) AS n FROM traceability_items WHERE {where_sql}", tuple(filter_args))["n"]
        unknown_count = self.s.one(f"SELECT count(*) AS n FROM traceability_items WHERE {where_sql} AND status='unknown' AND leaf=1", tuple(filter_args))["n"]
        undecided_count = self.s.one(f"SELECT count(*) AS n FROM traceability_items WHERE {where_sql} AND status='known' AND leaf=1", tuple(filter_args))["n"]
        stamp = self._revision_stamp(row)
        expected = {"revision": revision, "stamp": stamp, "query": query, "sort": "ordinal,id"}
        decoded = self._check_cursor(cursor, expected)
        if decoded is None:
            need(type(offset) is int and offset >= 0, "invalid_range", "offset must be nonnegative")
            start = offset
            page_sql = f"SELECT id,revision,project,ordinal,item_kind,path,status,start_byte,end_byte,body,digest,leaf FROM traceability_items WHERE {where_sql} ORDER BY ordinal,id LIMIT ? OFFSET ?"
            page_rows = self.s.all(page_sql, tuple(filter_args + [limit + 1, offset]))
        else:
            key = tuple(decoded.get("lastkey", [-1, ""]))
            need(type(key[0]) is int and isinstance(key[1], str), "invalid_cursor", "Cursor last key is malformed")
            page_where = f"{where_sql} AND (ordinal>? OR (ordinal=? AND id>?))"
            page_sql = f"SELECT id,revision,project,ordinal,item_kind,path,status,start_byte,end_byte,body,digest,leaf FROM traceability_items WHERE {page_where} ORDER BY ordinal,id LIMIT ?"
            page_rows = self.s.all(page_sql, tuple(filter_args + [key[0], key[0], key[1], limit + 1]))
            before = self.s.one(f"SELECT count(*) AS n FROM traceability_items WHERE {where_sql} AND (ordinal<? OR (ordinal=? AND id<=?))", tuple(filter_args + [key[0], key[0], key[1]]))["n"]
            start = before
        values = []
        for item in page_rows[:limit]:
            value = {"id": item["id"], "revision": revision, "ordinal": item["ordinal"], "item_kind": item["item_kind"], "path": item["path"], "status": item["status"], "leaf": bool(item["leaf"]), "start_byte": item["start_byte"], "end_byte": item["end_byte"], "digest": item["digest"]}
            # Metadata is bounded.  Full bytes/details are obtained through
            # read(), so a single giant source row cannot break page limits.
            # A single group may legitimately contain a large atom-id union.
            # Parse it within the explicit source bound, then omit that union
            # from the page response and leave complete details to read().
            body = parse_json(item["body"], limit=MAX_ITEM_BYTES)
            metadata, truncated = _bounded_metadata({key: val for key, val in body.items() if key != "atom_ids"})
            if truncated:
                metadata["truncated_fields"] = sorted(truncated)
            value["body"] = metadata
            value["detail"] = f"traceability://item/{revision}/{item['id']}"
            values.append(value)
        next_cursor = None
        if len(page_rows) > limit:
            last = page_rows[limit - 1]
            next_cursor = _cursor_encode({"format": "traceability.cursor.v1", **expected, "lastkey": [last["ordinal"], last["id"]]})
        consumed = start + len(values)
        result = {"revision": revision, "items": values, "total": total, "remaining": max(0, total - consumed),
                  "next_cursor": next_cursor, "snapshot": stamp,
                  "unknown_count": unknown_count, "undecided_count": undecided_count}
        _bounded_result(result)
        return result

    @_read_transaction
    def read(self, actor, revision, item=None, path=None, offset=0, limit=65536):
        row = self._revision(actor, revision, include_body=False)
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= MAX_PAGE_BYTES, "invalid_range", "Invalid item range")
        if item is not None:
            item_row = self.s.one("SELECT * FROM traceability_items WHERE revision=? AND id=?", (revision, item), True)
        else:
            need(isinstance(path, str), "invalid_input", "item or path is required")
            item_row = self.s.one("SELECT * FROM traceability_items WHERE revision=? AND path=? ORDER BY ordinal LIMIT 1", (revision, path), True)
        body = parse_json(item_row["body"], limit=MAX_ITEM_BYTES)
        bounded_body, truncated = _bounded_metadata(body)
        raw_digest = body.get("blob_digest") or body.get("source_span", {}).get("blob_digest")
        if raw_digest and _HEX64.fullmatch(raw_digest):
            raw = self.s.blob_get(raw_digest)
            need(offset <= len(raw), "invalid_range", "Item offset is past the source bytes")
            end = min(len(raw), offset + limit)
            chunk = raw[offset:end]
            result = {"revision": revision, "item": item_row["id"], "path": item_row["path"], "offset": offset,
                      "end": end, "total_bytes": len(raw), "sha256": raw_digest, "base64": base64.b64encode(chunk).decode(),
                      "next_offset": end if end < len(raw) else None, "body": bounded_body,
                      "body_truncated_fields": truncated}
            try:
                result["content"] = chunk.decode("utf-8")
            except UnicodeDecodeError:
                pass
            _bounded_result(result)
            return result
        result = {"revision": revision, "item": item_row["id"], "path": item_row["path"], "offset": 0, "end": 0, "total_bytes": 0, "base64": "", "next_offset": None,
                  "body": bounded_body, "body_truncated_fields": truncated}
        _bounded_result(result)
        return result

    @_read_transaction
    def diff(self, actor, revision, other_revision, limit=100, cursor=None):
        first = self._revision(actor, revision, include_body=False); second = self._revision(actor, other_revision, include_body=False)
        need(first["project"] == second["project"], "cross_project", "Cannot compare revisions from different projects")
        limit = _page_limit(limit)
        def identity(row):
            # Path/kind/byte interval is the immutable atom identity.  Symbol
            # names and line labels stay in the detail body, so diff does not
            # read every large body just to build a page.
            return (row["path"], row["item_kind"], row["start_byte"], row["end_byte"])
        columns = "path,item_kind,start_byte,end_byte,digest"
        left = {identity(row): row["digest"] for row in self.s.all(f"SELECT {columns} FROM traceability_items WHERE revision=?", (revision,))}
        right = {identity(row): row["digest"] for row in self.s.all(f"SELECT {columns} FROM traceability_items WHERE revision=?", (other_revision,))}
        changed = [{"kind": "added", "identity": key, "digest": right[key]} for key in sorted(right.keys(), key=repr) if key not in left]
        changed += [{"kind": "removed", "identity": key, "digest": left[key]} for key in sorted(left.keys(), key=repr) if key not in right]
        changed += [{"kind": "changed", "identity": key, "before": left[key], "after": right[key]} for key in sorted(left.keys() & right.keys(), key=repr) if left[key] != right[key]]
        stamp = digest({"left": first["digest"], "right": second["digest"], "count": len(changed)})
        start = 0
        if cursor:
            value = _cursor_decode(cursor);need(value.get("revision") == f"diff:{revision}:{other_revision}" and value.get("stamp") == stamp, "stale_cursor", "Diff cursor is stale", {"restart": True})
            start = value.get("index", 0)
        page = changed[start:start + limit]
        next_cursor = _cursor_encode({"format": "traceability.cursor.v1", "revision": f"diff:{revision}:{other_revision}", "stamp": stamp, "query": {}, "sort": "identity", "index": start + len(page)}) if start + len(page) < len(changed) else None
        result = {"revision": revision, "other_revision": other_revision, "changes": page, "total": len(changed), "remaining": len(changed) - start - len(page), "next_cursor": next_cursor, "snapshot": stamp}
        _bounded_result(result)
        return result

    # ---------- typed history surfaces / Unit B ----------
    @staticmethod
    def _packet_binding(body: dict[str, Any]) -> str:
        subject_key = "subject_ref" if "subject_ref" in body else "root_subject"
        fields = {key: body.get(key) for key in (
            "format", subject_key, "proposal", "proposal_digest", "revision",
            "revision_digest", "population_digest", "material_digest", "role",
            "packet_index", "packet_count", "leaf_ids", "required_coverage",
            "dependency_refs", "closure_ref", "stage") if key in body}
        return digest(fields)

    def _ensure_review_packets(self, project: str, proposal: str, revision: str | None,
                               required_coverage: list[str], role: str,
                               dependency_refs: list[dict[str, Any]], root_subject: dict[str, Any],
                               material_digest: str, closure_ref: dict[str, Any] | None = None,
                               stage: str | None = None) -> list[dict[str, Any]]:
        """Persist a fixed <=500-leaf packet set for a proposal/closure."""
        need(role in {"trace", "impact"}, "invalid_role", "Traceability packet role is invalid")
        need(isinstance(required_coverage, list) and len(set(required_coverage)) == len(required_coverage),
             "invalid_review_packet", "Review packet coverage is not a unique list")
        existing = []
        for row in self.s.all("SELECT * FROM traceability_records WHERE project=? AND kind='review_packet' ORDER BY created,id", (project,)):
            body = parse_json(row["body"], limit=MAX_REVISION_BODY_BYTES)
            subject_ref = body.get("subject_ref", body.get("root_subject"))
            if body.get("proposal") == proposal and subject_ref == root_subject and body.get("role") == role and body.get("closure_ref") == closure_ref:
                need(digest(body) == row["digest"], "integrity_error", "Review packet digest differs", row["id"])
                existing.append({**row, "body": body})
        if existing:
            existing.sort(key=lambda row: (row["body"].get("packet_index", -1), row["id"]))
            expected_count = max(1, (len(required_coverage) + MAX_PAGE - 1) // MAX_PAGE)
            need(len(existing) == expected_count, "invalid_review_packet", "Persisted review packet count differs")
            for index, packet in enumerate(existing):
                body = packet["body"]
                covered = required_coverage[index * MAX_PAGE:(index + 1) * MAX_PAGE]
                need(body.get("packet_index") == index and body.get("packet_count") == expected_count,
                     "invalid_review_packet", "Persisted review packet index/count differs", packet["id"])
                need(body.get("required_coverage") == covered and body.get("role") == role
                     and body.get("subject_ref", body.get("root_subject")) == root_subject
                     and body.get("closure_ref") == closure_ref,
                     "invalid_review_packet", "Persisted review packet material differs", packet["id"])
                need(body.get("binding") == self._packet_binding(body),
                     "integrity_error", "Persisted review packet binding differs", packet["id"])
            return existing
        packet_count = max(1, (len(required_coverage) + MAX_PAGE - 1) // MAX_PAGE)
        created = []
        for index in range(packet_count):
            covered = required_coverage[index * MAX_PAGE:(index + 1) * MAX_PAGE]
            body = {"format": "traceability.review-packet.v1", "kind": "review_packet",
                    "project": project, "proposal": proposal,
                    "proposal_digest": self.s.one("SELECT digest FROM traceability_proposals WHERE id=?", (proposal,), True)["digest"],
                    "subject_ref": root_subject, "revision": revision,
                    "revision_digest": self.s.one("SELECT digest FROM traceability_revisions WHERE id=?", (revision,))["digest"] if revision else None,
                    "population_digest": self.s.one("SELECT population_digest FROM traceability_revisions WHERE id=?", (revision,))["population_digest"] if revision else None,
                    "material_digest": material_digest, "role": role,
                    "packet_index": index, "packet_count": packet_count,
                    "leaf_ids": [value[5:] for value in covered if value.startswith("item:")],
                    "required_coverage": covered, "dependency_refs": dependency_refs}
            if closure_ref is not None: body["closure_ref"] = closure_ref
            if stage is not None: body["stage"] = stage
            body["binding"] = self._packet_binding(body)
            record_id = self._append_record(project, revision, proposal, "review_packet", body)
            created.append({"id": record_id, "project": project, "revision": revision,
                            "proposal": proposal, "kind": "review_packet", "body": body,
                            "digest": digest(body)})
        return created

    def _root_subject(self, actor, project: str, subject: str) -> tuple[str, dict[str, Any], dict[str, Any], str]:
        """Resolve a packet/root input to (table,row,root_ref,TPROP)."""
        self._project(actor, project)
        packet = self.s.one("SELECT * FROM traceability_records WHERE id=? AND project=?", (subject, project))
        if packet:
            # Keep the SQL row and its JSON body separate.  ``_row_json``
            # decodes the body column in place; it does not flatten the
            # decoded object into the row, and the record identity/digest
            # must remain the enclosing row for immutable subject refs.
            decoded = _row_json(packet)
            packet_body = decoded.get("body")
            need(isinstance(packet_body, dict), "integrity_error", "Traceability record body is malformed", subject)
            if packet.get("kind") == "review_packet":
                root = packet_body.get("subject_ref", packet_body.get("root_subject"))
                need(isinstance(root, dict) and isinstance(root.get("table"), str) and isinstance(root.get("id"), str),
                     "invalid_review_packet", "Review packet root subject is malformed")
                subject = root["id"]
            elif packet.get("kind") in {"closure_proposed", "closure_adopted"}:
                prop = packet_body.get("proposal")
                need(isinstance(prop, str), "missing_proposal", "Closure has no TPROP correspondence")
                # Keep the SQL row identity alongside its decoded body.  A
                # closure TREC body intentionally carries the proposal
                # subject, while the immutable record ID/digest live in the
                # enclosing row and are required for packet/adoption refs.
                return "traceability_records", decoded, self._subject_ref("traceability_records", decoded), prop
            else:
                raise Fault("invalid_subject", "Traceability record is not an adoption subject", subject)
        for table in ("traceability_proposals", "traceability_decisions", "traceability_mappings", "traceability_bindings"):
            row = self.s.one(f"SELECT * FROM {table} WHERE id=? AND project=?", (subject, project))
            if row is None: continue
            decoded = _row_json(row)
            root_ref = self._subject_ref(table, decoded)
            if table == "traceability_proposals":
                prop = decoded
                proposal = decoded["id"]
            else:
                b = decoded["body"]
                proposal = b.get("proposal")
                need(isinstance(proposal, str), "missing_proposal", f"{table} lacks its TPROP correspondence")
                prop = _row_json(self.s.one("SELECT * FROM traceability_proposals WHERE id=? AND project=?", (proposal, project), True))
            return table, decoded, root_ref, proposal
        raise Fault("not_found", "Unknown traceability adoption subject", subject)

    def _packets(self, project: str, root_ref: dict[str, Any], proposal: str) -> list[dict[str, Any]]:
        rows = []
        # The proposal FK is the durable reverse index for a packet set.  Do
        # not walk every project's history record for each page or review
        # request; a 100k-leaf proposal has only its own bounded packet rows.
        for row in self.s.all("SELECT * FROM traceability_records WHERE project=? AND proposal=? AND kind='review_packet' ORDER BY created,id", (project, proposal)):
            body = parse_json(row["body"], limit=MAX_REVISION_BODY_BYTES)
            subject_ref = body.get("subject_ref", body.get("root_subject"))
            proposal_ref = {"table": "traceability_proposals", "id": proposal,
                            "digest": self.s.one("SELECT digest FROM traceability_proposals WHERE id=?", (proposal,), True)["digest"]}
            if body.get("proposal") == proposal and subject_ref in (root_ref, proposal_ref):
                need(digest(body) == row["digest"], "integrity_error", "Review packet digest differs", row["id"])
                rows.append({**row, "body": body})
        rows.sort(key=lambda row: (row["body"].get("packet_index", -1), row["id"]))
        need(rows, "missing_review_packet", "Traceability subject has no persisted review packets")
        indexes = [row["body"].get("packet_index") for row in rows]
        need(indexes == list(range(len(rows))), "invalid_review_packet", "Review packet indexes are incomplete")
        need(len({row["body"].get("packet_count") for row in rows}) == 1 and rows[0]["body"].get("packet_count") == len(rows),
             "invalid_review_packet", "Review packet count differs")
        return rows

    def _packet_at(self, project: str, root_ref: dict[str, Any], proposal: str, packet: int) -> dict[str, Any]:
        """Read one fixed packet without decoding all sibling packet bodies.

        The packet count/index aggregate is evaluated by SQLite over the
        proposal's indexed rows.  The selected body is the only packet JSON
        decoded for a bounded public read; adoption still uses ``_packets`` to
        verify every receipt against the immutable set.
        """
        summary = self.s.one(
            """SELECT count(*) AS total, count(DISTINCT json_extract(body,'$.packet_index')) AS distinct_indexes,
                      min(json_extract(body,'$.packet_index')) AS first_index,
                      max(json_extract(body,'$.packet_index')) AS last_index,
                      min(json_extract(body,'$.packet_count')) AS packet_count,
                      max(json_extract(body,'$.packet_count')) AS max_packet_count
                 FROM traceability_records
                WHERE project=? AND proposal=? AND kind='review_packet'""",
            (project, proposal), True)
        total = summary["total"]
        need(type(total) is int and total > 0, "missing_review_packet", "Traceability subject has no persisted review packets")
        packet_count = summary["packet_count"]
        need(type(packet_count) is int and packet_count > 0
             and summary["max_packet_count"] == packet_count
             and summary["distinct_indexes"] == total
             and summary["first_index"] == 0
             and summary["last_index"] == total - 1
             and packet_count == total,
             "invalid_review_packet", "Review packet indexes or count are incomplete")
        need(type(packet) is int and 0 <= packet < packet_count, "not_found", "Review packet does not exist", packet)
        rows = self.s.all(
            """SELECT * FROM traceability_records
                WHERE project=? AND proposal=? AND kind='review_packet'
                  AND json_extract(body,'$.packet_index')=?
                ORDER BY created,id LIMIT 2""",
            (project, proposal, packet))
        need(len(rows) == 1, "invalid_review_packet", "Review packet index is duplicated or missing", packet)
        row = rows[0]
        body = parse_json(row["body"], limit=MAX_REVISION_BODY_BYTES)
        subject_ref = body.get("subject_ref", body.get("root_subject"))
        proposal_row = self.s.one("SELECT digest FROM traceability_proposals WHERE id=? AND project=?", (proposal, project), True)
        proposal_ref = {"table": "traceability_proposals", "id": proposal, "digest": proposal_row["digest"]}
        need(subject_ref in (root_ref, proposal_ref), "invalid_review_packet", "Review packet subject differs", row["id"])
        need(digest(body) == row["digest"], "integrity_error", "Review packet digest differs", row["id"])
        need(body.get("packet_index") == packet and body.get("packet_count") == packet_count,
             "invalid_review_packet", "Review packet index/count differs", row["id"])
        return {**row, "body": body}

    def _require_review_packets(self, actor, project: str, root_ref: dict[str, Any], proposal: str,
                                review_refs: Any, expected_role: str) -> list[dict[str, Any]]:
        packets = self._packets(project, root_ref, proposal)
        need(isinstance(review_refs, list) and len(review_refs) == len(packets),
             "review_required", "Every immutable traceability packet needs one observed review receipt",
             {"packets": [row["id"] for row in packets]})
        need(all(isinstance(value, str) for value in review_refs) and len(set(review_refs)) == len(review_refs),
             "invalid_evidence", "Review receipt IDs must be unique strings")
        observed = []
        for packet, receipt_id in zip(packets, review_refs):
            body = packet["body"]
            need(body.get("role") == expected_role, "invalid_review_packet", "Review role does not match adoption gate")
            need(body.get("binding") == self._packet_binding(body), "integrity_error", "Review packet binding differs", packet["id"])
            receipt = self.c.g.require_review(receipt_id, packet["id"], body["binding"], {expected_role})
            need(not receipt.get("simulated"), "unqualified_execution", "Traceability adoption requires an observed non-simulated review")
            need(receipt.get("subject") == packet["id"] and receipt.get("role") == expected_role,
                 "stale_evidence", "Review receipt subject or role differs")
            covered = receipt.get("result", {}).get("covered", [])
            required = body.get("required_coverage", [])
            need(set(covered) == set(required) and len(covered) == len(set(covered)),
                 "review_coverage", "Review receipt does not exactly cover its immutable packet", packet["id"])
            run = self.s.one("SELECT * FROM runs WHERE id=?", (receipt.get("run"),), True)
            need(run["status"] == "finished" and run["subject"] == packet["id"] and run["role"] == expected_role,
                 "invalid_evidence", "Review receipt run is not an independent finished observation")
            observed.append(receipt)
        return observed

    def _validate_contributors(self, project: str, value: Any) -> list[dict[str, Any]]:
        need(isinstance(value, list) and value, "contributors_required", "At least one required contributor is required")
        result = []
        seen = set()
        for contributor in value:
            obj(contributor, required=("task", "revision", "required"), optional=("label",), name="contributor")
            task = contributor["task"]; revision = contributor["revision"]
            need(isinstance(task, str) and type(revision) is int and revision > 0 and contributor["required"] is True,
                 "invalid_contributor", "Contributor must identify a required task revision")
            row = self.s.one("SELECT * FROM tasks WHERE id=?", (task,), True)
            need(row["project"] == project and row["status"] != "cancelled", "invalid_contributor", "Contributor task is unavailable")
            need(row["revision"] == revision, "stale_contributor", "Contributor task revision is not current")
            need(task not in seen, "invalid_contributor", "Contributor task is duplicated")
            seen.add(task);result.append({"task": task, "revision": revision, "required": True, **({"label": contributor["label"]} if "label" in contributor else {})})
        return result

    def _validate_decision_entries(self, actor, project: str, revision: str, decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        leaf_rows = {r["id"]: r for r in self.s.all("SELECT * FROM traceability_items WHERE revision=? AND leaf=1", (revision,))}
        need(leaf_rows, "invalid_decision", "Decision proposal cannot target a revision without leaves")
        result=[];refs=[]
        allowed=("item", "handling", "reason", "requirement", "acceptance", "design", "task", "contributors", "evidence", "input_requirements", "output_targets", "purpose")
        for raw in decisions:
            obj(raw, required=("item", "handling", "reason", "evidence"), optional=tuple(k for k in allowed if k not in {"item", "handling", "reason", "evidence"}), name="decision")
            item=raw["item"];need(item in leaf_rows,"unknown_item","Decision must name an exact immutable leaf",item)
            handling=raw["handling"];need(handling in {"undecided","port","replace","exclude"},"invalid_decision","Unknown item handling")
            item_body=parse_json(leaf_rows[item]["body"])
            text(raw["reason"], "decision reason", 20000)
            if raw.get("purpose") is not None:
                need(raw["purpose"] in {"code_port", "document_requirement"}, "invalid_decision", "Unknown decision purpose")
            if handling in {"port","replace"}:
                need(leaf_rows[item]["status"] == "known", "unknown_item", "Unknown extraction item cannot be adopted as port/replace", item)
                need(raw.get("task") is not None, "task_required", "Port/replace decisions need a responsible task")
                need(isinstance(raw.get("task"), str) and raw["task"], "task_required", "Port/replace decisions need a task ID")
                need(raw.get("purpose") in {"code_port", "document_requirement"}, "invalid_decision", "Port/replace decisions need a fixed purpose")
                contributors=self._validate_contributors(project, raw.get("contributors"))
                need(raw["task"] in {value["task"] for value in contributors},
                     "invalid_contributor", "Decision task must be one of its required contributors")
                need(raw.get("requirement") or raw.get("input_requirements"), "input_requirement_required", "Port/replace needs an enabling input requirement")
                input_refs = raw.get("input_requirements") or []
                need(isinstance(input_refs, list), "input_requirement_required", "input_requirements must be a list")
                for ref in ([raw.get("requirement")] if raw.get("requirement") is not None else []) + input_refs:
                    self._typed_resolve(actor, project, ref, require_current=True)
            if raw.get("output_targets") is not None:
                output_refs = raw["output_targets"]
                need(isinstance(output_refs, list), "invalid_reference", "output_targets must be a list")
                for ref in output_refs:
                    # Output obligations may still be pending at decision
                    # time.  Structural identity is required now; current
                    # acceptance is rechecked by the eventual mapping gate.
                    self._typed_resolve(actor, project, ref, require_current=False)
            if handling == "exclude":
                need(raw.get("reason"), "reason_required", "Exclusion needs an explicit reason")
            if raw.get("acceptance") is not None: self._typed_resolve(actor, project, raw["acceptance"], require_current=True)
            if raw.get("design") is not None: self._typed_resolve(actor, project, raw["design"], require_current=True)
            evidence=raw["evidence"];need(isinstance(evidence,list) and evidence,"evidence_required","Decision evidence must be explicit")
            for ev in evidence:
                need(isinstance(ev,str),"invalid_evidence","Decision evidence IDs must be strings")
                need(self.s.one("SELECT id FROM receipts WHERE id=? AND project=?",(ev,project)) or self.s.one("SELECT id FROM sources WHERE id=? AND project=?",(ev,project)) or self.s.one("SELECT id FROM artifacts WHERE id=? AND project=?",(ev,project)),"missing_evidence","Decision evidence does not exist",ev)
            normalized=dict(raw);normalized["contributors"] = self._validate_contributors(project, raw["contributors"]) if raw.get("contributors") else []
            result.append(normalized);refs.append(item)
        need(len(set(refs)) == len(refs), "duplicate_item", "A decision proposal cannot count one leaf twice")
        return result, refs

    def decide_propose(self, actor, project, revision, decisions, expected_digest=None, expected_head_record=None):
        self._project(actor, project);actor.require("owner", "agent", "worker", project=project)
        row = self._revision(actor, revision, include_body=False);need(row["project"] == project, "cross_project", "Revision belongs elsewhere")
        need(expected_digest is None or expected_digest == row["digest"], "stale_revision", "Revision digest differs")
        if isinstance(decisions, dict): decisions=[decisions]
        need(isinstance(decisions,list) and decisions,"invalid_decision","At least one item decision is required")
        normalized, refs = self._validate_decision_entries(actor, project, revision, decisions)
        self._check_head(project, expected_head_record)
        set_row=self.s.one("SELECT set_id FROM traceability_revisions WHERE id=?",(revision,),True)
        prop_id=uid("TPROP"); dec_id=uid("TDEC")
        prop_body={"format":TRACEABILITY_FORMAT,"id":prop_id,"project":project,"set_id":set_row["set_id"],"kind":"decision",
                   "scope":{"revision":revision,"purpose":sorted({entry.get("purpose") for entry in normalized if entry.get("purpose")})},
                   "revision":revision,"adapter":"traceability-decision-v1","expected_active":None,
                   "decision_id":dec_id,"decisions":normalized,"required_leaf_ids":sorted(refs),
                   "adapter_contract":{"kind":"decision","version":"v1"}}
        prop_digest=digest(prop_body);semantic=digest({"kind":"decision","scope":prop_body["scope"],"adapter":prop_body["adapter"]})
        body={"format":TRACEABILITY_FORMAT,"kind":"decision","id":dec_id,"proposal":prop_id,"revision":revision,"project":project,
              "decisions":normalized,"required_leaf_ids":sorted(refs),"expected_digest":row["digest"]}
        dec_digest=digest(body)
        with self.s.transaction():
            self._check_head(project, expected_head_record)
            self.s.execute("INSERT INTO traceability_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (prop_id,set_row["set_id"],project,"decision","proposed",canonical(prop_body).decode(),prop_digest,None,semantic,None,timestamp()))
            self.s.execute("INSERT INTO traceability_decisions VALUES(?,?,?,?,?,?,?)",(dec_id,revision,project,canonical(body).decode(),dec_digest,"proposed",timestamp()))
            self._append_record(project,revision,prop_id,"decision_proposed",self._record_body("decision_proposed",project,revision,prop_id,self._subject_ref("traceability_decisions",{"id":dec_id,"digest":dec_digest}),decision_id=dec_id,required_leaf_ids=sorted(refs),material_digest=semantic))
            decision_role = "impact" if any(entry.get("handling") == "exclude" for entry in normalized) else "trace"
            self._ensure_review_packets(project,prop_id,revision,["item:"+x for x in sorted(refs)],decision_role,
                                        [self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),self._subject_ref("traceability_decisions",{"id":dec_id,"digest":dec_digest}),self._subject_ref("traceability_revisions",{"id":revision,"digest":row["digest"]})],
                                        self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),
                                        self._decision_material_digest(prop_body, row["digest"]))
        return {"id":dec_id,"proposal":prop_id,"revision":revision,"status":"proposed","stored_status":"proposed","effective_status":"proposed","digest":dec_digest,"proposal_digest":prop_digest,"next_action":"traceability.review_subject","adoptable":True}

    def _decision_material_digest(self, proposal_body: dict[str, Any], revision_digest: str) -> str:
        return digest({"proposal":proposal_body.get("id"),"revision":proposal_body.get("revision"),"revision_digest":revision_digest,"decisions":proposal_body.get("decisions"),"required_leaf_ids":proposal_body.get("required_leaf_ids")})

    def _normalize_mapping_entries(self, actor, project: str, revision: str, mappings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        leaves={row["id"]:row for row in self.s.all("SELECT * FROM traceability_items WHERE revision=? AND leaf=1",(revision,))}
        result=[];all_leaves=[]
        for edge in mappings:
            obj(edge,required=("leaf_ids","purpose","decision_ref","contributors","target_refs","evidence_refs"),optional=(),name="mapping edge")
            purpose=edge["purpose"];need(purpose in {"code_port","document_requirement"},"invalid_mapping","Unknown mapping purpose")
            leaf_ids=edge["leaf_ids"];need(isinstance(leaf_ids,list) and leaf_ids and all(isinstance(x,str) for x in leaf_ids),"invalid_mapping","Mapping edge needs leaf IDs")
            need(len(set(leaf_ids))==len(leaf_ids),"invalid_mapping","Mapping edge repeats a leaf")
            need(set(leaf_ids)<=set(leaves),"unknown_item","Mapping edge names an unknown immutable leaf")
            # A TDEC identity is a local immutable relationship, not a free
            # text ID.  Accept the concise ID form at the API boundary but
            # store the canonical typed subject ref.
            ref=edge["decision_ref"]
            if isinstance(ref,str):
                drow=self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?",(ref,project),True)
                ref=self._subject_ref("traceability_decisions",drow)
            else:
                obj(ref,required=("table","id","digest"),optional=(),name="decision_ref")
                need(ref["table"]=="traceability_decisions","invalid_reference","Mapping decision_ref must be a TDEC")
                drow=self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?",(ref["id"],project),True)
                need(ref["digest"]==drow["digest"],"stale_reference","Decision digest differs")
            db=self._decision_body(drow);need(db.get("revision")==revision,"cross_revision","Mapping decision belongs to another revision")
            decision_entries={entry.get("item"): entry for entry in db.get("decisions", [])}
            for leaf_id in leaf_ids:
                decision = decision_entries.get(leaf_id)
                need(decision is not None, "planning_incomplete", "Mapping leaf is absent from its adopted decision", leaf_id)
                need(decision.get("handling") in {"port", "replace"}, "invalid_mapping", "Only port/replace decisions may be mapped", leaf_id)
            contributors=self._validate_contributors(project,edge["contributors"])
            target_refs=edge["target_refs"];need(isinstance(target_refs,list) and target_refs,"invalid_mapping","Mapping edge needs at least one typed target")
            normalized_targets=[]
            for target in target_refs:
                obj(target,required=("ref_type",),optional=tuple(k for k in target if k!="ref_type"),name="target_ref")
                # Resolve at proposal time so path/name-only or fabricated
                # candidates cannot become a stored edge.  require_current is
                # false only for the proposal: adoption rechecks currentness.
                self._typed_resolve(actor,project,target,require_current=False)
                normalized_targets.append(target)
            evidence=edge["evidence_refs"];need(isinstance(evidence,list) and evidence,"invalid_evidence","Mapping evidence is required")
            for ev in evidence:
                need(isinstance(ev,str),"invalid_evidence","Mapping evidence IDs must be strings")
                need(self.s.one("SELECT id FROM receipts WHERE id=? AND project=?",(ev,project)) or self.s.one("SELECT id FROM sources WHERE id=? AND project=?",(ev,project)) or self.s.one("SELECT id FROM artifacts WHERE id=? AND project=?",(ev,project)),"missing_evidence","Mapping evidence does not exist",ev)
            normalized={"leaf_ids":sorted(leaf_ids),"purpose":purpose,"decision_ref":ref,"contributors":contributors,"target_refs":normalized_targets,"evidence_refs":sorted(evidence)}
            result.append(normalized);all_leaves.extend(leaf_ids)
        return result,sorted(set(all_leaves))

    def map_propose(self, actor, project, revision, mappings, expected_digest=None, expected_head_record=None):
        self._project(actor,project);actor.require("owner","agent","worker",project=project)
        row=self._revision(actor,revision,include_body=False);need(row["project"]==project,"cross_project","Revision belongs elsewhere")
        need(expected_digest is None or expected_digest==row["digest"],"stale_revision","Revision digest differs")
        need(isinstance(mappings,list) and mappings,"invalid_mapping","At least one mapping edge is required")
        normalized,leaf_ids=self._normalize_mapping_entries(actor,project,revision,mappings)
        self._check_head(project,expected_head_record)
        set_id=self.s.one("SELECT set_id FROM traceability_revisions WHERE id=?",(revision,),True)["set_id"]
        prop_id=uid("TPROP");map_id=uid("TMAP")
        prop_body={"format":TRACEABILITY_FORMAT,"id":prop_id,"project":project,"set_id":set_id,"kind":"mapping",
                   "scope":{"revision":revision,"purpose":sorted({edge["purpose"] for edge in normalized})},"revision":revision,
                   "adapter":"traceability-mapping-v1","expected_active":None,"mapping_id":map_id,"mappings":normalized,
                   "required_leaf_ids":leaf_ids,"adapter_contract":{"kind":"mapping","version":"v1"}}
        prop_digest=digest(prop_body);semantic=digest({"kind":"mapping","scope":prop_body["scope"],"adapter":prop_body["adapter"]})
        body={"format":TRACEABILITY_FORMAT,"kind":"mapping","id":map_id,"proposal":prop_id,"revision":revision,"project":project,
              "mappings":normalized,"required_leaf_ids":leaf_ids,"expected_digest":row["digest"]}
        map_digest=digest(body)
        with self.s.transaction():
            self._check_head(project,expected_head_record)
            self.s.execute("INSERT INTO traceability_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)",(prop_id,set_id,project,"mapping","proposed",canonical(prop_body).decode(),prop_digest,None,semantic,None,timestamp()))
            self.s.execute("INSERT INTO traceability_mappings VALUES(?,?,?,?,?,?,?)",(map_id,revision,project,canonical(body).decode(),map_digest,"proposed",timestamp()))
            self._append_record(project,revision,prop_id,"mapping_proposed",self._record_body("mapping_proposed",project,revision,prop_id,self._subject_ref("traceability_mappings",{"id":map_id,"digest":map_digest}),mapping_id=map_id,required_leaf_ids=leaf_ids,material_digest=semantic))
            self._ensure_review_packets(project,prop_id,revision,["item:"+x for x in leaf_ids],"trace",
                                        [self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),self._subject_ref("traceability_mappings",{"id":map_id,"digest":map_digest}),self._subject_ref("traceability_revisions",{"id":revision,"digest":row["digest"]})],
                                        self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),
                                        digest({"proposal":prop_id,"revision":revision,"revision_digest":row["digest"],"mappings":normalized,"required_leaf_ids":leaf_ids}))
        return {"id":map_id,"proposal":prop_id,"revision":revision,"status":"proposed","stored_status":"proposed","effective_status":"proposed","digest":map_digest,"proposal_digest":prop_digest,"next_action":"traceability.review_subject","adoptable":True}

    def _task_assignment_contracts(self, project: str, revision: str,
                                   leaf_ids: Iterable[str], *, current: bool = True
                                   ) -> dict[str, list[dict[str, Any]]]:
        """Return the exact required Task revision contract for each leaf.

        The task ID alone is insufficient evidence: an adopted decision may
        require ``TASK@r1`` while the live task is already ``r2``.  ``current``
        is intentionally optional so gate code can first determine whether a
        particular Task is applicable.  An unrelated Task must not be blocked
        merely because another contributor has become stale; the applicable
        Task is rechecked with ``current=True`` before its own gate proceeds.
        """
        wanted = set(leaf_ids)
        decisions: dict[str, dict[str, Any]] = {}
        for row in self.s.all("SELECT * FROM traceability_decisions WHERE revision=? AND project=? ORDER BY created,id", (revision, project)):
            if self._effective_status("traceability_decisions", row["id"], project) != "accepted":
                continue
            body = self._decision_body(row)
            for entry in body.get("decisions", []):
                if entry.get("item") in wanted:
                    decisions[entry["item"]] = entry
        assignments: dict[str, list[dict[str, Any]]] = {}
        for leaf in sorted(wanted):
            entry = decisions.get(leaf)
            need(entry is not None and entry.get("handling") in {"port", "replace"},
                 "planning_incomplete", "Mapped leaf has no adopted port/replace decision", leaf)
            contributors = entry.get("contributors")
            need(isinstance(contributors, list) and contributors,
                 "contributors_required", "Mapped leaf has no required contributors", leaf)
            contracts: list[dict[str, Any]] = []
            seen: set[str] = set()
            for contributor in contributors:
                need(isinstance(contributor, dict) and isinstance(contributor.get("task"), str)
                     and type(contributor.get("revision")) is int and contributor.get("revision") > 0
                     and contributor.get("required") is True,
                     "invalid_contributor", "Mapped leaf contributor is malformed", leaf)
                task_id = contributor["task"]
                need(task_id not in seen, "invalid_contributor", "Mapped leaf repeats a contributor task", leaf)
                seen.add(task_id)
                contract = {"task": task_id, "revision": contributor["revision"], "required": True}
                if "label" in contributor:
                    contract["label"] = contributor["label"]
                if current:
                    task_row = self.s.one("SELECT project,revision,status FROM tasks WHERE id=?", (task_id,), True)
                    need(task_row["project"] == project and task_row["revision"] == contract["revision"]
                         and task_row["status"] != "cancelled",
                         "stale_contributor", "Mapped leaf contributor task revision is no longer current", leaf)
                contracts.append(contract)
            assignments[leaf] = sorted(contracts, key=lambda value: (value["task"], value["revision"]))
        return assignments

    def _task_assignments(self, project: str, revision: str, leaf_ids: Iterable[str]) -> dict[str, list[str]]:
        """Return contributor Task IDs after checking their exact revisions."""
        contracts = self._task_assignment_contracts(project, revision, leaf_ids, current=True)
        return {leaf: [value["task"] for value in values] for leaf, values in contracts.items()}

    def scope_propose(self, actor, project, revision, program, scope_requirement, applicable_from="plan", mandatory=True, expected_head_record=None):
        self._project(actor,project);actor.require("owner","agent","worker",project=project)
        need(isinstance(program,str) and program,"invalid_scope","program is required")
        prow=self.s.one("SELECT * FROM programs WHERE id=? AND project=?",(program,project),True)
        rev=self._revision(actor,revision,include_body=False)
        need(applicable_from in {"plan","implementation","integration","delivery"},"invalid_scope","Unknown mandatory phase")
        need(type(mandatory) is bool,"invalid_scope","mandatory must be Boolean")
        self._typed_resolve(actor,project,scope_requirement,require_current=True)
        self._check_head(project,expected_head_record)
        set_id=self.s.one("SELECT set_id FROM traceability_revisions WHERE id=?",(revision,),True)["set_id"]
        prop_id=uid("TPROP");bind_id=uid("TBIND")
        prop_body={"format":TRACEABILITY_FORMAT,"id":prop_id,"project":project,"set_id":set_id,"kind":"scope",
                   "scope":{"revision":revision,"program":program,"applicable_from":applicable_from,"mandatory":mandatory},
                   "revision":revision,"adapter":"traceability-scope-v1","expected_active":None,
                   "program":program,"scope_requirement":scope_requirement,"binding_id":bind_id,
                   "adapter_contract":{"kind":"scope","version":"v1"}}
        prop_digest=digest(prop_body);semantic=digest({"kind":"scope","scope":prop_body["scope"],"adapter":prop_body["adapter"]})
        binding_body={"format":TRACEABILITY_FORMAT,"kind":"scope_binding","id":bind_id,"proposal":prop_id,"project":project,"revision":revision,
                      "program":program,"scope_requirement":scope_requirement,"applicable_from":applicable_from,"mandatory":mandatory,
                      "population_digest":rev["population_digest"],"project_revision":prow["revision"]}
        binding_digest=digest(binding_body)
        with self.s.transaction():
            self._check_head(project,expected_head_record)
            self.s.execute("INSERT INTO traceability_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)",(prop_id,set_id,project,"scope","proposed",canonical(prop_body).decode(),prop_digest,None,semantic,None,timestamp()))
            self.s.execute("INSERT INTO traceability_bindings VALUES(?,?,?,?,?,?,?)",(bind_id,project,revision,canonical(binding_body).decode(),binding_digest,"pending",timestamp()))
            self._append_record(project,revision,prop_id,"scope_proposed",self._record_body("scope_proposed",project,revision,prop_id,self._subject_ref("traceability_bindings",{"id":bind_id,"digest":binding_digest}),binding_id=bind_id,material_digest=semantic,program=program))
            self._ensure_review_packets(project,prop_id,revision,["scope:"+prop_id],"impact",
                                        [self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),self._subject_ref("traceability_bindings",{"id":bind_id,"digest":binding_digest}),self._subject_ref("traceability_revisions",{"id":revision,"digest":rev["digest"]})],
                                        self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),
                                        digest({"proposal":prop_id,"revision":revision,"revision_digest":rev["digest"],"binding":binding_body}))
        return {"id":prop_id,"proposal":prop_id,"binding":bind_id,"revision":revision,"project":project,"status":"proposed","digest":prop_digest,"binding_digest":binding_digest,"next_action":"traceability.review_subject","adoptable":True}

    @_read_transaction
    def review_subject(self, actor, proposal, limit=MAX_PAGE, packet=0):
        need(type(packet) is int and packet>=0,"invalid_range","packet must be nonnegative")
        need(type(limit) is int and 1<=limit<=MAX_PAGE,"invalid_range","limit must be between 1 and 500")
        # A packet split is immutable and always uses MAX_PAGE leaves; a
        # caller's display limit cannot redefine the review material.
        row=None;project=None
        for table in ("traceability_proposals","traceability_decisions","traceability_mappings","traceability_bindings","traceability_records"):
            row=self.s.one(f"SELECT * FROM {table} WHERE id=?",(proposal,))
            if row:
                project=row["project"];break
        need(row is not None,"not_found","Unknown traceability proposal or packet")
        self._project(actor,project)
        if table=="traceability_records" and row["kind"]=="review_packet":
            body=parse_json(row["body"]);need(digest(body)==row["digest"],"integrity_error","Review packet digest differs")
            # A packet record is itself an immutable, indexed subject.  The
            # default page (0) is valid only for packet zero; accepting it for
            # every packet would let a caller silently review the wrong
            # material.
            need(body.get("packet_index")==packet,"invalid_range","Packet ID and index differ")
            return {"subject":row["id"],"binding":body.get("binding"),"required_coverage":body.get("required_coverage",[]),"packet":body,"review_required":True,"adoptable":True}
        root_table,root_row,root_ref,prop_id=self._root_subject(actor,project,proposal)
        selected=self._packet_at(project,root_ref,prop_id,packet)
        body=selected["body"]
        return {"subject":selected["id"],"binding":body["binding"],"required_coverage":body["required_coverage"],"packet":body,"review_required":True,"adoptable":True,"proposal":prop_id}

    def closure_propose(self, actor, project, revision, stage, task=None, delivery=None, expected_material_digest=None, expected_head_record=None):
        self._project(actor,project);actor.require("owner","agent","worker",project=project)
        need(stage in {"task","integrated","delivered"},"invalid_stage","Unknown traceability closure stage")
        rev=self._revision(actor,revision,include_body=False);need(rev["project"]==project,"cross_project","Revision belongs elsewhere")
        # The global closure denominator comes from the adopted decision
        # population, never from whichever leaves happened to receive a
        # mapping.  A task-stage closure is then narrowed to that Task's
        # assigned subset.  This ordering is essential: an unrelated Task's
        # unmapped/stale leaf must not block a valid local closure, while the
        # integrated and delivered gates still retain the whole denominator.
        required_leaf_ids=set()
        for decision_row in self.s.all("SELECT * FROM traceability_decisions WHERE revision=? AND project=? ORDER BY created,id", (revision, project)):
            if self._effective_status("traceability_decisions", decision_row["id"], project) != "accepted":
                continue
            decision_body=self._decision_body(decision_row)
            for entry in decision_body.get("decisions", []):
                if entry.get("handling") in {"port", "replace"}:
                    required_leaf_ids.add(entry.get("item"))
        need(required_leaf_ids,"planning_incomplete","Closure has no adopted port/replace leaf population")
        population_leaf_ids = sorted(required_leaf_ids)
        leaf_ids = population_leaf_ids
        assignment_contracts: dict[str, list[dict[str, Any]]] = {}
        if stage=="task":
            need(isinstance(task,str) and task,"task_required","Task closure needs a task")
            task_row=self.s.one("SELECT * FROM tasks WHERE id=? AND project=?",(task,project),True)
            need(task_row["status"]!="cancelled" and task_row["candidate"],"candidate_required","Task closure needs a sealed candidate")
            # First inspect retained decision contracts without checking live
            # revisions.  This determines applicability for this Task even if
            # another contributor has since gone stale.  Only the selected
            # subset is then revalidated against its exact required revision.
            retained = self._task_assignment_contracts(project, revision, population_leaf_ids, current=False)
            assigned_leaf_ids=sorted(leaf for leaf,contributors in retained.items()
                                     if any(value["task"] == task for value in contributors))
            need(assigned_leaf_ids,"task_assignment_missing","Task is not a required contributor for any mapped leaf",task)
            assignment_contracts = self._task_assignment_contracts(project, revision, assigned_leaf_ids, current=True)
            leaf_ids=assigned_leaf_ids
        target_leaf_ids = set(leaf_ids)
        maps=[]
        for row in self.s.all("SELECT * FROM traceability_mappings WHERE revision=? AND project=? ORDER BY created,id",(revision,project)):
            if self._effective_status("traceability_mappings",row["id"],project)!="accepted":
                continue
            body=self._mapping_body(row)
            selected_edges=[]
            for edge in body.get("mappings", []):
                edge_leaf_ids=set(edge.get("leaf_ids", []))
                if stage=="task" and not edge_leaf_ids & target_leaf_ids:
                    continue
                for ref in edge.get("target_refs", []):
                    self._typed_resolve(actor,project,ref,require_current=True)
                selected_edges.append(edge)
            if selected_edges:
                selected_leaf_ids=sorted({leaf for edge in selected_edges for leaf in edge.get("leaf_ids", [])})
                maps.append({"id":row["id"],"digest":row["digest"],"body":body,
                             "selected_leaf_ids":selected_leaf_ids})
        need(maps,"planning_incomplete","Closure needs an adopted mapping for its required leaf population")
        mapped_leaf_ids=sorted({leaf for mapping in maps for leaf in mapping["selected_leaf_ids"]})
        if stage=="task":
            need(target_leaf_ids <= set(mapped_leaf_ids), "planning_incomplete",
                 "Task closure mapping does not cover every assigned leaf",
                 {"missing": sorted(target_leaf_ids-set(mapped_leaf_ids))})
        else:
            need(required_leaf_ids <= set(mapped_leaf_ids), "planning_incomplete",
                 "Closure mapping does not cover every adopted port/replace leaf",
                 {"missing": sorted(required_leaf_ids-set(mapped_leaf_ids))})
        material={"revision":revision,"revision_digest":rev["digest"],"population_digest":rev["population_digest"],"stage":stage,
                  "population_leaf_ids":population_leaf_ids,
                  "mappings":[{"id":m["id"],"digest":m["digest"],"leaf_ids":m["selected_leaf_ids"]} for m in maps],
                  "leaf_ids":leaf_ids}
        if stage=="task":
            material.update({"task":task,"task_revision":task_row["revision"],"candidate":task_row["candidate"],"candidate_digest":self.s.one("SELECT digest FROM candidates WHERE id=?",(task_row["candidate"],),True)["digest"],"input_requirements":self.s.all("SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact",(task,)),"contributors":assignment_contracts})
        else:
            need(isinstance(delivery,str) and delivery,"delivery_required","Integrated/delivered closure needs a delivery")
            drow=self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?",(delivery,project),True);dbody=parse_json(drow["body"])
            material.update({"delivery":delivery,"delivery_digest":drow["digest"],"snapshot_digest":dbody.get("snapshot",{}).get("digest")})
            if stage=="delivered":
                need(drow["status"] in {"verified","delivered"} and isinstance(dbody.get("git"),dict) and dbody["git"],"delivery_required","Delivered closure needs observed repository commits")
                material["commit_refs"]={repo:{k:v for k,v in info.items() if k in {"commit","tree","ref","git_dir"}} for repo,info in dbody["git"].items()}
        material_digest=digest(material)
        need(expected_material_digest is None or expected_material_digest==material_digest,"stale_material","Closure material digest differs")
        self._check_head(project,expected_head_record)
        set_id=self.s.one("SELECT set_id FROM traceability_revisions WHERE id=?",(revision,),True)["set_id"]
        prop_id=uid("TPROP");closure_id=uid("TREC")
        prop_body={"format":TRACEABILITY_FORMAT,"id":prop_id,"project":project,"set_id":set_id,"kind":"mapping","scope":{"revision":revision,"stage":stage},"revision":revision,"adapter":"traceability-closure-v1","expected_active":None,"closure_stage":stage,"closure_material_digest":material_digest,"mapping_ids":[m["id"] for m in maps],"adapter_contract":{"kind":"closure","version":"v1"}}
        prop_digest=digest(prop_body)
        closure_body=self._record_body("closure_proposed",project,revision,prop_id,self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),closure_id=closure_id,stage=stage,material=material,material_digest=material_digest,mapping_ids=[m["id"] for m in maps],required_leaf_ids=leaf_ids)
        with self.s.transaction():
            self._check_head(project,expected_head_record)
            self.s.execute("INSERT INTO traceability_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)",(prop_id,set_id,project,"mapping","proposed",canonical(prop_body).decode(),prop_digest,None,digest({"kind":"mapping","scope":prop_body["scope"],"adapter":prop_body["adapter"]}),None,timestamp()))
            self.s.execute("INSERT INTO traceability_records(id,project,revision,proposal,kind,body,digest,created) VALUES(?,?,?,?,?,?,?,?)",(closure_id,project,revision,prop_id,"closure_proposed",canonical(closure_body).decode(),digest(closure_body),timestamp()))
            self._ensure_review_packets(project,prop_id,revision,["item:"+x for x in leaf_ids],"trace",
                                        [self._subject_ref("traceability_proposals",{"id":prop_id,"digest":prop_digest}),self._subject_ref("traceability_records",{"id":closure_id,"digest":digest(closure_body)}),self._subject_ref("traceability_revisions",{"id":revision,"digest":rev["digest"]})],
                                        self._subject_ref("traceability_records",{"id":closure_id,"digest":digest(closure_body)}),material_digest,closure_ref=self._subject_ref("traceability_records",{"id":closure_id,"digest":digest(closure_body)}),stage=stage)
        return {"id":closure_id,"subject":closure_id,"proposal":prop_id,"revision":revision,"stage":stage,"status":"proposed","material_digest":material_digest,"packet_count":max(1,(len(leaf_ids)+MAX_PAGE-1)//MAX_PAGE),"next_action":"traceability.closure_subject"}

    def closure_subject(self, actor, project, revision, packet=0, limit=MAX_PAGE, proposal=None):
        self._project(actor,project)
        need(proposal is not None,"missing_proposal","closure_subject requires the closure TREC ID")
        row=self.s.one("SELECT * FROM traceability_records WHERE id=? AND project=?",(proposal,project),True)
        need(row is not None,"not_found","Closure proposal record does not exist",proposal)
        need(row["kind"]=="closure_proposed" and row["revision"]==revision,"invalid_subject","Expected a closure_proposed TREC for this revision")
        return self.review_subject(actor,proposal,limit=limit,packet=packet)

    def _delivery_actual_target(self, actor, project: str, delivery_body: dict[str, Any], target: dict[str, Any],
                                *, require_commit: bool = True) -> dict[str, Any]:
        """Bind one adopted target to the immutable delivery snapshot.

        The candidate/population ref remains the semantic source identity.  A
        delivered record additionally captures the exact destination commit,
        tree, path, Git blob OID and CAS content identity observed after the
        delivery commit.  This keeps the post-commit observation separate
        from the pre-commit typed resolver and avoids inventing a future OID.
        """
        kind = target.get("ref_type")
        if kind in {"source_span", "artifact_ac"}:
            # Document requirements have no destination Git path.  The
            # accepted source/AC identity is still re-resolved at the
            # delivered boundary and retained as the actual target.
            self._typed_resolve(actor, project, target, require_current=True)
            return {"ref_type": kind, "typed_ref": target}
        need(kind in {"git_file", "git_symbol", "candidate_symbol"}, "invalid_reference", "Delivered target must be a resolvable file or symbol")
        repository = target.get("repository")
        path = target.get("path")
        need(isinstance(repository, str) and isinstance(path, str), "invalid_reference", "Delivered target lacks repository/path")
        self._typed_resolve(actor, project, target, require_current=True)
        snapshot = delivery_body.get("snapshot")
        repos = snapshot.get("repos") if isinstance(snapshot, dict) else None
        repo_snapshot = repos.get(repository) if isinstance(repos, dict) else None
        git_result = delivery_body.get("git", {}).get(repository) if isinstance(delivery_body.get("git"), dict) else None
        need(isinstance(repo_snapshot, dict) and isinstance(repo_snapshot.get("files"), dict),
             "stale_reference", "Delivered target repository is absent from the sealed snapshot", repository)
        entry = repo_snapshot["files"].get(path)
        need(isinstance(entry, dict) and entry.get("kind") == "file" and isinstance(entry.get("blob"), str),
             "stale_reference", "Delivered target path is absent from the sealed snapshot", path)
        commit = git_result.get("commit") if isinstance(git_result, dict) else None
        tree = git_result.get("tree") if isinstance(git_result, dict) else None
        if require_commit:
            need(isinstance(commit, str) and _OID.fullmatch(commit) and isinstance(tree, str) and _OID.fullmatch(tree),
                 "integrity_error", "Delivered Git result has no complete commit/tree identity", repository)
        raw = self.s.blob_get(entry["blob"])
        object_format = "sha256" if isinstance(commit, str) and len(commit) == 64 else "sha1"
        git_blob = hashlib.new(object_format, f"blob {len(raw)}\0".encode() + raw).hexdigest()
        actual = {"ref_type": kind, "repository": repository, "commit": commit, "tree": tree,
                  "path": path, "blob_oid": git_blob, "sha256": entry["blob"], "mode": entry.get("mode"),
                  "snapshot_digest": snapshot.get("digest")}
        if kind in {"git_symbol", "candidate_symbol"}:
            need(path.endswith((".py", ".pyi")), "unresolved_reference", "Delivered Python symbol path is not Python", path)
            parsed = partition_python(raw, path)
            symbols = [item["body"] for item in parsed.get("items", []) if item.get("item_kind") == "symbol"]
            matches = [item for item in symbols if item.get("qualified_name") == target.get("qualified_name")
                       and item.get("kind") == target.get("kind") and item.get("ordinal") == target.get("ordinal")]
            need(len(matches) == 1, "stale_reference", "Delivered target symbol identity cannot be resolved", target)
            symbol = matches[0]
            actual["symbol"] = {key: symbol.get(key) for key in
                                 ("qualified_name", "kind", "ordinal", "byte_start", "byte_end", "signature_hash")}
        return actual

    def record_delivered_mappings(self, actor, project: str, delivery: str,
                                  delivery_row: dict[str, Any] | None = None,
                                  delivery_body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Append observed destination mapping records after actual commits.

        This is called after every repository commit/reconciliation has
        produced an exact OID, in its own durable transaction before the
        final delivery status gate.  It is a durable observation, not an
        adoption shortcut: semantic mapping and the integrated review must
        already be current, and any delivered closure still requires its own
        actual review when the caller uses the closure API.
        """
        row = delivery_row or self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery, project), True)
        body = delivery_body or parse_json(row["body"])
        need(row["project"] == project and isinstance(body, dict), "integrity_error", "Delivery identity is malformed")
        program = body.get("binding", {}).get("program")
        bindings = self._mandatory_bindings(project, program=program, phase="delivery") if program else self._mandatory_bindings(project, phase="delivery")
        if not bindings:
            return []
        mapping_ids: set[str] = set()
        records: list[dict[str, Any]] = []
        for binding in bindings:
            self._binding_current(actor, project, binding)
            revision = binding["revision"]
            for mapping_row in self.s.all("SELECT * FROM traceability_mappings WHERE project=? AND revision=? ORDER BY created,id", (project, revision)):
                if mapping_row["id"] in mapping_ids or self._effective_status("traceability_mappings", mapping_row["id"], project) != "accepted":
                    continue
                mapping_ids.add(mapping_row["id"])
                mapping_body = self._mapping_body(mapping_row)
                actual = []
                for edge in mapping_body.get("mappings", []):
                    drow = self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?", (edge["decision_ref"]["id"], project), True)
                    self._validate_decision_current(actor, project, self._decision_body(drow))
                    for target in edge.get("target_refs", []):
                        actual.append({"leaf_ids": list(edge.get("leaf_ids", [])),
                                       "target_ref": target,
                                       "destination": self._delivery_actual_target(actor, project, body, target)})
                need(actual, "planning_incomplete", "Accepted mapping has no delivered targets", mapping_row["id"])
                commit_refs = {repo: {key: info.get(key) for key in ("commit", "tree", "ref", "git_dir")}
                               for repo, info in body.get("git", {}).items()}
                material = {"mapping_id": mapping_row["id"], "mapping_digest": mapping_row["digest"],
                            "delivery": delivery, "delivery_digest": row["digest"],
                            "snapshot_digest": body.get("snapshot", {}).get("digest"),
                            "commit_refs": commit_refs, "actual": actual}
                material_digest = digest(material)
                existing = self.s.one(
                    "SELECT * FROM traceability_records WHERE project=? AND kind='delivered_mapping' "
                    "AND json_extract(body,'$.delivery')=? AND json_extract(body,'$.mapping_id')=? "
                    "ORDER BY created DESC,id DESC LIMIT 1", (project, delivery, mapping_row["id"]))
                if existing is not None:
                    existing_body = parse_json(existing["body"])
                    need(digest(existing_body) == existing["digest"], "integrity_error", "Delivered mapping record digest differs", existing["id"])
                    need(existing_body.get("material_digest") == material_digest,
                         "write_conflict", "Delivered mapping observation differs for the same delivery", mapping_row["id"])
                    records.append({**existing, "body": existing_body})
                    continue
                record_body = self._record_body("delivered_mapping", project, revision, mapping_body["proposal"],
                                                self._subject_ref("traceability_mappings", mapping_row),
                                                mapping_id=mapping_row["id"], delivery=delivery,
                                                delivery_digest=row["digest"], snapshot_digest=body.get("snapshot", {}).get("digest"),
                                                commit_refs=commit_refs, actual=actual, material_digest=material_digest)
                record_id = self._append_record(project, revision, mapping_body["proposal"], "delivered_mapping", record_body)
                records.append({"id": record_id, "project": project, "revision": revision,
                                "proposal": mapping_body["proposal"], "kind": "delivered_mapping",
                                "body": record_body, "digest": digest(record_body)})
        return records

    def _validate_delivered_mapping_record(self, actor, project: str, record: dict[str, Any],
                                           delivery_row: dict[str, Any], delivery_body: dict[str, Any]) -> None:
        body = record.get("body")
        if isinstance(body, str):
            body = parse_json(body)
        need(isinstance(body, dict) and digest(body) == record.get("digest"),
             "integrity_error", "Delivered mapping record digest differs", record.get("id"))
        need(body.get("kind") == "delivered_mapping" and body.get("project") == project
             and body.get("delivery") == delivery_row["id"]
             and body.get("delivery_digest") == delivery_row["digest"],
             "stale_reference", "Delivered mapping is bound to another delivery", record.get("id"))
        mapping_id = body.get("mapping_id")
        mapping = self.s.one("SELECT * FROM traceability_mappings WHERE id=? AND project=?", (mapping_id, project), True)
        need(self._effective_status("traceability_mappings", mapping_id, project) == "accepted"
             and body.get("subject_ref") == self._subject_ref("traceability_mappings", mapping),
             "stale_reference", "Delivered mapping source mapping is no longer accepted", record.get("id"))
        mapping_body = self._mapping_body(mapping)
        current_commit_refs = {repo: {key: info.get(key) for key in ("commit", "tree", "ref", "git_dir")}
                               for repo, info in delivery_body.get("git", {}).items()}
        need(body.get("commit_refs") == current_commit_refs
             and body.get("snapshot_digest") == delivery_body.get("snapshot", {}).get("digest"),
             "stale_reference", "Delivered mapping destination commit changed", record.get("id"))
        actual = []
        for edge in mapping_body.get("mappings", []):
            drow = self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?", (edge["decision_ref"]["id"], project), True)
            if actor is not None:
                self._validate_decision_current(actor, project, self._decision_body(drow))
            for target in edge.get("target_refs", []):
                destination = self._delivery_actual_target(actor, project, delivery_body, target) if actor is not None else None
                if actor is not None:
                    actual.append({"leaf_ids": list(edge.get("leaf_ids", [])), "target_ref": target, "destination": destination})
        if actor is not None:
            need(body.get("actual") == actual, "stale_reference", "Delivered mapping target identity changed", record.get("id"))
        material = {"mapping_id": mapping_id, "mapping_digest": mapping["digest"],
                    "delivery": delivery_row["id"], "delivery_digest": delivery_row["digest"],
                    "snapshot_digest": delivery_body.get("snapshot", {}).get("digest"),
                    "commit_refs": body.get("commit_refs"), "actual": body.get("actual")}
        need(body.get("material_digest") == digest(material), "integrity_error", "Delivered mapping material digest differs", record.get("id"))

    def _adoption_record(self, project: str, root_table: str, root_id: str, kind: str) -> dict[str, Any] | None:
        for row in self.s.all("SELECT * FROM traceability_records WHERE project=? ORDER BY created DESC,id DESC",(project,)):
            if row["kind"]!=kind: continue
            body=parse_json(row["body"])
            need(digest(body) == row["digest"], "integrity_error", "Traceability adoption record digest differs", row["id"])
            ref=body.get("subject_ref")
            if isinstance(ref,dict) and ref.get("table")==root_table and ref.get("id")==root_id:
                return {**row,"body":body}
        return None

    def _validate_decision_current(self, actor, project: str, body: dict[str, Any],
                                   selected_items: set[str] | None = None) -> None:
        """Resolve enabling decision references against current rows.

        A task closure is a local gate over the leaves assigned to that task.
        A single TDEC may contain entries for several tasks, so callers that
        are validating such a local closure pass its selected leaf IDs.  The
        default remains the complete decision, which is required at the
        integrated and delivered population gates.
        """
        for entry in body.get("decisions", []):
            if selected_items is not None and entry.get("item") not in selected_items:
                continue
            if entry.get("handling") not in {"port", "replace"}:
                continue
            contributors=entry.get("contributors")
            need(isinstance(contributors, list) and contributors,
                 "stale_contributor", "Decision has no current required contributor")
            contributor_tasks={}
            for contributor in contributors:
                task_id=contributor.get("task") if isinstance(contributor, dict) else None
                task_row=self.s.one("SELECT project,revision,status FROM tasks WHERE id=?", (task_id,)) if isinstance(task_id, str) else None
                need(task_row is not None and task_row["project"] == project
                     and task_row["revision"] == contributor.get("revision")
                     and task_row["status"] != "cancelled",
                     "stale_contributor", "Decision contributor task revision is no longer current", entry.get("item"))
                need(task_id not in contributor_tasks,
                     "invalid_contributor", "Decision repeats a contributor task", entry.get("item"))
                contributor_tasks[task_id]=contributor.get("revision")
            need(entry.get("task") in contributor_tasks,
                 "stale_contributor", "Decision task is not a required current contributor", entry.get("item"))
            refs = ([] if entry.get("requirement") is None else [entry["requirement"]]) + list(entry.get("input_requirements") or [])
            for ref in refs:
                self._typed_resolve(actor, project, ref, require_current=True)
            for field in ("acceptance", "design"):
                if entry.get(field) is not None:
                    self._typed_resolve(actor, project, entry[field], require_current=True)

    def _validate_closure_dependencies(self, actor, project: str, body: dict[str, Any], stage: str) -> None:
        material=body.get("material",{})
        need(digest({k:material.get(k) for k in material if k not in {"generated_at"}})==body.get("material_digest"),"stale_material","Closure material no longer matches")
        required_leaf_ids=set()
        for decision_row in self.s.all("SELECT * FROM traceability_decisions WHERE revision=? AND project=? ORDER BY created,id", (material.get("revision"), project)):
            if self._effective_status("traceability_decisions", decision_row["id"], project) != "accepted":
                continue
            for entry in self._decision_body(decision_row).get("decisions", []):
                if entry.get("handling") in {"port", "replace"}:
                    required_leaf_ids.add(entry.get("item"))
        mapped_leaf_ids=set()
        covered_leaf_ids = set(body.get("required_leaf_ids", []))
        delivery_row = None
        delivery_body = None
        if stage in {"integrated", "delivered"}:
            delivery_row=self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (material.get("delivery"), project), True)
            delivery_body=parse_json(delivery_row["body"])
        for mapping in material.get("mappings",[]):
            row=self.s.one("SELECT * FROM traceability_mappings WHERE id=? AND project=?",(mapping["id"],project),True)
            need(row["digest"]==mapping["digest"] and self._effective_status("traceability_mappings",row["id"],project)=="accepted","stale_reference","Closure mapping is no longer current")
            mbody=self._mapping_body(row)
            selected_leaf_ids=set(mapping.get("leaf_ids", mbody.get("required_leaf_ids", [])))
            if stage == "task":
                mapped_leaf_ids.update(selected_leaf_ids & covered_leaf_ids)
            else:
                mapped_leaf_ids.update(mbody.get("required_leaf_ids", []))
            for edge in mbody["mappings"]:
                if stage == "task" and not set(edge.get("leaf_ids", [])) & covered_leaf_ids:
                    # This mapping row may also contain another Task's edge.
                    # Its target/currentness is outside this local closure.
                    continue
                drow=self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?",(edge["decision_ref"]["id"],project),True)
                selected_items = (set(edge.get("leaf_ids", [])) & covered_leaf_ids
                                  if stage == "task" else None)
                self._validate_decision_current(actor, project, self._decision_body(drow),
                                                selected_items=selected_items)
                for ref in edge["target_refs"]:
                    self._typed_resolve(actor,project,ref,require_current=True)
                    if stage in {"integrated", "delivered"}:
                        # Candidate/population identity alone is insufficient
                        # at the delivery boundary: resolve the same target
                        # against the frozen integration snapshot, and against
                        # the exact destination commit once it exists.
                        self._delivery_actual_target(actor, project, delivery_body, ref,
                                                     require_commit=stage == "delivered")
        # A task-stage closure is intentionally local to the contributor's
        # assigned leaves.  Integrated/delivered closures are the final
        # population closure and must cover the complete adopted
        # port/replace denominator.  Reusing the latter condition for task
        # closures would make a legitimate multi-task AND closure impossible
        # and would silently turn a local task gate into a global one.
        if stage == "task":
            need(covered_leaf_ids <= required_leaf_ids and covered_leaf_ids <= mapped_leaf_ids,
                 "stale_material", "Task closure covers an unknown or unmapped leaf")
        else:
            need(required_leaf_ids <= mapped_leaf_ids and required_leaf_ids <= covered_leaf_ids,
                 "stale_material", "Closure no longer covers every adopted port/replace leaf")
        if stage=="task":
            task=material.get("task");row=self.s.one("SELECT * FROM tasks WHERE id=? AND project=?",(task,project),True)
            need(row["revision"]==material.get("task_revision") and row["candidate"]==material.get("candidate") and row["status"]!="cancelled","stale_material","Task candidate or revision changed")
            candidate=self.s.one("SELECT * FROM candidates WHERE id=? AND task=?",(row["candidate"],task),True)
            need(candidate["digest"]==material.get("candidate_digest"),"stale_material","Candidate snapshot changed")
            reads=self.s.all("SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact",(task,))
            need(reads==material.get("input_requirements",[]),"stale_material","Task input requirements changed")
            assignment_contracts=self._task_assignment_contracts(project,material.get("revision"),material.get("leaf_ids",[]),current=True)
            need(material.get("contributors")=={leaf:assignment_contracts[leaf] for leaf in material.get("leaf_ids",[])},
                 "stale_material","Task contributor assignment changed")
            need(all(any(value["task"] == task for value in assignment_contracts[leaf])
                     for leaf in material.get("leaf_ids",[])),
                 "stale_material","Task is no longer assigned to a closure leaf")
        elif stage in {"integrated","delivered"}:
            need(delivery_row["digest"]==material.get("delivery_digest"),"stale_material","Delivery snapshot changed")
            snapshot=delivery_body.get("snapshot")
            need(isinstance(snapshot,dict) and snapshot.get("digest")==material.get("snapshot_digest"),
                 "stale_material","Integrated delivery snapshot changed")
            if stage=="delivered":
                need(delivery_row["status"] in {"verified","delivered"},"stale_material","Delivery is no longer certified")
                current_refs={repo:{k:value for k,value in info.items() if k in {"commit","tree","ref","git_dir"}}
                              for repo,info in delivery_body.get("git",{}).items()}
                need(current_refs==material.get("commit_refs"),"stale_material","Delivered commit references changed")

    def adopt(self, actor, project, revision=None, expected_digest=None, review_refs=None, subject=None, expected_active=None, expected_head_record=None):
        self._project(actor,project);actor.require("owner",project=project)
        need(isinstance(subject,str) and subject,"subject_required","Traceability adoption requires the exact proposal/packet subject")
        root_table,root_row,root_ref,prop_id=self._root_subject(actor,project,subject)
        prop=_row_json(self.s.one("SELECT * FROM traceability_proposals WHERE id=? AND project=?",(prop_id,project),True));pbody=prop["body"]
        # Public callers may retain the proposal ID returned by any of the
        # propose methods.  Resolve its immutable child row before dispatch so
        # TPROP/TDEC/TMAP/TBIND subjects have one common adoption path.
        if root_table == "traceability_proposals" and pbody.get("kind") == "decision":
            child_id=pbody.get("decision_id")
            child=self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?",(child_id,project),True)
            root_table,root_row,root_ref="traceability_decisions",_row_json(child),self._subject_ref("traceability_decisions",_row_json(child))
        elif root_table == "traceability_proposals" and pbody.get("kind") == "mapping" and pbody.get("mapping_id"):
            child_id=pbody.get("mapping_id")
            child=self.s.one("SELECT * FROM traceability_mappings WHERE id=? AND project=?",(child_id,project),True)
            root_table,root_row,root_ref="traceability_mappings",_row_json(child),self._subject_ref("traceability_mappings",_row_json(child))
        elif root_table == "traceability_proposals" and pbody.get("kind") == "scope":
            child_id=pbody.get("binding_id")
            child=self.s.one("SELECT * FROM traceability_bindings WHERE id=? AND project=?",(child_id,project),True)
            root_table,root_row,root_ref="traceability_bindings",_row_json(child),self._subject_ref("traceability_bindings",_row_json(child))
        root_body=pbody
        if root_table=="traceability_decisions": root_body=self._decision_body(root_row)
        elif root_table=="traceability_mappings": root_body=self._mapping_body(root_row)
        elif root_table=="traceability_bindings": root_body=self._binding_body(root_row)
        elif root_table=="traceability_records": root_body=root_row["body"]
        revision_id=(root_row.get("revision") if root_table!="traceability_proposals" else None) or pbody.get("revision") or pbody.get("scope",{}).get("revision")
        if root_table=="traceability_proposals" and pbody.get("kind") in {"population","code","document"}: revision_id=(prop.get("result") or {}).get("revision")
        need(isinstance(revision_id,str),"missing_revision","Adoption subject has no revision")
        need(revision is None or revision == revision_id, "stale_revision", "Adoption revision differs from the immutable subject")
        rev=self._revision(actor,revision_id,include_body=False);need(expected_digest is None or expected_digest==rev["digest"],"stale_revision","Revision digest differs")
        current_set=self.s.one("SELECT active_revision,active_digest FROM traceability_sets WHERE id=?",(rev["set_id"],),True)
        adoption_kind = {
            "traceability_proposals": "population_adopted",
            "traceability_bindings": "binding_adopted",
            "traceability_decisions": "decision_adopted",
            "traceability_mappings": "mapping_adopted",
            "traceability_records": "closure_adopted",
        }.get(root_table)
        if adoption_kind is not None:
            existing = self._adoption_record(project, root_table, root_row["id"], adoption_kind)
            if existing is not None:
                existing_body = existing["body"]
                if review_refs is not None:
                    need(list(review_refs) == list(existing_body.get("review_refs", [])), "idempotency_conflict", "Adoption review references differ")
                if expected_head_record is not None and existing_body.get("head_before") is not None:
                    need(expected_head_record == existing_body["head_before"], "write_conflict", "Adoption history head differs")
                if expected_active is not None:
                    need(expected_active == current_set["active_revision"] or expected_active == existing_body.get("active_before"),
                         "write_conflict", "Adoption active pointer differs")
                # A replay is still a currentness check.  Historical success
                # is never promoted back to a current PASS after its typed
                # input, target, candidate, or delivery has changed.
                try:
                    if root_table == "traceability_bindings":
                        self._binding_current(actor, project, {"body": self._binding_body(root_row), "revision": root_row["revision"]})
                    elif root_table == "traceability_decisions":
                        self._validate_decision_current(actor, project, root_body)
                    elif root_table == "traceability_mappings":
                        for edge in root_body.get("mappings", []):
                            drow=self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?",(edge["decision_ref"]["id"],project),True)
                            self._validate_decision_current(actor, project, self._decision_body(drow))
                            for target in edge.get("target_refs", []):
                                self._typed_resolve(actor, project, target, require_current=True)
                    elif root_table == "traceability_records" and root_body.get("kind") == "closure_proposed":
                        self._validate_closure_dependencies(actor, project, root_body, root_body.get("stage"))
                except Fault:
                    raise
                return {"subject":subject, "revision":revision_id, "record":existing,
                        "effective_status":({"population_adopted":"active", "binding_adopted":"mandatory",
                                              "decision_adopted":"accepted", "mapping_adopted":"accepted",
                                              "closure_adopted":str(root_body.get("stage")) + "_closure"}[adoption_kind]),
                        "material_digest":existing["body"].get("material_digest") or prop.get("semantic_material_digest"),
                        "review_refs":list(existing["body"].get("review_refs", [])), "adopted":True,
                        "replayed":True}
        if expected_active is not None:
            need(expected_active=={"revision":current_set["active_revision"],"digest":current_set["active_digest"]} or expected_active==current_set["active_revision"],"write_conflict","Active traceability pointer changed")
        expected_role="impact" if pbody.get("kind")=="scope" or (
            root_table == "traceability_decisions"
            and any(entry.get("handling") == "exclude" for entry in root_body.get("decisions", []))
        ) else "trace"
        self._require_review_packets(actor,project,root_ref,prop_id,review_refs,expected_role)
        head_before=self._latest_record_id(project)
        record=None;effective=None
        with self.s.transaction():
            current_head=self._check_head(project,expected_head_record)
            need(current_head == head_before, "write_conflict", "Traceability history head changed")
            if root_table=="traceability_proposals" and pbody.get("kind") in {"population","code","document"}:
                need(rev["status"] in {"ready","active"},"invalid_state","Population revision is not ready")
                need(prop.get("status") in {"ready","adopted"},"invalid_state","Population proposal is not ready")
                old=current_set["active_revision"]
                if old and old!=revision_id:self.s.execute("UPDATE traceability_revisions SET status='superseded' WHERE id=? AND status='active'",(old,))
                self.s.execute("UPDATE traceability_revisions SET status='active' WHERE id=? AND status IN ('ready','active')",(revision_id,))
                self.s.execute("UPDATE traceability_sets SET active_revision=?,active_digest=? WHERE id=?",(revision_id,rev["digest"],rev["set_id"]))
                self.s.execute("UPDATE traceability_proposals SET status='adopted',result=? WHERE id=? AND status IN ('ready','adopted')",(canonical({**(prop.get("result") or {}),"active":True,"status":"adopted"}).decode(),prop_id))
                body=self._record_body("population_adopted",project,revision_id,prop_id,root_ref,active_revision=revision_id,active_digest=rev["digest"],active_before={"revision":current_set["active_revision"],"digest":current_set["active_digest"]},head_before=head_before,review_refs=list(review_refs or []),material_digest=prop["semantic_material_digest"])
                record=self._append_record(project,revision_id,prop_id,"population_adopted",body);effective="active"
            elif root_table=="traceability_bindings" or pbody.get("kind")=="scope":
                bind_id=pbody.get("binding_id") or root_row.get("id")
                bind=self.s.one("SELECT * FROM traceability_bindings WHERE id=? AND project=?",(bind_id,project),True);bbody=self._binding_body(bind)
                self._typed_resolve(actor,project,bbody["scope_requirement"],require_current=True)
                need(bbody["mandatory"] is True,"invalid_scope","Only explicit mandatory scope can be adopted")
                self.s.execute("UPDATE traceability_proposals SET status='adopted' WHERE id=? AND status='proposed'",(prop_id,))
                body=self._record_body("binding_adopted",project,revision_id,prop_id,self._subject_ref("traceability_bindings",bind),binding_id=bind_id,program=bbody["program"],head_before=head_before,review_refs=list(review_refs or []),material_digest=prop["semantic_material_digest"])
                record=self._append_record(project,revision_id,prop_id,"binding_adopted",body);effective="mandatory"
            elif root_table=="traceability_decisions":
                need(self._effective_status("traceability_decisions",root_row["id"],project) in {"proposed","stale"},"invalid_state","Decision is already adopted or withdrawn")
                for entry in root_body["decisions"]:
                    if entry["handling"] in {"port","replace"}:
                        need(entry.get("task") is not None and entry.get("contributors"),"task_required","Adopted decision lacks task responsibility")
                        # Inputs and acceptance/design refs are targeted
                        # currentness gates.  A proposal may be staged while
                        # its inputs are replaced, but adoption must resolve
                        # the exact immutable candidate again immediately
                        # before appending decision_adopted.
                self._validate_decision_current(actor, project, root_body)
                self.s.execute("UPDATE traceability_proposals SET status='adopted' WHERE id=? AND status='proposed'",(prop_id,))
                body=self._record_body("decision_adopted",project,revision_id,prop_id,root_ref,decision_id=root_row["id"],required_leaf_ids=root_body["required_leaf_ids"],head_before=head_before,review_refs=list(review_refs or []),material_digest=prop["semantic_material_digest"])
                record=self._append_record(project,revision_id,prop_id,"decision_adopted",body);effective="accepted"
            elif root_table=="traceability_mappings":
                need(self._effective_status("traceability_mappings",root_row["id"],project) in {"proposed","stale"},"invalid_state","Mapping is already adopted or withdrawn")
                for edge in root_body["mappings"]:
                    drow=self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?",(edge["decision_ref"]["id"],project),True)
                    need(self._effective_status("traceability_decisions",drow["id"],project)=="accepted","planning_incomplete","Mapping decision is not adopted")
                    self._validate_decision_current(actor, project, self._decision_body(drow))
                    for target in edge["target_refs"]:self._typed_resolve(actor,project,target,require_current=True)
                self.s.execute("UPDATE traceability_proposals SET status='adopted' WHERE id=? AND status='proposed'",(prop_id,))
                body=self._record_body("mapping_adopted",project,revision_id,prop_id,root_ref,mapping_id=root_row["id"],required_leaf_ids=root_body["required_leaf_ids"],head_before=head_before,review_refs=list(review_refs or []),material_digest=prop["semantic_material_digest"])
                record=self._append_record(project,revision_id,prop_id,"mapping_adopted",body);effective="accepted"
            elif root_table=="traceability_records" and root_row["body"].get("kind")=="closure_proposed":
                stage=root_row["body"].get("stage");self._validate_closure_dependencies(actor,project,root_row["body"],stage)
                self.s.execute("UPDATE traceability_proposals SET status='adopted' WHERE id=? AND status='proposed'",(prop_id,))
                body=self._record_body("closure_adopted",project,revision_id,prop_id,root_ref,closure_ref=root_ref,stage=stage,material=root_row["body"].get("material"),required_leaf_ids=root_row["body"].get("required_leaf_ids",[]),head_before=head_before,review_refs=list(review_refs or []),material_digest=root_row["body"].get("material_digest"))
                record=self._append_record(project,revision_id,prop_id,"closure_adopted",body);effective=stage+"_closure"
            else:
                raise Fault("invalid_subject","Traceability adoption subject has no supported B kind")
        return {"subject":subject,"revision":revision_id,"record":record,"effective_status":effective,"material_digest":(record and record["body"] if False else (root_body.get("material_digest") or prop.get("semantic_material_digest"))),"review_refs":list(review_refs or []),"adopted":True}

    def map_adopt(self, actor, project, mapping, expected_digest=None, review_refs=None, expected_head_record=None):
        return self.adopt(actor,project,revision=None,expected_digest=expected_digest,review_refs=review_refs,subject=mapping,expected_head_record=expected_head_record)

    def _revision_coverage(self, project: str, revision: str, row: dict[str, Any], actor=None) -> dict[str, Any]:
        raw_counts=parse_json(self.s.one("SELECT json_extract(body,'$.counts') AS c FROM traceability_revisions WHERE id=?",(revision,),True)["c"])
        leaves=self.s.all("SELECT id,status FROM traceability_items WHERE revision=? AND leaf=1 ORDER BY ordinal,id",(revision,))
        leaf_ids={item["id"] for item in leaves}
        decisions={}; stale_decisions=set(); duplicate_decisions=set()
        excluded=set();unknown={item["id"] for item in leaves if item["status"]=="unknown"}
        for drow in self.s.all("SELECT * FROM traceability_decisions WHERE revision=? AND project=? ORDER BY created,id",(revision,project)):
            if self._effective_status("traceability_decisions",drow["id"],project)!="accepted": continue
            body=self._decision_body(drow)
            if actor is not None:
                try:
                    self._validate_decision_current(actor, project, body)
                except Fault:
                    stale_decisions.add(drow["id"])
                    continue
            for entry in body.get("decisions",[]):
                item=entry.get("item")
                if item in leaf_ids:
                    if item in decisions:
                        duplicate_decisions.add(item)
                    decisions[item]=entry
                    if entry.get("handling")=="exclude":excluded.add(item)
        undecided=leaf_ids-set(decisions)
        undecided.update(item for item,entry in decisions.items() if entry.get("handling")=="undecided")
        required={item for item,entry in decisions.items() if entry.get("handling") in {"port","replace"}}
        planning=bool(row["status"]=="active" and not unknown and not undecided and not stale_decisions and not duplicate_decisions)
        mappings=set()
        stale_mappings=set()
        for mrow in self.s.all("SELECT * FROM traceability_mappings WHERE revision=? AND project=? ORDER BY created,id",(revision,project)):
            if self._effective_status("traceability_mappings",mrow["id"],project)!="accepted":continue
            body=self._mapping_body(mrow)
            if actor is not None:
                try:
                    for edge in body.get("mappings", []):
                        for target in edge.get("target_refs", []):
                            self._typed_resolve(actor, project, target, require_current=True)
                except Fault:
                    stale_mappings.add(mrow["id"])
                    continue
            mappings.update(item for item in body.get("required_leaf_ids",[]) if item in leaf_ids)
        closure_leaves=set();task_closure_leaves=set();closure_stages=set();task_closure_by_task={}
        for record in self.s.all("SELECT * FROM traceability_records WHERE project=? AND revision=? AND kind='closure_adopted' ORDER BY created,id",(project,revision)):
            body=parse_json(record["body"])
            if actor is not None:
                try:
                    self._validate_closure_dependencies(actor, project, body, body.get("stage"))
                except Fault:
                    continue
            closure_stages.add(body.get("stage"));closure_leaves.update(item for item in body.get("required_leaf_ids",[]) if item in leaf_ids)
            if body.get("stage") == "task":
                task = body.get("material", {}).get("task")
                covered = {item for item in body.get("required_leaf_ids",[]) if item in leaf_ids}
                task_closure_leaves.update(covered)
                if isinstance(task, str):
                    task_closure_by_task.setdefault(task, set()).update(covered)
        task_assignments_ok = False
        if planning and required:
            try:
                assignments = self._task_assignments(project, revision, required)
                task_assignments_ok = all(
                    all(leaf in task_closure_by_task.get(task, set()) for task in contributors)
                    for leaf, contributors in assignments.items()
                )
            except Fault:
                task_assignments_ok = False
        execution=bool(planning and required<=mappings and required<=task_closure_leaves
                       and "task" in closure_stages and task_assignments_ok)
        # TBIND rows are immutable.  Their stored ``pending`` status remains
        # the proposal state after adoption; the effective mandatory state is
        # derived from the append-only binding_adopted record.
        mandatory=any(self._effective_status("traceability_bindings", binding["id"], project) == "mandatory"
                      for binding in self.s.all("SELECT id FROM traceability_bindings WHERE project=? AND revision=?",
                                                (project, revision)))
        return {"leaf_count":len(leaves),"unknown_count":len(unknown),"undecided_count":len(undecided),"excluded_count":len(excluded),
                "planning_coverage":planning,"execution_closure":execution,"adoption":row["status"]=="active",
                "mandatory_binding":mandatory,"empty_scope":bool(raw_counts.get("empty_scope") or raw_counts.get("empty_selection")),
                "empty_scope_requires_adoption":bool(raw_counts.get("empty_scope") or raw_counts.get("empty_selection")),
                "decision_count":len(decisions),"duplicate_decision_count":len(duplicate_decisions),
                "mapping_leaf_count":len(mappings),"closure_leaf_count":len(closure_leaves),
                "task_closure_leaf_count":len(task_closure_leaves)}

    @_read_transaction
    def coverage(self, actor, project, revision=None, limit=100, cursor=None):
        self._project(actor, project);limit=_page_limit(limit)
        if revision:
            metadata=self._revision(actor,revision,include_body=False);revisions=[metadata]
            stamp=digest({"revision":revision,"digest":metadata["digest"],"records":[{r["id"]:r["digest"]} for r in self.s.all("SELECT id,digest FROM traceability_records WHERE project=? AND revision=? ORDER BY id",(project,revision))]})
            expected={"revision":"coverage:"+revision,"stamp":stamp,"query":{"revision":revision},"sort":"created,id"}
        else:
            revisions=self.s.all("SELECT id,status,digest,population_digest,revision,created FROM traceability_revisions WHERE project=? ORDER BY created,id",(project,))
            stamp=digest({"revisions":[{r["id"]:{"digest":r["digest"],"status":r["status"]}} for r in revisions],"records":[{r["id"]:r["digest"]} for r in self.s.all("SELECT id,digest FROM traceability_records WHERE project=? ORDER BY id",(project,))]})
            expected={"revision":"coverage:"+project,"stamp":stamp,"query":{"revision":None},"sort":"created,id"}
        decoded=self._check_cursor(cursor,expected)
        if decoded is None:
            start=0;page=revisions[:limit+1]
        else:
            key=tuple(decoded.get("lastkey",[-1,""]));start=next((i for i,r in enumerate(revisions) if (r.get("created",0),r["id"])>key),len(revisions));page=[r for r in revisions if (r.get("created",0),r["id"])>key][:limit+1]
        page=page[:limit];reports=[]
        for row in page:
            reports.append({"revision":row["id"],"status":row["status"],"population_complete":row["status"] in {"ready","active","superseded"},**self._revision_coverage(project,row["id"],row,actor),"unit":"B","pending_unit_b":False})
        next_cursor=None
        if len(revisions)>start+len(reports):
            last=page[-1];next_cursor=_cursor_encode({"format":"traceability.cursor.v1",**expected,"lastkey":[last.get("created",0),last["id"]]})
        result={"project":project,"revisions":reports,"total":len(revisions),"remaining":max(0,len(revisions)-(start+len(reports))),"next_cursor":next_cursor,"snapshot":stamp,
                "population_complete":bool(reports) and all(r["population_complete"] for r in reports) and next_cursor is None,
                "planning_coverage":bool(reports) and all(r["planning_coverage"] for r in reports),"execution_closure":bool(reports) and all(r["execution_closure"] for r in reports),"unit":"B","adoption":bool(reports) and all(r["adoption"] for r in reports)}
        _bounded_result(result);return result

    _B_PHASE_ORDER = {"plan": 0, "implementation": 1, "integration": 2, "delivery": 3}

    def _mandatory_bindings(self, project: str, revision: str | None = None,
                            program: str | None = None, phase: str | None = None) -> list[dict[str, Any]]:
        rows=[]
        for row in self.s.all("SELECT * FROM traceability_bindings WHERE project=?" + (" AND revision=?" if revision else ""), (project,revision) if revision else (project,)):
            body=self._binding_body(row)
            if program is not None and body.get("program")!=program: continue
            if self._effective_status("traceability_bindings",row["id"],project)!="mandatory": continue
            if phase is not None:
                need(phase in self._B_PHASE_ORDER, "invalid_phase", "Unknown traceability gate phase")
                applicable = body.get("applicable_from")
                need(applicable in self._B_PHASE_ORDER, "invalid_scope", "Mandatory binding phase is malformed")
                if self._B_PHASE_ORDER[applicable] > self._B_PHASE_ORDER[phase]:
                    continue
            rows.append({**row,"body":body})
        return rows

    def _binding_current(self, actor, project: str, binding: dict[str, Any]) -> None:
        """Recheck a mandatory scope's immutable population and requirement.

        Binding adoption is append-only, so a later replacement of the
        enabling requirement or population must make only this binding stale.
        The binding is retained in history and the gate reports the failure;
        it is never silently dropped from the mandatory set.
        """
        body = binding["body"]
        revision = self.s.one("SELECT * FROM traceability_revisions WHERE id=? AND project=?", (binding["revision"], project), True)
        program = self.s.one("SELECT id,project,revision FROM programs WHERE id=?", (body.get("program"),), True)
        need(program["project"] == project, "cross_project", "Mandatory traceability program belongs elsewhere")
        need(body.get("project_revision") == program["revision"], "stale_reference", "Mandatory traceability program changed")
        need(body.get("population_digest") == revision["population_digest"], "stale_reference", "Mandatory traceability population changed")
        self._typed_resolve(actor, project, body["scope_requirement"], require_current=True)

    def _programs_for_task(self, project: str, task: str) -> set[str]:
        """Find active program ownership without adding a Task schema column."""
        programs = set()
        for row in self.s.all("SELECT program,body FROM breakdowns WHERE project=? AND status='active'", (project,)):
            body = parse_json(row["body"])
            if any(task in unit.get("tasks", []) for unit in body.get("units", [])):
                programs.add(row["program"])
        return programs

    def _program_for_delivery(self, project: str, delivery: str) -> str | None:
        row = self.s.one("SELECT body FROM deliveries WHERE id=? AND project=?", (delivery, project))
        if not row:
            return None
        body = parse_json(row["body"])
        value = body.get("binding", {}).get("program")
        return value if isinstance(value, str) and value else None

    def planning_gate(self, project: str, program: str, actor=None) -> dict[str, Any]:
        bindings=self._mandatory_bindings(project,program=program,phase="plan")
        failures=[]
        for binding in bindings:
            if actor is not None:
                try:
                    self._binding_current(actor, project, binding)
                except Fault:
                    failures.append("traceability_binding_stale:"+binding["id"])
                    continue
            rev=self.s.one("SELECT * FROM traceability_revisions WHERE id=? AND project=?",(binding["revision"],project),True)
            report=self._revision_coverage(project,rev["id"],rev,actor)
            if not report["planning_coverage"]: failures.append("planning_coverage:"+rev["id"])
        return {"mandatory":bool(bindings),"allowed":not failures,"failures":failures}

    def task_closure_gate(self, project: str, task: str, actor=None) -> dict[str, Any]:
        programs = self._programs_for_task(project, task)
        bindings=[]
        for program in sorted(programs):
            bindings.extend(self._mandatory_bindings(project,program=program,phase="implementation"))
        if not programs:
            bindings=self._mandatory_bindings(project,phase="implementation")
        failures=[]
        for binding in bindings:
            rev=binding["revision"]
            # A mandatory binding applies only to tasks that are actually
            # assigned to an adopted port/replace leaf.  Requiring an empty
            # closure from every unrelated project task would deadlock legacy
            # work and would contradict the contributor AND contract.
            try:
                decision_leaf_ids=set()
                for decision_row in self.s.all("SELECT * FROM traceability_decisions WHERE revision=? AND project=? ORDER BY created,id", (rev, project)):
                    if self._effective_status("traceability_decisions", decision_row["id"], project) != "accepted":
                        continue
                    for entry in self._decision_body(decision_row).get("decisions", []):
                        if entry.get("handling") in {"port", "replace"}:
                            decision_leaf_ids.add(entry.get("item"))
                # Applicability is derived from the retained adopted
                # assignment rows before currentness checks.  This lets an
                # unrelated legacy Task continue through its ordinary gate
                # even when a different assigned Task's binding is stale.
                retained=self._task_assignment_contracts(project, rev, decision_leaf_ids, current=False) if decision_leaf_ids else {}
                expected_for_task={leaf for leaf, contributors in retained.items()
                                   if any(value["task"] == task for value in contributors)}
            except Fault:
                failures.append("task_assignment:"+rev)
                continue
            if not expected_for_task:
                continue
            if actor is not None:
                try:
                    self._binding_current(actor, project, binding)
                except Fault:
                    failures.append("traceability_binding_stale:"+binding["id"])
                    continue
            try:
                # Recheck exact Task revisions only for an applicable Task;
                # stale required contributors remain a real failure here.
                self._task_assignment_contracts(project, rev, expected_for_task, current=True)
            except Fault:
                failures.append("task_assignment:"+rev)
                continue
            matching=[]
            for row in self.s.all("SELECT * FROM traceability_records WHERE project=? AND revision=? AND kind='closure_adopted' ORDER BY created,id",(project,rev)):
                body=parse_json(row["body"]);material=body.get("material",{})
                if body.get("stage")=="task" and material.get("task")==task:
                    if actor is not None:
                        try:
                            self._validate_closure_dependencies(actor, project, body, "task")
                        except Fault:
                            continue
                    matching.append(body)
            covered={leaf for body in matching for leaf in body.get("required_leaf_ids", [])}
            if not matching or not expected_for_task <= covered:
                failures.append("task_closure:"+rev)
        return {"mandatory":bool(bindings),"allowed":not failures,"failures":failures}

    def integrated_closure_gate(self, project: str, delivery: str, actor=None) -> dict[str, Any]:
        program = self._program_for_delivery(project, delivery)
        bindings=self._mandatory_bindings(project,program=program,phase="integration") if program else self._mandatory_bindings(project,phase="integration")
        failures=[]
        for binding in bindings:
            if actor is not None:
                try:
                    self._binding_current(actor, project, binding)
                except Fault:
                    failures.append("traceability_binding_stale:"+binding["id"])
                    continue
            rev=binding["revision"]
            matching=[]
            for row in self.s.all("SELECT * FROM traceability_records WHERE project=? AND revision=? AND kind='closure_adopted'",(project,rev)):
                body=parse_json(row["body"]);material=body.get("material",{})
                if body.get("stage")=="integrated" and material.get("delivery")==delivery:
                    if actor is not None:
                        try:
                            self._validate_closure_dependencies(actor, project, body, "integrated")
                        except Fault:
                            continue
                    matching.append(body)
            if not matching:
                failures.append("integrated_closure:"+rev)
        return {"mandatory":bool(bindings),"allowed":not failures,"failures":failures}

    def delivered_closure_gate(self, project: str, delivery: str, actor=None) -> dict[str, Any]:
        program = self._program_for_delivery(project, delivery)
        bindings=self._mandatory_bindings(project,program=program,phase="delivery") if program else self._mandatory_bindings(project,phase="delivery")
        failures=[]
        delivery_row=self.s.one("SELECT * FROM deliveries WHERE id=? AND project=?", (delivery, project))
        delivery_body=parse_json(delivery_row["body"]) if delivery_row else None
        for binding in bindings:
            if actor is not None:
                try:
                    self._binding_current(actor, project, binding)
                except Fault:
                    failures.append("traceability_binding_stale:"+binding["id"])
                    continue
            rev=binding["revision"]
            # The final boundary is the actual destination observation for
            # every accepted mapping.  A delivered closure review may add
            # context, but it cannot stand in for a post-commit mapping
            # record or make a partial mapping set complete.
            expected_mapping_ids=set()
            expected_leaf_ids=set()
            for mapping_row in self.s.all(
                    "SELECT * FROM traceability_mappings WHERE project=? AND revision=? ORDER BY created,id",
                    (project, rev)):
                if self._effective_status("traceability_mappings", mapping_row["id"], project) != "accepted":
                    continue
                try:
                    mapping_body=self._mapping_body(mapping_row)
                    for edge in mapping_body.get("mappings", []):
                        drow=self.s.one("SELECT * FROM traceability_decisions WHERE id=? AND project=?",
                                        (edge["decision_ref"]["id"], project), True)
                        if actor is not None:
                            self._validate_decision_current(actor, project, self._decision_body(drow))
                            for target in edge.get("target_refs", []):
                                self._typed_resolve(actor, project, target, require_current=True)
                    expected_mapping_ids.add(mapping_row["id"])
                    expected_leaf_ids.update(mapping_body.get("required_leaf_ids", []))
                except Fault:
                    # A stale accepted mapping is still a mandatory historical
                    # dependency.  Keep it in the expected set so delivery
                    # cannot pass by silently dropping its leaves.
                    expected_mapping_ids.add(mapping_row["id"])
            required_leaf_ids=set()
            for decision_row in self.s.all(
                    "SELECT * FROM traceability_decisions WHERE project=? AND revision=? ORDER BY created,id",
                    (project, rev)):
                if self._effective_status("traceability_decisions", decision_row["id"], project) != "accepted":
                    continue
                decision_body=self._decision_body(decision_row)
                required_leaf_ids.update(entry.get("item") for entry in decision_body.get("decisions", [])
                                         if entry.get("handling") in {"port", "replace"})
            if not expected_mapping_ids or not required_leaf_ids <= expected_leaf_ids:
                failures.append("delivered_mapping_population:"+rev)
                continue
            if delivery_row is None or delivery_body is None:
                failures.append("delivered_mapping:"+rev)
                continue
            delivered_rows=self.s.all(
                "SELECT * FROM traceability_records WHERE project=? AND revision=? AND kind='delivered_mapping' "
                "AND json_extract(body,'$.delivery')=? ORDER BY created,id", (project, rev, delivery))
            valid_by_mapping={}
            invalid=[]
            for delivered in delivered_rows:
                try:
                    self._validate_delivered_mapping_record(actor, project, delivered, delivery_row, delivery_body)
                    delivered_body=delivered.get("body")
                    if isinstance(delivered_body, str):
                        delivered_body=parse_json(delivered_body)
                    mapping_id=delivered_body.get("mapping_id") if isinstance(delivered_body, dict) else None
                    if mapping_id in valid_by_mapping:
                        invalid.append(mapping_id)
                    else:
                        valid_by_mapping[mapping_id]=delivered_body
                except Fault:
                    invalid.append(delivered.get("id"))
            missing=expected_mapping_ids-set(valid_by_mapping)
            if missing or invalid:
                failures.append("delivered_mapping:"+rev)
                continue
            observed_leaf_ids={leaf for body in valid_by_mapping.values()
                               for actual in body.get("actual", [])
                               for leaf in actual.get("leaf_ids", [])}
            if required_leaf_ids - observed_leaf_ids:
                failures.append("delivered_mapping_coverage:"+rev)
        return {"mandatory":bool(bindings),"allowed":not failures,"failures":failures}

    @_read_transaction
    def history(self, actor, project, limit=100, cursor=None):
        self._project(actor, project);limit = _page_limit(limit)
        # Snapshot/stamp uses only bounded metadata.  Page bodies are fetched
        # by keyset below, so a large staging record is never re-read for
        # every history page.
        metadata = self.s.all("SELECT id,created,digest FROM traceability_records WHERE project=? ORDER BY created,id", (project,))
        stamp = digest([{r["id"]: r["digest"]} for r in metadata])
        expected = {"revision": "history:" + project, "stamp": stamp, "query": {}, "sort": "created,id"}
        value = self._check_cursor(cursor, expected)
        lastkey = tuple(value.get("lastkey", [-1, ""])) if value is not None else (-1, "")
        start = next((i for i, row in enumerate(metadata) if (row["created"], row["id"]) > lastkey), len(metadata))
        rows = self.s.all(
            "SELECT * FROM traceability_records WHERE project=? AND (created>? OR (created=? AND id>?)) ORDER BY created,id LIMIT ?",
            (project, lastkey[0], lastkey[0], lastkey[1], limit + 1),
        )
        page = [_bounded_history_row(row) for row in rows]
        next_cursor = None
        if len(page) > limit:
            last = page[limit - 1];next_cursor = _cursor_encode({"format": "traceability.cursor.v1", **expected, "lastkey": [last["created"], last["id"]]})
            page = page[:limit]
        result = {"records": page, "history": page, "total": len(metadata), "remaining": max(0, len(metadata) - start - len(page)), "next_cursor": next_cursor, "snapshot": stamp, "history_version": HISTORY_VERSION}
        _bounded_result(result)
        return result

    # ---------- archive/export/import ----------
    @_read_transaction
    def _archive_payload(self, actor, project):
        self._project(actor, project)
        project_row = self.s.one("SELECT * FROM projects WHERE id=?", (project,), True)
        tables = {table: self.s.all(f"SELECT * FROM {table} WHERE project=? ORDER BY id", (project,)) for table in (
            "traceability_sets", "traceability_revisions", "traceability_items", "traceability_proposals", "traceability_decisions", "traceability_mappings", "traceability_bindings", "traceability_records")}
        decoded = {}
        for table, rows in tables.items():
            decoded[table] = [_row_json(row) for row in rows]
        # Typed candidate/artifact references are validated against this
        # immutable context when it is available.  The context is historical
        # metadata only; import still restores traceability rows/CAS and never
        # turns old runtime rows or receipts into fresh evidence.
        context = {
            "artifacts": [_row_json(row) for row in self.s.all(
                "SELECT * FROM artifacts WHERE project=? ORDER BY id", (project,))],
            "revisions": [_row_json(row) for row in self.s.all(
                "SELECT r.* FROM revisions r JOIN artifacts a ON a.id=r.artifact WHERE a.project=? ORDER BY r.artifact,r.revision", (project,))],
            "sources": [_row_json(row) for row in self.s.all(
                "SELECT * FROM sources WHERE project=? ORDER BY id", (project,))],
            "tasks": [_candidate_context_row("tasks", row) for row in self.s.all(
                "SELECT * FROM tasks WHERE project=? ORDER BY id", (project,))],
            "candidates": [_candidate_context_row("candidates", row) for row in self.s.all(
                "SELECT c.*, t.project AS project FROM candidates c JOIN tasks t ON t.id=c.task WHERE t.project=? ORDER BY c.id", (project,))],
            "runs": [_candidate_context_row("runs", row) for row in self.s.all(
                "SELECT * FROM runs WHERE project=? ORDER BY id", (project,))],
            "receipts": [_candidate_context_row("receipts", row) for row in self.s.all(
                "SELECT * FROM receipts WHERE project=? ORDER BY id", (project,))],
            "repos": [_candidate_context_row("repos", row) for row in self.s.all(
                "SELECT * FROM repos WHERE project=? ORDER BY id", (project,))],
            # Candidate references may legitimately point at a retained
            # pre-replan candidate.  The current Task row alone cannot prove
            # that historical association, so retain the immutable
            # before/after definition records as typed context as well.
            "task_revision_history": [_candidate_context_row("task_revision_history", row) for row in self.s.all(
                "SELECT * FROM task_revision_history WHERE project=? ORDER BY task,to_revision,id", (project,))],
        }
        hashes: set[str] = set()
        for table_rows in decoded.values():
            for row in table_rows:
                hashes.update(_trace_blob_refs(row))
        for table_rows in context.values():
            for row in table_rows:
                hashes.update(_trace_blob_refs(row))
        blobs = {}
        for value in sorted(hashes):
            try: blobs[value] = self.s.blob_get(value)
            except Fault as exc:
                raise Fault("missing_evidence", "Traceability archive references a missing CAS blob", value) from exc
        payload = {"format": ARCHIVE_FORMAT, "history_version": HISTORY_VERSION, "schema": 14,
                   "project": project, "project_record": project_row, "tables": decoded, "context": context,
                   "blob_manifest": [{"sha256": h, "bytes": len(data)} for h, data in sorted(blobs.items())],
                   "runtime_restore_supported": False, "fresh_review_or_test_evidence": False,
                   "historical_only": True}
        return payload, blobs

    def export(self, actor, project, path=None):
        actor.require("owner", "agent", "worker", project=project)
        payload, blobs = self._archive_payload(actor, project)
        destination = Path(path) if path is not None else self.s.home / "exports" / f"traceability-{project}.zip"
        destination = destination.absolute();destination.parent.mkdir(parents=True, exist_ok=True)
        body = canonical(payload)
        need(len(body) <= MAX_ARCHIVE_PAYLOAD_BYTES, "archive_too_large", "Traceability archive metadata exceeds the explicit bound")
        fd, temporary = tempfile.mkstemp(prefix=".traceability-export-", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                    manifest = {"format": ARCHIVE_FORMAT, "project": project, "history_version": HISTORY_VERSION,
                                "payload": {"name": "traceability.json", "sha256": digest(body), "bytes": len(body)},
                                "blobs": payload["blob_manifest"]}
                    for name, data in [("manifest.json", canonical(manifest)), ("traceability.json", body)]:
                        info = zipfile.ZipInfo(name, (2026, 9, 14, 0, 0, 0));info.compress_type = zipfile.ZIP_STORED;archive.writestr(info, data)
                    for blob, data in sorted(blobs.items()):
                        info = zipfile.ZipInfo(f"blobs/{blob}", (2026, 9, 14, 0, 0, 0));info.compress_type = zipfile.ZIP_STORED;archive.writestr(info, data)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        with destination.open("rb") as archive_stream:
            archive_hash = hashlib.file_digest(archive_stream, "sha256").hexdigest()
        archive_blob = self.s.blob_put_file(destination)
        need(archive_hash == archive_blob, "integrity_error", "Traceability export changed during collection")
        with self.s.transaction():
            self.c.sec.event(project, "traceability_exported", actor.id,
                             {"project": project, "archive_blob": archive_blob, "archive_sha256": archive_hash,
                              "history_version": HISTORY_VERSION, "fresh_review_or_test_evidence": False})
        return {"project": project, "path": str(destination), "sha256": archive_hash, "blob": archive_blob,
                "bytes": destination.stat().st_size, "format": ARCHIVE_FORMAT, "history_version": HISTORY_VERSION,
                "runtime_restore_supported": False, "fresh_review_or_test_evidence": False}

    def inspect_archive(self, actor, path, expected_sha256=None):
        actor.require("owner", "agent", "observer")
        return inspect_archive(path, expected_sha256)

    def import_archive(self, actor, path, expected_sha256=None, project=None):
        actor.require("owner")
        payload, blobs, manifest = _read_archive(path, expected_sha256)
        incoming_project = payload["project"]
        need(project is None or project == incoming_project, "cross_project", "Traceability archive project cannot be silently remapped")
        target_project = project or incoming_project
        existing = self.s.one("SELECT id FROM projects WHERE id=?", (target_project,))
        def normalized(column, value):
            if column in {"body", "result"}:
                if isinstance(value, str):
                    try:
                        value = parse_json(value)
                    except Fault:
                        return value
                if isinstance(value, (dict, list)):
                    return canonical(value)
            return value

        def insert_or_verify(table, columns, row):
            ident = row.get("id")
            current = self.s.one(f"SELECT * FROM {table} WHERE id=?", (ident,))
            if current is not None:
                for column in columns:
                    need(normalized(column, current.get(column)) == normalized(column, row.get(column)),
                         "archive_conflict", "Archive row conflicts with an existing immutable row", {"table": table, "id": ident, "column": column})
                return
            values = [canonical(row[k]).decode() if isinstance(row.get(k), (dict, list)) else row.get(k) for k in columns]
            self.s.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)

        try:
            with self.s.transaction():
                if existing is None:
                    record = payload.get("project_record") or {"id": target_project, "name": target_project, "config": "{}", "paused": 0, "created": timestamp()}
                    need(record.get("id", target_project) == target_project, "invalid_archive", "Archive project record differs")
                    self.s.execute("INSERT INTO projects(id,name,config,paused,created) VALUES(?,?,?,?,?)", (target_project, record.get("name", target_project), record.get("config", "{}") if isinstance(record.get("config"), str) else canonical(record.get("config", {})).decode(), int(record.get("paused", 0)), float(record.get("created", timestamp()))))
                for blob, data in blobs.items():
                    need(self.s.blob_put(data) == blob, "integrity_error", "Archive blob hash differs")
                tables = payload["tables"]
                # Preserve historical IDs and make a repeated import a true
                # idempotent no-op.  A same-ID or unique-key conflict with a
                # different body is rejected; INSERT OR IGNORE would silently
                # lose history and make a corrupt restore appear successful.
                pending_set_pointers = []
                for row in tables.get("traceability_sets", []):
                    row = dict(row);row["project"] = target_project
                    if row.get("active_revision") and self.s.one("SELECT id FROM traceability_revisions WHERE id=?", (row["active_revision"],)) is None:
                        pending_set_pointers.append((row["id"], row["active_revision"], row.get("active_digest")))
                        # The set/revision foreign key cycle is represented by
                        # a deferred pointer update after both rows exist.
                        row["active_revision"] = None
                        row["active_digest"] = None
                    insert_or_verify("traceability_sets", ("id", "project", "name", "kind", "active_revision", "active_digest", "created"), row)
                for table, columns in (("traceability_revisions", ("id","set_id","project","revision","status","body","digest","population_digest","adapter","created")),
                                       ("traceability_proposals", ("id","set_id","project","kind","status","body","digest","expected_active","semantic_material_digest","result","created"))):
                    for row in tables.get(table, []):
                        row = dict(row);row["project"] = target_project
                        insert_or_verify(table, columns, row)
                for table, columns in (("traceability_items", ("id","revision","project","ordinal","item_kind","path","status","start_byte","end_byte","body","digest","leaf")),
                                       ("traceability_decisions", ("id","revision","project","body","digest","status","created")),
                                       ("traceability_mappings", ("id","revision","project","body","digest","status","created")),
                                       ("traceability_bindings", ("id","project","revision","body","digest","status","created")),
                                       ("traceability_records", ("id","project","revision","proposal","kind","body","digest","created"))):
                    for row in tables.get(table, []):
                        row = dict(row);row["project"] = target_project
                        if table == "traceability_items" and "leaf" not in row:
                            body = row.get("body", {})
                            body = parse_json(body) if isinstance(body, str) else body
                            row["leaf"] = int(bool(isinstance(body, dict) and body.get("leaf")))
                        insert_or_verify(table, columns, row)
                for set_id, active_revision, active_digest in pending_set_pointers:
                    current = self.s.one("SELECT active_revision,active_digest FROM traceability_sets WHERE id=?", (set_id,), True)
                    if current["active_revision"] is None:
                        self.s.execute("UPDATE traceability_sets SET active_revision=?,active_digest=? WHERE id=?", (active_revision, active_digest, set_id))
                    else:
                        need(current["active_revision"] == active_revision and current["active_digest"] == active_digest,
                             "archive_conflict", "Archive active revision conflicts with the current set", set_id)
                self.c.sec.event(target_project, "traceability_archive_imported", actor.id, {"archive_project": incoming_project, "history_version": HISTORY_VERSION, "fresh_review_or_test_evidence": False})
        except sqlite3.IntegrityError as exc:
            raise Fault("archive_conflict", "Traceability archive conflicts with an existing immutable or unique row") from exc
        return {"project": target_project, "imported": True, "format": payload["format"], "history_version": payload.get("history_version"),
                "runtime_restore_supported": False, "fresh_review_or_test_evidence": False, "historical_only": True,
                "revisions": len(tables.get("traceability_revisions", [])), "blobs": len(blobs)}


def _sequence_digest(values: Iterable[str]) -> str:
    h = hashlib.sha256()
    for value in values:
        h.update(value.encode("ascii"));h.update(b"\n")
    return h.hexdigest()


def _bounded_metadata(value: Any) -> tuple[Any, list[str]]:
    """Return bounded revision metadata without changing its stored digest.

    Revision bodies retain the complete inventory in SQLite.  A public get
    response may contain a large inventory, so it exposes a deterministic
    prefix and a detail marker instead of silently crossing the 1 MiB page
    boundary.  Item range reads remain the path for complete source bytes.
    """
    omitted = object()
    truncated: list[str] = []

    def trim(current: Any, path: str, depth: int = 0):
        if depth > 8:
            truncated.append(path or "$")
            return omitted
        if isinstance(current, str):
            if len(current) > 4096:
                truncated.append(path or "$")
                return omitted
            return current
        if isinstance(current, list):
            values = current
            if len(values) > 100:
                truncated.append(path or "$")
                values = values[:100]
            output = []
            for index, child in enumerate(values):
                result = trim(child, f"{path}[{index}]" if path else f"[{index}]", depth + 1)
                if result is not omitted:
                    output.append(result)
            return output
        if isinstance(current, dict):
            output = {}
            keys = sorted(current)
            if len(keys) > 200:
                truncated.append(path or "$")
                keys = keys[:200]
            for key in keys:
                result = trim(current[key], f"{path}.{key}" if path else str(key), depth + 1)
                if result is not omitted:
                    output[key] = result
            return output
        return current

    result = trim(value, "")
    return ({} if result is omitted else result), sorted(set(truncated))


def _bounded_result(result: dict[str, Any]) -> None:
    try: size = len(canonical(result))
    except (TypeError, ValueError) as exc: raise Fault("invalid_result", "Traceability result cannot be encoded") from exc
    need(size <= MAX_PAGE_BYTES, "result_too_large", "Traceability page exceeds the 1 MiB encoded bound")


def _read_archive(path, expected_sha256=None):
    source = Path(path)
    need(source.is_file() and not source.is_symlink(), "invalid_archive", "Traceability archive is absent")
    with source.open("rb") as archive_stream:
        observed = hashlib.file_digest(archive_stream, "sha256").hexdigest()
    need(expected_sha256 is None or observed == expected_sha256, "archive_mismatch", "Traceability archive digest differs")
    try:
        with zipfile.ZipFile(source) as archive:
            names = sorted(archive.namelist())
            need("manifest.json" in names and "traceability.json" in names and len(names) == len(set(names)), "invalid_archive", "Traceability archive members are incomplete or duplicated")
            manifest = parse_json(archive.read("manifest.json"), limit=MAX_PAGE_BYTES)
            need(manifest.get("format") == ARCHIVE_FORMAT and manifest.get("history_version") == HISTORY_VERSION, "invalid_archive", "Unsupported traceability archive")
            payload_raw = archive.read("traceability.json")
            need(len(payload_raw) == manifest["payload"]["bytes"] and digest(payload_raw) == manifest["payload"]["sha256"], "invalid_archive", "Traceability payload digest differs")
            payload = parse_json(payload_raw, limit=MAX_ARCHIVE_PAYLOAD_BYTES)
            need(payload.get("format") == ARCHIVE_FORMAT and payload.get("schema") == 14 and payload.get("history_version") == HISTORY_VERSION, "invalid_archive", "Traceability payload format differs")
            blobs = {}
            manifest_blobs = manifest.get("blobs", [])
            payload_blobs = payload.get("blob_manifest", [])
            need(isinstance(manifest_blobs, list) and isinstance(payload_blobs, list), "invalid_archive", "Traceability blob manifest is malformed")
            need(len({item.get("sha256") for item in manifest_blobs if isinstance(item, dict)}) == len(manifest_blobs), "invalid_archive", "Traceability blob manifest contains duplicates")
            need(len({item.get("sha256") for item in payload_blobs if isinstance(item, dict)}) == len(payload_blobs), "invalid_archive", "Traceability payload blob manifest contains duplicates")
            expected = {item["sha256"]: int(item["bytes"]) for item in manifest_blobs}
            need(expected == {item["sha256"]: int(item["bytes"]) for item in payload_blobs}, "invalid_archive", "Traceability blob manifest differs")
            referenced = set()
            for rows in tuple(payload.get("tables", {}).values()) + tuple(payload.get("context", {}).values()):
                for row in rows:
                    referenced.update(_trace_blob_refs(row))
            need(referenced <= set(expected), "invalid_archive", "Traceability history references a blob absent from the archive", sorted(referenced - set(expected)))
            for blob, size in expected.items():
                need(_HEX64.fullmatch(blob), "invalid_archive", "Traceability blob name is malformed")
                raw = archive.read(f"blobs/{blob}")
                need(len(raw) == size and digest(raw) == blob, "invalid_archive", "Traceability blob checksum differs", blob)
                blobs[blob] = raw
            expected_names = {"manifest.json", "traceability.json"} | {f"blobs/{blob}" for blob in expected}
            need(set(names) == expected_names, "invalid_archive", "Traceability archive contains unexpected members")
            # The row validator is deliberately run only after the portable
            # CAS has been materialised.  Complete populations are defined by
            # their pinned bytes and Git object closure, so checking only the
            # JSON rows would let a recomputed outer manifest hide a missing
            # leaf or a changed interval.
            _validate_archive_rows(payload, blobs, payload.get("context"))
    except (KeyError, ValueError, TypeError, zipfile.BadZipFile, binascii.Error) as exc:
        raise Fault("invalid_archive", "Malformed traceability archive") from exc
    return payload, blobs, manifest


def _archive_body(row: dict[str, Any], key: str = "body") -> dict[str, Any]:
    """Decode and bound one traceability JSON column for archive checks."""
    value = row.get(key)
    if isinstance(value, str):
        value = parse_json(value, limit=MAX_REVISION_BODY_BYTES)
    need(isinstance(value, dict), "invalid_archive", "Traceability JSON column is not an object", row.get("id"))
    return value


def _archive_result(row: dict[str, Any]) -> Any:
    value = row.get("result")
    if isinstance(value, str):
        value = parse_json(value, limit=MAX_REVISION_BODY_BYTES)
    return value


def _archive_blob(blob_reader: Any, reference: Any, *, required: bool = True) -> bytes | None:
    """Read one archive CAS object through either a mapping or a callback."""
    if not isinstance(reference, str) or not _HEX64.fullmatch(reference):
        if required:
            need(False, "invalid_archive", "Traceability CAS reference is malformed", reference)
        return None
    if blob_reader is None:
        if required:
            need(False, "invalid_archive", "Traceability archive has no CAS reader", reference)
        return None
    try:
        raw = blob_reader(reference) if callable(blob_reader) else blob_reader.get(reference)
    except (Fault, KeyError, OSError, TypeError, ValueError) as exc:
        raise Fault("invalid_archive", "Traceability CAS object cannot be read", reference) from exc
    if raw is None:
        if required:
            need(False, "invalid_archive", "Traceability CAS object is missing", reference)
        return None
    need(isinstance(raw, bytes) and digest(raw) == reference,
         "invalid_archive", "Traceability CAS object digest differs", reference)
    return raw


class _ArchivePinnedContext(PortableObservedContext):
    """Compatibility name for the shared finite archive context reader."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]], blob_reader: Any,
                 *, project: str | None = None):
        def normalized_row(section, row):
            value = candidate_context_row(section, row)
            # Runtime SQL rows lack the derived checksums added by the
            # portable projection. Preserve supplied projection values so a
            # changed body cannot be normalized into a valid checksum.
            for key in ("body_digest", "result_digest"):
                if key in row:
                    value[key] = row[key]
            return value

        fixed = {
            section: [normalized_row(section, row)
                      for row in rows.get(section, [])]
            for section in ("tasks", "candidates", "runs", "receipts", "repos")
        }
        # Older archive callers pass the raw candidates query, whose table has
        # no project column.  The shared wire projection carries that column
        # from the owning Task; preserve the compatibility adapter by deriving
        # the same immutable value before the common reader validates rows.
        task_projects = {row.get("id"): row.get("project")
                         for row in fixed["tasks"] if isinstance(row, dict)}
        fixed["candidates"] = [
            ({**row, "project": row.get("project", task_projects.get(row.get("task")))})
            if isinstance(row, dict) else row
            for row in fixed["candidates"]
        ]
        extras = {section: list(values) for section, values in rows.items()
                  if section not in fixed}
        if project is None:
            project = next((row.get("project") for values in [*fixed.values(), *extras.values()]
                            for row in values if isinstance(row, dict)
                            and isinstance(row.get("project"), str)), None)
        super().__init__(fixed, blob_reader, project=project,
                         extra_rows=extras, code="invalid_archive")


def _archive_candidate_failure(kind: str, message: str, details: Any = None) -> None:
    # Historical inspection has no live authority.  Every shared semantic
    # failure is an invalid archive, including a missing context row.
    raise Fault("invalid_archive", message, details)


def _archive_span(span: Any, raw: bytes, start: int, end: int, blob: str | None = None,
                  source_id: str | None = None) -> None:
    """Check byte/hash/Unicode identity of a stored source span."""
    if span is None:
        return
    need(isinstance(span, dict), "invalid_archive", "Traceability source span is malformed")
    need(span.get("ref_type") == "source_span", "invalid_archive", "Traceability source span type differs")
    need(type(span.get("byte_start")) is int and type(span.get("byte_end")) is int,
         "invalid_archive", "Traceability source span byte bounds are malformed")
    need(span["byte_start"] == start and span["byte_end"] == end,
         "invalid_archive", "Traceability source span differs from item columns")
    need(0 <= start <= end <= len(raw), "invalid_archive", "Traceability source span is outside its source")
    need(span.get("span_hash") == digest(raw[start:end]),
         "invalid_archive", "Traceability source span hash differs")
    if blob is not None:
        need(span.get("blob_digest") == blob, "invalid_archive", "Traceability source span blob differs")
    if source_id is not None:
        need(span.get("source_id") == source_id, "invalid_archive", "Traceability source span source differs")
    try:
        expected_start = _unicode_position(raw, start, raw.startswith(b"\xef\xbb\xbf"))
        expected_end = _unicode_position(raw, end, raw.startswith(b"\xef\xbb\xbf"))
    except Fault:
        need(span.get("unicode_start") is None and span.get("unicode_end") is None,
             "invalid_archive", "Invalid UTF-8 span has guessed Unicode coordinates")
    else:
        need(span.get("unicode_start") == expected_start and span.get("unicode_end") == expected_end,
             "invalid_archive", "Traceability source span Unicode coordinates differ")


def _archive_item_range(row: dict[str, Any], body: dict[str, Any]) -> tuple[int, int]:
    start, end = row.get("start_byte"), row.get("end_byte")
    need(type(start) is int and type(end) is int and 0 <= start <= end,
         "invalid_archive", "Traceability item byte columns are malformed", row.get("id"))
    if "byte_start" in body or "byte_end" in body:
        need(type(body.get("byte_start")) is int and type(body.get("byte_end")) is int,
             "invalid_archive", "Traceability item body byte bounds are malformed", row.get("id"))
        need((body["byte_start"], body["byte_end"]) == (start, end),
             "invalid_archive", "Traceability item body and columns disagree", row.get("id"))
    span = body.get("source_span")
    if isinstance(span, dict):
        need(span.get("byte_start") == start and span.get("byte_end") == end,
             "invalid_archive", "Traceability item span and columns disagree", row.get("id"))
    if "bytes" in body:
        need(type(body["bytes"]) is int and body["bytes"] == end - start,
             "invalid_archive", "Traceability item byte count differs", row.get("id"))
    return start, end


def _archive_item_kind(body: dict[str, Any], kind: str) -> None:
    expected = {
        "file": {"file", "document", "git_entry"},
        "atom": {"byte_atom"},
        "line": {"physical_line"},
        "symbol": {"symbol_group", "group"},
        "group": {"symbol_group", "group"},
    }
    if kind in expected and "type" in body:
        need(body.get("type") in expected[kind], "invalid_archive", "Traceability item body type differs")


def _archive_validate_span_fields(row: dict[str, Any], body: dict[str, Any], raw: bytes | None,
                                  blob: str | None = None, source_id: str | None = None) -> tuple[int, int]:
    start, end = _archive_item_range(row, body)
    if raw is not None:
        need(end <= len(raw), "invalid_archive", "Traceability item range exceeds its source", row.get("id"))
        if body.get("text_digest") is not None:
            need(body.get("text_digest") == digest(raw[start:end]),
                 "invalid_archive", "Traceability item text digest differs", row.get("id"))
        _archive_span(body.get("source_span"), raw, start, end, blob, source_id)
    return start, end


def _archive_validate_git_item_body(row: dict[str, Any], body: dict[str, Any], scope: dict[str, Any],
                                    project: str, path: str, oid: str, source_blob: str) -> None:
    """Check typed Git references carried by file/atom/symbol bodies."""
    if row.get("item_kind") == "file" and body.get("type") == "git_entry" and body.get("git_kind") == "symlink":
        expected_ref = "git_file"
    elif row.get("item_kind") == "file":
        expected_ref = "git_file"
    elif row.get("item_kind") == "atom":
        expected_ref = "git_atom"
    elif row.get("item_kind") in {"symbol", "group"}:
        expected_ref = "git_symbol"
    else:
        return
    need(body.get("project") == project and body.get("repository") == scope.get("repository")
         and body.get("commit") == scope.get("commit") and body.get("object_format") == scope.get("object_format")
         and body.get("blob_oid") == oid and body.get("sha256") == source_blob,
         "invalid_archive", "Git item body reference differs", row.get("id"))
    need(body.get("ref_type") == expected_ref, "invalid_archive", "Git item reference type differs", row.get("id"))
    typed = body.get("typed_ref")
    if typed is None:
        need(row.get("item_kind") == "file" and body.get("type") == "git_entry" and body.get("git_kind") == "symlink",
             "invalid_archive", "Git item typed reference is missing", row.get("id"))
        return
    need(isinstance(typed, dict) and typed.get("type") == expected_ref
         and typed.get("project") == project and typed.get("repository") == scope.get("repository")
         and typed.get("commit") == scope.get("commit") and typed.get("object_format") == scope.get("object_format")
         and typed.get("path") == path and typed.get("blob_oid") == oid and typed.get("sha256") == source_blob,
         "invalid_archive", "Git typed item reference differs", row.get("id"))
    if row.get("item_kind") in {"atom", "symbol", "group"}:
        need(typed.get("byte_start") == row.get("start_byte") and typed.get("byte_end") == row.get("end_byte"),
             "invalid_archive", "Git typed item range differs", row.get("id"))
        span = body.get("source_span") or {}
        need(typed.get("span_hash") == span.get("span_hash"),
             "invalid_archive", "Git typed item span hash differs", row.get("id"))
    if row.get("item_kind") in {"symbol", "group"}:
        need(typed.get("qualified_name") == body.get("qualified_name")
             and typed.get("kind") == body.get("kind") and typed.get("ordinal") == body.get("ordinal")
             and typed.get("signature_hash") == body.get("signature_hash"),
             "invalid_archive", "Git symbol typed reference differs", row.get("id"))


def _archive_partition(rows: list[dict[str, Any]], raw: bytes, *, empty_ok: bool = True) -> None:
    """Require a complete, sorted, non-overlapping leaf byte partition."""
    leaves = [row for row in rows if int(row.get("leaf", 0)) == 1]
    need(leaves or (empty_ok and len(raw) == 0), "invalid_archive", "Complete source has no leaf partition")
    if not raw:
        need(len(leaves) == 1 and leaves[0]["start_byte"] == 0 and leaves[0]["end_byte"] == 0,
             "invalid_archive", "Empty source does not have one explicit zero-byte item")
        return
    ordered = sorted(leaves, key=lambda row: (row["start_byte"], row["end_byte"], row["id"]))
    cursor = 0
    for row in ordered:
        need(row["start_byte"] == cursor and row["end_byte"] >= row["start_byte"],
             "invalid_archive", "Complete source leaf partition has a gap or overlap", row.get("id"))
        cursor = row["end_byte"]
    need(cursor == len(raw), "invalid_archive", "Complete source leaf partition does not cover the source")


def _archive_compare_item_identity(actual: list[dict[str, Any]], expected: list[dict[str, Any]], path: str | None) -> None:
    """Compare deterministic population identity without trusting body text.

    Item IDs are content-derived population identities in Unit A.  Replaying
    the stdlib adapter here does not create new IDs or current-time values; it
    proves that an archived row still names the same path/kind/range/status
    and leaf boundary that its pinned bytes produce.
    """
    need(len(actual) == len(expected), "invalid_archive", "Population item count differs", path)
    for observed, calculated in zip(actual, expected):
        signature = lambda row: (row.get("id"), row.get("item_kind"), row.get("start_byte"),
                                 row.get("end_byte"), row.get("status"),
                                 int(bool(row.get("leaf", row.get("body", {}).get("leaf", 0)))))
        need(signature(observed) == signature(calculated),
             "invalid_archive", "Population item identity differs from pinned bytes", path)
        observed_body = _archive_body(observed)
        expected_body = calculated.get("body", {})
        normative = {
            "file": ("type", "path", "leaf", "bytes", "blob_oid", "blob_digest", "mode", "language",
                     "source_span", "empty", "unknown_reason", "git_kind", "git_oid", "target", "ref_type"),
            "atom": ("type", "path", "leaf", "byte_start", "byte_end", "text_digest", "owner_symbol",
                     "unknown_reason", "source_span"),
            "line": ("type", "leaf", "line", "byte_start", "byte_end", "unicode_start", "unicode_end",
                     "text_digest", "source_span", "unknown_reason"),
            "symbol": ("type", "path", "leaf", "symbol_id", "qualified_name", "name", "kind", "ordinal",
                       "parent", "byte_start", "byte_end", "signature_hash", "ast_hash", "atom_ids",
                       "source_span"),
            "group": ("type", "path", "leaf", "symbol_id", "qualified_name", "name", "kind", "ordinal",
                      "parent", "byte_start", "byte_end", "signature_hash", "ast_hash", "atom_ids",
                      "source_span"),
        }.get(calculated.get("item_kind"), ())
        for key in normative:
            if key in expected_body:
                need(key in observed_body and observed_body.get(key) == expected_body.get(key),
                     "invalid_archive", "Population syntax projection differs from pinned bytes", path)


def _archive_git_objects(pins: list[str], object_format: str, blob_reader: Any) -> tuple[dict[str, tuple[str, bytes, str]], dict[str, bytes]]:
    """Index pinned raw Git objects by OID and retain their CAS bytes."""
    objects: dict[str, tuple[str, bytes, str]] = {}
    cas: dict[str, bytes] = {}
    for reference in pins:
        raw = _archive_blob(blob_reader, reference)
        assert raw is not None
        cas[reference] = raw
        try:
            kind, payload = _git_object_payload(raw)
        except Fault:
            continue
        oid = _sha256_oid(raw, object_format)
        objects[oid] = (kind, payload, reference)
    return objects, cas


def _archive_validate_git_population(revision: dict[str, Any], items: list[dict[str, Any]],
                                     blob_reader: Any) -> None:
    body = _archive_body(revision)
    scope = body.get("scope")
    need(isinstance(scope, dict) and scope.get("kind") == "code", "invalid_archive", "Code revision scope is malformed")
    object_format = scope.get("object_format")
    need(object_format in {"sha1", "sha256"}, "invalid_archive", "Git object format is invalid")
    pins = body.get("pins")
    need(isinstance(pins, list) and len(set(pins)) == len(pins), "invalid_archive", "Git pin list is malformed")
    for reference in pins:
        need(isinstance(reference, str) and _HEX64.fullmatch(reference), "invalid_archive", "Git pin reference is malformed")
    git_pin = body.get("git_pin")
    need(isinstance(git_pin, dict), "invalid_archive", "Git revision pin is missing")
    commit, tree = git_pin.get("commit"), git_pin.get("tree")
    need(commit == scope.get("commit") and isinstance(commit, str) and _OID.fullmatch(commit),
         "invalid_archive", "Git commit pin differs from the scope")
    need(isinstance(tree, str) and _OID.fullmatch(tree), "invalid_archive", "Git tree pin is malformed")
    commit_blob, tree_blob = git_pin.get("commit_blob"), git_pin.get("tree_blob")
    need(commit_blob in pins and tree_blob in pins,
         "invalid_archive", "Git root objects are outside the revision pin boundary")
    commit_raw = _archive_blob(blob_reader, commit_blob)
    tree_raw = _archive_blob(blob_reader, tree_blob)
    assert commit_raw is not None and tree_raw is not None
    need(_sha256_oid(commit_raw, object_format) == commit, "invalid_archive", "Pinned Git commit object differs")
    need(_sha256_oid(tree_raw, object_format) == tree, "invalid_archive", "Pinned Git root tree object differs")
    _kind, commit_payload = _git_object_payload(commit_raw, "commit")
    _git_object_payload(tree_raw, "tree")
    tree_match = re.search(rb"(?m)^tree ([0-9a-f]{40,64})$", commit_payload)
    need(tree_match is not None and tree_match.group(1).decode("ascii") == tree,
         "invalid_archive", "Git commit and root tree disagree")
    objects, _cas = _archive_git_objects(pins, object_format, blob_reader)
    need(tree in objects and objects[tree][0] == "tree", "invalid_archive", "Pinned root tree is not in the CAS closure")
    try:
        roots = _safe_roots(scope.get("roots"))
        include = _patterns(scope.get("include"))
    except Fault as exc:
        raise Fault("invalid_archive", "Git selection scope is malformed") from exc
    reachable: list[dict[str, Any]] = []
    active_trees: set[str] = set()
    traversed_trees: set[str] = set()

    def walk(oid: str, prefix: str) -> None:
        need(oid not in active_trees, "invalid_archive", "Git tree closure contains a cycle", oid)
        active_trees.add(oid)
        traversed_trees.add(oid)
        entry = objects.get(oid)
        need(entry is not None and entry[0] == "tree", "invalid_archive", "Git tree closure is incomplete", oid)
        raw = _archive_blob(blob_reader, entry[2])
        assert raw is not None
        for mode, kind, child_oid, name in _tree_entries(raw, object_format):
            path = f"{prefix}/{name}" if prefix else name
            if kind == "tree":
                walk(child_oid, path)
            elif _matches(path, roots, include):
                reachable.append({"path": path, "type": kind, "mode": mode, "blob_oid": child_oid})
        active_trees.remove(oid)

    if not scope.get("empty_scope"):
        walk(tree, "")
    reachable.sort(key=lambda value: value["path"])
    inventory = body.get("inventory")
    need(isinstance(inventory, list), "invalid_archive", "Git revision inventory is missing")
    expected_inventory = []
    for value in inventory:
        need(isinstance(value, dict) and isinstance(value.get("path"), str), "invalid_archive", "Git inventory entry is malformed")
        need(value.get("type") in {"blob", "commit"}, "invalid_archive", "Git inventory entry type is invalid", value.get("path"))
        expected_inventory.append({"path": value["path"], "type": value["type"], "mode": value.get("mode"), "blob_oid": value.get("blob_oid")})
    need(len({value["path"] for value in expected_inventory}) == len(expected_inventory), "invalid_archive", "Git inventory contains duplicate paths")
    expected_inventory.sort(key=lambda value: value["path"])
    need(expected_inventory == reachable, "invalid_archive", "Git selected inventory differs from the pinned tree")
    # The extractor records the number of distinct tree objects pinned for
    # this commit.  Recompute it from the tree closure instead of trusting a
    # mutable summary counter.
    if isinstance(body.get("counts"), dict) and "trees" in body["counts"]:
        need(body["counts"]["trees"] == len(traversed_trees) or
             (body.get("scope", {}).get("empty_scope") and body["counts"]["trees"] == 1),
             "invalid_archive", "Complete Git tree count differs")
    item_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in items:
        if isinstance(row.get("path"), str):
            item_by_path.setdefault(row["path"], []).append(row)
    need(set(item_by_path) == {value["path"] for value in expected_inventory},
         "invalid_archive", "Git inventory and item paths differ")
    for entry in inventory:
        path, kind, mode, oid = entry["path"], entry["type"], entry.get("mode"), entry.get("blob_oid")
        path_rows = item_by_path[path]
        file_rows = [row for row in path_rows if row.get("item_kind") == "file"]
        need(len(file_rows) == 1, "invalid_archive", "Git inventory entry does not have one file item", path)
        file_row, file_body = file_rows[0], _archive_body(file_rows[0])
        need(file_body.get("path") == path and file_body.get("blob_oid") == oid,
             "invalid_archive", "Git file item identity differs from inventory", path)
        need(file_body.get("mode") == mode, "invalid_archive", "Git file mode differs from inventory", path)
        if kind == "commit":
            need(file_row["status"] == "unknown" and file_row["leaf"] == 1 and file_row["start_byte"] == file_row["end_byte"] == 0,
                 "invalid_archive", "Git submodule item is not an explicit unknown leaf", path)
            expected_item = _item("ITEM-" + digest([revision["set_id"], revision["revision"], path, kind, oid])[:40],
                                  0, "file", path, "unknown", 0, 0,
                                  {"type": "git_entry", "leaf": True, "path": path, "git_kind": kind,
                                   "git_oid": oid, "mode": mode, "unknown_reason": "submodule"})
            _archive_compare_item_identity(path_rows, [expected_item], path)
            continue
        source_blob = entry.get("sha256")
        object_blob = entry.get("git_object_blob")
        need(source_blob in pins and object_blob in pins,
             "invalid_archive", "Git source objects are outside the revision pin boundary", path)
        raw = _archive_blob(blob_reader, source_blob)
        object_raw = _archive_blob(blob_reader, object_blob)
        assert raw is not None and object_raw is not None
        need(entry.get("bytes") == len(raw) and entry.get("sha256") == digest(raw),
             "invalid_archive", "Git source blob identity differs", path)
        kind_check, payload = _git_object_payload(object_raw, "blob")
        need(kind_check == "blob" and payload == raw and _sha256_oid(object_raw, object_format) == oid,
             "invalid_archive", "Git source object identity differs", path)
        need(file_body.get("blob_digest") == source_blob and file_body.get("sha256") == source_blob,
             "invalid_archive", "Git file source digest differs", path)
        for row in path_rows:
            body_row = _archive_body(row)
            _archive_validate_git_item_body(row, body_row, scope, revision.get("project"), path, oid, source_blob)
            _archive_validate_span_fields(row, body_row, raw, source_blob)
        _archive_partition(path_rows, raw)
        if stat.S_ISLNK(mode):
            expected_items = [_item("ITEM-" + digest([revision["set_id"], revision["revision"], path, "symlink", oid])[:40],
                                    0, "file", path, "unknown", 0, len(raw),
                                    {"type": "git_entry", "leaf": True, "path": path, "git_kind": "symlink",
                                     "git_oid": oid, "mode": mode, "unknown_reason": "symlink",
                                     "blob_oid": oid, "blob_digest": source_blob, "target": raw.decode("utf-8", errors="replace"),
                                     "ref_type": "git_file"})]
        else:
            expected_items, _expected_stats = _code_file_items(
                revision["set_id"], revision["revision"], path, raw, source_blob, oid, mode)
        _archive_compare_item_identity(path_rows, expected_items, path)
        # A valid regular code file has definitions/groups and atoms; an
        # unsupported/parse-error file retains one whole-file unknown leaf.
        groups = [row for row in path_rows if row.get("item_kind") in {"symbol", "group"}]
        atoms = [row for row in path_rows if row.get("item_kind") == "atom"]
        if file_row.get("leaf") == 0:
            need(atoms or any(row.get("status") == "unknown" for row in path_rows),
                 "invalid_archive", "Non-empty code file has no atom or unknown partition", path)
        _archive_validate_groups(path_rows, groups, atoms, raw)


def _archive_validate_document_population(revision: dict[str, Any], items: list[dict[str, Any]],
                                           blob_reader: Any) -> None:
    body = _archive_body(revision)
    scope = body.get("scope")
    need(isinstance(scope, dict) and scope.get("kind") == "document", "invalid_archive", "Document revision scope is malformed")
    source = scope.get("source")
    need(isinstance(source, dict), "invalid_archive", "Document source descriptor is missing")
    source_blob = source.get("blob")
    raw = _archive_blob(blob_reader, source_blob)
    assert raw is not None
    need(source_blob in set(revision.get("body", {}).get("pins", [])),
         "invalid_archive", "Document source blob is outside the revision pin boundary")
    need(source.get("bytes") == len(raw), "invalid_archive", "Document scope source size differs")
    inventory = body.get("inventory")
    need(isinstance(inventory, list) and len(inventory) == 1 and isinstance(inventory[0], dict),
         "invalid_archive", "Document inventory is incomplete")
    entry = inventory[0]
    need(entry.get("type") == "source" and entry.get("sha256") == source_blob and entry.get("bytes") == len(raw),
         "invalid_archive", "Document source inventory differs")
    need(entry.get("sha256") == digest(raw), "invalid_archive", "Document source blob digest differs")
    file_rows = [row for row in items if row.get("item_kind") == "file"]
    need(len(file_rows) == 1 and file_rows[0].get("path") is None, "invalid_archive", "Document has no canonical file item")
    for row in items:
        body_row = _archive_body(row)
        _archive_validate_span_fields(row, body_row, raw, source_blob, source.get("source_id"))
    _archive_partition(items, raw)
    expected_items, _expected_stats = _document_line_items(
        _archive_body(revision).get("set_id"), revision["revision"], raw, source_blob,
        source.get("source_id"))
    _archive_compare_item_identity(items, expected_items, None)
    lines = [row for row in items if row.get("item_kind") == "line"]
    if raw:
        need(lines, "invalid_archive", "Non-empty document has no physical lines")
    else:
        need(file_rows[0].get("leaf") == 1, "invalid_archive", "Empty document is not an explicit zero-byte item")


def _archive_validate_groups(path_rows: list[dict[str, Any]], groups: list[dict[str, Any]],
                             atoms: list[dict[str, Any]], raw: bytes) -> None:
    """Validate symbol atom references and the parent DAG without counting
    child bytes twice.  Parent names are resolved by containing source span so
    same-name overloads remain distinct immutable groups.
    """
    atom_by_id = {row["id"]: row for row in atoms}
    group_by_id = {row["id"]: row for row in groups}
    need(len(group_by_id) == len(groups), "invalid_archive", "Duplicate symbol/group identity")
    names: dict[str, list[dict[str, Any]]] = {}
    parent_of: dict[str, str] = {}
    for row in groups:
        body = _archive_body(row)
        need(body.get("symbol_id") == row["id"], "invalid_archive", "Symbol body identity differs", row.get("id"))
        start, end = _archive_validate_span_fields(row, body, raw)
        atom_ids = body.get("atom_ids")
        need(isinstance(atom_ids, list) and len(set(atom_ids)) == len(atom_ids),
             "invalid_archive", "Symbol atom references are malformed", row.get("id"))
        for atom_id in atom_ids:
            atom = atom_by_id.get(atom_id)
            need(atom is not None and atom.get("path") == row.get("path"),
                 "invalid_archive", "Symbol references a foreign or missing atom", row.get("id"))
            need(start <= atom["start_byte"] <= atom["end_byte"] <= end,
                 "invalid_archive", "Symbol atom lies outside its group", row.get("id"))
        name = body.get("qualified_name")
        need(isinstance(name, str) and name, "invalid_archive", "Symbol qualified name is missing", row.get("id"))
        names.setdefault(name, []).append(row)
        parent = body.get("parent")
        if parent is not None:
            need(isinstance(parent, str), "invalid_archive", "Symbol parent reference is malformed", row.get("id"))
            parent_of[row["id"]] = parent
    for row in groups:
        parent_name = parent_of.get(row["id"])
        if parent_name is None:
            continue
        start, end = row["start_byte"], row["end_byte"]
        candidates = [candidate for candidate in names.get(parent_name, [])
                      if candidate.get("path") == row.get("path")
                      and candidate["id"] != row["id"]
                      and candidate["start_byte"] <= start and end <= candidate["end_byte"]]
        need(candidates, "invalid_archive", "Symbol parent reference is dangling", row.get("id"))
        parent_row = min(candidates, key=lambda candidate: (candidate["end_byte"] - candidate["start_byte"], candidate["id"]))
        parent_of[row["id"]] = parent_row["id"]
        parent_body = _archive_body(parent_row)
        need(set(_archive_body(row).get("atom_ids", [])) <= set(parent_body.get("atom_ids", [])),
             "invalid_archive", "Parent symbol does not contain child atoms", row.get("id"))
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ident: str) -> None:
        if ident in visiting:
            need(False, "invalid_archive", "Symbol parent graph contains a cycle", ident)
        if ident in visited:
            return
        visiting.add(ident)
        parent = parent_of.get(ident)
        if parent is not None:
            need(parent in group_by_id, "invalid_archive", "Symbol parent ID is foreign", ident)
            visit(parent)
        visiting.remove(ident);visited.add(ident)
    for ident in group_by_id:
        visit(ident)
    for atom in atoms:
        owner = _archive_body(atom).get("owner_symbol")
        if owner is not None:
            need(owner in group_by_id and group_by_id[owner].get("path") == atom.get("path"),
                 "invalid_archive", "Atom owner symbol is foreign", atom.get("id"))


def _archive_validate_partial_records(records: list[dict[str, Any]], proposals: dict[str, dict[str, Any]],
                                      revisions: dict[str, dict[str, Any]], blob_reader: Any) -> None:
    """Validate resumable checkpoints without pretending they are complete."""
    for record in records:
        kind = record.get("kind")
        if kind not in {"extraction_checkpoint", "extraction_failed", "extracted", "proposal"}:
            continue
        body = _archive_body(record)
        proposal_id = record.get("proposal") or body.get("proposal")
        if proposal_id is not None:
            need(proposal_id in proposals, "invalid_archive", "Staging record references a missing proposal", record.get("id"))
        if kind != "extraction_checkpoint":
            continue
        need(isinstance(proposal_id, str), "invalid_archive", "Extraction checkpoint has no proposal")
        need(body.get("format") == TRACEABILITY_FORMAT and body.get("proposal") == proposal_id,
             "invalid_archive", "Extraction checkpoint identity differs", record.get("id"))
        need(isinstance(body.get("revision_id"), str) and type(body.get("revision_number")) is int and body["revision_number"] > 0,
             "invalid_archive", "Extraction checkpoint revision marker is malformed", record.get("id"))
        proposal = proposals[proposal_id]
        proposal_body = _archive_body(proposal)
        scope = proposal_body.get("scope")
        need(isinstance(scope, dict), "invalid_archive", "Extraction checkpoint proposal scope is missing", record.get("id"))
        pins = body.get("pins", [])
        need(isinstance(pins, list) and len(set(pins)) == len(pins), "invalid_archive", "Extraction checkpoint pins are malformed")
        for pin in pins:
            _archive_blob(blob_reader, pin)
        checkpoint_inventory = body.get("inventory", [])
        need(isinstance(checkpoint_inventory, list), "invalid_archive", "Extraction checkpoint inventory is malformed")
        count_delta = body.get("count_delta", {})
        if count_delta:
            need(isinstance(count_delta, dict) and all(type(count_delta.get(key, 0)) is int and count_delta.get(key, 0) >= 0
                                                       for key in ("files", "bytes", "known", "unknown")),
                 "invalid_archive", "Extraction checkpoint counts are malformed")
        for entry in checkpoint_inventory:
            need(isinstance(entry, dict), "invalid_archive", "Checkpoint inventory entry is malformed")
            if entry.get("type") == "blob":
                source_blob = entry.get("sha256")
                raw = _archive_blob(blob_reader, source_blob)
                assert raw is not None
                need(entry.get("bytes") == len(raw) and entry.get("sha256") == digest(raw),
                     "invalid_archive", "Checkpoint source blob identity differs", entry.get("path"))
                object_blob = entry.get("git_object_blob")
                object_raw = _archive_blob(blob_reader, object_blob)
                assert object_raw is not None
                object_format = scope.get("object_format")
                need(object_format in {"sha1", "sha256"} and entry.get("blob_oid")
                     and _sha256_oid(object_raw, object_format) == entry.get("blob_oid"),
                     "invalid_archive", "Checkpoint Git blob identity differs", entry.get("path"))
                need(object_blob in pins and source_blob in pins,
                     "invalid_archive", "Checkpoint Git source is outside its pin boundary", entry.get("path"))
                _git_object_payload(object_raw, "blob")
            elif entry.get("type") in {"commit", "source"}:
                if entry.get("sha256"):
                    raw = _archive_blob(blob_reader, entry["sha256"])
                    assert raw is not None
                    need(entry.get("bytes") == len(raw), "invalid_archive", "Checkpoint source size differs")
        git_pin = body.get("git_pin")
        entry_manifest = body.get("entry_manifest")
        if git_pin is not None or entry_manifest is not None:
            need(scope.get("kind") == "code" and isinstance(git_pin, dict) and isinstance(entry_manifest, list),
                 "invalid_archive", "Extraction checkpoint Git boundary is malformed", record.get("id"))
            object_format = scope.get("object_format")
            need(object_format in {"sha1", "sha256"} and git_pin.get("commit") == scope.get("commit"),
                 "invalid_archive", "Extraction checkpoint Git scope differs", record.get("id"))
            commit_raw = _archive_blob(blob_reader, git_pin.get("commit_blob"))
            tree_raw = _archive_blob(blob_reader, git_pin.get("tree_blob"))
            assert commit_raw is not None and tree_raw is not None
            need(git_pin.get("commit_blob") in pins and git_pin.get("tree_blob") in pins,
                 "invalid_archive", "Checkpoint Git root objects are outside its pin boundary")
            need(_sha256_oid(commit_raw, object_format) == git_pin.get("commit")
                 and _sha256_oid(tree_raw, object_format) == git_pin.get("tree"),
                 "invalid_archive", "Extraction checkpoint Git root identity differs", record.get("id"))
            _git_object_payload(commit_raw, "commit");_git_object_payload(tree_raw, "tree")
            entries = []
            for entry in entry_manifest:
                need(isinstance(entry, dict) and isinstance(entry.get("path"), str)
                     and entry.get("kind") in {"blob", "commit"}
                     and type(entry.get("mode")) is int and isinstance(entry.get("oid"), str),
                     "invalid_archive", "Extraction checkpoint entry manifest is malformed")
                try:
                    selected = _matches(entry["path"], _safe_roots(scope.get("roots")), _patterns(scope.get("include")))
                except Fault as exc:
                    raise Fault("invalid_archive", "Extraction checkpoint selection scope is malformed") from exc
                need(selected or scope.get("empty_scope"), "invalid_archive", "Checkpoint entry lies outside selection scope", entry["path"])
                entries.append({"path": entry["path"], "type": entry["kind"], "mode": entry["mode"], "blob_oid": entry["oid"]})
            need(len({entry["path"] for entry in entries}) == len(entries),
                 "invalid_archive", "Extraction checkpoint entry manifest contains duplicates")
            if body.get("pin_complete") is True:
                trees = git_pin.get("trees")
                need(isinstance(trees, list) and trees, "invalid_archive", "Complete Git checkpoint omits tree closure")
                tree_objects = {}
                for tree in trees:
                    need(isinstance(tree, dict) and isinstance(tree.get("oid"), str),
                         "invalid_archive", "Extraction checkpoint tree reference is malformed")
                    need(tree.get("blob") in pins,
                         "invalid_archive", "Checkpoint tree object is outside its pin boundary", tree.get("oid"))
                    tree_bytes = _archive_blob(blob_reader, tree.get("blob"))
                    assert tree_bytes is not None
                    need(_sha256_oid(tree_bytes, object_format) == tree["oid"],
                         "invalid_archive", "Extraction checkpoint tree identity differs", tree.get("oid"))
                    _git_object_payload(tree_bytes, "tree")
                    tree_objects[tree["oid"]] = tree_bytes
                need(git_pin.get("tree") in tree_objects, "invalid_archive", "Complete Git checkpoint omits its root tree")
                _kind, checkpoint_commit = _git_object_payload(commit_raw, "commit")
                root_match = re.search(rb"(?m)^tree ([0-9a-f]{40,64})$", checkpoint_commit)
                need(root_match is not None and root_match.group(1).decode("ascii") == git_pin.get("tree"),
                     "invalid_archive", "Complete Git checkpoint commit/tree boundary differs")
                expected_entries = []
                active = set()
                def walk_checkpoint(oid, prefix):
                    need(oid not in active, "invalid_archive", "Complete Git checkpoint tree cycle", oid)
                    active.add(oid)
                    tree_value = tree_objects.get(oid)
                    need(tree_value is not None, "invalid_archive", "Complete Git checkpoint tree closure is incomplete", oid)
                    for mode, kind, child_oid, name in _tree_entries(tree_value, object_format):
                        path = f"{prefix}/{name}" if prefix else name
                        if kind == "tree":
                            walk_checkpoint(child_oid, path)
                        elif _matches(path, _safe_roots(scope.get("roots")), _patterns(scope.get("include"))):
                            expected_entries.append({"path": path, "type": kind, "mode": mode, "blob_oid": child_oid})
                    active.remove(oid)
                if not scope.get("empty_scope"):
                    walk_checkpoint(git_pin["tree"], "")
                expected_entries.sort(key=lambda value: value["path"])
                entries.sort(key=lambda value: value["path"])
                need(entries == expected_entries, "invalid_archive", "Complete Git checkpoint entry manifest differs")
        if body.get("complete") is True and body.get("stage") != "git_pin":
            need(isinstance(body.get("key"), str) and isinstance(body.get("items"), list)
                 and isinstance(body.get("inventory"), list) and isinstance(body.get("count_delta"), dict),
                 "invalid_archive", "Complete extraction checkpoint is malformed")
            for item in body["items"]:
                need(isinstance(item, dict) and isinstance(item.get("id"), str)
                     and isinstance(item.get("body"), dict) and digest(item["body"]) == item.get("digest"),
                     "invalid_archive", "Checkpoint item is malformed")
            for entry in body["inventory"]:
                need(isinstance(entry, dict), "invalid_archive", "Checkpoint inventory entry is malformed")
                for key in ("sha256", "git_object_blob"):
                    if isinstance(entry.get(key), str):
                        _archive_blob(blob_reader, entry[key])
            items = body["items"]
            delta = body["count_delta"]
            counted_items = items[1:] if items and isinstance(items[0].get("body"), dict) \
                and items[0]["body"].get("type") == "document" else items
            need(delta.get("files") == len(body["inventory"])
                 and delta.get("bytes") == sum(int(entry.get("bytes", 0) or 0) for entry in body["inventory"])
                 and delta.get("known") == sum(item.get("status") == "known" for item in counted_items)
                 and delta.get("unknown") == sum(item.get("status") == "unknown" for item in counted_items),
                 "invalid_archive", "Complete extraction checkpoint counts differ")
            if "lines" in delta:
                need(delta.get("lines") == len(counted_items),
                     "invalid_archive", "Complete document checkpoint line count differs")
        if body.get("pin_complete") is True:
            need(isinstance(body.get("git_pin"), dict) and isinstance(body.get("entry_manifest"), list),
                 "invalid_archive", "Pinned checkpoint is missing its boundary manifest")
    for proposal in proposals.values():
        result = _archive_result(proposal)
        if proposal.get("status") in {"staging", "failed"}:
            if isinstance(result, dict):
                need(result.get("status") in {None, "staging", "failed"},
                     "invalid_archive", "Failed/staging proposal claims a ready result", proposal.get("id"))
            need(not any(record.get("proposal") == proposal["id"] and record.get("kind") == "extracted" for record in records),
                 "invalid_archive", "Failed/staging proposal has a published extraction record", proposal.get("id"))


def _archive_validate_typed_ref(ref: Any, owner: str) -> None:
    """Validate the immutable wire shape of a stored Unit B target ref.

    Archive inspection has no live controller against which to resolve an
    artifact or candidate, but it can still reject the common corruption case
    where a typed edge is replaced by a path/name or a partially copied JSON
    object.  Full identity/currentness remains the resolver's responsibility
    at proposal/adoption time.
    """
    need(isinstance(ref, dict) and isinstance(ref.get("ref_type"), str),
         "invalid_archive", "Typed target reference is malformed", owner)
    kind = ref["ref_type"]
    schemas = {
        "git_file": ({"ref_type", "repository", "object_format", "commit", "path", "blob_oid", "sha256", "mode", "pin_revision", "pin_revision_digest"}, set()),
        "git_symbol": ({"ref_type", "repository", "object_format", "commit", "path", "blob_oid", "sha256", "mode", "pin_revision", "pin_revision_digest", "adapter", "adapter_digest", "qualified_name", "kind", "ordinal", "start_byte", "end_byte", "span_sha256", "signature_hash"}, set()),
        "candidate_symbol": ({"ref_type", "candidate", "task", "task_revision", "candidate_digest", "snapshot_digest", "repository", "path", "sha256", "mode", "adapter", "adapter_digest", "qualified_name", "kind", "ordinal", "start_byte", "end_byte", "span_sha256", "signature_hash"}, set()),
        "source_span": ({"ref_type", "source_id", "blob_digest", "byte_start", "byte_end", "unicode_start", "unicode_end", "span_hash"}, set()),
        "artifact_ac": ({"ref_type", "artifact", "revision", "body_digest", "ac_pointer", "ac_digest"}, {"ac_id"}),
    }
    required, optional = schemas.get(kind, (None, None))
    need(required is not None, "invalid_archive", "Unknown typed target reference", {"owner": owner, "ref_type": kind})
    need(set(ref) == required | (set(ref) & optional), "invalid_archive", "Typed target reference fields differ", owner)
    for key in ("sha256", "blob_digest", "candidate_digest", "snapshot_digest", "span_sha256", "signature_hash", "ac_digest", "pin_revision_digest", "adapter_digest"):
        if key in ref:
            need(isinstance(ref[key], str) and _HEX64.fullmatch(ref[key]), "invalid_archive", "Typed reference digest is malformed", owner)
    for key in ("byte_start", "byte_end", "unicode_start", "unicode_end", "start_byte", "end_byte", "ordinal", "task_revision", "revision", "mode"):
        if key in ref:
            need(type(ref[key]) is int and ref[key] >= 0, "invalid_archive", "Typed reference coordinate is malformed", owner)
    if kind in {"git_file", "git_symbol"}:
        need(ref.get("object_format") in {"sha1", "sha256"} and isinstance(ref.get("commit"), str)
             and isinstance(ref.get("blob_oid"), str) and isinstance(ref.get("path"), str),
             "invalid_archive", "Git target reference identity is malformed", owner)
        oid_length = 40 if ref["object_format"] == "sha1" else 64
        need(len(ref["commit"]) == oid_length and _HEX.fullmatch(ref["commit"])
             and ref["commit"].lower() == ref["commit"]
             and len(ref["blob_oid"]) == oid_length and _HEX.fullmatch(ref["blob_oid"])
             and ref["blob_oid"].lower() == ref["blob_oid"],
             "invalid_archive", "Git target reference OID is malformed", owner)
        path = ref["path"]
        try:
            parsed_path = PurePosixPath(path)
        except (TypeError, ValueError):
            parsed_path = None
        need(isinstance(path, str) and bool(path) and parsed_path is not None
             and not parsed_path.is_absolute() and path not in {".", ".."}
             and "\\" not in path and "\x00" not in path
             and str(parsed_path) == path
             and all(part not in {"", ".", ".."} for part in parsed_path.parts)
             and ".git" not in parsed_path.parts
             and all(not part.startswith(".daikibo-control") for part in parsed_path.parts),
             "invalid_archive", "Git target reference path is malformed", owner)
        need(isinstance(ref.get("pin_revision"), str) and ref["pin_revision"],
             "invalid_archive", "Git target reference has no immutable pin", owner)
    elif kind == "source_span":
        need(isinstance(ref.get("source_id"), str) and isinstance(ref.get("span_hash"), str)
             and ref["byte_start"] < ref["byte_end"] and ref["unicode_start"] <= ref["unicode_end"],
             "invalid_archive", "Source span reference interval is malformed", owner)
        need(_HEX64.fullmatch(ref["span_hash"]), "invalid_archive", "Source span hash is malformed", owner)
    elif kind == "artifact_ac":
        need(isinstance(ref.get("artifact"), str) and type(ref.get("revision")) is int and ref["revision"] > 0
             and isinstance(ref.get("ac_pointer"), str), "invalid_archive", "Artifact AC reference is malformed", owner)
    else:
        need(ref.get("adapter") == "python-ast-v1" and ref.get("kind") in {"function", "async_function", "class"}
             and isinstance(ref.get("qualified_name"), str) and ref["qualified_name"],
             "invalid_archive", "Python symbol reference identity is malformed", owner)
        need(ref["start_byte"] <= ref["end_byte"],
             "invalid_archive", "Python symbol reference interval is malformed", owner)


def _archive_validate_complete_revision(revision: dict[str, Any], items: list[dict[str, Any]],
                                         proposal: dict[str, Any] | None, records: list[dict[str, Any]],
                                         blob_reader: Any) -> None:
    body = _archive_body(revision)
    need(revision.get("status") in {"ready", "active", "superseded"}, "invalid_archive", "Complete revision has an invalid status")
    need(body.get("revision") == revision.get("id") and body.get("set_id") == revision.get("set_id")
         and body.get("project") == revision.get("project") and body.get("kind") in {"code", "document"},
         "invalid_archive", "Complete revision identity differs", revision.get("id"))
    need(body.get("adapter") == revision.get("adapter"),
         "invalid_archive", "Complete revision adapter differs", revision.get("id"))
    need(proposal is not None and proposal.get("status") in {"ready", "adopted"},
         "invalid_archive", "Complete revision has no ready proposal", revision.get("id"))
    proposal_body = _archive_body(proposal)
    need(proposal_body.get("id") == proposal.get("id") and proposal_body.get("set_id") == revision.get("set_id")
         and proposal_body.get("project") == revision.get("project") and proposal_body.get("kind") == body.get("kind"),
         "invalid_archive", "Revision and proposal identity differs", revision.get("id"))
    need(proposal_body.get("scope") == body.get("scope") and proposal_body.get("adapter") == body.get("adapter"),
         "invalid_archive", "Revision and proposal scope differs", revision.get("id"))
    pins = body.get("pins")
    need(isinstance(pins, list) and len(set(pins)) == len(pins),
         "invalid_archive", "Complete revision pins are malformed", revision.get("id"))
    for pin in pins:
        _archive_blob(blob_reader, pin)
    complete_records = [record for record in records if record.get("revision") == revision.get("id") and record.get("kind") == "extracted"]
    need(len(complete_records) == 1, "invalid_archive", "Complete revision has an incomplete extraction record", revision.get("id"))
    extraction = _archive_body(complete_records[0])
    result = _archive_result(proposal)
    for value in (extraction, result):
        if isinstance(value, dict):
            need(value.get("revision") == revision.get("id") and value.get("revision_digest") == revision.get("digest")
                 and value.get("population_digest") == revision.get("population_digest") and value.get("status") == "ready",
                 "invalid_archive", "Published extraction result differs from revision", revision.get("id"))
    ordered = sorted(items, key=lambda row: (row.get("ordinal"), row.get("id")))
    need(len(ordered) == len(items), "invalid_archive", "Complete revision item order is malformed")
    for index, row in enumerate(ordered):
        need(type(row.get("ordinal")) is int and row["ordinal"] == index,
             "invalid_archive", "Complete revision item ordinals have a gap or duplicate", revision.get("id"))
    leaf_rows = []
    unknown_leaf_count = 0
    for row in ordered:
        body_row = _archive_body(row)
        need(row.get("revision") == revision.get("id") and digest(body_row) == row.get("digest"),
             "invalid_archive", "Traceability item digest or revision differs", row.get("id"))
        need(row.get("item_kind") in {"file", "atom", "symbol", "line", "group"},
             "invalid_archive", "Traceability item kind is invalid", row.get("id"))
        need(row.get("status") in {"known", "unknown", "tombstone"}, "invalid_archive", "Traceability item status is invalid", row.get("id"))
        need(int(bool(body_row.get("leaf"))) == int(row.get("leaf", 0)), "invalid_archive", "Traceability item leaf flag differs", row.get("id"))
        if "path" in body_row:
            need(body_row.get("path") == row.get("path"), "invalid_archive", "Traceability item path identity differs", row.get("id"))
        if "status" in body_row:
            need(body_row.get("status") == row.get("status"), "invalid_archive", "Traceability item status identity differs", row.get("id"))
        _archive_item_kind(body_row, row["item_kind"])
        leaf_rows.append(row) if int(row.get("leaf", 0)) else None
        if int(row.get("leaf", 0)) and row.get("status") == "unknown":
            unknown_leaf_count += 1
    counts = body.get("counts")
    need(isinstance(counts, dict), "invalid_archive", "Complete revision counts are missing", revision.get("id"))
    need(counts.get("items") == len(ordered) and counts.get("leaf") == len(leaf_rows)
         and counts.get("unknown") == unknown_leaf_count,
         "invalid_archive", "Complete revision item counts differ", revision.get("id"))
    if body.get("kind") == "code":
        need(counts.get("known") == sum(row.get("status") == "known" for row in ordered),
             "invalid_archive", "Complete code known count differs", revision.get("id"))
    else:
        line_rows = [row for row in ordered if row.get("item_kind") == "line"]
        need(counts.get("lines") == len(line_rows)
             and counts.get("known") == sum(row.get("status") == "known" for row in line_rows),
             "invalid_archive", "Complete document line counts differ", revision.get("id"))
    item_digest = _sequence_digest(row["digest"] for row in ordered)
    leaf_ids = sorted(row["id"] for row in leaf_rows)
    need(body.get("item_digest") == item_digest and body.get("leaf_ids_digest") == digest(leaf_ids),
         "invalid_archive", "Complete revision sequence or leaf digest differs", revision.get("id"))
    population = digest({"proposal": proposal["digest"], "items": item_digest,
                         "leaf_count": len(leaf_rows), "unknown_count": unknown_leaf_count})
    need(revision.get("population_digest") == population,
         "invalid_archive", "Complete revision population digest differs", revision.get("id"))
    inventory = body.get("inventory")
    need(isinstance(inventory, list), "invalid_archive", "Complete revision inventory is missing", revision.get("id"))
    expected_files = len(inventory)
    need(counts.get("files") == expected_files and counts.get("bytes") == sum(int(entry.get("bytes", 0)) for entry in inventory),
         "invalid_archive", "Complete revision inventory counts differ", revision.get("id"))
    if body.get("kind") == "code":
        need(counts.get("empty_selection") == (not inventory)
             and counts.get("empty_scope") == bool(body.get("scope", {}).get("empty_scope")),
             "invalid_archive", "Complete code selection flags differ", revision.get("id"))
    for row in ordered:
        _archive_item_range(row, _archive_body(row))
    if body.get("kind") == "code":
        _archive_validate_git_population(revision, ordered, blob_reader)
    else:
        _archive_validate_document_population(revision, ordered, blob_reader)


def _validate_archive_rows(payload: dict[str, Any], blob_reader: Any = None,
                           external_tables: dict[str, list[dict[str, Any]]] | None = None) -> None:
    """Validate traceability rows and their complete/partial population.

    This is intentionally shared by the dedicated v10 ZIP and the standard
    chunked v10 archive.  A complete revision is checked against its actual
    pinned source bytes; a failed/staging proposal is checked only for the
    durable checkpoint invariants and is never promoted by validation.
    """
    tables = payload.get("tables")
    need(isinstance(tables, dict), "invalid_archive", "Traceability history tables are missing")
    project = payload.get("project")
    need(isinstance(project, str) and project, "invalid_archive", "Traceability archive project is missing")
    project_record = payload.get("project_record")
    need(isinstance(project_record, dict) and project_record.get("id") == project,
         "invalid_archive", "Traceability archive project record differs")
    expected_tables = {"traceability_sets", "traceability_revisions", "traceability_items", "traceability_proposals", "traceability_decisions", "traceability_mappings", "traceability_bindings", "traceability_records"}
    need(expected_tables <= set(tables), "invalid_archive", "Traceability history tables are incomplete")
    ids: dict[str, set[str]] = {}
    decoded: dict[str, list[dict[str, Any]]] = {}
    for table in expected_tables:
        rows = tables[table]
        need(isinstance(rows, list), "invalid_archive", "Traceability history table is not a list", table)
        ids[table] = set();decoded[table] = []
        for row in rows:
            need(isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] not in ids[table], "invalid_archive", "Duplicate or malformed traceability row", table)
            ids[table].add(row["id"]);decoded[table].append(row)
            if table != "traceability_sets":
                need(row.get("project") == project, "invalid_archive", "Traceability row belongs to another project", {"table": table, "id": row.get("id")})
            if table in {"traceability_revisions", "traceability_proposals", "traceability_items", "traceability_decisions", "traceability_mappings", "traceability_bindings", "traceability_records"}:
                body = _archive_body(row)
                need(digest(body) == row.get("digest"), "invalid_archive", "Traceability row digest differs", {"table": table, "id": row.get("id")})
    revisions = ids["traceability_revisions"];proposals = ids["traceability_proposals"];sets = ids["traceability_sets"]
    context = external_tables if external_tables is not None else payload.get("context", {})
    if context is None:
        context = {}
    need(isinstance(context, dict), "invalid_archive", "Traceability archive context is malformed")
    context_rows: dict[str, list[dict[str, Any]]] = {}
    context_ids: dict[str, set[str]] = {}
    for section, rows in context.items():
        need(isinstance(section, str) and isinstance(rows, list),
             "invalid_archive", "Traceability archive context section is malformed", section)
        seen: set[str] = set(); decoded_rows: list[dict[str, Any]] = []
        for row in rows:
            need(isinstance(row, dict), "invalid_archive", "Malformed traceability context row", section)
            row_key = row.get("id")
            if section == "revisions" and row_key is None:
                row_key = canonical([row.get("artifact"), row.get("revision")]).decode()
            need(isinstance(row_key, str) and row_key not in seen,
                 "invalid_archive", "Duplicate or malformed traceability context row", section)
            if row.get("project") is not None:
                need(row.get("project") == project, "invalid_archive", "Traceability context row belongs to another project", row.get("id"))
            seen.add(row_key); decoded_rows.append(row)
        context_rows[section] = decoded_rows; context_ids[section] = seen

    # Candidate provenance is resolved through this finite accessor by both
    # dedicated and chunked archive validation.  It never consults the live
    # controller, signing keys, or a mutable repository working tree.
    pinned_context = _ArchivePinnedContext(context_rows, blob_reader, project=project)

    def required_context(section: str, owner: Any, *, allow_empty: bool = False) -> list[dict[str, Any]]:
        """Return a typed context section, never treating absence as success.

        Archive context is a finite projection rather than a live database.
        A ref therefore derives the sections it needs and a missing/empty
        required section is an invalid archive, even if the remaining CAS
        bytes happen to make the structural ref look plausible.
        """
        need(section in context_rows, "invalid_archive",
             "Required typed-reference context section is missing", {"section": section, "owner": owner})
        rows = context_rows[section]
        if not allow_empty:
            need(rows, "invalid_archive",
                 "Required typed-reference context section is empty", {"section": section, "owner": owner})
        return rows

    def context_row(section: str, ident: str, owner: Any) -> dict[str, Any]:
        rows = required_context(section, owner)
        matches = [row for row in rows if row.get("id") == ident]
        need(len(matches) == 1, "invalid_archive",
             "Required typed-reference context row is missing or ambiguous",
             {"section": section, "id": ident, "owner": owner})
        return matches[0]

    def context_revision(artifact: str, revision: int, owner: Any) -> dict[str, Any]:
        rows = required_context("revisions", owner)
        matches = [row for row in rows
                   if row.get("artifact") == artifact and row.get("revision") == revision]
        need(len(matches) == 1, "invalid_archive",
             "Required artifact revision context row is missing or ambiguous",
             {"artifact": artifact, "revision": revision, "owner": owner})
        return matches[0]

    revision_map = {row["id"]: row for row in decoded["traceability_revisions"]}
    proposal_map = {row["id"]: row for row in decoded["traceability_proposals"]}
    items_by_revision: dict[str, list[dict[str, Any]]] = {ident: [] for ident in revisions}
    for row in decoded["traceability_revisions"]:
        need(row.get("set_id") in sets and row.get("project") == project, "invalid_archive", "Revision references a missing traceability set", row.get("id"))
        need(row.get("status") in {"staging", "failed", "ready", "active", "superseded"}, "invalid_archive", "Revision status is invalid", row.get("id"))
        need(type(row.get("revision")) is int and row["revision"] > 0, "invalid_archive", "Revision number is malformed", row.get("id"))
        need(isinstance(row.get("adapter"), str), "invalid_archive", "Revision adapter is malformed", row.get("id"))
    for row in decoded["traceability_items"]:
        need(row.get("revision") in revisions, "invalid_archive", "Item references a missing traceability revision", row.get("id"))
        body = _archive_body(row)
        need(type(row.get("leaf")) is int and row["leaf"] in {0, 1},
             "invalid_archive", "Item leaf column is malformed", row.get("id"))
        need(int(bool(body.get("leaf"))) == int(row.get("leaf", 0)), "invalid_archive", "Item leaf flag differs", row.get("id"))
        need(row.get("status") in {"known", "unknown", "tombstone"}, "invalid_archive", "Item status is invalid", row.get("id"))
        items_by_revision[row["revision"]].append(row)
    for row in decoded["traceability_proposals"]:
        need(row.get("set_id") in sets and row.get("project") == project, "invalid_archive", "Proposal references a missing traceability set", row.get("id"))
        need(row.get("status") in {"proposed", "staging", "ready", "failed", "adopted", "withdrawn"}, "invalid_archive", "Proposal status is invalid", row.get("id"))
        proposal_body = _archive_body(row)
        need(proposal_body.get("id") == row.get("id") and proposal_body.get("set_id") == row.get("set_id")
             and proposal_body.get("project") == project and proposal_body.get("kind") == row.get("kind"),
             "invalid_archive", "Proposal identity differs", row.get("id"))
        need(row.get("semantic_material_digest") == digest({"kind": proposal_body.get("kind"),
                                                              "scope": proposal_body.get("scope"),
                                                              "adapter": proposal_body.get("adapter")}),
             "invalid_archive", "Proposal semantic material digest differs", row.get("id"))
        expected_active = row.get("expected_active")
        if isinstance(expected_active, str):
            expected_active = parse_json(expected_active, limit=MAX_REVISION_BODY_BYTES)
        need(expected_active == proposal_body.get("expected_active"),
             "invalid_archive", "Proposal expected-active identity differs", row.get("id"))
    for table in ("traceability_decisions", "traceability_mappings", "traceability_bindings"):
        for row in decoded[table]:
            need(row.get("revision") in revisions, "invalid_archive", "Traceability history row references a missing revision", row.get("id"))
    for row in decoded["traceability_records"]:
        need(row.get("revision") is None or row.get("revision") in revisions, "invalid_archive", "Record references a missing revision", row.get("id"))
        need(row.get("proposal") is None or row.get("proposal") in proposals, "invalid_archive", "Record references a missing proposal", row.get("id"))
    for row in decoded["traceability_sets"]:
        need(row.get("project") == project, "invalid_archive", "Traceability set belongs to another project", row.get("id"))
        need(row.get("kind") in {"population", "code", "document"} and isinstance(row.get("name"), str),
             "invalid_archive", "Traceability set identity is malformed", row.get("id"))
        active = row.get("active_revision")
        if active is not None:
            need(active in revisions, "invalid_archive", "Active traceability revision is missing", row.get("id"))
            active_row = revision_map[active]
            need(row.get("active_digest") == active_row.get("digest"), "invalid_archive", "Active traceability digest differs", row.get("id"))
        else:
            need(row.get("active_digest") is None, "invalid_archive", "Traceability set has a digest without an active revision", row.get("id"))
    _archive_validate_partial_records(decoded["traceability_records"], proposal_map, revision_map, blob_reader)
    records = decoded["traceability_records"]
    for record in records:
        body = _archive_body(record)
        if record.get("kind") == "proposal":
            proposal = proposal_map.get(record.get("proposal"))
            need(proposal is not None and body.get("proposal") == proposal["id"]
                 and body.get("digest") == proposal["digest"],
                 "invalid_archive", "Proposal history record differs", record.get("id"))
        elif record.get("kind") == "extraction_failed":
            proposal = proposal_map.get(record.get("proposal"))
            need(proposal is not None and body.get("proposal") == proposal["id"]
                 and body.get("status") == "failed",
                 "invalid_archive", "Failed extraction history record differs", record.get("id"))
    # Unit B typed rows and append-only adoption records share the Unit A
    # archive.  Validate their cross-table identity here so a dedicated
    # traceability export and the standard chunked v10 archive reject the
    # same missing/foreign packet, edge, or closure reference.
    decision_ids={row["id"] for row in decoded["traceability_decisions"]}
    mapping_ids={row["id"] for row in decoded["traceability_mappings"]}
    binding_ids={row["id"] for row in decoded["traceability_bindings"]}
    record_map={row["id"]:row for row in records}
    def check_b_ref(ref, owner):
        need(isinstance(ref,dict) and ref.get("table") in {"traceability_proposals","traceability_decisions","traceability_mappings","traceability_bindings","traceability_records","traceability_revisions"}
             and isinstance(ref.get("id"),str) and isinstance(ref.get("digest"),str),"invalid_archive","Typed traceability subject ref is malformed",owner)
        table=ref["table"];ident=ref["id"]
        table_ids=ids.get(table,set())
        need(ident in table_ids,"invalid_archive","Typed traceability subject ref is missing",owner)
        source=(decoded[table] if table in decoded else [])
        source_row=next(row for row in source if row["id"]==ident)
        need(ref["digest"]==source_row.get("digest"),"invalid_archive","Typed traceability subject digest differs",owner)
        return source_row
    def check_typed_ref(ref, owner):
        """Check a typed edge against the archived pin/index, not only shape."""
        _archive_validate_typed_ref(ref, owner)
        kind=ref["ref_type"]
        if kind in {"git_file", "git_symbol"}:
            revision=revision_map.get(ref.get("pin_revision"))
            need(revision is not None and revision.get("project")==project
                 and revision.get("status") in {"ready", "active", "superseded"},
                 "invalid_archive", "Git target pin revision is missing or incomplete", owner)
            need(ref.get("pin_revision_digest") == revision.get("digest"),
                 "invalid_archive", "Git target pin revision digest differs from its archived pin", owner)
            revision_body=_archive_body(revision)
            need(revision_body.get("kind")=="code" and revision_body.get("revision")==revision.get("id"),
                 "invalid_archive", "Git target pin revision kind differs", owner)
            scope=revision_body.get("scope")
            need(isinstance(scope,dict) and scope.get("repository")==ref.get("repository")
                 and scope.get("object_format")==ref.get("object_format")
                 and scope.get("commit")==ref.get("commit"),
                 "invalid_archive", "Git target differs from its archived pin scope", owner)
            inventory=next((entry for entry in revision_body.get("inventory", [])
                            if isinstance(entry,dict) and entry.get("path")==ref.get("path")),None)
            need(inventory is not None and inventory.get("blob_oid")==ref.get("blob_oid")
                 and inventory.get("sha256")==ref.get("sha256") and inventory.get("mode")==ref.get("mode"),
                 "invalid_archive", "Git target differs from its archived file inventory", owner)
            raw=_archive_blob(blob_reader, ref.get("sha256"))
            assert raw is not None
            if kind=="git_symbol":
                contract=revision_body.get("adapter_contract")
                need(isinstance(contract,dict) and contract.get("id")=="python-ast-v1"
                     and contract.get("implementation_digest")==ref.get("adapter_digest"),
                     "invalid_archive", "Git symbol adapter differs from its archived revision", owner)
                matches=[]
                for item in items_by_revision.get(revision.get("id"),[]):
                    if item.get("item_kind")!="symbol" or item.get("path")!=ref.get("path"):
                        continue
                    item_body=_archive_body(item)
                    if (item_body.get("qualified_name")==ref.get("qualified_name")
                            and item_body.get("kind")==ref.get("kind")
                            and item_body.get("ordinal")==ref.get("ordinal")
                            and item_body.get("byte_start")==ref.get("start_byte")
                            and item_body.get("byte_end")==ref.get("end_byte")
                            and item_body.get("signature_hash")==ref.get("signature_hash")
                            and (item_body.get("source_span") or {}).get("span_hash")==ref.get("span_sha256")):
                        matches.append(item)
                need(len(matches)==1, "invalid_archive", "Git symbol target is absent or ambiguous in its archived items", owner)
        elif kind=="source_span":
            raw=_archive_blob(blob_reader, ref.get("blob_digest"))
            assert raw is not None
            source = context_row("sources", ref.get("source_id"), owner)
            need(source.get("project") == project
                 and source.get("blob") == ref.get("blob_digest"),
                 "invalid_archive", "Source span source differs from its archived source", owner)
            try:
                source_characters = len(raw.decode("utf-8"))
            except UnicodeDecodeError:
                need(False, "invalid_archive", "Source span source bytes are not valid UTF-8", owner)
            need(type(source.get("characters")) is int and source["characters"] == source_characters,
                 "invalid_archive", "Source span source character count differs", owner)
            start,end=ref.get("byte_start"),ref.get("byte_end")
            need(0 <= start <= end <= len(raw) and digest(raw[start:end])==ref.get("span_hash"),
                 "invalid_archive", "Source span target differs from its archived CAS bytes", owner)
            try:
                expected_start=_unicode_position(raw,start,raw.startswith(b"\xef\xbb\xbf"))
                expected_end=_unicode_position(raw,end,raw.startswith(b"\xef\xbb\xbf"))
            except Fault:
                need(ref.get("unicode_start") is None and ref.get("unicode_end") is None,
                     "invalid_archive", "Invalid UTF-8 source span has guessed coordinates", owner)
            else:
                need(ref.get("unicode_start")==expected_start and ref.get("unicode_end")==expected_end,
                     "invalid_archive", "Source span Unicode coordinates differ", owner)
        elif kind=="candidate_symbol":
            # Candidate provenance is resolved by the same finite helper used
            # by the live resolver.  The archive adapter supplies only saved
            # context/CAS and maps every semantic failure to invalid_archive.
            resolved = resolve_candidate_pin(project, ref, pinned_context,
                                             failure=_archive_candidate_failure)
            raw = _archive_blob(blob_reader, ref.get("sha256"))
            assert raw is not None
            # Candidate refs do not have a traceability item row in the
            # archive. Re-run the frozen AST partition over the pinned CAS so
            # a recomputed outer archive hash cannot preserve a wrong ordinal,
            # name, signature, or span.
            parsed = partition_python(raw, ref["path"])
            symbols = [item["body"] for item in parsed.get("items", [])
                       if item.get("item_kind") == "symbol"]
            matches = [item for item in symbols
                       if item.get("qualified_name") == ref.get("qualified_name")
                       and item.get("kind") == ref.get("kind")
                       and item.get("ordinal") == ref.get("ordinal")
                       and item.get("byte_start") == ref.get("start_byte")
                       and item.get("byte_end") == ref.get("end_byte")
                       and item.get("signature_hash") == ref.get("signature_hash")
                       and (item.get("source_span") or {}).get("span_hash") == ref.get("span_sha256")]
            need(len(matches) == 1, "invalid_archive",
                 "Candidate symbol target is absent or ambiguous in its archived CAS bytes", owner)
        elif kind=="artifact_ac":
            need(type(ref.get("revision")) is int and ref.get("revision")>0,
                 "invalid_archive", "Artifact AC revision is malformed", owner)
            pointer = ref.get("ac_pointer")
            need(isinstance(pointer, str)
                 and re.fullmatch(r"/acceptance/(?:0|[1-9][0-9]*)", pointer) is not None,
                 "invalid_archive", "Artifact AC pointer is outside the accepted container", owner)
            if "ac_id" in ref:
                need(isinstance(ref.get("ac_id"), str) and ref["ac_id"],
                     "invalid_archive", "Artifact AC id is malformed", owner)
            artifact = context_row("artifacts", ref.get("artifact"), owner)
            need(artifact.get("project") == project,
                 "invalid_archive", "Artifact AC points to a missing or foreign artifact", owner)
            artifact_body = _archive_body(artifact)
            need(digest(artifact_body) == artifact.get("digest"),
                 "invalid_archive", "Artifact body digest differs", owner)
            revision_row = context_revision(ref.get("artifact"), ref.get("revision"), owner)
            need(revision_row.get("digest") == ref.get("body_digest"),
                 "invalid_archive", "Artifact AC revision digest differs", owner)
            revision_body = _archive_body(revision_row)
            need(digest(revision_body) == revision_row.get("digest"),
                 "invalid_archive", "Artifact revision body digest differs", owner)
            current = (artifact.get("revision") == ref.get("revision")
                       and artifact.get("digest") == ref.get("body_digest")
                       and artifact.get("status") == "accepted")
            need(revision_row.get("status") == "accepted" or current,
                 "invalid_archive", "Artifact AC revision lacks an accepted-history basis", owner)
            acceptance = revision_body.get("acceptance")
            need(isinstance(acceptance, list), "invalid_archive", "Artifact AC revision has no acceptance container", owner)
            index = int(ref["ac_pointer"].rsplit("/", 1)[1])
            need(index < len(acceptance) and digest(acceptance[index]) == ref.get("ac_digest"),
                 "invalid_archive", "Artifact AC digest differs from its archived criterion", owner)
            if "ac_id" in ref:
                value = acceptance[index]
                criterion_id = (value.get("id") if isinstance(value, dict)
                                else value if isinstance(value, str) else None)
                need(criterion_id == ref.get("ac_id"),
                     "invalid_archive", "Artifact AC id differs from its archived criterion", owner)
        return ref
    for row in decoded["traceability_proposals"]:
        body=_archive_body(row);kind=row.get("kind")
        if kind=="decision":
            ident=body.get("decision_id");need(ident in decision_ids,"invalid_archive","Decision proposal lacks TDEC",row["id"])
            drow=next(value for value in decoded["traceability_decisions"] if value["id"]==ident)
            db=_archive_body(drow);need(db.get("proposal")==row["id"] and db.get("revision")==body.get("revision"),"invalid_archive","Decision/TProp correspondence differs",row["id"])
        elif kind=="mapping" and body.get("mapping_id"):
            ident=body.get("mapping_id");need(ident in mapping_ids,"invalid_archive","Mapping proposal lacks TMAP",row["id"])
            mrow=next(value for value in decoded["traceability_mappings"] if value["id"]==ident)
            mb=_archive_body(mrow);need(mb.get("proposal")==row["id"] and mb.get("revision")==body.get("revision"),"invalid_archive","Mapping/TProp correspondence differs",row["id"])
        elif kind=="scope":
            ident=body.get("binding_id");need(ident in binding_ids,"invalid_archive","Scope proposal lacks TBIND",row["id"])
            brow=next(value for value in decoded["traceability_bindings"] if value["id"]==ident)
            bb=_archive_body(brow);need(bb.get("proposal")==row["id"] and bb.get("revision")==body.get("revision"),"invalid_archive","Scope/TBind correspondence differs",row["id"])
    for table in ("traceability_decisions","traceability_mappings","traceability_bindings"):
        for row in decoded[table]:
            body=_archive_body(row)
            need(body.get("id")==row["id"] and body.get("project")==project,"invalid_archive","Typed traceability row identity differs",row["id"])
            prop=body.get("proposal");need(prop in proposals,"invalid_archive","Typed traceability row lacks TPROP",row["id"])
            need(body.get("revision")==row.get("revision"),"invalid_archive","Typed traceability revision differs",row["id"])
            if table == "traceability_decisions":
                need(body.get("kind")=="decision" and isinstance(body.get("decisions"),list)
                     and body.get("required_leaf_ids")==sorted(set(body.get("required_leaf_ids",[]))),
                     "invalid_archive","Decision body shape is malformed",row["id"])
                for entry in body["decisions"]:
                    need(isinstance(entry,dict) and entry.get("item") in set(body["required_leaf_ids"])
                         and entry.get("handling") in {"undecided","port","replace","exclude"},
                         "invalid_archive","Decision entry identity is malformed",row["id"])
                    for field in ("requirement", "acceptance", "design"):
                        if entry.get(field) is not None:
                            check_typed_ref(entry[field], row["id"])
                    for field in ("input_requirements", "output_targets"):
                        refs = entry.get(field, [])
                        need(isinstance(refs, list), "invalid_archive", "Decision typed reference list is malformed", row["id"])
                        for target in refs:
                            check_typed_ref(target, row["id"])
            elif table == "traceability_mappings":
                need(body.get("kind")=="mapping" and isinstance(body.get("mappings"),list),
                     "invalid_archive","Mapping body shape is malformed",row["id"])
                for edge in body["mappings"]:
                    need(isinstance(edge,dict) and isinstance(edge.get("leaf_ids"),list)
                         and edge.get("leaf_ids")==sorted(set(edge.get("leaf_ids",[])))
                         and edge.get("purpose") in {"code_port","document_requirement"},
                         "invalid_archive","Mapping edge shape is malformed",row["id"])
                    decision_ref=edge.get("decision_ref")
                    drow=check_b_ref(decision_ref,row["id"])
                    need(decision_ref.get("table")=="traceability_decisions" and drow.get("revision")==row.get("revision"),
                         "invalid_archive","Mapping decision reference differs",row["id"])
                    dbody=_archive_body(drow)
                    entries={entry.get("item"):entry for entry in dbody.get("decisions",[])}
                    for leaf in edge["leaf_ids"]:
                        need(leaf in entries and entries[leaf].get("handling") in {"port","replace"},
                             "invalid_archive","Mapping edge is not covered by its decision",row["id"])
                    for target in edge.get("target_refs",[]):
                        check_typed_ref(target, row["id"])
            else:
                need(body.get("kind")=="scope_binding" and isinstance(body.get("program"),str)
                     and isinstance(body.get("scope_requirement"),dict),
                     "invalid_archive","Scope binding body shape is malformed",row["id"])
                check_typed_ref(body["scope_requirement"], row["id"])
    packet_rows=[]
    for record in records:
        body=_archive_body(record)
        if isinstance(body.get("subject_ref"),dict):
            subject_row=check_b_ref(body["subject_ref"],record["id"])
            need(body.get("project")==project,"invalid_archive","Traceability record project differs",record["id"])
            if body.get("revision") is not None:
                need(body.get("revision")==record.get("revision"),"invalid_archive","Traceability record revision differs",record["id"])
        if record.get("kind")=="review_packet":
            packet_rows.append(record)
            need(body.get("format")=="traceability.review-packet.v1" and body.get("kind")=="review_packet","invalid_archive","Review packet format is malformed",record["id"])
            subject_ref = body.get("subject_ref", body.get("root_subject"))
            subject_row=check_b_ref(subject_ref,record["id"])
            need(body.get("proposal") in proposals,"invalid_archive","Review packet proposal is missing",record["id"])
            proposal=proposal_map[body["proposal"]]
            need(body.get("proposal_digest")==proposal.get("digest"),"invalid_archive","Review packet proposal digest differs",record["id"])
            need(body.get("role") in {"trace","impact"},"invalid_archive","Review packet role is invalid",record["id"])
            need(type(body.get("packet_index")) is int and body["packet_index"]>=0
                 and type(body.get("packet_count")) is int and body["packet_count"]>0,
                 "invalid_archive","Review packet index/count is malformed",record["id"])
            if body.get("revision") is not None:
                revision=revision_map.get(body["revision"])
                need(revision is not None and body.get("revision_digest")==revision.get("digest")
                     and body.get("population_digest")==revision.get("population_digest"),
                     "invalid_archive","Review packet revision identity differs",record["id"])
            required=body.get("required_coverage");leaf_ids=body.get("leaf_ids")
            need(isinstance(required,list) and isinstance(leaf_ids,list) and len(required)==len(set(required)),"invalid_archive","Review packet coverage is malformed",record["id"])
            need(required==["item:"+value for value in leaf_ids]
                 or required==["scope:"+body.get("proposal")]
                 or (body.get("revision") and required==["empty_scope:"+body.get("revision")]),
                 "invalid_archive","Review packet coverage is not exact",record["id"])
            if body.get("revision") in revision_map:
                rev_items={row["id"] for row in items_by_revision[body["revision"]] if row.get("leaf")==1}
                need(set(leaf_ids)<=rev_items,"invalid_archive","Review packet contains a foreign leaf",record["id"])
            if body.get("closure_ref") is not None:
                check_b_ref(body["closure_ref"],record["id"])
            dependencies=body.get("dependency_refs",[])
            need(isinstance(dependencies,list),"invalid_archive","Review packet dependencies are malformed",record["id"])
            for dependency in dependencies: check_b_ref(dependency,record["id"])
            packet_subject_key = "subject_ref" if "subject_ref" in body else "root_subject"
            need(body.get("binding")==digest({key:body.get(key) for key in ("format",packet_subject_key,"proposal","proposal_digest","revision","revision_digest","population_digest","material_digest","role","packet_index","packet_count","leaf_ids","required_coverage","dependency_refs","closure_ref","stage") if key in body}),"invalid_archive","Review packet binding differs",record["id"])
    grouped={}
    for record in packet_rows:
        body=_archive_body(record);key=(body.get("proposal"),canonical(body.get("subject_ref", body.get("root_subject"))),body.get("role"),canonical(body.get("closure_ref")))
        grouped.setdefault(key,[]).append(body)
    expected_groups = {}
    for proposal_id, proposal_row in proposal_map.items():
        proposal_body = _archive_body(proposal_row)
        proposal_kind = proposal_body.get("kind")
        if proposal_kind in {"population", "code", "document"}:
            result = _archive_result(proposal_row)
            revision_id = proposal_body.get("revision")
            if revision_id is None and isinstance(result, dict):
                revision_id = result.get("revision")
            # Staging/failed and not-yet-extracted population proposals do
            # not create review packets.  Once a complete revision exists,
            # its immutable packet group is mandatory.
            if revision_id is None:
                continue
        subject_ref = {"table": "traceability_proposals", "id": proposal_id,
                       "digest": proposal_row.get("digest")}
        closure_ref = None
        if proposal_kind == "mapping" and proposal_body.get("closure_stage") is not None:
            closure_rows = [row for row in records
                            if row.get("proposal") == proposal_id
                            and row.get("kind") == "closure_proposed"]
            need(len(closure_rows) == 1,
                 "invalid_archive", "Closure proposal packet group is missing its closure record", proposal_id)
            closure_row = closure_rows[0]
            closure_ref = {"table": "traceability_records", "id": closure_row.get("id"),
                           "digest": closure_row.get("digest")}
            subject_ref = closure_ref
            role = "trace"
        elif proposal_kind == "decision":
            decision_id = proposal_body.get("decision_id")
            decision_row = next((row for row in decoded["traceability_decisions"]
                                 if row.get("id") == decision_id), None)
            need(decision_row is not None,
                 "invalid_archive", "Decision packet group lacks TDEC", proposal_id)
            decision_body = _archive_body(decision_row)
            role = "impact" if any(entry.get("handling") == "exclude"
                                    for entry in decision_body.get("decisions", [])) else "trace"
        elif proposal_kind in {"population", "code", "document", "mapping", "scope"}:
            role = "impact" if proposal_kind == "scope" else "trace"
        else:
            need(False, "invalid_archive", "Review packet proposal kind is unsupported", proposal_id)
        key = (proposal_id, canonical(subject_ref), role, canonical(closure_ref))
        expected_groups[key] = proposal_id
    for key, proposal_id in expected_groups.items():
        need(key in grouped, "invalid_archive", "Review packet group is missing", proposal_id)
    for key,packets in grouped.items():
        packets.sort(key=lambda value:value.get("packet_index",-1));need([value.get("packet_index") for value in packets]==list(range(len(packets))),"invalid_archive","Review packet indexes have gaps",key)
        need(len({value.get("packet_count") for value in packets})==1 and packets[0].get("packet_count")==len(packets),"invalid_archive","Review packet count differs",key)
        # A packet's local binding proves that its JSON was not changed, but
        # it does not prove that the immutable packet set still covers the
        # proposal's complete population.  Derive the expected coverage from
        # the archived TPROP/TDEC/TMAP/TREC rows and compare every fixed
        # 500-marker slice.  This catches a missing or duplicated leaf even
        # when a caller recomputes the packet, row, payload, and ZIP hashes.
        first=packets[0]
        proposal_id=first.get("proposal")
        proposal_row=proposal_map.get(proposal_id)
        need(proposal_row is not None,"invalid_archive","Review packet proposal is missing",proposal_id)
        proposal_body=_archive_body(proposal_row)
        proposal_kind=proposal_body.get("kind")
        expected_role=None
        expected_subject=None
        expected_coverage=None
        if proposal_kind in {"population", "code", "document"}:
            expected_role="trace"
            expected_subject={"table":"traceability_proposals","id":proposal_id,"digest":proposal_row.get("digest")}
            revision_id=proposal_body.get("revision")
            if revision_id is None:
                result=_archive_result(proposal_row)
                revision_id=result.get("revision") if isinstance(result,dict) else None
            revision_items=items_by_revision.get(revision_id, [])
            leaves=sorted((row for row in revision_items if row.get("leaf")==1),
                          key=lambda row:(row.get("ordinal",-1),row.get("id","")))
            expected_coverage=["item:"+row["id"] for row in leaves]
            if not expected_coverage:
                expected_coverage=["empty_scope:"+revision_id] if revision_id else []
        elif proposal_kind == "decision":
            expected_subject={"table":"traceability_proposals","id":proposal_id,"digest":proposal_row.get("digest")}
            decision_id=proposal_body.get("decision_id")
            decision_row=next((row for row in decoded["traceability_decisions"] if row.get("id")==decision_id),None)
            need(decision_row is not None,"invalid_archive","Decision review packet lacks TDEC",proposal_id)
            decision_body=_archive_body(decision_row)
            expected_coverage=["item:"+value for value in decision_body.get("required_leaf_ids",[])]
            expected_role="impact" if any(entry.get("handling")=="exclude" for entry in decision_body.get("decisions",[])) else "trace"
        elif proposal_kind == "mapping" and proposal_body.get("closure_stage") is None:
            expected_subject={"table":"traceability_proposals","id":proposal_id,"digest":proposal_row.get("digest")}
            mapping_id=proposal_body.get("mapping_id")
            mapping_row=next((row for row in decoded["traceability_mappings"] if row.get("id")==mapping_id),None)
            need(mapping_row is not None,"invalid_archive","Mapping review packet lacks TMAP",proposal_id)
            mapping_body=_archive_body(mapping_row)
            expected_coverage=["item:"+value for value in mapping_body.get("required_leaf_ids",[])]
            expected_role="trace"
        elif proposal_kind == "scope":
            expected_subject={"table":"traceability_proposals","id":proposal_id,"digest":proposal_row.get("digest")}
            expected_coverage=["scope:"+proposal_id]
            expected_role="impact"
        elif proposal_kind == "mapping" and proposal_body.get("closure_stage") is not None:
            expected_role="trace"
            closure_id=first.get("closure_ref",{}).get("id") if isinstance(first.get("closure_ref"),dict) else None
            closure_row=record_map.get(closure_id)
            need(closure_row is not None and closure_row.get("kind")=="closure_proposed",
                 "invalid_archive","Closure review packet lacks its closure proposal",proposal_id)
            expected_subject={"table":"traceability_records","id":closure_id,"digest":closure_row.get("digest")}
            closure_body=_archive_body(closure_row)
            need(closure_body.get("proposal")==proposal_id
                 and closure_body.get("stage")==proposal_body.get("closure_stage"),
                 "invalid_archive","Closure review packet is bound to another closure proposal",proposal_id)
            expected_coverage=["item:"+value for value in closure_body.get("required_leaf_ids",[])]
        else:
            need(False,"invalid_archive","Review packet proposal kind is unsupported",proposal_id)
        for packet_index,packet in enumerate(packets):
            need(packet.get("role")==expected_role and packet.get("subject_ref")==expected_subject,
                 "invalid_archive","Review packet subject or role differs from its proposal",proposal_id)
            need(packet.get("closure_ref") is not None if proposal_body.get("closure_stage") is not None else packet.get("closure_ref") is None,
                 "invalid_archive","Review packet closure binding differs from its proposal",proposal_id)
            expected_slice=expected_coverage[packet_index*MAX_PAGE:(packet_index+1)*MAX_PAGE]
            need(packet.get("required_coverage")==expected_slice
                 and packet.get("leaf_ids")==[value[5:] for value in expected_slice if value.startswith("item:")],
                 "invalid_archive","Review packet coverage omits or reassigns immutable population",proposal_id)
        expected_count=max(1,(len(expected_coverage)+MAX_PAGE-1)//MAX_PAGE)
        need(len(packets)==expected_count,"invalid_archive","Review packet set count differs from immutable population",proposal_id)
    for record in records:
        body=_archive_body(record)
        if record.get("kind") in {"decision_adopted","mapping_adopted","binding_adopted","population_adopted","closure_proposed","closure_adopted","delivered_mapping","withdrawn"}:
            subject_row=check_b_ref(body.get("subject_ref"),record["id"])
            need(body.get("proposal") in proposals,"invalid_archive","Adoption record lacks TPROP FK",record["id"])
            subject_table=body["subject_ref"].get("table")
            if record.get("kind") == "decision_adopted":
                need(subject_table == "traceability_decisions" and body.get("decision_id") == body["subject_ref"].get("id")
                     and subject_row.get("project") == project,
                     "invalid_archive", "Decision adoption subject differs", record["id"])
            elif record.get("kind") == "mapping_adopted":
                need(subject_table == "traceability_mappings" and body.get("mapping_id") == body["subject_ref"].get("id")
                     and subject_row.get("project") == project,
                     "invalid_archive", "Mapping adoption subject differs", record["id"])
            elif record.get("kind") == "binding_adopted":
                need(subject_table == "traceability_bindings" and body.get("binding_id") == body["subject_ref"].get("id")
                     and subject_row.get("project") == project,
                     "invalid_archive", "Binding adoption subject differs", record["id"])
            elif record.get("kind") == "population_adopted":
                need(subject_table == "traceability_proposals" and body["subject_ref"].get("id") == body.get("proposal"),
                     "invalid_archive", "Population adoption subject differs", record["id"])
            elif record.get("kind") == "delivered_mapping":
                need(subject_table == "traceability_mappings"
                     and body.get("mapping_id") == body["subject_ref"].get("id")
                     and body.get("delivery") and isinstance(body.get("delivery_digest"), str)
                     and body.get("snapshot_digest") is not None
                     and isinstance(body.get("commit_refs"), dict)
                     and isinstance(body.get("actual"), list) and body["actual"],
                     "invalid_archive", "Delivered mapping observation is malformed", record["id"])
                mapping_row=subject_row
                mapping_body=_archive_body(mapping_row)
                need(body.get("proposal") == mapping_body.get("proposal")
                     and body.get("revision") == mapping_row.get("revision"),
                     "invalid_archive", "Delivered mapping correspondence differs", record["id"])
                commit_refs=body["commit_refs"]
                for repo, info in commit_refs.items():
                    need(isinstance(repo, str) and isinstance(info, dict)
                         and isinstance(info.get("commit"), str) and _OID.fullmatch(info["commit"])
                         and isinstance(info.get("tree"), str) and _OID.fullmatch(info["tree"]),
                         "invalid_archive", "Delivered commit reference is malformed", record["id"])
                for actual in body["actual"]:
                    need(isinstance(actual, dict) and isinstance(actual.get("leaf_ids"), list)
                         and isinstance(actual.get("target_ref"), dict)
                         and isinstance(actual.get("destination"), dict),
                         "invalid_archive", "Delivered target observation is malformed", record["id"])
                    check_typed_ref(actual["target_ref"], record["id"])
                    destination=actual["destination"]
                    need(destination.get("ref_type") in {"git_file", "git_symbol", "candidate_symbol", "source_span", "artifact_ac"},
                         "invalid_archive", "Delivered destination reference kind is malformed", record["id"])
                    if destination.get("ref_type") in {"git_file", "git_symbol", "candidate_symbol"}:
                        need(isinstance(destination.get("repository"), str)
                             and isinstance(destination.get("commit"), str) and _OID.fullmatch(destination["commit"])
                             and isinstance(destination.get("tree"), str) and _OID.fullmatch(destination["tree"])
                             and isinstance(destination.get("path"), str)
                             and isinstance(destination.get("blob_oid"), str) and _HEX.fullmatch(destination["blob_oid"])
                             and isinstance(destination.get("sha256"), str) and _HEX64.fullmatch(destination["sha256"]),
                             "invalid_archive", "Delivered Git destination identity is malformed", record["id"])
                        _archive_blob(blob_reader, destination["sha256"])
                        target = actual["target_ref"]
                        need(target.get("ref_type") == destination.get("ref_type")
                             and target.get("repository") == destination.get("repository")
                             and target.get("path") == destination.get("path")
                             and target.get("sha256") == destination.get("sha256")
                             and target.get("mode") == destination.get("mode"),
                             "invalid_archive", "Delivered destination differs from its typed target", record["id"])
                        if destination.get("ref_type") in {"git_symbol", "candidate_symbol"}:
                            symbol = destination.get("symbol")
                            need(isinstance(symbol, dict)
                                 and symbol.get("qualified_name") == target.get("qualified_name")
                                 and symbol.get("kind") == target.get("kind")
                                 and symbol.get("ordinal") == target.get("ordinal")
                                 and symbol.get("byte_start") == target.get("start_byte")
                                 and symbol.get("byte_end") == target.get("end_byte")
                                 and symbol.get("signature_hash") == target.get("signature_hash"),
                                 "invalid_archive", "Delivered symbol identity differs from its typed target", record["id"])
                    elif destination.get("ref_type") in {"source_span", "artifact_ac"}:
                        check_typed_ref(destination.get("typed_ref"), record["id"])
                        need(destination.get("typed_ref") == actual["target_ref"],
                             "invalid_archive", "Delivered document destination differs from its typed target", record["id"])
                material={"mapping_id":body.get("mapping_id"),"mapping_digest":mapping_row.get("digest"),
                          "delivery":body.get("delivery"),"delivery_digest":body.get("delivery_digest"),
                          "snapshot_digest":body.get("snapshot_digest"),"commit_refs":commit_refs,"actual":body.get("actual")}
                need(body.get("material_digest") == digest(material), "invalid_archive", "Delivered mapping material digest differs", record["id"])
        if record.get("kind") in {"closure_proposed","closure_adopted"}:
            material=body.get("material")
            if isinstance(material,dict): need(digest(material)==body.get("material_digest"),"invalid_archive","Closure material digest differs",record["id"])
            if record.get("kind") == "closure_adopted":
                need(body.get("closure_ref") == body.get("subject_ref"),
                     "invalid_archive", "Closure adoption subject differs", record["id"])
    for revision in decoded["traceability_revisions"]:
        if revision.get("status") in {"ready", "active", "superseded"}:
            links = [record for record in records if record.get("revision") == revision.get("id") and record.get("kind") == "extracted"]
            need(len(links) == 1 and links[0].get("proposal") in proposals,
                 "invalid_archive", "Complete revision proposal correspondence is incomplete", revision.get("id"))
            _archive_validate_complete_revision(revision, items_by_revision[revision["id"]],
                                                 proposal_map.get(links[0]["proposal"]), records, blob_reader)
        else:
            # Staging/failed rows are retained operational evidence.  Their
            # item rows, if present, remain structural only and can never be
            # accepted as a complete population by this validator.
            for row in items_by_revision[revision["id"]]:
                _archive_item_range(row, _archive_body(row))


def inspect_archive(path, expected_sha256=None):
    payload, blobs, manifest = _read_archive(path, expected_sha256)
    tables = payload.get("tables", {})
    need(set(tables) >= {"traceability_sets", "traceability_revisions", "traceability_items", "traceability_proposals", "traceability_decisions", "traceability_mappings", "traceability_bindings", "traceability_records"}, "invalid_archive", "Traceability archive omits history tables")
    return {"verified": True, "format": payload["format"], "history_version": payload["history_version"], "schema": payload["schema"],
            "project": payload["project"], "counts": {table: len(rows) for table, rows in tables.items()}, "blobs": len(blobs),
            "runtime_restore_supported": False, "fresh_review_or_test_evidence": False, "historical_only": True}


def validate_archive(path, expected_sha256=None):
    return inspect_archive(path, expected_sha256)
