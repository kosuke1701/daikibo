from __future__ import annotations

import json

import pytest

from daikibo.assurance import MAX_PAGE
from daikibo.assurance_relations import REGISTRY_DIGEST
from daikibo.common import Fault, canonical, digest, timestamp


def _artifact_ref(project, row):
    return {"kind": "artifact", "project": project, "artifact": row["id"],
            "revision": row["revision"], "body_digest": row["digest"]}


def _accepted_artifacts(full, project):
    source = full.k.source(full.owner, project, "E2 source")
    requirement = full.k.propose(
        full.owner, project, "requirement",
        {"title": "Requirement", "statement": "The requirement is explicit.",
         "acceptance": ["AC-E2"], "source_refs": [source["id"]]},
    )
    requirement = full.k.accept(full.owner, requirement["id"], 1)
    design = full.k.propose(
        full.owner, project, "design",
        {"title": "Design", "statement": "The design realizes the requirement.",
         "source_refs": [source["id"]]},
    )
    design = full.k.accept(full.owner, design["id"], 1)
    return source, requirement, design


def test_e2_edge_set_packets_and_adoption_gate(full):
    project = full.k.create_project(full.owner, "E2 edge and set")['id']
    _source, requirement, design = _accepted_artifacts(full, project)
    requirement_ref = _artifact_ref(project, requirement)
    design_ref = _artifact_ref(project, design)

    scope_result = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [requirement_ref], "selection_rules": {},
         "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": []},
    )
    profile_result = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope_result["scope_ref"],
         "stage_rules": {"plan": {}, "task": {}, "integration": {}, "delivery": {}},
         "relation_selectors": ["realizes"], "test_definition_bindings": []},
    )
    obligation = scope_result["obligations"]["body"]["obligations"][0]["id"]
    edge_result = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": design_ref, "target_ref": requirement_ref, "relation": "realizes",
         "scope_ref": profile_result["profile_ref"], "claim": "Design realizes the requirement.",
         "obligation_ids": [obligation], "required_evidence_refs": [], "authority_refs": []},
    )
    set_result = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": design_ref, "relation": "realizes", "direction": "outgoing",
         "scope_ref": profile_result["profile_ref"], "criteria": {},
         "required_evidence_refs": []},
    )

    assert set_result["missing_obligations"] == []
    assert set_result["criteria"]["all_obligations_covered"] is True
    packets = set_result["packets"]
    assert any(packet["body"]["review_kind"] == "relation_set" and
               packet["body"]["required_roles"] == ["trace"] for packet in packets)
    assert any(packet["body"]["review_kind"] == "relation_set" and
               packet["body"]["required_roles"] == ["impact"] for packet in packets)
    assert all(len(packet["body"]["leaf_manifest"]) <= MAX_PAGE for packet in packets)
    review_page = full.assurance.review_subject(full.owner, project, set_result["set"]["id"], limit=1)
    assert review_page["total"] == len(packets)
    assert review_page["packets"]
    assert edge_result["packets"][0]["body"]["required_roles"] == ["trace"]
    with pytest.raises(Fault) as missing_review:
        full.assurance.adopt(full.owner, project, set_result["set"]["id"],
                             set_result["set"]["digest"], None, [])
    # The dependency gate runs before review receipt lookup.  A proposed set
    # cannot bypass the not-yet-adopted scope/profile/edge dependencies by
    # presenting an empty receipt list.
    assert missing_review.value.code == "stale_reference"


def test_e2_set_uses_source_container_membership(full):
    project = full.k.create_project(full.owner, "E2 source membership")['id']
    source, requirement, _design = _accepted_artifacts(full, project)
    raw = full.s.blob_get(source["digest"])
    span = {"kind": "traceability_ref", "project": project, "locator": {
        "ref_type": "source_span", "source_id": source["id"], "blob_digest": source["digest"],
        "byte_start": 0, "byte_end": len(raw), "unicode_start": 0,
        "unicode_end": len(raw.decode("utf-8")), "span_hash": digest(raw),
    }}
    scope_result = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [{"kind": "source", "project": project, "source": source["id"],
                    "blob_digest": source["digest"]}], "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []},
    )
    profile_result = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope_result["scope_ref"], "stage_rules": {"plan": {}},
         "relation_selectors": ["extracted_from"], "test_definition_bindings": []},
    )
    edge = full.assurance.edge_propose(
        full.owner, project,
        {"source_ref": _artifact_ref(project, requirement), "target_ref": span,
         "relation": "extracted_from", "scope_ref": profile_result["profile_ref"],
         "claim": "Requirement is extracted from the source span.",
         "obligation_ids": [], "required_evidence_refs": [], "authority_refs": []},
    )
    result = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": {"kind": "source", "project": project, "source": source["id"],
                        "blob_digest": source["digest"]},
         "relation": "extracted_from", "direction": "incoming",
         "scope_ref": profile_result["profile_ref"], "criteria": {},
         "required_evidence_refs": []},
    )
    manifest = result["manifest"]["body"]
    payload = json.loads(full.s.blob_get(manifest["payload_blob"]))
    assert payload["count"] == 1
    assert payload["stream_digest"] == digest([(edge["edge"]["id"], 1, edge["edge"]["digest"])])


def test_e2_material_nested_cas_missing_is_not_optional(full):
    project = full.k.create_project(full.owner, "E2 CAS")['id']
    with pytest.raises(Fault) as missing:
        full.assurance.store_material(
            full.owner, project, "verification_execution",
            {"format": "execution.v1", "input_snapshot_blob": "0" * 64}, [],
            {"kind": "test", "id": "missing"}, {"source": "negative"},
        )
    assert missing.value.code == "missing_evidence"


