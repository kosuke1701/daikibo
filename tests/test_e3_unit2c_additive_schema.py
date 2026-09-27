from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from daikibo.assurance_additive import (
    ARTIFACT_STRUCTURAL_FORMAT,
    TASK_STRUCTURAL_FORMAT,
    artifact_structural_metadata,
    structural_contract,
    task_definition_contract,
    task_structural_metadata,
    validate_artifact_structural_obligations,
    validate_task_structural_obligations,
)
from daikibo.assurance_denominators import collect_stage_context, derive_denominator
from daikibo.common import Fault, canonical, digest, timestamp
from daikibo.knowledge import Knowledge


def _domain(full, project, name="Weather domain"):
    domain = full.k.propose(full.owner, project, "domain", {
        "title": name,
        "statement": "A stable domain responsibility fixture",
        "responsibilities": ["keep the physical source identity"],
        "non_responsibilities": [], "owned_data": [], "interfaces": [],
    })
    return full.k.accept(full.owner, domain["id"], 1)


def _requirement(full, project):
    source = full.k.source(full.owner, project, "Task requirement source")
    requirement = full.k.propose(full.owner, project, "requirement", {
        "title": "Required input", "statement": "A required input",
        "acceptance": ["AC-STRUCTURAL"], "source_refs": [source["id"]],
    })
    return full.k.accept(full.owner, requirement["id"], 1)


def _artifact_ref(artifact):
    return {"kind": "artifact", "project": artifact["project"], "artifact": artifact["id"],
            "revision": artifact["revision"], "body_digest": artifact["digest"]}


def _task_body(requirement, domain, *, declaration=None):
    body = {
        "title": "explicit denominator task",
        "goal": "exercise the explicit denominator",
        "read_artifacts": [requirement["id"], domain["id"]],
        "write_paths": ["src/result.py"],
        "acceptance": ["AC-STRUCTURAL"], "dependencies": [], "repos": [], "non_goals": [],
    }
    if declaration is not None:
        body["structural_obligations"] = declaration
    return body


def test_a_validator_resolves_historical_domain_pin_and_preserves_empty_status(full):
    project = full.k.create_project(full.owner, "A additive schema")["id"]
    domain = _domain(full, project)
    ref = _artifact_ref(domain)
    declaration = {
        "format": ARTIFACT_STRUCTURAL_FORMAT,
        "responsibilities": [{
            "id": "source-identity", "type": "domain_reference", "domain": ref,
            "responsibility_index": 0,
            "responsibility_digest": digest(domain["body"]["responsibilities"][0]),
        }],
    }
    design = full.k.propose(full.owner, project, "design", {
        "title": "Design", "statement": "Uses the domain responsibility",
        "structural_obligations": declaration,
    })
    assert design["body"]["structural_obligations"] == declaration
    assert artifact_structural_metadata(design["body"], kind="design")["status"] == "declared"
    assert artifact_structural_metadata(
        {"title": "legacy", "statement": "old"}, kind="design")["status"] == "legacy_unavailable"
    assert artifact_structural_metadata({"title": "empty", "statement": "old",
                                         "structural_obligations": {
                                             "format": ARTIFACT_STRUCTURAL_FORMAT,
                                             "responsibilities": [],
                                         }}, kind="design")["status"] == "explicit_empty"

    bad_digest = copy.deepcopy(declaration)
    bad_digest["responsibilities"][0]["responsibility_digest"] = "0" * 64
    with pytest.raises(Fault) as error:
        full.k.propose(full.owner, project, "design", {
            "title": "Bad", "statement": "bad", "structural_obligations": bad_digest,
        })
    assert error.value.code == "stale_reference"

    bad_index = copy.deepcopy(declaration)
    bad_index["responsibilities"][0]["responsibility_index"] = True
    with pytest.raises(Fault):
        full.k.propose(full.owner, project, "design", {
            "title": "Bad index", "statement": "bad", "structural_obligations": bad_index,
        })

    extra = copy.deepcopy(declaration)
    extra["responsibilities"][0]["extra"] = True
    with pytest.raises(Fault):
        Knowledge.validate_body("design", {"title": "Bad extra", "statement": "bad",
                                            "structural_obligations": extra})


