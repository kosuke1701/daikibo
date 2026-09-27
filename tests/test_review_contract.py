"""Dev20/21 review-output vocabulary and findings-scope boundaries."""
from __future__ import annotations

import json
import sys
from copy import deepcopy

import pytest

from conftest import make_task
from daikibo.agents import Adapters, REVIEW_SCHEMA
from daikibo.common import Fault, canonical, parse_json
from daikibo.review_contract import (
    EXECUTION_CONTROL_RESOLUTIONS,
    execution_control_resolution_instructions,
    review_schema,
)
from daikibo.runtime import FIXED_TEST_CONTRACT, MANAGED_OUTPUT_CONTRACT, SNAPSHOT_IDENTITY_CONTEXT


def _review(resolution="progress", marker="attempt:1"):
    return {
        "verdict": "pass",
        "rationale": "typed fixture result",
        "covered": [marker],
        "findings": [],
        "observations": [{"ref": marker, "detail": "retained fixture observation"}],
        "dispositions": [{"id": marker, "resolution": resolution, "reason": "typed fixture decision"}],
    }


def test_execution_control_schema_is_shared_by_prompt_and_claude_codex(tmp_path, full, full_project, monkeypatch):
    selected = review_schema("execution_control")
    resolution = selected["properties"]["dispositions"]["items"]["properties"]["resolution"]
    assert resolution["enum"] == list(EXECUTION_CONTROL_RESOLUTIONS)
    assert "approve" not in resolution["enum"]
    assert review_schema("spec") == REVIEW_SCHEMA
    assert review_schema("spec") is not REVIEW_SCHEMA
    assert "findings=[]" in REVIEW_SCHEMA["properties"]["verdict"]["description"]
    assert "assessment or permission proposal" in selected["properties"]["findings"]["description"]
    assert "existing product defect" in selected["properties"]["observations"]["description"]
    selected["properties"]["dispositions"]["items"]["properties"]["resolution"]["enum"].append("mutated")
    assert "mutated" not in review_schema("execution_control")["properties"]["dispositions"]["items"]["properties"]["resolution"]["enum"]

    adapters = Adapters(None, None, "validation")
    claude = {"kind": "claude", "executable": "claude", "extra_args": [], "model": None}
    claude_argv, _ = adapters.command(claude, "execution_control", tmp_path, tmp_path)
    claude_schema = json.loads(claude_argv[claude_argv.index("--json-schema") + 1])

    codex = {"kind": "codex", "executable": "codex", "extra_args": [], "model": None}
    adapters.command(codex, "execution_control", tmp_path, tmp_path)
    codex_schema = json.loads((tmp_path / "review-schema.json").read_text())
    assert claude_schema == review_schema("execution_control") == codex_schema

    task = make_task(full, full_project)
    captured = {}

    def fake_observe(*args, **kwargs):
        captured["prompt"] = parse_json(kwargs["prompt"])
        return {"id": "EVD-schema", "run": "RUN-schema", "result": _review(), "assurance": {}}, None, None

    monkeypatch.setattr(full.rt, "observe", fake_observe)
    full.rt.review(full.owner, task, "execution_control", "fixture")
    assert captured["prompt"]["schema"] == review_schema("execution_control")
    assert captured["prompt"]["instructions"].count(MANAGED_OUTPUT_CONTRACT) == 1
    assert "attempt:<epoch> uses progress, no_progress, inconclusive" in captured["prompt"]["instructions"]
    assert "recovery:<proposal> and timeout:<proposal> use approved, rejected, inconclusive" in captured["prompt"]["instructions"]
    assert "findings=[]" in captured["prompt"]["instructions"]
    assert "existing implementation, quality, or test defects" in captured["prompt"]["instructions"]
    assert execution_control_resolution_instructions().strip() in captured["prompt"]["instructions"]


def test_test_plan_reviewer_receives_the_fixed_candidate_test_contract(full, full_project, monkeypatch):
    task = make_task(full, full_project)
    captured = {}

    def fake_observe(*args, **kwargs):
        captured['prompt'] = parse_json(kwargs['prompt'])
        return {'id': 'EVD-test-plan-contract', 'run': 'RUN-test-plan-contract',
                'result': _review(), 'assurance': 'validation'}, None, None

    monkeypatch.setattr(full.rt, 'observe', fake_observe)
    full.rt.review(full.owner, task, 'test_plan', 'fixture')

    assert FIXED_TEST_CONTRACT in captured['prompt']['instructions']
    assert captured['prompt']['instructions'].count(MANAGED_OUTPUT_CONTRACT) == 1
    assert SNAPSHOT_IDENTITY_CONTEXT in captured['prompt']['instructions']