def test_e2_pinned_plan_identity_cannot_cross_tasks(full, tmp_path):
    project = full.k.create_project(full.owner, "E2 plan identity")['id']
    source = full.k.source(full.owner, project, "plan source")
    req = full.k.propose(full.owner, project, "requirement",
                         {"title": "R", "statement": "R", "acceptance": ["A"],
                          "source_refs": [source["id"]]})
    full.k.accept(full.owner, req["id"], 1)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "x.py").write_text("x\n")
    repository = full.sn.register(full.owner, project, "app", str(repo_root))["id"]
    task_body = {"title": "task", "goal": "x", "read_artifacts": [req["id"]],
                 "write_paths": ["x.py"], "acceptance": ["A"], "dependencies": [],
                 "repos": [repository], "non_goals": []}
    task_a = full.w.create(full.owner, project, task_body)
    task_b = full.w.create(full.owner, project, {**task_body, "title": "task B"})
    plan_a = full.w.plan_tests(full.owner, task_a["id"], {"checks": [{
        "id": "a", "argv": ["true"], "kind": "pytest", "report": "a.xml",
        "required_tests": ["a"], "purpose": "a"}]})
    full.w.plan_tests(full.owner, task_b["id"], {"checks": [{
        "id": "b", "argv": ["true"], "kind": "pytest", "report": "b.xml",
        "required_tests": ["b"], "purpose": "b"}]})
    pinned = full.assurance.pin(full.owner, project, {"kind": "test_plan", "task": task_a["id"],
                                                     "task_revision": 1, "plan_digest": plan_a["digest"]})
    wrong = {**pinned["ref"], "task": task_b["id"]}
    with pytest.raises(Fault) as mismatch:
        full.assurance.resolve_pinned(full.owner, wrong)
    assert mismatch.value.code == "integrity_error"


def test_e2_archive_rejects_missing_e2_endpoint(full, tmp_path):
    project = full.k.create_project(full.owner, "E2 archive closure")['id']
    fake = {"kind": "artifact", "project": project, "artifact": "ABSENT",
            "revision": 1, "body_digest": "a" * 64}
    scope = full.assurance.store_object(
        full.owner, project, "scope", "scope:fake", 1,
        {"format": "assurance.scope.v1", "project": project, "roots": [fake],
         "selection_rules": {}, "exclusion_proposals": [], "authority_refs": [],
         "discovery_unknowns": []},
    )
    full.assurance.store_object(
        full.owner, project, "profile", "profile:fake", 1,
        {"format": "assurance.profile.v1", "project": project,
         "scope_ref": full.assurance._object_ref(scope), "stage_rules": {"plan": {}},
         "relation_selectors": [], "test_definition_bindings": []},
    )
    with pytest.raises(Fault) as rejected:
        full.k.baseline(full.owner, project, layout="chunked", chunk_bytes=1024)
    assert rejected.value.code == "invalid_archive"


def test_e2_100k_edge_partition_and_bounded_packets(full):
    project = full.k.create_project(full.owner, "E2 100k")['id']
    scope = full.assurance.scope_propose(
        full.owner, project,
        {"roots": [], "selection_rules": {}, "exclusion_proposals": [],
         "authority_refs": [], "discovery_unknowns": []},
    )
    profile = full.assurance.profile_propose(
        full.owner, project, None,
        {"scope_ref": scope["scope_ref"], "stage_rules": {"plan": {}},
         "relation_selectors": ["realizes"], "test_definition_bindings": []},
    )
    center = {"kind": "artifact", "project": project, "artifact": "SRC",
              "revision": 1, "body_digest": "1" * 64}
    target = {"kind": "artifact", "project": project, "artifact": "TGT",
              "revision": 1, "body_digest": "2" * 64}
    obligation = scope["obligations"]["body"]["obligations"][0]["id"]
    registry_digest = full.assurance.catalog(full.owner)["registry_digest"]
    now = timestamp()
    rows = []
    for index in range(100_000):
        body = {"format": "assurance.edge.v1", "project": project,
                "source_ref": center, "target_ref": target, "relation": "realizes",
                "relation_contract_digest": registry_digest, "scope_ref": profile["profile_ref"],
                "claim": str(index), "obligation_ids": [obligation],
                "required_evidence_refs": [], "authority_refs": []}
        rows.append((f"E2-{index:06d}", project, "edge", f"bench:{index:06d}", 1,
                     canonical(body).decode(), digest(body), now + index / 1_000_000))
    with full.s.transaction():
        full.s.conn.executemany(
            "INSERT INTO assurance_objects(id,project,kind,logical_id,revision,body,digest,created) "
            "VALUES(?,?,?,?,?,?,?,?)", rows,
        )
    result = full.assurance.set_propose(
        full.owner, project,
        {"center_ref": center, "relation": "realizes", "direction": "outgoing",
         "scope_ref": profile["profile_ref"], "criteria": {}, "required_evidence_refs": []},
    )
    manifest = json.loads(full.s.blob_get(result["manifest"]["body"]["payload_blob"]))
    assert manifest["count"] == 100_000
    assert len(manifest["partitions"]) == 200
    assert len(result["packets"]) == 201
    assert all(len(packet["body"]["leaf_manifest"]) <= MAX_PAGE for packet in result["packets"])