def test_a_historical_domain_revision_is_an_exact_resolvable_pin(full):
    project = full.k.create_project(full.owner, "A historical schema")["id"]
    original = full.k.propose(full.owner, project, "domain", {
        "title": "Historical domain", "statement": "old",
        "responsibilities": ["old responsibility"], "non_responsibilities": [],
        "owned_data": [], "interfaces": [],
    })
    old_ref = _artifact_ref(original)
    revised = full.k.revise(full.owner, original["id"], 1, {
        "title": "Historical domain", "statement": "new",
        "responsibilities": ["new responsibility"], "non_responsibilities": [],
        "owned_data": [], "interfaces": [],
    }, "retain history")
    full.k.accept(full.owner, revised["id"], 2)
    declaration = {
        "format": ARTIFACT_STRUCTURAL_FORMAT,
        "responsibilities": [{"id": "old-pin", "type": "domain_reference", "domain": old_ref,
                               "responsibility_index": 0,
                               "responsibility_digest": digest(original["body"]["responsibilities"][0])}],
    }
    design = full.k.propose(full.owner, project, "design", {
        "title": "Historical user", "statement": "retains an old domain meaning",
        "structural_obligations": declaration,
    })
    assert design["body"]["structural_obligations"] == declaration


def test_formal_change_can_remove_a_declaration_and_preserve_history(full):
    project = full.k.create_project(full.owner, "formal omission")['id']
    full.rt.adapters.register(full.owner, "fixture", "fixture", sys.executable,
                              [str(Path(__file__).with_name("fixture_agent.py"))])
    source = full.k.source(full.owner, project, "Record the reviewed design change")
    declaration = {
        "format": ARTIFACT_STRUCTURAL_FORMAT,
        "responsibilities": [{"id": "one", "type": "statement", "statement": "one responsibility"}],
    }
    design = full.k.propose(full.owner, project, "design", {
        "title": "Design", "statement": "Design body", "structural_obligations": declaration,
    })
    full.k.accept(full.owner, design["id"], 1)
    program = full.p.begin(full.owner, project,
                           full.k.source(full.owner, project, "formal omission program")['id'], compact=True)['program']
    before = derive_denominator(collect_stage_context(full, full.owner, project=project,
                                                      program=program, stage="plan"))
    changed_body = {"title": "Design", "statement": "Design body"}
    change = full.p.change(full.owner, project, {
        "title": "Remove obsolete declaration", "origin": "design",
        "reason": "The reviewed design no longer carries this declaration",
        "affected": [design["id"]], "evidence": [source["id"]],
        "deltas": [{"artifact": design["id"], "expected_revision": 1, "body": changed_body}],
    })
    feasibility = full.rt.review(full.owner, change["id"], "feasibility", "fixture")
    full.p.attempt(full.owner, change["id"], "local_repair", {
        "hypothesis": "Apply the reviewed design body",
        "alternatives": ["retain the declaration"], "evidence": [feasibility["receipt"]],
        "outcome": "solution", "remaining_unknown": "",
    })
    consistency = full.rt.review(full.owner, change["id"], "consistency", "fixture")
    full.p.apply_technical_change(full.owner, change["id"], consistency["receipt"])
    current = full.k.artifact(full.owner, design["id"])
    historical = full.k.artifact(full.owner, design["id"], 1)
    assert "structural_obligations" not in current["body"]
    assert historical["body"]["structural_obligations"] == declaration
    after_context = collect_stage_context(full, full.owner, project=project, program=program, stage="plan")
    after = derive_denominator(after_context)
    assert after["input_digest"] != before["input_digest"]
    current_item = next(item for item in after_context["artifacts"] if item["id"] == design["id"])
    assert current_item["structural_obligations"]["status"] == "legacy_unavailable"


