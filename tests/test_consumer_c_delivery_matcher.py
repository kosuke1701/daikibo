from __future__ import annotations

from daikibo.assurance_delivery import match_live_delivery_declared_outputs
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_relations import REGISTRY_V2_DIGEST
from daikibo.common import canonical, parse_json
from conftest import finish_task
from test_delivery_git_and_recovery import profile
from test_e3_consumer_c_profile_v3 import (
    _prepare_reviewed_program,
    _replace_fixture_profile,
)
from test_e3_unit2b_delivery_criteria import _receipt_delivery_snapshot


def _fixture_with_failed_declared_output(full, full_project, tmp_path):
    fixture = _prepare_reviewed_program(full, full_project, tmp_path)
    project = fixture["project"]
    body = profile(project, fixture["repository"], fixture["requirement"], fixture["task"])
    body["build_outputs"] = [
        {"id": output_id, "repo": fixture["repository"], "path": f".daikibo-build/{output_id}"}
        for output_id in ("a", "b")
    ]
    body["checks"][0].update(
        argv=[
            "python", "-c",
            "from pathlib import Path; Path('.daikibo-build').mkdir(exist_ok=True); "
            "Path('.daikibo-build/a').write_text('a')",
        ],
        produces=["a"],
    )
    body["checks"].insert(1, {
        "id": "build-b", "category": "build", "repo": fixture["repository"],
        "kind": "command", "argv": ["python", "-c", "raise SystemExit(1)"],
        "purpose": "actual failed producer", "produces": ["b"],
    })
    body["checks"][2]["uses"] = ["a", "b"]
    full.d.configure(full.owner, project, body)
    finish_task(full, project, fixture["task"])
    full.breakdowns.activate(full.owner, fixture["breakdown"])
    delivery_id = full.d.prepare(full.owner, project)["id"]
    verification = full.d.verify(full.owner, delivery_id)
    _delivery_row, delivery_body = full.d.current(delivery_id)
    assert len(delivery_body["build_definitions"]) == 2
    assert len(delivery_body["build_outputs"]) == 1
    failed = [item for item in verification["results"] if item["check"] == "build-b"]
    assert len(failed) == 1 and failed[0]["passed"] is False
    build_receipt = next(item["receipt"] for item in verification["results"] if item["check"] == "build")
    snapshot = _receipt_delivery_snapshot(full, full.g.receipt(build_receipt))

    _replace_fixture_profile(
        full, fixture, relation_selectors=["contains", "produced_by", "realizes"],
    )
    context = collect_stage_context(
        full, full.owner, project=project, program=fixture["program"],
        stage="delivery", proposed_breakdown=fixture["breakdown"], delivery=snapshot,
    )
    denominator = derive_denominator(context)
    return fixture, denominator, snapshot, build_receipt


def test_live_declared_output_match_keeps_failed_producer_and_pins_successful_output(
    full, full_project, tmp_path,
):
    fixture, denominator, snapshot, build_receipt = _fixture_with_failed_declared_output(
        full, full_project, tmp_path,
    )
    project = fixture["project"]
    pinned = full.assurance.pin(
        full.owner, project,
        {"kind": "output_artifact", "delivery": snapshot, "check_id": "build",
         "receipt": build_receipt, "output_id": "a"},
    )
    assert pinned["ref"]["output_id"] == "a"
    result = match_live_delivery_declared_outputs(full, full.owner, denominator)
    assert result["registry_digest"] == REGISTRY_V2_DIGEST
    assert result["semantic_status"] == "unverified"
    assert result["mechanical_state"] == "failed"
    by_id = {item["output_id"]: item for item in result["obligations"]}
    assert by_id["a"]["mechanical_state"] == "eligible"
    assert by_id["a"]["membership"][0]["relation"] == "delivery_build_output"
    assert by_id["b"]["mechanical_state"] == "failed"
    assert any(item["code"] == "producer_execution_failed" for item in by_id["b"]["diagnostics"])
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    assert full.history.inspect_archive(
        full.owner, exported["path"], exported["sha256"],
    )["verified"]


def test_live_declared_output_match_keeps_observed_output_unverified_until_pinned(
    full, full_project, tmp_path,
):
    fixture, denominator, _snapshot, _build_receipt = _fixture_with_failed_declared_output(
        full, full_project, tmp_path,
    )
    result = match_live_delivery_declared_outputs(full, full.owner, denominator)
    by_id = {item["output_id"]: item for item in result["obligations"]}
    assert by_id["a"]["mechanical_state"] == "unverified"
    assert any(item["code"] == "output_material_missing_evidence" for item in by_id["a"]["diagnostics"])
    assert by_id["b"]["mechanical_state"] == "failed"


