import copy

import pytest

from daikibo.assurance_stage import _relation_component
from daikibo.assurance_stage import classify_relation_obligation
from daikibo.assurance_stage import _execution_component
from daikibo.assurance_criteria import build_relation_request
from daikibo.assurance_relations import REGISTRY_V2_DIGEST
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.common import Fault, canonical
from test_e3_consumer_c_integration import _case, _edge, _reviews
from test_e3_consumer_c_profile_v3 import (
    _prepare_reviewed_program, _replace_fixture_profile,
)
from test_e3_unit2b_delivery_criteria import _receipt_delivery_snapshot
from test_delivery_git_and_recovery import profile
from conftest import finish_task
from test_unit3_stage_evaluator import _state_digest


def test_delivery_output_relations_are_current_from_integration_only(
):
    obligation = {"id": "obligation:delivery-output", "category": "delivery_declared_output"}
    owner = [{"kind": "delivery_snapshot", "project": "p", "delivery": "D",
              "binding_digest": "1" * 64, "snapshot_digest": "2" * 64,
              "pin": {"id": "M", "digest": "3" * 64}}]
    integration = classify_relation_obligation(
        "produced_by", stage="integration", checkpoint="certify",
        obligation=obligation, owner_refs=owner, context={},
    )
    delivery = classify_relation_obligation(
        "contains", stage="delivery", checkpoint="finalize",
        obligation=obligation, owner_refs=owner, context={},
    )
    task = classify_relation_obligation(
        "contains", stage="task", checkpoint="complete",
        obligation=obligation, owner_refs=owner, context={},
    )
    assert integration["classification"] == "required_now"
    assert integration["first_required_checkpoint"] == "integration"
    assert delivery["classification"] == "required_now"
    assert task["classification"] == "deferred_future"


