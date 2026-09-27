from __future__ import annotations

import copy
import sys

import pytest

from daikibo.assurance_relations import REGISTRY_V2_DIGEST
from daikibo.assurance_denominators import _delivery_declared_outputs
from daikibo.assurance import validate_assurance_rows
from daikibo.common import Fault, digest, parse_json
from test_e3_selection_contract import (
    _adopt,
    _artifact_ref,
    _fixture,
    _profile_body,
    _register_fixture_review,
    _source_ref,
)
from conftest import finish_task
from test_delivery_git_and_recovery import profile
from test_e3_unit2b_delivery_criteria import _receipt_delivery_snapshot, _run_delivery_fixture
from test_reviewed_breakdowns import leaf, make_domain, make_work
from daikibo.assurance_denominators import collect_stage_context, derive_denominator


def _profile_v3(project, program, scope, **kwargs):
    body = _profile_body(project, program, scope, **kwargs)
    body["format"] = "assurance.profile.v3"
    body["required_relation_contract_digest"] = REGISTRY_V2_DIGEST
    return body


def _replace_fixture_profile(full, fixture, *, relation_selectors=None,
                             stage_rules=None,
                             body_mutator=None,
                             reason="replace bootstrap with fixture relation profile"):
    """Replace the bootstrap profile through its canonical scope and CAS head."""
    project, program = fixture["project"], fixture["program"]
    selection = full.assurance.selected_profile(full.owner, project, program)
    selected = full.assurance.object_get(
        full.owner, project, selection["profile_ref"]["object"],
    )
    scope = {
        "scope_ref": selected["body"]["scope_ref"],
        "obligations_ref": selected["body"]["obligations_ref"],
    }
    source = full.s.one(
        "SELECT id,blob AS digest FROM sources WHERE id=? AND project=?",
        (fixture["source"], project), True,
    )
    # Preserve the bootstrap's current stage/node contract and only upgrade
    # the wire/registry and requested relation population.  Rebuilding the
    # profile from a generic test body would silently replace the observed
    # Task-plan selector and make admission correctly stale.
    body = copy.deepcopy(selected["body"])
    body["format"] = "assurance.profile.v3"
    body["required_relation_contract_digest"] = REGISTRY_V2_DIGEST
    body["previous_selection_ref"] = selection["profile_ref"]
    body["authority_refs"] = [_source_ref(project, source)]
    body["change_reason"] = reason
    if relation_selectors is not None:
        body["relation_selectors"] = list(relation_selectors)
    if stage_rules is not None:
        body["stage_rules"] = stage_rules
    if body_mutator is not None:
        body_mutator(body)
    proposal = full.assurance.profile_propose(
        full.owner, project, program, body, selection["head_event"],
    )
    _adopt(full, project, proposal, selection["head_event"])
    return proposal


def test_profile_v3_catalog_bootstrap_and_stage_gate_remains_ungated(full):
    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    catalog = full.assurance.catalog(full.owner)
    assert catalog["profile"]["format"] == "assurance.profile.v2"
    assert catalog["profiles"]["v3"]["format"] == "assurance.profile.v3"
    assert catalog["profile_v3"]["required_relation_contract_digest"] == REGISTRY_V2_DIGEST
    descriptor = full.invoke(full.owner, "api.describe", {"method": "assurance.profile_propose"})
    contract = descriptor["methods"]["assurance.profile_propose"]["body_contract"]
    assert contract["formats"] == ["assurance.profile.v2", "assurance.profile.v3", "assurance.profile.v4", "assurance.profile.v5"]
    assert contract["v3_required_relation_contract_digest"] == "REGISTRY_V2_DIGEST"
    body = _profile_v3(project, program, scope)
    proposal = full.assurance.profile_propose(full.owner, project, program, body, None)
    _adopt(full, project, proposal, None)
    selection = full.assurance.selected_profile(full.owner, project, program)
    assert selection["profile_format"] == "assurance.profile.v3"
    assert selection["effective_relation_contract_digest"] == REGISTRY_V2_DIGEST
    assert selection["stage_evaluator"] is False
    assert selection["strong_complete"] is False


def test_profile_v3_archive_rows_reuse_strict_wire_and_event_validation(full):
    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposal = full.assurance.profile_propose(
        full.owner, project, program, _profile_v3(project, program, scope), None,
    )
    _adopt(full, project, proposal, None)
    validate_assurance_rows(full.assurance.archive_rows(project), project)


