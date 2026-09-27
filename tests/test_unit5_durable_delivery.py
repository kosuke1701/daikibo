"""Finite Unit5-O checks for durable Git observation and retry boundaries.

These tests use the repository's local bare-Git fixture.  The release gate is
stubbed only at this test boundary because the full governed review/qualification
path is a separate Unit5 acceptance job; no production allow path is added.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from daikibo.common import Fault, digest
from conftest import finish_task, make_task
from test_delivery_git_and_recovery import profile
from test_traceability_refs import _git_file_ref


def _two_repository_delivery(control, full_project, tmp_path):
    project, first_repo, requirement, _root = full_project
    second_root = tmp_path / "unit5-secondary"
    second_root.mkdir()
    (second_root / "secondary.py").write_text("value = 2\n")
    second_repo = control.sn.register(
        control.owner, project, "unit5-secondary", str(second_root),
    )["id"]
    task = make_task(control, full_project)
    body = profile(project, first_repo, requirement, task)
    body["repo_order"] = [first_repo, second_repo]
    control.d.configure(control.owner, project, body)
    finish_task(control, project, task)
    delivery = control.d.prepare(control.owner, project)["id"]
    control.d.verify(control.owner, delivery)
    control.s.execute("UPDATE deliveries SET status='verified' WHERE id=?", (delivery,))
    return project, delivery, [first_repo, second_repo]


def _test_gate(control, monkeypatch, *, trace_calls):
    """Isolate the transaction test from the separate governed acceptance."""
    def certify(actor, delivery, check_only=False):
        assert check_only is True
        row = control.s.one("SELECT status FROM deliveries WHERE id=?", (delivery,), True)
        return {"id": delivery, "status": row["status"], "currently_valid": True}

    monkeypatch.setattr(control.d, "certify", certify)
    trace = control.d.traceability
    original_record = trace.record_delivered_mappings

    def record(*args, **kwargs):
        trace_calls.append("mapping")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(trace, "record_delivered_mappings", record)
    return trace


def _codex_trace_review_adapter(control, tmp_path):
    """Register a protocol-valid subprocess review for public trace adoption.

    The adapter is a local validation fixture, so it is explicitly kept out of
    the product path.  Registering it as the Codex protocol makes Runtime
    collect a non-simulated, read-only review receipt; the traceability APIs
    still perform the real packet, coverage, and currentness checks.
    """
    script = tmp_path / "unit5_trace_review_codex.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "\n"
        "if '--version' in sys.argv:\n"
        "    print('unit5-trace-review-codex 1')\n"
        "    raise SystemExit(0)\n"
        "packet = json.load(sys.stdin)\n"
        "context = packet.get('context') or {}\n"
        "result = {'verdict': 'pass', 'rationale': 'read-only managed validation fixture',\n"
        "          'covered': context.get('required_coverage') or [], 'findings': [],\n"
        "          'observations': [{'ref': packet.get('subject', 'traceability-packet'),\n"
        "                           'detail': 'fixture inspected the immutable packet'}],\n"
        "          'dispositions': []}\n"
        "for index, value in enumerate(sys.argv):\n"
        "    if value == '--output-last-message' and index + 1 < len(sys.argv):\n"
        "        Path(sys.argv[index + 1]).write_text(json.dumps(result), encoding='utf-8')\n"
        "        break\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'unit5-trace-review'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',\n"
        "                   'text': json.dumps(result)}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    control.rt.adapters.register(
        control.owner, "unit5-trace-review", "codex", str(script), [],
    )
    return "unit5-trace-review"


def _adopt_trace_subject(control, project, subject, revision, adapter):
    """Review and adopt one immutable traceability proposal through public APIs."""
    packet = control.traceability.review_subject(control.owner, subject)
    role = packet["packet"]["role"]
    receipt = control.rt.review(control.owner, packet["subject"], role, adapter)
    assert receipt["result"]["verdict"] == "pass"
    assert receipt["result"]["covered"] == packet["required_coverage"]
    assert receipt["assurance"] == "validation"
    row = control.s.one("SELECT project FROM traceability_proposals WHERE id=?", (subject,), True)
    assert row["project"] == project
    return control.traceability.adopt(
        control.owner, project, revision=revision, subject=subject,
        review_refs=[receipt["receipt"]],
    )


def _initial_git_commit(root):
    """Create the real source Git identity consumed by Traceability.extract."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "calc.py", "test_calc.py"], cwd=root, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "unit5-traceability",
        "GIT_AUTHOR_EMAIL": "unit5-traceability@example.invalid",
        "GIT_COMMITTER_NAME": "unit5-traceability",
        "GIT_COMMITTER_EMAIL": "unit5-traceability@example.invalid",
    }
    subprocess.run(
        ["git", "-c", "user.name=unit5-traceability",
         "-c", "user.email=unit5-traceability@example.invalid",
         "commit", "-qm", "traceability source fixture"],
        cwd=root, check=True, env=env,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()


def _public_traceability_delivery_fixture(control, full_project, tmp_path):
    """Build one real adopted binding/mapping over the Delivery fixture."""
    project, delivery, repositories = _two_repository_delivery(control, full_project, tmp_path)
    _project, source_repository, requirement, root = full_project
    source_commit = _initial_git_commit(root)
    adapter = _codex_trace_review_adapter(control, tmp_path)
    trace = control.traceability
    population = trace.propose(
        control.owner, project, kind="code",
        scope={"repository": source_repository, "commit": source_commit},
    )
    extracted = trace.extract(control.owner, population["id"])
    revision = extracted["revision"]
    population_row = control.s.one(
        "SELECT digest FROM traceability_revisions WHERE id=?", (revision,), True,
    )
    _adopt_trace_subject(control, project, population["id"], revision, adapter)

    task_row = control.s.one(
        "SELECT id,revision,status FROM tasks WHERE project=? ORDER BY created,id LIMIT 1",
        (project,), True,
    )
    requirement_row = control.s.one(
        "SELECT revision,digest FROM artifacts WHERE id=? AND project=?", (requirement, project), True,
    )
    source_row = control.s.one(
        "SELECT id FROM sources WHERE project=? ORDER BY created,id LIMIT 1", (project,), True,
    )
    requirement_ref = {
        "ref_type": "artifact_ac", "artifact": requirement,
        "revision": requirement_row["revision"], "body_digest": requirement_row["digest"],
        "ac_pointer": "/acceptance/0", "ac_digest": digest("AC-ADD"),
    }
    leaf_rows = control.s.all(
        "SELECT id FROM traceability_items WHERE revision=? AND path='calc.py' "
        "AND leaf=1 AND status='known' ORDER BY ordinal,id LIMIT 2", (revision,),
    )
    # Keep one adopted decision leaf deliberately without a mapping edge.  The
    # real delivered gate must therefore remain pending after the durable
    # observation is saved; the test never installs a successful gate stub.
    assert len(leaf_rows) == 2
    leaf_row = leaf_rows[0]
    contributors = [{"task": task_row["id"], "revision": task_row["revision"], "required": True}]
    decision = trace.decide_propose(
        control.owner, project, revision, [{
            "item": row["id"], "handling": "port",
            "reason": "the completed Task owns this source leaf",
            "evidence": [source_row["id"]], "purpose": "code_port",
            "task": task_row["id"], "contributors": contributors,
            "requirement": requirement_ref, "acceptance": requirement_ref,
        } for row in leaf_rows],
    )
    _adopt_trace_subject(control, project, decision["proposal"], revision, adapter)
    target = _git_file_ref(control, project, revision, source_repository, "calc.py")
    mapping = trace.map_propose(
        control.owner, project, revision, [{
            "leaf_ids": [leaf_row["id"]], "purpose": "code_port",
            "decision_ref": decision["id"], "contributors": contributors,
            "target_refs": [target], "evidence_refs": [source_row["id"]],
        }],
    )
    _adopt_trace_subject(control, project, mapping["proposal"], revision, adapter)
    source_id = source_row["id"]
    program = control.p.begin(control.owner, project, source_id, compact=True)["program"]
    scope = trace.scope_propose(
        control.owner, project, revision, program, requirement_ref,
        applicable_from="delivery", mandatory=True,
    )
    _adopt_trace_subject(control, project, scope["proposal"], revision, adapter)
    assert trace._mandatory_bindings(project, phase="delivery")
    return {
        "project": project, "delivery": delivery, "repositories": repositories,
        "revision": revision, "revision_digest": population_row["digest"],
        "mapping": mapping["id"], "target": target, "adapter": adapter,
        "task": task_row["id"], "program": program,
    }


def test_git_observation_survives_halfway_pin_failure_and_retry_reuses_oid(
    full, full_project, tmp_path, monkeypatch,
):
    control = full
    project, delivery, repositories = _two_repository_delivery(control, full_project, tmp_path)
    trace_calls = []
    trace = _test_gate(control, monkeypatch, trace_calls=trace_calls)
    monkeypatch.setattr(trace, "delivered_closure_gate",
                        lambda *args, **kwargs: {"mandatory": False, "allowed": True, "failures": []})

    original_git_commit = control.sn.commit_snapshot
    git_calls = []

    def commit_snapshot(snapshot, repository, message, **kwargs):
        git_calls.append(repository)
        return original_git_commit(snapshot, repository, message, **kwargs)

    monkeypatch.setattr(control.sn, "commit_snapshot", commit_snapshot)
    original_pin = control.rt.verification_materials.pin_actual_delivery_commit
    calls = []
    failed = {"value": False}

    def pin_once(actor, project_id, row, body, repository, result, **kwargs):
        calls.append(repository)
        if repository == repositories[1] and not failed["value"]:
            failed["value"] = True
            raise Fault("capture_failed", "second repository material capture failed")
        return original_pin(actor, project_id, row, body, repository, result, **kwargs)

    monkeypatch.setattr(control.rt.verification_materials,
                        "pin_actual_delivery_commit", pin_once)
    with pytest.raises(Fault) as rejected:
        control.d.commit(control.owner, delivery, "unit5 durable observation")
    assert rejected.value.code == "capture_failed"
    assert set(rejected.value.details["saved_repositories"]) == set(repositories)
    assert git_calls == repositories

    saved = control.s.one("SELECT body,status FROM deliveries WHERE id=?", (delivery,), True)
    saved_body = json.loads(saved["body"])
    assert saved["status"] == "verified"
    assert set(saved_body["git"]) == set(repositories)
    for repository in repositories:
        outbox = control.s.one(
            "SELECT status,result FROM outbox WHERE dedup=?",
            (delivery + ":" + repository,), True,
        )
        assert outbox["status"] == "delivered"
        assert json.loads(outbox["result"])["commit"] == saved_body["git"][repository]["commit"]
    material_kinds = [
        json.loads(item["body"]).get("material_kind")
        for item in control.s.all(
            "SELECT body FROM assurance_objects WHERE project=? AND kind='material'",
            (project,),
        )
    ]
    # `verify` already owns one definition snapshot; the commit phase adds
    # its own immutable observation anchor before the first actual pin.
    assert material_kinds.count("delivery_snapshot") >= 2
    assert material_kinds.count("actual_delivery_commit") == 1
    assert calls == repositories

    # A retry reads the first durable actual material and pins only the
    # missing second repository.  Both exact refs are returned in the result.
    result = control.d.commit(control.owner, delivery, "unit5 durable observation retry")
    assert result["status"] == "delivered"
    assert {ref["repository"] for ref in result["actual_delivery_commit_refs"]} == set(repositories)
    assert calls == repositories + [repositories[1]]
    assert git_calls == repositories
    assert trace_calls == ["mapping"]
    actual_materials = [
        json.loads(item["body"]).get("material_kind")
        for item in control.s.all(
            "SELECT body FROM assurance_objects WHERE project=? AND kind='material'",
            (project,),
        )
    ]
    assert actual_materials.count("actual_delivery_commit") == 2

    # A second replay does not repeat Git or actual-commit production; a fresh
    # Delivery snapshot observation may still be captured for that attempt.
    before = list(calls)
    replay = control.d.commit(control.owner, delivery, "unit5 same oid replay")
    assert replay["status"] == "delivered"
    assert {ref["repository"] for ref in replay["actual_delivery_commit_refs"]} == set(repositories)
    assert calls == before
    assert git_calls == repositories


def test_final_gate_failure_keeps_mapping_and_same_oid_can_finalize(
    full, full_project, tmp_path, monkeypatch,
):
    control = full
    project, delivery, repositories = _two_repository_delivery(control, full_project, tmp_path)
    trace_calls = []
    trace = _test_gate(control, monkeypatch, trace_calls=trace_calls)
    original_pin = control.rt.verification_materials.pin_actual_delivery_commit
    pin_calls = []

    def pin(actor, project_id, row, body, repository, result, **kwargs):
        pin_calls.append(repository)
        return original_pin(actor, project_id, row, body, repository, result, **kwargs)

    monkeypatch.setattr(control.rt.verification_materials,
                        "pin_actual_delivery_commit", pin)
    gate = {"first": True}

    def final_gate(*args, **kwargs):
        if gate["first"]:
            gate["first"] = False
            raise Fault("traceability_incomplete", "final mapping review remains pending")
        return {"mandatory": True, "allowed": True, "failures": []}

    monkeypatch.setattr(trace, "delivered_closure_gate", final_gate)
    with pytest.raises(Fault) as rejected:
        control.d.commit(control.owner, delivery, "unit5 final gate failure")
    assert rejected.value.code == "traceability_incomplete"
    saved = control.s.one("SELECT body,status FROM deliveries WHERE id=?", (delivery,), True)
    assert saved["status"] == "verified"
    assert set(json.loads(saved["body"])["git"]) == set(repositories)
    assert trace_calls == ["mapping"]
    assert pin_calls == repositories

    result = control.d.commit(control.owner, delivery, "unit5 final gate repair")
    assert result["status"] == "delivered"
    assert {ref["repository"] for ref in result["actual_delivery_commit_refs"]} == set(repositories)
    assert pin_calls == repositories
    assert trace_calls == ["mapping", "mapping"]


def test_public_request_pin_failure_reproducible_boundary(
    full, full_project, tmp_path, monkeypatch,
):
    """The public request path must retain observation before a pin retry."""
    control = full
    project, delivery, repositories = _two_repository_delivery(control, full_project, tmp_path)
    trace_calls = []
    trace = _test_gate(control, monkeypatch, trace_calls=trace_calls)
    monkeypatch.setattr(trace, "delivered_closure_gate",
                        lambda *args, **kwargs: {"mandatory": False, "allowed": True, "failures": []})
    original_pin = control.rt.verification_materials.pin_actual_delivery_commit
    failed = {"value": False}

    def pin_once(actor, project_id, row, body, repository, result, **kwargs):
        if repository == repositories[1] and not failed["value"]:
            failed["value"] = True
            raise Fault("capture_failed", "public request capture failed")
        return original_pin(actor, project_id, row, body, repository, result, **kwargs)

    monkeypatch.setattr(control.rt.verification_materials,
                        "pin_actual_delivery_commit", pin_once)
    token = Path(control.sec.bootstrap()).read_text()
    request = {"id": "unit5-public-pin-failure", "method": "delivery.commit",
               "params": {"delivery": delivery, "message": "public request observation"}}
    with pytest.raises(Fault, match="public request capture failed"):
        control.request(token, request)
    changed = copy.deepcopy(request)
    changed["params"]["message"] = "different payload"
    with pytest.raises(Fault, match="reused for different content"):
        control.request(token, changed)

    row = control.s.one("SELECT body,status FROM deliveries WHERE id=?", (delivery,), True)
    body = __import__("json").loads(row["body"])
    assert row["status"] == "verified"
    # This assertion is intentionally the public-path regression target.  The
    # pre-repair outer request transaction erased this observation; the fixed
    # request lifecycle keeps both repository OIDs for the restart.
    assert set(body["git"]) == set(repositories)

    # The pending request marker and the observed OIDs survive a real Control
    # restart.  The same request id resumes the missing pin and then becomes
    # the ordinary idempotent replay record.
    home = control.s.home
    control.close()
    from daikibo.control import Control
    reopened = Control(home, mode="validation", start_workers=False)
    try:
        reopened_trace_calls = []
        _test_gate(reopened, monkeypatch, trace_calls=reopened_trace_calls)
        monkeypatch.setattr(reopened.d.traceability, "delivered_closure_gate",
                            lambda *args, **kwargs: {"mandatory": False, "allowed": True, "failures": []})
        resumed = reopened.request(token, request)
        assert resumed["status"] == "delivered"
        assert {ref["repository"] for ref in resumed["actual_delivery_commit_refs"]} == set(repositories)
        assert reopened.request(token, request) == resumed
    finally:
        reopened.close()


def test_public_nonempty_mapping_survives_later_fault_and_reuses_oid_after_restart(
    full, full_project, tmp_path, monkeypatch,
):
    """A real adopted mapping is durable before a genuine pending gate.

    The fixture reaches Traceability through its public propose/extract/review/
    adopt APIs and resolves the delivered target against the committed Git
    result.  It faults once after the mapping transaction, then resumes with a
    new Control instance.  The adopted decision intentionally contains one
    unmapped leaf, so the real final gate rejects the retry as pending.  No
    mapping row, OID, or successful gate result is authored by this test.
    """
    control = full
    fixture = _public_traceability_delivery_fixture(control, full_project, tmp_path)
    project, delivery = fixture["project"], fixture["delivery"]
    trace_calls = []
    trace = _test_gate(control, monkeypatch, trace_calls=trace_calls)
    original_gate = trace.delivered_closure_gate
    gate = {"first": True}

    def fail_once(*args, **kwargs):
        if gate["first"]:
            gate["first"] = False
            raise Fault("later_gate_fault", "final delivery gate failed after mapping persistence")
        return original_gate(*args, **kwargs)

    # Fault only at the final gate to exercise the already durable mapping
    # boundary.  The mapping producer itself is never replaced.
    monkeypatch.setattr(trace, "delivered_closure_gate", fail_once)
    pin_calls = []
    actual_refs = []
    original_pin = control.rt.verification_materials.pin_actual_delivery_commit

    def capture_pin(*args, **kwargs):
        pin_calls.append(args[4])
        result, material = original_pin(*args, **kwargs)
        actual_refs.append(copy.deepcopy(result))
        return result, material

    monkeypatch.setattr(
        control.rt.verification_materials,
        "pin_actual_delivery_commit",
        capture_pin,
    )
    token = Path(control.sec.bootstrap()).read_text()
    request = {
        "id": "unit5-nonempty-mapping-restart",
        "method": "delivery.commit",
        "params": {"delivery": delivery, "message": "unit5 mapping durable retry"},
    }
    with pytest.raises(Fault) as rejected:
        control.request(token, request)
    assert rejected.value.code == "later_gate_fault"
    assert actual_refs and {ref["repository"] for ref in actual_refs} == set(fixture["repositories"])
    assert pin_calls == fixture["repositories"]
    assert trace_calls == ["mapping"]

    rows = control.s.all(
        "SELECT id,body,digest FROM traceability_records "
        "WHERE project=? AND kind='delivered_mapping' ORDER BY created,id",
        (project,),
    )
    assert len(rows) == 1
    saved_record = {
        "id": rows[0]["id"], "body": json.loads(rows[0]["body"]),
        "digest": rows[0]["digest"],
    }
    assert saved_record["body"]["mapping_id"] == fixture["mapping"]
    assert saved_record["body"]["actual"]
    destination = saved_record["body"]["actual"][0]["destination"]
    assert destination["commit"] in {ref["commit"] for ref in actual_refs}
    assert destination["blob_oid"]
    assert digest(saved_record["body"]) == saved_record["digest"]

    home = control.s.home
    control.close()
    from daikibo.control import Control

    reopened = Control(home, mode="validation", start_workers=False)
    reopened.owner = reopened.sec.authenticate(token)
    try:
        reopened_trace_calls = []
        _test_gate(reopened, monkeypatch, trace_calls=reopened_trace_calls)
        resumed_pin_calls = []

        def unexpected_pin(*args, **kwargs):
            resumed_pin_calls.append(args[4] if len(args) > 4 else "unknown")
            raise AssertionError("a valid saved actual OID must be reused on restart")

        monkeypatch.setattr(
            reopened.rt.verification_materials,
            "pin_actual_delivery_commit",
            unexpected_pin,
        )
        from daikibo.delivery_material_reader import read_delivery_material

        with pytest.raises(Fault) as resumed_rejected:
            reopened.request(token, request)
        assert resumed_rejected.value.code == "traceability_incomplete"
        assert resumed_pin_calls == []
        assert reopened_trace_calls == ["mapping"]
        request_row = reopened.s.one(
            "SELECT result FROM requests WHERE actor=? AND id=?",
            (reopened.owner.id, request["id"]), True,
        )
        request_marker = json.loads(request_row["result"])
        assert request_marker["format"] == "daikibo.request-pending.v1"
        assert request_marker["last_error"]["code"] == "traceability_incomplete"
        assert reopened.s.one(
            "SELECT status FROM deliveries WHERE id=?", (delivery,), True,
        )["status"] == "verified"
        saved_material = read_delivery_material(
            reopened, reopened.owner, project=project,
            delivery=actual_refs[0]["delivery"],
        )
        reader_refs = {
            item["repository"]: item["actual_ref"]
            for item in saved_material["repositories"]
            if item.get("actual_ref") is not None
        }
        assert saved_material["status"] == "available"
        assert reader_refs == {
            ref["repository"]: ref for ref in actual_refs
        }
        replay_rows = reopened.s.all(
            "SELECT id,body,digest FROM traceability_records "
            "WHERE project=? AND kind='delivered_mapping' ORDER BY created,id",
            (project,),
        )
        assert len(replay_rows) == 1
        assert {
            "id": replay_rows[0]["id"], "body": json.loads(replay_rows[0]["body"]),
            "digest": replay_rows[0]["digest"],
        } == saved_record
        evidence_path = os.environ.get("UNIT5_O_MAPPING_EVIDENCE")
        if evidence_path:
            Path(evidence_path).write_text(json.dumps({
                "format": "unit5-o.mapping-repair-evidence.v2",
                "project": project,
                "delivery": delivery,
                "mapping_id": fixture["mapping"],
                "record_id": saved_record["id"],
                "record_digest": saved_record["digest"],
                "record_material_digest": saved_record["body"]["material_digest"],
                "actual_destinations": saved_record["body"]["actual"],
                "actual_delivery_commit_refs": actual_refs,
                "producer_repositories": pin_calls,
                "resume_pin_repositories": resumed_pin_calls,
                "pending_fault_code": resumed_rejected.value.code,
                "request_marker_format": request_marker["format"],
                "request_marker_error": request_marker["last_error"]["code"],
                "delivery_status_after_restart": "verified",
                "reader_actual_delivery_commit_refs": reader_refs,
                "same_record_after_restart": True,
                "same_saved_actual_refs_after_restart": reader_refs == {
                    ref["repository"]: ref for ref in actual_refs
                },
            }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    finally:
        reopened.close()
