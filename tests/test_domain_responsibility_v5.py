"""Versioned DOMAIN writer and semantic node contract; finite observed fixtures."""
import copy
import json
import pytest
from daikibo.common import Fault, canonical, digest
from daikibo.assurance import SET_UNIVERSAL_CRITERIA
from daikibo.assurance_relations import REGISTRY_V2_DIGEST, registry_entry
from daikibo.assurance_node_reviews import build_node_requests, select_node_reviews
from daikibo.assurance_criteria import build_review_assurance, evaluate_criteria
from test_unit4p_domain_profile_v4 import _selected, _context, _request, _domain_proof, _snapshot
from unit4p_domain_fixture import aref


def domain_nodes(c,f):
    return build_node_requests(c,c.owner,project=f['project'],contract='assurance.node-contract.v2',
        selectors=[{'selector':'domain','node_ref':aref(c,f['project'],x['id'])} for x in f['domains']])


def test_Q1_R1_N1_D1_exact_saved_responsibilities_and_observed_node(full,tmp_path):
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    ctx,den=_context(full,f)
    saved=f['scope']['obligations']['body']['obligations']
    assert saved==sorted([x for x in den['obligations'] if x['category']=='artifact_responsibility'],key=lambda x:x['id'])
    assert len(saved)==2
    nodes=domain_nodes(full,f)
    assert len(nodes[0]['required_coverage'])==6
    missing=select_node_reviews(full,full.owner,node_requests=nodes)
    assert missing[0]['roles']['domain_responsibility']['status']=='unverified'
    review=full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    assert review['result']['verdict']=='pass'
    nodes=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
    assert nodes[0]['roles']['domain_responsibility']['status']=='satisfied', nodes
    edge,aset=_domain_proof(full,f,tmp_path)
    request=_request(full,f,ctx,den,aref(full,f['project'],f['domains'][0]['id']),'implements','incoming')
    rr=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(aset))
    result=evaluate_criteria(relation='implements', requirements=sorted(SET_UNIVERSAL_CRITERIA|set(registry_entry('implements',contract_digest=REGISTRY_V2_DIGEST)['set_checks'])),
        denominator=den, edges=[edge], validated_reviews=nodes,relation_request=request,relation_reviews=rr)
    assert result['status']=='satisfied',result


def test_P1_P2_spec_and_archive_versioned_pair(full,tmp_path):
    from daikibo.knowledge_history import validate_specifications
    from daikibo import archive_chunks
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    spec=full.history.export_current(full.owner,f['project'])
    assert spec['format']=='daikibo.spec.v7'
    assert spec['assurance_history']['format']=='daikibo.assurance-history.v2'
    assert validate_specifications(spec)
    bad=copy.deepcopy(spec);bad['format']='daikibo.spec.v6'
    with pytest.raises(Fault):validate_specifications(bad)
    bad=copy.deepcopy(spec);bad['assurance_history']['required_contracts']=[]
    with pytest.raises(Fault):validate_specifications(bad)


def test_R1_normal_governed_canonical_activation(tmp_path):
    from unit4p_domain_fixture import canonical_flow
    f=canonical_flow(tmp_path/'v5canonical',profile_format='assurance.profile.v5')
    c=f['control']
    try:
        before=_snapshot(c)
        for _ in range(2):
            result=c.assurance.evaluate_stage(c.owner,f['project'],f['program'],'plan',proposed_breakdown=f['breakdown'])
            assert result['status']=='satisfied',json.dumps(result['failures'],indent=2)
            c.p.next(c.owner,f['program'])
            c.lifecycle.status(c.owner,f['program'])
        assert _snapshot(c)==before
        assert c.breakdowns.activate(c.owner,f['breakdown'])['status']=='active'
    finally:c.close()


def test_P1_chunked_roundtrip_and_legacy_refusal(full,tmp_path):
    from daikibo.knowledge_history import inspect_archive
    from daikibo import archive_chunks
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    before=_snapshot(full)
    with pytest.raises(Fault):full.history.create(full.owner,f['project'],layout='legacy')
    assert _snapshot(full)==before
    base=full.history.create(full.owner,f['project'],layout='chunked')
    payload=json.loads(full.s.blob_get(base['snapshot_blob']))
    assert payload['format']=='daikibo.knowledge-snapshot.v13'
    exported=full.history.export_archive(full.owner,base['id'])
    assert exported['format']=='daikibo.knowledge-archive.v13'
    assert inspect_archive(exported['path'],exported['sha256'])
    before=_snapshot(full)
    for mutation in ('legacy','missing','unknown'):
        bad=copy.deepcopy(payload)
        if mutation=='legacy':bad['format']='daikibo.knowledge-snapshot.v12';bad.pop('required_contracts')
        elif mutation=='missing':bad.pop('required_contracts')
        else:bad['required_contracts'].append('unknown.v99')
        with pytest.raises(Fault):archive_chunks.validate(bad,full.s.blob_get)
    assert _snapshot(full)==before


