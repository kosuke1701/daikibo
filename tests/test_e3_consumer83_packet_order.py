import copy,json,sys
from pathlib import Path
import pytest
from daikibo.common import Fault,digest
from daikibo.assurance_criteria import build_relation_request,evaluate_criteria,_set_denominator_alignment
from daikibo.assurance_denominators import collect_stage_context,derive_denominator
from daikibo.assurance_node_reviews import build_node_requests,select_node_reviews
from daikibo.assurance_relations import REGISTRY_DIGEST
from test_e3_consumer_mr import _artifact_ref,_requirements,_scope
from test_e3_unit2a_denominators import _fixture

@pytest.fixture
def case(full,tmp_path):
 f=_fixture(full,tmp_path)
 from test_e3_unit2c_extractors import _valid_breakdown
 f['breakdown']=_valid_breakdown(full,f,'BREAKDOWN-independent-full')
 context=collect_stage_context(full,full.owner,project=f['project'],program=f['program'],stage='plan',proposed_breakdown=f['breakdown'])
 den=derive_denominator(context)
 scope=_scope(full,f['project'],f['parent'])
 return f,context,den,scope

def test_center_owner_population(full,case):
 f,context,den,scope=case
 request=build_relation_request(full,full.owner,context=context,denominator=den,relation='assigned_to',center_ref=_artifact_ref(f['project'],f['parent']),direction='outgoing',scope_ref=scope['scope_ref'],registry_digest=REGISTRY_DIGEST)
 required=[o for o in den['obligations'] if o['id'] in request['required_obligation_ids']]
 assert required
 assert all(o['source_ref'].get('artifact')==f['parent']['id'] for o in required),required

def test_set_alignment_cannot_ignore_stored_population(full,case):
 f,context,den,scope=case
 obs={x['id']:x for x in den['obligations']}
 required=[x['id'] for x in den['obligations'] if x['category']=='acceptance_condition' and x['source_ref']['locator']['artifact']==f['parent']['id']]
 empty=full.assurance.store_object(full.owner,f['project'],'obligations','independent-empty-obligations',1,{'format':'assurance.obligations.v1','project':f['project'],'obligations':[]})
 aligned,reason=_set_denominator_alignment(full,f['project'],{'scope_ref':scope['scope_ref'],'expected_obligations_ref':full.assurance._object_ref(empty)},obs,required)
 assert aligned is False,reason

def test_actual_runtime_relation_reviews(full,case,tmp_path):
 from daikibo.assurance_criteria import build_review_assurance
 f,context,den,unused=case;p=f['project']
 design=full.k.propose(full.owner,p,'design',{'title':'Design','statement':'Realize both requirements','source_refs':[f['source']['id']]})
 design=full.k.accept(full.owner,design['id'],1)
 roots=[_artifact_ref(p,x) for x in (f['parent'],f['child'],design)]
 scope=full.assurance.scope_propose(full.owner,p,{'roots':roots,'selection_rules':{},'exclusion_proposals':[],'authority_refs':[],'discovery_unknowns':[]})
 profile=full.assurance.profile_propose(full.owner,p,None,{'scope_ref':scope['scope_ref'],'stage_rules':{'plan':{}},'relation_selectors':['realizes'],'test_definition_bindings':[]})
 script=tmp_path/'review.py'
 script.write_text("import json,sys\np=json.load(sys.stdin);c=p.get('context',{})\nprint(json.dumps({'verdict':'pass','rationale':'actual subprocess fixture, not semantic LLM','covered':c.get('required_coverage',[]),'findings':[],'observations':[{'ref':p['subject'],'detail':'fixture read context'}],'dispositions':[]}))\n")
 full.rt.adapters.register(full.owner,'mr-markers','fixture',sys.executable,[str(script)])
 def review_adopt(root):
  refs=[]
  for packet,role in full.assurance._review_requirements(p,full.assurance._adoption_roots(p,root)):
   ev=full.rt.review(full.owner,packet['id'],role,'mr-markers')
   refs.append({'packet':packet['id'],'role':role,'id':ev['receipt']})
  full.assurance.adopt(full.owner,p,root['id'],root['digest'],None,refs)
 review_adopt(profile['profile'])
 edges=[]
 for req in (f['parent'],f['child']):
  obs=[x['id'] for x in scope['obligations']['body']['obligations'] if x['source_ref'].get('artifact')==req['id']]
  edge=full.assurance.edge_propose(full.owner,p,{'source_ref':_artifact_ref(p,design),'target_ref':_artifact_ref(p,req),'relation':'realizes','scope_ref':profile['profile_ref'],'claim':'Actual design relation','obligation_ids':obs,'required_evidence_refs':[],'authority_refs':[]})
  review_adopt(edge['edge']);edges.append(edge['edge'])
 aset=full.assurance.set_propose(full.owner,p,{'center_ref':_artifact_ref(p,design),'relation':'realizes','direction':'outgoing','scope_ref':profile['profile_ref'],'criteria':{},'required_evidence_refs':[]})
 review_adopt(aset['set'])
 request=build_relation_request(full,full.owner,context=context,denominator=den,relation='realizes',center_ref=_artifact_ref(p,design),direction='outgoing',scope_ref=profile['profile_ref'],registry_digest=REGISTRY_DIGEST)
 rr=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(aset['set']))
 assert rr['status']=='satisfied',dict(rr)
 assert rr['synthesis_review']['status']=='satisfied'
 assert rr['independent_runs']['independent'] is True
 # The actual result is retained for a second negative in this same real flow.
 packet=next(x for x in full.assurance._root_packets(p,aset['set']) if x['body'].get('children'))
 script.write_text(script.read_text().replace("'verdict':'pass'","'verdict':'fail'"))
 full.rt.review(full.owner,packet['id'],packet['body']['required_roles'][0],'mr-markers')
 newer=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(aset['set']))
 assert newer['status']!='satisfied',dict(newer)
