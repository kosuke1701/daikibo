"""Portable validation for local-execution history.

The validator checks historical identity and packet continuity only.  A portable
archive never turns a recorded review receipt into a current observed PASS.
"""
from __future__ import annotations

from .common import digest, need, parse_json
from .local_executions import FORMAT, MAX_PROPOSAL_BYTES, PACKET_FORMAT, ROLES, STAGES
from .obligations import marker as obligation_marker


def _material_task_map(material):
    values = material.get("tasks", [])
    result = {}
    for item in values:
        need(isinstance(item, dict) and isinstance(item.get("task"), str),
             "invalid_archive", "Local material task identity is incomplete")
        need(item["task"] not in result, "invalid_archive", "Local material task identity is duplicated")
        need(item.get("kind") == "task" and isinstance(item.get("value"), dict)
             and item["value"].get("id") == item["task"]
             and digest(item["value"]) == item.get("digest"),
             "invalid_archive", "Local material task semantic digest differs")
        result[item["task"]] = item
    return result


def _validate_external_tasks(material, selected):
    """Check the identity of prerequisite projections retained in history."""
    external = material.get("external_tasks", [])
    need(isinstance(external, list), "invalid_archive", "Local external task history is malformed")
    seen = set(selected)
    for item in external:
        need(isinstance(item, dict) and isinstance(item.get("task"), str)
             and item.get("kind") == "external_task"
             and isinstance(item.get("value"), dict)
             and item["value"].get("id") == item["task"]
             and item["task"] not in seen
             and digest(item["value"]) == item.get("digest"),
             "invalid_archive", "Local external task semantic identity differs")
        seen.add(item["task"])


def _expected_coverage(body, material):
    stages = []
    for task in body["tasks"]:
        for stage in STAGES:
            stages.append("LEXSTAGE-" + digest([task, stage, digest(material["stage_evidence"][task][stage])]))
    obligations = [obligation_marker(pair[0], pair[1]) for pair in material["obligations"]]
    impact = ["LEXIMPACT-" + digest([item["task"], item["item_id"], item["item_digest"], item["disposition"]])
              for item in body["dispositions"]]
    return {"stages": stages, "obligations": obligations, "impact": impact}


