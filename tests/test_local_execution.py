"""Model-free local-execution mechanism regressions.

The adapter used here is an explicit protocol test double.  Its receipts prove
job routing, packet coverage and durable state transitions only; they are not
live LLM or independent acceptance evidence.
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.control import Control
from daikibo.db import SCHEMA_VERSION
from daikibo.knowledge_history import inspect_archive, validate_specifications
from daikibo.assurance_denominators import _find_current_candidate, _task_ref
from daikibo.unit4_enforcement import inspect_task_admission

from test_delivery_git_and_recovery import profile
from test_reviewed_breakdowns import review_all, setup
from test_subplan_drafts import draft, local, prepare_draft_admission, review_draft


def _pages(c, method, *args, limit=2):
    """Follow the complete public cursor for a bounded API read."""
    offset = 0
    result = []
    while True:
        page = method(c.owner, *args, offset=offset, limit=limit)
        result.append(page)
        if page["next_offset"] is None:
            return result
        offset = page["next_offset"]


def _local_review_script(tmp_path, disposition_ids):
    """Return a deterministic local-review protocol fixture.

    The disposition IDs are copied from the public inventory/proposal result
    by the test harness.  The fixture echoes packet markers and those IDs so
    mechanical coverage checks can run without pretending to make a semantic
    judgment.
    """
    disposition_file = tmp_path / "local_review_disposition_ids.json"
    disposition_file.write_text(json.dumps(sorted(disposition_ids)), encoding="utf-8")
    script = tmp_path / "local_review_fixture.py"
    script.write_text(
        """import json, sys
p = json.load(sys.stdin)
ctx = p.get('context', {})
covered = list(ctx.get('required_coverage', []))
with open(sys.argv[1], encoding='utf-8') as stream:
    disposition_ids = json.load(stream)
dispositions = [
    {'id': ident, 'resolution': 'acceptable',
     'reason': 'fixture mechanism evidence only; no semantic acceptance'}
    for ident in disposition_ids
]
print(json.dumps({'verdict': 'pass',
 'rationale': 'Deterministic protocol fixture; not an LLM judgment.',
 'covered': covered, 'findings': [],
 'observations': [{'ref': p.get('subject', 'packet'),
                   'detail': 'Fixture packet was observed.'}],
 'dispositions': dispositions}))
