"""Same-project Consumer-P to Consumer-M/R closure over real runtime material."""
from __future__ import annotations

import copy
import json
import sys

import pytest

from daikibo.assurance_criteria import (
    build_relation_request,
    build_review_assurance,
    evaluate_criteria,
)
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.assurance_node_reviews import build_node_requests, select_node_reviews
from daikibo.assurance_relations import REGISTRY_DIGEST
from daikibo.artifact_provenance import resolve_produced_artifact
from daikibo.common import Fault, canonical, digest, timestamp, uid
from daikibo.task_revisions import task_definition_digest

from test_consumer_p_artifact_provenance import _artifact_ref, _run_collect
from test_e3_consumer_mr import _requirements


def _breakdown_for_collected_task(full, project: str, program: str,
                                  requirement: str, task: str) -> str:
    body = {
        "format": "daikibo.breakdown.v1", "program": program,
        "title": "P to M/R closure", "rationale": "actual production output",
        "units": [{
            "id": "unit-p", "title": "produced output", "parent": None,
            "domain": None, "rationale": "actual runtime output",
            "obligations": [{"requirement": requirement, "acceptance": "AC-ADD"}],
            "tasks": [task], "interfaces": [], "dependencies": [],
        }],
        "scope": full.breakdowns._scope(full.owner, project),
        "structure": {}, "material_bindings": {},
    }
    ident = "BREAKDOWN-p-mr-integration"
    full.s.execute(
        "INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)",
        (ident, program, project, canonical(body).decode(), digest(body),
         "proposed", None, timestamp()),
    )
    return ident


def _task_ref(full, project: str, task: str) -> dict:
    row = full.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (task, project), True)
    body = json.loads(row["body"])
    return {
        "kind": "task_revision", "project": project, "task": task,
        "revision": row["revision"], "definition_digest": task_definition_digest(body),
    }


def _material_payload(full, material_id: str) -> tuple[dict, dict]:
    row = full.s.one("SELECT * FROM assurance_objects WHERE id=?", (material_id,), True)
    envelope = json.loads(row["body"])
    return envelope, json.loads(full.s.blob_get(envelope["payload_blob"]))


def _actual_review_adapter(full, tmp_path):
    script = tmp_path / "p_mr_review.py"
    script.write_text(
        "import json,sys\n"
        "packet=json.load(sys.stdin)\n"
        "context=packet.get('context', {})\n"
        "print(json.dumps({'verdict':'pass','rationale':'actual subprocess fixture',"
        "'covered':context.get('required_coverage') or context.get('task',{}).get('acceptance',[]),'findings':[],"
        "'observations':[{'ref':packet['subject'],'detail':'fixture read context'}],"
        "'dispositions':[]}))\n"
    )
    full.rt.adapters.register(
        full.owner, "p-mr-markers", "fixture", sys.executable, [str(script)],
    )

    def review_adopt(project: str, root: dict) -> None:
        refs = []
        for packet, role in full.assurance._review_requirements(
                project, full.assurance._adoption_roots(project, root)):
            receipt = full.rt.review(full.owner, packet["id"], role, "p-mr-markers")
            refs.append({"packet": packet["id"], "role": role, "id": receipt["receipt"]})
        full.assurance.adopt(
            full.owner, project, root["id"], root["digest"], None, refs,
        )

    return review_adopt


