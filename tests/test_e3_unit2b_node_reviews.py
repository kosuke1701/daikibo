"""Finite controller fixtures for E3 Unit 2b node review reuse."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from daikibo.assurance_node_reviews import build_node_requests, select_node_reviews
from daikibo.assurance_criteria import evaluate_criteria
from daikibo.assurance_denominators import collect_stage_context, derive_denominator, project_task
from daikibo.common import Actor, Fault, canonical, digest, uid


def _artifact_ref(control, project, ident):
    row = control.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (ident, project), True)
    return {"kind": "artifact", "project": project, "artifact": ident,
            "revision": row["revision"], "body_digest": row["digest"]}


def _task_ref(control, project, ident):
    from daikibo.task_revisions import task_definition_digest

    row = control.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (ident, project), True)
    body = json.loads(row["body"])
    return {"kind": "task_revision", "project": project, "task": ident,
            "revision": row["revision"], "definition_digest": task_definition_digest(body)}


def _review_adapter(control, tmp_path, name="node-review"):
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin); c=p.get('context',{})\n"
        "a=c.get('artifact',{}).get('body',{}).get('acceptance')\n"
        "t=c.get('task',{}).get('acceptance')\n"
        "covered=a if isinstance(a,list) else (t if isinstance(t,list) else [])\n"
        "print(json.dumps({'verdict':'pass','rationale':'deterministic protocol fixture',"
        "'covered':covered,'findings':[],'observations':[{'ref':p['subject'],'detail':'fixture observation'}],"
        "'dispositions':[]}))\n"
    )
    control.rt.adapters.register(control.owner, name, "fixture", sys.executable, [str(script)])
    return name


def _failing_adapter(control, tmp_path, name="node-fail"):
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "print(json.dumps({'verdict':'fail','rationale':'deliberate observed failure',"
        "'covered':[],'findings':[],'observations':[{'ref':p['subject'],'detail':'failure fixture'}],"
        "'dispositions':[]}))\n"
    )
    control.rt.adapters.register(control.owner, name, "fixture", sys.executable, [str(script)])
    return name


def _make_task(control, project, requirement):
    task = control.w.create(control.owner, project, {
        "title": "Unit2b review task", "goal": "inspect frozen plan",
        "read_artifacts": [requirement], "write_paths": [],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [], "non_goals": [],
    })
    plan = control.w.plan_tests(control.owner, task["id"], {"checks": [{
        "id": "unit", "argv": [sys.executable, "-c", "print(1)"],
        "kind": "pytest", "report": "results.xml", "required_tests": ["unit"],
        "purpose": "bounded fixture",
    }]})
    task_row = control.w.task(control.owner, task["id"])
    plan_row = control.s.one("SELECT * FROM plans WHERE task=?", (task["id"],), True)
    control.rt.verification_materials.pin_test_plan(
        control.owner, project, task_row, plan_row,
        captured_from={"controller": "runtime", "operation": "unit2b-test", "capture_id": uid("CAP")},
    )
    return task_row, plan, json.loads(plan_row["body"])


def _role(result, index=0):
    return next(iter(result[index]["roles"].values()))


def test_owner_acceptance_without_receipt_is_unverified(full, full_project):
    control, project_data = full, full_project
    project, _, requirement, _ = project_data
    ref = _artifact_ref(control, project, requirement)
    requests = build_node_requests(control, control.owner, project=project,
                                   selectors=[{"selector": "accepted_requirement", "node_ref": ref}])
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert _role(selected)["status"] == "unverified"
    assert _role(selected)["reason"] == "no_current_receipt"


def test_real_runtime_artifact_receipt_is_reused(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path)
    control.rt.review(control.owner, requirement, "requirements", adapter)
    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "requirement", "node_ref": _artifact_ref(control, project, requirement)}],
    )
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    role = _role(selected)
    assert role["status"] == "satisfied"
    assert role["selected_receipt"]["id"].startswith("EVD-")
    assert role["binding"] == requests[0]["binding"]


def test_task_plan_review_does_not_require_future_test_receipt(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    task, _, plan_body = _make_task(control, project, requirement)
    adapter = _review_adapter(control, tmp_path, "plan-review")
    control.rt.review(control.owner, task["id"], "test_plan", adapter, proposal=plan_body)
    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "test_plan", "node_ref": _task_ref(control, project, task["id"])}],
    )
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert _role(selected)["status"] == "satisfied"


def test_task_plan_supplemental_quality_uses_its_own_prompt_material(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    task, _, plan_body = _make_task(control, project, requirement)
    adapter = _review_adapter(control, tmp_path, "supplemental-task-review")
    control.rt.review(control.owner, task["id"], "quality", adapter)
    control.rt.review(control.owner, task["id"], "test_plan", adapter, proposal=plan_body)

    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "test_plan", "node_ref": _task_ref(control, project, task["id"]),
                    "roles": ["quality", "test_plan"]}],
    )
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert selected[0]["roles"]["quality"]["status"] == "satisfied"
    assert selected[0]["roles"]["test_plan"]["status"] == "satisfied"


def test_owner_frozen_plan_review_keeps_baseline_after_code_changes(full, full_project, tmp_path):
    from conftest import make_task

    control, project, _repository, _requirement, root = (full, *full_project)
    task = make_task(control, full_project)
    control.w.ready(control.owner, task)
    row = control.w.task(control.owner, task)
    plan_row = control.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    plan_body = json.loads(plan_row["body"])
    adapter = _review_adapter(control, tmp_path, "frozen-plan-baseline")
    review = control.rt.review(control.owner, task, "test_plan", adapter, proposal=plan_body)
    freeze = control.rt.review_materials.latest_plan_freeze(row, plan_row)
    assert freeze["review_binding"] == control.g.receipt(review["receipt"])["binding"]
    assert freeze["snapshot_digest"] == control.g.receipt(review["receipt"])["snapshot"]

    # Runtime's current checkout now differs from the owner-frozen baseline.
    source_file = root / "calc.py"
    original_source = source_file.read_text()
    source_file.write_text("def add(a, b):\n    return a + b + 1\n")
    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "test_plan", "node_ref": _task_ref(control, project, task)}],
    )
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert _role(selected)["status"] == "satisfied"
    source_file.write_text(original_source)

    # The snapshot identity remains reusable only while semantic review
    # material such as policy stays current.
    policy = control.g.policy(project)
    policy_body = dict(policy["body"])
    policy_body["max_parallel"] += 1
    with control.s.transaction():
        control.s.execute("UPDATE policies SET revision=?,body=?,digest=? WHERE project=?",
                          (policy["revision"] + 1, canonical(policy_body).decode(),
                           digest(policy_body), project))
    assert control.rt.review_materials.frozen_plan_snapshot(
        control.owner, row, plan_row,
    ) is None

    agent = Actor("plan-adopting-agent", "agent", project)
    with pytest.raises(Fault) as old_review:
        control.w.plan_tests(agent, task, plan_body, review["receipt"])
    assert old_review.value.code == "stale_evidence"
    refreshed = control.rt.review(control.owner, task, "test_plan", adapter, proposal=plan_body)
    control.w.plan_tests(agent, task, plan_body, refreshed["receipt"])
    current_row = control.w.task(control.owner, task)
    current_plan = control.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    assert control.rt.review_materials.frozen_plan_snapshot(
        control.owner, current_row, current_plan,
    ) is not None
    refreshed_requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "test_plan", "node_ref": _task_ref(control, project, task)}],
    )
    refreshed_selected = select_node_reviews(
        control, control.owner, node_requests=refreshed_requests,
    )
    assert _role(refreshed_selected)["status"] == "satisfied"


def test_legacy_owner_frozen_plan_can_recover_current_review_baseline(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    task, _, plan_body = _make_task(control, project, requirement)
    adapter = _review_adapter(control, tmp_path, "legacy-plan-baseline")
    control.rt.review(control.owner, task["id"], "test_plan", adapter, proposal=plan_body)
    plan_row = control.s.one("SELECT * FROM plans WHERE task=?", (task["id"],), True)
    # This signed event has the historical shape, before review binding and
    # snapshot baseline were stored with a frozen plan.
    control.sec.event(project, "test_plan_frozen", control.owner.id,
                      {"task": task["id"], "digest": plan_row["digest"], "checks": ["unit"]})

    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "test_plan", "node_ref": _task_ref(control, project, task["id"])}],
    )
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert _role(selected)["status"] == "satisfied"


def test_json_copy_and_seal_mutation_are_rejected(full, full_project):
    control, project, _, requirement, _ = (full, *full_project)
    ref = _artifact_ref(control, project, requirement)
    requests = build_node_requests(control, control.owner, project=project,
                                   selectors=[{"selector": "requirement", "node_ref": ref}])
    plain = json.loads(json.dumps(requests))
    with pytest.raises(Fault) as copied:
        select_node_reviews(control, control.owner, node_requests=plain)
    assert copied.value.code == "invalid_node_request"
    requests[0]["binding"] = "0" * 64
    with pytest.raises(Fault) as changed:
        select_node_reviews(control, control.owner, node_requests=requests)
    assert changed.value.code == "invalid_node_request"


def test_changed_task_definition_is_stale(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    task, _, plan_body = _make_task(control, project, requirement)
    adapter = _review_adapter(control, tmp_path, "plan-stale")
    control.rt.review(control.owner, task["id"], "test_plan", adapter, proposal=plan_body)
    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "test_plan", "node_ref": _task_ref(control, project, task["id"])}],
    )
    old = control.s.one("SELECT body FROM tasks WHERE id=?", (task["id"],), True)
    body = json.loads(old["body"])
    body["goal"] = "changed after review"
    with control.s.transaction():
        control.s.execute("UPDATE tasks SET body=? WHERE id=?", (canonical(body).decode(), task["id"]))
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert _role(selected)["status"] == "stale"


def test_missing_prompt_cas_is_unverified(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "prompt-missing")
    reviewed = control.rt.review(control.owner, requirement, "requirements", adapter)
    receipt = control.g.receipt(reviewed["receipt"])
    # The receipt remains immutable, but a missing prompt CAS must not be
    # treated as reusable evidence.
    path = control.s.blob_path(receipt["input_digest"])
    path.unlink()
    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "requirement", "node_ref": _artifact_ref(control, project, requirement)}],
    )
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert _role(selected)["status"] == "unverified"


def test_latest_failed_receipt_is_retained_as_failed(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _failing_adapter(control, tmp_path)
    control.rt.review(control.owner, requirement, "requirements", adapter)
    selected = select_node_reviews(
        control, control.owner,
        node_requests=build_node_requests(control, control.owner, project=project,
                                          selectors=[{"selector": "requirement", "node_ref": _artifact_ref(control, project, requirement)}]),
    )
    role = _role(selected)
    assert role["status"] == "failed"
    assert role["reason"] == "review_failed"
    assert role["selected_receipt"] is not None


def test_same_wall_clock_uses_durable_event_order(full, full_project, tmp_path, monkeypatch):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "ambiguous")
    import daikibo.runtime as runtime_module
    monkeypatch.setattr(runtime_module, "timestamp", lambda: 1234567890.0)
    first = control.rt.review(control.owner, requirement, "requirements", adapter)
    second = control.rt.review(control.owner, requirement, "requirements", adapter)
    selected = select_node_reviews(
        control, control.owner,
        node_requests=build_node_requests(control, control.owner, project=project,
                                          selectors=[{"selector": "requirement", "node_ref": _artifact_ref(control, project, requirement)}]),
    )
    role = _role(selected)
    assert role["status"] == "satisfied"
    assert role["selected_receipt"]["id"] == second["receipt"]
    assert role["selected_receipt"]["id"] != first["receipt"]


@pytest.mark.parametrize("tamper", ["missing", "duplicate", "mac"])
def test_durable_receipt_event_link_is_unique_and_valid(full, full_project, tmp_path, tamper):
    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "event-integrity-" + tamper)
    reviewed = control.rt.review(control.owner, requirement, "requirements", adapter)
    receipt = control.g.receipt(reviewed["receipt"])
    event = control.s.one(
        "SELECT * FROM events WHERE kind='run_observed' AND project=? "
        "AND json_extract(body,'$.receipt')=?",
        (project, reviewed["receipt"]),
        True,
    )
    assert event is not None
    if tamper == "missing":
        with control.s.transaction():
            control.s.execute("DROP TRIGGER events_no_delete")
            control.s.execute("DELETE FROM events WHERE seq=?", (event["seq"],))
    elif tamper == "duplicate":
        control.sec.event(project, "run_observed", "collector", {
            "run": receipt["run"], "receipt": receipt["id"],
            "exit": receipt["exit_code"], "cancelled": bool(receipt["cancelled"]),
            "timed_out": bool(receipt["timed_out"]),
        })
    else:
        with control.s.transaction():
            control.s.execute("DROP TRIGGER events_no_update")
            control.s.execute("UPDATE events SET mac=? WHERE seq=?", ("0" * 64, event["seq"]))
    selected = select_node_reviews(
        control, control.owner,
        node_requests=build_node_requests(
            control, control.owner, project=project,
            selectors=[{"selector": "requirement", "node_ref": _artifact_ref(control, project, requirement)}],
        ),
    )
    role = _role(selected)
    assert role["status"] == "unverified"
    assert role["reason"] == "observed_order_invalid"


def test_durable_receipt_order_survives_backup_restore(full, full_project, tmp_path):
    from daikibo.control import Control
    from daikibo.operations import restore_backup
    from daikibo.observed_receipts import ordered_observed_receipts

    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "event-backup")
    first = control.rt.review(control.owner, requirement, "requirements", adapter)
    second = control.rt.review(control.owner, requirement, "requirements", adapter)
    backup = control.ops.backup(control.owner)
    restored_home = tmp_path / "event-order-restored"
    restore_backup(backup["path"], restored_home, backup["sha256"])
    restored = Control(restored_home, mode="validation", start_workers=False)
    try:
        ordered = ordered_observed_receipts(
            restored, project=project, subject=requirement,
            role="requirements", binding=control.s.one(
                "SELECT binding FROM receipts WHERE id=?", (first["receipt"],), True,
            )["binding"],
            receipt_ids=[first["receipt"], second["receipt"]],
        )
        assert [item["row"]["id"] for item in ordered] == [first["receipt"], second["receipt"]]
        assert ordered[0]["event_seq"] < ordered[1]["event_seq"]
    finally:
        restored.close()


def test_foreign_subject_receipt_is_rejected_by_order_reader(full, full_project, tmp_path):
    from daikibo.observed_receipts import ordered_observed_receipts

    control, project, _, requirement, _ = (full, *full_project)
    adapter = _review_adapter(control, tmp_path, "event-foreign")
    reviewed = control.rt.review(control.owner, requirement, "requirements", adapter)
    binding = control.s.one(
        "SELECT binding FROM receipts WHERE id=?", (reviewed["receipt"],), True,
    )["binding"]
    with pytest.raises(Fault) as error:
        ordered_observed_receipts(
            control, project=project, subject="foreign-subject",
            role="requirements", binding=binding, receipt_ids=[reviewed["receipt"]],
        )
    assert error.value.code == "observed_order_invalid"


def test_body_kind_self_claim_cannot_override_canonical_row(full, full_project):
    control, project, _, requirement, _ = (full, *full_project)
    row = control.s.one("SELECT * FROM artifacts WHERE id=?", (requirement,), True)
    body = json.loads(row["body"])
    body["kind"] = "design"
    with control.s.transaction():
        control.s.execute("UPDATE artifacts SET body=?,digest=? WHERE id=?",
                          (canonical(body).decode(), digest(body), requirement))
    with pytest.raises(Fault) as error:
        build_node_requests(control, control.owner, project=project,
                            selectors=[{"selector": "requirement", "node_ref": _artifact_ref(control, project, requirement)}])
    assert error.value.code == "integrity_error"


def test_other_invariant_change_makes_saved_artifact_review_stale(full, full_project, tmp_path):
    control, project, _, requirement, _ = (full, *full_project)
    source = control.k.source(control.owner, project, "Critical invariant source")
    critical = control.k.propose(control.owner, project, "requirement", {
        "title": "Critical", "statement": "Critical requirement",
        "acceptance": ["AC-CRITICAL"], "source_refs": [source["id"]],
        "constraints": {"mode": "safe"},
    })
    control.k.accept(control.owner, critical["id"], 1)
    adapter = _review_adapter(control, tmp_path, "invariant")
    control.rt.review(control.owner, critical["id"], "requirements", adapter)
    requests = build_node_requests(
        control, control.owner, project=project,
        selectors=[{"selector": "requirement", "node_ref": _artifact_ref(control, project, critical["id"])}],
    )
    other = control.k.propose(control.owner, project, "design", {
        "title": "Other invariant", "statement": "Separate design invariant",
        "constraints": {"version": 1},
    })
    control.k.accept(control.owner, other["id"], 1)
    selected = select_node_reviews(control, control.owner, node_requests=requests)
    assert _role(selected)["status"] == "stale"


def test_criteria_preserve_missing_denominator_and_unconnected_synthesis(full, tmp_path):
    # Reuse the published Unit2a controller fixture, rather than injecting a
    # hand-written denominator into the checker.
    from test_e3_unit2a_denominators import _fixture

    fixture = _fixture(full, tmp_path)
    denominator = derive_denominator(collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"],
    ))
    ref = _artifact_ref(full, fixture["project"], fixture["parent"]["id"])
    requests = build_node_requests(full, full.owner, project=fixture["project"],
                                   selectors=[{"selector": "requirement", "node_ref": ref}])
    reviews = select_node_reviews(full, full.owner, node_requests=requests)
    from daikibo.assurance import SET_UNIVERSAL_CRITERIA
    from daikibo.assurance_relations import registry_entry

    relation = "realizes"
    requirements = sorted(SET_UNIVERSAL_CRITERIA | set(registry_entry(relation)["set_checks"]))
    result = evaluate_criteria(relation=relation, requirements=requirements,
                               denominator=denominator, edges=[], validated_reviews=reviews)
    assert result["criteria"]["all_acceptance_conditions"]["status"] == "missing"
    assert result["criteria"]["all_edges_current"]["status"] == "unsupported"
    assert result["criteria"]["meaning_review"]["status"] == "unverified"
    assert result["status"] != "satisfied"


def test_projection_without_origin_adapter_never_becomes_global_pass(full, tmp_path):
    from test_e3_unit2a_denominators import _fixture
    from daikibo.assurance import SET_UNIVERSAL_CRITERIA
    from daikibo.assurance_relations import registry_entry

    fixture = _fixture(full, tmp_path)
    denominator = derive_denominator(collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"],
    ))
    task_ref = _task_ref(full, fixture["project"], fixture["task_a"]["id"])
    projection = project_task(denominator, task_ref)
    ref = _artifact_ref(full, fixture["project"], fixture["parent"]["id"])
    reviews = select_node_reviews(
        full, full.owner,
        node_requests=build_node_requests(full, full.owner, project=fixture["project"],
                                          selectors=[{"selector": "requirement", "node_ref": ref}]),
    )
    relation = "realizes"
    requirements = sorted(SET_UNIVERSAL_CRITERIA | set(registry_entry(relation)["set_checks"]))
    result = evaluate_criteria(relation=relation, requirements=requirements,
                               denominator=projection, edges=[], validated_reviews=reviews)
    assert result["status"] == "unsupported"


def test_criteria_rejects_requirements_subset(full, tmp_path):
    from test_e3_unit2a_denominators import _fixture
    fixture = _fixture(full, tmp_path)
    denominator = derive_denominator(collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"],
    ))
    ref = _artifact_ref(full, fixture["project"], fixture["parent"]["id"])
    reviews = select_node_reviews(
        full, full.owner,
        node_requests=build_node_requests(full, full.owner, project=fixture["project"],
                                          selectors=[{"selector": "requirement", "node_ref": ref}]),
    )
    with pytest.raises(Fault) as error:
        evaluate_criteria(relation="realizes", requirements=[], denominator=denominator,
                          edges=[], validated_reviews=reviews)
    assert error.value.code == "invalid_criteria"
