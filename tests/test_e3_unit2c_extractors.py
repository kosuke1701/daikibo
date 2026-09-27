from __future__ import annotations

import json

import pytest

from daikibo.assurance_denominators import collect_stage_context, derive_denominator, project_task
from daikibo.common import Fault, canonical, digest, timestamp

from test_e3_unit2a_denominators import _fixture, _task


def _valid_breakdown(full, fixture, ident, *, body=None, preserve_scope=False):
    original = full.s.one("SELECT * FROM breakdowns WHERE id=?", (fixture["breakdown"],))
    if body is None:
        if original is None:
            raise AssertionError("a source body is required when no fixture Breakdown exists")
        value = json.loads(original["body"])
    else:
        value = json.loads(json.dumps(body))
    if not preserve_scope:
        value["scope"] = full.breakdowns._scope(full.owner, fixture["project"])
    with full.s.transaction():
        full.s.execute(
            "INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)",
            (ident, fixture["program"], fixture["project"], canonical(value).decode(),
             digest(value), "proposed", None, timestamp()),
        )
    return ident


def _context(full, fixture, breakdown, *, source_partition=False):
    if source_partition:
        proposal = full.traceability.propose(
            full.owner, fixture["project"], kind="document",
            scope={"source": fixture["source"]["id"]},
        )
        full.traceability.extract(full.owner, proposal["id"])
    return collect_stage_context(
        full, full.owner, project=fixture["project"], program=fixture["program"],
        stage="plan", proposed_breakdown=breakdown,
    )


def test_s1_unicode_classifications_and_saved_partition_are_exact(full, tmp_path):
    project = full.k.create_project(full.owner, "Unit2c unicode")['id']
    source = full.k.source(full.owner, project, "要件 A\nASCII tail\n")
    requirement = full.k.propose(full.owner, project, "requirement", {
        "title": "Unicode requirement", "statement": "Keep the source span",
        "acceptance": ["AC-UNICODE"], "source_refs": [source["id"]],
    })
    requirement = full.k.accept(full.owner, requirement["id"], 1)
    full.k.classify(full.owner, source["id"], 0, 4, "requirement", [requirement["id"]], "semantic requirement")
    full.k.classify(full.owner, source["id"], 5, 10, "out_of_scope", [], "background text")
    program = full.p.begin(full.owner, project, source["id"], compact=True)["program"]
    task = _task(full, project, requirement["id"], "Unicode task", [
        {"id": "unicode", "argv": ["python", "-c", "print(1)"], "purpose": "unicode"},
    ])
    body = {"format": "daikibo.breakdown.v1", "program": program, "title": "unicode",
            "rationale": "fixture", "units": [{
                "id": "unicode-unit", "title": "Unicode", "parent": None, "domain": None,
                "rationale": "fixture", "obligations": [{"requirement": requirement["id"], "acceptance": "AC-UNICODE"}],
                "tasks": [task["id"]], "interfaces": [], "dependencies": [],
            }], "scope": {}, "structure": {}, "material_bindings": {}}
    fixture = {"project": project, "program": program, "source": source,
               "parent": requirement, "breakdown": "BREAKDOWN-unicode"}
    breakdown = _valid_breakdown(full, fixture, fixture["breakdown"], body=body)
    context = _context(full, fixture, breakdown, source_partition=True)
    denominator = derive_denominator(context)
    partitions = context["source_partitions"]
    assert len(partitions) == 1
    partition = partitions[0]
    assert partition["status"] == "partitioned"
    assert partition["unclassified"]
    spans = [item for item in denominator["obligations"] if item["category"] == "source_span"]
    assert len(spans) == len(partition["leaves"])
    raw = full.s.blob_get(source["digest"])
    text = raw.decode("utf-8")
    for leaf in partition["leaves"]:
        assert raw[leaf["byte_start"]:leaf["byte_end"]].decode("utf-8") == text[leaf["unicode_start"]:leaf["unicode_end"]]
        assert leaf["source_ref"]["locator"]["blob_digest"] == source["digest"]
    assert denominator["capabilities"]["extractors"]["source_span"]["meaning_review"] == "pending"
    assert not any(item.get("status") == "PASS" for item in denominator["capabilities"].values() if isinstance(item, dict))