def _build_actual_flow(full, full_project, tmp_path):
    project, repository, requirement, _root = full_project
    project, repository, task, executed, collected = _run_collect(full, full_project)
    task_row = full.w.task(full.owner, task)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    full.rt.verification_materials.pin_test_plan(
        full.owner, project, task_row, plan_row,
        captured_from={"controller": "runtime", "operation": "p-mr-test-plan", "capture_id": uid("CAP")},
    )
    artifact = collected["artifacts"][0]["artifact"]
    artifact_ref = {
        "kind": "artifact", "project": project, "artifact": artifact["id"],
        "revision": artifact["revision"], "body_digest": artifact["digest"],
    }
    task_ref = _task_ref(full, project, task)
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    breakdown = _breakdown_for_collected_task(full, project, program, requirement, task)
    context = collect_stage_context(
        full, full.owner, project=project, program=program, stage="plan",
        proposed_breakdown=breakdown,
    )
    denominator = derive_denominator(context)
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [_artifact_ref(full, project, requirement)], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    profile = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope["scope_ref"], "stage_rules": {"plan": {}},
         "relation_selectors": ["produced_by"], "test_definition_bindings": []},
    )
    review_adopt = _actual_review_adapter(full, tmp_path)
    review_adopt(project, profile["profile"])
    request = build_relation_request(
        full, full.owner, context=context, denominator=denominator,
        relation="produced_by", center_ref=task_ref, direction="incoming",
        scope_ref=profile["profile_ref"], registry_digest=REGISTRY_DIGEST,
    )
    required = [
        item for item in denominator["obligations"]
        if item["id"] in request["required_obligation_ids"]
    ]
    assert len(required) == 1
    old_scope_obligation = scope["obligations"]["body"]["obligations"][0]["id"]
    edge = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": artifact_ref, "target_ref": task_ref,
         "relation": "produced_by", "scope_ref": profile["profile_ref"],
         "claim": "The runtime-produced finding belongs to this Task output",
         "obligation_ids": [old_scope_obligation],
         "required_evidence_refs": [], "authority_refs": []},
    )
    review_adopt(project, edge["edge"])
    relation_set = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": task_ref, "relation": "produced_by", "direction": "incoming",
         "scope_ref": profile["profile_ref"], "criteria": {},
         "required_evidence_refs": []},
    )
    review_adopt(project, relation_set["set"])
    # N is the real task-plan Runtime review.  The output draft has no N
    # request: its P material is the M provenance evidence.
    node_requests = build_node_requests(
        full, full.owner, project=project,
        selectors=[{"selector": "test_plan", "node_ref": task_ref}],
    )
    full.rt.review(
        full.owner, task, "test_plan", "p-mr-markers",
        proposal=json.loads(plan_row["body"]),
    )
    node_reviews = select_node_reviews(full, full.owner, node_requests=node_requests)
    relation_reviews = build_review_assurance(
        full, full.owner, relation_request=request,
        set_ref=full.assurance._object_ref(relation_set["set"]),
    )
    return {
        "project": project, "repository": repository, "requirement": requirement,
        "task": task, "executed": executed, "collected": collected,
        "artifact": artifact, "artifact_ref": artifact_ref, "task_ref": task_ref,
        "context": context, "denominator": denominator, "scope": scope,
        "profile": profile, "request": request, "required": required,
        "edge": edge, "relation_set": relation_set,
        "node_reviews": node_reviews, "relation_reviews": relation_reviews,
    }


def test_p_collect_to_mr_uses_actual_material_and_runtime_nes(full, full_project, tmp_path):
    flow = _build_actual_flow(full, full_project, tmp_path)
    result = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[flow["edge"]["edge"]],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert result["status"] == "satisfied", result
    assert result["criteria"]["all_declared_outputs"]["status"] == "satisfied"
    assert result["criteria"]["all_obligations_covered"]["status"] == "satisfied"
    assert result["criteria"]["meaning_review"]["status"] == "satisfied"
    envelope, payload = _material_payload(
        full, flow["collected"]["artifacts"][0]["material"]["id"],
    )
    assert envelope["material_kind"] == "artifact_production"
    assert payload["artifact_ref"] == flow["artifact_ref"]
    assert payload["candidate_ref"]["candidate"] == flow["executed"]["candidate"]
    assert flow["artifact"]["status"] == "draft"


def test_p_mr_missing_material_is_unverified_without_accepting_draft(full, full_project, tmp_path):
    flow = _build_actual_flow(full, full_project, tmp_path)
    missing = full.k.propose(
        full.owner, flow["project"], "finding",
        {"title": "Unmaterialized", "statement": "No producer packet"},
    )
    missing_ref = {
        "kind": "artifact", "project": flow["project"], "artifact": missing["id"],
        "revision": missing["revision"], "body_digest": missing["digest"],
    }
    body = json.loads(flow["edge"]["edge"]["body"] if isinstance(flow["edge"]["edge"].get("body"), str)
                      else json.dumps(flow["edge"]["edge"]["body"]))
    body["source_ref"] = missing_ref
    edge = full.assurance.store_object(
        full.owner, flow["project"], "edge", "edge-missing-p-material", 1, body,
    )
    result = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[edge],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert result["status"] == "unverified"
    assert result["criteria"]["all_declared_outputs"]["status"] != "satisfied"


