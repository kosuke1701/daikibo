"""Legacy Task attempt telemetry remains visible without fabricated ledger rows."""
from __future__ import annotations

import zipfile

from conftest import make_task
from daikibo.common import parse_json
from daikibo.knowledge_history import inspect_archive, validate_specifications


def test_legacy_attempt_telemetry_selects_execution_history_for_direct_and_chunked_exports(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    full.s.execute("UPDATE tasks SET attempts=3 WHERE id=?", (task,))

    assert full.s.one("SELECT count(*) AS n FROM execution_attempts WHERE project=?", (project,))["n"] == 0
    assert full.s.one("SELECT count(*) AS n FROM attempt_assessments WHERE project=?", (project,))["n"] == 0

    direct = full.k.export(full.owner, project)
    # Task planning material now selects the additive v6 direct format so the
    # retained assurance rows have their finite observed context/CAS closure.
    assert direct["format"] == "daikibo.spec.v6"
    history = direct["execution_control_history"]
    assert [row["attempts"] for row in history["tasks"] if row["id"] == task] == [3]
    assert all(not history[table] for table in (
        "execution_attempts", "attempt_assessments", "execution_control_proposals",
        "execution_control_packets", "execution_control_events", "execution_control_authorizations"))
    direct_counts = validate_specifications(direct)
    assert direct_counts["execution_attempts"] == 0
    assert direct_counts["legacy_unknown_attempts"] == 3

    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    archive = full.history.export_archive(full.owner, baseline["id"])
    report = inspect_archive(archive["path"], archive["sha256"])
    assert report["format"] == "daikibo.knowledge-archive.v12"
    assert report["counts"]["programs"] == 0
    assert report["counts"]["program_origins"] == 0
    assert report["counts"]["tasks"] == 1
    assert report["counts"]["execution_attempts"] == 0
    assert report["counts"]["attempt_assessments"] == 0
    with zipfile.ZipFile(archive["path"]) as exported:
        payload = parse_json(exported.read("snapshot.json"))
        stream = b"".join(exported.read("objects/" + part["sha256"])
                           for part in payload["records"]["chunks"])
    task_record = next(parse_json(line)["row"] for line in stream.splitlines()
                       if parse_json(line)["section"] == "tasks")
    assert task_record["id"] == task and task_record["attempts"] == 3
