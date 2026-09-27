import pytest
from test_consumer_p_mr_integration import _build_actual_flow
from daikibo.assurance_relations import REGISTRY_V2_DIGEST

def test_v2_retains_declared_task_producer_center(full,full_project,tmp_path):
 f=_build_actual_flow(full,full_project,tmp_path)
 result=full.assurance.set_propose(full.owner,f['project'],{'center_ref':f['task_ref'],'relation':'produced_by','direction':'incoming','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],'criteria':{},'required_evidence_refs':[]})
 assert result['set']['body']['relation_contract_digest']==REGISTRY_V2_DIGEST
