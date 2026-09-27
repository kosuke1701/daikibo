"""Materialize the secret-free Unit4-O old-schema fixtures for tests.

The checked-in fixture is a SQL dump plus content-addressed leaves.  It is
deliberately not a runtime database or an operational backup.  Materializing
it into a caller-owned temporary directory makes the existing Store/Control
migration path observe the same logical old rows without changing production
database or distribution rules.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path
from pathlib import PurePosixPath


ROOT = Path(__file__).resolve().parent / "fixtures"
FIXTURES = {
    "legacy": ROOT / "unit4_origin_legacy",
    "closure": ROOT / "unit4_origin_closure",
}
_CAS_HEX = re.compile(r"^[0-9a-f]+$")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_digest(connection: sqlite3.Connection, table: str) -> str:
    rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        default=lambda value: {"__bytes__": value.hex()}
        if isinstance(value, bytes)
        else value,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fixture_manifest(name: str) -> dict:
    try:
        root = FIXTURES[name]
    except KeyError as exc:
        raise ValueError(f"unknown Unit4-O fixture: {name}") from exc
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _cas_member_path(member: dict) -> Path:
    """Return a canonical, digest-addressed relative CAS path.

    The manifest is data, so its path cannot be trusted merely because it
    starts with ``cas``.  Requiring the canonical two-level digest namespace
    rejects absolute paths, traversal, alternate spellings, and paths whose
    location disagrees with their declared content digest.
    """
    raw_path = member.get("path")
    declared_digest = member.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(declared_digest, str):
        raise ValueError("portable CAS member has invalid path or digest")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or relative.as_posix() != raw_path:
        raise ValueError("portable CAS member path is not canonical")
    parts = relative.parts
    if len(parts) != 3 or parts[0] != "cas":
        raise ValueError("portable CAS member path is outside the CAS namespace")
    prefix, leaf = parts[1:]
    if (
        not _CAS_HEX.fullmatch(prefix)
        or len(prefix) != 2
        or not _CAS_HEX.fullmatch(leaf)
        or len(leaf) != 62
        or not _CAS_HEX.fullmatch(declared_digest)
        or len(declared_digest) != 64
        or prefix + leaf != declared_digest
    ):
        raise ValueError("portable CAS member path does not match its digest")
    return Path("cas", prefix, leaf)


def _contained_path(root: Path, relative: Path, label: str) -> Path:
    """Resolve a path and require it to remain below the supplied root."""
    root = root.resolve(strict=False)
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"portable {label} path escapes its root") from exc
    return candidate


def _relative_member_path(raw_path: object, label: str) -> Path:
    """Parse a non-CAS manifest member as a canonical relative path."""
    if not isinstance(raw_path, str):
        raise ValueError(f"portable {label} member path is not text")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw_path
        or not relative.parts
        or any(part in {".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"portable {label} member path is not canonical")
    return Path(*relative.parts)


def materialize_fixture(name: str, destination: Path) -> Path:
    """Restore one portable fixture into a new temporary runtime home.

    The destination must not already exist.  Every SQL/CAS member is checked
    against the manifest before it is used, and the logical table row counts
    and digests are checked after SQL restoration.  Credential-bearing tables
    are expected to be empty; no key or identity files are copied.
    """
    try:
        source = FIXTURES[name]
    except KeyError as exc:
        raise ValueError(f"unknown Unit4-O fixture: {name}") from exc
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    manifest = fixture_manifest(name)
    portable = manifest["portable"]
    source_root = source.resolve(strict=False)
    sql_relative = _relative_member_path(portable["state_sql"].get("path"), "SQL")
    sql_source = _contained_path(source_root, sql_relative, "fixture")
    if sql_source.stat().st_size != portable["state_sql"]["bytes"]:
        raise ValueError("portable SQL byte count differs from manifest")
    if _sha256(sql_source) != portable["state_sql"]["sha256"]:
        raise ValueError("portable SQL digest differs from manifest")

    # Validate every source and destination CAS location before creating the
    # caller-owned home or copying any material.  This keeps a malformed
    # member from causing a partial restore or an outside write.
    cas_locations = []
    destination_root = destination.resolve(strict=False)
    for member in portable["cas"]:
        relative = _cas_member_path(member)
        source_member = _contained_path(source_root, relative, "fixture")
        target_member = _contained_path(
            destination_root, Path("blobs", *relative.parts[1:]), "destination"
        )
        cas_locations.append((member, source_member, target_member))

    destination.mkdir(parents=True)

    sql_target = destination / "state.sql"
    shutil.copyfile(sql_source, sql_target)
    state = destination / "state.sqlite3"
    with sqlite3.connect(state) as connection:
        connection.executescript(sql_source.read_text(encoding="utf-8"))
        schema_version = int(portable["schema_user_version"])
        if schema_version != 15:
            raise ValueError("unexpected portable fixture schema")
        connection.execute(f"PRAGMA user_version = {schema_version}")
        connection.commit()
        if connection.execute("PRAGMA user_version").fetchone()[0] != 15:
            raise ValueError("portable fixture did not restore schema 15")

        expected_counts = portable["source_state"]["row_counts"]
        expected_digests = portable["source_state"]["row_digests"]
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if actual_tables != set(expected_counts):
            raise ValueError("portable fixture table set differs from manifest")
        for table, count in expected_counts.items():
            actual_count = connection.execute(
                f'SELECT count(*) FROM "{table}"'
            ).fetchone()[0]
            if actual_count != count or _row_digest(connection, table) != expected_digests[table]:
                raise ValueError(f"portable fixture rows differ for {table}")
        if any(portable["credential_tables"].get(table, 0) != 0 for table in ("tokens", "providers")):
            raise ValueError("portable fixture contains credential rows")

    for member, source_member, target_member in cas_locations:
        if source_member.stat().st_size != member["bytes"] or _sha256(source_member) != member["sha256"]:
            raise ValueError(f"portable CAS digest differs for {member['path']}")
        target_member.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_member, target_member)
    return destination