""",
        encoding="utf-8",
    )
    return script


def _managed(c, kind, args, *, expected="succeeded"):
    job = c.jobs.submit(c.owner, kind, args)
    row = c.s.one("SELECT * FROM jobs WHERE id=?", (job["id"],), True)
    outcome = c.jobs.run_one(row)
    state = c.jobs.get(c.owner, job["id"])
    assert state["status"] == expected, (kind, outcome, state)
    return outcome, state


def _stage_evidence(task, requirement, scenario, scenario_review, feasibility_receipt):
    requirements = requirement if isinstance(requirement, list) else [requirement]
    artifacts = [{"id": ident, "kind": "artifact"} for ident in requirements]
    result = {}
    for stage in ("requirements", "boundaries", "contracts", "design", "plan"):
        result[stage] = {"refs": artifacts, "explanation": "Fixture canonical input reference."}
    result["scenarios"] = {
        "refs": [scenario, scenario_review], "review": scenario_review, "role": "spec",
        "explanation": "Accepted scenario artifact with a separate managed artifact review.",
    }
    result["feasibility"] = {
        "refs": [feasibility_receipt],
        "explanation": "Completed analysis Task receipt records the feasibility observation.",
    }
    return {task: result}


def _direct_analysis_stage(case, analysis=None):
    """Build an intentionally unsupported typed Task stage reference."""
    ident = analysis or case["analysis"]
    stage = copy.deepcopy(case["stage"])
    stage[case["task"]]["feasibility"] = {
        "refs": [
            {"id": ident, "kind": "task"},
            case["feasibility_receipt"],
        ],
        "role": "feasibility",
        "explanation": "完了して現行の分析Taskと、その観測レビューを根拠にする。",
    }
    return stage


def _inventory(c, program, subplan, task):
    pages = _pages(c, c.local_executions.inventory, program, subplan, [task], limit=2)
    assert pages[0]["read_only"] is True
    assert pages[0]["certification"] is False
    items = [item for page in pages for item in page["items"]]
    assert len(items) == pages[0]["total"]
    assert len({item["id"] for item in items}) == len(items)
    assert all(isinstance(item["digest"], str) and len(item["digest"]) == 64 for item in items)
    assert all(page["material_digest"] == pages[0]["material_digest"] for page in pages)
    return items


def _dispositions(task, items, evidence):
    return [
        {
            "task": task,
            "item_id": item["id"],
            "item_digest": item["digest"],
            "disposition": "required_resolved",
            "reason": "Fixture records an explicit mechanical boundary for this item.",
            "evidence_refs": [evidence],
            "boundary": "Selected task consumes this frozen item through its canonical inputs.",
            "consumers": [task],
        }
        for item in items
    ]


def _make_local_case(setup, tmp_path, *, activate_root=True,
                     include_structural_output=False, mature_material=False,
                     required_output_ids=None, produced_output_ids=None):
    """Build a reviewed subplan/root proposal and an exact local proposal.

    ``activate_root`` is a fixture choice, not a data mutation: the
    unadopted variant exercises the legitimate local proposal/claim ownership
    path while retaining the root proposal in its original proposed state.
    """
    c, project, repo, requirement, program, domain, old_task, units = setup

    # The existing local-execution fixture intentionally exercises legacy
    # history.  The selected-local admission case needs a clean, source-backed
    # project so its stage context contains no unrelated retired inputs.
    if include_structural_output:
        project = c.k.create_project(c.owner, "Selected local canonical fixture")["id"]
        repo_path = tmp_path / "selected-local-repo"
        repo_path.mkdir()
        (repo_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        repo = c.sn.register(c.owner, project, "selected-local", str(repo_path))["id"]
        source_item = c.k.source(c.owner, project, "The selected local Task is source grounded.")
        requirement_item = c.k.propose(
            c.owner, project, "requirement",
            {"title": "Selected local requirement", "statement": "The local result is correct.",
             "acceptance": ["AC-ADD"], "source_refs": [source_item["id"]]},
        )
        c.k.accept(c.owner, requirement_item["id"], requirement_item["revision"])
        c.k.classify(
            c.owner, source_item["id"], 0, source_item["characters"],
            "requirement", [requirement_item["id"]], "Selected local source",
        )
        requirement = requirement_item["id"]
        program = c.p.begin(c.owner, project, source_item["id"], compact=True)["program"]
        domain_item = c.k.propose(
            c.owner, project, "domain",
            {"title": "Selected local domain", "statement": "The local Task owns arithmetic.",
             "responsibilities": ["arithmetic"], "non_responsibilities": [],
             "owned_data": [], "interfaces": [], "source_refs": [source_item["id"]]},
        )
        domain = c.k.accept(c.owner, domain_item["id"], domain_item["revision"])["id"]
        units = [
            {"id": "system", "title": "system", "parent": None, "domain": None,
             "rationale": "Aggregate of independently checked children", "obligations": [],
             "tasks": [], "interfaces": [], "dependencies": []},
            {"id": "arithmetic", "title": "arithmetic", "parent": "system", "domain": domain,
             "rationale": "Selected local arithmetic responsibility", "obligations": [],
             "tasks": [], "interfaces": [], "dependencies": []},
        ]
        old_task = None
        setup_for_case = (c, project, repo, requirement, program, domain, None, units)
        source = source_item["id"]
        partition = c.traceability.propose(
            c.owner, project, kind="document", scope={"source": source},
        )
        c.traceability.extract(c.owner, partition["id"])
    else:
        # The older shared fixture predates the root-program binding.  Retire
        # that task through its public transition and preserve its existing
        # test coverage for the ordinary local path.
        c.w.cancel(c.owner, old_task, "Replace the legacy fixture task with a program-bound task")
        setup_for_case = setup
        source = c.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (project,), True)["id"]
    # Keep four whole-system acceptance identities in the root requirements
    # phase.  The local Task implements only its selected slice; the other
    # obligations remain visible to root gates and are compared byte-for-byte
    # around the local lifecycle below.
    requirements = [requirement]
    acceptance = ["AC-ADD"]
    for index in range(1, 4):
        label = f"AC-BLOCKED-{index}"
        extra = c.k.propose(
            c.owner,
            project,
            "requirement",
            {"title": f"Retained whole-system acceptance {index}",
             "statement": f"The root keeps acceptance {label} pending.",
             "acceptance": [label], "source_refs": [source]},
        )
        c.k.accept(c.owner, extra["id"], 1)
        requirements.append(extra["id"])
        acceptance.append(label)
    if include_structural_output and not mature_material:
        # The stage context admits the source-backed domain as its bounded
        # scenario material for the isolated canonical local project.
        scenario = {"id": domain}
        scenario_review = c.rt.review(c.owner, domain, "spec", "fixture")["receipt"]
    else:
        scenario = c.k.propose(
            c.owner,
            project,
            "scenario",
            {"title": "Addition scenario", "statement": "An observed input pair produces its sum.",
             "source_refs": [source]},
        )
        c.k.accept(c.owner, scenario["id"], scenario["revision"])
        scenario_review = c.rt.review(c.owner, scenario["id"], "spec", "fixture")["receipt"]
    required_output_ids = list(required_output_ids or ["local-result"])
    produced_output_ids = list(
        produced_output_ids if produced_output_ids is not None else required_output_ids
    )
    output_body = {
        "title": "Local result",
        "statement": "The local fixture emitted its result.",
    }
    output_manifest = {
        "format": "daikibo.artifact-output.v1",
        "outputs": [{
            "declaration_id": output_id,
            "kind": "finding",
            "body": output_body,
        } for output_id in produced_output_ids],
    }
    task_body = {
            "title": "Fix arithmetic locally",
            "goal": "WRITE:" + json.dumps({
                "calc.py": "def add(a, b):\n    return a + b\n",
                # The positive local completion path collects this exact
                # declared output through task.artifacts_collect.  Keeping
                # the manifest in the implementer packet makes its producer
                # identity a sealed Runtime observation rather than a
                # candidate singleton inference.
                "artifact-output.json": json.dumps(output_manifest, sort_keys=True),
            }),
            "read_artifacts": [*requirements, domain] + ([] if include_structural_output else [scenario["id"]]),
            "write_paths": ["calc.py", "artifact-output.json"],
            "acceptance": acceptance,
            "dependencies": [],
            "repos": [repo],
            "non_goals": [],
            "workflow_id": program,
        }
    requirement_row = c.s.one(
        "SELECT * FROM artifacts WHERE id=? AND project=?", (requirements[0], project), True,
    )
    # A canonical produced_by relation needs a saved producer declaration for
    # every current Task.  Keeping the declaration on both the selected
    # production Task and the unselected feasibility Task makes the root plan
    # projection complete; the local projection can then select only the
    # production Task without changing the relation contract.
    task_body["structural_obligations"] = {
        "format": "daikibo.task-structural-obligations.v1",
        "required_outputs": [{
            "id": output_id,
            "statement": "The local Task emits its result.",
            "artifact_refs": [{
                "kind": "artifact", "project": project,
                "artifact": requirement_row["id"], "revision": requirement_row["revision"],
                "body_digest": requirement_row["digest"],
            }],
            "realization_kind": "artifact",
        } for output_id in required_output_ids],
        "required_exercises": [],
    }
    task = c.w.create(c.owner, project, task_body)["id"]
    c.w.plan_tests(
        c.owner,
        task,
        {"checks": [{"id": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                      "kind": "pytest", "required_tests": ["test_add"]}]},
    )
    analysis_body = {
        "title": "Check arithmetic feasibility",
        "goal": "WRITE:" + json.dumps({".daikibo-research/feasibility.txt": "fixture feasibility observation\n"}),
        "read_artifacts": [requirement, domain],
        "write_paths": [".daikibo-research/feasibility.txt"],
        "acceptance": ["AC-ADD"],
        "dependencies": [],
        "repos": [repo],
        "non_goals": [],
        "phase": "feasibility",
        "workflow_id": program,
        "structural_obligations": {
        "format": "daikibo.task-structural-obligations.v1",
        "required_outputs": [{
            "id": "feasibility-result", "statement": "The feasibility Task records its result.",
            "artifact_refs": [{
                "kind": "artifact", "project": project,
                "artifact": requirement_row["id"], "revision": requirement_row["revision"],
                "body_digest": requirement_row["digest"],
            }],
            "realization_kind": "artifact",
        }],
        "required_exercises": [],
        },
    }
    analysis = c.w.create(c.owner, project, analysis_body)["id"]
    c.w.plan_tests(
        c.owner,
        analysis,
        {"checks": [{"id": "feasibility-observed", "argv": ["python", "-c", "print('feasible')"],
                      "kind": "command", "purpose": "Record the bounded feasibility command."}]},
    )
    c.w.ready(c.owner, analysis)
    c.w.claim(c.owner, project, task=analysis)
    _managed(c, "execute", {"task": analysis, "adapter": "fixture"})
    _managed(c, "tests", {"task": analysis})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": analysis, "role": role, "adapter": "fixture"})
    c.w.complete(c.owner, analysis, c.w.task(c.owner, analysis)["revision"])
    feasibility_review, _ = _managed(
        c, "review", {"subject": analysis, "role": "feasibility", "adapter": "fixture"}
    )
    feasibility_receipt = feasibility_review["result"]["receipt"]
    if mature_material:
        interface = c.k.propose(
            c.owner, project, "interface",
            {"title": "Mature local interface",
             "statement": "The selected local Task exposes the arithmetic result.",
             "input": "two integers", "output": "one integer", "authentication": "none",
             "errors": "invalid values are rejected",
             "idempotency": "same inputs have the same result",
             "compatibility": "existing callers remain compatible", "consumers": [],
             "verification": "test_calc.py", "source_refs": [source]},
        )
        c.k.accept(c.owner, interface["id"], interface["revision"])
        finding = c.k.propose(
            c.owner, project, "finding",
            {"title": "Mature local feasibility",
             "statement": "The bounded arithmetic experiment completed.",
             "source_refs": [source]},
        )
        c.k.accept(c.owner, finding["id"], finding["revision"])
        design = c.k.propose(
            c.owner, project, "design",
            {"title": "Mature local design",
             "statement": "The selected local Task preserves the accepted result.",
             "source_refs": [source]},
        )
        c.k.accept(c.owner, design["id"], design["revision"])
        verification = c.k.propose(
            c.owner, project, "test",
            {"title": "Mature local verification",
             "statement": "The measured test checks the accepted result.",
             "source_refs": [source]},
        )
        c.k.accept(c.owner, verification["id"], verification["revision"])
        for requirement_id in requirements:
            c.k.link(
                c.owner, design["id"], requirement_id, "realizes", "asserted",
                "The mature local design preserves this requirement.",
            )
            c.k.link(
                c.owner, verification["id"], requirement_id, "verifies", "asserted",
                "The mature local test covers this requirement.",
            )
        c.idx.index(c.owner, repo)
        c.idx.search(c.owner, project, "add")
    units = copy.deepcopy(units)
    units[1]["tasks"] = [task, analysis]
    units[1]["obligations"] = [
        {"requirement": ident, "acceptance": ac}
        for ident, ac in zip(requirements, acceptance)
    ]
    units = local(units)

    partial = draft(
        setup_for_case,
        units=units,
        obligations=[{"requirement": ident, "acceptance": ac}
                     for ident, ac in zip(requirements, acceptance)],
        title="Local execution subplan",
    )
    prepare_draft_admission(
        c, partial["id"], requirements=requirements,
        task_ids=[task, analysis],
    )
    review_draft(c, partial["id"])
    root = c.subplans.compose(c.owner, partial["id"])
    review_all(c, root["breakdown"])
    if activate_root:
        c.breakdowns.activate(c.owner, root["breakdown"])

    stage = _stage_evidence(task, requirements, scenario["id"], scenario_review, feasibility_receipt)
    items = _inventory(c, program, partial["id"], task)
    dispositions = _dispositions(task, items, requirement)
    proposal = c.local_executions.propose(
        c.owner,
        program,
        partial["id"],
        [task],
        "Run this production Task after local前段 evidence and boundary review.",
        stage,
        dispositions,
        byte_budget=24000,
        request_id="local-main-1",
    )
    disposition_ids = [f"disposition:{task}:{item['id']}" for item in items]
    adapter_script = _local_review_script(tmp_path, disposition_ids)
    disposition_file = adapter_script.with_name("local_review_disposition_ids.json")
    c.rt.adapters.register(
        c.owner,
        "local-review-fixture",
        "fixture",
        sys.executable,
        [str(adapter_script), str(disposition_file)],
    )
    return {
        "c": c,
        "project": project,
        "repo": repo,
        "requirement": requirement,
        "requirements": requirements,
        "acceptance": acceptance,
        "program": program,
        "domain": domain,
        "task": task,
        "partial": partial["id"],
        "root": root["breakdown"],
        "scenario": scenario["id"],
        "scenario_review": scenario_review,
        "analysis": analysis,
        "feasibility_receipt": feasibility_receipt,
        "items": items,
        "stage": stage,
        "dispositions": dispositions,
        "proposal": proposal,
    }


@pytest.fixture
def local_case(setup, tmp_path):
    return _make_local_case(setup, tmp_path, activate_root=True)


@pytest.fixture
def local_unadopted_case(setup, tmp_path):
    return _make_local_case(
        setup, tmp_path, activate_root=False, include_structural_output=True,
    )


def _review_local_packets(case):
    c = case["c"]
    proposal = case["proposal"]["id"]
    packets = []
    for page in _pages(c, c.local_executions.get, proposal, limit=1):
        packets.extend(page["packets"])
    assert packets
    for packet in packets:
        for role in ("feasibility", "impact"):
            args = {"subject": packet["id"], "role": role, "adapter": "local-review-fixture"}
            assert c.jobs.subject_project("review", args) == case["project"]
            outcome, state = _managed(
                c,
                "review",
                args,
            )
            assert outcome["result"]["result"]["verdict"] == "pass"
            assert state["attempt_count"] == 1
    return packets


def _review_proposal(c, project, proposal, adapter):
    """Observe every packet/role for a proposal through managed review Jobs."""
    packets = []
    for page in _pages(c, c.local_executions.get, proposal, limit=1):
        packets.extend(page["packets"])
    assert packets
    for packet in packets:
        for role in ("feasibility", "impact"):
            args = {"subject": packet["id"], "role": role, "adapter": adapter}
            assert c.jobs.subject_project("review", args) == project
            outcome, state = _managed(c, "review", args)
            assert outcome["result"]["result"]["verdict"] == "pass"
            assert state["attempt_count"] == 1
    return packets


def _claim_local(case):
    """Run the public reviewed authorization path and read its current binding."""
    c = case["c"]
    _review_local_packets(case)
    certified = c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    c.w.ready(c.owner, case["task"])
    running = c.w.claim(c.owner, case["project"], task=case["task"])
    # Workflow.claim owns both the Task epoch transition and the local
    # responsibility record.  Reading the record here keeps later lifecycle
    # coverage from repairing a missing writer binding after the fact.
    claim = c.local_executions.claimed(case["task"], running["epoch"])
    assert claim is not None
    return certified, running, claim


def _adopt_current_produced_by(c, case, task):
    """Adopt the current produced_by set for one completed Task.

    Unit4-R consumes the exact relation population selected by the active
    profile.  The local fixture owns a real selected profile, so completion
    must carry the same public edge/set evidence as the root route.  The
    marker adapter is a protocol fixture; it only records packet coverage.
    """
    project = case["project"]
    selection = c.assurance.selected_profile(c.owner, project, case["program"])
    profile_ref = selection["profile_ref"]
    task_row = c.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (task, project), True)
    task_ref = _task_ref(project, task_row)
    candidate = _find_current_candidate(c, c.owner, project, task_row, [])
    assert candidate is not None
    candidate_ref = candidate["ref"]
    scope = c.assurance._scope_from_ref(project, profile_ref)
    obligations_row = c.s.one(
        "SELECT * FROM assurance_objects WHERE project=? AND kind='obligations' "
        "AND logical_id=? ORDER BY revision DESC LIMIT 1",
        (project, "obligations:" + scope["id"]), True,
    )
    obligations = c.assurance._decode_object(obligations_row)["body"]
    obligation_ids = [
        item["id"] for item in obligations.get("obligations", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    assert obligation_ids

    edge = c.assurance.edge_propose(
        c.owner, project,
        {
            "source_ref": candidate_ref, "target_ref": task_ref,
            "relation": "produced_by", "scope_ref": profile_ref,
            "claim": "The accepted requirement is produced by this current Task.",
            "obligation_ids": obligation_ids, "required_evidence_refs": [],
            "authority_refs": [],
        },
    )
    _adopt_assurance_root(c, project, edge["edge"])
    relation_set = c.assurance.set_propose(
        c.owner, project,
        {
            "center_ref": task_ref, "relation": "produced_by", "direction": "incoming",
            "scope_ref": profile_ref, "criteria": {}, "required_evidence_refs": [],
        },
    )
    _adopt_assurance_root(c, project, relation_set["set"])
    return {"edge": edge, "set": relation_set, "task_ref": task_ref,
            "profile_ref": profile_ref, "obligation_ids": obligation_ids}


def _adopt_current_artifact_produced_by(c, case, task):
    """Collect and adopt the exact declared artifact output for a Task.

    Candidate-only produced_by evidence remains available above for negative
    contract probes.  Completion fixtures use this path so an artifact
    declaration is satisfied only by the public collector's immutable
    artifact_production material and its declaration ID.
    """
    project = case["project"]
    selection = c.assurance.selected_profile(c.owner, project, case["program"])
    profile_ref = selection["profile_ref"]
    task_row = c.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (task, project), True)
    task_ref = _task_ref(project, task_row)
    candidate = _find_current_candidate(c, c.owner, project, task_row, [])
    assert candidate is not None
    candidate_ref = candidate["ref"]
    collected = c.w.artifacts_collect(
        c.owner, task, task_row["revision"], candidate_ref["candidate"],
        case["repo"], "artifact-output.json",
    )
    assert len(collected["artifacts"]) == 1
    artifact = collected["artifacts"][0]["artifact"]
    artifact_ref = {
        "kind": "artifact", "project": project,
        "artifact": artifact["id"], "revision": artifact["revision"],
        "body_digest": artifact["digest"],
    }
    scope = c.assurance._scope_from_ref(project, profile_ref)
    obligations_row = c.s.one(
        "SELECT * FROM assurance_objects WHERE project=? AND kind='obligations' "
        "AND logical_id=? ORDER BY revision DESC LIMIT 1",
        (project, "obligations:" + scope["id"]), True,
    )
    obligations = c.assurance._decode_object(obligations_row)["body"]
    obligation_ids = [
        item["id"] for item in obligations.get("obligations", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    assert obligation_ids

    edge = c.assurance.edge_propose(
        c.owner, project,
        {
            "source_ref": artifact_ref, "target_ref": task_ref,
            "relation": "produced_by", "scope_ref": profile_ref,
            "claim": "The immutable collected artifact is produced by this current Task.",
            "obligation_ids": obligation_ids, "required_evidence_refs": [],
            "authority_refs": [],
        },
    )
    _adopt_assurance_root(c, project, edge["edge"])
    relation_set = c.assurance.set_propose(
        c.owner, project,
        {
            "center_ref": task_ref, "relation": "produced_by", "direction": "incoming",
            "scope_ref": profile_ref, "criteria": {}, "required_evidence_refs": [],
        },
    )
    _adopt_assurance_root(c, project, relation_set["set"])
    return {"edge": edge, "set": relation_set, "task_ref": task_ref,
            "profile_ref": profile_ref, "artifact_ref": artifact_ref,
            "obligation_ids": obligation_ids}


def _adopt_assurance_root(c, project, root):
    refs = []
    for packet, role in c.assurance._review_requirements(
            project, c.assurance._adoption_roots(project, root)):
        receipt = c.rt.review(c.owner, packet["id"], role, "markers")
        refs.append({"packet": packet["id"], "role": role, "id": receipt["receipt"]})
    c.assurance.adopt(c.owner, project, root["id"], root["digest"], None, refs)


def _prepare_current_root_for_execution(case):
    """Build the source-backed root and phase prefix used by root takeover tests."""
    c = case["c"]
    source = c.s.one(
        "SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1",
        (case["project"],), True,
    )["id"]
    c.idx.index(c.owner, case["repo"])
    c.idx.search(c.owner, case["project"], "add")
    interface = c.k.propose(
        c.owner,
        case["project"],
        "interface",
        {
            "title": "Arithmetic boundary",
            "statement": "The arithmetic operation accepts two values and returns their sum.",
            "input": "two integers", "output": "one integer", "authentication": "none",
            "errors": "invalid values are rejected", "idempotency": "same inputs have the same result",
            "compatibility": "existing callers remain compatible", "consumers": [],
            "verification": "test_calc.py", "source_refs": [source],
        },
    )
    c.k.accept(c.owner, interface["id"], interface["revision"])
    finding = c.k.propose(
        c.owner, case["project"], "finding",
        {"title": "Feasibility observation", "statement": "The bounded arithmetic experiment completed.",
         "source_refs": [source]},
    )
    c.k.accept(c.owner, finding["id"], finding["revision"])
    design = c.k.propose(
        c.owner, case["project"], "design",
        {"title": "Arithmetic design", "statement": "Use the canonical addition function for every retained acceptance.",
         "source_refs": [source]},
    )
    c.k.accept(c.owner, design["id"], design["revision"])
    verification = c.k.propose(
        c.owner, case["project"], "test",
        {"title": "Arithmetic verification", "statement": "The measured unit test checks the addition acceptance.",
         "source_refs": [source]},
    )
    c.k.accept(c.owner, verification["id"], verification["revision"])
    for requirement in case["requirements"]:
        c.k.link(c.owner, design["id"], requirement, "realizes", "asserted",
                "Root design preserves this requirement.")
        c.k.link(c.owner, verification["id"], requirement, "verifies", "asserted",
                "Root test covers this requirement.")

    base_setup = (c, case["project"], case["repo"], case["requirement"], case["program"],
                  case["domain"], case["task"], [])
    obligations = [
        {"requirement": ident, "acceptance": ac}
        for ident, ac in zip(case["requirements"], case["acceptance"])
    ]
    root_units = local([
        {"id": "current-system", "title": "current-system", "parent": None, "domain": None,
         "rationale": "Aggregate the complete root scope.", "obligations": [], "tasks": [],
         "interfaces": [], "dependencies": []},
        {"id": "current-arithmetic", "title": "current-arithmetic", "parent": "current-system",
         "domain": case["domain"], "rationale": "Retain the selected production and analysis Tasks.",
         "obligations": obligations, "tasks": [case["task"], case["analysis"]],
         "interfaces": [], "dependencies": []},
    ])
    current_subplan = draft(
        base_setup, units=root_units, obligations=obligations,
        title="Current root plan after observed design",
    )
    prepare_draft_admission(
        c, current_subplan["id"], requirements=case["requirements"],
        task_ids=[case["task"], case["analysis"]],
    )
    review_draft(c, current_subplan["id"])
    current_root = c.subplans.compose(
        c.owner, current_subplan["id"], expected_active=case["root"],
    )
    review_all(c, current_root["breakdown"])
    c.breakdowns.activate(c.owner, current_root["breakdown"], expected_active=case["root"])

    delivery_profile = profile(case["project"], case["repo"], case["requirement"], case["task"])
    delivery_profile["required_requirements"] = case["requirements"]
    delivery_profile["required_tasks"] = [case["task"], case["analysis"]]
    delivery_profile["program"] = case["program"]
    c.d.configure(c.owner, case["project"], delivery_profile)
    while c.p.next(c.owner, case["program"])["phase"] != "implementation":
        current = c.p.next(c.owner, case["program"])
        receipt = c.rt.review(c.owner, case["program"], "phase", "fixture")["receipt"]
        c.p.advance(c.owner, case["program"], current["revision"], receipt)


def _prepare_multi_task_local_proposal(case, tmp_path, *, review_b, stale_b=False):
    """Create a public A->B local proposal with independently selectable B proof."""
    c = case["c"]
    a = case["task"]
    a_body = c.w.task(c.owner, a)["body"]
    b = c.w.create(
        c.owner,
        case["project"],
            {
                "title": "Follow-up arithmetic output",
                "goal": "WRITE:" + json.dumps({
                    "second.py": "def twice(value):\n    return value * 2\n",
                    "artifact-output.json": json.dumps({
                        "format": "daikibo.artifact-output.v1",
                        "outputs": [{
                            "declaration_id": "local-result", "kind": "finding",
                            "body": {"title": "Local result", "statement": "The local fixture emitted its result."},
                        }],
                    }, sort_keys=True),
                }),
                "read_artifacts": list(a_body["read_artifacts"]),
                "write_paths": ["second.py", "artifact-output.json"],
            "acceptance": list(a_body["acceptance"]),
            "dependencies": [a],
            "repos": list(a_body["repos"]),
            "non_goals": list(a_body["non_goals"]),
            "structural_obligations": copy.deepcopy(a_body["structural_obligations"]),
            "workflow_id": case["program"],
        },
    )["id"]
    c.w.plan_tests(
        c.owner,
        b,
        {"checks": [{"id": "unit-b", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                      "kind": "pytest", "required_tests": ["test_add"]}]},
    )
    b_plan = c.s.one("SELECT body FROM plans WHERE task=?", (b,), True)
    if review_b:
        c.rt.review(c.owner, b, "test_plan", "markers", proposal=json.loads(b_plan["body"]))
    if stale_b:
        # Replan clears the observed plan and increments the immutable Task
        # revision.  Reinstall the same test definition without a new review
        # so the retained receipt is a real historical, stale observation.
        b_definition = c.w.task(c.owner, b)
        c.w.replan(c.owner, b, b_definition["revision"], "Invalidate B after its observed plan review")
        current_plan = json.loads(b_plan["body"])
        current_plan["checks"][0]["id"] = "unit-b-current"
        c.w.plan_tests(c.owner, b, current_plan)

    base_setup = (c, case["project"], case["repo"], case["requirement"], case["program"],
                  case["domain"], a, [])
    obligations = [
        {"requirement": ident, "acceptance": ac}
        for ident, ac in zip(case["requirements"], case["acceptance"])
    ]
    chain_units = local([
        {"id": "chain-system", "title": "chain-system", "parent": None, "domain": None,
         "rationale": "Aggregate the selected dependency chain.", "obligations": [], "tasks": [],
         "interfaces": [], "dependencies": []},
        {"id": "chain-arithmetic", "title": "chain-arithmetic", "parent": "chain-system",
         "domain": case["domain"], "rationale": "Keep both production Tasks in one bounded domain.",
         "obligations": obligations, "tasks": [a, case["analysis"], b], "interfaces": [],
         "dependencies": []},
    ])
    partial = draft(base_setup, units=chain_units, obligations=obligations, title="A to B local chain")
    # Packet review is independent from Task-plan observations.  Explicitly
    # prepare only A; B's deliberately missing or historical plan review must
    # remain untouched for the negative cases.
    prepare_draft_admission(
        c, partial["id"], requirements=case["requirements"], task_ids=[a],
    )
    review_draft(c, partial["id"])
    c.subplans.compose(c.owner, partial["id"], expected_active=case["root"])

    stage = {
        a: copy.deepcopy(case["stage"][a]),
        b: copy.deepcopy(case["stage"][a]),
    }
    pages = _pages(c, c.local_executions.inventory, case["program"], partial["id"], [a, b], limit=2)
    items = [item for page in pages for item in page["items"]]
    assert len(items) == pages[0]["total"]
    assert all(page["material_digest"] == pages[0]["material_digest"] for page in pages)
    dispositions = _dispositions(a, items, case["requirement"]) + _dispositions(b, items, case["requirement"])
    chain_dir = tmp_path / ("chain-review-stale" if stale_b else
                             "chain-review-reviewed" if review_b else "chain-review-missing")
    chain_dir.mkdir()
    script = _local_review_script(
        chain_dir,
        [f"disposition:{task}:{item['id']}" for task in (a, b) for item in items],
    )
    disposition_file = script.with_name("local_review_disposition_ids.json")
    c.rt.adapters.register(
        c.owner,
        "local-chain-review-fixture",
        "fixture",
        sys.executable,
        [str(script), str(disposition_file)],
    )
    proposal = c.local_executions.propose(
        c.owner,
        case["program"],
        partial["id"],
        [a, b],
        "Execute the selected production dependency chain in order.",
        stage,
        dispositions,
        byte_budget=100000,
        request_id="local-chain-reviewed-1" if review_b else "local-chain-missing-1",
    )
    return {"case": case, "a": a, "b": b, "proposal": proposal,
            "adapter": "local-chain-review-fixture"}


def _task_gate_failures(exc):
    """Flatten public certification details to the Unit4 task-gate failures."""
    assert exc.value.code == "local_execution_gate_denied"
    details = exc.value.details
    assert isinstance(details, list)
    plan_failure = next(item for item in details if item.get("code") == "stage_assurance_blocked")
    gate = plan_failure["plan_gate"]
    evaluation = gate["evaluation"]
    assert isinstance(evaluation, dict)
    return evaluation["failures"], evaluation


def test_multiple_selected_task_missing_review_is_structured_denial_and_attributed(
    local_case, tmp_path,
):
    """A missing B plan review denies the public proposal and names B only."""
    prepared = _prepare_multi_task_local_proposal(local_case, tmp_path, review_b=False)
    c = local_case["c"]
    _review_proposal(c, local_case["project"], prepared["proposal"]["id"], prepared["adapter"])
    with pytest.raises(Fault) as rejected:
        c.local_executions.certify(
            c.owner, prepared["proposal"]["id"], prepared["proposal"]["digest"],
        )
    failures, evaluation = _task_gate_failures(rejected)
    assert evaluation["assurance_allow"] is False
    assert any(failure.get("task") == prepared["b"] for failure in failures)
    assert all(failure.get("task") != prepared["a"] for failure in failures)


def test_multiple_selected_task_stale_review_is_structured_denial_and_attributed(
    local_case, tmp_path,
):
    """A changed B definition keeps A's result separate from B's stale result."""
    prepared = _prepare_multi_task_local_proposal(
        local_case, tmp_path, review_b=True, stale_b=True,
    )
    c = local_case["c"]
    _review_proposal(c, local_case["project"], prepared["proposal"]["id"], prepared["adapter"])
    with pytest.raises(Fault) as rejected:
        c.local_executions.certify(
            c.owner, prepared["proposal"]["id"], prepared["proposal"]["digest"],
        )
    failures, evaluation = _task_gate_failures(rejected)
    assert evaluation["assurance_allow"] is False
    assert any(failure.get("task") == prepared["b"] for failure in failures)
    assert all(failure.get("task") != prepared["a"] for failure in failures)


def test_public_proposal_accepts_a_completed_current_analysis_review_receipt(local_case):
    """Feasibility accepts the managed review receipt for a current analysis Task."""
    case = local_case
    c = case["c"]
    receipt = c.g.receipt(case["feasibility_receipt"])
    assert receipt["subject"] == case["analysis"]
    assert receipt["role"] == "feasibility"
    assert receipt["exit_code"] == 0
    assert receipt["result"]["verdict"] == "pass"
    assert receipt["judgment_valid"] is True
    assert receipt["readonly_verified"] is True
    proposal = c.local_executions.propose(
        c.owner,
        case["program"],
        case["partial"],
        [case["task"]],
        "Run this production Task after local前段 evidence and boundary review.",
        copy.deepcopy(case["stage"]),
        case["dispositions"],
        byte_budget=24000,
        request_id="local-analysis-review-receipt-1",
    )
    assert proposal["id"] == case["proposal"]["id"]
    assert proposal["material_digest"] == case["proposal"]["material_digest"]


def test_stage_evidence_rejects_an_unsupported_direct_task_reference(local_case):
    """The public stage contract accepts canonical artifacts/receipts, not Task IDs."""
    case = local_case
    c = case["c"]
    with pytest.raises(Fault) as exc:
        c.local_executions.propose(
            c.owner,
            case["program"],
            case["partial"],
            [case["task"]],
            "Reject the unsupported direct analysis Task reference.",
            _direct_analysis_stage(case),
            case["dispositions"],
            byte_budget=24000,
            request_id="local-direct-analysis-unsupported-1",
        )
    assert exc.value.code in {"invalid_evidence", "missing_evidence"}
    assert c.s.one("SELECT count(*) AS n FROM local_execution_proposals")["n"] == 1


@pytest.mark.parametrize("state", ["not_completed", "stale"])
def test_analysis_review_receipt_rejects_a_noncurrent_analysis_subject(local_case, state):
    """A passing review receipt cannot hide a planned or stale analysis Task."""
    case = local_case
    c = case["c"]
    if state == "not_completed":
        analysis = c.w.create(
            c.owner,
            case["project"],
            {
                "title": "Pending feasibility analysis",
                "goal": "WRITE:" + json.dumps({".daikibo-research/pending.txt": "not completed\n"}),
                "read_artifacts": [case["requirement"], case["domain"]],
                "write_paths": [".daikibo-research/pending.txt"],
                "acceptance": ["AC-ADD"],
                "dependencies": [],
                "repos": [case["repo"]],
                "non_goals": [],
                "phase": "feasibility",
                "workflow_id": case["program"],
            },
        )["id"]
        c.w.plan_tests(
            c.owner,
            analysis,
            {"checks": [{"id": "pending", "argv": ["python", "-c", "print('pending')"],
                          "kind": "command", "purpose": "Pending analysis fixture."}]},
        )
    else:
        analysis = case["analysis"]
        c.p.change(
            c.owner,
            case["project"],
            {
                "title": "Invalidate feasibility input",
                "origin": "design",
                "reason": "The current domain input needs reassessment before feasibility is reused.",
                "affected": [case["domain"]],
                "evidence": [c.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1",
                                      (case["project"],), True)["id"]],
            },
        )
    observed, _ = _managed(c, "review", {"subject": analysis, "role": "feasibility", "adapter": "fixture"})
    receipt_id = observed["result"]["receipt"]
    receipt = c.g.receipt(receipt_id)
    assert receipt["subject"] == analysis
    assert receipt["role"] == "feasibility"
    stage = copy.deepcopy(case["stage"])
    stage[case["task"]]["feasibility"] = {
        "refs": [receipt_id],
        "role": "feasibility",
        "explanation": "完了済み分析の管理レビューとして扱うが、対象状態を再確認する。",
    }
    with pytest.raises(Fault) as exc:
        c.local_executions.propose(
            c.owner,
            case["program"],
            case["partial"],
            [case["task"]],
            f"Reject {state} analysis review evidence.",
            stage,
            case["dispositions"],
            byte_budget=24000,
            request_id=f"local-analysis-review-{state}-1",
        )
    assert exc.value.code == "invalid_evidence"
    assert c.s.one("SELECT count(*) AS n FROM local_execution_proposals")["n"] == 1


def test_newer_neutral_draft_preserves_the_certified_authorization(local_case):
    """An unreviewed later material variant remains neutral until it is negative."""
    case = local_case
    c = case["c"]
    _review_local_packets(case)
    certified = c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    newer_stage = copy.deepcopy(case["stage"])
    newer_stage[case["task"]]["boundaries"]["explanation"] = "境界条件と接続点を確認した証拠を保持する。"
    newer = c.local_executions.propose(
        c.owner,
        case["program"],
        case["partial"],
        [case["task"]],
        "Keep the later draft neutral pending its own observed reviews.",
        newer_stage,
        case["dispositions"],
        byte_budget=24000,
        request_id="local-neutral-draft-1",
    )
    assert newer["id"] != case["proposal"]["id"]
    assert c.s.one("SELECT count(*) AS n FROM local_execution_records WHERE proposal=?",
                   (newer["id"],))["n"] == 0
    readiness = c.local_executions.execution_readiness(c.owner, case["task"], "execute")
    assert readiness["allowed"] is True
    assert readiness["proposal"] == case["proposal"]["id"]
    assert readiness["certification"]["id"] == certified["id"]


def test_one_certified_proposal_allows_selected_tasks_in_dependency_order(local_case, tmp_path):
    """A selected A→B chain shares one certification and gates B until A completes."""
    case = local_case
    c = case["c"]
    a = case["task"]
    a_body = c.w.task(c.owner, a)["body"]
    b = c.w.create(
        c.owner,
        case["project"],
        {
            "title": "Follow-up arithmetic output",
            "goal": "WRITE:" + json.dumps({
                "second.py": "def twice(value):\n    return value * 2\n",
                "artifact-output.json": json.dumps({
                    "format": "daikibo.artifact-output.v1",
                    "outputs": [{
                        "declaration_id": "local-result", "kind": "finding",
                        "body": {"title": "Local result", "statement": "The local fixture emitted its result."},
                    }],
                }, sort_keys=True),
            }),
            "read_artifacts": list(a_body["read_artifacts"]),
            "write_paths": ["second.py", "artifact-output.json"],
            "acceptance": list(a_body["acceptance"]),
            "dependencies": [a],
                "repos": list(a_body["repos"]),
                "non_goals": list(a_body["non_goals"]),
                "structural_obligations": copy.deepcopy(
                    a_body["structural_obligations"]
                ),
                "workflow_id": case["program"],
            },
        )["id"]
    c.w.plan_tests(
        c.owner,
        b,
        {"checks": [{"id": "unit-b", "argv": ["python", "-m", "pytest", "-q", "test_calc.py"],
                      "kind": "pytest", "required_tests": ["test_add"]}]},
    )
    b_plan = c.s.one("SELECT body FROM plans WHERE task=?", (b,), True)
    c.rt.review(c.owner, b, "test_plan", "markers", proposal=json.loads(b_plan["body"]))

    base_setup = (c, case["project"], case["repo"], case["requirement"], case["program"],
                  case["domain"], a, [])
    obligations = [
        {"requirement": ident, "acceptance": ac}
        for ident, ac in zip(case["requirements"], case["acceptance"])
    ]
    chain_units = local([
        {"id": "chain-system", "title": "chain-system", "parent": None, "domain": None,
         "rationale": "Aggregate the selected dependency chain.", "obligations": [], "tasks": [],
         "interfaces": [], "dependencies": []},
        {"id": "chain-arithmetic", "title": "chain-arithmetic", "parent": "chain-system",
         "domain": case["domain"], "rationale": "Keep both production Tasks in one bounded domain.",
         "obligations": obligations, "tasks": [a, case["analysis"], b], "interfaces": [],
         "dependencies": []},
    ])
    partial = draft(base_setup, units=chain_units, obligations=obligations, title="A to B local chain")
    prepare_draft_admission(
        c, partial["id"], requirements=case["requirements"],
        task_ids=[a, case["analysis"], b],
    )
    review_draft(c, partial["id"])
    # Local certification consumes the same composed root selector as the
    # public root route.  Compose this reviewed chain before proposing its
    # local execution so the Unit4-P gate can bind the selector to retained
    # breakdown material.
    c.subplans.compose(c.owner, partial["id"], expected_active=case["root"])

    stage = {
        a: copy.deepcopy(case["stage"][a]),
        b: copy.deepcopy(case["stage"][a]),
    }
    pages = _pages(c, c.local_executions.inventory, case["program"], partial["id"], [a, b], limit=2)
    items = [item for page in pages for item in page["items"]]
    assert len(items) == pages[0]["total"]
    assert all(page["material_digest"] == pages[0]["material_digest"] for page in pages)
    dispositions = _dispositions(a, items, case["requirement"]) + _dispositions(b, items, case["requirement"])
    chain_dir = tmp_path / "chain-review"
    chain_dir.mkdir()
    script = _local_review_script(
        chain_dir,
        [f"disposition:{task}:{item['id']}" for task in (a, b) for item in items],
    )
    disposition_file = script.with_name("local_review_disposition_ids.json")
    c.rt.adapters.register(
        c.owner,
        "local-chain-review-fixture",
        "fixture",
        sys.executable,
        [str(script), str(disposition_file)],
    )
    proposal = c.local_executions.propose(
        c.owner,
        case["program"],
        partial["id"],
        [a, b],
        "Execute the selected production dependency chain in order.",
        stage,
        dispositions,
        byte_budget=100000,
        request_id="local-chain-1",
    )
    packets = _review_proposal(c, case["project"], proposal["id"], "local-chain-review-fixture")
    certified = c.local_executions.certify(c.owner, proposal["id"], proposal["digest"])
    assert certified["tasks"] == sorted([a, b])
    assert len(packets) == proposal["packet_count"]

    blocked = c.local_executions.execution_readiness(c.owner, b, "ready")
    assert blocked["allowed"] is False
    assert any("dependency" in str(failure) for failure in blocked["failures"])
    with pytest.raises(Fault):
        c.w.ready(c.owner, b)

    c.w.ready(c.owner, a)
    running_a = c.w.claim(c.owner, case["project"], task=a)
    claim_a = c.local_executions.claimed(a, running_a["epoch"])
    assert parse_json(claim_a["body"])["certified_event"]["id"] == certified["id"]
    assert c.local_executions.claimed(a, running_a["epoch"])
    _managed(c, "execute", {"task": a, "adapter": "fixture"})
    _managed(c, "tests", {"task": a})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": a, "role": role, "adapter": "fixture"})
    _adopt_current_artifact_produced_by(c, case, a)
    c.w.complete(c.owner, a, c.w.task(c.owner, a)["revision"])

    c.w.ready(c.owner, b)
    running_b = c.w.claim(c.owner, case["project"], task=b)
    claim_b = c.local_executions.claimed(b, running_b["epoch"])
    assert parse_json(claim_b["body"])["certified_event"]["id"] == certified["id"]
    assert c.local_executions.claimed(b, running_b["epoch"])
    allowed = c.local_executions.execution_readiness(c.owner, b, "execute")
    assert allowed["allowed"] is True
    _managed(c, "execute", {"task": b, "adapter": "fixture"})
    _managed(c, "tests", {"task": b})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": b, "role": role, "adapter": "fixture"})
    _adopt_current_artifact_produced_by(c, case, b)
    c.w.complete(c.owner, b, c.w.task(c.owner, b)["revision"])

    assert c.w.task(c.owner, a)["status"] == "completed"
    assert c.w.task(c.owner, b)["status"] == "completed"
    assert c.s.one(
        "SELECT count(*) AS n FROM local_execution_records WHERE proposal=? AND kind='claimed'",
        (proposal["id"],),
    )["n"] == 2


def test_root_route_takes_over_at_integration_while_delivery_remains_gated(local_case):
    """Root adoption keeps the full phase prefix and still refuses unverified delivery."""
    case = local_case
    c = case["c"]
    task = case["task"]
    _claim_local(case)
    _managed(c, "execute", {"task": task, "adapter": "fixture"})
    _managed(c, "tests", {"task": task})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": task, "role": role, "adapter": "fixture"})
    _adopt_current_artifact_produced_by(c, case, task)
    c.w.complete(c.owner, task, c.w.task(c.owner, task)["revision"])

    source = c.s.one("SELECT id FROM sources WHERE project=? ORDER BY id LIMIT 1", (case["project"],), True)["id"]
    c.idx.index(c.owner, case["repo"])
    c.idx.search(c.owner, case["project"], "add")
    interface = c.k.propose(
        c.owner,
            case["project"],
            "interface",
        {
            "title": "Arithmetic boundary",
            "statement": "The arithmetic operation accepts two values and returns their sum.",
            "input": "two integers",
            "output": "one integer",
            "authentication": "none",
            "errors": "invalid values are rejected",
            "idempotency": "same inputs have the same result",
                "compatibility": "existing callers remain compatible",
                "consumers": [],
                "verification": "test_calc.py",
                "source_refs": [source],
            },
        )
    c.k.accept(c.owner, interface["id"], interface["revision"])
    finding = c.k.propose(
        c.owner,
        case["project"],
            "finding",
            {"title": "Feasibility observation", "statement": "The bounded arithmetic experiment completed.",
             "source_refs": [source]},
    )
    c.k.accept(c.owner, finding["id"], finding["revision"])
    design = c.k.propose(
        c.owner,
        case["project"],
            "design",
            {"title": "Arithmetic design", "statement": "Use the canonical addition function for every retained acceptance.",
             "source_refs": [source]},
    )
    c.k.accept(c.owner, design["id"], design["revision"])
    verification = c.k.propose(
        c.owner,
        case["project"],
            "test",
            {"title": "Arithmetic verification", "statement": "The measured unit test checks the addition acceptance.",
             "source_refs": [source]},
    )
    c.k.accept(c.owner, verification["id"], verification["revision"])
    for requirement in case["requirements"]:
        c.k.link(c.owner, design["id"], requirement, "realizes", "asserted", "Root design preserves this requirement.")
        c.k.link(c.owner, verification["id"], requirement, "verifies", "asserted", "Root test covers this requirement.")

    # The original local fixture's root was reviewed before these whole-root
    # artifacts existed.  Adopt a fresh public root proposal so later phase
    # gates observe the current root material rather than a stale child review.
    base_setup = (c, case["project"], case["repo"], case["requirement"], case["program"],
                  case["domain"], task, [])
    root_obligations = [
        {"requirement": ident, "acceptance": ac}
        for ident, ac in zip(case["requirements"], case["acceptance"])
    ]
    root_units = local([
        {"id": "current-system", "title": "current-system", "parent": None, "domain": None,
         "rationale": "Aggregate the complete root scope.", "obligations": [], "tasks": [],
         "interfaces": [], "dependencies": []},
        {"id": "current-arithmetic", "title": "current-arithmetic", "parent": "current-system",
         "domain": case["domain"], "rationale": "Retain the selected production and analysis Tasks.",
         "obligations": root_obligations, "tasks": [task, case["analysis"]], "interfaces": [],
         "dependencies": []},
    ])
    current_subplan = draft(base_setup, units=root_units, obligations=root_obligations,
                            title="Current root plan after observed design")
    prepare_draft_admission(
        c, current_subplan["id"], requirements=case["requirements"],
        task_ids=[task, case["analysis"]],
    )
    review_draft(c, current_subplan["id"])
    current_root = c.subplans.compose(c.owner, current_subplan["id"], expected_active=case["root"])
    review_all(c, current_root["breakdown"])
    c.breakdowns.activate(c.owner, current_root["breakdown"], expected_active=case["root"])

    delivery_profile = profile(case["project"], case["repo"], case["requirement"], task)
    delivery_profile["required_requirements"] = case["requirements"]
    delivery_profile["required_tasks"] = [task, case["analysis"]]
    delivery_profile["program"] = case["program"]
    c.d.configure(c.owner, case["project"], delivery_profile)

    def advance_phase():
        current = c.p.next(c.owner, case["program"])
        receipt = c.rt.review(c.owner, case["program"], "phase", "fixture")["receipt"]
        return c.p.advance(c.owner, case["program"], current["revision"], receipt)

    while c.p.next(c.owner, case["program"])["phase"] != "implementation":
        advance_phase()
    implementation_history = [
        item["phase"]
        for item in parse_json(c.s.one("SELECT body FROM programs WHERE id=?", (case["program"],), True)["body"])["history"]
    ]
    assert implementation_history == ["requirements", "scenarios", "boundaries", "contracts", "feasibility", "design", "plan"]
    implementation = c.g.execution_readiness(c.owner, task, "recheck")
    assert implementation["allowed"] is True
    assert implementation["route"] == "root"
    assert implementation["root_adopted"] is True

    # Advancing through the ordinary implementation phase extends the exact
    # root prefix; it does not resurrect the local certificate as a permanent
    # delivery authority.
    advance_phase()
    assert c.p.next(c.owner, case["program"])["phase"] == "integration"
    integration = c.g.execution_readiness(c.owner, task, "recheck")
    assert integration["allowed"] is True
    assert integration["route"] == "root"
    assert integration["root_adopted"] is True
    history = [
        item["phase"]
        for item in parse_json(c.s.one("SELECT body FROM programs WHERE id=?", (case["program"],), True)["body"])["history"]
    ]
    assert history == ["requirements", "scenarios", "boundaries", "contracts", "feasibility", "design", "plan", "implementation"]

    delivery = c.d.prepare(c.owner, case["project"])["id"]
    results = c.d.verify(c.owner, delivery)
    assert all(item["passed"] for item in results["results"]), results
    for role in ("integration", "goal_validation"):
        c.rt.review(c.owner, delivery, role, "fixture")
    with pytest.raises(Fault) as exc:
        c.d.certify(c.owner, delivery)
    assert exc.value.code == "release_gate_denied"
    assert "validation_mode_cannot_certify_deploy_ready" in exc.value.details
    blockers = c.p.phase_blockers(
        c.s.one("SELECT * FROM programs WHERE id=?", (case["program"],), True)
    )
    assert "integrated_delivery_unverified" in blockers
    assert c.p.next(c.owner, case["program"])["phase"] == "integration"


def test_local_execution_managed_vertical_keeps_root_scope_and_refuses_release(local_case):
    """Local proof may advance one Task while the root workflow stays blocked."""
    case = local_case
    c, task, program = case["c"], case["task"], case["program"]
    before_program = c.s.one("SELECT phase,revision,body FROM programs WHERE id=?", (program,), True)
    before_root = c.s.one("SELECT body,digest,status FROM breakdowns WHERE id=?", (case["root"],), True)
    before_obligations = c.s.all(
        "SELECT id,body FROM artifacts WHERE project=? AND kind='requirement' ORDER BY id",
        (case["project"],),
    )
    assert before_program["phase"] == "requirements"
    assert len(before_obligations) == 4
    assert sorted(
        ac
        for row in before_obligations
        for ac in parse_json(row["body"])["acceptance"]
    ) == sorted(case["acceptance"])

    packets = _review_local_packets(case)
    assert len(packets) == case["proposal"]["packet_count"]
    certified = c.local_executions.certify(
        c.owner, case["proposal"]["id"], case["proposal"]["digest"], request_id="certify-main-1"
    )
    replay = c.local_executions.certify(
        c.owner, case["proposal"]["id"], case["proposal"]["digest"], request_id="certify-main-1"
    )
    assert certified["id"] == replay["id"] and replay["replayed"] is True

    # The workflow writer attaches the current local claim in the same
    # transaction as the Task epoch; no later local helper repairs it.
    c.w.ready(c.owner, task)
    claimed_root = c.w.claim(c.owner, case["project"], task=task)
    claimed = c.local_executions.claimed(task, claimed_root["epoch"])
    assert claimed is not None
    assert parse_json(claimed["body"])["certified_event"]["id"] == certified["id"]
    assert c.local_executions.execution_readiness(c.owner, task, "execute")["allowed"] is True

    executed, execute_job = _managed(c, "execute", {"task": task, "adapter": "fixture"})
    assert executed["result"]["status"] == "submitted"
    assert execute_job["kind"] == "execute"
    tested, _ = _managed(c, "tests", {"task": task})
    assert tested["result"]["checks"][0]["result"]["passed"] is True
    for role in ("spec", "quality", "test_adequacy"):
        reviewed, _ = _managed(c, "review", {"subject": task, "role": role, "adapter": "fixture"})
        assert reviewed["result"]["result"]["verdict"] == "pass"
    _adopt_current_artifact_produced_by(c, case, task)
    completed = c.w.complete(c.owner, task, c.w.task(c.owner, task)["revision"])
    assert completed["status"] == "completed"

    after_program = c.s.one("SELECT phase,revision,body FROM programs WHERE id=?", (program,), True)
    after_root = c.s.one("SELECT body,digest,status FROM breakdowns WHERE id=?", (case["root"],), True)
    assert after_program == before_program
    assert after_root == before_root
    assert c.s.all(
        "SELECT id,body FROM artifacts WHERE project=? AND kind='requirement' ORDER BY id",
        (case["project"],),
    ) == before_obligations
    assert c.lifecycle.completion(c.owner, program)["completed"] is False
    assert c.local_executions.get(c.owner, case["proposal"]["id"])["deploy_ready"] is False


def test_invalidation_during_managed_execution_preserves_partial_work_without_adoption(local_case, tmp_path):
    """A changed authorization fences candidate adoption after subprocess observation."""
    case = local_case
    c = case["c"]
    _, running, _ = _claim_local(case)
    marker = tmp_path / "started"
    delayed = tmp_path / "delayed_execute.py"
    delayed.write_text(
        """import json, sys, time
