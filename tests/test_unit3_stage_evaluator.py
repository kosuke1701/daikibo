from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from daikibo.common import Fault, digest
from daikibo.task_revisions import task_definition_digest

from test_e3_selection_contract import (
    _adopt,
    _artifact_ref,
    _fixture,
    _profile_body,
    _register_fixture_review,
)
from test_e3_unit2b_node_reviews import _review_adapter


def _state_digest(full, project):
    tables = {}
    for table in ("tasks", "runs", "receipts", "events", "assurance_objects", "assurance_events", "assurance_heads"):
        tables[table] = full.s.all(f"SELECT * FROM {table} WHERE project=? ORDER BY rowid", (project,))
    return digest(tables)


def test_stage_evaluator_not_enabled_is_read_only_and_fail_closed(full):
    project, _source, _requirement, program, _scope = _fixture(full)
    before = _state_digest(full, project)
    result = full.assurance.evaluate_stage(full.owner, project, program, "plan")
    assert _state_digest(full, project) == before
    assert result["selection"]["profile_ref"] is None
    assert result["selection_state"] == "not_enabled"
    assert result["strong_complete"] is False
    assert result["system_enforcement"] is False


def test_stage_evaluator_profile_selection_and_report_are_read_only(full):
    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposed = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, proposed, None)
    before = _state_digest(full, project)
    result = full.assurance.evaluate_stage(full.owner, project, program, "plan", checkpoint="plan")
    after = _state_digest(full, project)
    assert before == after
    assert result["selection"]["program"] == program
    assert result["selection"]["profile_ref"] == proposed["profile_ref"]
    assert result["system_enforcement"] is False
    assert result["membership"]["state"] == "program"
    assert result["capabilities"]["relation_consumer"]["supported"] is True
    assert result["strong_complete"] is False
    assert result["relations"]["status"] == "missing"
    report = full.assurance.report(full.owner, project, program=program, stage="plan", limit=1)
    assert report["stage_evaluator"] is True
    assert report["system_enforcement"] is False
    assert report["result"]["semantic_fingerprint"] == result["semantic_fingerprint"]
    assert report["report_snapshot"] == result["report_snapshot"]
    assert report["next_cursor"] is not None
    next_page = full.assurance.report(
        full.owner, project, program=program, stage="plan", limit=1,
        cursor=report["next_cursor"],
    )
    assert next_page["offset"] == 1
    assert next_page["report_snapshot"] == report["report_snapshot"]


def test_task_checkpoint_keeps_global_and_local_denominators_separate(full, tmp_path):
    project, _source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposed = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, proposed, None)
    repository_root = tmp_path / "task-repo"
    repository_root.mkdir()
    repository = full.sn.register(full.owner, project, "task", str(repository_root))["id"]
    task = full.w.create(full.owner, project, {
        "title": "Task-local evaluator",
        "goal": "observe a precompletion checkpoint",
        "read_artifacts": [requirement["id"]], "write_paths": ["result.txt"],
        "acceptance": ["AC-E3"], "dependencies": [], "repos": [repository],
        "non_goals": [], "workflow_id": program,
    })
    task_row = full.w.task(full.owner, task["id"])
    from daikibo.task_revisions import task_definition_digest
    task_ref = {"kind": "task_revision", "project": project, "task": task["id"],
                "revision": task_row["revision"],
                "definition_digest": task_definition_digest(task_row["body"])}
    result = full.assurance.evaluate_stage(
        full.owner, project, program, "task", task=task_ref, checkpoint="ready",
    )
    assert result["membership"]["state"] == "satisfied"
    assert result["global_denominator"] is not None
    assert result["local_denominator"] is not None
    assert result["execution"]["status"] == "deferred"
    assert result["deferred_future"]
    assert any(item.get("code") == "test_plan_missing" for item in result["failures"])
    assert not any(item.get("code") == "candidate_missing" for item in result["failures"])
    assert result["unit_b"]["status"] == "deferred"
    assert result["strong_complete"] is False