def test_foreign_failed_receipt_is_unverified_before_failure_classification(
    full, full_project, tmp_path,
):
    """A retained failed receipt cannot stand in for another producer check."""
    fixture, denominator, snapshot, _build_receipt = _fixture_with_failed_declared_output(
        full, full_project, tmp_path,
    )
    delivery_id = snapshot["delivery"]
    _row, body = full.d.current(delivery_id)
    failed = next(item for item in body["results"] if item["check"] == "build-b")
    target = next(item for item in body["results"] if item["check"] == "build")
    assert full.g.receipt(failed["receipt"])["exit_code"] == 1
    target["receipt"] = failed["receipt"]
    target["passed"] = False
    full.s.execute(
        "UPDATE deliveries SET body=? WHERE id=?",
        (canonical(body).decode(), delivery_id),
    )
    result = match_live_delivery_declared_outputs(full, full.owner, denominator)
    output = next(item for item in result["obligations"] if item["output_id"] == "a")
    assert output["mechanical_state"] == "unverified"
    assert any(
        item["code"] == "producer_observed_definition_mismatch"
        for item in output["diagnostics"]
    )


def test_receipt_from_another_delivery_snapshot_is_unverified(
    full, full_project, tmp_path,
):
    """A valid receipt from another Delivery cannot satisfy this snapshot."""
    fixture, denominator, snapshot, _build_receipt = _fixture_with_failed_declared_output(
        full, full_project, tmp_path,
    )
    second = full.d.prepare(full.owner, fixture["project"])
    second_verification = full.d.verify(full.owner, second["id"])
    foreign = next(
        item for item in second_verification["results"] if item["check"] == "build-b"
    )
    _row, body = full.d.current(snapshot["delivery"])
    target = next(item for item in body["results"] if item["check"] == "build")
    assert full.g.receipt(foreign["receipt"])["exit_code"] == 1
    target["receipt"] = foreign["receipt"]
    target["passed"] = False
    full.s.execute(
        "UPDATE deliveries SET body=? WHERE id=?",
        (canonical(body).decode(), snapshot["delivery"]),
    )
    result = match_live_delivery_declared_outputs(full, full.owner, denominator)
    output = next(item for item in result["obligations"] if item["output_id"] == "a")
    assert output["mechanical_state"] == "unverified"


def test_missing_failed_execution_material_is_unverified_without_output_material(
    full, full_project, tmp_path,
):
    """A genuine exit-1 run needs execution material, but no output material."""
    fixture, denominator, snapshot, _build_receipt = _fixture_with_failed_declared_output(
        full, full_project, tmp_path,
    )
    _row, body = full.d.current(snapshot["delivery"])
    failed = next(item for item in body["results"] if item["check"] == "build-b")
    receipt = full.g.receipt(failed["receipt"])
    pin = receipt["verification_material"]
    material = full.s.one(
        "SELECT body FROM assurance_objects WHERE id=?", (pin["id"],), True,
    )
    material_payload = parse_json(material["body"])["payload_blob"]
    full.s.blob_path(material_payload).unlink()
    result = match_live_delivery_declared_outputs(full, full.owner, denominator)
    output = next(item for item in result["obligations"] if item["output_id"] == "b")
    assert output["mechanical_state"] == "unverified"


def test_malformed_delivery_result_is_unverified_after_observation_resolution(
    full, full_project, tmp_path,
):
    """Malformed delivery telemetry cannot create a mechanical failure."""
    fixture, denominator, snapshot, _build_receipt = _fixture_with_failed_declared_output(
        full, full_project, tmp_path,
    )
    _row, body = full.d.current(snapshot["delivery"])
    failed = next(item for item in body["results"] if item["check"] == "build-b")
    failed["passed"] = "false"
    full.s.execute(
        "UPDATE deliveries SET body=? WHERE id=?",
        (canonical(body).decode(), snapshot["delivery"]),
    )
    result = match_live_delivery_declared_outputs(full, full.owner, denominator)
    output = next(item for item in result["obligations"] if item["output_id"] == "b")
    assert output["mechanical_state"] == "unverified"
    assert any(
        item["code"] == "producer_result_status_missing"
        for item in output["diagnostics"]
    )


def test_absent_delivery_observation_remains_missing(full, full_project, tmp_path):
    """A declaration with no retained result is missing, not a failed run."""
    fixture, denominator, snapshot, _build_receipt = _fixture_with_failed_declared_output(
        full, full_project, tmp_path,
    )
    _row, body = full.d.current(snapshot["delivery"])
    body["results"] = [
        item for item in body["results"] if item.get("check") != "build-b"
    ]
    full.s.execute(
        "UPDATE deliveries SET body=? WHERE id=?",
        (canonical(body).decode(), snapshot["delivery"]),
    )
    result = match_live_delivery_declared_outputs(full, full.owner, denominator)
    output = next(item for item in result["obligations"] if item["output_id"] == "b")
    assert output["mechanical_state"] == "missing"
    assert output["producer_observation"]["status"] == "missing"
    assert any(
        item["code"] == "producer_observation_missing"
        for item in output["diagnostics"]
    )
