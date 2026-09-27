from __future__ import annotations

import sys

from daikibo.assurance import SET_UNIVERSAL_CRITERIA
from daikibo.assurance_criteria import _observed_definition_ref, evaluate_criteria
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_relations import REGISTRY_DIGEST, registry_entry
from daikibo.common import digest, parse_json
from conftest import finish_task
from test_delivery_git_and_recovery import profile
from test_reviewed_breakdowns import leaf, make_domain, make_work
from test_e3_unit2b_criteria_authority import _review, _scope


def _observed_ref(project: str, receipt: dict) -> dict:
    return {
        "kind": "observed_result", "project": project,
        "receipt": receipt["id"], "run": receipt["run"],
        "receipt_digest": digest(receipt), "run_binding": receipt["binding"],
        "snapshot_digest": receipt["snapshot"],
        "result_digest": digest(receipt["result"]),
    }


def _receipt_delivery_snapshot(full, receipt: dict) -> dict:
    pin = receipt["verification_material"]
    row = full.s.one(
        "SELECT * FROM assurance_objects WHERE id=?", (pin["id"],), True,
    )
    body = parse_json(row["body"])
    payload = parse_json(full.s.blob_get(body["payload_blob"]))
    return payload["definition_ref"]["delivery"]


def _edge(full, project: str, scope: dict, source: dict, target: dict,
          obligation: dict, logical_id: str) -> dict:
    body = {
        "format": "assurance.edge.v1", "project": project,
        "source_ref": source, "target_ref": target,
        "relation": "execution_of", "relation_contract_digest": REGISTRY_DIGEST,
        "scope_ref": scope["scope_ref"], "claim": "delivery execution fixture",
        "obligation_ids": [obligation["id"]],
        "required_evidence_refs": [], "authority_refs": [],
    }
    return full.assurance.store_object(
        full.owner, project, "edge", logical_id, 1, body,
    )


def _requirements(relation: str) -> list[str]:
    return sorted(SET_UNIVERSAL_CRITERIA | set(registry_entry(relation)["set_checks"]))


def _delivery_denominator(full, project: str, program: str, breakdown: str,
                         delivery_ref: dict) -> dict:
    context = collect_stage_context(
        full, full.owner, project=project, program=program, stage="delivery",
        proposed_breakdown=breakdown, delivery=delivery_ref,
    )
    return derive_denominator(context)


def _run_delivery_fixture(full, full_project, tmp_path):
    project, repository, requirement_id, _root = full_project
    source = full.k.source(full.owner, project, "Delivery criterion source")
    # This is a fixture label referring to the accepted requirement, not a
    # second requirement source.  Preserve that distinction in the source
    # population consumed by canonical Unit4-P admission.
    full.k.classify(
        full.owner, source["id"], 0, source["characters"], "reference",
        [requirement_id],
        "Fixture delivery label references the existing accepted requirement; no additional requirement text",
    )
    program = full.p.begin(full.owner, project, source["id"], compact=True)["program"]
    domain = make_domain(full, project)
    task = make_work(full, project, repository, requirement_id, domain)
    full.w.ready(full.owner, task)
    task_row = full.w.task(full.owner, task)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    full.rt.verification_materials.pin_test_plan(
        full.owner, project, task_row, plan_row,
        captured_from={"controller": "runtime", "operation": "delivery-criterion"},
    )
    units = [leaf("unit-delivery", domain, [task], requirement_id)]
    script = tmp_path / "delivery_breakdown_reviewer.py"
    script.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin); c=p.get('context',{})\n"
        "print(json.dumps({'verdict':'pass','rationale':'fixture protocol',"
        "'covered':c.get('required_coverage') or c.get('task',{}).get('acceptance',[]),'findings':[],"
        "'observations':[{'ref':p['subject'],'detail':'fixture'}],"
        "'dispositions':[]}))\n"
    )
    full.rt.adapters.register(
        full.owner, "delivery-breakdown-markers", "fixture", sys.executable, [str(script)],
    )
    proposed = full.breakdowns.propose(
        full.owner, program, "Delivery criterion", "Delivery criterion fixture", units,
    )
    offset = 0
    while True:
        page = full.breakdowns.get(full.owner, proposed["id"], offset, 3)
        for packet in page["packets"]:
            for role in ("design", "trace"):
                full.rt.review(full.owner, packet["id"], role, "delivery-breakdown-markers")
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    from test_reviewed_breakdowns import _bootstrap_mandatory_profile
    full.rt.adapters.register(
        full.owner, "markers", "fixture", sys.executable, [str(script)],
    )
    for source_row in full.s.all("SELECT id FROM sources WHERE project=? ORDER BY id", (project,)):
        partition = full.traceability.propose(
            full.owner, project, kind="document", scope={"source": source_row["id"]},
        )
        full.traceability.extract(full.owner, partition["id"])
    _bootstrap_mandatory_profile(full, project, program, [requirement_id], [task])
    from test_e3_consumer_c_profile_v3 import _replace_fixture_profile
    profile_selection = _replace_fixture_profile(
        full,
        {"project": project, "program": program, "source": source["id"]},
        relation_selectors=["contains", "execution_of", "produced_by", "realizes"],
    )
    breakdown = proposed["id"]
    full.d.configure(full.owner, project, profile(project, repository, requirement_id, task))
    finish_task(full, project, task)
    full.breakdowns.activate(full.owner, breakdown)
    first = full.d.prepare(full.owner, project)["id"]
    first_results = full.d.verify(full.owner, first)
    assert all(item["passed"] for item in first_results["results"]), first_results
    first_row, first_body = full.d.current(first)
    first_receipt = full.g.receipt(first_results["results"][0]["receipt"])
    first_snapshot = _receipt_delivery_snapshot(full, first_receipt)

    second = full.d.prepare(full.owner, project)["id"]
    second_results = full.d.verify(full.owner, second)
    assert all(item["passed"] for item in second_results["results"]), second_results
    second_row, second_body = full.d.current(second)
    second_receipt = full.g.receipt(second_results["results"][0]["receipt"])
    second_snapshot = _receipt_delivery_snapshot(full, second_receipt)
    return {
        "project": project, "requirement": full.k.artifact(full.owner, requirement_id),
        "source": source["id"],
        "program": program, "breakdown": breakdown,
        "profile_ref": profile_selection["profile_ref"],
        "first": {"row": first_row, "body": first_body, "snapshot": first_snapshot,
                   "receipt": first_receipt},
        "second": {"row": second_row, "body": second_body, "snapshot": second_snapshot,
                    "receipt": second_receipt},
    }


