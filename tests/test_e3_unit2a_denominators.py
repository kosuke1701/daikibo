from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from daikibo.assurance_denominators import (
    collect_stage_context,
    derive_denominator,
    page_obligations,
    project_task,
)
from daikibo.common import Fault, canonical, digest, timestamp, uid


def _task(control, project, requirement, title, checks):
    row = control.w.create(control.owner, project, {
        "title": title,
        "goal": "fixture goal",
        "read_artifacts": [requirement],
        "write_paths": [title.replace(" ", "_") + ".py"],
        "acceptance": ["AC-PARENT-0"],
        "dependencies": [], "repos": [], "non_goals": [],
    })
    measured = []
    for check in checks:
        check = dict(check)
        check["kind"] = "pytest"
        check.setdefault("report", "results.xml")
        check.setdefault("required_tests", [check["id"]])
        measured.append(check)
    control.w.plan_tests(control.owner, row["id"], {"checks": measured})
    task = control.w.task(control.owner, row["id"])
    plan = control.s.one("SELECT * FROM plans WHERE task=?", (row["id"],), True)
    control.rt.verification_materials.pin_test_plan(
        control.owner, project, task, plan,
        captured_from={"controller": "runtime", "operation": "pin", "capture_id": uid("CAP")},
    )
    return task


def _fixture(full, tmp_path):
    project = full.k.create_project(full.owner, "Unit2a denominator")['id']
    source = full.k.source(full.owner, project, "Parent and child requirements.")
    parent = full.k.propose(full.owner, project, "requirement", {
        "title": "Parent", "statement": "Parent requirement",
        "acceptance": ["AC-PARENT-0", "AC-SHARED"], "source_refs": [source["id"]],
    })
    parent = full.k.accept(full.owner, parent["id"], 1)
    child = full.k.propose(full.owner, project, "requirement", {
        "title": "Child", "statement": "Child requirement",
        "acceptance": ["AC-SHARED"], "source_refs": [source["id"]],
    })
    child = full.k.accept(full.owner, child["id"], 1)
    program = full.p.begin(full.owner, project, source["id"], compact=True)["program"]
    checks_a = [
        {"id": "same", "argv": ["python", "-c", "print(1)"], "kind": "command", "purpose": "first"},
        {"id": "second", "argv": ["python", "-c", "print(2)"], "kind": "command", "purpose": "second"},
    ]
    task_a = _task(full, project, parent["id"], "Task A", checks_a)
    units = [{
        "id": "leaf-a", "title": "A", "parent": None, "domain": None,
        "rationale": "fixture", "obligations": [
            {"requirement": parent["id"], "acceptance": "AC-PARENT-0"},
            {"requirement": parent["id"], "acceptance": "AC-SHARED"},
        ], "tasks": [task_a["id"]], "interfaces": [], "dependencies": [],
    }]
    breakdown_body = {
        "format": "daikibo.breakdown.v1", "program": program,
        "title": "fixture", "rationale": "fixture", "units": units,
        "scope": {}, "structure": {}, "material_bindings": {},
    }
    breakdown = "BREAKDOWN-unit2a"
    with full.s.transaction():
        full.s.execute(
            "INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)",
            (breakdown, program, project, canonical(breakdown_body).decode(),
             digest(breakdown_body), "proposed", None, timestamp()),
        )
    return {
        "control": full, "project": project, "source": source,
        "parent": parent, "child": child, "program": program,
        "task_a": task_a, "breakdown": breakdown,
    }


def _ac_ref(project, artifact, index, value):
    return {"kind": "traceability_ref", "project": project, "locator": {
        "ref_type": "artifact_ac", "artifact": artifact["id"],
        "revision": artifact["revision"], "body_digest": artifact["digest"],
        "ac_pointer": f"/acceptance/{index}", "ac_digest": digest(value), "ac_id": value,
    }}