from pathlib import Path
payload = json.load(sys.stdin)
Path(sys.argv[1]).write_text('started', encoding='utf-8')
Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n', encoding='utf-8')
time.sleep(1.2)
print(json.dumps({'message': 'delayed fixture implementation'}))
""",
        encoding="utf-8",
    )
    c.rt.adapters.register(c.owner, "delayed-local-execute", "fixture", sys.executable, [str(delayed), str(marker)])
    job = c.jobs.submit(c.owner, "execute", {"task": case["task"], "adapter": "delayed-local-execute"})
    row = c.s.one("SELECT * FROM jobs WHERE id=?", (job["id"],), True)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("outcome", c.jobs.run_one(row)), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "managed subprocess did not reach the invalidation window"
    c.local_executions.withdraw(
        c.owner,
        case["proposal"]["id"],
        case["proposal"]["digest"],
        "Invalidate the local authorization while implementation is still running",
    )
    worker.join(10)
    assert not worker.is_alive(), "managed execution did not finish after invalidation"
    outcome = result["outcome"]
    assert outcome["status"] == "failed"
    assert outcome["error"]["code"] == "local_execution_stale"
    receipt_id = outcome["error"]["receipt"]
    receipt = c.g.receipt(receipt_id)
    assert receipt["process_started"] is True
    assert receipt["work_product"]["adopted"] is False
    assert outcome["error"]["partial_work"]["snapshot_blob"] == receipt["work_product"]["snapshot_blob"]
    assert c.s.one("SELECT candidate FROM tasks WHERE id=?", (case["task"],))["candidate"] is None
    assert c.w.task(c.owner, case["task"])["status"] == "planned"


def test_withdrawn_local_authorization_transfers_to_ordinary_root_execution(local_case):
    """A later ordinary root claim remains available after local preparation is withdrawn."""
    case = local_case
    c = case["c"]
    _review_local_packets(case)
    certified = c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    _prepare_current_root_for_execution(case)
    c.local_executions.withdraw(
        c.owner,
        case["proposal"]["id"],
        case["proposal"]["digest"],
        "Root plan now owns the ordinary execution route",
    )
    c.w.ready(c.owner, case["task"])
    root_claim = c.w.claim(c.owner, case["project"], task=case["task"])
    assert root_claim["status"] == "running"
    assert c.local_executions.claimed(case["task"], root_claim["epoch"]) is None
    assert c.g.execution_readiness(c.owner, case["task"], "execute")["route"] == "root"
    executed, _ = _managed(c, "execute", {"task": case["task"], "adapter": "fixture"})
    assert executed["result"]["status"] == "submitted"
    _managed(c, "tests", {"task": case["task"]})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": case["task"], "role": role, "adapter": "fixture"})
    _adopt_current_artifact_produced_by(c, case, case["task"])
    completed = c.w.complete(c.owner, case["task"], c.w.task(c.owner, case["task"])["revision"])
    assert completed["status"] == "completed"
    candidate = parse_json(c.s.one("SELECT body FROM candidates WHERE id=?", (completed["candidate"],), True)["body"])
    assert "execution_authorization" not in candidate
    assert certified["current"] is True


def test_local_completion_does_not_bypass_final_delivery_with_open_decision(local_case):
    """A locally completed Task still leaves final delivery blocked by a global decision."""
    case = local_case
    c = case["c"]
    _claim_local(case)
    _managed(c, "execute", {"task": case["task"], "adapter": "fixture"})
    _managed(c, "tests", {"task": case["task"]})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": case["task"], "role": role, "adapter": "fixture"})
    _adopt_current_artifact_produced_by(c, case, case["task"])
    c.w.complete(c.owner, case["task"], c.w.task(c.owner, case["task"])["revision"])

    delivery_profile = profile(case["project"], case["repo"], case["requirement"], case["task"])
    delivery_profile["required_requirements"] = case["requirements"]
    delivery_profile["required_tasks"] = [case["task"], case["analysis"]]
    delivery_profile["program"] = case["program"]
    c.d.configure(c.owner, case["project"], delivery_profile)
    delivery = c.d.prepare(c.owner, case["project"])["id"]
    decision = c.p.propose_decision(
        c.owner,
        case["project"],
        {
            "title": "Open product decision remains",
            "reason": "The root still needs a human choice before integrated delivery.",
            "options": ["retain", "change"],
            "recommendation": "retain",
            "refs": [case["requirement"]],
            "requirement_affecting": False,
        },
    )
    assert decision["id"]
    with pytest.raises(Fault) as exc:
        c.d.certify(c.owner, delivery)
    assert exc.value.code == "release_gate_denied"
    assert "unconfirmed_decisions" in exc.value.details
    assert c.s.one("SELECT status FROM deliveries WHERE id=?", (delivery,))["status"] == "prepared"
    assert c.local_executions.get(c.owner, case["proposal"]["id"])["deploy_ready"] is False


def test_local_inventory_and_proposal_are_exactly_public_and_read_only(local_case):
    case = local_case
    c = case["c"]
    api = c.describe(c.owner)["methods"]
    assert {
        "local_execution.propose", "local_execution.inventory", "local_execution.get",
        "local_execution.list", "local_execution.packet", "local_execution.audit",
        "local_execution.certify", "local_execution.withdraw",
    } <= set(api)
    described = api["local_execution.inventory"]
    assert described["read_only"] is True
    assert "items" in described["body_contract"]["result"]
    before = c.s.one("SELECT count(*) AS n FROM local_execution_proposals")["n"]
    assert case["proposal"]["packet_count"] >= 2
    again = _inventory(c, case["program"], case["partial"], case["task"])
    assert [(x["id"], x["digest"]) for x in again] == [(x["id"], x["digest"]) for x in case["items"]]
    packet = c.local_executions.get(c.owner, case["proposal"]["id"])["packets"][0]
    packet_read = c.local_executions.packet(c.owner, packet["id"])
    assert packet_read["digest"] == packet["digest"]
    assert packet_read["body"]["material_digest"] == case["proposal"]["material_digest"]
    with pytest.raises(Fault) as exc:
        c.local_executions.propose(
            c.owner,
            case["program"],
            case["partial"],
            [case["task"]],
            "Changed rationale must not reuse a request ID.",
            case["stage"],
            case["dispositions"],
            byte_budget=24000,
            request_id="local-main-1",
        )
    assert exc.value.code == "idempotency_conflict"
    assert c.s.one("SELECT count(*) AS n FROM local_execution_proposals")["n"] == before == 1
    assert c.s.one("SELECT count(*) AS n FROM local_execution_records")["n"] == 0


@pytest.mark.parametrize("bad", ["no_certification", "latest_fail", "withdrawn", "epoch"])
def test_local_authorization_rejects_missing_or_invalidated_claim(local_case, tmp_path, bad):
    case = local_case
    c, task = case["c"], case["task"]
    if bad == "no_certification":
        assert c.local_executions.execution_readiness(c.owner, task, "claim")["allowed"] is False
        with pytest.raises(Fault):
            c.local_executions.claim(c.owner, task, 0)
        assert c.w.task(c.owner, task)["status"] == "planned"
        return

    _review_local_packets(case)
    certified = c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    c.w.ready(c.owner, task)
    running = c.w.claim(c.owner, case["project"], task=task)
    assert c.local_executions.claimed(task, running["epoch"]) is not None

    if bad == "latest_fail":
        failing = tmp_path / "local-fail.py"
        failing.write_text(
            """import json,sys