def test_s2_missing_partition_cas_is_unresolved_and_never_synthesized(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    breakdown = _valid_breakdown(full, fixture, "BREAKDOWN-s2")
    _context(full, fixture, breakdown, source_partition=True)
    full.s.blob_path(fixture["source"]["digest"]).unlink()
    context = _context(full, fixture, breakdown)
    denominator = derive_denominator(context)
    assert context["source_inputs"][0]["material"]["status"] == "missing"
    assert any(item["code"] in {"source_material_missing", "source_partition_material_unresolved"}
               for item in denominator["unresolved"])
    assert not [item for item in denominator["obligations"] if item["category"] == "source_span"]


def test_s3_classification_refs_change_digest_but_reason_does_not(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    full.k.classify(full.owner, fixture["source"]["id"], 0, 5, "requirement",
                    [fixture["parent"]["id"]], "first reason")
    breakdown = _valid_breakdown(full, fixture, "BREAKDOWN-s3")
    first = derive_denominator(_context(full, fixture, breakdown))
    disposition = full.s.one("SELECT * FROM dispositions WHERE source=?", (fixture["source"]["id"],), True)
    with full.s.transaction():
        full.s.execute("UPDATE dispositions SET reason=? WHERE id=?", ("changed explanation", disposition["id"]))
    second = derive_denominator(_context(full, fixture, breakdown))
    assert second["input_digest"] == first["input_digest"]
    with full.s.transaction():
        full.s.execute("UPDATE dispositions SET refs=? WHERE id=?",
                       (canonical([fixture["child"]["id"]]).decode(), disposition["id"]))
    third = derive_denominator(_context(full, fixture, breakdown))
    assert third["input_digest"] != second["input_digest"]


def test_s4_mechanical_source_coverage_stays_meaning_review_pending(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    breakdown = _valid_breakdown(full, fixture, "BREAKDOWN-s4")
    context = _context(full, fixture, breakdown)
    denominator = derive_denominator(context)
    assert [item for item in denominator["obligations"] if item["category"] == "source_span"]
    extractor = denominator["capabilities"]["extractors"]["source_span"]
    assert extractor["meaning_review"] == "pending"
    assert "relation_results" not in denominator
    assert not any(item.get("criterion") == "meaning_review" and item.get("status") == "PASS"
                   for item in denominator["unresolved"] if isinstance(item, dict))


def test_h1_requirement_and_child_denominators_keep_unassigned_parent(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    orphan = full.k.propose(full.owner, fixture["project"], "requirement", {
        "title": "Orphan", "statement": "No unit assignment", "acceptance": ["AC-ORPHAN"],
        "source_refs": [fixture["source"]["id"]],
    })
    orphan = full.k.accept(full.owner, orphan["id"], 1)
    original = json.loads(full.s.one("SELECT body FROM breakdowns WHERE id=?", (fixture["breakdown"],))["body"])
    original["units"][0]["obligations"].append({"requirement": fixture["child"]["id"], "acceptance": "AC-SHARED"})
    fixture["breakdown"] = _valid_breakdown(full, fixture, "BREAKDOWN-h1", body=original)
    denominator = derive_denominator(_context(full, fixture, fixture["breakdown"]))
    categories = {category: sum(item["category"] == category for item in denominator["obligations"])
                  for category in {item["category"] for item in denominator["obligations"]}}
    assert categories["requirement"] == 3
    assert categories["acceptance_condition"] == 4
    assert categories["child_obligation"] == 3
    child = [item for item in denominator["obligations"] if item["category"] == "child_obligation"]
    assert all("/obligations/" in item["pointer"] for item in child)
    orphan_acceptance = next(item for item in denominator["obligations"]
                             if item["category"] == "acceptance_condition"
                             and item["source_ref"]["locator"]["artifact"] == orphan["id"])
    assert orphan_acceptance["contributors"] == []


def test_h2_child_denominator_does_not_discharge_missing_parent(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    original = json.loads(full.s.one("SELECT body FROM breakdowns WHERE id=?", (fixture["breakdown"],))["body"])
    original["units"][0]["obligations"] = [{"requirement": fixture["child"]["id"], "acceptance": "AC-SHARED"}]
    breakdown = _valid_breakdown(full, fixture, "BREAKDOWN-h2", body=original)
    denominator = derive_denominator(_context(full, fixture, breakdown))
    parent_acs = [item for item in denominator["obligations"]
                  if item["category"] == "acceptance_condition"
                  and item["source_ref"]["locator"]["artifact"] == fixture["parent"]["id"]]
    assert len(parent_acs) == 2
    assert all(item["contributors"] == [] for item in parent_acs)
    assert len([item for item in denominator["obligations"] if item["category"] == "child_obligation"]) == 1


def test_h3_scope_version_and_unit_hierarchy_corruption_are_rejected(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    original = json.loads(full.s.one("SELECT body FROM breakdowns WHERE id=?", (fixture["breakdown"],))["body"])
    original["scope"] = {"requirements": []}
    stale = _valid_breakdown(full, fixture, "BREAKDOWN-h3-stale", body=original, preserve_scope=True)
    with pytest.raises(Fault) as error:
        _context(full, fixture, stale)
    assert error.value.code == "integrity_error"

    cyclic = json.loads(full.s.one("SELECT body FROM breakdowns WHERE id=?", (fixture["breakdown"],))["body"])
    cyclic["units"][0]["parent"] = cyclic["units"][0]["id"]
    cyclic_id = _valid_breakdown(full, fixture, "BREAKDOWN-h3-cycle", body=cyclic)
    with pytest.raises(Fault) as error:
        _context(full, fixture, cyclic_id)
    assert error.value.code == "integrity_error"


def test_h4_global_digest_and_local_task_projection_preserve_joint_assignment(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    task_b = _task(full, fixture["project"], fixture["parent"]["id"], "Task B", [
        {"id": "b", "argv": ["python", "-c", "print(2)"], "purpose": "b"},
    ])
    task_c = _task(full, fixture["project"], fixture["parent"]["id"], "Task C", [
        {"id": "c", "argv": ["python", "-c", "print(3)"], "purpose": "c"},
    ])
    original = json.loads(full.s.one("SELECT body FROM breakdowns WHERE id=?", (fixture["breakdown"],))["body"])
    original["units"][0]["tasks"] = [fixture["task_a"]["id"], task_b["id"]]
    breakdown = _valid_breakdown(full, fixture, "BREAKDOWN-h4", body=original)
    context = _context(full, fixture, breakdown)
    denominator = derive_denominator(context)
    refs = {item["id"]: item["task_ref"] for item in context["task_definitions"]}
    a = project_task(denominator, refs[fixture["task_a"]["id"]])
    b = project_task(denominator, refs[task_b["id"]])
    c = project_task(denominator, refs[task_c["id"]])
    assert a["global_digest"] == b["global_digest"] == c["global_digest"] == denominator["digest"]
    child_ids = {item["id"] for item in denominator["obligations"] if item["category"] == "child_obligation"}
    assert child_ids & set(a["obligation_ids"])
    assert child_ids & set(b["obligation_ids"])
    assert not child_ids & set(c["obligation_ids"])


def test_legacy_empty_breakdown_scope_is_explicitly_unavailable(full, tmp_path):
    fixture = _fixture(full, tmp_path)
    context = _context(full, fixture, fixture["breakdown"])
    denominator = derive_denominator(context)
    extractors = denominator["capabilities"]["extractors"]
    assert extractors["requirement"] == {
        "supported": False, "version": "requirement-scope.v1", "reason": "legacy_scope_missing",
    }
    assert extractors["child_obligation"] == {
        "supported": False, "version": "requirement-hierarchy.v1",
        "reason": "legacy_scope_missing", "meaning_review": "not_applicable",
    }
    assert not [item for item in denominator["obligations"] if item["category"] in {"requirement", "child_obligation"}]