def _unit_b(control, fixture, task_b):
    project, program = fixture["project"], fixture["program"]
    artifact = fixture["parent"]
    ac = _ac_ref(project, artifact, 0, "AC-PARENT-0")
    set_id, revision = "TSET-unit2a", "TREV-unit2a"
    revision_body = {"format": "traceability.revision.v1", "project": project,
                     "set_id": set_id, "revision": 1, "counts": {"leaf": 3, "unknown": 0}}
    revision_digest = digest(revision_body)
    population_digest = digest({"revision": revision, "revision_digest": revision_digest, "leaves": ["ITEM-A", "ITEM-B", "ITEM-C"]})
    entries = [
        {"item": "ITEM-A", "handling": "port", "requirement": ac, "acceptance": ac,
         "contributors": [{"task": fixture["task_a"]["id"], "revision": 1, "required": True},
                          {"task": task_b["id"], "revision": 1, "required": True}]},
        {"item": "ITEM-B", "handling": "exclude", "reason": "out of scope", "contributors": []},
    ]
    decision_body = {"format": "daikibo.traceability.v1", "kind": "decision", "id": "TDEC-unit2a",
                     "project": project, "revision": revision, "decisions": entries}
    decision_digest = digest(decision_body)
    binding_body = {"format": "daikibo.traceability.v1", "kind": "scope_binding", "id": "TBIND-unit2a",
                    "proposal": None, "project": project, "revision": revision, "program": program,
                    "scope_requirement": ac, "applicable_from": "implementation", "mandatory": True,
                    "population_digest": population_digest, "project_revision": 1}
    binding_digest = digest(binding_body)
    with control.s.transaction():
        control.s.execute("INSERT INTO traceability_sets VALUES(?,?,?,?,?,?,?)",
                          (set_id, project, "unit2a", "code", None, None, timestamp()))
        control.s.execute("INSERT INTO traceability_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (revision, set_id, project, 1, "active", canonical(revision_body).decode(),
                           revision_digest, population_digest, "fixture", timestamp()))
        for ordinal, item_id in enumerate(("ITEM-A", "ITEM-B", "ITEM-C")):
            item_body = {"format": "traceability.item.v1", "id": item_id, "revision": revision,
                         "item_kind": "file", "leaf": True}
            control.s.execute("INSERT INTO traceability_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (item_id, revision, project, ordinal, "file", item_id + ".py", "known",
                               0, 1, canonical(item_body).decode(), digest(item_body), 1))
        control.s.execute("UPDATE traceability_sets SET active_revision=?,active_digest=? WHERE id=?",
                          (revision, revision_digest, set_id))
        control.s.execute("INSERT INTO traceability_decisions VALUES(?,?,?,?,?,?,?)",
                          ("TDEC-unit2a", revision, project, canonical(decision_body).decode(), decision_digest, "accepted", timestamp()))
        control.traceability._append_record(
            project, revision, None, "decision_adopted",
            control.traceability._record_body(
                "decision_adopted", project, revision, None,
                {"table": "traceability_decisions", "id": "TDEC-unit2a", "digest": decision_digest},
            ),
        )
        control.s.execute("INSERT INTO traceability_bindings VALUES(?,?,?,?,?,?,?)",
                          ("TBIND-unit2a", project, revision, canonical(binding_body).decode(), binding_digest, "pending", timestamp()))
        control.traceability._append_record(
            project, revision, None, "binding_adopted",
            control.traceability._record_body(
                "binding_adopted", project, revision, None,
                {"table": "traceability_bindings", "id": "TBIND-unit2a", "digest": binding_digest},
            ),
        )
    return {"revision": revision, "population_digest": population_digest}