def test_stage_evaluator_evaluates_every_declared_node_role(full, tmp_path):
    project, _source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    body = _profile_body(project, program, scope)
    body["node_review_rules"][0]["roles"] = ["quality", "requirements"]
    proposed = full.assurance.profile_propose(full.owner, project, program, body, None)
    _adopt(full, project, proposed, None)

    adapter = _review_adapter(full, tmp_path, "unit3-all-roles")
    for role in ("requirements", "quality"):
        full.rt.review(full.owner, requirement["id"], role, adapter)

    result = full.assurance.evaluate_stage(full.owner, project, program, "plan")
    request = next(item for item in result["nodes"]["requests"]
                   if item["node_ref"].get("artifact") == requirement["id"])
    selected = next(item for item in result["nodes"]["items"]
                    if item["node_ref"].get("artifact") == requirement["id"])
    assert request["selector"] == "requirement"
    assert request["roles"] == ["quality", "requirements"]
    assert set(selected["roles"]) == {"quality", "requirements"}
    assert all(value["status"] == "satisfied" for value in selected["roles"].values())
    assert all(isinstance(item, dict) for item in result["nodes"]["diagnostics"])
    assert not any(item.get("code") == "profile_extra_node_role_unconnected"
                   for item in result["failures"])


def test_node_fault_diagnostic_has_no_self_reference(full, monkeypatch):
    import daikibo.assurance_stage as stage_module

    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposed = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, proposed, None)

    def fail_request(*_args, **_kwargs):
        raise Fault("invalid_node_request", "synthetic request validation failure")

    monkeypatch.setattr(stage_module, "build_node_requests", fail_request)
    result = full.assurance.evaluate_stage(full.owner, project, program, "plan")
    node_result = result["nodes"]
    diagnostic = node_result["diagnostics"][0]
    assert isinstance(diagnostic, dict)
    assert diagnostic is not node_result
    assert diagnostic["code"] == "invalid_node_request"
    assert result["strong_complete"] is False


