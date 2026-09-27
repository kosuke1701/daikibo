"""Finite regression probes for the Unit 2c-4 packet/history contract."""
import json

from daikibo.common import canonical, digest, timestamp

from test_e3_unit2c4_impact import _add_child, _base, _change, _context


def test_packet_set_with_consistent_foreign_coverage_is_unknown(full):
    fixture = _base(full)
    change = _change(full, fixture)
    packet = {"format": "impact.packet.v1", "leaf_manifest": ["unrelated-leaf"],
              "required_coverage": ["unrelated"]}
    packet_digest = digest(packet)
    with full.s.transaction():
        full.s.execute(
            "INSERT INTO breakdown_packets VALUES(?,?,?,?,?)",
            ("PACKET-rerepair-unrelated", fixture["project"], canonical(packet).decode(),
             packet_digest, timestamp()),
        )
        body = json.loads(full.s.one("SELECT body FROM changes WHERE id=?", (change["id"],))["body"])
        body["packet_manifest"] = [{"id": "PACKET-rerepair-unrelated",
                                    "digest": packet_digest, "ordinal": 0}]
        body["packet_count"] = 1
        full.s.execute("UPDATE changes SET body=? WHERE id=?",
                       (canonical(body).decode(), change["id"]))
    impact = _context(full, fixture)["impact_inventory"]["changes"][0]
    assert impact["status"] == "unknown"
    assert impact["review_packets"]["complete"] is not True
    assert impact["review_packets"]["coverage"]["status"] == "unknown"


def test_baseline_nonexistent_version_is_unknown(full):
    fixture = _base(full)
    change = _change(full, fixture)
    with full.s.transaction():
        body = json.loads(full.s.one("SELECT body FROM changes WHERE id=?", (change["id"],))["body"])
        body["impact"]["artifact_refs"][0]["revision"] = 999
        body["impact"]["artifact_refs"][0]["body_digest"] = "0" * 64
        full.s.execute("UPDATE changes SET body=? WHERE id=?",
                       (canonical(body).decode(), change["id"]))
    impact = _context(full, fixture)["impact_inventory"]["changes"][0]
    assert impact["status"] == "unknown"
    assert any(item["code"] == "impact_target_history_unknown"
               for item in impact["unresolved"])


def test_valid_reduced_historical_baseline_remains_stale_not_unknown(full):
    fixture = _base(full)
    child = _add_child(full, fixture, 0)
    change = _change(full, fixture)
    with full.s.transaction():
        body = json.loads(full.s.one("SELECT body FROM changes WHERE id=?", (change["id"],))["body"])
        body["impact"]["artifacts"].remove(child["id"])
        body["impact"]["artifact_refs"] = [
            ref for ref in body["impact"]["artifact_refs"]
            if ref["artifact"] != child["id"]
        ]
        full.s.execute("UPDATE changes SET body=? WHERE id=?",
                       (canonical(body).decode(), change["id"]))
    impact = _context(full, fixture)["impact_inventory"]["changes"][0]
    assert impact["status"] != "current"
    assert child["id"] in impact["current"]["artifacts"]
    assert child["id"] in {item["id"] for item in impact["delta"]["added"]}


def test_retained_artifact_history_is_distinct_from_current_staleness(full):
    fixture = _base(full)
    _change(full, fixture)
    current = full.k.artifact(full.owner, fixture["root"]["id"])
    revised_body = {**current["body"], "statement": "The impact graph remains complete."}
    full.k._revise(full.owner, current, 1, revised_body, "history fixture", "accepted")
    impact = _context(full, fixture)["impact_inventory"]["changes"][0]
    assert impact["status"] == "stale"
    assert not any(item["code"] == "impact_target_history_unknown"
                   for item in impact["unresolved"])
    assert impact["baseline"]["artifact_refs"][0]["revision"] == 1
    assert impact["current"]["artifact_refs"][0]["revision"] == 2


def test_packet_inventory_uses_real_breakdown_accessor_when_group_is_complete(full):
    """A genuine Breakdown producer is accepted only when it covers the pin set."""
    project = full.k.create_project(full.owner, "Unit2c4 producer")['id']
    source = full.k.source(full.owner, project, "Canonical packet producer authority.")
    root = full.k.propose(full.owner, project, "requirement", {
        "title": "Impact root", "statement": "The impact graph is complete.",
        "acceptance": ["AC-IMPACT"], "source_refs": [source["id"]],
    })
    root = full.k.accept(full.owner, root["id"], 1)
    program = full.p.begin(full.owner, project, source["id"], compact=True)["program"]
    domain = full.k.propose(full.owner, project, "domain", {
        "title": "Impact domain", "statement": "Owns impact output.",
        "responsibilities": ["impact-output"], "non_responsibilities": [],
        "owned_data": ["impact-output"], "interfaces": [],
    }, artifact_id="IMPACT-DOMAIN")
    domain = full.k.accept(full.owner, domain["id"], 1)
    full.k.link(full.owner, domain["id"], root["id"], "depends_on",
                confidence="inferred", basis="packet producer fixture")
    task = full.w.create(full.owner, project, {
        "title": "Impact task", "goal": "Produce the impact result.",
        "read_artifacts": [root["id"], domain["id"]],
        "write_paths": ["impact.py"], "acceptance": ["AC-IMPACT"],
        "dependencies": [], "repos": [], "non_goals": [],
    })
    full.w.plan_tests(full.owner, task["id"], {"checks": [{
        "id": "impact-check", "argv": ["python", "-c", "print(1)"],
        "kind": "pytest", "purpose": "impact fixture",
        "required_tests": ["impact-check"],
    }]})
    task = full.w.task(full.owner, task["id"])
    plan = full.s.one("SELECT * FROM plans WHERE task=?", (task["id"],), True)
    full.rt.verification_materials.pin_test_plan(
        full.owner, project, task, plan,
        captured_from={"controller": "runtime", "operation": "pin", "capture_id": "REREPAIR"},
    )
    units = [{
        "id": "impact-unit-real", "title": "Impact", "parent": None,
        "domain": domain["id"], "rationale": "fixture",
        "obligations": [{"requirement": root["id"], "acceptance": "AC-IMPACT"}],
        "tasks": [task["id"]], "interfaces": [], "dependencies": [],
    }]
    proposal = full.breakdowns.propose(full.owner, program, "Impact producer",
                                       "fixture", units, expected_active=None,
                                       byte_budget=24000)
    fixture = {"project": project, "source": source, "root": root,
               "task": task, "program": program, "breakdown": proposal["id"]}
    change = _change(full, fixture)
    with full.s.transaction():
        row = full.s.one("SELECT body FROM changes WHERE id=?", (change["id"],))
        body = json.loads(row["body"])
        view = full.breakdowns.get(full.owner, proposal["id"])
        body["packet_manifest"] = [
            {key: packet[key] for key in ("id", "digest", "ordinal")}
            for packet in view["packets"]
        ]
        body["packet_count"] = view["packet_count"]
        full.s.execute("UPDATE changes SET body=? WHERE id=?",
                       (canonical(body).decode(), change["id"]))
    impact = _context(full, fixture)["impact_inventory"]["changes"][0]
    assert impact["review_packets"]["coverage"]["status"] == "complete"
    assert impact["review_packets"]["complete"] is True
