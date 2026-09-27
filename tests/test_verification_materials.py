import json
import sys
from dataclasses import replace

import pytest

import daikibo.runtime as runtime_module
from daikibo.common import Fault, canonical, digest
from daikibo.verification_materials import (
    EXECUTION_MATERIAL_KIND,
    MATERIAL_FORMAT,
    VerificationMaterialCoordinator,
)
from conftest import finish_task, make_task
from test_delivery_git_and_recovery import profile


class MaterialStore:
    """Small E1 boundary double; it owns objects, while the helper owns none."""

    def __init__(self, blobs):
        self.blobs = blobs
        self.objects = {}
        self.calls = []

    def store_material(self, actor, project, material_kind, payload, dependencies, origin, captured_from):
        payload_blob = self.blobs.blob_put(canonical(payload))
        body = {"format": MATERIAL_FORMAT, "material_kind": material_kind,
                "project": project, "origin": origin,
                "semantic_digest": digest(payload), "payload_blob": payload_blob,
                "dependency_refs": dependencies, "captured_from": captured_from}
        ident = f"AOBJ-{len(self.objects) + 1}"
        row = {"id": ident, "project": project, "kind": "material",
               "logical_id": f"{material_kind}:{digest(body)}", "revision": 1,
               "body": body, "digest": digest(body)}
        self.objects[ident] = row
        self.calls.append(row)
        return row

    def object_get(self, actor, project, object_id):
        row = self.objects.get(object_id)
        assert row and row["project"] == project
        return row


class BlobStore:
    def __init__(self):
        self.blobs = {}

    def blob_put(self, value):
        if isinstance(value, str):
            value = value.encode()
        ident = digest(value)
        self.blobs[ident] = bytes(value)
        return ident

    def blob_get(self, ident):
        return self.blobs[ident]


class RuntimeDouble:
    def __init__(self):
        self.s = BlobStore()
        self.assurance = MaterialStore(self.s)


def _task_row(project):
    body = {"title": "test", "goal": "goal", "read_artifacts": [],
            "write_paths": ["x.py"], "acceptance": ["AC"],
            "dependencies": [], "repos": [], "non_goals": []}
    return {"id": "TASK-1", "project": project, "revision": 3, "body": body,
            "candidate": "CAND-1", "status": "submitted"}


def _plan_row():
    body = {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest"],
                         "kind": "pytest", "required_tests": ["test_unit"]}]}
    return {"body": body, "digest": digest(body)}


def _candidate_row(project):
    body = {"snapshot": {"format": "snapshot.v1", "repos": {},
                          "digest": digest({"format": "snapshot.v1", "repos": {}})},
            "changes": [], "findings": [], "implementation_receipt": "EVD-1"}
    return {"id": "CAND-1", "project": project, "digest": digest(body), "body": body}


def test_definition_and_adjusted_check_digests_are_separate():
    runtime = RuntimeDouble()
    coordinator = VerificationMaterialCoordinator(runtime)
    project = "PRJ-1"
    actor = object()
    task = _task_row(project)
    plan = _plan_row()
    plan_ref, _ = coordinator.pin_test_plan(actor, project, task, plan,
                                            captured_from={"controller": "runtime", "operation": "test"})
    original = plan["body"]["checks"][0]
    definition_ref = coordinator.test_plan_check_ref(project, plan_ref, original)
    candidate_ref = coordinator.candidate_ref(project, task, _candidate_row(project))
    context = coordinator.context(
        actor=actor, definition_ref=definition_ref,
        execution_subject={"kind": "task", "id": task["id"], "binding": "b"},
        task_revision=task["revision"], candidate_ref=candidate_ref, binding="b",
        revalidate=lambda: {"current": True},
        captured_from={"controller": "runtime", "operation": "test"})
    adjusted = {**original, "report": "repo/results.xml", "build_inputs": [], "build_outputs": []}
    result = coordinator.prepare_execution(
        actor, project, context, check=adjusted,
        argv=[sys.executable, "-m", "pytest", "--junitxml", "repo/results.xml"],
        cwd_relative="repo", snapshot=_candidate_row(project)["body"]["snapshot"],
        timeout=17, extra_env={"CHECK_MODE": "test"},
        managed_context={"DAIKIBO_RUN_ID": "RUN-1"}, run_id="RUN-1",
        effective_env={"PATH": "/private/bin", "CHECK_MODE": "test",
                       "DAIKIBO_RUN_ID": "RUN-1", "INHERITED_FLAG": "kept"})
    payload = result["payload"]
    assert definition_ref["check_digest"] == digest(original)
    assert payload["runtime_check_blob"] == digest(canonical(adjusted))
    assert payload["runtime_check_blob"] != definition_ref["check_digest"]
    assert result["pin"]["id"] in runtime.assurance.objects
    assert runtime.assurance.objects[result["pin"]["id"]]["body"]["material_kind"] == EXECUTION_MATERIAL_KIND
    assert runtime.assurance.objects[result["pin"]["id"]]["body"]["format"] == MATERIAL_FORMAT
    environment = json.loads(runtime.s.blob_get(payload["environment_blob"]))
    assert environment["effective_environment"] == {
        "PATH": "/private/bin", "CHECK_MODE": "test",
        "DAIKIBO_RUN_ID": "RUN-1", "INHERITED_FLAG": "kept",
    }
    assert environment["extra_env"] == {"CHECK_MODE": "test"}
    assert environment["managed_context"] == {"DAIKIBO_RUN_ID": "RUN-1"}


