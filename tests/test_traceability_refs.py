"""Finite real-fixture checks for the Unit B typed reference resolver."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest
from daikibo.traceability_refs import PYTHON_AST_V1_DIGEST, TraceabilityRefResolver


def _commit(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "trace-ref-test",
        "GIT_AUTHOR_EMAIL": "trace-ref@test",
        "GIT_COMMITTER_NAME": "trace-ref-test",
        "GIT_COMMITTER_EMAIL": "trace-ref@test",
    }
    subprocess.run(
        ["git", "-c", "user.name=trace-ref-test", "-c", "user.email=trace-ref@test", "commit", "-qm", "fixture"],
        cwd=root,
        check=True,
        env=env,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _git_file_ref(control, project, revision, repository, path, *, sha256_override=None):
    revision_row = control.s.one("SELECT * FROM traceability_revisions WHERE id=?", (revision,), True)
    body = json.loads(revision_row["body"])
    item = next(entry for entry in body["inventory"] if entry["path"] == path)
    return {
        "ref_type": "git_file", "repository": repository,
        "object_format": body["scope"]["object_format"], "commit": body["scope"]["commit"],
        "path": path, "blob_oid": item["blob_oid"],
        "sha256": item["sha256"] if sha256_override is None else sha256_override,
        "mode": item["mode"], "pin_revision": revision,
        "pin_revision_digest": revision_row["digest"],
    }


def _db_and_blob_state(control):
    tables = [row["name"] for row in control.s.all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    rows = {}
    for table in tables:
        rows[table] = [tuple(row.values()) for row in control.s.all(f'SELECT * FROM "{table}" ORDER BY rowid')]
    blobs = []
    for path in sorted(control.s.blobs.rglob("*")):
        if path.is_file():
            blobs.append((str(path.relative_to(control.s.blobs)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return rows, blobs


def _candidate_symbol_ref(control, project, candidate_id, task_id):
    candidate = control.s.one("SELECT * FROM candidates WHERE id=?", (candidate_id,), True)
    body = json.loads(candidate["body"])
    snapshot = body["snapshot"]
    repository, repo = next(iter(snapshot["repos"].items()))
    path = "calc.py"
    entry = repo["files"][path]
    raw = control.s.blob_get(entry["blob"])
    tree = ast.parse(raw, filename=path, type_comments=True)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    start, end = 0, len(raw.rstrip(b"\n"))
    return {
        "ref_type": "candidate_symbol",
        "candidate": candidate_id,
        "task": task_id,
        "task_revision": control.s.one("SELECT revision FROM tasks WHERE id=?", (task_id,), True)["revision"],
        "candidate_digest": candidate["digest"],
        "snapshot_digest": snapshot["digest"],
        "repository": repository,
        "path": path,
        "sha256": entry["blob"],
        "mode": entry["mode"],
        "adapter": "python-ast-v1",
        "adapter_digest": PYTHON_AST_V1_DIGEST,
        "qualified_name": function.name,
        "kind": "function",
        "ordinal": 0,
        "start_byte": start,
        "end_byte": end,
        "span_sha256": digest(raw[start:end]),
        "signature_hash": digest(ast.dump(function, include_attributes=False)),
    }


@pytest.fixture
def refs_fixture(full, tmp_path):
    control = full
    project = control.k.create_project(control.owner, "typed reference fixture")["id"]
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("@decorator\ndef add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    commit = _commit(root)
    repository = control.sn.register(control.owner, project, "app", str(root))["id"]

    source = control.k.source(control.owner, project, "\ufeffA😀\r\nlast", "unicode fixture")
    artifact = control.k.propose(
        control.owner,
        project,
        "requirement",
        {"title": "Addition", "statement": "Returns a sum", "acceptance": ["AC-ADD"], "source_refs": [source["id"]]},
    )
    artifact = control.k.accept(control.owner, artifact["id"], 1)

    proposal = control.traceability.propose(
        control.owner,
        project,
        kind="code",
        scope={"repository": repository, "commit": commit},
    )
    extracted = control.traceability.extract(control.owner, proposal["id"])
    revision = extracted["revision"]
    revision_row = control.s.one("SELECT * FROM traceability_revisions WHERE id=?", (revision,), True)
    revision_body = json.loads(revision_row["body"])
    file_inventory = next(item for item in revision_body["inventory"] if item["path"] == "calc.py")
    symbol_item = next(
        json.loads(item["body"])
        for item in control.s.all("SELECT body FROM traceability_items WHERE revision=? AND item_kind='symbol'", (revision,))
        if json.loads(item["body"]).get("path") == "calc.py"
    )
    git_file = {
        "ref_type": "git_file", "repository": repository, "object_format": revision_body["scope"]["object_format"],
        "commit": commit, "path": "calc.py", "blob_oid": file_inventory["blob_oid"], "sha256": file_inventory["sha256"],
        "mode": file_inventory["mode"], "pin_revision": revision, "pin_revision_digest": revision_row["digest"],
    }
    git_symbol = {
        **git_file,
        "ref_type": "git_symbol", "adapter": "python-ast-v1",
        "adapter_digest": revision_body["adapter_contract"]["implementation_digest"],
        "qualified_name": symbol_item["qualified_name"], "kind": symbol_item["kind"], "ordinal": symbol_item["ordinal"],
        "start_byte": symbol_item["byte_start"], "end_byte": symbol_item["byte_end"],
        "span_sha256": symbol_item["source_span"]["span_hash"], "signature_hash": symbol_item["signature_hash"],
    }

    body = {
        "title": "Implement addition", "goal": "WRITE:" + json.dumps({"calc.py": "def add(a, b):\n    return a + b\n"}),
        "read_artifacts": [artifact["id"]], "write_paths": ["calc.py"], "acceptance": ["AC-ADD"],
        "dependencies": [], "repos": [repository], "non_goals": [],
    }
    task = control.w.create(control.owner, project, body)
    control.w.plan_tests(control.owner, task["id"], {"checks": [{
        "id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"], "kind": "pytest",
        "required_tests": ["test_add"],
    }]})
    control.w.ready(control.owner, task["id"])
    control.w.claim(control.owner, project, task["id"])
    control.rt.adapters.register(control.owner, "fixture", "fixture", os.sys.executable, [str(Path(__file__).with_name("fixture_agent.py"))])
    control.rt.execute(control.owner, task["id"], "fixture")
    task_row = control.s.one("SELECT * FROM tasks WHERE id=?", (task["id"],), True)
    candidate_ref = _candidate_symbol_ref(control, project, task_row["candidate"], task["id"])

    source_raw = control.s.blob_get(source["digest"])
    source_ref = {
        "ref_type": "source_span", "source_id": source["id"], "blob_digest": source["digest"],
        "byte_start": 0, "byte_end": len(source_raw), "unicode_start": 0,
        "unicode_end": len(source_raw.decode("utf-8")),
        "span_hash": digest(source_raw),
    }
    artifact_ref = {
        "ref_type": "artifact_ac", "artifact": artifact["id"], "revision": artifact["revision"],
        "body_digest": artifact["digest"], "ac_pointer": "/acceptance/0", "ac_digest": digest("AC-ADD"),
    }
    yield {
        "control": control, "project": project, "root": root, "repository": repository, "commit": commit,
        "revision": revision, "revision_row": revision_row, "proposal": proposal["id"],
        "git_file": git_file, "git_symbol": git_symbol,
        "source": source_ref, "artifact": artifact_ref, "candidate": candidate_ref, "task": task["id"],
    }


def test_resolves_all_five_real_refs_read_only_and_git_survives_source_deletion(refs_fixture):
    f = refs_fixture
    resolver = TraceabilityRefResolver(f["control"])
    before = _db_and_blob_state(f["control"])
    results = [resolver.resolve(f["control"].owner, f["project"], f["source"]),
               resolver.resolve(f["control"].owner, f["project"], f["artifact"]),
               resolver.resolve(f["control"].owner, f["project"], f["git_symbol"]),
               resolver.resolve(f["control"].owner, f["project"], f["candidate"]),
               resolver.resolve(f["control"].owner, f["project"], f["git_file"])]
    assert {item["ref_type"] for item in results} == {"git_file", "git_symbol", "candidate_symbol", "source_span", "artifact_ac"}
    assert all(item["format"] == "traceability.resolved-ref.v1" for item in results)
    assert all(item["evidence_claim"] == "structural_identity_only" for item in results)
    assert results[3]["current"] is True
    assert _db_and_blob_state(f["control"]) == before

    # The original repository is not an authority after the immutable pin was
    # created.  Removing it must leave the Git refs resolvable from CAS/TREC.
    shutil.rmtree(f["root"])
    resolver.resolve(f["control"].owner, f["project"], f["git_file"])
    resolver.resolve(f["control"].owner, f["project"], f["git_symbol"])


def test_git_pin_shape_and_cas_integrity_are_checked(refs_fixture):
    f = refs_fixture
    resolver = TraceabilityRefResolver(f["control"])
    f["control"].s.execute("UPDATE traceability_proposals SET status='adopted' WHERE id=?", (f["proposal"],))
    resolver.resolve(f["control"].owner, f["project"], f["git_file"])
    unpinned = {key: value for key, value in f["git_file"].items()
                if key not in {"pin_revision", "pin_revision_digest"}}
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], unpinned)
    assert error.value.code == "unresolved_reference"
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], {**f["git_file"], "unexpected": True})
    assert error.value.code == "invalid_reference"

    missing = f["control"].s.blob_path(f["git_file"]["sha256"])
    saved = missing.read_bytes()
    missing.unlink()
    try:
        with pytest.raises(Fault) as error:
            resolver.resolve(f["control"].owner, f["project"], f["git_file"])
        assert error.value.code == "unresolved_reference"
    finally:
        missing.parent.mkdir(parents=True, exist_ok=True)
        missing.write_bytes(saved)

    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], {**f["git_file"], "path": "calc.py/child.py"})
    assert error.value.code == "unknown_reference"


def test_git_walks_repeated_subtrees_and_tolerates_unrelated_gitlink(full, tmp_path):
    root = tmp_path / "repeated-repo"
    for directory in (root / "a" / "nested", root / "b" / "nested"):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "a" / "x.py").write_text("def a_x():\n    return 1\n", encoding="utf-8")
    (root / "b" / "x.py").write_text("def a_x():\n    return 1\n", encoding="utf-8")
    (root / "a" / "nested" / "y.py").write_text("def nested_y():\n    return 2\n", encoding="utf-8")
    (root / "b" / "nested" / "y.py").write_text("def nested_y():\n    return 2\n", encoding="utf-8")
    first_commit = _commit(root)
    subprocess.run(
        ["git", "-C", str(root), "update-index", "--add",
         "--cacheinfo", f"160000,{first_commit},vendor"], check=True,
    )
    env = {**os.environ, "GIT_AUTHOR_NAME": "trace-ref-test", "GIT_AUTHOR_EMAIL": "trace-ref@test",
           "GIT_COMMITTER_NAME": "trace-ref-test", "GIT_COMMITTER_EMAIL": "trace-ref@test"}
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=trace-ref-test", "-c",
         "user.email=trace-ref@test", "commit", "-qm", "gitlink"], check=True, env=env,
    )
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    project = full.k.create_project(full.owner, "repeated tree fixture")["id"]
    repository = full.sn.register(full.owner, project, "repeated", str(root))["id"]
    proposal = full.traceability.propose(full.owner, project, kind="code",
                                         scope={"repository": repository, "commit": commit})
    revision = full.traceability.extract(full.owner, proposal["id"])["revision"]
    resolver = TraceabilityRefResolver(full)

    # a/ and b/ deliberately share one Git tree OID, as do their nested trees.
    resolver.resolve(full.owner, project, _git_file_ref(full, project, revision, repository, "a/x.py"))
    resolver.resolve(full.owner, project, _git_file_ref(full, project, revision, repository, "b/x.py"))
    resolver.resolve(full.owner, project, _git_file_ref(full, project, revision, repository, "b/nested/y.py"))

    gitlink_ref = _git_file_ref(full, project, revision, repository, "vendor",
                                sha256_override=digest(b"gitlink has no content CAS"))
    with pytest.raises(Fault) as error:
        resolver.resolve(full.owner, project, gitlink_ref)
    assert error.value.code == "unresolved_reference"

    revision_row = full.s.one("SELECT * FROM traceability_revisions WHERE id=?", (revision,), True)
    revision_body = json.loads(revision_row["body"])
    checkpoint = next(
        json.loads(record["body"])
        for record in full.s.all(
            "SELECT body FROM traceability_records WHERE proposal=? AND kind='extraction_checkpoint' ORDER BY created,id",
            (proposal["id"],),
        )
        if json.loads(record["body"]).get("key") == "__git_pin_complete__"
    )
    missing_tree = next(tree for tree in checkpoint["git_pin"]["trees"] if tree["oid"] != revision_body["git_pin"]["tree"])
    missing_path = full.s.blob_path(missing_tree["blob"])
    saved = missing_path.read_bytes()
    missing_path.unlink()
    try:
        with pytest.raises(Fault) as error:
            resolver.resolve(full.owner, project, _git_file_ref(full, project, revision, repository, "b/nested/y.py"))
        assert error.value.code == "unresolved_reference"
    finally:
        missing_path.parent.mkdir(parents=True, exist_ok=True)
        missing_path.write_bytes(saved)


def test_source_utf8_boundaries_and_artifact_pointer_are_strict(refs_fixture):
    f = refs_fixture
    resolver = TraceabilityRefResolver(f["control"])
    raw = f["control"].s.blob_get(f["source"]["blob_digest"])
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], {**f["source"], "source_id": None})
    assert error.value.code == "unresolved_reference"
    # The emoji occupies four bytes; the interior byte is not a valid UTF-8
    # endpoint even though it is an integer in the raw byte range.
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], {**f["source"], "byte_start": 5, "byte_end": len(raw)})
    assert error.value.code == "invalid_reference"
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], {**f["artifact"], "ac_pointer": "/title"})
    assert error.value.code == "unresolved_reference"
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], {**f["artifact"], "ac_digest": digest("other")})
    assert error.value.code == "stale_reference"


def test_cross_project_and_candidate_currentness_are_not_inferred(refs_fixture):
    f = refs_fixture
    resolver = TraceabilityRefResolver(f["control"])
    other = f["control"].k.create_project(f["control"].owner, "other project")["id"]
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, other, f["git_file"])
    assert error.value.code == "cross_project"

    f["control"].s.execute("UPDATE tasks SET candidate=NULL WHERE id=?", (f["task"],))
    with pytest.raises(Fault) as error:
        resolver.resolve(f["control"].owner, f["project"], f["candidate"])
    assert error.value.code == "stale_reference"


def test_historical_candidate_uses_replan_body_digest_and_rejects_bad_history(refs_fixture):
    f = refs_fixture
    control = f["control"]
    resolver = TraceabilityRefResolver(control)
    task_row = control.s.one("SELECT * FROM tasks WHERE id=?", (f["task"],), True)
    old_revision = task_row["revision"]
    old_body_digest = digest(json.loads(task_row["body"]))
    control.task_revisions.replan(control.owner, f["task"], old_revision, "refresh historical candidate fixture")

    historical_ref = {**f["candidate"], "task_revision": old_revision}
    result = resolver.resolve(control.owner, f["project"], historical_ref, require_current=False)
    task_dependency = next(item for item in result["dependencies"] if item["kind"] == "task")
    assert result["current"] is False
    assert task_dependency["revision"] == old_revision
    assert task_dependency["digest"] == old_body_digest
    with pytest.raises(Fault) as error:
        resolver.resolve(control.owner, f["project"], historical_ref)
    assert error.value.code == "stale_reference"

    missing_history = {**historical_ref, "task_revision": old_revision + 1}
    with pytest.raises(Fault) as error:
        resolver.resolve(control.owner, f["project"], missing_history, require_current=False)
    assert error.value.code == "stale_reference"

    # A mutable current row cannot silently overwrite the immutable history
    # identity.  This creates a conflicting same-revision candidate mapping;
    # the resolver must reject it as ambiguous.
    current = control.s.one("SELECT * FROM tasks WHERE id=?", (f["task"],), True)
    conflicting_body = json.loads(current["body"])
    conflicting_body["goal"] = conflicting_body["goal"] + "\nconflicting current material"
    control.s.execute(
        "UPDATE tasks SET revision=?,candidate=?,body=? WHERE id=?",
        (old_revision, f["candidate"]["candidate"], canonical(conflicting_body).decode(), f["task"]),
    )
    with pytest.raises(Fault) as error:
        resolver.resolve(control.owner, f["project"], historical_ref, require_current=False)
    assert error.value.code == "ambiguous_reference"