@pytest.mark.parametrize("bad", [None, True, [], "unknown", "" + "0" * 64])
def test_profile_v3_digest_is_strict(full, bad):
    project, _source, _requirement, program, scope = _fixture(full)
    body = _profile_v3(project, program, scope)
    body["required_relation_contract_digest"] = bad
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(full.owner, project, program, body, None)
    assert rejected.value.code in {"invalid_registry", "invalid_profile"}


@pytest.mark.parametrize("bad", [None, True, [], {}, "assurance.profile.v9"])
def test_public_profile_format_dispatch_rejects_malformed_values_as_fault(full, bad):
    project, _source, _requirement, program, scope = _fixture(full)
    body = _profile_v3(project, program, scope)
    body["format"] = bad
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(full.owner, project, program, body, None)
    assert rejected.value.code == "invalid_profile"


def test_profile_v3_replaces_v2_only_with_previous_head_and_authority(full):
    project, source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    first = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, first, None)
    selected = full.assurance.selected_profile(full.owner, project, program)
    body = _profile_v3(
        project, program, scope, previous=selected["profile_ref"],
        authority_refs=[_source_ref(project, source)], reason="pin output registry",
    )
    proposal = full.assurance.profile_propose(
        full.owner, project, program, body, selected["head_event"],
    )
    adopted = _adopt(full, project, proposal, selected["head_event"])
    assert parse_json(adopted["event"]["body"])["registry_digest"] == REGISTRY_V2_DIGEST
    assert full.assurance.selected_profile(full.owner, project, program)["profile_format"] == "assurance.profile.v3"


def test_profile_v3_unknown_relation_is_rejected_even_when_v2_name_exists(full):
    project, _source, _requirement, program, scope = _fixture(full)
    body = _profile_v3(project, program, scope)
    body["relation_selectors"] = ["realizes", "made_up_relation"]
    with pytest.raises(Fault) as rejected:
        full.assurance.profile_propose(full.owner, project, program, body, None)
    assert rejected.value.code == "invalid_profile"


def test_delivery_declared_output_inventory_uses_pinned_declaration_and_one_producer():
    project = "PRJ-output-contract"
    snapshot = {
        "kind": "delivery_snapshot", "project": project, "delivery": "DEL-1",
        "binding_digest": "1" * 64, "snapshot_digest": "2" * 64,
        "pin": {"id": "MAT-1", "digest": "3" * 64},
    }
    check = {"id": "build", "category": "build", "produces": ["pkg"]}
    check_ref = {"kind": "delivery_check", "project": project,
                 "delivery": snapshot, "check_id": "build",
                 "check_digest": digest(check)}
    payload = {
        "snapshot": {"repos": {"repo-a": {"name": "repo-a"}}},
        "checks": [check],
        "build_definitions": [{"id": "pkg", "repo": "repo-a", "path": ".daikibo-build/pkg"}],
    }
    unresolved = []
    result = _delivery_declared_outputs(project, payload, snapshot, [check_ref], unresolved)
    assert result["status"] == "available"
    assert result["items"][0]["identity"]["category"] == "delivery_declared_output"
    assert result["items"][0]["producer_ref"] == check_ref
    assert unresolved == []


def test_delivery_declared_output_inventory_does_not_turn_ambiguous_producer_into_empty_success():
    project = "PRJ-output-contract"
    snapshot = {
        "kind": "delivery_snapshot", "project": project, "delivery": "DEL-1",
        "binding_digest": "1" * 64, "snapshot_digest": "2" * 64,
        "pin": {"id": "MAT-1", "digest": "3" * 64},
    }
    checks = [
        {"id": "build-a", "produces": ["pkg"]},
        {"id": "build-b", "produces": ["pkg"]},
    ]
    check_refs = [{"kind": "delivery_check", "project": project,
                   "delivery": snapshot, "check_id": check["id"],
                   "check_digest": digest(check)}
                  for check in checks]
    unresolved = []
    result = _delivery_declared_outputs(
        project,
        {"snapshot": {"repos": {"repo-a": {"name": "repo-a"}}},
         "checks": checks,
         "build_definitions": [{"id": "pkg", "repo": "repo-a", "path": ".daikibo-build/pkg"}]},
        snapshot, check_refs, unresolved,
    )
    assert result["status"] == "invalid"
    assert result["items"] == []
    assert any(item["code"] == "delivery_build_definition_producer_unresolved" for item in unresolved)


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"checks": []}, "unverified", "delivery_build_definitions_missing"),
        ({"build_definitions": None, "checks": []}, "invalid", "delivery_build_definitions_invalid"),
        ({"build_definitions": [], "checks": []}, "explicit_empty", None),
    ],
)
def test_declared_output_missing_null_and_explicit_empty_remain_distinct(payload, status, code):
    project = "PRJ-output-contract"
    snapshot = {
        "kind": "delivery_snapshot", "project": project, "delivery": "DEL-1",
        "binding_digest": "1" * 64, "snapshot_digest": "2" * 64,
        "pin": {"id": "MAT-1", "digest": "3" * 64},
    }
    unresolved = []
    result = _delivery_declared_outputs(project, payload, snapshot, [], unresolved)
    assert result["status"] == status
    assert result["items"] == []
    if code is None:
        assert unresolved == []
    else:
        assert any(item["code"] == code for item in unresolved)


