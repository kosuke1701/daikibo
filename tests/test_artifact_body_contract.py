import json
import sys
from pathlib import Path

import pytest

from daikibo.common import Fault
from daikibo.knowledge import KINDS, Knowledge, artifact_body_contract


EXPECTED_REQUIRED = {
    "requirement": ["title", "statement", "acceptance"],
    "domain": ["title", "statement", "responsibilities", "non_responsibilities", "owned_data", "interfaces"],
    "interface": ["title", "statement", "input", "output", "authentication", "errors", "idempotency", "compatibility", "consumers", "verification"],
}


def _valid_body(kind):
    body = {"title": kind, "statement": "A bounded statement", "semantic_extra": {"meaning": "reviewed separately"}}
    if kind == "requirement":
        body["acceptance"] = ["AC-1"]
    elif kind == "domain":
        body.update({"responsibilities": [], "non_responsibilities": [], "owned_data": [], "interfaces": []})
    elif kind == "interface":
        body.update({key: {} for key in EXPECTED_REQUIRED["interface"][2:]})
    return body


def test_describe_body_contract_matches_validator_for_every_kind(full):
    contract = artifact_body_contract()
    assert set(contract["kinds"]) == KINDS
    for kind in KINDS:
        metadata = contract["kinds"][kind]
        body = _valid_body(kind)
        Knowledge.validate_body(kind, body)
        assert metadata["required"] == EXPECTED_REQUIRED.get(kind, ["title", "statement"])
        for field in metadata["required"]:
            missing = dict(body)
            missing.pop(field)
            with pytest.raises(Fault):
                Knowledge.validate_body(kind, missing)

    assert contract["kinds"]["requirement"]["limits"]["acceptance"]["max_items"] == 200
    assert contract["kinds"]["domain"]["limits"]["responsibilities"]["max_items"] == 10000
    assert contract["kinds"]["interface"]["required"][2:] == EXPECTED_REQUIRED["interface"][2:]
    assert {item["when"]["field"] for item in contract["kinds"]["interface"]["conditional"]} == {"input", "output"}
    assert all(item["when"]["format"] == "daikibo.type.v1" for item in contract["kinds"]["interface"]["conditional"])
    assert contract["kinds"]["interface"]["fields"]["input"]["validation"] == "presence_only"
    assert all(item["type"] == "object" and item["json"]["finite"] for item in contract["kinds"].values())
    assert all(item["extra_fields"] == "allowed" for item in contract["kinds"].values())

    described = full.describe(full.owner)
    methods = described["methods"]
    assert methods["artifact.propose"]["body_contract"] == contract
    assert methods["artifact.revise"]["body_contract"] == contract
    assert methods["artifact.save"]["body_contract"] == contract
    assert "body_contract" not in methods["artifact.accept"]
    assert full.describe(full.owner, method="artifact.get")["methods"]["artifact.get"].get("body_contract") is None
    assert set(full.describe(full.owner, method="artifact.propose")["methods"]) == {"artifact.propose"}


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_body_contract_limits_and_existing_validation_remain_bounded(full, kind):
    body = _valid_body(kind)
    with pytest.raises(Fault):
        Knowledge.validate_body(kind, {**body, "title": " "})
    with pytest.raises(Fault):
        Knowledge.validate_body(kind, {**body, "title": "x\x00y"})
    with pytest.raises(ValueError):
        Knowledge.validate_body(kind, {**body, "nonfinite": float("nan")})
    with pytest.raises(Fault):
        Knowledge.validate_body(kind, {**body, "constraints": {"nested": {"value": 1}}})
    if kind not in {"requirement", "domain", "interface"}:
        body["constraints"] = {"mode": "serial", "enabled": True, "count": 2, "ratio": 0.5, "unset": None}
        Knowledge.validate_body(kind, body)

    if kind == "requirement":
        with pytest.raises(Fault):
            Knowledge.validate_body(kind, {**body, "acceptance": []})
        at_limit = {**body, "acceptance": [f"AC-{index}" for index in range(200)]}
        Knowledge.validate_body(kind, at_limit)
        with pytest.raises(Fault):
            Knowledge.validate_body(kind, {**body, "acceptance": [f"AC-{index}" for index in range(201)]})
        with pytest.raises(Fault):
            Knowledge.validate_body(kind, {**body, "acceptance": ["x" * 4097]})
        with pytest.raises(Fault):
            Knowledge.validate_body(kind, {**body, "acceptance": ["AC-1", "AC-1"]})
    elif kind == "domain":
        at_limit = {**body, "owned_data": [str(index) for index in range(10000)]}
        Knowledge.validate_body(kind, at_limit)
        with pytest.raises(Fault):
            Knowledge.validate_body(kind, {**body, "owned_data": [str(index) for index in range(10001)]})
    elif kind == "interface":
        typed = {**body, "input": {"format": "daikibo.type.v1", "schema": {"type": "string"}},
                 "output": {"format": "daikibo.type.v1", "schema": {"type": "string"}}}
        Knowledge.validate_body(kind, typed)
        with pytest.raises(Fault):
            Knowledge.validate_body(kind, {**typed, "input": {"format": "daikibo.type.v1", "schema": {"type": "string"}, "extra": True}})
    else:
        with pytest.raises(Fault):
            Knowledge.validate_body(kind, {**body, "title": "x" * 401})


def test_supervisor_prompt_guides_fixture_proposal_and_surfaces_missing_statement(full, tmp_path):
    project = full.k.create_project(full.owner, "metadata guided supervisor")["id"]
    script = tmp_path / "metadata_planner.py"
    capture = tmp_path / "prompt.json"
    script.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "prompt=json.load(sys.stdin)\n"
        "Path(sys.argv[1]).write_text(json.dumps(prompt))\n"
        "metadata=prompt['contract_metadata']['artifact.propose']\n"
        "required=metadata['kinds']['finding']['required']\n"
        "body={}\n"
        "for field in required: body[field]='guided-'+field\n"
        "print(json.dumps({'message':'metadata guided fixture','actions':[{'method':'artifact.propose','params':{'project':prompt['context']['project'],'kind':'finding','body':body}}],'questions':[]}))\n"
    )
    full.rt.adapters.register(full.owner, "metadata-planner", "fixture", sys.executable, [str(script), str(capture)])
    result = full.supervisor.turn(full.owner, project, "metadata-planner")
    assert result["actions"][0]["method"] == "artifact.propose"
    assert result["actions"][0]["result"]["body"]["statement"] == "guided-statement"

    prompt = json.loads(capture.read_text())
    assert isinstance(prompt["contracts"]["artifact.propose"], str)
    assert isinstance(prompt["contracts"]["artifact.revise"], str)
    assert prompt["contract_metadata"]["artifact.propose"] == artifact_body_contract()
    assert prompt["contract_metadata"]["artifact.revise"] == artifact_body_contract()

    missing_script = tmp_path / "missing_statement_planner.py"
    missing_script.write_text(
        "import json,sys\n"
        "prompt=json.load(sys.stdin)\n"
        "print(json.dumps({'message':'missing field fixture','actions':[{'method':'artifact.propose','params':{'project':prompt['context']['project'],'kind':'finding','body':{'title':'missing statement'}}}],'questions':[]}))\n"
    )
    full.rt.adapters.register(full.owner, "missing-statement-planner", "fixture", sys.executable, [str(missing_script)])
    rejected = full.supervisor.turn(full.owner, project, "missing-statement-planner")
    error = rejected["actions"][0]["error"]
    assert error["code"] == "invalid_input"
    assert "statement" in error["message"]