def test_d01_parent_child_acceptance_and_two_checks_survive_edge_zero(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    context = collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan",
        proposed_breakdown=fixture["breakdown"],
    )
    denominator = derive_denominator(context)
    acceptance = [item for item in denominator["obligations"] if item["category"] == "acceptance_condition"]
    checks = [item for item in denominator["obligations"] if item["category"] == "required_check"]
    assert len(acceptance) == 3
    assert len(checks) == 2
    assert len({item["source_ref"]["locator"]["artifact"] for item in acceptance}) == 2
    assert denominator["count"] == 5
    assert denominator["unresolved"] == []


def test_d02_hidden_controller_inputs_are_rejected(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    context = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"])
    hidden = copy.deepcopy(context)
    hidden["artifacts"].pop()
    with pytest.raises(Fault) as error:
        derive_denominator(hidden)
    assert error.value.code == "denominator_input_mismatch"

    task_b = _task(full, fixture["project"], fixture["parent"]["id"], "Task B", [
        {"id": "b", "argv": ["python", "-c", "print(3)"], "kind": "command", "purpose": "b"},
    ])
    _unit_b(full, fixture, task_b)
    task_context = collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"], stage="task",
        proposed_breakdown=fixture["breakdown"], task={
            "kind": "task_revision", "project": fixture["project"], "task": fixture["task_a"]["id"],
            "revision": 1, "definition_digest": digest(fixture["task_a"]["body"]),
        })
    hidden = copy.deepcopy(task_context)
    hidden["unit_b"]["leaves"].pop()
    with pytest.raises(Fault) as error:
        derive_denominator(hidden)
    assert error.value.code == "denominator_input_mismatch"
    hidden = copy.deepcopy(context)
    hidden["task_definitions"][0]["plan"]["check_refs"].pop()
    with pytest.raises(Fault) as error:
        derive_denominator(hidden)
    assert error.value.code == "denominator_input_mismatch"