def test_stage_actual_runtime_reaches_allow_and_is_read_only(full, full_project, tmp_path, monkeypatch):
    from test_consumer_p_mr_integration import _actual_review_adapter, _task_ref

    project, repository, requirement, root = full_project
    failure_flag = tmp_path / "formal-fail"
    (Path(root) / "test_calc.py").write_text(
        "from pathlib import Path\n"
        "def test_add():\n"
        f"    assert not Path({str(failure_flag)!r}).exists()\n"
    )
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    partition_proposal = full.traceability.propose(
        full.owner, project, kind="document", scope={"source": source},
    )
    full.traceability.extract(full.owner, partition_proposal["id"])
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    domain = full.k.propose(
        full.owner, project, "domain",
        {"title": "Runtime domain", "statement": "The runtime owns arithmetic calculation.", "responsibilities": ["calculation"],
         "non_responsibilities": ["billing"], "owned_data": [], "interfaces": [], "source_refs": [source]},
    )
    domain = full.k.accept(full.owner, domain["id"], 1)
    manifest = {"format": "daikibo.artifact-output.v1", "outputs": [{
        "declaration_id": "artifact-result", "kind": "finding",
        "body": {"title": "Observed result", "statement": "The managed subprocess emitted the finding."},
    }]}
    body = {
        "title": "Normal stage P task", "goal": "WRITE:" + json.dumps({"artifact-output.json": json.dumps(manifest, sort_keys=True)}),
        "read_artifacts": [requirement, domain["id"]], "write_paths": ["artifact-output.json"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repository], "non_goals": [],
        "workflow_id": program,
        "structural_obligations": {"format": "daikibo.task-structural-obligations.v1",
            "required_outputs": [{"id": "artifact-result", "statement": "one finding",
                                   "artifact_refs": [_artifact_ref(project, full.s.one("SELECT * FROM artifacts WHERE id=?", (requirement,), True))],
                                   "realization_kind": "artifact"}],
            "required_exercises": []},
    }
    task = full.w.create(full.owner, project, body)
    full.w.plan_tests(full.owner, task["id"], {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"], "kind": "pytest", "required_tests": ["test_add"]}]})
    full.w.ready(full.owner, task["id"])
    claimed = full.w.claim(full.owner, project, task["id"])
    executed = full.rt.execute(full.owner, task["id"], "fixture")
    collected = full.invoke(full.owner, "task.artifacts_collect", {"task": task["id"], "expected_revision": claimed["revision"], "candidate": executed["candidate"], "repository": repository, "path": "artifact-output.json"})
    artifact = collected["artifacts"][0]["artifact"]
    artifact_ref = {"kind": "artifact", "project": project, "artifact": artifact["id"], "revision": artifact["revision"], "body_digest": artifact["digest"]}
    task_ref = _task_ref(full, project, task["id"])

    _register_fixture_review(full)
    units = [{"id": "runtime-unit", "title": "runtime", "parent": None, "domain": domain["id"],
              "rationale": "actual runtime owner", "obligations": [{"requirement": requirement, "acceptance": "AC-ADD"}],
              "tasks": [task["id"]], "interfaces": [], "dependencies": []}]
    breakdown = full.breakdowns.propose(full.owner, program, "Runtime stage plan", "actual normal fixture", units)
    review_adopt = _actual_review_adapter(full, tmp_path)
    page = full.breakdowns.get(full.owner, breakdown["id"])
    for packet in page["packets"]:
        for role in ("design", "trace"):
            full.rt.review(full.owner, packet["id"], role, "p-mr-markers")

    requirement_row = full.s.one("SELECT * FROM artifacts WHERE id=? AND project=?", (requirement, project), True)
    scope = full.assurance.scope_propose(full.owner, project, {"roots": [_artifact_ref(project, requirement_row)], "selection_rules": {}, "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []})
    relation_set = {"relation": "produced_by", "direction": "incoming", "centers": ["assigned_tasks"]}
    stages = {s: {"denominator": d, "relation_sets": [relation_set], "node_rules": ["test-plan"], "execution_results": e} for s, d, e in [("plan", "program_plan", "none"), ("task", "assigned_task_contributors", "assigned_checks"), ("integration", "program_integration", "integration_checks"), ("delivery", "actual_delivery", "certified_integration_and_actual_outputs")]}
    profile_body = {"format": "assurance.profile.v2", "project": project, "program": program, "scope_ref": scope["scope_ref"], "obligations_ref": scope["obligations_ref"], "previous_selection_ref": None, "application_mode": "mandatory", "stage_rules": stages, "node_review_rules": [{"id": "test-plan", "selector": "test_plan", "roles": ["test_plan"]}], "relation_selectors": ["produced_by"], "test_definition_bindings": [], "change_reason": "normal stage fixture", "authority_refs": []}
    proposed = full.assurance.profile_propose(full.owner, project, program, profile_body, None)
    _adopt(full, project, proposed, None)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task["id"],), True)
    full.rt.review(full.owner, task["id"], "test_plan", "p-mr-markers", proposal=json.loads(plan_row["body"]))
    # Unit4-P now consumes the selected canonical profile at the public
    # breakdown writer boundary.  The plan review is current, while the
    # produced_by output remains an explicit future obligation.
    full.breakdowns.activate(full.owner, breakdown["id"])
    old_scope_obligation = scope["obligations"]["body"]["obligations"][0]["id"]
    edge = full.assurance.edge_propose(full.owner, project, {"source_ref": artifact_ref, "target_ref": task_ref, "relation": "produced_by", "scope_ref": proposed["profile_ref"], "claim": "actual output", "obligation_ids": [old_scope_obligation], "required_evidence_refs": [], "authority_refs": []})
    review_adopt(project, edge["edge"])
    relation_set_result = full.assurance.set_propose(full.owner, project, {"center_ref": task_ref, "relation": "produced_by", "direction": "incoming", "scope_ref": proposed["profile_ref"], "criteria": {}, "required_evidence_refs": []})
    review_adopt(project, relation_set_result["set"])
    full.rt.tests(full.owner, task["id"])
    before = _state_digest(full, project)
    plan_result = full.assurance.evaluate_stage(full.owner, project, program, "plan", proposed_breakdown=breakdown["id"])
    task_result = full.assurance.evaluate_stage(full.owner, project, program, "task", task=task_ref, checkpoint="complete", proposed_breakdown=breakdown["id"])
    assert _state_digest(full, project) == before
    # A Task-owned artifact declaration is a future producer result at the
    # plan checkpoint even when a later fixture has already collected one.
    # The complete Task checkpoint re-evaluates the same declaration as a
    # current obligation.
    assert plan_result["relations"]["status"] == "deferred"
    assert task_result["execution"]["status"] == "satisfied"
    assert plan_result["assurance_allow"] is True
    assert plan_result["strong_complete"] is False
    assert task_result["assurance_allow"] is True
    assert task_result["strong_complete"] is True
    semantic_before_repeat = task_result["semantic_fingerprint"]
    global_before_repeat = task_result["global_denominator"]
    local_before_repeat = task_result["local_denominator"]
    nodes_before_repeat = {
        "requests": task_result["nodes"]["requests"],
        "items": task_result["nodes"]["items"],
    }

    # Runtime.tests is append-only: a second successful capture has a new
    # provenance envelope but the same frozen Task/plan definition.  The
    # consumer must collapse that equivalent material identity and keep the
    # normal Task completion proof intact.
    material_count = full.s.one(
        "SELECT count(*) AS n FROM assurance_objects "
        "WHERE project=? AND kind='material' AND json_extract(body,'$.material_kind')='test_plan'",
        (project,),
    )["n"]
    material_before_repeat = full.s.all(
        "SELECT id,digest,body FROM assurance_objects "
        "WHERE project=? AND kind='material' AND json_extract(body,'$.material_kind')='test_plan' "
        "ORDER BY id",
        (project,),
    )
    full.rt.tests(full.owner, task["id"])
    assert full.s.one(
        "SELECT count(*) AS n FROM assurance_objects "
        "WHERE project=? AND kind='material' AND json_extract(body,'$.material_kind')='test_plan'",
        (project,),
    )["n"] == material_count + 1
    repeat_before = _state_digest(full, project)
    repeated_task_result = full.assurance.evaluate_stage(
        full.owner, project, program, "task", task=task_ref,
        checkpoint="complete", proposed_breakdown=breakdown["id"],
    )
    assert _state_digest(full, project) == repeat_before
    assert repeated_task_result["semantic_fingerprint"] == semantic_before_repeat
    assert repeated_task_result["global_denominator"] == global_before_repeat
    assert repeated_task_result["local_denominator"] == local_before_repeat
    assert {key: repeated_task_result["nodes"][key] for key in ("requests", "items")} == nodes_before_repeat
    assert repeated_task_result["execution"]["status"] == "satisfied"
    assert repeated_task_result["assurance_allow"] is True
    assert repeated_task_result["strong_complete"] is True
    material_after_repeat = full.s.all(
        "SELECT id,digest,body FROM assurance_objects "
        "WHERE project=? AND kind='material' AND json_extract(body,'$.material_kind')='test_plan' "
        "ORDER BY id",
        (project,),
    )
    assert len(material_after_repeat) == len(material_before_repeat) + 1
    assert len({row["id"] for row in material_after_repeat}) == len(material_after_repeat)
    for row in material_after_repeat:
        material = json.loads(row["body"])
        payload = full.s.blob_get(material["payload_blob"])
        assert material["semantic_digest"] == digest(json.loads(payload))
        assert isinstance(material["captured_from"], dict)

    # A later real formal check can have an older wall-clock timestamp. The
    # durable run_observed sequence still makes that actual failure the latest
    # observation consumed by the Task execution reader.
    failure_flag.write_text("force a real formal failure")
    import daikibo.runtime as runtime_module
    clock = runtime_module.timestamp
    monkeypatch.setattr(runtime_module, "timestamp", lambda: clock() - 5)
    failed_capture = full.rt.tests(full.owner, task["id"])
    failed_receipt = full.g.receipt(failed_capture["checks"][0]["receipt"])
    assert failed_receipt["exit_code"] != 0
    candidate_body = json.loads(full.s.one(
        "SELECT body FROM candidates WHERE id=?", (executed["candidate"],), True,
    )["body"])
    evidence = full.g.task_test_evidence(
        full.owner, task["id"], binding=failed_capture["binding"],
        snapshot_digest=candidate_body["snapshot"]["digest"],
    )
    assert evidence["checks"][0]["status"] == "failed"
    assert evidence["checks"][0]["selected_receipt"] == failed_receipt["id"]
    failed_task_result = full.assurance.evaluate_stage(
        full.owner, project, program, "task", task=task_ref,
        checkpoint="complete", proposed_breakdown=breakdown["id"],
    )
    assert failed_task_result["execution"]["status"] == "failed"
    assert failed_task_result["assurance_allow"] is False
    assert failed_task_result["strong_complete"] is False

    # A second material with the same payload but a different Task-definition
    # dependency is a real identity conflict.  It must remain a blocking
    # negative even when valid captures are retained alongside it.
    task_row = full.s.one("SELECT * FROM tasks WHERE id=?", (task["id"],), True)
    plan_body = json.loads(plan_row["body"])
    invalid_dependency = {
        "kind": "task_revision", "project": project, "task": task["id"],
        "revision": task_row["revision"], "definition_digest": "0" * 64,
    }
    full.assurance.store_material(
        full.owner, project, "test_plan",
        {"task": task["id"], "task_revision": task_row["revision"],
         "plan_body": plan_body, "plan_digest": plan_row["digest"]},
        [invalid_dependency], {"kind": "test_plan", "id": task["id"]},
        {"controller": "runtime", "operation": "identity-negative",
         "capture_id": "VMAT-identity-negative"},
    )
    conflict_before = _state_digest(full, project)
    conflict_result = full.assurance.evaluate_stage(
        full.owner, project, program, "task", task=task_ref,
        checkpoint="complete", proposed_breakdown=breakdown["id"],
    )
    assert _state_digest(full, project) == conflict_before
    assert conflict_result["assurance_allow"] is False
    assert conflict_result["strong_complete"] is False
    assert any(item.get("code") == "test_plan_material_invalid"
               for item in conflict_result["failures"])


@pytest.mark.parametrize(
    ("selector", "kind", "role"),
    [("component", "component", "design"), ("test_artifact", "test", "test_plan")],
)
def test_stage_evaluator_preserves_exact_node_population_selector(full, selector, kind, role):
    project, _source, _requirement, program, scope = _fixture(full)
    artifact = full.k.propose(
        full.owner, project, kind,
        {"title": f"{kind} node", "statement": f"Controller persisted {kind} node"},
    )
    artifact = full.k.accept(full.owner, artifact["id"], 1)
    _register_fixture_review(full)
    body = _profile_body(project, program, scope)
    body["node_review_rules"].append(
        {"id": f"{kind}-node", "selector": selector, "roles": [role]},
    )
    body["node_review_rules"] = sorted(body["node_review_rules"], key=lambda item: item["id"])
    for stage_rule in body["stage_rules"].values():
        stage_rule["node_rules"].append(f"{kind}-node")
        stage_rule["node_rules"].sort()
    proposed = full.assurance.profile_propose(full.owner, project, program, body, None)
    _adopt(full, project, proposed, None)

    result = full.assurance.evaluate_stage(full.owner, project, program, "plan")
    matching = [item for item in result["nodes"]["requests"]
                if item["node_ref"].get("artifact") == artifact["id"]]
    assert len(matching) == 1
    assert matching[0]["selector"] == selector
    assert matching[0]["roles"] == [role]


def test_candidate_checkpoint_requires_same_candidate_identity(full, tmp_path):
    project, _source, requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposed = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, proposed, None)
    repository_root = tmp_path / "candidate-repo"
    repository_root.mkdir()
    repository = full.sn.register(full.owner, project, "candidate", str(repository_root))["id"]
    task = full.w.create(full.owner, project, {
        "title": "Candidate checkpoint",
        "goal": "require a sealed candidate identity",
        "read_artifacts": [requirement["id"]], "write_paths": ["result.txt"],
        "acceptance": ["AC-E3"], "dependencies": [], "repos": [repository],
        "non_goals": [], "workflow_id": program,
    })
    row = full.w.task(full.owner, task["id"])
    task_ref = {"kind": "task_revision", "project": project, "task": row["id"],
                "revision": row["revision"],
                "definition_digest": task_definition_digest(row["body"])}
    result = full.assurance.evaluate_stage(
        full.owner, project, program, "task", task=task_ref, checkpoint="candidate",
    )
    assert result["strong_complete"] is False
    assert any(item.get("code") == "candidate_missing" for item in result["failures"])
    assert result["execution"]["status"] != "deferred"


def test_stage_relation_consumer_reads_actual_produced_by_material(full, full_project, tmp_path):
    """The plan relation path consumes the retained P material through M/R."""
    from test_consumer_p_artifact_provenance import _run_collect
    from test_consumer_p_mr_integration import (
        _actual_review_adapter,
        _breakdown_for_collected_task,
        _task_ref,
    )

    project, _repository, requirement, _root = full_project
    (Path(full_project[3]) / "test_calc.py").write_text(
        "def test_add():\n    assert 1 + 1 == 2\n",
    )
    _project, _repository, task, executed, collected = _run_collect(full, full_project)
    plan_row = full.s.one("SELECT * FROM plans WHERE task=?", (task,), True)
    artifact = collected["artifacts"][0]["artifact"]
    artifact_ref = {
        "kind": "artifact", "project": project, "artifact": artifact["id"],
        "revision": artifact["revision"], "body_digest": artifact["digest"],
    }
    task_ref = _task_ref(full, project, task)
    source = full.s.one("SELECT id FROM sources WHERE project=?", (project,))["id"]
    program = full.p.begin(full.owner, project, source, compact=True)["program"]
    breakdown = _breakdown_for_collected_task(full, project, program, requirement, task)
    requirement_row = full.s.one(
        "SELECT * FROM artifacts WHERE id=? AND project=?", (requirement, project), True,
    )
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [_artifact_ref(project, requirement_row)], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    relation_set = {"relation": "produced_by", "direction": "incoming", "centers": ["assigned_tasks"]}
    stage_rules = {
        "plan": {"denominator": "program_plan", "relation_sets": [relation_set],
                 "node_rules": ["test-plan"], "execution_results": "none"},
        "task": {"denominator": "assigned_task_contributors", "relation_sets": [relation_set],
                 "node_rules": ["test-plan"], "execution_results": "assigned_checks"},
        "integration": {"denominator": "program_integration", "relation_sets": [relation_set],
                         "node_rules": ["test-plan"], "execution_results": "integration_checks"},
        "delivery": {"denominator": "actual_delivery", "relation_sets": [relation_set],
                      "node_rules": ["test-plan"],
                      "execution_results": "certified_integration_and_actual_outputs"},
    }
    body = {
        "format": "assurance.profile.v2", "project": project, "program": program,
        "scope_ref": scope["scope_ref"], "obligations_ref": scope["obligations_ref"],
        "previous_selection_ref": None, "application_mode": "mandatory",
        "stage_rules": stage_rules,
        "node_review_rules": [{"id": "test-plan", "selector": "test_plan", "roles": ["test_plan"]}],
        "relation_selectors": ["produced_by"], "test_definition_bindings": [],
        "change_reason": "Unit3 produced-by consumer fixture", "authority_refs": [],
    }
    _register_fixture_review(full)
    proposed = full.assurance.profile_propose(full.owner, project, program, body, None)
    _adopt(full, project, proposed, None)
    review_adopt = _actual_review_adapter(full, tmp_path)
    old_scope_obligation = scope["obligations"]["body"]["obligations"][0]["id"]
    edge = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": artifact_ref, "target_ref": task_ref,
         "relation": "produced_by", "scope_ref": proposed["profile_ref"],
         "claim": "Runtime artifact belongs to this Task output",
         "obligation_ids": [old_scope_obligation], "required_evidence_refs": [],
         "authority_refs": []},
    )
    review_adopt(project, edge["edge"])
    relation_set_result = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": task_ref, "relation": "produced_by", "direction": "incoming",
         "scope_ref": proposed["profile_ref"], "criteria": {}, "required_evidence_refs": []},
    )
    review_adopt(project, relation_set_result["set"])
    full.rt.review(full.owner, task, "test_plan", "p-mr-markers",
                   proposal=json.loads(plan_row["body"]))

    full.rt.tests(full.owner, task)
    result = full.assurance.evaluate_stage(
        full.owner, project, program, "plan", proposed_breakdown=breakdown,
    )
    assert result["capabilities"]["relation_consumer"]["supported"] is True
    assert result["relations"]["status"] == "deferred", result["relations"]
    task_result = full.assurance.evaluate_stage(
        full.owner, project, program, "task", task=_task_ref(full, project, task),
        checkpoint="complete", proposed_breakdown=breakdown,
    )
    assert task_result["execution"]["status"] == "satisfied"
    assert task_result["execution"]["reason"] == "execution_results_evaluated"
    assert task_result["relations"]["status"] == "satisfied"


@pytest.mark.parametrize(
    ("stage", "checkpoint"),
    [("plan", "complete"), ("task", "plan"), ("integration", "finish"), ("delivery", "certify")],
)
def test_stage_evaluator_rejects_unknown_checkpoint(stage, checkpoint, full):
    project, _source, _requirement, program, _scope = _fixture(full)
    with pytest.raises(Fault) as error:
        full.assurance.evaluate_stage(full.owner, project, program, stage, checkpoint=checkpoint)
    assert error.value.code == "invalid_checkpoint"


def test_stage_evaluator_rejects_caller_supplied_task_body_or_wrong_context(full):
    project, _source, _requirement, program, _scope = _fixture(full)
    with pytest.raises(Fault) as raw_task:
        full.assurance.evaluate_stage(full.owner, project, program, "task", task="TASK-1")
    assert raw_task.value.code == "invalid_stage_context"
    with pytest.raises(Fault) as wrong_delivery:
        full.assurance.evaluate_stage(full.owner, project, program, "plan", delivery={})
    assert wrong_delivery.value.code == "invalid_stage_context"
