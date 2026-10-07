"""Immutable program-origin metadata and its read-only validation boundary."""
from __future__ import annotations

from typing import Any, Iterable

from .common import Fault, canonical, digest, need, parse_json

FORMAT = "daikibo.program-origin.v1"
SCHEMA = 16
# The origin table was introduced in v16 and is unchanged by the additive v17
# decision-batch table. Accept both explicit layouts, but never infer support
# from `>= 16`: a future schema must be reviewed here before it emits origins.
SUPPORTED_SCHEMA_VERSIONS = {16, 17}
POLICIES = {"legacy-preserved", "e3-required"}
ORIGINS = {"schema-migration", "program.begin"}
BODY_KEYS = {
    "format", "program", "project", "policy", "origin",
    "introduced_schema", "legacy_program_digest",
}
ORIGIN_COLUMNS = ("program", "project", "digest", "body")


def _connection_row(connection: Any, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    """Read one row from either sqlite3.Connection or the Store connection."""
    cursor = connection.execute(sql, args)
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row))


def _connection_rows(connection: Any, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, args)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def require_origin_schema(connection: Any) -> None:
    """Require the current schema's complete origin table before new output."""
    version = _connection_row(connection, "PRAGMA user_version")
    need(version is not None and version.get("user_version") in SUPPORTED_SCHEMA_VERSIONS,
         "origin_schema_invalid", "Program-origin output requires a supported schema 16 or 17", version)
    table = _connection_row(
        connection,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='program_origins'",
    )
    need(table is not None, "origin_schema_missing", "Schema 16 program-origin table is missing")
    columns = [row["name"] for row in _connection_rows(connection, "PRAGMA table_info(program_origins)")]
    need(tuple(columns) == ORIGIN_COLUMNS,
         "origin_schema_invalid", "Schema 16 program-origin table metadata differs", columns)


def validate_origin_database(connection: Any) -> dict[str, int]:
    """Validate the current database's complete one-to-one origin projection."""
    require_origin_schema(connection)
    programs = _connection_rows(connection, "SELECT * FROM programs ORDER BY id")
    origins = _connection_rows(connection, "SELECT * FROM program_origins ORDER BY program")
    projects = {row.get("project") for row in programs} | {row.get("project") for row in origins}
    for project in projects:
        validate_origin_rows(
            [row for row in programs if row.get("project") == project],
            [row for row in origins if row.get("project") == project],
            project,
            code="origin_integrity",
        )
    return {"programs": len(programs), "program_origins": len(origins)}


def validate_origin_store(store: Any) -> dict[str, int]:
    """Validate a Store using the same database validator as backup/GC."""
    return validate_origin_database(store.conn)


def legacy_program_digest(row: dict[str, Any]) -> str:
    """Commit to the complete pre-migration program row."""
    body = parse_json(row["body"])
    need(isinstance(body, dict), "invalid_origin", "Program body is not an object")
    return digest({
        "id": row["id"],
        "project": row["project"],
        "phase": row["phase"],
        "revision": row["revision"],
        "body": body,
        "created": row["created"],
    })


def migration_body(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "program": row["id"],
        "project": row["project"],
        "policy": "legacy-preserved",
        "origin": "schema-migration",
        "introduced_schema": SCHEMA,
        "legacy_program_digest": legacy_program_digest(row),
    }


def begin_body(program: str, project: str) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "program": program,
        "project": project,
        "policy": "e3-required",
        "origin": "program.begin",
        "introduced_schema": SCHEMA,
        "legacy_program_digest": None,
    }