def scope_v2(c,p,roots):
    return c.assurance.scope_propose(c.owner,p,{'format':'assurance.scope.v2','roots':roots,
        'selection_rules':{},'exclusion_proposals':[],'authority_refs':[],'discovery_unknowns':[]})


def test_Q1_Q2_Q4_two_same_label_domains_structural_closure_identity(full):
    from unit4p_domain_fixture import accept
    from daikibo.domain_responsibility import responsibility_records
    p=full.k.create_project(full.owner,'responsibility identity')['id']
    first=accept(full,p,'domain','same',responsibilities=['same duty','other duty'],non_responsibilities=[],owned_data=[],interfaces=[],
        structural_obligations={'format':'daikibo.structural-obligations.v1','responsibilities':[{'id':'extra','type':'statement','statement':'boundary invariant'}]})
    ref1=aref(full,p,first['id'])
    historical_same, _ = responsibility_records(ref1,'domain',{**first['body'],'responsibilities':['same duty','same duty']})
    assert len({x['id'] for x in historical_same})==3
    second=accept(full,p,'domain','same',responsibilities=['first','second'],non_responsibilities=[],owned_data=[],interfaces=[],
        structural_obligations={'format':'daikibo.structural-obligations.v1','responsibilities':[{'id':'ref','type':'domain_reference','domain':ref1,
            'responsibility_index':0,'responsibility_digest':digest('same duty')}]})
    ref2=aref(full,p,second['id'])
    s=scope_v2(full,p,[ref2,ref1]);repeat=scope_v2(full,p,[ref1,ref2])
    assert s['scope_ref']==repeat['scope_ref'] and s['obligations_ref']==repeat['obligations_ref']
    records=s['obligations']['body']['obligations'];assert len(records)==6 and len({x['id'] for x in records})==6
    assert {x['source_ref']['artifact'] for x in records}=={first['id'],second['id']}
    only_second=scope_v2(full,p,[ref2])
    assert only_second['obligations']['body']['input_refs']==sorted([ref1,ref2],key=canonical)
    assert all(x['source_ref']==ref2 for x in only_second['obligations']['body']['obligations'])
    before=_snapshot(full)
    with pytest.raises(Fault):scope_v2(full,p,[ref1,ref1])
    assert _snapshot(full)==before
    empty=accept(full,p,'domain','zero',responsibilities=[],non_responsibilities=[],owned_data=[],interfaces=[])
    es=scope_v2(full,p,[aref(full,p,empty['id'])]);assert es['obligations']['body']['obligations']==[]
    nodes=build_node_requests(full,full.owner,project=p,contract='assurance.node-contract.v2',selectors=[{'selector':'accepted_domain','node_ref':aref(full,p,empty['id'])}])
    assert len(nodes[0]['required_coverage'])==4
    assert select_node_reviews(full,full.owner,node_requests=nodes)[0]['roles']['domain_responsibility']['status']!='satisfied'


@pytest.mark.parametrize('change',['missing','null','blank','structural_null','unknown','foreign','stale','body_digest'])
def test_Q3_invalid_source_never_publishes_complete(full,change):
    from unit4p_domain_fixture import accept
    p=full.k.create_project(full.owner,'invalid responsibility')['id']
    x=accept(full,p,'domain','domain',responsibilities=['duty'],non_responsibilities=[],owned_data=[],interfaces=[])
    ref=aref(full,p,x['id'])
    if change in {'foreign','stale','body_digest'}:
        bad=copy.deepcopy(ref)
        bad[{'foreign':'project','stale':'revision','body_digest':'body_digest'}[change]]={'foreign':'PRJ-foreign','stale':99,'body_digest':'0'*64}[change]
    else:
        # Corrupt a private fixture's retained source, preserving its body hash,
        # to test the writer rather than Knowledge's earlier schema rejection.
        body=copy.deepcopy(x['body'])
        if change=='missing':body.pop('responsibilities')
        elif change=='null':body['responsibilities']=None
        elif change=='blank':body['responsibilities']=['']
        elif change=='structural_null':body['structural_obligations']=None
        else:body['structural_obligations']={'format':'unknown','responsibilities':[]}
        full.s.execute('UPDATE artifacts SET body=?,digest=? WHERE id=?',(canonical(body).decode(),digest(body),x['id']))
        full.s.execute('DROP TRIGGER revisions_no_update')
        full.s.execute('UPDATE revisions SET body=?,digest=? WHERE artifact=?',(canonical(body).decode(),digest(body),x['id']))
        bad=aref(full,p,x['id'])
    before=_snapshot(full)
    with pytest.raises(Fault):scope_v2(full,p,[bad])
    assert _snapshot(full)==before


