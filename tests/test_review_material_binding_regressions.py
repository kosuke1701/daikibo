"""Regression coverage for review material currentness at adoption boundaries."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from daikibo.common import Actor, Fault, digest, parse_json


def _reviewer(control, tmp_path):
    state = tmp_path / "review-state.txt"
    state.write_text("pass")
    script = tmp_path / "material_reviewer.py"
    script.write_text(
        "import json,pathlib,sys\n"
        "p=json.load(sys.stdin); c=p.get('context',{})\n"
        "mode=pathlib.Path(sys.argv[1]).read_text().strip()\n"
        "if p['role']=='test_plan': covered=c.get('task',{}).get('acceptance',[])\n"
        "elif 'artifact' in c: covered=c['artifact'].get('body',{}).get('acceptance',[])\n"
        "else: covered=c.get('required_coverage',[])\n"
        "if mode=='invalid':\n"
        " print(json.dumps({'verdict':'pass','rationale':'invalid protocol fixture','covered':covered,'findings':[],'observations':[],'dispositions':[]})); sys.exit(0)\n"
        "verdict='fail' if mode=='fail' else 'pass'\n"
        "print(json.dumps({'verdict':verdict,'rationale':'finite protocol fixture, not semantic judgment',"
        "'covered':covered,'findings':[],'observations':[{'ref':p['subject'],'detail':'observed exact supplied material'}],"
        "'dispositions':[]}))\n"
    )
    control.rt.adapters.register(control.owner, "material-reviewer", "fixture", sys.executable,
                                 [str(script), str(state)])
    return state


def _project_source(control, name):
    project = control.k.create_project(control.owner, name)["id"]
    source = control.k.source(control.owner, project,
                              "The accepted feature and its verification must preserve the stated behavior.")
    return project, source


def _test_plan():
    return {"checks": [{"id": "feature-a", "kind": "pytest",
                        "argv": [sys.executable, "-m", "pytest", "-q"],
                        "required_tests": ["test_feature_a"]}]}


def _revise_task_with_extra_acceptance(control, project, task):
    definition = control.w.task(control.owner, task)["body"]
    proposed = {key: value for key, value in definition.items() if key != "task_kind"}
    proposed.update(title="Features A and B", goal="Implement Features A and B",
                    acceptance=["AC-A", "AC-B"], non_goals=[])
    proposal = control.task_revisions.propose(
        control.owner, task, 1, proposed, "Add Feature B to this Task",
    )
    impact = control.rt.review(control.owner, proposal["id"], "impact", "material-reviewer")
    control.task_revisions.apply(control.owner, proposal["id"], proposal["digest"], impact["receipt"])


def test_test_plan_review_binds_task_snapshot_and_latest_valid_judgment(full, tmp_path):
    state = _reviewer(full, tmp_path)
    project, source = _project_source(full, "Versioned test-plan review")
    requirement = full.k.propose(full.owner, project, "requirement", {
        "title": "Features", "statement": "Implement Features A and B",
        "acceptance": ["AC-A", "AC-B"], "source_refs": [source["id"]],
    })
    full.k.accept(full.owner, requirement["id"], 1)
    task = full.w.create(full.owner, project, {
        "title": "Feature A", "goal": "Implement Feature A",
        "read_artifacts": [requirement["id"]], "write_paths": [],
        "acceptance": ["AC-A"], "dependencies": [], "repos": [],
        "non_goals": ["Feature B"],
    })["id"]
    agent = Actor("review-agent", "agent", project)
    plan = _test_plan()

    old = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    assert old["result"]["verdict"] == "pass"
    old_prompt = parse_json(full.s.blob_get(full.g.receipt(old["receipt"])["input_blob"]))
    assert old_prompt["context"]["review_policy"]["digest"] == full.g.policy(project)["digest"]
    _revise_task_with_extra_acceptance(full, project, task)

    state.write_text("fail")
    current_fail = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    assert current_fail["result"]["verdict"] == "fail"
    with pytest.raises(Fault) as stale_old:
        full.w.plan_tests(agent, task, plan, old["receipt"])
    assert stale_old.value.code == "stale_evidence"

    state.write_text("pass")
    current_pass = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    state.write_text("invalid")
    invalid = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    assert full.g.receipt(invalid["receipt"])["judgment_valid"] is False
    full.w.plan_tests(agent, task, plan, current_pass["receipt"])

    state.write_text("fail")
    later_fail = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    with pytest.raises(Fault) as superseded:
        full.w.plan_tests(agent, task, plan, current_pass["receipt"])
    assert superseded.value.code == "stale_evidence"
    assert full.g.receipt(later_fail["receipt"])["result"]["verdict"] == "fail"

    state.write_text("pass")
    recovered = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    full.w.plan_tests(agent, task, plan, recovered["receipt"])
    assert full.w.ready(agent, task)["status"] == "ready"


def test_artifact_acceptance_binds_sources_invariants_and_latest_verdict(full, tmp_path):
    state = _reviewer(full, tmp_path)
    project, source = _project_source(full, "Versioned artifact review")
    invariant = full.k.propose(full.owner, project, "design", {
        "title": "Feature policy", "statement": "Feature A is permitted",
        "critical": True, "source_refs": [source["id"]],
    })
    full.k.accept(full.owner, invariant["id"], 1)
    requirement = full.k.propose(full.owner, project, "requirement", {
        "title": "Feature A", "statement": "Feature A is enabled",
        "acceptance": ["AC-A"], "source_refs": [source["id"]],
    })
    agent = Actor("artifact-agent", "agent", project)

    old = full.rt.review(full.owner, requirement["id"], "requirements", "material-reviewer")
    change = full.p.change(full.owner, project, {
        "title": "Prohibit Feature A", "origin": "design", "reason": "Update feature policy",
        "affected": [invariant["id"]], "evidence": [source["id"]],
        "deltas": [{"artifact": invariant["id"], "expected_revision": 1,
                    "body": {**invariant["body"], "statement": "Feature A is prohibited"}}],
    })
    full.p.attempt(full.owner, change["id"], "local_repair", {
        "hypothesis": "Update the canonical policy", "alternatives": ["Retain the old rule"],
        "evidence": [old["receipt"]], "outcome": "solution", "remaining_unknown": "",
    })
    consistency = full.rt.review(full.owner, change["id"], "consistency", "material-reviewer")
    full.p.apply_technical_change(full.owner, change["id"], consistency["receipt"])

    state.write_text("fail")
    current_fail = full.rt.review(full.owner, requirement["id"], "requirements", "material-reviewer")
    old_body = full.g.receipt(old["receipt"])
    current_body = full.g.receipt(current_fail["receipt"])
    assert old_body["binding"] != current_body["binding"]
    with pytest.raises(Fault) as stale_old:
        full.k.accept(agent, requirement["id"], 1, old["receipt"])
    assert stale_old.value.code == "stale_evidence"

    state.write_text("pass")
    current_pass = full.rt.review(full.owner, requirement["id"], "requirements", "material-reviewer")
    state.write_text("fail")
    later_fail = full.rt.review(full.owner, requirement["id"], "requirements", "material-reviewer")
    with pytest.raises(Fault) as superseded:
        full.k.accept(agent, requirement["id"], 1, current_pass["receipt"])
    assert superseded.value.code == "stale_evidence"
    assert full.g.receipt(later_fail["receipt"])["result"]["verdict"] == "fail"

    state.write_text("pass")
    recovered = full.rt.review(full.owner, requirement["id"], "requirements", "material-reviewer")
    assert full.k.accept(agent, requirement["id"], 1, recovered["receipt"])["status"] == "accepted"


def test_critical_artifact_binding_survives_its_own_acceptance(full, tmp_path):
    _reviewer(full, tmp_path)
    project, source = _project_source(full, "Invariant self-binding")
    artifact = full.k.propose(full.owner, project, "requirement", {
        "title": "Critical requirement", "statement": "Preserve the safe operating mode.",
        "acceptance": ["AC-SAFE"], "source_refs": [source["id"]],
        "critical": True, "constraints": {"mode": "safe"},
    })
    agent = Actor("critical-review-agent", "agent", project)
    review = full.rt.review(full.owner, artifact["id"], "requirements", "material-reviewer")
    before = full.g.review_materials.artifact(full.owner, artifact["id"], "requirements")

    full.k.accept(agent, artifact["id"], 1, review["receipt"])

    after = full.g.review_materials.artifact(full.owner, artifact["id"], "requirements")
    assert after["binding"] == before["binding"]
    full.g.require_review(review["receipt"], artifact["id"], after["binding"],
                          {"requirements"}, latest=True)


def test_asserted_link_review_binds_both_endpoints_relation_and_basis(full, tmp_path):
    _reviewer(full, tmp_path)
    project, source = _project_source(full, "Versioned asserted link")
    source_artifact = full.k.propose(full.owner, project, "design", {
        "title": "Feature design", "statement": "The design realizes Feature A.",
        "source_refs": [source["id"]],
    })
    target = full.k.propose(full.owner, project, "requirement", {
        "title": "Feature A", "statement": "Feature A is supported.",
        "acceptance": ["AC-A"], "source_refs": [source["id"]],
    })
    other = full.k.propose(full.owner, project, "requirement", {
        "title": "Feature B", "statement": "Feature B is supported.",
        "acceptance": ["AC-B"], "source_refs": [source["id"]],
    })
    for artifact in (source_artifact, target, other):
        full.k.accept(full.owner, artifact["id"], 1)

    agent = Actor("link-agent", "agent", project)
    proposal = {"format": "artifact.link.v1", "target": target["id"],
                "relation": "realizes", "confidence": "asserted",
                "basis": "The design implements the stated Feature A requirement."}
    review = full.rt.review(full.owner, source_artifact["id"], "trace", "material-reviewer",
                            proposal=proposal)
    assert full.k.link(agent, source_artifact["id"], target["id"], "realizes", "asserted",
                       proposal["basis"], review["receipt"])["target"] == target["id"]

    for alternate_target, relation, basis in (
        (other["id"], "realizes", proposal["basis"]),
        (target["id"], "verifies", proposal["basis"]),
        (target["id"], "realizes", "A different explanation."),
    ):
        with pytest.raises(Fault) as stale:
            full.k.link(agent, source_artifact["id"], alternate_target,
                        relation, "asserted", basis, review["receipt"])
        assert stale.value.code == "stale_evidence"


def test_local_stage_reader_uses_generic_and_exact_link_material(full, tmp_path):
    _reviewer(full, tmp_path)
    project, source = _project_source(full, "Stage reader follows Runtime material")
    requirement = full.k.propose(full.owner, project, "requirement", {
        "title": "Feature A", "statement": "Feature A is supported.",
        "acceptance": ["AC-A"], "source_refs": [source["id"]],
    })
    source_artifact = full.k.propose(full.owner, project, "design", {
        "title": "Feature design", "statement": "The design realizes Feature A.",
        "source_refs": [source["id"]],
    })
    full.k.accept(full.owner, requirement["id"], 1)
    full.k.accept(full.owner, source_artifact["id"], 1)

    generic = full.rt.review(full.owner, requirement["id"], "requirements", "material-reviewer")
    generic_binding, kind = full.local_executions._stage_receipt_binding(
        full.owner, project, generic["receipt"],
    )
    assert kind == "artifact"
    assert generic_binding == full.rt.review_materials.artifact(
        full.owner, requirement["id"], "requirements",
    )["binding"]
    assert generic_binding == full.g.receipt(generic["receipt"])["binding"]
    full.g.require_review(generic["receipt"], requirement["id"], generic_binding,
                          {"requirements"}, latest=True)

    link_proposal = {
        "format": "artifact.link.v1", "target": requirement["id"],
        "relation": "realizes", "confidence": "asserted",
        "basis": "The design implements the stated Feature A requirement.",
    }
    linked = full.rt.review(
        full.owner, source_artifact["id"], "trace", "material-reviewer",
        proposal=link_proposal,
    )
    link_binding, kind = full.local_executions._stage_receipt_binding(
        full.owner, project, linked["receipt"],
    )
    assert kind == "artifact"
    assert link_binding == full.rt.review_materials.artifact_link(
        full.owner, source_artifact["id"], "trace", link_proposal,
    )["binding"]
    assert link_binding == full.g.receipt(linked["receipt"])["binding"]
    assert link_binding != full.rt.review_materials.artifact(
        full.owner, source_artifact["id"], "trace",
    )["binding"]
    full.g.require_review(linked["receipt"], source_artifact["id"], link_binding,
                          {"trace"}, latest=True)


def test_local_stage_reader_uses_frozen_test_plan_material(full, full_project, tmp_path):
    _reviewer(full, tmp_path)
    project, repository, requirement, _root = full_project
    task = full.w.create(full.owner, project, {
        "title": "Plan-stage binding", "goal": "Implement Feature A",
        "read_artifacts": [requirement], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [],
    })["id"]
    plan = _test_plan()
    full.w.plan_tests(full.owner, task, plan)
    review = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    binding, kind = full.local_executions._stage_receipt_binding(
        full.owner, project, review["receipt"],
    )
    assert kind == "task"
    assert binding == full.g.receipt(review["receipt"])["binding"]
    assert binding != full.g.task_binding(task)
    full.g.require_review(review["receipt"], task, binding, {"test_plan"}, latest=True)


def test_post_execution_plan_review_materializes_the_frozen_baseline(full, full_project, tmp_path):
    project, repository, requirement, root = full_project
    baseline = (root / "calc.py").read_text(encoding="utf-8")
    script = tmp_path / "baseline_reader.py"
    script.write_text(
        "import json,pathlib,sys\n"
        "p=json.load(sys.stdin); c=p.get('context',{})\n"
        "observed=pathlib.Path('calc.py').read_text(encoding='utf-8')\n"
        "print(json.dumps({'verdict':'pass','rationale':'finite baseline fixture',"
        "'covered':c.get('task',{}).get('acceptance',[]),'findings':[],"
        "'observations':[{'ref':'calc.py','detail':observed}],'dispositions':[]}))\n",
        encoding="utf-8",
    )
    full.rt.adapters.register(full.owner, "baseline-reader", "fixture", sys.executable,
                              [str(script)])
    task = full.w.create(full.owner, project, {
        "title": "Post-execution frozen plan review", "goal": "Implement Feature A",
        "read_artifacts": [requirement], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [],
    })["id"]
    plan = _test_plan()
    full.w.plan_tests(full.owner, task, plan)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    task_row = full.w.task(full.owner, task)
    freeze = full.rt.review_materials.latest_plan_freeze(task_row, plan_row)
    assert isinstance(freeze.get("snapshot_manifest_blob"), str)
    snapshot = parse_json(full.s.blob_get(freeze["snapshot_manifest_blob"]))
    assert snapshot["digest"] == freeze["snapshot_digest"]
    assert snapshot["repos"][repository]["files"]["calc.py"]["blob"] == digest(
        baseline.encode("utf-8"),
    )

    first = full.rt.review(full.owner, task, "test_plan", "baseline-reader", proposal=plan)
    first_receipt = full.g.receipt(first["receipt"])
    assert first_receipt["snapshot"] == freeze["snapshot_digest"]
    full.w.ready(full.owner, task)
    full.w.claim(full.owner, project, task=task)
    changed = "def add(a, b):\n    return a + b + 100\n"
    (root / "calc.py").write_text(changed, encoding="utf-8")

    # A later independent plan review still runs in the original complete
    # baseline workspace. It does not label the changed checkout with the old
    # digest or give the reviewer a digest-only empty repository map.
    # Omitting a proposal resolves to the saved plan through the same frozen
    # baseline path as explicitly passing the saved plan body.
    second = full.rt.review(full.owner, task, "test_plan", "baseline-reader")
    second_receipt = full.g.receipt(second["receipt"])
    assert second_receipt["snapshot"] == first_receipt["snapshot"]
    assert second_receipt["readonly_verified"] is True
    assert second_receipt["result"]["observations"][0]["detail"] == baseline
    assert second_receipt["binding"] == first_receipt["binding"]


def test_planned_task_can_re_review_changed_snapshot_and_refreeze(full, full_project, tmp_path):
    _reviewer(full, tmp_path)
    project, repository, requirement, root = full_project
    task = full.w.create(full.owner, project, {
        "title": "Refresh a planned baseline", "goal": "Implement Feature A",
        "read_artifacts": [requirement], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [],
    })["id"]
    plan = _test_plan()
    full.w.plan_tests(full.owner, task, plan)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    prior = full.rt.review_materials.latest_plan_freeze(
        full.w.task(full.owner, task), plan_row,
    )
    changed = "def add(a, b):\n    return a + b + 7\n"
    (root / "calc.py").write_text(changed, encoding="utf-8")
    live_snapshot = full.sn.capture(full.owner, project, [repository])

    review = full.rt.review(full.owner, task, "test_plan", "material-reviewer", proposal=plan)
    reviewed = full.g.receipt(review["receipt"])
    assert reviewed["snapshot"] == live_snapshot["digest"]
    assert reviewed["snapshot"] != prior["snapshot_digest"]

    agent = Actor("baseline-refresh-agent", "agent", project)
    full.w.plan_tests(agent, task, plan, review["receipt"])
    refreshed = full.rt.review_materials.latest_plan_freeze(
        full.w.task(full.owner, task),
        full.s.one("SELECT * FROM plans WHERE task=?", (task,), True),
    )
    assert refreshed["snapshot_digest"] == live_snapshot["digest"]


def test_missing_frozen_plan_manifest_cannot_reuse_digest_only_baseline(full, full_project, tmp_path):
    from daikibo.assurance_node_reviews import build_node_requests
    from test_e3_unit2b_node_reviews import _task_ref

    _reviewer(full, tmp_path)
    project, repository, requirement, _root = full_project
    task = full.w.create(full.owner, project, {
        "title": "Missing frozen plan manifest", "goal": "Implement Feature A",
        "read_artifacts": [requirement], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository],
        "non_goals": [],
    })["id"]
    full.w.plan_tests(full.owner, task, _test_plan())
    task_row = full.w.task(full.owner, task)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    freeze = full.rt.review_materials.latest_plan_freeze(task_row, plan_row)
    full.s.blob_path(freeze["snapshot_manifest_blob"]).unlink()
    assert full.rt.review_materials.frozen_plan_snapshot(
        full.owner, task_row, plan_row,
    ) is None
    with pytest.raises(Fault) as node_unverified:
        build_node_requests(
            full, full.owner, project=project,
            selectors=[{"selector": "test_plan", "node_ref": _task_ref(full, project, task)}],
        )
    assert node_unverified.value.code == "unverified_node_review"
    full.w.ready(full.owner, task)
    full.w.claim(full.owner, project, task=task)
    before = full.s.one(
        "SELECT count(*) AS n FROM receipts WHERE subject=? AND role='test_plan'", (task,),
    )["n"]
    with pytest.raises(Fault) as unavailable:
        full.rt.review(full.owner, task, "test_plan", "material-reviewer",
                       proposal=parse_json(plan_row["body"]))
    assert unavailable.value.code == "review_material_unavailable"
    after = full.s.one(
        "SELECT count(*) AS n FROM receipts WHERE subject=? AND role='test_plan'", (task,),
    )["n"]
    assert after == before


def test_concurrent_revision_cannot_create_false_accept_event(full, tmp_path):
    _reviewer(full, tmp_path)
    project, source = _project_source(full, "Concurrent artifact acceptance")
    artifact = full.k.propose(full.owner, project, "requirement", {
        "title": "Stable behavior", "statement": "Return the exact requested value.",
        "acceptance": ["AC-1"], "source_refs": [source["id"]],
    })
    review = full.rt.review(full.owner, artifact["id"], "requirements", "material-reviewer")
    agent = Actor("race-agent", "agent", project)

    entered = threading.Event()
    resume = threading.Event()
    original = full.g.review_materials.artifact

    def hold_acceptance_material(actor, subject, role):
        material = original(actor, subject, role)
        if threading.current_thread().name == "accept-artifact" and not entered.is_set():
            entered.set()
            assert resume.wait(10)
        return material

    full.g.review_materials.artifact = hold_acceptance_material
    outcomes = {}

    def accept():
        try:
            outcomes["accept"] = full.k.accept(agent, artifact["id"], 1, review["receipt"])
        except Exception as exc:  # captured for assertions in the main test thread
            outcomes["accept_error"] = exc

    def revise():
        try:
            outcomes["revise"] = full.k.revise(
                full.owner, artifact["id"], 1,
                {**artifact["body"], "statement": "A conflicting concurrent revision."},
                "Concurrent requirement update",
            )
        except Exception as exc:  # expected after serialized acceptance commits
            outcomes["revise_error"] = exc

    adopter = threading.Thread(target=accept, name="accept-artifact")
    reviser = threading.Thread(target=revise, name="revise-artifact")
    adopter.start()
    assert entered.wait(10)
    reviser.start()
    resume.set()
    adopter.join(10)
    reviser.join(10)
    assert not adopter.is_alive() and not reviser.is_alive()
    full.g.review_materials.artifact = original

    current = full.k.artifact(full.owner, artifact["id"])
    assert "accept_error" not in outcomes
    assert current["revision"] == 1 and current["status"] == "accepted"
    assert isinstance(outcomes.get("revise_error"), Fault)
    assert outcomes["revise_error"].code == "change_required"
    accepted = full.s.all("SELECT body FROM events WHERE kind='artifact_accepted' AND project=?",
                          (project,))
    assert len(accepted) == 1
    event = parse_json(accepted[0]["body"])
    assert event["revision"] == current["revision"] and event["digest"] == current["digest"]