p=json.load(sys.stdin)
print(json.dumps({'verdict':'fail','rationale':'fixture latest failure',
 'covered':p['context'].get('required_coverage',[]),
 'findings':[{'severity':'high','statement':'fixture failure','evidence':'fixture'}],
 'observations':[{'ref':p.get('subject','packet'),'detail':'fixture'}],
 'dispositions':[]}))
""",
            encoding="utf-8",
        )
        c.rt.adapters.register(c.owner, "local-fail-fixture", "fixture", sys.executable, [str(failing)])
        packet = c.local_executions.get(c.owner, case["proposal"]["id"])["packets"][0]["id"]
        _managed(c, "review", {"subject": packet, "role": "feasibility", "adapter": "local-fail-fixture"}, expected="succeeded")
        assert c.local_executions.execution_readiness(c.owner, task, "execute")["allowed"] is False
        with pytest.raises(Fault):
            c.rt.execute(c.owner, task, "fixture")
    elif bad == "withdrawn":
        c.local_executions.withdraw(c.owner, case["proposal"]["id"], case["proposal"]["digest"], "withdraw for reassessment")
        with pytest.raises(Fault):
            c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
        with pytest.raises(Fault):
            c.local_executions.claim(c.owner, task, running["epoch"])
    else:
        # A public pause transition fences the old epoch and lease.
        c.w.pause(c.owner, case["project"], task=task, paused=True)
        with pytest.raises(Fault):
            c.local_executions.claim(c.owner, task, running["epoch"])
        assert c.local_executions.execution_readiness(c.owner, task, "execute")["allowed"] is False


def test_local_plan_revision_invalidates_certification_and_preserves_history(local_case):
    case = local_case
    c, task = case["c"], case["task"]
    _review_local_packets(case)
    certified = c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    assert certified["current"] is True
    old_task = c.w.task(c.owner, task)
    c.w.replan(c.owner, task, old_task["revision"], "Change test plan after local certification")
    # The canonical task replan changes the frozen semantic material.  The old
    # certification remains immutable history but cannot authorize execution.
    assert c.local_executions.audit(c.owner, case["proposal"]["id"], reviews=True)["current"] is False
    assert c.local_executions.execution_readiness(c.owner, task, "execute")["allowed"] is False
    with pytest.raises(Fault):
        c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    assert c.s.one("SELECT id FROM local_execution_records WHERE kind='certified'") is not None
    assert c.local_executions.get(c.owner, case["proposal"]["id"])["history_approved"] is True


def test_invalid_current_claim_retains_local_owner_and_denies_admission(local_case):
    """A corrupt current claim cannot collapse the local branch to empty ownership."""
    case = local_case
    c = case["c"]
    _claim_local(case)
    claim = c.local_executions.claimed(case["task"])
    claim_body = parse_json(claim["body"])
    claim_body["epoch"] += 1
    c.s.execute("DROP TRIGGER IF EXISTS local_execution_records_immutable")
    c.s.execute(
        "UPDATE local_execution_records SET body=? WHERE id=?",
        (json.dumps(claim_body), claim["id"]),
    )

    result = inspect_task_admission(
        c, c.owner, task=case["task"], checkpoint="complete",
    )

    assert result["allowed"] is False
    assert result["local_selection"]["state"] == "invalid"
    assert result["local_selection"]["selector"] == case["proposal"]["id"]
    assert result["local_selection"]["program"] == case["program"]
    assert any(item.get("code") == "integrity_error" for item in result["failures"])


def test_missing_local_provider_retains_proposal_owner_and_denies_admission(local_case):
    """A retained local row cannot become an empty allowed population."""
    case = local_case
    c = case["c"]
    _claim_local(case)
    c.local_executions = None

    result = inspect_task_admission(
        c, c.owner, task=case["task"], checkpoint="complete",
    )

    assert result["allowed"] is False
    assert result["local_selection"]["state"] == "invalid"
    assert result["local_selection"]["program"] == case["program"]
    assert result["canonical_programs"] == [case["program"]]
    assert any(item.get("code") == "admission_dependency_unavailable"
               for item in result["failures"])


def test_missing_current_local_selector_retains_proposal_owner_and_denies_admission(
    local_case, monkeypatch,
):
    """A resolver that returns no selector is invalid while a proposal exists."""
    case = local_case
    c = case["c"]
    _claim_local(case)
    monkeypatch.setattr(c.local_executions, "claimed", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        c.local_executions,
        "current_authorization_readonly",
        lambda *args, **kwargs: {},
    )

    result = inspect_task_admission(
        c, c.owner, task=case["task"], checkpoint="complete",
    )

    assert result["allowed"] is False
    assert result["local_selection"]["state"] == "invalid"
    assert result["local_selection"]["program"] == case["program"]
    assert result["canonical_programs"] == [case["program"]]
    assert any(item.get("code") == "local_claim_missing"
               for item in result["failures"])


def test_local_certification_rejects_missing_coverage_and_disposition_finding(local_case, tmp_path):
    case = local_case
    c = case["c"]
    bad = tmp_path / "local-incomplete-review.py"
    bad.write_text(
        """import json,sys