@pytest.mark.parametrize('mutation',['missing','extra','duplicate','fail','blocked'])
def test_N2_observed_wrong_coverage_and_verdict(full,tmp_path,mutation):
    import sys
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    script=tmp_path/'reviewer.py'
    script.write_text("import json,sys\np=json.load(sys.stdin);c=p['context']['required_coverage'];m="+repr(mutation)+"\n"
        "c=c[1:] if m=='missing' else c+['extra'] if m=='extra' else c+c[:1] if m=='duplicate' else c\n"
        "print(json.dumps(dict(verdict=m if m in ('fail','blocked') else 'pass',rationale='finite negative',covered=c,findings=[],observations=[dict(ref=p['subject'],detail='finite')],dispositions=[])))\n")
    full.rt.adapters.register(full.owner,'domain-negative','fixture',sys.executable,[str(script)])
    full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','domain-negative')
    before=_snapshot(full)
    result=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
    assert result[0]['roles']['domain_responsibility']['status']!='satisfied'
    assert _snapshot(full)==before


@pytest.mark.parametrize('mutation',['json','binding','contract','selector','foreign'])
def test_V2_seal_and_version_reject_modified_bundle(full,mutation):
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    nodes=domain_nodes(full,f)
    if mutation=='json':nodes=json.loads(json.dumps(nodes))
    elif mutation=='contract':object.__setattr__(nodes,'_contract','assurance.node-contract.v1')
    elif mutation=='selector':nodes[0]['selector']='design'
    elif mutation=='foreign':nodes[0]['node_ref']['project']='PRJ-other'
    else:nodes[0]['binding']='0'*64
    before=_snapshot(full)
    with pytest.raises(Fault):select_node_reviews(full,full.owner,node_requests=nodes)
    assert _snapshot(full)==before


@pytest.mark.parametrize('mutation',['add','change','remove','reorder','body','invariant','dependency'])
def test_C1_C2_currentness_and_historical_revision(full,mutation):
    from unit4p_domain_fixture import accept
    from daikibo.knowledge_history import validate_specifications
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    old=domain_nodes(full,f)
    if mutation=='invariant':
        accept(full,f['project'],'design','new invariant',constraints={'mode':'safe'})
    elif mutation=='dependency':
        # A source CAS corruption cannot be papered over by a matching DOMAIN body.
        full.s.blob_path(f['source']['digest']).write_bytes(b'changed')
    else:
        row=full.k.artifact(full.owner,f['domains'][0]['id']);body=copy.deepcopy(row['body'])
        if mutation=='add':body['responsibilities'].append('third')
        elif mutation=='change':body['responsibilities'][0]='changed'
        elif mutation=='remove':body['responsibilities'].pop()
        elif mutation=='reorder':body['responsibilities'].reverse()
        else:body['statement']='new meaning with unchanged Q values'
        full.k._revise(full.owner,row,row['revision'],body,'synthetic accepted change','accepted')
    before=_snapshot(full)
    assert select_node_reviews(full,full.owner,node_requests=old)[0]['roles']['domain_responsibility']['status']!='satisfied'
    assert _snapshot(full)==before
    if mutation!='dependency':
        assert validate_specifications(full.history.export_current(full.owner,f['project']))


