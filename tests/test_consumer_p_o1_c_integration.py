"""One real Control session covering the O1/P/C component boundaries."""
from __future__ import annotations

import json

from conftest import finish_task, make_task
from test_consumer_p_artifact_provenance import _run_collect
from test_unit2c_output_adapter import output_profile


def test_same_control_runtime_collect_and_delivery_output_pin(full, full_project, tmp_path):
    """Runtime capture and both downstream immutable material paths share Control."""
    project, repository, requirement, _root = full_project

    # Consumer-P reads the producer actor/run/receipt and candidate snapshot
    # written by this very Control's Runtime, rather than a hand-built row.
    _project, _repo, _p_task, _executed, collected = _run_collect(full, full_project)
    assert collected["artifacts"][0]["artifact"]["status"] == "draft"

    # The same Runtime and Assurance instances now execute the Delivery
    # producer/consumer checks and pin their exact non-Git output.
    # Delivery has its own protected task set, so use a second project while
    # retaining the same Control/Runtime/Assurance instances.  This keeps the
    # positive boundary test honest without pretending the unfinished P task
    # is a completed Delivery task.
    delivery_project = full.k.create_project(full.owner, "Same Control Delivery")["id"]
    delivery_root = tmp_path / "delivery-repo"
    delivery_root.mkdir()
    (delivery_root / "calc.py").write_text("def add(a,b):\n    return a-b\n")
    (delivery_root / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(2,3) == 5\n"
    )
    delivery_repository = full.sn.register(
        full.owner, delivery_project, "app", str(delivery_root)
    )["id"]
    delivery_source = full.k.source(
        full.owner, delivery_project, "Addition returns the arithmetic sum."
    )
    delivery_requirement = full.k.propose(
        full.owner, delivery_project, "requirement",
        {"title": "Addition", "statement": "Returns arithmetic sum",
         "acceptance": ["AC-ADD"], "source_refs": [delivery_source["id"]]},
    )
    full.k.accept(full.owner, delivery_requirement["id"], 1)
    full.k.classify(full.owner, delivery_source["id"], 0,
                    delivery_source["characters"], "requirement",
                    [delivery_requirement["id"]], "Original source")
    delivery_fixture = (delivery_project, delivery_repository,
                        delivery_requirement["id"], delivery_root)
    delivery_task = make_task(full, delivery_fixture)
    full.d.configure(full.owner, delivery_project,
                     output_profile(delivery_project, delivery_repository,
                                    delivery_requirement["id"], delivery_task))
    finish_task(full, delivery_project, delivery_task)
    delivery = full.d.prepare(full.owner, delivery_project)["id"]
    verified = full.d.verify(full.owner, delivery)
    assert all(item["passed"] for item in verified["results"])

    _row, body = full.d.current(delivery)
    producer_receipt = next(item["receipt"] for item in body["results"]
                            if item["check"] == "producer")
    run = full.s.one("SELECT * FROM runs WHERE id=?",
                     (full.g.receipt(producer_receipt)["run"],), True)
    snapshot_ref = json.loads(run["body"])["verification_material"]
    material = full.assurance.object_get(full.owner, delivery_project, snapshot_ref["id"])
    payload = json.loads(full.s.blob_get(material["body"]["payload_blob"]))
    ref = full.assurance.pin(full.owner, delivery_project, {
        "kind": "output_artifact", "delivery": payload["definition_ref"]["delivery"],
        "check_id": "producer", "receipt": producer_receipt, "output_id": "artifact",
    })["ref"]
    resolved = full.assurance.resolve_pinned(full.owner, ref)
    assert resolved["resolution"]["content"]["id"] == "artifact"
    assert resolved["resolution"]["membership"][0]["relation"] == "delivery_build_output"