def test_a_validator_rejects_unsupported_kind_and_foreign_domain(full):
    project = full.k.create_project(full.owner, "A foreign schema")["id"]
    other_project = full.k.create_project(full.owner, "A foreign project")["id"]
    other = _domain(full, other_project, "Other domain")
    declaration = {
        "format": ARTIFACT_STRUCTURAL_FORMAT,
        "responsibilities": [{
            "id": "foreign", "type": "domain_reference", "domain": _artifact_ref(other),
            "responsibility_index": 0,
            "responsibility_digest": digest(other["body"]["responsibilities"][0]),
        }],
    }
    with pytest.raises(Fault) as error:
        full.k.propose(full.owner, project, "design", {
            "title": "Foreign", "statement": "bad", "structural_obligations": declaration,
        })
    assert error.value.code == "cross_project"
    with pytest.raises(Fault):
        Knowledge.validate_body("requirement", {
            "title": "Wrong kind", "statement": "bad", "acceptance": ["AC"],
            "structural_obligations": {"format": ARTIFACT_STRUCTURAL_FORMAT, "responsibilities": []},
        })


@pytest.mark.parametrize("kind", ["artifact", "task"])
def test_explicit_null_is_rejected_and_missing_remains_legacy(full, kind):
    project = full.k.create_project(full.owner, "null boundary")['id']
    if kind == "artifact":
        before = full.s.one("SELECT COUNT(*) AS count FROM artifacts WHERE project=?", (project,))["count"]
        with pytest.raises(Fault) as error:
            full.k.propose(full.owner, project, "design", {
                "title": "null", "statement": "null", "structural_obligations": None,
            })
        after = full.s.one("SELECT COUNT(*) AS count FROM artifacts WHERE project=?", (project,))["count"]
        assert error.value.code == "invalid_input"
        assert after == before
        assert artifact_structural_metadata({"title": "legacy", "statement": "old"}, kind="design")["status"] == "legacy_unavailable"
        with pytest.raises(Fault):
            validate_artifact_structural_obligations(None, kind="design")
        with pytest.raises(Fault):
            Knowledge.validate_body("requirement", {
                "title": "wrong kind null", "statement": "bad", "acceptance": [],
                "structural_obligations": None,
            })
    else:
        domain = _domain(full, project)
        requirement = _requirement(full, project)
        before = full.s.one("SELECT COUNT(*) AS count FROM tasks WHERE project=?", (project,))["count"]
        body = _task_body(requirement, domain, declaration=None)
        body["structural_obligations"] = None
        with pytest.raises(Fault) as error:
            full.w.create(full.owner, project, body)
        after = full.s.one("SELECT COUNT(*) AS count FROM tasks WHERE project=?", (project,))["count"]
        assert error.value.code == "invalid_input"
        assert after == before
        assert task_structural_metadata(_task_body(requirement, domain), project=project)["status"] == "legacy_unavailable"
        with pytest.raises(Fault):
            validate_task_structural_obligations(None, project=project)


@pytest.mark.parametrize("value", [None, True, 1, [], {}, "unknown"])
def test_realization_kind_malformed_values_are_structured_faults(full, value):
    project = full.k.create_project(full.owner, "realization wire")['id']
    domain = _domain(full, project)
    requirement = _requirement(full, project)
    declaration = {
        "format": TASK_STRUCTURAL_FORMAT,
        "required_outputs": [{"id": "output", "statement": "output",
                               "artifact_refs": [_artifact_ref(domain)],
                               "realization_kind": value}],
        "required_exercises": [],
    }
    before = full.s.one("SELECT COUNT(*) AS count FROM tasks WHERE project=?", (project,))["count"]
    with pytest.raises(Fault) as error:
        full.w.create(full.owner, project, _task_body(requirement, domain, declaration=declaration))
    after = full.s.one("SELECT COUNT(*) AS count FROM tasks WHERE project=?", (project,))["count"]
    assert error.value.code == "invalid_input"
    assert after == before