@pytest.mark.parametrize('kind',['legacy_pair','new_pair','unknown_scope','unknown_node','unknown_role'])
def test_V1_profile_contract_mismatch_no_write(full,kind):
    from test_unit4p_domain_profile_v4 import _body
    from test_e3_selection_contract import _fixture
    p,source,req,program,legacy=_fixture(full)
    modern=scope_v2(full,p,[aref(full,p,req['id'])])
    body=_body(p,program,legacy if kind=='legacy_pair' else modern,5)
    if kind=='new_pair':body['format']='assurance.profile.v4';body.pop('required_scope_contract');body.pop('required_node_contract')
    elif kind=='unknown_scope':body['required_scope_contract']='unknown'
    elif kind=='unknown_node':body['required_node_contract']='unknown'
    elif kind=='unknown_role':body['node_review_rules'][0]['roles'].append('unknown')
    before=_snapshot(full)
    with pytest.raises(Fault):full.assurance.profile_propose(full.owner,p,program,body,None)
    assert _snapshot(full)==before


def test_P1_backup_restored_candidate_preserves_domain_receipt(full,tmp_path):
    from daikibo.control import Control
    from daikibo.operations import restore_backup
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    backup=full.ops.backup(full.owner);home=tmp_path/'restored'
    restore_backup(backup['path'],home,backup['sha256'])
    restored=Control(home,mode='validation',start_workers=False)
    try:
        from pathlib import Path
        restored.owner=restored.sec.authenticate(Path(restored.sec.bootstrap()).read_text())
        assert select_node_reviews(restored,restored.owner,node_requests=domain_nodes(restored,f))[0]['roles']['domain_responsibility']['status']=='satisfied'
        assert restored.history.export_current(restored.owner,f['project'])['format']=='daikibo.spec.v7'
    finally:restored.close()


def test_M1_M2_normal_authority_migration_and_cas(tmp_path):
    from unit4p_domain_fixture import canonical_flow, reviews_adopt, populate_realizes, populate_implements
    from test_unit4p_domain_profile_v4 import _replacement
    f=canonical_flow(tmp_path/'migration',profile_format='assurance.profile.v3');c=f['control'];p=f['project']
    try:
        old=c.assurance.archive_rows(p)
        body,selected=_replacement(c,f)
        scope=scope_v2(c,p,[aref(c,p,x) for x in [*f['requirements'],f['domain'],f['design'],f['interface']]])
        body.update(format='assurance.profile.v5',required_scope_contract='assurance.scope.v2',required_node_contract='assurance.node-contract.v2',
                    scope_ref=scope['scope_ref'],obligations_ref=scope['obligations_ref'])
        body['node_review_rules']=sorted([*body['node_review_rules'],{'id':'domain','selector':'domain','roles':['domain_responsibility']}],key=lambda x:x['id'])
        body['relation_selectors']=sorted([*body['relation_selectors'],'implements'])
        for name,stage in body['stage_rules'].items():
            stage['node_rules']=sorted([*stage['node_rules'],'domain'])
            if name in {'plan','task'}:
                stage['relation_sets']=sorted([*stage['relation_sets'],{'relation':'implements','direction':'incoming','centers':['design_artifacts']}],key=canonical)
        for authority in ([],[{**body['authority_refs'][0],'project':'foreign'}],[{**body['authority_refs'][0],'source':'SRC-missing'}],
                          [{**body['authority_refs'][0],'blob_digest':'0'*64}]):
            bad=copy.deepcopy(body);bad['authority_refs']=authority
            before=_snapshot(c)
            with pytest.raises(Fault):c.assurance.profile_propose(c.owner,p,f['program'],bad,selected['head_event'])
            assert _snapshot(c)==before
        reviews_adopt(c,p,scope['scope'],'finite')
        reviews_adopt(c,p,scope['obligations'],'finite')
        proposal=c.assurance.profile_propose(c.owner,p,f['program'],body,selected['head_event'])
        competitor=copy.deepcopy(body);competitor['change_reason']='independent competing transition'
        second=c.assurance.profile_propose(c.owner,p,f['program'],competitor,selected['head_event'])
        reviews_adopt(c,p,proposal['profile'],'finite',selected['head_event'])
        before=_snapshot(c)
        assert c.assurance.evaluate_stage(c.owner,p,f['program'],'plan',proposed_breakdown=f['breakdown'])['status']!='satisfied'
        assert _snapshot(c)==before
        # Obtain ordinary reviews before testing the adoption CAS; review writes are intentional.
        from test_e3_selection_contract import _review_refs
        # finite adapter remains the only governed adapter.
        refs=[]
        for packet,role in c.assurance._review_requirements(p,c.assurance._adoption_roots(p,second['profile'])):
            ev=c.rt.review(c.owner,packet['id'],role,'finite');refs.append({'packet':packet['id'],'role':role,'id':ev['receipt']})
        before=_snapshot(c)
        with pytest.raises(Fault):c.assurance.adopt(c.owner,p,second['profile']['id'],second['profile']['digest'],selected['head_event'],refs)
        assert _snapshot(c)==before
        c.rt.review(c.owner,f['domain'],'domain_responsibility','finite')
        populate_realizes(c,p,f['realize_sources'],f['requirements'],scope,proposal)
        pin=c.s.one("SELECT r.id FROM traceability_revisions r JOIN traceability_sets s ON s.id=r.set_id WHERE s.project=? AND json_extract(r.body,'$.scope.repository')=? ORDER BY r.created DESC LIMIT 1",(p,f['repos'][0]))
        populate_implements(c,p,[f['domain'],f['design'],f['interface']],pin['id'],f['repos'][0],scope,proposal)
        result=c.assurance.evaluate_stage(c.owner,p,f['program'],'plan',proposed_breakdown=f['breakdown'])
        assert result['status']=='satisfied',result['failures']
        assert c.breakdowns.activate(c.owner,f['breakdown'])['status']=='active'
        after=c.assurance.archive_rows(p)
        for key,rows in old.items():
            if key!='assurance_heads':assert all(x in after[key] for x in rows)
    finally:c.close()


