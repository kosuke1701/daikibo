"""Owner partition contract tests for Unit5 relation denominators."""
from __future__ import annotations

from daikibo.assurance import Assurance
from daikibo.common import digest
from daikibo.task_revisions import task_definition_digest


def _task(project: str, task: str) -> dict:
    return {
        "kind": "task_revision", "project": project, "task": task,
        "revision": 1, "definition_digest": "a" * 64,
    }


def _obligations(items: list[dict]) -> dict:
    return {"project": "project", "body": {"project": "project", "obligations": items}}


def _owner_ids(assurance, obligations, center):
    return assurance._set_owner_obligation_ids(
        obligations, center, "produced_by", "incoming",
    )


def test_untyped_scope_population_keeps_full_denominator():
    assurance = object.__new__(Assurance)
    center = _task("project", "TASK-A")
    obligations = _obligations([
        {"id": "obligation-a"}, {"id": "obligation-b"},
    ])

    assert _owner_ids(assurance, obligations, center) == {
        "obligation-a", "obligation-b",
    }


def test_partial_owner_typing_does_not_shrink_denominator():
    assurance = object.__new__(Assurance)
    center = _task("project", "TASK-A")
    obligations = _obligations([
        {"id": "obligation-a", "contributors": [{"task_ref": center}]},
        {"id": "obligation-b"},
    ])

    assert _owner_ids(assurance, obligations, center) == {
        "obligation-a", "obligation-b",
    }


def test_complete_typed_owner_partitions_cover_the_original_population():
    assurance = object.__new__(Assurance)
    task_a = _task("project", "TASK-A")
    task_b = _task("project", "TASK-B")
    obligations = _obligations([
        {"id": "obligation-a", "contributors": [{"task_ref": task_a}]},
        {"id": "obligation-b", "contributors": [{"task_ref": task_b}]},
        {"id": "obligation-shared", "contributors": [
            {"task_ref": task_a}, {"task_ref": task_b},
        ]},
    ])

    owner_a = _owner_ids(assurance, obligations, task_a)
    owner_b = _owner_ids(assurance, obligations, task_b)
    population = {item["id"] for item in obligations["body"]["obligations"]}
    assert owner_a == {"obligation-a", "obligation-shared"}
    assert owner_b == {"obligation-b", "obligation-shared"}
    assert owner_a | owner_b == population


def test_invalid_typed_owner_is_unresolved_and_keeps_full_population():
    assurance = object.__new__(Assurance)
    center = _task("project", "TASK-A")
    obligations = _obligations([
        {"id": "obligation-a", "contributors": [{"task_ref": center}]},
        {"id": "obligation-b", "contributors": [{"task_ref": {
            "kind": "task_revision", "project": "foreign", "task": "TASK-X",
            "revision": 1, "definition_digest": "b" * 64,
            "unexpected": True,
        }}]},
    ])

    assert _owner_ids(assurance, obligations, center) == {
        "obligation-a", "obligation-b",
    }


def test_valid_shape_foreign_owner_cannot_reduce_scope():
    """A schema-valid owner in another project remains in the denominator."""
    assurance = object.__new__(Assurance)
    center = _task("project", "TASK-A")
    foreign = _task("foreign", "TASK-X")
    obligations = _obligations([
        {"id": "obligation-local", "contributors": [{"task_ref": center}]},
        {"id": "obligation-foreign", "contributors": [{"task_ref": foreign}]},
    ])

    assert _owner_ids(assurance, obligations, center) == {
        "obligation-local", "obligation-foreign",
    }


def test_foreign_center_keeps_scope_denominator():
    assurance = object.__new__(Assurance)
    obligations = _obligations([
        {"id": "obligation-a", "contributors": [{"task_ref": _task("project", "TASK-A")}]},
    ])

    assert _owner_ids(assurance, obligations, _task("foreign", "TASK-X")) == {
        "obligation-a",
    }
    assert _owner_ids(assurance, obligations, {"kind": "malformed"}) == {
        "obligation-a",
    }


def test_production_owner_partition_resolves_current_task_inside_scope(full):
    """The narrowing path needs an adopted project/scope and current Task material."""
    project = full.k.create_project(full.owner, "Owner material")['id']
    source = full.k.source(full.owner, project, "Owner material source.")
    requirement = full.k.propose(full.owner, project, "requirement", {
        "title": "Owner requirement", "statement": "The task owns this obligation.",
        "acceptance": ["AC-OWNER"], "source_refs": [source["id"]],
    })
    full.k.accept(full.owner, requirement["id"], 1)
    artifact_ref = {
        "kind": "artifact", "project": project, "artifact": requirement["id"],
        "revision": requirement["revision"], "body_digest": requirement["digest"],
    }
    scope = full.assurance.scope_propose(full.owner, project, {
        "roots": [artifact_ref], "selection_rules": {},
        "exclusion_proposals": [], "authority_refs": [], "discovery_unknowns": [],
    })
    task = full.w.create(full.owner, project, {
        "title": "Owner task", "goal": "Retain current owner material.",
        "read_artifacts": [requirement["id"]], "write_paths": ["owner.py"],
        "acceptance": ["AC-OWNER"], "dependencies": [], "repos": [], "non_goals": [],
    })
    task_ref = {
        "kind": "task_revision", "project": project, "task": task["id"],
        "revision": task["revision"],
        "definition_digest": task_definition_digest(task["body"]),
    }
    obligations = {
        "project": project,
        "body": {"project": project, "scope_ref": scope["scope_ref"], "obligations": [
            {"id": "obligation-owner", "contributors": [{"task_ref": task_ref}]},
        ]},
    }

    assert full.assurance._set_owner_obligation_ids(
        obligations, task_ref, "produced_by", "incoming", full.owner,
    ) == {"obligation-owner"}

    stale = dict(task_ref, definition_digest=digest({"changed": True}))
    assert full.assurance._set_owner_obligation_ids(
        obligations, stale, "produced_by", "incoming", full.owner,
    ) == {"obligation-owner"}

    foreign = dict(task_ref, task="TASK-MISSING")
    unresolved_obligations = {
        "project": project,
        "body": {"project": project, "scope_ref": scope["scope_ref"], "obligations": [
            {"id": "obligation-owner", "contributors": [{"task_ref": foreign}]},
        ]},
    }
    assert full.assurance._set_owner_obligation_ids(
        unresolved_obligations, task_ref, "produced_by", "incoming", full.owner,
    ) == {"obligation-owner"}