@pytest.mark.parametrize("target", ["artifact", "task"])
def test_denominator_keeps_persisted_explicit_null_unverified(full, target):
    project = full.k.create_project(full.owner, "stored null")['id']
    domain = _domain(full, project)
    requirement = _requirement(full, project)
    if target == "artifact":
        row = full.s.one("SELECT * FROM artifacts WHERE id=?", (domain["id"],))
        body = json.loads(row["body"])
        body["structural_obligations"] = None
        full.s.execute("UPDATE artifacts SET body=?, digest=? WHERE id=?", (canonical(body).decode(), digest(body), domain["id"]))
    else:
        task = full.w.create(full.owner, project, _task_body(requirement, domain))
        row = full.s.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
        body = json.loads(row["body"])
        body["structural_obligations"] = None
        full.s.execute("UPDATE tasks SET body=?, updated=? WHERE id=?", (canonical(body).decode(), timestamp(), task["id"]))
    program = full.p.begin(full.owner, project, full.k.source(full.owner, project, "stored null program")['id'], compact=True)['program']
    context = collect_stage_context(full, full.owner, project=project, program=program, stage="plan")
    if target == "artifact":
        item = next(item for item in context["artifacts"] if item["id"] == domain["id"])
        assert item["structural_obligations"] == {"status": "invalid", "items": [], "reason": "explicit_null"}
        assert any(item["code"] == "artifact_structural_invalid" for item in context["unresolved"])
    else:
        item = next(item for item in context["task_definitions"] if item["id"] == task["id"])
        assert item["structural_obligations"] == {
            "status": "invalid", "required_outputs": [], "required_exercises": [], "reason": "explicit_null",
        }
        assert any(item["code"] == "task_structural_invalid" for item in context["unresolved"])
    denominator = derive_denominator(context)
    extractor = denominator["capabilities"]["extractors"]["artifact_responsibility" if target == "artifact" else "task_structural"]
    assert extractor["invalid"] == 1


def test_b_task_validator_requires_real_reads_and_distinguishes_missing_empty(full):
    project = full.k.create_project(full.owner, "B additive schema")["id"]
    domain = _domain(full, project)
    requirement = _requirement(full, project)
    ref = _artifact_ref(domain)
    declaration = {
        "format": TASK_STRUCTURAL_FORMAT,
        "required_outputs": [{"id": "candidate", "statement": "Produce the candidate",
                               "artifact_refs": [ref], "realization_kind": "candidate_member"}],
        "required_exercises": [{"id": "branch", "statement": "Exercise the branch",
                                 "artifact_refs": [ref]}],
    }
    body = _task_body(requirement, domain, declaration=declaration)
    assert task_structural_metadata(body, project=project)["status"] == "declared"
    assert task_structural_metadata(_task_body(requirement, domain), project=project)["status"] == "legacy_unavailable"
    explicit_empty = _task_body(requirement, domain, declaration={
        "format": TASK_STRUCTURAL_FORMAT, "required_outputs": [], "required_exercises": [],
    })
    assert task_structural_metadata(explicit_empty, project=project)["status"] == "explicit_empty"
    created = full.w.create(full.owner, project, body)
    assert created["body"]["structural_obligations"] == declaration

    outside = copy.deepcopy(declaration)
    outside["required_outputs"][0]["artifact_refs"] = [{
        "kind": "artifact", "project": project, "artifact": "ABSENT", "revision": 1,
        "body_digest": "0" * 64,
    }]
    with pytest.raises(Fault) as error:
        full.w.create(full.owner, project, _task_body(requirement, domain, declaration=outside))
    assert error.value.code in {"invalid_reference", "not_found"}

    with pytest.raises(Fault):
        validate_task_structural_obligations({
            "format": TASK_STRUCTURAL_FORMAT, "required_outputs": [{
                "id": "x", "statement": "x", "artifact_refs": [ref],
                "realization_kind": "unknown", "extra": 1,
            }], "required_exercises": [],
        }, project=project, read_artifacts=[domain["id"], requirement["id"]])


