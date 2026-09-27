from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import pytest

from conftest import finish_task, make_task
from daikibo.common import Fault, digest, parse_json
from daikibo.traceability_refs import PYTHON_AST_V1_DIGEST


def _candidate_ref(full, project, task, *, revision=None, project_override=None,
                   snapshot_override=None):
    task_row = full.s.one("SELECT * FROM tasks WHERE id=?", (task,), True)
    candidate_row = full.s.one("SELECT * FROM candidates WHERE id=?", (task_row["candidate"],), True)
    candidate_body = parse_json(candidate_row["body"])
    snapshot = candidate_body["snapshot"]
    return {
        "kind": "candidate",
        "project": project_override or project,
        "candidate": candidate_row["id"],
        "task": task,
        "task_revision": task_row["revision"] if revision is None else revision,
        "candidate_digest": candidate_row["digest"],
        "snapshot_digest": snapshot_override or snapshot["digest"],
    }


def test_candidate_assurance_uses_shared_identity_and_closure(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    finish_task(full, project, task)
    ref = _candidate_ref(full, project, task)

    resolved = full.assurance.resolve_pinned(full.owner, ref)
    resolution = resolved["resolution"]
    assert resolution["mode"] == "candidate"
    assert resolution["candidate_identity"]["canonical_ref"] == ref
    assert resolution["content"]["task_definition_digest"]
    dependency_kinds = {item["kind"] for item in resolution["dependencies"]}
    assert {"task", "candidate", "implementation_run", "implementation_receipt",
            "snapshot", "repository", "cas"} <= dependency_kinds
    assert resolved["current"]["state"] == "not_evaluated"

    current = full.assurance.evaluate_current(full.owner, ref)
    assert current["current"]["state"] == "current"
    assert current["resolution"]["current"] is True


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("task_revision", 999, "stale_reference"),
        ("snapshot_digest", "0" * 64, "integrity_error"),
    ],
)
def test_candidate_assurance_rejects_wrong_identity(full, full_project, field, value, code):
    project = full_project[0]
    task = make_task(full, full_project)
    finish_task(full, project, task)
    ref = {**_candidate_ref(full, project, task), field: value}
    with pytest.raises(Fault) as failure:
        full.assurance.resolve_pinned(full.owner, ref)
    assert failure.value.code == code


