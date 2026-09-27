from __future__ import annotations

import json

from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.common import canonical, digest, timestamp, uid


def _task(control, project, requirement, title, path):
    task = control.w.create(control.owner, project, {
        "title": title,
        "goal": "fixture goal",
        "read_artifacts": [requirement],
        "write_paths": [path],
        "acceptance": ["AC-IMPACT"],
        "dependencies": [], "repos": [], "non_goals": [],
    })
    control.w.plan_tests(control.owner, task["id"], {"checks": [{
        "id": "impact-check", "argv": ["python", "-c", "print(1)"],
        "kind": "pytest", "purpose": "impact fixture", "required_tests": ["impact-check"],
    }]})
    task = control.w.task(control.owner, task["id"])
    plan = control.s.one("SELECT * FROM plans WHERE task=?", (task["id"],), True)
    control.rt.verification_materials.pin_test_plan(
        control.owner, project, task, plan,
        captured_from={"controller": "runtime", "operation": "pin", "capture_id": uid("CAP")},
    )
    return task


def _base(full):
    project = full.k.create_project(full.owner, "Unit2c4 impact")['id']
    source = full.k.source(full.owner, project, "Impact inventory fixture authority.")
    root = full.k.propose(full.owner, project, "requirement", {
        "title": "Impact root", "statement": "The impact graph is complete.",
        "acceptance": ["AC-IMPACT"], "source_refs": [source["id"]],
    })
    root = full.k.accept(full.owner, root["id"], 1)
    program = full.p.begin(full.owner, project, source["id"], compact=True)["program"]
    task = _task(full, project, root["id"], "Impact task", "impact.py")
    breakdown_body = {
        "format": "daikibo.breakdown.v1", "program": program,
        "title": "impact fixture", "rationale": "fixture", "units": [{
            "id": "impact-unit", "title": "Impact", "parent": None,
            "domain": None, "rationale": "fixture",
            "obligations": [{"requirement": root["id"], "acceptance": "AC-IMPACT"}],
            "tasks": [task["id"]], "interfaces": [], "dependencies": [],
        }], "scope": {}, "structure": {}, "material_bindings": {},
    }
    breakdown = f"BREAKDOWN-unit2c4-{project}"
    with full.s.transaction():
        full.s.execute(
            "INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)",
            (breakdown, program, project, canonical(breakdown_body).decode(),
             digest(breakdown_body), "proposed", None, timestamp()),
        )
    return {"project": project, "source": source, "root": root,
            "program": program, "task": task, "breakdown": breakdown}


def _change(full, fixture, *, affected=None):
    return full.p.change(full.owner, fixture["project"], {
        "title": "Impact graph change", "origin": "design",
        "reason": "Recompute the complete affected graph.",
        "affected": affected or [fixture["root"]["id"]],
        "evidence": [fixture["source"]["id"]], "program": fixture["program"],
    })


def _context(full, fixture):
    return collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=fixture["breakdown"],
    )


def _add_child(full, fixture, index):
    child = full.k.propose(full.owner, fixture["project"], "design", {
        "title": f"Impact child {index}", "statement": f"Reachable child {index}.",
    }, artifact_id=f"IMPACT-CHILD-{index:03d}")
    child = full.k.accept(full.owner, child["id"], 1)
    full.k.link(full.owner, child["id"], fixture["root"]["id"], "depends_on",
                confidence="inferred", basis="finite impact fixture")
    return child


def test_i1_complete_impact_material_keeps_all_targets_over_201(full, tmp_path):
    fixture = _base(full)
    for index in range(202):
        _add_child(full, fixture, index)
    _change(full, fixture)

    context = _context(full, fixture)
    impact = context["impact_inventory"]["changes"][0]
    current = impact["current"]
    assert len(current["artifacts"]) == 203
    assert len(current["artifact_refs"]) == 203
    assert context["capabilities"]["enumeration"]["impacted_targets"] == 204
    denominator = derive_denominator(context)
    impacted = [item for item in denominator["obligations"]
                if item["category"] == "impacted_target"]
    assert len(impacted) == 204  # 203 artifacts plus the fixture Task
    assert all("IMPACT-CHILD-" in item["pointer"] or fixture["root"]["id"] in item["pointer"]
               or fixture["task"]["id"] in item["pointer"] for item in impacted)
    assert not any(item["pointer"].endswith("packet-leaf") for item in impacted)