def validate_local_executions(get, each, project):
    proposals = {}
    for row in each("local_execution_proposals"):
        body = row["body"]
        need(row["id"] not in proposals, "invalid_archive", "Duplicate local execution proposal")
        need(row["project"] == project and body["format"] == FORMAT and body["project"] == project
             and body["program"] == row["program"] and body["subplan"] == row["subplan"]
             and digest(body) == row["digest"], "invalid_archive", "Local execution proposal differs")
        program = get("programs", row["program"])
        subplan = get("subplans", row["subplan"])
        need(program["project"] == project and subplan["project"] == project
             and subplan["program"] == row["program"]
             and body["program"] == program["id"] and body["subplan"] == subplan["id"],
             "invalid_archive", "Local execution program or subplan binding differs")
        stable = {"format": FORMAT, "program": body["program"], "subplan": body["subplan"],
                  "project": body["project"], "tasks": body["tasks"], "rationale": body["rationale"],
                  "stage_evidence": body["stage_evidence"], "dispositions": body["dispositions"],
                  "material_digest": body["material_digest"], "coverage_manifest": body["coverage_manifest"],
                  "task_obligations": body["task_obligations"]}
        need(body.get("payload_digest") == digest(stable), "invalid_archive", "Local execution payload digest differs")
        proposals[row["id"]] = row

    packets_by = {ident: [] for ident in proposals}
    for row in each("local_execution_packets"):
        body = row["body"]
        need(row["project"] == project and row["proposal"] in proposals and digest(body) == row["digest"]
             and body.get("format") == PACKET_FORMAT and body.get("id") == row["id"]
             and body.get("proposal") == row["proposal"]
             and body.get("program") == proposals[row["proposal"]]["program"]
             and body.get("subplan") == proposals[row["proposal"]]["subplan"]
             and body.get("material_digest") == proposals[row["proposal"]]["body"]["material_digest"],
             "invalid_archive", "Local execution packet identity differs")
        packets_by[row["proposal"]].append(row)

    for ident, proposal in proposals.items():
        body = proposal["body"]
        packets = sorted(packets_by.get(ident, []), key=lambda x: x["ordinal"])
        need([{"id": p["id"], "digest": p["digest"]} for p in packets] == body["packet_manifest"],
             "invalid_archive", "Local execution packet manifest differs")
        need(packets and len(packets) == body["packet_count"], "invalid_archive", "Local execution packet count differs")
        cursor = 0
        fragments = []
        for ordinal, row in enumerate(packets):
            p = row["body"]
            fragment = p["serialized_fragment"]
            need(row["ordinal"] == ordinal and p.get("ordinal") == ordinal
                 and p["start"] == cursor and p["end"] == cursor + len(fragment)
                 and p["total_characters"] == body["material_characters"]
                 and p["required_coverage"]
                 and p["required_coverage"][0] == "LEXPART-" + digest([ident, body["material_digest"], p["start"], p["end"]]),
                 "invalid_archive", "Local execution packet continuity differs")
            cursor = p["end"]
            fragments.append(fragment)
        raw = "".join(fragments)
        need(cursor == body["material_characters"] and digest(raw.encode()) == body["material_digest"],
             "invalid_archive", "Local execution material is truncated")
        material = parse_json(raw, limit=MAX_PROPOSAL_BYTES)
        need(material["format"] == FORMAT and material["project"] == project and material["program"] == body["program"]
             and material["subplan"] == body["subplan"] and material["stage_evidence"] == body["stage_evidence"]
             and material["dispositions"] == body["dispositions"] and digest(material) == body["material_digest"],
             "invalid_archive", "Local execution material differs")
        task_map = _material_task_map(material)
        need(set(task_map) == set(body["tasks"]), "invalid_archive", "Local selected Task identity differs")
        _validate_external_tasks(material, task_map)
        expected_coverage = _expected_coverage(body, material)
        need(body["coverage_manifest"] == expected_coverage, "invalid_archive", "Local coverage manifest differs")
        for item in material.get("inventory", []):
            need(isinstance(item.get("id"), str) and item.get("digest"),
                 "invalid_archive", "Local inventory item is incomplete")
        inventory = {item["id"]: item for item in material.get("inventory", [])}
        need(len(inventory) == len(material.get("inventory", [])), "invalid_archive", "Local inventory has duplicate IDs")
        selected_tasks = set(body.get("tasks", []))
        seen_dispositions = set()
        for item in body["dispositions"]:
            key = (item.get("task"), item.get("item_id"))
            need(item.get("task") in selected_tasks and key not in seen_dispositions,
                 "invalid_archive", "Local disposition task identity differs")
            seen_dispositions.add(key)
            need(item["item_id"] in inventory and item["item_digest"] == inventory[item["item_id"]]["digest"],
                 "invalid_archive", "Local disposition refers to changed inventory")
        need(seen_dispositions == {(task, ident) for task in selected_tasks for ident in inventory},
             "invalid_archive", "Local disposition inventory is incomplete")
        complete_markers = set(sum(expected_coverage.values(), []))
        assigned = set()
        for row in packets:
            packet = row["body"]
            need(packet.get("coverage_manifest") == body["coverage_manifest"]
                 and packet.get("complete_manifest", {}).get("markers") == body["coverage_manifest"],
                 "invalid_archive", "Local packet coverage manifest differs")
            need(len(packet["required_coverage"]) == len(set(packet["required_coverage"]))
                 and set(packet["required_coverage"][1:]) <= complete_markers,
                 "invalid_archive", "Local packet coverage marker is unknown or duplicated")
            assigned.update(packet["required_coverage"][1:])
        need(assigned == complete_markers, "invalid_archive", "Local packet coverage is incomplete")

    records = list(each("local_execution_records"))
    record_by_id = {}
    certified = set()
    withdrawn = set()
    claims = set()
    for row in records:
        body = row["body"]
        need(row["id"] not in record_by_id, "invalid_archive", "Duplicate local execution record")
        record_by_id[row["id"]] = row
        need(row["project"] == project and row["proposal"] in proposals and digest(body) == row["digest"]
             and body.get("kind") == row["kind"] and body.get("proposal") == row["proposal"]
             and "task" in body and "epoch" in body
             and body.get("task") == row["task"] and body.get("epoch") == row["epoch"],
             "invalid_archive", "Local execution record differs")
        need(row["kind"] in {"certified", "withdrawn", "claimed", "invalidated"},
             "invalid_archive", "Unknown local execution record")
        proposal_body = proposals[row["proposal"]]["body"]
        if row["kind"] in {"certified", "withdrawn"}:
            need(row["task"] is None and row["epoch"] is None,
                 "invalid_archive", "Local certification/withdrawal cannot bind a task epoch")
            need(body.get("material_digest") == proposal_body["material_digest"],
                 "invalid_archive", "Local record material differs")
            if row["kind"] == "certified":
                need(row["proposal"] not in certified, "invalid_archive", "Duplicate local certification")
                certified.add(row["proposal"])
                expected_packets = {x["id"] for x in proposal_body["packet_manifest"]}
                need(body.get("proposal_digest") == proposals[row["proposal"]]["digest"]
                     and body.get("packet_manifest") == proposal_body["packet_manifest"]
                     and body.get("tasks") == proposal_body["tasks"],
                     "invalid_archive", "Local certification packet manifest differs")
                reviews = body.get("reviews")
                need(isinstance(reviews, list) and len(reviews) == len(expected_packets) * len(ROLES),
                     "invalid_archive", "Local certification review history is incomplete")
                review_keys = set()
                review_runs = set()
                review_receipts = set()
                packet_digest = {item["id"]: item["digest"] for item in proposal_body["packet_manifest"]}
                for review in reviews:
                    need(isinstance(review, dict), "invalid_archive", "Local certification review reference is malformed")
                    need(isinstance(review.get("packet"), str) and bool(review.get("packet"))
                         and isinstance(review.get("role"), str),
                         "invalid_archive", "Local certification review identity is malformed")
                    key = (review.get("packet"), review.get("role"))
                    need(key not in review_keys and key[0] in expected_packets and key[1] in ROLES
                         and isinstance(review.get("receipt"), str) and bool(review.get("receipt"))
                         and isinstance(review.get("run"), str) and bool(review.get("run"))
                         and review.get("binding") == packet_digest[key[0]]
                         and review["run"] not in review_runs and review["receipt"] not in review_receipts,
                         "invalid_archive", "Local certification review reference is incomplete")
                    review_keys.add(key)
                    review_runs.add(review["run"])
                    review_receipts.add(review["receipt"])
                need(review_keys == {(packet, role) for packet in expected_packets for role in ROLES},
                     "invalid_archive", "Local certification review roles are incomplete")
                need(body.get("review_digest") == digest(reviews), "invalid_archive", "Local certification review digest differs")
            else:
                need(row["proposal"] not in withdrawn, "invalid_archive", "Duplicate local withdrawal")
                withdrawn.add(row["proposal"])
                need(body.get("proposal_digest") == proposals[row["proposal"]]["digest"],
                     "invalid_archive", "Local withdrawal proposal digest differs")
        else:
            need(row["task"] is not None and type(row["epoch"]) is int and row["epoch"] >= 0
                 and body.get("task") == row["task"] and body.get("epoch") == row["epoch"],
                 "invalid_archive", "Local claim task/epoch differs")
            need(body.get("material_digest") == proposal_body["material_digest"],
                 "invalid_archive", "Local claim material differs")
            certification = body.get("certified_event")
            need(isinstance(certification, dict) and isinstance(certification.get("id"), str)
                 and certification.get("digest") == body.get("certification_digest"),
                 "invalid_archive", "Local claim certification reference is incomplete")
            packet_material = "".join(p["body"]["serialized_fragment"]
                                      for p in sorted(packets_by[row["proposal"]], key=lambda x: x["ordinal"]))
            proposal_material = parse_json(packet_material, limit=MAX_PROPOSAL_BYTES)
            task_item = next((item for item in proposal_material.get("tasks", [])
                              if item.get("task") == row["task"]), None)
            need(task_item is not None and body.get("task_semantic_digest") == task_item.get("digest"),
                 "invalid_archive", "Local claim task semantic digest differs")
            key = (row["proposal"], row["task"], row["epoch"], row["kind"])
            need(key not in claims, "invalid_archive", "Duplicate local task epoch event")
            claims.add(key)

    for row in records:
        if row["kind"] not in {"claimed", "invalidated"}:
            continue
        reference = row["body"]["certified_event"]
        cert = record_by_id.get(reference["id"])
        need(cert is not None and cert["proposal"] == row["proposal"] and cert["kind"] == "certified"
             and cert["digest"] == reference["digest"],
             "invalid_archive", "Local claim certification is dangling")
    return {"local_execution_proposals": len(proposals),
            "local_execution_packets": sum(len(v) for v in packets_by.values()),
            "local_execution_records": len(records)}
