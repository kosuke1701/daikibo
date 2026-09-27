"""Standalone public Delivery profile reader contract checks.

These tests intentionally use only the public product composition and the
test fixture's local store.  They do not import the plan-only driver.
"""
from __future__ import annotations

import copy
import json

import pytest

from conftest import make_task
from daikibo.common import Fault, canonical, digest


def _profile(repo: str, requirement: str, task: str) -> dict:
    checks = [
        {"id": "build", "category": "build", "repo": repo,
         "kind": "command", "argv": ["python", "-c", "print('build')"],
         "purpose": "build fixture"},
        {"id": "start", "category": "start", "repo": repo,
         "kind": "command", "argv": ["python", "-c", "print('start')"],
         "purpose": "start fixture"},
    ]
    for category in ("smoke", "integration", "scenario"):
        checks.append({"id": category, "category": category, "repo": repo,
                       "kind": "pytest", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                       "required_tests": ["test_add"]})
    return {
        "target_environment": "CPython fixture",
        "required_requirements": [requirement],
        "required_tasks": [task],
        "repo_order": [repo],
        "rollback": "Restore the fixture snapshot",
        "applicability": {
            category: {"applicable": False, "reason": "fixture"}
            for category in ("migration", "security", "performance", "contract")
        },
        "checks": checks,
    }


def _configured(control, full_project):
    project, repo, requirement, _root = full_project
    task = make_task(control, full_project)
    body = _profile(repo, requirement, task)
    configured = control.invoke(control.owner, "delivery.configure", {"project": project, "body": body})
    return project, repo, requirement, task, body, configured


def test_profile_current_is_readonly_and_reports_policy_currentness(full, full_project):
    project, repo, requirement, task, body, configured = _configured(full, full_project)
    before = full.s.one("SELECT total_changes() AS n")["n"]
    value = full.invoke(full.owner, "delivery.profile_current", {"project": project})
    after = full.s.one("SELECT total_changes() AS n")["n"]

    assert value["digest"] == configured["digest"]
    assert value["body"] == body
    assert value["stored_body"]["baseline_snapshot"] == value["baseline_snapshot"]
    assert value["policy_current"] is True
    assert value["current_policy_digest"] == value["scope"]["policy"]
    assert value["scope_currentness"]["policy_current"] is True
    assert value["read_only"] is True
    assert after == before


def test_profile_current_rejects_malformed_scope_policy(full, full_project):
    project, _repo, _requirement, _task, _body, _configured_result = _configured(full, full_project)
    row = full.invoke(full.owner, "delivery.profile_current", {"project": project})
    scope = copy.deepcopy(row["scope"])
    scope["policy"] = "arbitrary-unbound-policy"
    full.s.execute("UPDATE profiles SET scope=? WHERE project=?", (json.dumps(scope), project))

    with pytest.raises(Fault) as caught:
        full.invoke(full.owner, "delivery.profile_current", {"project": project})
    assert caught.value.code == "profile_corrupt"


def test_profile_current_reads_valid_historical_policy_and_marks_it_noncurrent(full, full_project):
    project, _repo, _requirement, _task, _body, _configured_result = _configured(full, full_project)
    old = full.g.policy(project)
    new_body = copy.deepcopy(old["body"])
    new_body["max_parallel"] = old["body"]["max_parallel"] + 1
    new_digest = digest(new_body)
    full.s.execute(
        "UPDATE policies SET revision=?,body=?,digest=? WHERE project=?",
        (old["revision"] + 1, canonical(new_body).decode(), new_digest, project),
    )

    value = full.invoke(full.owner, "delivery.profile_current", {"project": project})
    assert value["scope"]["policy"] == old["digest"]
    assert value["current_policy_digest"] == new_digest
    assert value["policy_current"] is False
    assert value["scope_currentness"] == {
        "policy_current": False,
        "stored_policy_digest": old["digest"],
        "current_policy_digest": new_digest,
    }