def test_approve_is_rejected_only_for_execution_control_and_never_normalized():
    ordinary = _review("approve")
    assert Adapters.validate_review(ordinary, "quality") is ordinary

    control = deepcopy(ordinary)
    with pytest.raises(Fault) as error:
        Adapters.validate_review(control, "execution_control")
    assert error.value.code == "invalid_review"
    assert control["dispositions"][0]["resolution"] == "approve"

    with pytest.raises(Fault) as error:
        from daikibo.execution_controls import ExecutionControls

        ExecutionControls._dispositions(
            object(),
            {"result": _review("approve")},
            {"id": "XCP", "body": {"control_type": "assessment", "target_attempt": {"epoch": 1}}},
        )
    assert error.value.code == "invalid_review"


def test_invalid_execution_control_receipt_keeps_raw_output_and_is_not_authoritative(full, full_project, tmp_path):
    task = make_task(full, full_project)
    reviewer = tmp_path / "approve-review.py"
    reviewer.write_text(
        "import json\n"
        "payload = json.load(__import__('sys').stdin)\n"
        "print(json.dumps({'verdict':'pass','rationale':'bad alias fixture','covered':['attempt:1'],"
        "'findings':[],'observations':[{'ref':payload['subject'],'detail':'raw alias'}],"
        "'dispositions':[{'id':'attempt:1','resolution':'approve','reason':'alias'}]}))\n"
    )
    reviewer.chmod(0o755)
    full.rt.adapters.register(full.owner, "approve-review", "fixture", sys.executable, [str(reviewer)])

    review = full.rt.review(full.owner, task, "execution_control", "approve-review")
    receipt = parse_json(full.s.one("SELECT body FROM receipts WHERE id=?", (review["receipt"],), True)["body"])
    raw = full.s.blob_get(receipt["stdout_blob"])
    assert b'"approve"' in raw
    assert receipt["judgment_valid"] is False
    assert receipt["result"]["verdict"] == "blocked"
    assert receipt["result"]["error"]["code"] == "protocol_error"


def test_pass_with_findings_is_invalid_and_raw_fixture_evidence_is_retained(full, full_project, tmp_path):
    task = make_task(full, full_project)
    reviewer = tmp_path / "pass-findings-review.py"
    reviewer.write_text(
        "import json\n"
        "payload = json.load(__import__('sys').stdin)\n"
        "print(json.dumps({'verdict':'pass','rationale':'invalid pass with finding','covered':['attempt:1'],"
        "'findings':[{'severity':'high','statement':'existing implementation defect',"
        "'evidence':'repo/calc.py'}],"
        "'observations':[{'ref':payload['subject'],'detail':'defect retained as raw evidence'}],"
        "'dispositions':[{'id':'attempt:1','resolution':'progress','reason':'semantic progress evidence'}]}))\n"
    )
    reviewer.chmod(0o755)
    full.rt.adapters.register(full.owner, "pass-findings-review", "fixture", sys.executable, [str(reviewer)])

    review = full.rt.review(full.owner, task, "execution_control", "pass-findings-review")
    receipt = parse_json(full.s.one("SELECT body FROM receipts WHERE id=?", (review["receipt"],), True)["body"])
    raw = full.s.blob_get(receipt["stdout_blob"])
    assert b"existing implementation defect" in raw
    assert receipt["judgment_valid"] is False
    assert receipt["result"]["verdict"] == "blocked"


def test_observations_can_carry_product_defect_without_invalidating_typed_pass():
    from daikibo.execution_controls import ExecutionControls

    review = _review("progress")
    review["observations"].append({
        "ref": "repo/calc.py",
        "detail": "Existing product defect retained as evidence; it is not by itself a defect in this assessment.",
    })
    assert Adapters.validate_review(review, "execution_control") is review
    class EmptyAssessments:
        def one(self, *args):
            return None

    control = type("Control", (), {"s": EmptyAssessments()})()
    assert ExecutionControls._dispositions(
        control,
        {"result": review},
        {"id": "XCP", "task": "TASK", "body": {"control_type": "assessment", "target_attempt": {"epoch": 1}}},
    ) == ("progress", "inconclusive", "inconclusive")


def test_assessment_findings_remain_a_failed_decision_and_cannot_apply():
    from daikibo.execution_controls import ExecutionControls

    failed = _review("progress")
    failed["verdict"] = "fail"
    failed["findings"] = [{
        "severity": "high",
        "statement": "The assessment lacks semantic evidence.",
        "evidence": "receipt:EVD-missing",
    }]
    with pytest.raises(Fault) as error:
        ExecutionControls._dispositions(
            object(),
            {"result": failed},
            {"id": "XCP", "body": {"control_type": "assessment", "target_attempt": {"epoch": 1}}},
        )
    assert error.value.code == "review_failed"
    assert failed["findings"][0]["evidence"] == "receipt:EVD-missing"
