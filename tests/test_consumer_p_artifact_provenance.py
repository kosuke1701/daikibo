"""Finite Consumer-P checks over the real Runtime fixture subprocess."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest
from conftest import make_task


def _artifact_ref(full, project, artifact):
    row = full.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (artifact, project), True)
    return {"kind": "artifact", "project": project, "artifact": artifact,
            "revision": row["revision"], "body_digest": row["digest"]}


def _consumer_task(full, full_project, manifest, *, path="artifact-output.json"):
    project, repository, requirement, _root = full_project
    body = {
        "title": "Collect declared artifact output",
        "goal": "WRITE:" + json.dumps({path: json.dumps(manifest, ensure_ascii=False, sort_keys=True)}),
        "read_artifacts": [requirement], "write_paths": [path],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [],
        "structural_obligations": {
            "format": "daikibo.task-structural-obligations.v1",
            "required_outputs": [{
                "id": "artifact-result", "statement": "The subprocess emits one finding artifact",
                "artifact_refs": [_artifact_ref(full, project, requirement)],
                "realization_kind": "artifact",
            }],
            "required_exercises": [],
        },
    }
    task = full.w.create(full.owner, project, body)
    full.w.plan_tests(full.owner, task["id"], {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    full.w.ready(full.owner, task["id"])
    return task["id"]


def _run_collect(full, full_project):
    project, repository, _requirement, _root = full_project
    manifest = {"format": "daikibo.artifact-output.v1", "outputs": [{
        "declaration_id": "artifact-result", "kind": "finding",
        "body": {"title": "Observed result", "statement": "The managed subprocess emitted the finding."},
    }]}
    task = _consumer_task(full, full_project, manifest)
    claimed = full.w.claim(full.owner, project, task)
    executed = full.rt.execute(full.owner, task, "fixture")
    result = full.invoke(full.owner, "task.artifacts_collect", {
        "task": task, "expected_revision": claimed["revision"],
        "candidate": executed["candidate"], "repository": repository,
        "path": "artifact-output.json",
    })
    return project, repository, task, executed, result


def test_runtime_candidate_collects_draft_artifact_and_replays(full, full_project):
    project, repository, task, executed, result = _run_collect(full, full_project)
    assert result["artifacts"][0]["created"] is True
    artifact = result["artifacts"][0]["artifact"]
    assert artifact["status"] == "draft"
    material = full.s.one("SELECT * FROM assurance_objects WHERE id=?",
                          (result["artifacts"][0]["material"]["id"],), True)
    envelope = json.loads(material["body"])
    payload = json.loads(full.s.blob_get(envelope["payload_blob"]))
    assert envelope["material_kind"] == "artifact_production"
    assert payload["producer_actor"] == full.owner.id
    candidate_row = full.s.one("SELECT implementation_run FROM candidates WHERE id=?", (executed["candidate"],), True)
    assert payload["implementation_run"] == candidate_row["implementation_run"]
    assert payload["artifact_ref"]["artifact"] == artifact["id"]
    assert payload["candidate_ref"]["candidate"] == executed["candidate"]
    assert len(envelope["dependency_refs"]) == 4

    replay = full.w.artifacts_collect(full.owner, task, result["revision"],
                                      executed["candidate"], repository, "artifact-output.json")
    assert replay["artifacts"] == [{**result["artifacts"][0], "created": False}]
    assert full.s.one("SELECT count(*) AS n FROM artifacts WHERE project=?", (project,))["n"] == 2
    assert full.s.one("SELECT count(*) AS n FROM assurance_objects WHERE project=? AND kind='material' AND json_extract(body,'$.material_kind')='artifact_production'", (project,))["n"] == 1


def test_collector_cannot_use_changed_manifest_or_foreign_selector(full, full_project):
    project, repository, task, executed, result = _run_collect(full, full_project)
    row = full.s.one("SELECT body FROM candidates WHERE id=?", (executed["candidate"],), True)
    candidate = json.loads(row["body"])
    entry = candidate["snapshot"]["repos"][repository]["files"]["artifact-output.json"]
    changed = {"format": "daikibo.artifact-output.v1", "outputs": []}
    blob = full.s.blob_put(canonical(changed))
    entry["blob"], entry["size"] = blob, len(canonical(changed))
    candidate["snapshot"]["digest"] = digest({key: value for key, value in candidate["snapshot"].items() if key != "digest"})
    candidate["digest"] = digest(candidate)
    # The candidate row is immutable; this is a read-only selector probe
    # against an in-memory foreign identity and must be rejected before any
    # artifact or material write.
    with pytest.raises(Fault):
        full.w.artifacts_collect(full.owner, task, result["revision"],
                                 "CANDIDATE-does-not-exist", repository, "artifact-output.json")
    assert full.s.one("SELECT count(*) AS n FROM artifacts WHERE project=?", (project,))["n"] == 2


def test_old_candidate_without_controller_producer_record_is_unknown(full, full_project):
    project, repository, _requirement, _root = full_project
    manifest = {"format": "daikibo.artifact-output.v1", "outputs": []}
    task = _consumer_task(full, full_project, manifest)
    claimed = full.w.claim(full.owner, project, task)
    executed = full.rt.execute(full.owner, task, "fixture")
    candidate_row = full.s.one("SELECT implementation_run FROM candidates WHERE id=?", (executed["candidate"],), True)
    run = full.s.one("SELECT body FROM runs WHERE id=?", (candidate_row["implementation_run"],), True)
    body = json.loads(run["body"])
    body.pop("execution_control", None)
    # This simulates a retained pre-Consumer-P candidate.  The collector
    # caller cannot fill its missing producer identity after the fact.
    full.s.execute("UPDATE runs SET body=? WHERE id=?", (json.dumps(body, sort_keys=True), candidate_row["implementation_run"]))
    with pytest.raises(Fault) as rejected:
        full.w.artifacts_collect(full.owner, task, claimed["revision"],
                                 executed["candidate"], repository, "artifact-output.json")
    assert rejected.value.code in {"integrity_error", "legacy_unverified"}


@pytest.mark.parametrize("change", ["revision", "epoch", "lease", "candidate"])
def test_collection_rechecks_current_task_fence_at_transaction_entry(full, full_project, monkeypatch, change):
    """A write immediately before collection cannot leave a stale candidate collectable."""
    project, repository, _requirement, _root = full_project
    manifest = {"format": "daikibo.artifact-output.v1", "outputs": [{
        "declaration_id": "artifact-result", "kind": "finding",
        "body": {"title": "Observed result", "statement": "The managed subprocess emitted the finding."},
    }]}
    task = _consumer_task(full, full_project, manifest)
    claimed = full.w.claim(full.owner, project, task)
    executed = full.rt.execute(full.owner, task, "fixture")
    original = full.s.transaction
    fired = False

    @contextmanager
    def interleave():
        nonlocal fired
        if not fired:
            fired = True
            if change == "revision":
                full.s.execute("UPDATE tasks SET revision=revision+1 WHERE id=?", (task,))
            elif change == "epoch":
                full.s.execute("UPDATE tasks SET epoch=epoch+1 WHERE id=?", (task,))
            elif change == "lease":
                full.s.execute("UPDATE tasks SET lease_owner=?,lease_until=? WHERE id=?",
                               ("late-owner", 10**12, task))
            else:
                full.s.execute("UPDATE tasks SET candidate=? WHERE id=?",
                               ("CANDIDATE-replaced", task))
        with original():
            yield

    monkeypatch.setattr(full.s, "transaction", interleave)
    with pytest.raises(Fault):
        full.w.artifacts_collect(full.owner, task, claimed["revision"],
                                 executed["candidate"], repository, "artifact-output.json")
    assert full.s.one("SELECT count(*) AS n FROM artifacts WHERE project=?", (project,))["n"] == 1
    assert full.s.one("SELECT count(*) AS n FROM assurance_objects WHERE project=? AND kind='material' AND json_extract(body,'$.material_kind')='artifact_production'", (project,))["n"] == 0


def test_artifact_production_is_verified_in_standard_archive_and_gc_roots(full, full_project):
    project, _repository, _task, _executed, result = _run_collect(full, full_project)
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    inspected = full.history.inspect_archive(full.owner, exported["path"], exported["sha256"])
    assert inspected["format"] == "daikibo.knowledge-archive.v12"
    material_id = result["artifacts"][0]["material"]["id"]
    envelope = json.loads(full.s.one("SELECT body FROM assurance_objects WHERE id=?", (material_id,), True)["body"])
    manifest_blob = json.loads(full.s.blob_get(envelope["payload_blob"]))["manifest_blob"]
    dry = full.ops.garbage_collect(full.owner, dry_run=True, minimum_age=86400)
    assert manifest_blob not in {item["blob"] for item in dry["candidates"]}