def test_R2_missing_N_E_S_and_Q_are_independent_nonpass(full,tmp_path):
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    ctx,den=_context(full,f)
    full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    nodes=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
    edge,aset=_domain_proof(full,f,tmp_path)
    request=_request(full,f,ctx,den,aref(full,f['project'],f['domains'][0]['id']),'implements','incoming')
    rr=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(aset))
    requirements=sorted(SET_UNIVERSAL_CRITERIA|set(registry_entry('implements',contract_digest=REGISTRY_V2_DIGEST)['set_checks']))
    def evaluate(edges,ns,rs):return evaluate_criteria(relation='implements', requirements=requirements,denominator=den,
        edges=edges,validated_reviews=ns,relation_request=request,relation_reviews=rs)
    assert evaluate([edge],nodes,rr)['status']=='satisfied'
    empty_nodes=select_node_reviews(full,full.owner,node_requests=build_node_requests(full,full.owner,project=f['project'],contract='assurance.node-contract.v2',selectors=[]))
    before=_snapshot(full)
    assert evaluate([],nodes,rr)['criteria']['all_responsibilities']['status']!='satisfied'
    assert evaluate([edge],empty_nodes,rr)['criteria']['meaning_review']['status']!='satisfied'
    assert evaluate([edge],nodes,None)['criteria']['meaning_review']['status']!='satisfied'
    assert _snapshot(full)==before
    # Missing a single saved Q is a normal partial claim, never a remapped ID.
    body=copy.deepcopy(edge['body']);body['obligation_ids']=body['obligation_ids'][:1];body['claim']='partial responsibility proof'
    partial=full.assurance.edge_propose(full.owner,f['project'],body)['edge']
    from unit4p_domain_fixture import reviews_adopt
    reviews_adopt(full,f['project'],partial,'e3-assurance-fixture')
    before=_snapshot(full)
    partial_result=evaluate([partial],nodes,None)
    assert partial_result['criteria']['all_responsibilities']['status']!='satisfied'
    assert len(partial_result['criteria']['all_responsibilities']['missing_ids'])==1
    assert _snapshot(full)==before


@pytest.mark.parametrize('mutation',['foreign','wrong_owner','extra_Q','wrong_scope','stale','duplicate'])
def test_R3_foreign_stale_extra_and_owner_cannot_pass(full,tmp_path,mutation):
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    edge,aset=_domain_proof(full,f,tmp_path)
    body=copy.deepcopy(edge['body'])
    if mutation=='foreign':body['target_ref']['project']='PRJ-foreign'
    elif mutation=='wrong_owner':body['target_ref']=aref(full,f['project'],f['req']['id'])
    elif mutation=='extra_Q':body['obligation_ids'].append('obligation:'+ '0'*64)
    elif mutation=='wrong_scope':body['scope_ref']['object_digest']='0'*64
    elif mutation=='stale':body['target_ref']['revision']=99
    else:body['obligation_ids'].append(body['obligation_ids'][0])
    before=_snapshot(full)
    if mutation=='wrong_owner':
        proposal=full.assurance.edge_propose(full.owner,f['project'],body)
        # Endpoint kind artifact is registry-legal; exact responsibility owner
        # is enforced by the semantic matcher, not by widening the registry.
        ctx,den=_context(full,f)
        request=_request(full,f,ctx,den,aref(full,f['project'],f['domains'][0]['id']),'implements','incoming')
        nodes=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
        result=evaluate_criteria(relation='implements',requirements=sorted(SET_UNIVERSAL_CRITERIA|set(registry_entry('implements',contract_digest=REGISTRY_V2_DIGEST)['set_checks'])),
            denominator=den,edges=[proposal['edge']],validated_reviews=nodes,relation_request=request)
        assert result['status']!='satisfied'
    else:
        with pytest.raises(Fault):full.assurance.edge_propose(full.owner,f['project'],body)
        assert _snapshot(full)==before