def _decode_record(row: dict[str, Any], *, project: str, program: str) -> dict[str, Any]:
    need(type(project) is str and type(program) is str,
         "origin_invalid", "Program origin identity types differ", program)
    need(row.get("program") == program and row.get("project") == project,
         "origin_cross_project", "Program origin belongs to another program or project", program)
    raw = row.get("body")
    body = parse_json(raw) if isinstance(raw, str) else raw
    need(isinstance(body, dict) and set(body) == BODY_KEYS,
         "origin_invalid", "Program origin body shape differs", program)
    need(body.get("format") == FORMAT and type(body.get("program")) is str and
         body.get("program") == program and type(body.get("project")) is str and
         body.get("project") == project and type(body.get("introduced_schema")) is int and
         not isinstance(body.get("introduced_schema"), bool) and
         body.get("introduced_schema") == SCHEMA,
         "origin_invalid", "Program origin identity differs", program)
    policy = body.get("policy")
    origin = body.get("origin")
    legacy = body.get("legacy_program_digest")
    need(type(policy) is str and type(origin) is str,
         "origin_invalid", "Program origin policy and origin must be strings", program)
    need((policy, origin) in {
        ("legacy-preserved", "schema-migration"),
        ("e3-required", "program.begin"),
    }, "origin_invalid", "Program origin policy/origin combination is unknown", program)
    if policy == "legacy-preserved":
        need(isinstance(legacy, str) and len(legacy) == 64 and
             all(char in "0123456789abcdef" for char in legacy),
             "origin_invalid", "Legacy program origin digest is malformed", program)
    else:
        need(legacy is None, "origin_invalid", "New program origin carries a legacy digest", program)
    need(row.get("digest") == digest(body), "origin_invalid", "Program origin digest differs", program)
    return {
        "program": program,
        "project": project,
        "policy": policy,
        "origin": origin,
        "introduced_schema": SCHEMA,
        "legacy_program_digest": legacy,
        "digest": row["digest"],
        "body": body,
    }


def resolve_program_origin(store: Any, *, project: str, program: str) -> dict[str, Any]:
    """Read and validate one immutable origin record without making a gate decision."""
    require_origin_schema(store.conn)
    program_row = store.one("SELECT * FROM programs WHERE id=? AND project=?", (program, project))
    need(program_row is not None, "not_found", "Program does not belong to this project", program)
    row = store.one("SELECT * FROM program_origins WHERE program=?", (program,))
    need(row is not None, "origin_missing", "Program origin is missing", program)
    return _decode_record(row, project=project, program=program)


def validate_origin_rows(programs: Iterable[dict[str, Any]], origins: Iterable[dict[str, Any]],
                         project: str, *, code: str = "invalid_snapshot") -> dict[str, Any]:
    """Validate an exported one-to-one program/origin history projection."""
    program_rows = list(programs)
    origin_rows = list(origins)
    program_map = {row.get("id"): row for row in program_rows}
    need(len(program_map) == len(program_rows), code, "Duplicate program in origin history")
    need(all(row.get("project") == project for row in program_rows), code,
         "Program history crosses project boundary")
    origin_map = {row.get("program"): row for row in origin_rows}
    need(len(origin_map) == len(origin_rows), code, "Duplicate program origin")
    need(set(origin_map) == set(program_map), code,
         "Program origin history is not one-to-one")
    for program, row in origin_map.items():
        try:
            _decode_record(row, project=project, program=program)
        except Fault as exc:
            raise Fault(code, exc.message, exc.details) from exc
    return {"programs": len(program_rows), "program_origins": len(origin_rows)}


def insert_origin(store: Any, body: dict[str, Any]) -> None:
    """Insert one immutable origin row; caller owns the surrounding transaction."""
    need(set(body) == BODY_KEYS and body.get("format") == FORMAT,
         "origin_invalid", "Program origin body shape differs")
    row = {
        "program": body.get("program"),
        "project": body.get("project"),
        "digest": digest(body),
        "body": canonical(body).decode(),
    }
    # Validate the complete private writer payload before it reaches the
    # immutable table.  The FK below still verifies that the program exists;
    # this check keeps unknown policy/origin combinations out of the table.
    _decode_record(row, project=body.get("project"), program=body.get("program"))
    store.execute(
        "INSERT INTO program_origins(program,project,digest,body) VALUES(?,?,?,?)",
        (row["program"], row["project"], row["digest"], row["body"]),
    )


__all__ = [
    "FORMAT", "SCHEMA", "BODY_KEYS", "legacy_program_digest", "migration_body",
    "begin_body", "require_origin_schema", "validate_origin_database", "validate_origin_store",
    "resolve_program_origin", "validate_origin_rows", "insert_origin",
]