def test_b_denominator_rejects_structural_ref_removed_from_task_reads(full):
    project = full.k.create_project(full.owner, "B registered reads")['id']
    domain = _domain(full, project)
    requirement = _requirement(full, project)
    declaration = {
        "format": TASK_STRUCTURAL_FORMAT,
        "required_outputs": [{"id": "candidate", "statement": "Produce the candidate",
                               "artifact_refs": [_artifact_ref(domain)],
                               "realization_kind": "candidate_member"}],
        "required_exercises": [],
    }
    task = full.w.create(full.owner, project, _task_body(requirement, domain, declaration=declaration))
    with full.s.transaction():
        full.s.execute("DELETE FROM task_reads WHERE task=? AND artifact=?", (task['id'], domain['id']))
    program = full.p.begin(full.owner, project,
                           full.k.source(full.owner, project, "program source")['id'], compact=True)['program']
    with pytest.raises(Fault) as error:
        derive_denominator(collect_stage_context(full, full.owner, project=project,
                                                 program=program, stage='plan'))
    assert error.value.code == 'integrity_error'


def test_b_task_declaration_changes_task_definition_identity(full):
    from daikibo.task_revisions import task_definition_digest

    project = full.k.create_project(full.owner, "B identity")["id"]
    domain = _domain(full, project)
    requirement = _requirement(full, project)
    base = _task_body(requirement, domain)
    empty = _task_body(requirement, domain, declaration={
        "format": TASK_STRUCTURAL_FORMAT, "required_outputs": [], "required_exercises": [],
    })
    assert task_definition_digest(base) != task_definition_digest(empty)


def test_ab_denominator_extracts_explicit_leaves_without_write_path_or_candidate_inference(full):
    project = full.k.create_project(full.owner, "AB denominator")["id"]
    domain = _domain(full, project)
    requirement = _requirement(full, project)
    ref = _artifact_ref(domain)
    declaration = {
        "format": TASK_STRUCTURAL_FORMAT,
        "required_outputs": [{"id": "candidate", "statement": "Make output",
                               "artifact_refs": [ref], "realization_kind": "candidate_member"}],
        "required_exercises": [{"id": "exercise", "statement": "Exercise branch",
                                 "artifact_refs": [ref]}],
    }
    task = full.w.create(full.owner, project, _task_body(requirement, domain, declaration=declaration))
    program = full.p.begin(full.owner, project, full.k.source(full.owner, project, "program source")["id"], compact=True)["program"]
    context = collect_stage_context(full, full.owner, project=project, program=program, stage="plan")
    denominator = derive_denominator(context)
    categories = [item["category"] for item in denominator["obligations"]]
    assert "artifact_responsibility" in categories
    assert categories.count("required_output") == 1
    assert categories.count("required_exercise") == 1
    assert not any(item["category"] == "output_artifact" for item in denominator["obligations"])
    task_item = next(item for item in context["task_definitions"] if item["id"] == task["id"])
    assert task_item["structural_obligations"]["status"] == "declared"
    assert denominator["capabilities"]["extractors"]["artifact_responsibility"]["supported"] is True
    assert denominator["capabilities"]["extractors"]["task_structural"]["supported"] is True

    # Telemetry/state is not semantic input.  The Task definition and all
    # typed material stay byte-identical while only its status changes.
    before = denominator["input_digest"]
    with full.s.transaction():
        full.s.execute("UPDATE tasks SET status='ready',updated=? WHERE id=?", (timestamp(), task["id"]))
    after = derive_denominator(collect_stage_context(full, full.owner, project=project, program=program, stage="plan"))
    assert after["input_digest"] == before


def test_public_metadata_and_skill_copies_expose_the_additive_contract(full):
    artifact = full.describe(full.owner, method="artifact.propose")["methods"]["artifact.propose"]["body_contract"]
    assert artifact["version"] == 2
    assert artifact["additive"]["field"] == "structural_obligations"
    task = full.describe(full.owner, method="task.create")["methods"]["task.create"]["body_contract"]
    assert "structural_obligations" in task["optional"]
    assert task["structural_obligations"]["missing"] == "legacy_unavailable"
    assert structural_contract()["task"]["format"] == TASK_STRUCTURAL_FORMAT
    assert task_definition_contract()["revision_identity"].startswith("SHA-256")