def test_stale_revalidation_rejects_before_subprocess():
    runtime = RuntimeDouble()
    coordinator = VerificationMaterialCoordinator(runtime)
    project = "PRJ-1"
    actor = object()
    task = _task_row(project)
    plan = _plan_row()
    plan_ref, _ = coordinator.pin_test_plan(actor, project, task, plan)
    definition_ref = coordinator.test_plan_check_ref(project, plan_ref, plan["body"]["checks"][0])
    candidate_ref = coordinator.candidate_ref(project, task, _candidate_row(project))
    calls = []

    def revalidate():
        calls.append(True)
        return {"current": len(calls) == 1}

    context = coordinator.context(
        actor=actor, definition_ref=definition_ref,
        execution_subject={"kind": "task", "id": task["id"], "binding": "b"},
        task_revision=task["revision"], candidate_ref=candidate_ref, binding="b",
        revalidate=revalidate)
    result = coordinator.prepare_execution(
        actor, project, context, check=plan["body"]["checks"][0], argv=[sys.executable, "-c", "pass"],
        cwd_relative="repo", snapshot=_candidate_row(project)["body"]["snapshot"],
        timeout=1, extra_env={}, managed_context={}, run_id="RUN-2")
    assert result["pin"]["digest"]
    with pytest.raises(Fault) as exc:
        current = context.revalidate()
        if current is False or current.get("current") is False:
            raise Fault("stale_verification_material", "stale")
    assert exc.value.code == "stale_verification_material"


def test_missing_or_fake_material_is_not_accepted():
    runtime = RuntimeDouble()
    coordinator = VerificationMaterialCoordinator(runtime)
    with pytest.raises(Fault) as missing:
        coordinator.validate_stored_pin(object(), "PRJ-1", {"id": "AOBJ-missing", "digest": "0" * 64})
    assert missing.value.code == "invalid_verification_material"
    runtime.assurance.objects["AOBJ-fake"] = {
        "id": "AOBJ-fake", "project": "PRJ-1", "kind": "material", "digest": "1" * 64,
        "body": {"format": MATERIAL_FORMAT, "material_kind": EXECUTION_MATERIAL_KIND,
                  "payload_blob": "0" * 64, "semantic_digest": "0" * 64, "captured_from": {}}
    }
    with pytest.raises((Fault, KeyError)):
        coordinator.validate_stored_pin(object(), "PRJ-1", {"id": "AOBJ-fake", "digest": "1" * 64})


def test_task_tests_bind_same_material_to_run_and_receipt(system, project, task):
    system.rt.assurance = MaterialStore(system.s)
    pid = project[0]
    system.w.claim(system.owner, pid, task)
    system.rt.execute(system.owner, task, "fixture")
    result = system.rt.tests(system.owner, task)
    item = result["checks"][0]
    receipt = system.g.receipt(item["receipt"])
    run = system.rt.run_status(system.owner, receipt["run"])
    assert item["result"]["passed"] is True
    assert receipt["verification_material"] == item["verification_material"]
    assert run["body"]["verification_material"] == item["verification_material"]
    material = system.rt.assurance.objects[item["verification_material"]["id"]]
    payload = json.loads(system.s.blob_get(material["body"]["payload_blob"]))
    assert material["body"]["material_kind"] == EXECUTION_MATERIAL_KIND
    assert payload["definition_ref"]["kind"] == "test_plan_check"
    assert payload["runtime_check_blob"] == digest(system.s.blob_get(payload["runtime_check_blob"]))
    actual_plan = json.loads(system.s.one("SELECT body FROM plans WHERE task=?", (task,), True)["body"])
    assert payload["definition_ref"]["check_digest"] == digest(actual_plan["checks"][0])