def test_d03_exact_task_revision_and_check_digest_identity(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    context = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"])
    denominator = derive_denominator(context)
    current = fixture["task_a"]
    stale = {"kind": "task_revision", "project": fixture["project"], "task": current["id"], "revision": 2, "definition_digest": current["body"] and digest(current["body"])}
    with pytest.raises(Fault) as error:
        project_task(denominator, stale)
    assert error.value.code in {"stale_reference", "unresolved_reference", "invalid_reference"}
    other = full.k.create_project(full.owner, "other")['id']
    cross = dict(current)
    cross_ref = {"kind": "task_revision", "project": other, "task": current["id"], "revision": 1, "definition_digest": digest(current["body"])}
    with pytest.raises(Fault) as error:
        project_task(denominator, cross_ref)
    assert error.value.code == "cross_project"
    checks = [item["source_ref"] for item in denominator["obligations"] if item["category"] == "required_check"]
    assert checks[0]["check_id"] != checks[1]["check_id"] or checks[0]["check_digest"] != checks[1]["check_digest"]


def test_d04_task_projection_keeps_a_b_and_does_not_transfer_to_c(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    task_b = _task(full, fixture["project"], fixture["parent"]["id"], "Task B", [
        {"id": "b", "argv": ["python", "-c", "print(3)"], "kind": "command", "purpose": "b"},
    ])
    task_c = _task(full, fixture["project"], fixture["parent"]["id"], "Task C", [
        {"id": "c", "argv": ["python", "-c", "print(4)"], "kind": "command", "purpose": "c"},
    ])
    _unit_b(full, fixture, task_b)
    context = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="task", proposed_breakdown=fixture["breakdown"], task={
        "kind": "task_revision", "project": fixture["project"], "task": fixture["task_a"]["id"], "revision": 1, "definition_digest": digest(fixture["task_a"]["body"]),
    })
    denominator = derive_denominator(context)
    a_ref = next(item["task_ref"] for item in context["task_definitions"] if item["id"] == fixture["task_a"]["id"])
    a = project_task(denominator, a_ref)
    b_ref = next(item["task_ref"] for item in context["task_definitions"] if item["id"] == task_b["id"])
    c_ref = next(item["task_ref"] for item in context["task_definitions"] if item["id"] == task_c["id"])
    b = project_task(denominator, b_ref)
    c = project_task(denominator, c_ref)
    population = [item["id"] for item in denominator["obligations"] if item["category"] == "population_leaf"]
    assert len(population) == 3
    a_population = set(a["obligation_ids"])
    b_population = set(b["obligation_ids"])
    assert a_population & b_population
    assert not (set(c["obligation_ids"]) & set(population))
    assert a["global_digest"] == b["global_digest"] == denominator["digest"]


def test_d05_all_unit_b_leaf_treatments_remain_in_denominator(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    task_b = _task(full, fixture["project"], fixture["parent"]["id"], "Task B", [
        {"id": "b", "argv": ["python", "-c", "print(3)"], "kind": "command", "purpose": "b"},
    ])
    _unit_b(full, fixture, task_b)
    context = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="task", proposed_breakdown=fixture["breakdown"], task={
        "kind": "task_revision", "project": fixture["project"], "task": fixture["task_a"]["id"], "revision": 1, "definition_digest": digest(fixture["task_a"]["body"]),
    })
    denominator = derive_denominator(context)
    handling = {leaf["id"]: leaf["handling"] for leaf in context["unit_b"]["leaves"]}
    observed = {item["source_ref"]["item"]: handling[item["source_ref"]["item"]]
                for item in denominator["obligations"] if item["category"] == "population_leaf"}
    assert observed == {"ITEM-A": "port", "ITEM-B": "exclude", "ITEM-C": "unprocessed"}


def test_d06_plan_does_not_require_candidate_but_delivery_missing_is_explicit(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    plan = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"])
    assert not any(item["code"] == "candidate_missing" for item in plan["unresolved"])
    delivery = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="delivery", proposed_breakdown=fixture["breakdown"])
    assert any(item["code"] == "delivery_material_missing" for item in delivery["unresolved"])


def test_d07_proposed_root_is_readable_but_other_program_is_rejected(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    context = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"])
    assert context["root_plan_ref"]["status"] == "proposed"
    other = full.p.begin(full.owner, fixture["project"], fixture["source"]["id"], compact=True)["program"]
    with pytest.raises(Fault) as error:
        collect_stage_context(full, full.owner, project=fixture["project"], program=other, stage="plan", proposed_breakdown=fixture["breakdown"])
    assert error.value.code == "invalid_reference"


def test_d08_semantic_inputs_change_but_telemetry_does_not(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    first = derive_denominator(collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"]))
    with full.s.transaction():
        full.s.execute("UPDATE tasks SET status='completed',attempts=99,epoch=7,updated=? WHERE id=?", (timestamp() + 1, fixture["task_a"]["id"]))
    second = derive_denominator(collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"]))
    assert first["input_digest"] == second["input_digest"]
    with full.s.transaction():
        change_body = {"title": "fixture change", "reason": "new input"}
        full.s.execute("INSERT INTO changes VALUES(?,?,?,?,?,?)", ("CHANGE-unit2a", fixture["project"], "ready", canonical(change_body).decode(), 1, timestamp()))
        full.s.execute("UPDATE changes SET body=?,revision=2 WHERE id=?", (canonical({"title": "changed", "reason": "new input"}).decode(), "CHANGE-unit2a"))
    third = derive_denominator(collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"]))
    assert third["input_digest"] != second["input_digest"]


def test_d09_bounded_pages_require_one_digest_and_preserve_identity(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    denominator = derive_denominator(collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"]))
    first = page_obligations(denominator, limit=2)
    pages = [first]
    while pages[-1]["next_offset"] is not None:
        pages.append(page_obligations(denominator, offset=pages[-1]["next_offset"], limit=2, expected_digest=denominator["digest"]))
    ids = [item["id"] for page in pages for item in page["items"]]
    assert ids == [item["id"] for item in denominator["obligations"]]
    with pytest.raises(Fault) as error:
        page_obligations(denominator, offset=first["next_offset"], limit=2, expected_digest="0" * 64)
    assert error.value.code == "stale_page"


def test_d10_declared_checks_keep_distinct_identity(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    _task(full, fixture["project"], fixture["parent"]["id"], "Task B", [
        {"id": "same", "argv": ["python", "-c", "print(999)"], "kind": "command", "purpose": "different"},
    ])
    context = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"])
    denominator = derive_denominator(context)
    checks = [item for item in denominator["obligations"] if item["category"] == "required_check" and item["source_ref"]["check_id"] == "same"]
    assert len(checks) == 2
    assert len({item["source_ref"]["check_digest"] for item in checks}) == 2


def test_d11_unknown_artifact_type_is_explicit_unsupported(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    body = {"title": "unknown", "statement": "unknown"}
    with full.s.transaction():
        full.s.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)", ("ART-unknown", fixture["project"], "unknown", 1, "accepted", canonical(body).decode(), digest(body), full.owner.id, timestamp()))
        source_body = {"title": "source-unknown", "statement": "source-unknown", "acceptance": ["AC-SOURCE-UNKNOWN"], "source_refs": ["SOURCE-missing"]}
        full.s.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)", ("ART-source-unknown", fixture["project"], "requirement", 1, "accepted", canonical(source_body).decode(), digest(source_body), full.owner.id, timestamp()))
    context = collect_stage_context(full, full.owner, project=fixture["project"], program=fixture["program"], stage="plan", proposed_breakdown=fixture["breakdown"])
    assert any(item["code"] == "artifact_kind_unsupported" for item in context["unresolved"])
    denominator = derive_denominator(context)
    assert denominator["count"] > 0
    assert any(item["code"] == "artifact_kind_unsupported" for item in denominator["unresolved"])
    assert any(item["code"] == "source_reference_unknown" for item in denominator["unresolved"])


def test_review_context_authority_seal_rejects_coherent_empty_copy(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    context = collect_stage_context(full, full.owner, project=fixture["project"],
                                    program=fixture["program"], stage="plan",
                                    proposed_breakdown=fixture["breakdown"])
    hidden = copy.deepcopy(context)
    hidden["source_inputs"] = []
    hidden["artifacts"] = []
    hidden["task_definitions"] = []
    hidden["assignments"] = []
    hidden["input_refs"] = []
    hidden["unresolved"] = []
    hidden["capabilities"]["enumeration"] = {}
    with pytest.raises(Fault) as error:
        derive_denominator(hidden)
    assert error.value.code == "denominator_input_mismatch"

    denominator = derive_denominator(context)
    empty = copy.deepcopy(denominator)
    empty["obligations"] = []
    empty["count"] = 0
    empty["capabilities"]["enumeration"]["obligations"] = 0
    with pytest.raises(Fault) as error:
        page_obligations(empty)
    assert error.value.code == "denominator_input_mismatch"


def test_source_partition_meaning_changes_input_digest(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    first = derive_denominator(collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"]))
    full.k.classify(full.owner, fixture["source"]["id"], 0, 5, "requirement",
                    [fixture["parent"]["id"]], "classify source span")
    second = derive_denominator(collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"]))
    assert first["input_digest"] != second["input_digest"]


def test_missing_source_cas_is_explicit_unresolved_material(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    path = full.s.blob_path(fixture["source"]["digest"])
    path.unlink()
    context = collect_stage_context(full, full.owner, project=fixture["project"],
                                    program=fixture["program"], stage="plan",
                                    proposed_breakdown=fixture["breakdown"])
    assert any(item["code"] == "source_material_missing" for item in context["unresolved"])
    assert context["source_inputs"][0]["material"]["status"] == "missing"
    denominator = derive_denominator(context)
    assert any(item["code"] == "source_material_missing" for item in denominator["unresolved"])