def _normal_case(full, full_project, tmp_path, *, two_repositories=False,
                 output_relations=False, profile_format="assurance.profile.v3"):
    fixture = _prepare_reviewed_program(full, full_project, tmp_path)
    project = fixture["project"]
    repositories = [fixture["repository"]]
    if two_repositories:
        second_root = tmp_path / "consumer-c-second-repository"
        second_root.mkdir()
        (second_root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        repositories.append(full.sn.register(
            full.owner, project, "consumer-c-secondary", str(second_root),
        )["id"])
    body = profile(project, fixture["repository"], fixture["requirement"], fixture["task"])
    body["repo_order"] = repositories
    body["build_outputs"] = [
        {"id": "a", "repo": repositories[0], "path": ".daikibo-build/a"},
        {"id": "b", "repo": repositories[-1], "path": ".daikibo-build/b"},
    ]
    body["checks"][0].update(
        argv=["python", "-c", "from pathlib import Path; Path('.daikibo-build').mkdir(exist_ok=True); Path('.daikibo-build/a').write_text('a')"],
        produces=["a"],
    )
    body["checks"].insert(1, {
        "id": "build-b", "category": "build", "repo": repositories[-1],
        "kind": "command", "argv": ["python", "-c", "from pathlib import Path; Path('.daikibo-build').mkdir(exist_ok=True); Path('.daikibo-build/b').write_text('b')"],
        "purpose": "second actual producer", "produces": ["b"],
    })
    body["checks"][2]["uses"] = ["a", "b"]
    full.d.configure(full.owner, project, body)
    finish_task(full, project, fixture["task"])
    full.breakdowns.activate(full.owner, fixture["breakdown"])
    delivery_id = full.d.prepare(full.owner, project)["id"]
    verification = full.d.verify(full.owner, delivery_id)
    _row, delivery_body = full.d.current(delivery_id)
    assert all(item.get("passed") is True for item in verification["results"])
    build_receipt = next(item["receipt"] for item in verification["results"] if item["check"] == "build")
    snapshot = _receipt_delivery_snapshot(full, full.g.receipt(build_receipt))
    def adjust_stage_rules(body):
        if profile_format == "assurance.profile.v5":
            from test_domain_responsibility_v5 import scope_v2
            from unit4p_domain_fixture import reviews_adopt
            previous_scope = full.assurance._object_by_ref(body["scope_ref"], project, kinds={"scope"})
            modern = scope_v2(full, project, previous_scope["body"]["roots"])
            reviews_adopt(full, project, modern["scope"], "e3-assurance-fixture")
            reviews_adopt(full, project, modern["obligations"], "e3-assurance-fixture")
            body.update(format=profile_format, scope_ref=modern["scope_ref"],
                        obligations_ref=modern["obligations_ref"],
                        required_scope_contract="assurance.scope.v2",
                        required_node_contract="assurance.node-contract.v2")
        if output_relations:
            for stage in ("integration", "delivery"):
                body["stage_rules"][stage]["relation_sets"] = [
                    {"relation": relation, "direction": "outgoing", "centers": ["delivery_snapshots"]}
                    for relation in ("contains", "produced_by")
                ]

    proposal = _replace_fixture_profile(
        full, fixture, relation_selectors=["contains", "produced_by", "realizes"],
        body_mutator=adjust_stage_rules,
    )
    context = collect_stage_context(full, full.owner, project=project, program=fixture["program"],
                                    stage="delivery", proposed_breakdown=fixture["breakdown"], delivery=snapshot)
    denominator = derive_denominator(context)
    output_ref = full.assurance.pin(full.owner, project, {
        "kind": "output_artifact", "delivery": snapshot, "check_id": "build",
        "receipt": build_receipt, "output_id": "a",
    })["ref"]
    producer = next(item["producer_ref"] for item in context["delivery_material"]["declared_outputs"]["items"]
                    if item["definition"]["id"] == "a")
    output_b = full.assurance.pin(full.owner, project, {
        "kind": "output_artifact", "delivery": snapshot, "check_id": "build-b",
        "receipt": next(item["receipt"] for item in verification["results"] if item["check"] == "build-b"),
        "output_id": "b",
    })["ref"]
    producer_b = next(item["producer_ref"] for item in context["delivery_material"]["declared_outputs"]["items"]
                      if item["definition"]["id"] == "b")
    actual_refs = {}
    if two_repositories:
        updated = copy.deepcopy(delivery_body)
        for repository in repositories:
            result = full.sn.commit_snapshot(
                delivery_body["snapshot"], repository, "Consumer-C execution fixture",
            )
            updated["git"][repository] = result
        full.s.execute(
            "UPDATE deliveries SET body=? WHERE id=?",
            (canonical(updated).decode(), delivery_id),
        )
        row = full.s.one(
            "SELECT * FROM deliveries WHERE id=? AND project=?", (delivery_id, project), True,
        )
        for repository in repositories:
            actual_refs[repository], _ = full.rt.verification_materials.pin_actual_delivery_commit(
                full.owner, project, row, updated, repository, updated["git"][repository],
                snapshot_ref=snapshot,
                captured_from={"controller": "delivery", "operation": "consumer-c-execution.actual"},
            )
        context = collect_stage_context(
            full, full.owner, project=project, program=fixture["program"],
            stage="delivery", proposed_breakdown=fixture["breakdown"],
            delivery=actual_refs[repositories[0]],
        )
        denominator = derive_denominator(context)
        delivery_body = updated
    return {"fixture": fixture, "project": project, "snapshot": snapshot,
            "context": context, "denominator": denominator, "scope": proposal["profile_ref"],
            "output_ref": output_ref, "producer": producer, "output_b": output_b, "producer_b": producer_b,
            "repositories": repositories, "actual_refs": actual_refs,
            "build_receipt": build_receipt,
            "verification": verification, "delivery_body": delivery_body}


def _adopt_output_relations(full, case, adapter):
    for relation in ("produced_by", "contains"):
        if relation == "produced_by":
            endpoints = [
                (case["output_ref"], case["producer"], "a"),
                (case["output_b"], case["producer_b"], "b"),
            ]
        else:
            endpoints = [
                (case["snapshot"], case["output_ref"], "a"),
                (case["snapshot"], case["output_b"], "b"),
            ]
        for index, (source, target, output_id) in enumerate(endpoints):
            edge = full.assurance.edge_propose(full.owner, case["project"], {
                "source_ref": source, "target_ref": target, "relation": relation,
                "relation_contract_digest": REGISTRY_V2_DIGEST,
                "scope_ref": case["scope"], "claim": f"{relation} output {index}",
                "obligation_ids": [],
                "required_evidence_refs": [], "authority_refs": [],
            })
            adapter(case["project"], edge["edge"])
        relation_set = full.assurance.set_propose(full.owner, case["project"], {
            "center_ref": case["snapshot"], "relation": relation, "direction": "outgoing",
            "relation_contract_digest": REGISTRY_V2_DIGEST, "scope_ref": case["scope"],
            "criteria": {}, "required_evidence_refs": [],
        })
        adapter(case["project"], relation_set["set"])


from test_consumer_p_mr_integration import _actual_review_adapter


def test_delivery_execution_consumes_every_saved_check_and_keeps_failures(
    full, full_project, tmp_path,
):
    """The reader population is the saved checks list, not successful outputs."""
    case = _case(full, full_project, tmp_path)
    second_verification = full.d.verify(full.owner, case["snapshot"]["delivery"])
    latest_receipts = {
        item["check"]: item["receipt"]
        for item in second_verification["results"] if item.get("receipt")
    }
    profile_row = full.assurance._object_by_ref(
        case["scope"], case["project"], kinds={"profile"},
    )
    before = _state_digest(full, case["project"])
    result, _diagnostics, _future = _execution_component(
        full, full.owner, case["project"], "delivery", "finalize",
        case["context"], case["denominator"], None, profile_row["body"],
    )
    assert _state_digest(full, case["project"]) == before
    expected = {ref["check_id"] for ref in case["context"]["delivery_material"]["check_refs"]}
    observed_items = {
        item["check"]["check_id"]: item["status"]
        for item in result["items"] if item.get("delivery") and item.get("check")
    }
    observed = {
        item["check"]["check_id"]: item
        for item in result["items"] if item.get("delivery") and item.get("check")
    }
    assert set(observed_items) == expected
    assert observed_items["build"] == "satisfied"
    assert observed_items["build-b"] == "failed"
    assert observed["build"]["observed"]["receipt"] == latest_receipts["build"]
    assert observed["build-b"]["observed"]["receipt"] == latest_receipts["build-b"]
    # The later consumer is blocked by the failed producer and therefore has
    # no fabricated receipt; its obligation remains in the denominator.
    assert observed_items["start"] == "missing"
    assert result["status"] == "failed"


def test_delivery_execution_accepts_same_meaning_from_a_new_snapshot_capture(
    full, full_project, tmp_path,
):
    case = _case(full, full_project, tmp_path)
    delivery_id = case["snapshot"]["delivery"]
    row = full.s.one(
        "SELECT * FROM deliveries WHERE id=? AND project=?",
        (delivery_id, case["project"]), True,
    )
    replay_ref, _ = full.rt.verification_materials.pin_delivery_snapshot(
        full.owner, case["project"], row, case["delivery_body"],
        captured_from={"controller": "delivery", "operation": "same-meaning-recapture",
                       "capture_id": "VMAT-recapture-second"},
    )
    replay_context = collect_stage_context(
        full, full.owner, project=case["project"], program=case["fixture"]["program"],
        stage="delivery", proposed_breakdown=case["fixture"]["breakdown"],
        delivery=replay_ref,
    )
    replay_denominator = derive_denominator(replay_context)
    original_check = next(
        item for item in case["context"]["delivery_material"]["check_refs"]
        if item["check_id"] == "build"
    )
    replay_check = next(
        item for item in replay_context["delivery_material"]["check_refs"]
        if item["check_id"] == "build"
    )
    assert replay_check["delivery"]["pin"] != original_check["delivery"]["pin"]
    profile_row = full.assurance._object_by_ref(
        case["scope"], case["project"], kinds={"profile"},
    )
    result, _diagnostics, _future = _execution_component(
        full, full.owner, case["project"], "delivery", "finalize",
        replay_context, replay_denominator, None, profile_row["body"],
    )
    build = next(
        item for item in result["items"]
        if item.get("delivery") and item.get("check", {}).get("check_id") == "build"
    )
    assert build["status"] == "satisfied"


def test_consumer_c_delivery_relations_use_normal_mr_for_both_output_relations(
    full, full_project, tmp_path,
):
    """Both connected C relations consume full declarations through M/R."""
    case = _normal_case(full, full_project, tmp_path, two_repositories=True)
    adapter = _actual_review_adapter(full, tmp_path)
    assert case["context"]["delivery_material"]["reader"]["status"] == "available"
    assert {item["repository"] for item in case["context"]["delivery_material"]["repositories"]} == set(case["repositories"])
    assert len(case["context"]["delivery_material"]["actual_commit_refs"]) == len(case["repositories"])
    profile_row = full.assurance._object_by_ref(
        case["scope"], case["project"], kinds={"profile"},
    )
    before = _state_digest(full, case["project"])
    execution, _diagnostics, _future = _execution_component(
        full, full.owner, case["project"], "delivery", "finalize",
        case["context"], case["denominator"], None, profile_row["body"],
    )
    assert _state_digest(full, case["project"]) == before
    check_ids = {ref["check_id"] for ref in case["context"]["delivery_material"]["check_refs"]}
    observed_checks = {
        item["check"]["check_id"]: item["status"]
        for item in execution["items"] if item.get("delivery") and item.get("check")
    }
    assert set(observed_checks) == check_ids
    assert set(observed_checks.values()) == {"satisfied"}
    assert execution["status"] == "satisfied"
    _adopt_output_relations(full, case, adapter)

    profile_body = {"stage_rules": {"delivery": {"relation_sets": [
        {"relation": "contains", "direction": "outgoing", "centers": ["delivery_snapshots"]},
        {"relation": "produced_by", "direction": "outgoing", "centers": ["delivery_snapshots"]},
    ]}}}
    before_relation = _state_digest(full, case["project"])
    result, _items, _future = _relation_component(
        full, full.owner, case["project"], profile_body, "delivery", "finalize",
        case["context"], case["denominator"], None, case["scope"], _reviews(full, case),
        REGISTRY_V2_DIGEST,
    )
    assert _state_digest(full, case["project"]) == before_relation
    assert result["capability"]["supported"] is True
    assert result["status"] == "satisfied"
    assert all(item["status"] == "satisfied" for item in result["items"])


def test_consumer_c_actual_centers_partition_declared_outputs_by_repository(
    full, full_project, tmp_path,
):
    """Each actual-commit C center owns only its repository's declarations."""
    case = _normal_case(full, full_project, tmp_path, two_repositories=True)
    context = case["context"]
    denominator = case["denominator"]
    assurance = full.assurance
    binding_base = {
        "format": "assurance.delivery-declared-output.v1",
        "project": case["project"], "relation": "contains", "direction": "outgoing",
        "relation_contract_digest": REGISTRY_V2_DIGEST,
        "scope_ref": case["scope"],
    }
    snapshot = context["delivery_material"]["snapshot_ref"]
    snapshot_body, _ = assurance._consumer_c_obligation_material(
        full.owner, case["project"], {**binding_base, "center_ref": snapshot}, current=False,
    )
    snapshot_ids = {item["id"] for item in snapshot_body["obligations"]}
    partitions = {}
    for actual in context["delivery_material"]["centers"]["actual_commits"]:
        actual_body, _ = assurance._consumer_c_obligation_material(
            full.owner, case["project"], {**binding_base, "center_ref": actual}, current=False,
        )
        actual_ids = {item["id"] for item in actual_body["obligations"]}
        partitions[actual["repository"]] = actual_ids
        request = build_relation_request(
            full, full.owner, context=context, denominator=denominator,
            relation="contains", center_ref=actual, direction="outgoing",
            scope_ref=case["scope"], registry_digest=REGISTRY_V2_DIGEST,
        )
        assert set(request["required_obligation_ids"]) == actual_ids

    assert snapshot_ids == set().union(*partitions.values())
    assert all(len(ids) == 1 for ids in partitions.values())
    with pytest.raises(Fault) as rejected:
        full.assurance.edge_propose(full.owner, case["project"], {
            "source_ref": case["actual_refs"][case["repositories"][0]],
            "target_ref": case["output_b"], "relation": "contains",
            "relation_contract_digest": REGISTRY_V2_DIGEST,
            "scope_ref": case["scope"], "claim": "foreign output control",
            "obligation_ids": [], "required_evidence_refs": [], "authority_refs": [],
        })
    assert rejected.value.code == "invalid_relation_endpoint"


def test_consumer_c_integration_requires_saved_output_relations_now(
    full, full_project, tmp_path,
):
    """Integration sees the saved output population before later actual Git material."""
    case = _normal_case(full, full_project, tmp_path, output_relations=True)
    integration_context = collect_stage_context(
        full, full.owner, project=case["project"], program=case["fixture"]["program"],
        stage="integration", proposed_breakdown=case["fixture"]["breakdown"],
        delivery=case["snapshot"],
    )
    integration_denominator = derive_denominator(integration_context)
    profile_row = full.assurance._object_by_ref(
        case["scope"], case["project"], kinds={"profile"},
    )
    result, _items, future = _relation_component(
        full, full.owner, case["project"], profile_row["body"], "integration", "certify",
        integration_context, integration_denominator, None, case["scope"],
        _reviews(full, case), REGISTRY_V2_DIGEST,
    )
    assert result["status"] == "missing"
    assert future == []
    assert len(result["items"]) == 2
    for item in result["items"]:
        schedule = item["schedule"]
        assert len(schedule["population_ids"]) == 2
        assert schedule["required_now_ids"] == schedule["population_ids"]
        assert schedule["deferred_future_ids"] == []
        assert item["status"] == "missing"

    adapter = _actual_review_adapter(full, tmp_path)
    _adopt_output_relations(full, case, adapter)
    repaired, _items, future = _relation_component(
        full, full.owner, case["project"], profile_row["body"], "integration", "certify",
        integration_context, integration_denominator, None, case["scope"],
        _reviews(full, case), REGISTRY_V2_DIGEST,
    )
    assert repaired["status"] == "satisfied", repaired
    assert future == []
    assert all(item["status"] == "satisfied" for item in repaired["items"])


def test_consumer_c_integration_defers_only_uncreated_actual_and_rejects_unpinned_material(
    full, full_project, tmp_path,
):
    """A missing actual can be future; saved Git material without a pin is current missing."""
    case = _normal_case(full, full_project, tmp_path, output_relations=True)
    profile_row = full.assurance._object_by_ref(
        case["scope"], case["project"], kinds={"profile"},
    )
    context = collect_stage_context(
        full, full.owner, project=case["project"], program=case["fixture"]["program"],
        stage="integration", proposed_breakdown=case["fixture"]["breakdown"],
        delivery=case["snapshot"],
    )
    denominator = derive_denominator(context)
    _execution, _diagnostics, future = _execution_component(
        full, full.owner, case["project"], "integration", "certify",
        context, denominator, None, profile_row["body"],
    )
    assert future
    assert {item["code"] for item in future} == {"delivery_repository_observation_missing"}

    repository = case["repositories"][0]
    updated = copy.deepcopy(case["delivery_body"])
    actual = full.sn.commit_snapshot(
        updated["snapshot"], repository, "Consumer-C unpinned actual fixture",
    )
    updated.setdefault("git", {})[repository] = actual
    full.s.execute(
        "UPDATE deliveries SET body=? WHERE id=?",
        (canonical(updated).decode(), case["snapshot"]["delivery"]),
    )
    saved_actual_context = collect_stage_context(
        full, full.owner, project=case["project"], program=case["fixture"]["program"],
        stage="integration", proposed_breakdown=case["fixture"]["breakdown"],
        delivery=case["snapshot"],
    )
    saved_actual_denominator = derive_denominator(saved_actual_context)
    execution, diagnostics, future = _execution_component(
        full, full.owner, case["project"], "integration", "certify",
        saved_actual_context, saved_actual_denominator, None, profile_row["body"],
    )
    assert future == []
    assert execution["status"] == "missing"
    assert any(item["code"] == "delivery_actual_material_missing" for item in diagnostics)


def test_consumer_c_delivery_rejects_a_foreign_producer_for_a_declared_output(
    full, full_project, tmp_path,
):
    case = _normal_case(full, full_project, tmp_path)
    adapter = _actual_review_adapter(full, tmp_path)
    for index, (output_ref, wrong_producer) in enumerate(
        ((case["output_ref"], case["producer_b"]),
         (case["output_b"], case["producer"])),
    ):
        with pytest.raises(Fault) as exc:
            full.assurance.edge_propose(full.owner, case["project"], {
                "source_ref": output_ref, "target_ref": wrong_producer,
                "relation": "produced_by",
                "relation_contract_digest": REGISTRY_V2_DIGEST,
                "scope_ref": case["scope"], "claim": "foreign producer fixture",
                "obligation_ids": [], "required_evidence_refs": [], "authority_refs": [],
            })
        assert exc.value.code == "invalid_relation_endpoint"


def test_consumer_c_delivery_keeps_unconnected_profile_relations_unsupported(
    full, full_project, tmp_path,
):
    case = _case(full, full_project, tmp_path)
    profile_body = {"stage_rules": {"delivery": {"relation_sets": [
        {"relation": "realizes", "direction": "outgoing", "centers": ["delivery_snapshots"]},
    ]}}}
    result, items, _future = _relation_component(
        full, full.owner, case["project"], profile_body, "delivery", "finalize",
        case["context"], case["denominator"], None, case["scope"], _reviews(full, case),
        REGISTRY_V2_DIGEST,
    )
    assert result["capability"]["supported"] is False
    assert result["capability"]["unsupported_relations"] == ["realizes"]
    assert result["status"] == "unsupported"
    assert items[0]["code"] == "relation_consumer_unsupported"
