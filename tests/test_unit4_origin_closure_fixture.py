"""Read-only assertions for the real old-source closure/history fixture."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from portable_origin_fixture import FIXTURES, fixture_manifest, materialize_fixture


def _rows(connection, sql):
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(sql)]


def test_old_public_flow_fixture_contains_real_history_and_closed_program():
    manifest = fixture_manifest("closure")
    assert manifest["source_commit"] == "ac013df0653abe312e1e49f75a62bd9a64ef4518"
    assert manifest["schema"] == 15
    assert len(manifest["closures"]) == 1
    assert manifest["closed"]["closure"] == manifest["closures"][0]["id"]
    closed = next(row for row in manifest["programs"] if row["id"] == manifest["closed"]["program"])
    closed_history = json.loads(closed["body"])["history"]
    assert closed["phase"] == "delivery"
    assert [entry["phase"] for entry in closed_history] == [
        "requirements", "scenarios", "boundaries", "contracts", "feasibility",
        "design", "plan", "implementation", "integration",
    ]
    assert len(closed_history) == 9
    history = next(row for row in manifest["programs"] if row["id"] == manifest["history_program"])
    assert len(json.loads(history["body"])["history"]) == 6

    # The loader rebuilds a temporary runtime home from SQL/CAS.  The test
    # never reads the author's ops tree or a runtime SQLite file from source.
    with tempfile.TemporaryDirectory(prefix="u4o-closure-fixture-") as temporary:
        home = materialize_fixture("closure", Path(temporary) / "home")
        connection = sqlite3.connect(home / "state.sqlite3")
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 15
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='program_origins'"
            ).fetchone() is None
            assert _rows(connection, "SELECT * FROM program_closures ORDER BY id") == manifest["closures"]
            assert _rows(connection, "SELECT id,project,phase,revision,body,created FROM programs ORDER BY id") == manifest["programs"]
            assert _rows(connection, "SELECT * FROM task_revision_history ORDER BY id") == manifest["task_revision_history"]
        finally:
            connection.close()


def test_portable_cas_path_cannot_escape_fixture_or_destination(tmp_path, monkeypatch):
    source = tmp_path / "source" / "fixture"
    shutil.copytree(FIXTURES["legacy"], source)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    original = source / manifest["portable"]["cas"][0]["path"]
    (source.parent / "escape").write_bytes(original.read_bytes())
    manifest["portable"]["cas"][0]["path"] = "cas/../../escape"
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setitem(FIXTURES, "path_boundary", source)

    destination = tmp_path / "target" / "home"
    with pytest.raises(ValueError, match="CAS member path"):
        materialize_fixture("path_boundary", destination)
    assert not (destination.parent / "escape").exists()
    assert not destination.exists()
