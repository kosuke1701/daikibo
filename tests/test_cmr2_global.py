import pytest
from cmr2_success_fixture import _case,_requirements,_reviews
from test_consumer_p_mr_integration import _actual_review_adapter
from daikibo.assurance_criteria import build_relation_request,build_review_assurance,evaluate_criteria
from daikibo.assurance_relations import REGISTRY_V2_DIGEST

@pytest.mark.parametrize('relation',['produced_by','contains'])
def test_global_actual_two_outputs(full,full_project,tmp_path,relation):
 c=_case(full,full_project,tmp_path);adopt=_actual_review_adapter(full,tmp_path)
 receipt=next(x['receipt'] for x in c['verification']['results'] if x['check']=='build-b')
 b=full.assurance.pin(full.owner,c['project'],{'kind':'output_artifact','delivery':c['snapshot'],'check_id':'build-b','receipt':receipt,'output_id':'b'})['ref']
 request=build_relation_request(full,full.owner,context=c['context'],denominator=c['denominator'],relation=relation,center_ref=c['snapshot'],direction='outgoing',scope_ref=c['scope'],registry_digest=REGISTRY_V2_DIGEST)
 assert len(request['required_obligation_ids'])==2
 edges=[]
 for output in [c['output_ref'],b]:
  producer=next(x['producer_ref'] for x in c['context']['delivery_material']['declared_outputs']['items'] if x['definition']['id']==output['output_id'])
  edge=full.assurance.edge_propose(full.owner,c['project'],{'source_ref':output if relation=='produced_by' else c['snapshot'],'target_ref':producer if relation=='produced_by' else output,'relation':relation,'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':c['scope'],'claim':'Actual output declaration','obligation_ids':[],'required_evidence_refs':[],'authority_refs':[]})
  adopt(c['project'],edge['edge']);edges.append(edge['edge'])
 s=full.assurance.set_propose(full.owner,c['project'],{'center_ref':c['snapshot'],'relation':relation,'direction':'outgoing','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':c['scope'],'criteria':{},'required_evidence_refs':[]})
 assert len(s['obligations']['body']['obligations'])==2
 adopt(c['project'],s['set'])
 reviews=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(s['set']))
 def evaluate(es):return evaluate_criteria(relation=relation,requirements=_requirements(relation),denominator=c['denominator'],edges=es,validated_reviews=_reviews(full,c),relation_request=request,relation_reviews=reviews)
 value=evaluate(edges);assert value['status']=='satisfied',value
 assert evaluate(edges[:1])['status']!='satisfied'