def test_p_mr_current_edge_stales_after_produced_artifact_revision(full, full_project, tmp_path):
    """A live produced-by pin must follow the artifact's current Knowledge head."""
    flow = _build_actual_flow(full, full_project, tmp_path)
    baseline = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[flow["edge"]["edge"]],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert baseline["status"] == "satisfied", baseline
    body = copy.deepcopy(flow["artifact"]["body"])
    body["statement"] = "A later Knowledge revision changes the produced output."
    full.k.revise(
        full.owner, flow["artifact"]["id"], flow["artifact"]["revision"],
        body, "independent currentness revision",
    )
    historical = resolve_produced_artifact(
        full.s, flow["artifact_ref"], project=flow["project"], current=False,
    )
    assert historical["revision"] == flow["artifact_ref"]["revision"]
    with pytest.raises(Fault) as stale_pin:
        resolve_produced_artifact(
            full.s, flow["artifact_ref"], project=flow["project"], current=True,
        )
    assert stale_pin.value.code == "stale_reference"
    stale = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[flow["edge"]["edge"]],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert stale["status"] != "satisfied", stale


def test_p_mr_unrelated_production_cas_loss_is_local(full, full_project, tmp_path):
    """Selecting by saved artifact/Task refs isolates unrelated project material."""
    flow = _build_actual_flow(full, full_project, tmp_path)
    _project, _repository, other_task, _executed, other_collected = _run_collect(
        full, full_project,
    )
    assert other_task != flow["task"]
    other_envelope, _payload = _material_payload(
        full, other_collected["artifacts"][0]["material"]["id"],
    )
    baseline = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[flow["edge"]["edge"]],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert baseline["status"] == "satisfied", baseline
    full.s.blob_path(other_envelope["payload_blob"]).unlink()
    isolated = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[flow["edge"]["edge"]],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert isolated["status"] == "satisfied", isolated


def test_p_mr_global_archive_and_gc_still_reject_unrelated_cas_loss(full, full_project, tmp_path):
    """The local matcher is selective; archive and GC closure remain global."""
    project, _repository, task_a, _executed_a, _collected_a = _run_collect(
        full, full_project,
    )
    _project, _repository, other_task, _executed, other_collected = _run_collect(
        full, full_project,
    )
    assert other_task != task_a
    other_envelope, _payload = _material_payload(
        full, other_collected["artifacts"][0]["material"]["id"],
    )
    baseline = full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    exported = full.history.export_archive(full.owner, baseline["id"])
    assert full.history.inspect_archive(
        full.owner, exported["path"], exported["sha256"],
    )["verified"]
    full.s.blob_path(other_envelope["payload_blob"]).unlink()
    with pytest.raises(Fault):
        full.assurance.cas_closure(project)