def test_real_task_pins_the_exact_environment_passed_to_popen(full, full_project, monkeypatch):
    project, repo, requirement, _ = full_project
    task = full.w.create(full.owner, project, {
        "title": "Pin environment",
        "goal": "WRITE:{}".format(json.dumps({"calc.py": "def add(a,b):\n    return a+b\n"})),
        "read_artifacts": [requirement], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repo],
        "non_goals": [],
    })["id"]
    full.w.plan_tests(full.owner, task, {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    full.w.ready(full.owner, task)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")

    popen_envs = []
    original_popen = runtime_module.subprocess.Popen

    def capture_popen(*args, **kwargs):
        popen_envs.append(dict(kwargs["env"]))
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(runtime_module.subprocess, "Popen", capture_popen)
    result = full.rt.tests(full.owner, task)
    item = result["checks"][0]
    receipt = full.g.receipt(item["receipt"])
    run = full.rt.run_status(full.owner, receipt["run"])
    material = full.assurance.object_get(full.owner, project, item["verification_material"]["id"])
    payload = json.loads(full.s.blob_get(material["body"]["payload_blob"]))
    environment = json.loads(full.s.blob_get(payload["environment_blob"]))

    assert item["result"]["passed"] is True
    assert len(popen_envs) == 1
    assert popen_envs[0] == environment["effective_environment"]
    assert run["body"]["verification_material"] == receipt["verification_material"]
    assert environment["effective_environment"]["DAIKIBO_TASK_ID"] == task
    assert payload["definition_ref"]["kind"] == "test_plan_check"


def test_real_task_rejects_changed_currentness_before_popen(full, full_project, monkeypatch):
    project, repo, requirement, _ = full_project
    task = full.w.create(full.owner, project, {
        "title": "Reject stale environment",
        "goal": "WRITE:{}".format(json.dumps({"calc.py": "def add(a,b):\n    return a+b\n"})),
        "read_artifacts": [requirement], "write_paths": ["calc.py"],
        "acceptance": ["AC-ADD"], "dependencies": [], "repos": [repo],
        "non_goals": [],
    })["id"]
    full.w.plan_tests(full.owner, task, {
        "checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                    "kind": "pytest", "required_tests": ["test_add"]}],
    })
    full.w.ready(full.owner, task)
    full.w.claim(full.owner, project, task)
    full.rt.execute(full.owner, task, "fixture")

    original_context = full.rt.verification_materials.context

    def stale_context(*args, **kwargs):
        context = original_context(*args, **kwargs)
        checks = iter(({"current": True}, {"current": False, "reason": "changed"}))
        return replace(context, revalidate=lambda: next(checks))

    popen_calls = []
    original_popen = runtime_module.subprocess.Popen

    def capture_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(full.rt.verification_materials, "context", stale_context)
    monkeypatch.setattr(runtime_module.subprocess, "Popen", capture_popen)
    with pytest.raises(Fault) as exc:
        full.rt.tests(full.owner, task)
    assert exc.value.code == "stale_verification_material"
    assert popen_calls == []
    row = full.s.one("SELECT status FROM runs WHERE subject=? ORDER BY start DESC LIMIT 1", (task,), True)
    assert row["status"] == "unknown"


def test_real_delivery_pins_each_subprocess_environment(full, full_project, monkeypatch):
    project, repository, _, _ = full_project
    task = make_task(full, full_project)
    full.d.configure(full.owner, project, profile(project, repository, full_project[2], task))
    finish_task(full, project, task)
    delivery = full.d.prepare(full.owner, project)["id"]

    popen_envs = []
    original_popen = runtime_module.subprocess.Popen

    def capture_popen(*args, **kwargs):
        popen_envs.append(dict(kwargs["env"]))
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(runtime_module.subprocess, "Popen", capture_popen)
    result = full.d.verify(full.owner, delivery)
    assert all(item["passed"] for item in result["results"])
    assert len(popen_envs) == len(result["results"])
    for popen_env, item in zip(popen_envs, result["results"]):
        material = full.assurance.object_get(full.owner, project, item["verification_material"]["id"])
        payload = json.loads(full.s.blob_get(material["body"]["payload_blob"]))
        environment = json.loads(full.s.blob_get(payload["environment_blob"]))
        assert popen_env == environment["effective_environment"]
        assert payload["definition_ref"]["kind"] == "delivery_check"
