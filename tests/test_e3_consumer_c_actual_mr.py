from __future__ import annotations

import pytest

from daikibo.assurance import SET_UNIVERSAL_CRITERIA
from daikibo.assurance_criteria import build_relation_request, build_review_assurance, evaluate_criteria
from daikibo.assurance_relations import REGISTRY_V2_DIGEST, registry_entry

from test_consumer_p_mr_integration import _actual_review_adapter
from test_e3_consumer_c_integration import _case, _reviews


def _requirements(relation):
    return sorted(SET_UNIVERSAL_CRITERIA | set(
        registry_entry(relation, contract_digest=REGISTRY_V2_DIGEST)["set_checks"]
    ))


@pytest.mark.parametrize("relation", ["produced_by", "contains"])
def test_c_v2_actual_delivery_edge_set_runtime_reviews_close(full, full_project, tmp_path, relation):
    case = _case(full, full_project, tmp_path)
    direction = "outgoing" if relation == "produced_by" else "incoming"
    request = build_relation_request(
        full, full.owner, context=case["context"], denominator=case["denominator"],
        relation=relation, center_ref=case["output_ref"], direction=direction,
        scope_ref=case["scope"], registry_digest=REGISTRY_V2_DIGEST,
    )
    review_adopt = _actual_review_adapter(full, tmp_path)
    edge = full.assurance.edge_propose(
        full.owner, case["project"], {
            "source_ref": case["output_ref"] if relation == "produced_by" else case["snapshot"],
            "target_ref": case["producer"] if relation == "produced_by" else case["output_ref"],
            "relation": relation, "relation_contract_digest": REGISTRY_V2_DIGEST,
            "scope_ref": case["scope"], "claim": "Actual pinned Delivery output and producer",
            # The controller resolver owns declaration coverage; an empty
            # claim list is the normal v2 path and cannot self-assert IDs.
            "obligation_ids": [], "required_evidence_refs": [], "authority_refs": [],
        },
    )
    review_adopt(case["project"], edge["edge"])
    relation_set = full.assurance.set_propose(
        full.owner, case["project"], {
            "center_ref": case["output_ref"], "relation": relation,
            "direction": direction, "relation_contract_digest": REGISTRY_V2_DIGEST,
            "scope_ref": case["scope"], "criteria": {}, "required_evidence_refs": [],
        },
    )
    obligations = relation_set["obligations"]["body"]
    assert obligations["consumer_binding"]["relation_contract_digest"] == REGISTRY_V2_DIGEST
    assert [item["id"] for item in obligations["obligations"]] == request["required_obligation_ids"]
    review_adopt(case["project"], relation_set["set"])
    relation_reviews = build_review_assurance(
        full, full.owner, relation_request=request,
        set_ref=full.assurance._object_ref(relation_set["set"]),
    )
    result = evaluate_criteria(
        relation=relation, requirements=_requirements(relation),
        denominator=case["denominator"], edges=[edge["edge"]],
        validated_reviews=_reviews(full, case), relation_request=request,
        relation_reviews=relation_reviews,
    )
    assert result["status"] == "satisfied", result
    assert result["criteria"]["all_obligations_covered"]["status"] == "satisfied"