def test_p_mr_historical_candidate_epoch_cannot_remain_current(full, full_project, tmp_path):
    """A real replan/new claim fences the earlier P candidate and Task revision."""
    flow = _build_actual_flow(full, full_project, tmp_path)
    baseline = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[flow["edge"]["edge"]],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert baseline["status"] == "satisfied", baseline
    project, task = flow["project"], flow["task"]
    old_task = full.w.task(full.owner, task)
    old_epoch = old_task["epoch"]
    full.rt.tests(full.owner, task)
    for role in ("spec", "quality", "test_adequacy"):
        full.rt.review(full.owner, task, role, "fixture")
    revised_body = {
        key: copy.deepcopy(value)
        for key, value in old_task["body"].items()
        if key != "task_kind"
    }
    goal_payload = json.loads(revised_body["goal"][6:])
    goal_payload["calc.py"] = "def add(a,b):\n    return a+b\n"
    revised_body["goal"] = "WRITE:" + json.dumps(goal_payload)
    revised_body["write_paths"] = list(revised_body["write_paths"]) + ["calc.py"]
    proposal = full.task_revisions.propose(
        full.owner, task, old_task["revision"], revised_body,
        "Repair the implementation output while retaining the declared artifact.",
    )
    impact = full.rt.review(full.owner, proposal["id"], "impact", "p-mr-markers")
    full.task_revisions.apply(
        full.owner, proposal["id"], proposal["digest"], impact["receipt"],
    )
    plan = full.s.one("SELECT body FROM plans WHERE task=?", (task,))
    assert plan is None
    full.w.plan_tests(full.owner, task, {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    full.w.ready(full.owner, task)
    new_claim = full.w.claim(full.owner, project, task)
    assert new_claim["epoch"] > old_epoch
    new_executed = full.rt.execute(full.owner, task, "fixture")
    assert new_executed["candidate"] != flow["executed"]["candidate"]
    edge_row = full.s.one(
        "SELECT * FROM assurance_objects WHERE id=? AND project=? AND kind='edge'",
        (flow["edge"]["edge"]["id"], project), True,
    )
    with pytest.raises(Fault) as stale:
        full.assurance._ensure_object_current(
            full.owner, project, full.assurance._decode_object(edge_row), require_self=True,
        )
    assert stale.value.code == "stale_reference"


@pytest.mark.parametrize("tamper", ["foreign_producer", "old_candidate", "meaning_review_missing"])
def test_p_mr_negative_provenance_or_semantic_review_is_not_pass(full, full_project, tmp_path, tamper):
    flow = _build_actual_flow(full, full_project, tmp_path)
    if tamper == "meaning_review_missing":
        # A missing sealed E/S result cannot be replaced with a caller-shaped
        # mapping that claims meaning review was performed.
        result = evaluate_criteria(
            relation="produced_by", requirements=_requirements("produced_by"),
            denominator=flow["denominator"], edges=[flow["edge"]["edge"]],
            validated_reviews=flow["node_reviews"], relation_request=flow["request"],
            relation_reviews=None,
        )
        assert result["status"] != "satisfied"
        return

    envelope, payload = _material_payload(
        full, flow["collected"]["artifacts"][0]["material"]["id"],
    )
    new_artifact = full.k.propose(
        full.owner, flow["project"], "finding", flow["artifact"]["body"],
    )
    new_artifact_ref = {
        "kind": "artifact", "project": flow["project"],
        "artifact": new_artifact["id"], "revision": new_artifact["revision"],
        "body_digest": new_artifact["digest"],
    }
    tampered_payload = copy.deepcopy(payload)
    tampered_payload["artifact_ref"] = new_artifact_ref
    if tamper == "foreign_producer":
        tampered_payload["producer_actor"] = "foreign-actor"
    else:
        old_candidate = copy.deepcopy(tampered_payload["candidate_ref"])
        old_candidate["candidate"] = "CANDIDATE-old-retained"
        tampered_payload["candidate_ref"] = old_candidate
    dependencies = copy.deepcopy(envelope["dependency_refs"])
    for dependency in dependencies:
        if dependency.get("kind") == "artifact":
            dependency.clear()
            dependency.update(new_artifact_ref)
        elif tamper == "old_candidate" and dependency.get("kind") == "candidate":
            dependency.clear()
            dependency.update(tampered_payload["candidate_ref"])
    full.assurance.store_material(
        full.owner, flow["project"], "artifact_production", tampered_payload,
        dependencies, {"kind": "test.p_mr_negative", "tamper": tamper},
        {"controller": "test", "operation": "negative_fixture"},
    )
    edge_body = copy.deepcopy(flow["edge"]["edge"]["body"])
    edge_body["source_ref"] = new_artifact_ref
    edge = full.assurance.store_object(
        full.owner, flow["project"], "edge", f"edge-{tamper}-p-material", 1, edge_body,
    )
    assert envelope["material_kind"] == "artifact_production"
    result = evaluate_criteria(
        relation="produced_by", requirements=_requirements("produced_by"),
        denominator=flow["denominator"], edges=[edge],
        validated_reviews=flow["node_reviews"], relation_request=flow["request"],
        relation_reviews=flow["relation_reviews"],
    )
    assert result["status"] != "satisfied", result