def test_i2_current_rederivation_marks_new_artifact_and_task_stale_without_telemetry(full, tmp_path):
    fixture = _base(full)
    _change(full, fixture)
    first = _context(full, fixture)
    first_denominator = derive_denominator(first)

    child = _add_child(full, fixture, 0)
    added_task = _task(full, fixture["project"], child["id"], "New impact task", "new-impact.py")
    second = _context(full, fixture)
    impact = second["impact_inventory"]["changes"][0]
    assert impact["status"] == "stale"
    assert {item["id"] for item in impact["delta"]["added"]} >= {child["id"], added_task["id"]}
    assert fixture["root"]["id"] in impact["baseline"]["artifacts"]
    assert child["id"] not in impact["baseline"]["artifacts"]
    assert child["id"] in impact["current"]["artifacts"]
    assert added_task["id"] in impact["current"]["tasks"]

    # Controller execution state is telemetry.  It must not alter the impact
    # semantic material or the denominator input digest.
    with full.s.transaction():
        full.s.execute(
            "UPDATE tasks SET status='completed',validity='current',attempts=91,epoch=17,updated=? WHERE id=?",
            (timestamp() + 1, fixture["task"]["id"]),
        )
    third = _context(full, fixture)
    third_denominator = derive_denominator(third)
    second_denominator = derive_denominator(second)
    assert second["impact_inventory"]["digest"] == third["impact_inventory"]["digest"]
    assert second["input_refs"] == third["input_refs"]
    assert third_denominator["input_digest"] == second_denominator["input_digest"]
    assert first_denominator["input_digest"] != second_denominator["input_digest"]


def test_i3_missing_final_packet_is_unknown_and_packet_leaves_do_not_expand_denominator(full):
    fixture = _base(full)
    change = _change(full, fixture)
    packet_body = {"format": "impact.packet.v1", "leaf_manifest": ["packet-leaf"]}
    packet_digest = digest(packet_body)
    with full.s.transaction():
        full.s.execute(
            "INSERT INTO breakdown_packets VALUES(?,?,?,?,?)",
            ("PACKET-IMPACT-PRESENT", fixture["project"], canonical(packet_body).decode(),
             packet_digest, timestamp()),
        )
        row = full.s.one("SELECT body FROM changes WHERE id=?", (change["id"],), True)
        body = json.loads(row["body"])
        body["packet_manifest"] = [
            {"id": "PACKET-IMPACT-PRESENT", "digest": packet_digest, "ordinal": 0},
            {"id": "PACKET-IMPACT-MISSING", "digest": "0" * 64, "ordinal": 1},
        ]
        body["packet_count"] = 2
        full.s.execute("UPDATE changes SET body=? WHERE id=?", (canonical(body).decode(), change["id"]))

    context = _context(full, fixture)
    impact = context["impact_inventory"]["changes"][0]
    assert impact["status"] == "unknown"
    assert impact["review_packets"]["complete"] is False
    assert impact["review_packets"]["present"] == [{"id": "PACKET-IMPACT-PRESENT", "digest": packet_digest, "ordinal": 0}]
    assert impact["review_packets"]["missing"] == [{"id": "PACKET-IMPACT-MISSING", "digest": "0" * 64, "ordinal": 1}]
    assert any(item["code"] == "impact_review_packet_missing" for item in impact["unresolved"])
    denominator = derive_denominator(context)
    impacted = [item for item in denominator["obligations"] if item["category"] == "impacted_target"]
    assert len(impacted) == 2  # root artifact and the fixture Task
    assert not any("PACKET-IMPACT" in item["pointer"] or "packet-leaf" in item["pointer"] for item in impacted)


def test_i4_partial_baseline_identity_and_wrong_program_are_unknown(full):
    fixture = _base(full)
    change = _change(full, fixture)
    with full.s.transaction():
        row = full.s.one("SELECT body FROM changes WHERE id=?", (change["id"],), True)
        body = json.loads(row["body"])
        body["impact"]["artifact_refs"] = []
        full.s.execute("UPDATE changes SET body=? WHERE id=?", (canonical(body).decode(), change["id"]))
    context = _context(full, fixture)
    impact = context["impact_inventory"]["changes"][0]
    assert impact["status"] == "unknown"
    assert any(item["code"] in {"impact_target_identity_missing", "impact_target_identity_mismatch"}
               for item in impact["unresolved"])

    other = _base(full)
    other_change = _change(full, other)
    with full.s.transaction():
        row = full.s.one("SELECT body FROM changes WHERE id=?", (other_change["id"],), True)
        body = json.loads(row["body"])
        body["program"] = "PROGRAM-OTHER"
        full.s.execute("UPDATE changes SET body=? WHERE id=?", (canonical(body).decode(), other_change["id"]))
    other_context = _context(full, other)
    other_impact = other_context["impact_inventory"]["changes"][0]
    assert other_impact["status"] == "unknown"
    assert any(item["code"] == "impact_program_mismatch" for item in other_impact["unresolved"])