def test_v3_delivery_denominator_counts_declarations_before_actual_output_observation(full, full_project, tmp_path):
    fixture = _run_delivery_fixture(full, full_project, tmp_path)
    project = fixture["project"]
    context = collect_stage_context(
        full, full.owner, project=project, program=fixture["program"],
        stage="delivery", proposed_breakdown=fixture["breakdown"],
        delivery=fixture["first"]["snapshot"],
    )
    denominator = derive_denominator(context)
    declared = [item for item in denominator["obligations"]
                if item["category"] == "delivery_declared_output"]
    # A v3 output profile still uses the Delivery reader's v4 context wire;
    # profile-version compatibility is independent from material collection.
    assert context["format"] == "assurance.stage-context.v4"
    assert len(declared) == len(fixture["first"]["body"]["build_definitions"])
    assert all(item["contributors"] == [] for item in declared)
    assert all(item["required_at"] == "delivery" for item in declared)
    assert all(item["source_ref"]["kind"] == "delivery_snapshot" for item in declared)


def _prepare_reviewed_program(full, full_project, tmp_path):
    project, repository, requirement_id, _root = full_project
    source = full.k.source(full.owner, project, "Delivery declaration source")
    # This source is a fixture label that points at the already accepted
    # requirement.  Record that meaning explicitly so the canonical
    # source population is complete without inventing another requirement.
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
        captured_from={"controller": "runtime", "operation": "consumer-c-profile-v3"},
    )
    units = [leaf("unit-delivery", domain, [task], requirement_id)]
    script = tmp_path / "consumer_c_breakdown_reviewer.py"
    script.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin); c=p.get('context',{})\n"
        "print(json.dumps({'verdict':'pass','rationale':'fixture protocol',"
        "'covered':c.get('required_coverage') or c.get('task',{}).get('acceptance',[]),'findings':[],"
        "'observations':[{'ref':p['subject'],'detail':'fixture'}],"
        "'dispositions':[]}))\n"
    )
    full.rt.adapters.register(
        full.owner, "consumer-c-breakdown-reviewer", "fixture", sys.executable, [str(script)],
    )
    proposed = full.breakdowns.propose(
        full.owner, program, "Consumer-C delivery", "Consumer-C declaration fixture", units,
    )
    offset = 0
    while True:
        page = full.breakdowns.get(full.owner, proposed["id"], offset, 3)
        for packet in page["packets"]:
            for role in ("design", "trace"):
                full.rt.review(full.owner, packet["id"], role, "consumer-c-breakdown-reviewer")
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    # Unit4-P admission consumes the same source population, canonical
    # profile, and current Task-plan reviews as a public new-program flow.
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
    profile_selection = _replace_fixture_profile(
        full,
        {"project": project, "program": program, "source": source["id"]},
    )
    return {
        "project": project, "repository": repository, "requirement": requirement_id,
        "source": source["id"],
        "program": program, "breakdown": proposed["id"], "task": task,
        "profile_ref": profile_selection["profile_ref"],
    }


def test_v3_delivery_denominator_keeps_two_real_declarations_with_one_failed_producer_and_archive(
    full, full_project, tmp_path,
):
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
    assert any(not item["passed"] for item in verification["results"])
    build_receipt = full.g.receipt(
        next(item["receipt"] for item in verification["results"] if item["check"] == "build")
    )
    snapshot = _receipt_delivery_snapshot(full, build_receipt)
    context = collect_stage_context(
        full, full.owner, project=project, program=fixture["program"], stage="delivery",
        proposed_breakdown=fixture["breakdown"], delivery=snapshot,
    )
    denominator = derive_denominator(context)
    declared = [
        item for item in denominator["obligations"]
        if item["category"] == "delivery_declared_output"
    ]
    assert len(declared) == 2
    assert context["delivery_material"]["declared_outputs"]["status"] == "available"
    assert len(context["delivery_material"]["declared_outputs"]["items"]) == 2
    assert any(item["passed"] is False for item in verification["results"])
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    assert full.history.inspect_archive(
        full.owner, exported["path"], exported["sha256"],
    )["verified"]