def test_Q5_context_outside_scope_remains_visible(full):
    from unit4p_domain_fixture import accept
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    second=accept(full,f['project'],'domain','outside scope',responsibilities=['third','fourth'],non_responsibilities=[],owned_data=[],interfaces=[])
    ctx,den=_context(full,f)
    assert len([x for x in den['obligations'] if x['category']=='artifact_responsibility'])==4
    before=_snapshot(full)
    with pytest.raises(Fault):_request(full,f,ctx,den,aref(full,f['project'],second['id']),'implements','incoming')
    assert _snapshot(full)==before


@pytest.mark.parametrize('subject_kind',['requirement','draft','foreign','legacy_role'])
def test_N1_domain_selector_never_borrows_legacy_authority(full,subject_kind):
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    ref=aref(full,f['project'],f['domains'][0]['id'])
    if subject_kind=='requirement':ref=aref(full,f['project'],f['req']['id'])
    elif subject_kind=='draft':
        x=full.k.propose(full.owner,f['project'],'domain',dict(f['domains'][0]['body']));ref=aref(full,f['project'],x['id'])
    elif subject_kind=='foreign':ref['project']='PRJ-other'
    else:
        full.rt.review(full.owner,f['domains'][0]['id'],'design','e3-assurance-fixture')
        before=_snapshot(full)
        assert select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))[0]['roles']['domain_responsibility']['status']!='satisfied'
        assert _snapshot(full)==before
        return
    before=_snapshot(full)
    with pytest.raises(Fault):build_node_requests(full,full.owner,project=f['project'],contract='assurance.node-contract.v2',selectors=[{'selector':'domain','node_ref':ref}])
    assert _snapshot(full)==before


def test_N3_complete_long_material_and_no_silent_truncation(full,tmp_path):
    from unit4p_domain_fixture import accept
    from test_e3_selection_contract import _register_fixture_review
    p=full.k.create_project(full.owner,'long domain material')['id']
    source=full.k.source(full.owner,p,'duty and boundary source. '*10000)
    x=accept(full,p,'domain','long',source_refs=[source['id']],responsibilities=[],non_responsibilities=[],owned_data=[],interfaces=[])
    _register_fixture_review(full)
    ev=full.rt.review(full.owner,x['id'],'domain_responsibility','e3-assurance-fixture')
    receipt=full.g.receipt(ev['receipt']);prompt=json.loads(full.s.blob_get(receipt['input_digest']))
    assert prompt['context']['domain_review']['sources'][0]['content']=='duty and boundary source. '*10000
    nodes=build_node_requests(full,full.owner,project=p,contract='assurance.node-contract.v2',selectors=[{'selector':'domain','node_ref':aref(full,p,x['id'])}])
    assert len(nodes[0]['required_coverage'])==4
    assert select_node_reviews(full,full.owner,node_requests=nodes)[0]['roles']['domain_responsibility']['status']=='satisfied'


def test_U1_failed_set_cas_cleans_new_material(full,tmp_path):
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    edge,aset=_domain_proof(full,f,tmp_path)
    before=_snapshot(full)
    with pytest.raises(Fault):
        full.assurance.set_propose(full.owner,f['project'],{'center_ref':aref(full,f['project'],f['domains'][0]['id']),
            'relation':'implements','direction':'incoming','relation_contract_digest':REGISTRY_V2_DIGEST,
            'scope_ref':f['profile']['profile_ref'],'criteria':{},'required_evidence_refs':[],'set_id':'fresh-cas-target'},expected_head='AEVT-foreign')
    assert _snapshot(full)==before
