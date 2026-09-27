"""Partial 21-row v4 matrix (WIP/HOLD); D1 deliberately exposes the blocker.

Protocol fixtures never establish semantic/LLM acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from daikibo.assurance import (
    PROFILE_V4_FORMAT, _validate_profile_v2_wire, _validate_profile_v3_wire,
    _validate_profile_v4_wire, validate_assurance_rows, SET_UNIVERSAL_CRITERIA,
)
from daikibo.assurance_profile_contract import profile_registry, profile_has_outputs
from daikibo.assurance_relations import REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST, registry_entry
from daikibo.assurance_stage import _relation_center_refs
from daikibo.assurance_denominators import collect_stage_context, derive_denominator, project_task
from daikibo.assurance_criteria import build_relation_request, build_review_assurance, evaluate_criteria, _obligation_owner_refs
from daikibo.assurance_node_reviews import build_node_requests, select_node_reviews
from daikibo.common import Fault, canonical, digest
from test_e3_selection_contract import _fixture, _profile_body, _adopt, _register_fixture_review, _source_ref, _review_refs
from unit4p_domain_fixture import canonical_flow, aref, tref, accept, reviews_adopt, populate_realizes, git_commit, git_file_ref


def _body(p, program, scope, version=4):
    body = json.loads(json.dumps(_profile_body(p, program, scope)))
    body['format'] = f'assurance.profile.v{version}'
    if version >= 3:
        body['required_relation_contract_digest'] = REGISTRY_V2_DIGEST
    for stage in ('plan', 'task'):
        body['stage_rules'][stage]['relation_sets'][0]['centers'] = [
            'realization_sources' if version >= 4 else 'design_artifacts']
    if version == 5:
        body.update(required_scope_contract="assurance.scope.v2", required_node_contract="assurance.node-contract.v2")
    return body


def _snapshot(c):
    """All logical DB rows and CAS bytes, including config/receipts/packets."""
    tables = [x['name'] for x in c.s.all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    rows = {t: c.s.all('SELECT * FROM "'+t+'" ORDER BY rowid') for t in tables}
    # The Store path is deliberately not inferred from a live home.
    blobs = {str(p.relative_to(c.s.home)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (c.s.home / 'blobs').rglob('*') if p.is_file()}
    return digest({'rows':rows, 'blobs':blobs})


def _selected(full, *, version=4, domain_scope=False, domains=1, designs=1, implements=False):
    p, source, req, program, initial_scope = _fixture(full)
    ds = [accept(full,p,'domain','same label',source_refs=[source['id']],
                 responsibilities=['first duty','second duty'],non_responsibilities=[],owned_data=[],interfaces=[])
          for _ in range(domains)]
    designs_ = [accept(full,p,kind,'same label',source_refs=[source['id']],
                        **({'input':'two values','output':'result','authentication':'none','errors':'none',
                            'idempotency':'pure','compatibility':'stable','consumers':[],'verification':'test'}
                           if kind=='interface' else {}))
                for kind in ('design','component','interface') for _ in range(designs)]
    scope = initial_scope
    if domain_scope or version == 5:
        scope = full.assurance.scope_propose(full.owner,p,{
            **({'format':'assurance.scope.v2'} if version == 5 else {}),
            'roots':[aref(full,p,x['id']) for x in ds], 'selection_rules':{},
            'exclusion_proposals':[], 'authority_refs':[], 'discovery_unknowns':[]})
    body = _body(p,program,scope,version)
    if implements:
        body['relation_selectors'] = ['implements','realizes']
        for stage in ('plan','task'):
            body['stage_rules'][stage]['relation_sets']=[{'relation':'implements','direction':'incoming','centers':['design_artifacts']}]
    if version == 5:
        body['node_review_rules'] = [{'id':'domain', 'selector':'domain', 'roles':['domain_responsibility']}]
        for stage in body['stage_rules'].values(): stage['node_rules'] = ['domain']
    _register_fixture_review(full)
    prof=full.assurance.profile_propose(full.owner,p,program,body,None)
    _adopt(full,p,prof,None)
    return {'project':p,'program':program,'source':source,'req':req,'domains':ds,
            'designs':designs_,'scope':scope,'profile':prof,'body':body}


def _context(c,f,stage='plan',**kwargs):
    context=collect_stage_context(c,c.owner,project=f['project'],program=f['program'],stage=stage,**kwargs)
    return context,derive_denominator(context)


def _request(c,f,context,den,center,relation='realizes',direction='outgoing',**kwargs):
    return build_relation_request(c,c.owner,context=context,denominator=den,relation=relation,
        center_ref=center,direction=direction,scope_ref=f['profile']['profile_ref'],registry_digest=REGISTRY_V2_DIGEST,**kwargs)


def _criteria(c,den,request,edges,aset=None):
    nodes=select_node_reviews(c,c.owner,node_requests=build_node_requests(c,c.owner,project=request['project'],selectors=[]))
    rr=None if aset is None else build_review_assurance(c,c.owner,relation_request=request,set_ref=c.assurance._object_ref(aset))
    reqs=sorted(SET_UNIVERSAL_CRITERIA|set(registry_entry(request['relation'],contract_digest=REGISTRY_V2_DIGEST)['set_checks']))
    return evaluate_criteria(relation=request['relation'],requirements=reqs,denominator=den,
                             edges=edges,validated_reviews=nodes,relation_request=request,relation_reviews=rr)


def test_W1_legacy_wire_catalog_and_registry_are_frozen(full):
    p,_,_,program,scope=_fixture(full)
    for version,validator in ((2,_validate_profile_v2_wire),(3,_validate_profile_v3_wire)):
        body=_body(p,program,scope,version)
        before=canonical(body)
        validator(body)
        assert canonical(body)==before
    assert REGISTRY_V1_DIGEST=='a19aec07b902481d9a243e61f8d17dfa6153fc74ce69c09d494a67c21b3143a6'
    assert REGISTRY_V2_DIGEST=='140c22498e28df3606f8981f28a818f20f0101c41471ca98b2f1a9f1b02610ac'
    cat=full.assurance.catalog(full.owner)
    assert 'realization_sources' not in canonical(cat['profile']).decode()
    assert 'realization_sources' not in canonical(cat['profile_v3']).decode()
    assert cat['profile_v4']['selector_constraints']['realization_sources']['artifact_kinds']==['component','design','interface']


@pytest.mark.parametrize('mutation',['ok','digest','format','extra','null','list'])
def test_W2_v4_closed_wire(full,mutation):
    p,_,_,program,scope=_fixture(full)
    body=_body(p,program,scope)
    if mutation=='ok':
        assert full.assurance.profile_propose(full.owner,p,program,body,None)['profile']['body']==body
        return
    if mutation=='digest':body['required_relation_contract_digest']=REGISTRY_V1_DIGEST
    elif mutation=='format':body['format']='assurance.profile.v99'
    elif mutation=='extra':body['extra']=True
    else:body['format']=None if mutation=='null' else []
    with pytest.raises(Fault): full.assurance.profile_propose(full.owner,p,program,body,None)


@pytest.mark.parametrize('version,stage,relation,direction',[
    (2,'plan','realizes','outgoing'),(3,'plan','realizes','outgoing'),
    (4,'plan','implements','outgoing'),(4,'plan','realizes','incoming'),
    (4,'integration','realizes','outgoing'),(4,'delivery','realizes','outgoing')])
def test_W3_selector_has_exact_version_and_use(full,version,stage,relation,direction):
    p,_,_,program,scope=_fixture(full);body=_body(p,program,scope,version)
    body['stage_rules'][stage]['relation_sets']=[{'relation':relation,'direction':direction,'centers':['realization_sources']}]
    with pytest.raises(Fault):full.assurance.profile_propose(full.owner,p,program,body,None)


def test_P1_complete_population_order_identity_and_denominator(full):
    f=_selected(full,domains=2,designs=2);ctx,den=_context(full,f)
    selected=_relation_center_refs(ctx,'realization_sources',None,profile_format=PROFILE_V4_FORMAT)
    assert len(selected)==6
    assert {r['artifact'] for r in selected}=={x['id'] for x in f['designs']}
    legacy=_relation_center_refs(ctx,'design_artifacts',None)
    assert len(legacy)==8
    assert _relation_center_refs({'artifacts':list(reversed(ctx['artifacts']))},'realization_sources',None,profile_format=PROFILE_V4_FORMAT)==selected
    assert len([o for o in den['obligations'] if o['category']=='artifact_responsibility'])==4
    assert all(_obligation_owner_refs(o,None)==[o['source_ref']] for o in den['obligations'] if o['category']=='artifact_responsibility')
    with pytest.raises(Fault):_relation_center_refs(ctx,'realization_sources',None)


def test_P2_domain_only_has_missing_center(full):
    f=_selected(full,designs=0)
    r=full.assurance.evaluate_stage(full.owner,f['project'],f['program'],'plan')
    assert r['status']!='satisfied'
    assert 'relation_center_missing' in canonical(r).decode()


def test_P3_domain_responsibility_revision_preserves_all_entries(full):
    f=_selected(full);ctx,den=_context(full,f);d=f['domains'][0]
    old=[o for o in den['obligations'] if o['category']=='artifact_responsibility']
    body=dict(d['body']);body['responsibilities'].append('third duty')
    # A supplemental declaration cannot replace the canonical domain array.
    body['structural_obligations']={'format':'daikibo.structural-obligations.v1','responsibilities':[]}
    _change_artifact(full,f,d,body)
    ctx2,den2=_context(full,f)
    current=[o for o in den2['obligations'] if o['category']=='artifact_responsibility']
    assert len(old)==2 and len(current)==3
    assert {o['pointer'] for o in current}=={'/responsibilities/0','/responsibilities/1','/responsibilities/2'}
    assert den['input_digest']!=den2['input_digest']


def test_A1_D3_R2_canonical_activation_and_read_only_population(tmp_path):
    f=canonical_flow(tmp_path/'canonical');c=f['control']
    try:
        ctx,den=_context(c,f,proposed_breakdown=f['breakdown'])
        q=[o for o in den['obligations'] if o['category']=='artifact_responsibility']
        assert len(q)==1 and q[0]['source_ref']['artifact']==f['domain']
        before=_snapshot(c)
        results=[c.assurance.evaluate_stage(c.owner,f['project'],f['program'],'plan',proposed_breakdown=f['breakdown']) for _ in range(2)]
        assert _snapshot(c)==before
        assert all(r['status']=='satisfied' for r in results)
        assert results[0]['semantic_fingerprint']==results[1]['semantic_fingerprint']
        requests=results[0]['relations']['items']
        assert len(requests)==2
        assert all(len(x['request']['required_obligation_ids'])==2 for x in requests)
        assert 'implements' not in f['profile_body']['relation_selectors']
        result=c.breakdowns.activate(c.owner,f['breakdown'])
        assert result['status']=='active' and result['plan_gate']['allowed']
        assert c.breakdowns.active(c.owner,f['program'])['id']==f['breakdown']
    finally:c.close()


@pytest.mark.parametrize('registry',[REGISTRY_V1_DIGEST,REGISTRY_V2_DIGEST])
def test_E1_domain_realizes_remains_invalid(full,registry):
    f=_selected(full);p=f['project'];body={'source_ref':aref(full,p,f['domains'][0]['id']),
        'target_ref':aref(full,p,f['req']['id']),'relation':'realizes','scope_ref':f['profile']['profile_ref'],
        'relation_contract_digest':registry,'claim':'invalid domain realizes','obligation_ids':[],
        'required_evidence_refs':[],'authority_refs':[]}
    with pytest.raises(Fault) as err:full.assurance.edge_propose(full.owner,p,body)
    assert err.value.code=='invalid_relation_endpoint'


def _change_artifact(c,f,artifact,body):
    p=f['project'];src=f['source']['id']
    change=c.p.change(c.owner,p,{'title':'Reviewed revision','origin':'user','reason':'update declared contract',
        'source':src,'affected':[artifact['id']],'evidence':[src],
        'deltas':[{'artifact':artifact['id'],'expected_revision':artifact['revision'],'body':body}]})
    decision=c.p.propose_decision(c.owner,p,{'title':'Approve revision','reason':'source grounded',
        'options':['approve','keep_existing'],'recommendation':'approve','refs':[artifact['id']],
        'requirement_affecting':True,'change':change['id']})
    c.p.respond(c.owner,decision['id'],decision['digest'],'approve','Approve explicit revision')
    review=c.rt.review(c.owner,decision['id'],'consistency','e3-assurance-fixture')
    c.p.apply_decision(c.owner,decision['id'],review['receipt'])


def _replacement(c,f,*,authority=True):
    selected=c.assurance.selected_profile(c.owner,f['project'],f['program'])
    body=copy.deepcopy(f.get('body',f.get('profile_body')))
    body['format']=PROFILE_V4_FORMAT
    body['previous_selection_ref']=selected['profile_ref']
    body['change_reason']='Separate realization sources from domain responsibility without altering per-source AC coverage'
    for stage in ('plan','task'):
        for rule in body['stage_rules'][stage]['relation_sets']:
            if rule['relation']=='realizes' and rule['direction']=='outgoing':
                rule['centers']=['realization_sources' if x=='design_artifacts' else x for x in rule['centers']]
    if authority:
        src=c.k.source(c.owner,f['project'],body['change_reason'])
        c.k.classify(c.owner,src['id'],0,src['characters'],'reference',[],
                     'Profile selector transition rationale; no new product requirement')
        partition=c.traceability.propose(c.owner,f['project'],kind='document',scope={'source':src['id']})
        c.traceability.extract(c.owner,partition['id'])
        body['authority_refs']=sorted([*body['authority_refs'],_source_ref(f['project'],src)],key=canonical)
    return body,selected


def test_M1_empty_authority_v3_migrates_by_review_and_new_proofs(tmp_path):
    f=canonical_flow(tmp_path/'migration',profile_format='assurance.profile.v3');c=f['control'];p=f['project']
    try:
        old_rows=c.assurance.archive_rows(p)
        old_body=canonical(f['profile']['profile']['body'])
        assert f['profile_body']['authority_refs']==[]
        before=_snapshot(c)
        with pytest.raises(Fault) as err:c.breakdowns.activate(c.owner,f['breakdown'])
        assert err.value.code=='breakdown_gate_denied'
        assert _snapshot(c)==before
        body,selection=_replacement(c,f)
        for authority in ([],[{**body['authority_refs'][0],'source':'SRC-unresolved'}],
                          [{**body['authority_refs'][0],'project':'PRJ-foreign'}]):
            invalid=copy.deepcopy(body);invalid['authority_refs']=authority
            before=_snapshot(c)
            with pytest.raises(Fault):c.assurance.profile_propose(c.owner,p,f['program'],invalid,selection['head_event'])
            assert _snapshot(c)==before
        proposal=c.assurance.profile_propose(c.owner,p,f['program'],body,selection['head_event'])
        reviews_adopt(c,p,proposal['profile'],'finite',selection['head_event'])
        assert c.assurance.object_get(c.owner,p,f['profile']['profile']['id'])['body']==json.loads(old_body)
        f['profile']=proposal
        r=c.assurance.evaluate_stage(c.owner,p,f['program'],'plan',proposed_breakdown=f['breakdown'])
        assert r['status']!='satisfied' and 'relation_set_missing' in canonical(r).decode()
        populate_realizes(c,p,f['realize_sources'],f['requirements'],f['scope'],proposal)
        r=c.assurance.evaluate_stage(c.owner,p,f['program'],'plan',proposed_breakdown=f['breakdown'])
        assert r['status']=='satisfied', json.dumps(r['failures'],indent=2)
        assert c.breakdowns.activate(c.owner,f['breakdown'])['status']=='active'
        # Every old immutable object and event remains present byte for byte.
        current=c.assurance.archive_rows(p)
        for key,rows in old_rows.items():
            if key in {'assurance_heads'}:continue
            assert all(row in current[key] for row in rows)
    finally:c.close()


def test_S2_concurrent_cas_rejects_adoption_without_mutation(full):
    f=_selected(full,version=3);p=f['project'];body,selection=_replacement(full,f)
    proposal=full.assurance.profile_propose(full.owner,p,f['program'],body,selection['head_event'])
    other=copy.deepcopy(body);other['change_reason']='competing explicit transition'
    competitor=full.assurance.profile_propose(full.owner,p,f['program'],other,selection['head_event'])
    refs=_review_refs(full,p,competitor['profile'])
    _adopt(full,p,proposal,selection['head_event'])
    before=_snapshot(full)
    with pytest.raises(Fault) as err:
        full.assurance.adopt(full.owner,p,competitor['profile']['id'],competitor['profile']['digest'],selection['head_event'],refs)
    assert err.value.code=='stale_head'
    assert _snapshot(full)==before
    assert full.assurance.resolve_pinned(full.owner,f['profile']['profile_ref'])


def _domain_proof(full,f,tmp_path):
    p=f['project'];domain=f['domains'][0]
    root=tmp_path/'implementation';root.mkdir();(root/'impl.py').write_text('def duties():\n    return ("first duty", "second duty")\n')
    commit=git_commit(root);repo=full.sn.register(full.owner,p,'fixed implementation',str(root))['id']
    pop=full.traceability.propose(full.owner,p,kind='code',scope={'repository':repo,'commit':commit})
    rev=full.traceability.extract(full.owner,pop['id'])['revision']
    source={'kind':'traceability_ref','project':p,'locator':git_file_ref(full,p,rev,repo,'impl.py')}
    edge=full.assurance.edge_propose(full.owner,p,{'source_ref':source,'target_ref':aref(full,p,domain['id']),
        'relation':'implements','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],
        'claim':'The pinned implementation fulfills every declared domain responsibility',
        'obligation_ids':[x['id'] for x in f['scope']['obligations']['body']['obligations']],
        'required_evidence_refs':[],'authority_refs':[]})
    reviews_adopt(full,p,edge['edge'],'e3-assurance-fixture')
    aset=full.assurance.set_propose(full.owner,p,{'center_ref':aref(full,p,domain['id']),'relation':'implements',
        'direction':'incoming','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],
        'criteria':{},'required_evidence_refs':[]})
    reviews_adopt(full,p,aset['set'],'e3-assurance-fixture')
    return edge['edge'],aset['set']


def test_legacy_v4_cannot_claim_domain_responsibility_proof(full,tmp_path):
    f=_selected(full,domain_scope=True,designs=0,implements=True)
    edge,aset=_domain_proof(full,f,tmp_path)
    ctx,den=_context(full,f)
    request=_request(full,f,ctx,den,aref(full,f['project'],f['domains'][0]['id']),'implements','incoming')
    q=[o for o in den['obligations'] if o['category']=='artifact_responsibility']
    assert set(request['required_obligation_ids'])=={o['id'] for o in q}
    assert len(q)==2 and all(o['contributors']==[] for o in q)
    good=_criteria(full,den,request,[edge],aset)
    rr=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(aset))
    with pytest.raises(Fault) as unsupported_node:
        build_node_requests(full,full.owner,project=f['project'],selectors=[{
            'selector':'design','node_ref':aref(full,f['project'],f['domains'][0]['id'])}])
    print(json.dumps({'criteria_status':good['status'],
        'criteria':{k:v['status'] for k,v in good['criteria'].items()},
        'synthesis':rr['synthesis_review'], 'node_code':unsupported_node.value.code,
        'required_ids':request['required_obligation_ids'],
        'saved_obligations':full.assurance._object_by_ref(aset['body']['expected_obligations_ref'],f['project'],kinds={'obligations'})['body']['obligations']},sort_keys=True))
    assert good['status']!='satisfied', 'Legacy v4 cannot reinterpret empty_scope as responsibility Q'
    missing=_criteria(full,den,request,[],aset)
    assert missing['criteria']['all_responsibilities']['status']!='satisfied'
    assert set(missing['criteria']['all_responsibilities']['missing_ids'])=={o['id'] for o in q}
    no_review=_criteria(full,den,request,[edge])
    assert no_review['criteria']['meaning_review']['status']!='satisfied'