def test_candidate_assurance_rejects_foreign_project_identity(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    finish_task(full, project, task)
    foreign = full.k.create_project(full.owner, "candidate foreign project")["id"]
    ref = _candidate_ref(full, project, task, project_override=foreign)
    with pytest.raises(Fault) as failure:
        full.assurance.resolve_pinned(full.owner, ref)
    assert failure.value.code == "cross_project"


def test_candidate_assurance_retains_history_after_replan(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    finish_task(full, project, task)
    before = full.w.task(full.owner, task)
    ref = _candidate_ref(full, project, task, revision=before["revision"])

    after = full.w.replan(full.owner, task, before["revision"], "retain completed candidate history")
    assert after["revision"] == before["revision"] + 1
    assert after["candidate"] is None

    historical = full.assurance.resolve_pinned(full.owner, ref)
    assert historical["resolution"]["current"] is False
    assert historical["resolution"]["candidate_identity"]["task_revision"] == before["revision"]

    current = full.assurance.evaluate_current(full.owner, ref)
    assert current["current"]["state"] == "stale"
    with pytest.raises(Fault) as stale:
        full.assurance._resolve_locator(full.owner, {**ref, "identity_digest": digest(ref)}, current=True)
    assert stale.value.code == "stale_reference"


def test_candidate_assurance_uses_historical_definition_after_repo_addition(
    full, full_project, tmp_path,
):
    project = full_project[0]
    task = make_task(full, full_project)
    finish_task(full, project, task)
    before = full.w.task(full.owner, task)
    ref = _candidate_ref(full, project, task, revision=before["revision"])

    second_root = tmp_path / "second-repo"
    second_root.mkdir()
    (second_root / "README.txt").write_text("additional repository\n", encoding="utf-8")
    second_repository = full.sn.register(full.owner, project, "second", str(second_root))["id"]
    revised_body = {key: value for key, value in before["body"].items() if key != "task_kind"}
    revised_body.update({"repos": [*before["body"]["repos"], second_repository],
                        "title": "Retain candidate while adding a repository"}
    )
    reviewer_script = Path(tmp_path) / "history_revision_reviewer.py"
    reviewer_script.write_text(
        "import json,sys\n"
        "payload=json.load(sys.stdin)\n"
        "print(json.dumps({'verdict':'pass','rationale':'bounded history fixture',"
        "'covered':payload['context']['required_coverage'],'findings':[],"
        "'observations':[{'ref':payload['subject'],'detail':'history fixture'}],"
        "'dispositions':[]}))\n",
        encoding="utf-8",
    )
    full.rt.adapters.register(
        full.owner, "history-revision", "fixture", sys.executable,
        [str(reviewer_script)],
    )
    proposal = full.task_revisions.propose(
        full.owner, task, before["revision"], revised_body,
        "add a repository after retaining the candidate",
    )
    review = full.rt.review(full.owner, proposal["id"], "impact", "history-revision")
    applied = full.task_revisions.apply(full.owner, proposal["id"], proposal["digest"], review["receipt"])
    assert applied["revision"] == before["revision"] + 1

    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["resolution"]["current"] is False
    repository_ids = {
        item["id"] for item in resolved["resolution"]["dependencies"]
        if item["kind"] == "repository"
    }
    assert repository_ids == set(before["body"]["repos"])


def test_candidate_assurance_contains_resolves_generic_parent(full, full_project):
    project = full_project[0]
    task = make_task(full, full_project)
    finish_task(full, project, task)
    ref = _candidate_ref(full, project, task)
    candidate_row = full.s.one("SELECT * FROM candidates WHERE id=?", (ref["candidate"],), True)
    snapshot = parse_json(candidate_row["body"])["snapshot"]
    repository, repo = next(iter(snapshot["repos"].items()))
    entry = repo["files"]["calc.py"]
    raw = full.s.blob_get(entry["blob"])
    function = next(node for node in ast.parse(raw, type_comments=True).body
                    if isinstance(node, ast.FunctionDef))
    symbol = {
        "kind": "traceability_ref",
        "project": project,
        "locator": {
            "ref_type": "candidate_symbol", "candidate": ref["candidate"],
            "task": task, "task_revision": ref["task_revision"],
            "candidate_digest": ref["candidate_digest"],
            "snapshot_digest": ref["snapshot_digest"], "repository": repository,
            "path": "calc.py", "sha256": entry["blob"], "mode": entry["mode"],
            "adapter": "python-ast-v1", "adapter_digest": PYTHON_AST_V1_DIGEST,
            "qualified_name": function.name, "kind": "function", "ordinal": 0,
            "start_byte": 0, "end_byte": len(raw.rstrip(b"\n")),
            "span_sha256": digest(raw.rstrip(b"\n")),
            "signature_hash": digest(ast.dump(function, include_attributes=False)),
        },
    }
    result = full.assurance.contains(full.owner, project, ref, symbol)
    assert result["contains"] is True
    assert result["state"] == "verified"


@pytest.mark.parametrize("mutation", ["run", "receipt", "cas"])
def test_candidate_assurance_rejects_provenance_and_cas_mutation(
    full, full_project, mutation,
):
    project = full_project[0]
    task = make_task(full, full_project)
    finish_task(full, project, task)
    ref = _candidate_ref(full, project, task)
    original = full.assurance._candidate_context

    class MutatedContext:
        def row(self, kind, ident):
            value = original.row(kind, ident)
            if mutation == "run" and kind == "run":
                value = copy.deepcopy(value)
                value["body"]["argv"] = ["tampered"]
            return value

        def task_history(self, ident):
            return original.task_history(ident)

        def blob(self, sha256):
            if mutation == "cas":
                return b"tampered CAS"
            return original.blob(sha256)

        def receipt_body(self, ident):
            value = original.receipt_body(ident)
            if mutation == "receipt":
                value = copy.deepcopy(value)
                value["result"] = {"tampered": True}
            return value

    full.assurance._candidate_context = MutatedContext()
    with pytest.raises(Fault) as failure:
        full.assurance.resolve_pinned(full.owner, ref)
    assert failure.value.code == "integrity_error"
