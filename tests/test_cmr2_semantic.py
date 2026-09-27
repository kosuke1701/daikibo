import pytest
from test_e3_consumer_c_integration import _case,_requirements,_reviews
from test_consumer_p_mr_integration import _actual_review_adapter
from daikibo.assurance_criteria import build_relation_request,build_review_assurance,evaluate_criteria
from daikibo.assurance_relations import REGISTRY_V2_DIGEST

@pytest.mark.parametrize('relation',['produced_by','contains'])
def test_actual_c_same_control_meaning(full,full_project,tmp_path,relation):
 c=_case(full,full_project,tmp_path)
 direction='outgoing' if relation=='produced_by' else 'incoming'
 request=build_relation_request(full,full.owner,context=c['context'],denominator=c['denominator'],relation=relation,center_ref=c['output_ref'],direction=direction,scope_ref=c['scope'],registry_digest=REGISTRY_V2_DIGEST)
 adopt=_actual_review_adapter(full,tmp_path)
 edge=full.assurance.edge_propose(full.owner,c['project'],{'source_ref':c['output_ref'] if relation=='produced_by' else c['snapshot'],'target_ref':c['producer'] if relation=='produced_by' else c['output_ref'],'relation':relation,'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':c['scope'],'claim':'Actual pinned delivery output and producer','obligation_ids':[],'required_evidence_refs':[],'authority_refs':[]})
 adopt(c['project'],edge['edge'])
 s=full.assurance.set_propose(full.owner,c['project'],{'center_ref':c['output_ref'],'relation':relation,'direction':direction,'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':c['scope'],'criteria':{},'required_evidence_refs':[]})
 adopt(c['project'],s['set'])
 reviews=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(s['set']))
 value=evaluate_criteria(relation=relation,requirements=_requirements(relation),denominator=c['denominator'],edges=[edge['edge']],validated_reviews=_reviews(full,c),relation_request=request,relation_reviews=reviews)
 assert value['status']=='satisfied',value
 assert value['criteria']['meaning_review']['status']=='satisfied',value
 missing=evaluate_criteria(relation=relation,requirements=_requirements(relation),denominator=c['denominator'],edges=[edge['edge']],validated_reviews=_reviews(full,c),relation_request=request,relation_reviews=None)
 assert missing['status']!='satisfied',missing
 allset=full.assurance.set_propose(full.owner,c['project'],{'center_ref':c['snapshot'],'relation':relation,'direction':'outgoing','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':c['scope'],'criteria':{},'required_evidence_refs':[]})
 assert len(allset['obligations']['body']['obligations'])==2