p=json.load(sys.stdin)
print(json.dumps({'verdict':'pass','rationale':'fixture intentionally omits required markers',
'covered':[],'findings':[],'observations':[{'ref':p.get('subject','packet'),'detail':'fixture'}],
'dispositions':[]}))
""",
        encoding="utf-8",
    )
    c.rt.adapters.register(c.owner, "local-incomplete-fixture", "fixture", sys.executable, [str(bad)])
    packet = c.local_executions.get(c.owner, case["proposal"]["id"])["packets"][0]["id"]
    _managed(c, "review", {"subject": packet, "role": "feasibility", "adapter": "local-incomplete-fixture"})
    with pytest.raises(Fault) as exc:
        c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    assert exc.value.code == "local_execution_gate_denied"
    assert any(item.get("code") in {"review_required", "review_coverage"} for item in exc.value.details)


def test_stage_evidence_rejects_exit_zero_failed_review_receipt(local_case, tmp_path):
    """A successful process with a FAIL judgment is not canonical stage evidence."""
    case = local_case
    c = case["c"]
    failed = tmp_path / "failed-scenario-review.py"
    failed.write_text(
        """import json, sys
payload = json.load(sys.stdin)
print(json.dumps({'verdict': 'fail', 'rationale': 'fixture records a failed scenario review',
 'covered': [], 'findings': [{'severity': 'high', 'statement': 'scenario is unresolved', 'evidence': payload.get('subject', 'scenario')}],
 'observations': [{'ref': payload.get('subject', 'scenario'), 'detail': 'Observed failing fixture review.'}],
 'dispositions': []}))