def test_delivery_execution_edge_uses_public_resolver_and_keeps_delivery_identity(full, full_project, tmp_path):
    fixture = _run_delivery_fixture(full, full_project, tmp_path)
    project = fixture["project"]
    first = fixture["first"]
    second = fixture["second"]
    first_denominator = _delivery_denominator(
        full, project, fixture["program"], fixture["breakdown"], first["snapshot"],
    )
    # Reuse the selected profile's immutable scope.  A second equivalent
    # scope proposal would collide with the canonical scope head and would
    # also make the public edge refer to a non-selected population.
    scope = {"scope_ref": fixture["profile_ref"]}
    source = _observed_ref(project, first["receipt"])
    material_ref, runtime_check, material_reason = _observed_definition_ref(
        full, full.owner, source, first["receipt"],
    )
    assert material_reason is None, material_reason
    first_obligation = next(
        item for item in first_denominator["obligations"]
        if item["category"] == "delivery_check"
        and item["source_ref"]["check_id"] == material_ref["check_id"]
        and item["source_ref"]["check_digest"] == material_ref["check_digest"]
    )
    first_check = first_obligation["source_ref"]
    assert material_ref == first_check, {"material": material_ref, "target": first_check,
                                         "runtime": runtime_check}
    # This is the public endpoint that the criterion must use before matching
    # the execution edge, rather than a producer-only helper.
    resolved = full.assurance.resolve_pinned(full.owner, first_check)
    assert resolved["resolution"]["content"]["id"] == first_check["check_id"]
    edge = _edge(full, project, scope, source, first_check, first_obligation,
                 "edge-unit2b-delivery-same")
    result = evaluate_criteria(
        relation="execution_of", requirements=_requirements("execution_of"),
        denominator=first_denominator, edges=[edge],
        validated_reviews=_review(full, {"project": project, "parent": fixture["requirement"]}),
    )
    assert first_obligation["id"] in result["criteria"]["all_required_checks"]["observed_ids"], {
        "checks": result["criteria"]["all_required_checks"],
        "diagnostics": result["capabilities"]["adapter_diagnostics"],
    }

    # The second Delivery has the same check id and body, but its snapshot
    # binding is a different typed identity.  A first Delivery receipt must
    # never satisfy that second Delivery obligation by name/content alone.
    second_denominator = _delivery_denominator(
        full, project, fixture["program"], fixture["breakdown"], second["snapshot"],
    )
    second_obligation = next(
        item for item in second_denominator["obligations"]
        if item["category"] == "delivery_check"
        and item["source_ref"]["check_id"] == first_check["check_id"]
        and item["source_ref"]["check_digest"] == first_check["check_digest"]
    )
    assert second_obligation["source_ref"]["check_id"] == first_check["check_id"]
    foreign_edge = _edge(
        full, project, scope, source, second_obligation["source_ref"],
        second_obligation, "edge-unit2b-delivery-foreign",
    )
    foreign_result = evaluate_criteria(
        relation="execution_of", requirements=_requirements("execution_of"),
        denominator=second_denominator, edges=[foreign_edge],
        validated_reviews=_review(full, {"project": project, "parent": fixture["requirement"]}),
    )
    assert second_obligation["id"] not in foreign_result["criteria"]["all_required_checks"]["observed_ids"]
    assert any(
        item.get("reason") == "observed_definition_identity_mismatch"
        for item in foreign_result["capabilities"]["adapter_diagnostics"]
    )