""",
        encoding="utf-8",
    )
    c.rt.adapters.register(c.owner, "failed-scenario-review", "fixture", sys.executable, [str(failed)])
    failed_receipt = c.rt.review(c.owner, case["scenario"], "spec", "failed-scenario-review")["receipt"]
    bad_stage = copy.deepcopy(case["stage"])
    bad_stage[case["task"]]["scenarios"]["review"] = failed_receipt
    with pytest.raises(Fault):
        c.local_executions.propose(
            c.owner,
            case["program"],
            case["partial"],
            [case["task"]],
            "Do not certify a failed scenario review receipt.",
            bad_stage,
            case["dispositions"],
            byte_budget=24000,
            request_id="local-failed-stage-1",
        )
    assert c.s.one("SELECT count(*) AS n FROM local_execution_proposals")["n"] == 1


def test_fixture_review_cannot_be_promoted_to_governed_local_certification(local_case):
    case = local_case
    c = case["c"]
    _review_local_packets(case)
    c.g.mode = "governed"
    with pytest.raises(Fault) as exc:
        c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    assert exc.value.code == "local_execution_gate_denied"
    assert any(item.get("code") in {"unqualified_execution", "adapter_unqualified"}
               for item in exc.value.details)


def test_schema11_migrates_additively_and_local_archive_includes_assurance_v11(local_case, tmp_path):
    case = local_case
    c = case["c"]
    _review_local_packets(case)
    c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    # Claim through the public workflow before exporting so archive validation
    # checks the task/epoch authorization reference as well.
    c.w.ready(c.owner, case["task"])
    running = c.w.claim(c.owner, case["project"], task=case["task"])
    assert c.local_executions.claimed(case["task"], running["epoch"]) is not None
    baseline = c.k.baseline(c.owner, case["project"])
    exported = c.history.export_archive(c.owner, baseline["id"])
    assert exported["format"] == "daikibo.knowledge-archive.v12"
    inspected = inspect_archive(exported["path"], exported["sha256"])
    assert inspected["verified"] is True
    assert inspected["counts"]["local_execution_proposals"] == 1
    assert inspected["counts"]["local_execution_packets"] == case["proposal"]["packet_count"]
    assert inspected["counts"]["local_execution_records"] == 2
    assert inspected["counts"]["assurance_objects"] >= 1
    assert inspected["counts"]["assurance_refs"] >= 1
    spec = c.k.export(c.owner, case["project"])
    assert "local_execution_history" in spec
    assert spec["runtime_restore_supported"] is False
    assert spec["fresh_review_or_test_evidence"] is False

    # Exercise a real v11 -> v12 migration: copy the canonical database,
    # remove the v12 tables/triggers from that copy, and lower its version
    # before opening it.  Merely changing user_version on a schema12 database
    # would only test idempotent CREATE IF NOT EXISTS behavior.
    old_home = tmp_path / "v11-control"
    old_home.mkdir()
    c.s.backup_database(old_home / "state.sqlite3")
    shutil.copytree(c.s.home / "blobs", old_home / "blobs")
    old_db = sqlite3.connect(old_home / "state.sqlite3")
    old_db.executescript(
        """
        DROP TABLE program_origins;
        DROP TRIGGER IF EXISTS local_execution_proposals_immutable;
        DROP TRIGGER IF EXISTS local_execution_proposals_no_delete;
        DROP TRIGGER IF EXISTS local_execution_packets_immutable;
        DROP TRIGGER IF EXISTS local_execution_packets_no_delete;
        DROP TRIGGER IF EXISTS local_execution_records_immutable;
        DROP TRIGGER IF EXISTS local_execution_records_no_delete;
        DROP TRIGGER IF EXISTS local_execution_records_shape;
        DROP TABLE local_execution_records;
        DROP TABLE local_execution_packets;
        DROP TABLE local_execution_proposals;
        PRAGMA user_version=11;
        """
    )
    old_db.commit()
    old_db.close()
    before_task_count = c.s.one("SELECT count(*) AS n FROM tasks")["n"]
    before_plan_count = c.s.one("SELECT count(*) AS n FROM plans")["n"]
    before_subplan_count = c.s.one("SELECT count(*) AS n FROM subplans")["n"]
    reopened = Control(old_home, mode="validation", start_workers=False)
    try:
        reopened.owner = reopened.sec.authenticate(None)
        assert reopened.s.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION == 16
        assert (old_home / "pre-migration-v11.sqlite3").is_file()
        assert reopened.s.one("SELECT count(*) AS n FROM local_execution_proposals")["n"] == 0
        assert reopened.s.one("SELECT count(*) AS n FROM local_execution_packets")["n"] == 0
        assert reopened.s.one("SELECT count(*) AS n FROM local_execution_records")["n"] == 0
        assert reopened.s.one("SELECT count(*) AS n FROM tasks")["n"] == before_task_count
        assert reopened.s.one("SELECT count(*) AS n FROM plans")["n"] == before_plan_count
        assert reopened.s.one("SELECT count(*) AS n FROM subplans")["n"] == before_subplan_count
    finally:
        reopened.close()


def test_local_packet_tamper_and_archive_missing_section_are_rejected(local_case, tmp_path):
    case = local_case
    c = case["c"]
    packet_id = c.local_executions.get(c.owner, case["proposal"]["id"])["packets"][0]["id"]
    with pytest.raises(sqlite3.IntegrityError):
        c.s.execute("UPDATE local_execution_packets SET body=? WHERE id=?", ("{}", packet_id))
    _review_local_packets(case)
    c.local_executions.certify(c.owner, case["proposal"]["id"], case["proposal"]["digest"])
    c.w.ready(c.owner, case["task"])
    running = c.w.claim(c.owner, case["project"], task=case["task"])
    assert c.local_executions.claimed(case["task"], running["epoch"]) is not None
    baseline = c.k.baseline(c.owner, case["project"])
    exported = c.history.export_archive(c.owner, baseline["id"])
    target = tmp_path / "archive-copy.zip"
    shutil.copyfile(exported["path"], target)
    # A transport copy with its original digest remains valid; changing the
    # archive bytes without recomputing the external digest is rejected.
    with target.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(Fault):
        inspect_archive(target, exported["sha256"])

    # The standalone specification verifier receives a fully materialized,
    # re-digested export.  Missing history sections, duplicate identities and
    # dangling certification references must still be rejected; archive byte
    # checksums alone cannot provide those cross-record guarantees.
    spec = c.k.export(c.owner, case["project"])
    assert validate_specifications(spec)["local_execution_records"] == 2

    missing = copy.deepcopy(spec)
    del missing["local_execution_history"]["local_execution_records"]
    with pytest.raises(Fault):
        validate_specifications(missing)

    duplicate = copy.deepcopy(spec)
    history = duplicate["local_execution_history"]
    history["local_execution_proposals"].append(copy.deepcopy(history["local_execution_proposals"][0]))
    with pytest.raises(Fault):
        validate_specifications(duplicate)

    dangling = copy.deepcopy(spec)
    claimed = next(row for row in dangling["local_execution_history"]["local_execution_records"]
                   if row["kind"] == "claimed")
    claimed["body"]["certified_event"]["id"] = "LEXREC-missing"
    claimed["digest"] = digest(claimed["body"])
    with pytest.raises(Fault):
        validate_specifications(dangling)

    wrong_record = copy.deepcopy(spec)
    wrong_claim = next(row for row in wrong_record["local_execution_history"]["local_execution_records"]
                       if row["kind"] == "claimed")
    wrong_claim["body"]["certified_event"] = {
        "id": wrong_claim["id"], "digest": wrong_claim["digest"],
    }
    wrong_claim["body"]["certification_digest"] = wrong_claim["digest"]
    wrong_claim["digest"] = digest(wrong_claim["body"])
    with pytest.raises(Fault):
        validate_specifications(wrong_record)

    duplicate_review = copy.deepcopy(spec)
    certified = next(row for row in duplicate_review["local_execution_history"]["local_execution_records"]
                     if row["kind"] == "certified")
    reviews = certified["body"]["reviews"]
    reviews[1] = copy.deepcopy(reviews[0])
    certified["body"]["review_digest"] = digest(reviews)
    certified["digest"] = digest(certified["body"])
    with pytest.raises(Fault):
        validate_specifications(duplicate_review)


def test_legacy_v2_archive_reader_remains_available():
    fixture = Path(__file__).with_name("fixtures") / "dev5-v2-history.dkarchive"
    checksum = fixture.with_name("dev5-v2-history.sha256").read_text(encoding="utf-8").split()[0]
    report = inspect_archive(fixture, checksum)
    assert report["verified"] is True
    assert report["runtime_restore_supported"] is False


def test_candidate_edge_cannot_replace_declared_artifact_material(local_case):
    """A candidate alias cannot satisfy an artifact output by cardinality."""
    case = local_case
    c = case["c"]
    task = case["task"]
    _claim_local(case)
    _managed(c, "execute", {"task": task, "adapter": "fixture"})
    _managed(c, "tests", {"task": task})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": task, "role": role, "adapter": "fixture"})
    assert c.s.one(
        "SELECT count(*) AS n FROM assurance_objects "
        "WHERE kind='material' AND json_extract(body,'$.material_kind')='artifact_production'",
    )["n"] == 0

    _adopt_current_produced_by(c, case, task)
    result = inspect_task_admission(c, c.owner, task=task, checkpoint="complete")
    assert result["allowed"] is False


def test_local_inventory_keeps_unrelated_malformed_and_stale_drafts(local_case):
    """Only a current, exact producer material may project a draft away."""
    case = local_case
    c, task = case["c"], case["task"]
    _claim_local(case)
    _managed(c, "execute", {"task": task, "adapter": "fixture"})
    _managed(c, "tests", {"task": task})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": task, "role": role, "adapter": "fixture"})

    unrelated = c.k.propose(
        c.owner, case["project"], "finding",
        {"title": "Unrelated draft", "statement": "This draft is outside the selected Task."},
    )
    malformed = c.k.propose(
        c.owner, case["project"], "finding",
        {"title": "Malformed production draft", "statement": "Its producer packet is invalid."},
    )
    # This is an explicit retained-data negative fixture.  It has the right
    # broad material label and selected Task/artifact selectors, but no valid
    # controller production payload or indexed dependency closure.
    malformed_payload = {
        "task_ref": {"task": task},
        "artifact_ref": {"kind": "artifact", "project": case["project"],
                          "artifact": malformed["id"], "revision": malformed["revision"],
                          "body_digest": malformed["digest"]},
    }
    payload_blob = c.s.blob_put(canonical(malformed_payload))
    envelope = {"material_kind": "artifact_production", "payload_blob": payload_blob}
    c.s.execute(
        "INSERT INTO assurance_objects(id,project,kind,logical_id,revision,body,digest,created) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("AOBJ-malformed-local-production", case["project"], "material",
         "malformed-local-production", 1, canonical(envelope).decode(), digest(envelope),
         time.time()),
    )

    produced = _adopt_current_artifact_produced_by(c, case, task)
    produced_artifact = produced["artifact_ref"]["artifact"]
    current = c.s.one(
        "SELECT * FROM artifacts WHERE id=? AND project=?",
        (produced_artifact, case["project"]), True,
    )
    revised = parse_json(current["body"])
    revised["statement"] = "The produced draft was revised after collection."
    c.k.revise(
        c.owner, produced_artifact, current["revision"], revised,
        "Make the producer pin stale for the projection check.",
    )

    inventory = c.local_executions._artifact_inventory(
        c.owner, case["project"], [task],
    )
    inventory_ids = {item["id"] for item in inventory}
    assert "artifact:" + unrelated["id"] in inventory_ids
    assert "artifact:" + malformed["id"] in inventory_ids
    # The exact producer existed, but its pinned draft is no longer current;
    # a stale output cannot be silently treated as the selected Task's result.
    assert "artifact:" + produced_artifact in inventory_ids


def test_completed_local_claim_recheck_rejects_a_stale_plan(local_case):
    """A completed local claim cannot replay through a later stale proof."""
    case = local_case
    c, task = case["c"], case["task"]
    _claim_local(case)
    _managed(c, "execute", {"task": task, "adapter": "fixture"})
    _managed(c, "tests", {"task": task})
    for role in ("spec", "quality", "test_adequacy"):
        _managed(c, "review", {"subject": task, "role": role, "adapter": "fixture"})
    _adopt_current_artifact_produced_by(c, case, task)
    completed = c.w.complete(c.owner, task, c.w.task(c.owner, task)["revision"])
    assert completed["status"] == "completed"

    c.s.execute("DROP TRIGGER receipts_no_delete")
    c.s.execute(
        "DELETE FROM receipts WHERE subject=? AND role='test_plan'", (task,),
    )
    before_changes = c.s.conn.total_changes
    before = c.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate "
        "FROM tasks WHERE id=?", (task,), True,
    )
    result = inspect_task_admission(c, c.owner, task=task, checkpoint="recheck")
    assert result["allowed"] is False
    assert result["failures"]
    after = c.s.one(
        "SELECT status,epoch,attempts,lease_owner,lease_until,candidate "
        "FROM tasks WHERE id=?", (task,), True,
    )
    assert after == before
    assert c.s.conn.total_changes == before_changes
