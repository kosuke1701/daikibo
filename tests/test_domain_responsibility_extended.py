"""Cross-owner, compatibility and failure-atomic DOMAIN contract probes."""
import copy
import json
import pytest
from daikibo.common import Fault, canonical, digest
from daikibo.assurance_relations import REGISTRY_V1_DIGEST, REGISTRY_V2_DIGEST, registry_entry
from daikibo.assurance import SET_UNIVERSAL_CRITERIA
from daikibo.assurance_node_reviews import select_node_reviews
from daikibo.assurance_criteria import build_review_assurance, evaluate_criteria
from test_unit4p_domain_profile_v4 import _selected,_context,_request,_snapshot
from test_domain_responsibility_v5 import domain_nodes,scope_v2
from unit4p_domain_fixture import canonical_flow,aref,reviews_adopt,populate_realizes


def test_U1_blob_rollback_does_not_remove_another_writer_publication(full):
    import concurrent.futures
    import threading
    import io
    data=b'new transaction material shared by two writers'
    started=threading.Event();completed=threading.Event()
    def publish():
        started.set()
        result=full.s.blob_put_stream(io.BytesIO(data))
        completed.set()
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError):
            with full.s.transaction(rollback_blobs=True):
                ident=full.s.blob_put(data)
                future=pool.submit(publish)
                assert started.wait(2)
                assert not completed.wait(.05)
                raise RuntimeError('rollback first writer')
        assert future.result(timeout=2)==(ident,len(data))
    assert full.s.blob_get(ident)==data


def test_C2_old_S2_multiple_current_sets_are_ambiguous_and_withdrawn_is_not_current(full,tmp_path):
    from test_unit4p_domain_profile_v4 import _domain_proof
    from daikibo.assurance_stage import _matching_relation_set
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    _,first=_domain_proof(full,f,tmp_path)
    p=f['project'];target=aref(full,p,f['domains'][0]['id'])
    second=full.assurance.set_propose(full.owner,p,{'center_ref':target,'relation':'implements','direction':'incoming',
        'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],'criteria':{},
        'required_evidence_refs':[{'kind':'source','project':p,'source':f['source']['id'],'blob_digest':f['source']['digest']}],
        'set_id':'independent responsibility set'})['set']
    reviews_adopt(full,p,second,'e3-assurance-fixture')
    def read():return _matching_relation_set(full,p,relation='implements',direction='incoming',center_ref=target,
        scope_ref=f['profile']['profile_ref'],registry_digest=REGISTRY_V2_DIGEST)
    before=_snapshot(full);chosen,error=read();assert chosen is None and error['code']=='relation_set_ambiguous';assert _snapshot(full)==before
    # E1 records withdrawal history only; this probe claims no authorization
    # for an automatic withdrawal command or a fresh semantic review.
    head=full.assurance._head_event(p,first['logical_id'])
    full.assurance._append_storage_event(full.owner,p,first['logical_id'],first['id'],first['digest'],'withdraw',head['id'])
    before=_snapshot(full);chosen,error=read();assert error is None and chosen['id']==second['id'];assert _snapshot(full)==before
    assert full.assurance.object_get(full.owner,p,first['id'])['body']==first['body']


def test_U1_versioned_pair_never_chooses_latest_duplicate(full):
    from daikibo.knowledge_history import validate_specifications
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    ob=f['scope']['obligations']
    full.assurance.store_object(full.owner,f['project'],'obligations',ob['logical_id'],ob['revision']+1,ob['body'])
    before=_snapshot(full)
    with pytest.raises(Fault):scope_v2(full,f['project'],f['scope']['scope']['body']['roots'])
    with pytest.raises(Fault):full.assurance._obligations_for_scope(f['project'],f['scope']['scope'])
    assert full.assurance.evaluate_stage(full.owner,f['project'],f['program'],'plan')['status']!='satisfied'
    with pytest.raises(Fault):validate_specifications(full.history.export_current(full.owner,f['project']))
    assert _snapshot(full)==before


@pytest.mark.parametrize('tamper',['pointer','value_digest','duplicate_record','missing_source','missing_prompt'])
def test_P2_retained_closure_rejects_tampering_before_import(full,tamper):
    from daikibo.knowledge_history import validate_specifications
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    spec=full.history.export_current(full.owner,f['project'])
    assert validate_specifications(spec)
    if tamper in {'pointer','value_digest','duplicate_record'}:
        rows=spec['assurance_history']['assurance_objects']
        row=next(x for x in rows if x['id']==f['scope']['obligations']['id'])
        row['body']=json.loads(row['body']) if isinstance(row['body'],str) else row['body']
        if tamper=='pointer':row['body']['obligations'][0]['pointer']='/responsibilities/999'
        elif tamper=='value_digest':row['body']['obligations'][0]['value_digest']='0'*64
        else:row['body']['obligations'].append(copy.deepcopy(row['body']['obligations'][0]))
        row['digest']=digest(row['body'])
    else:
        # Removing exact source or prompt bytes cannot be replaced with a
        # label or a successful retained verdict.
        target=f['source']['digest'] if tamper=='missing_source' else json.loads(full.s.one("SELECT body FROM runs WHERE role='domain_responsibility'")['body'])['input_digest']
        context=spec['observed_context'];old=len(context['blobs'])
        context['blobs']=[x for x in context['blobs'] if x['sha256']!=target]
        assert len(context['blobs'])==old-1
        context['digest']=digest({k:v for k,v in context.items() if k!='digest'})
    before=_snapshot(full)
    with pytest.raises(Fault):validate_specifications(spec)
    assert _snapshot(full)==before


@pytest.mark.parametrize('tamper',['prompt','marker','run_role','run_project','receipt_binding','event_mac','event_missing','receipt_mac'])
def test_N2_domain_receipt_integrity_is_independent_of_pass_verdict(full,tamper):
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    reviewed=full.rt.review(full.owner,f['domains'][0]['id'],'domain_responsibility','e3-assurance-fixture')
    receipt=full.g.receipt(reviewed['receipt'])
    assert select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))[0]['roles']['domain_responsibility']['status']=='satisfied'
    if tamper in {'prompt','marker'}:
        path=full.s.blob_path(receipt['input_digest']);prompt=json.loads(path.read_bytes())
        if tamper=='prompt':prompt['context']['domain_review']['artifact']['body']['owned_data']=['changed']
        else:prompt['context']['domain_review']['required_coverage'].pop()
        path.write_bytes(canonical(prompt))
    elif tamper in {'run_role','run_project'}:
        field='role' if tamper=='run_role' else 'project'
        foreign=full.k.create_project(full.owner,'foreign receipt')['id'] if field=='project' else None
        full.s.execute('DROP TRIGGER IF EXISTS runs_no_update')
        full.s.execute(f'UPDATE runs SET {field}=? WHERE id=?',('design' if field=='role' else foreign,receipt['run']))
    elif tamper=='receipt_binding':
        full.s.execute('DROP TRIGGER receipts_no_update')
        full.s.execute('UPDATE receipts SET binding=? WHERE id=?',('0'*64,receipt['id']))
    elif tamper in {'event_mac','event_missing'}:
        row=full.s.one("SELECT seq FROM events WHERE kind='run_observed' AND json_extract(body,'$.receipt')=?",(receipt['id'],))
        if tamper=='event_mac':
            full.s.execute('DROP TRIGGER events_no_update');full.s.execute('UPDATE events SET mac=? WHERE seq=?',('0'*64,row['seq']))
        else:
            full.s.execute('DROP TRIGGER events_no_delete');full.s.execute('DELETE FROM events WHERE seq=?',(row['seq'],))
    else:
        full.s.execute('DROP TRIGGER receipts_no_update');full.s.execute('UPDATE receipts SET mac=? WHERE id=?',('0'*64,receipt['id']))
    before=_snapshot(full)
    result=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
    assert result[0]['roles']['domain_responsibility']['status']!='satisfied'
    assert _snapshot(full)==before


def test_Q4_zero_responsibilities_still_requires_boundary_node_and_set_review(full):
    from test_e3_selection_contract import _fixture,_register_fixture_review,_adopt
    from test_unit4p_domain_profile_v4 import _body
    from unit4p_domain_fixture import accept
    from daikibo.assurance_node_reviews import build_node_requests
    p,source,_,program,_=_fixture(full)
    domain=accept(full,p,'domain','empty explicit',source_refs=[source['id']],responsibilities=[],non_responsibilities=['network'],owned_data=[],interfaces=[],
        structural_obligations={'format':'daikibo.structural-obligations.v1','responsibilities':[]})
    scope=scope_v2(full,p,[aref(full,p,domain['id'])]);body=_body(p,program,scope,5)
    body['relation_selectors']=['implements','realizes']
    _register_fixture_review(full)
    profile=full.assurance.profile_propose(full.owner,p,program,body,None);_adopt(full,p,profile,None)
    f={'project':p,'program':program,'domains':[domain],'scope':scope,'profile':profile}
    ctx,den=_context(full,f);target=aref(full,p,domain['id'])
    assert scope['obligations']['body']['obligations']==[]
    aset=full.assurance.set_propose(full.owner,p,{'center_ref':target,'relation':'implements','direction':'incoming',
        'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':profile['profile_ref'],'criteria':{},'required_evidence_refs':[]})
    reviews_adopt(full,p,aset['set'],'e3-assurance-fixture')
    request=_request(full,f,ctx,den,target,'implements','incoming')
    rr=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(aset['set']))
    def result(nodes,synthesis):return evaluate_criteria(relation='implements',requirements=sorted(SET_UNIVERSAL_CRITERIA|set(registry_entry('implements',contract_digest=REGISTRY_V2_DIGEST)['set_checks'])),
        denominator=den,edges=[],validated_reviews=nodes,relation_request=request,relation_reviews=synthesis)
    missing=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
    assert result(missing,rr)['status']!='satisfied'
    full.rt.review(full.owner,domain['id'],'domain_responsibility','e3-assurance-fixture')
    nodes=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
    assert len(domain_nodes(full,f)[0]['required_coverage'])==5
    assert result(nodes,None)['status']!='satisfied'
    assert result(nodes,rr)['status']=='satisfied',json.dumps(result(nodes,rr),indent=2)


def test_N3_over_budget_material_is_blocked_without_truncated_receipt(full):
    from unit4p_domain_fixture import accept
    from test_e3_selection_contract import _register_fixture_review
    from daikibo.assurance_node_reviews import build_node_requests
    p=full.k.create_project(full.owner,'over budget domain')['id']
    source=full.k.source(full.owner,p,'x'*1_010_000)
    domain=accept(full,p,'domain','bounded refusal',source_refs=[source['id']],responsibilities=[],
        non_responsibilities=[],owned_data=[],interfaces=[])
    _register_fixture_review(full)
    with pytest.raises(Fault) as error:full.rt.review(full.owner,domain['id'],'domain_responsibility','e3-assurance-fixture')
    assert error.value.code=='context_insufficient'
    nodes=build_node_requests(full,full.owner,project=p,contract='assurance.node-contract.v2',
        selectors=[{'selector':'domain','node_ref':aref(full,p,domain['id'])}])
    assert select_node_reviews(full,full.owner,node_requests=nodes)[0]['roles']['domain_responsibility']['status']!='satisfied'


def test_S1_v5_integration_delivery_consumers_and_checkpoint_missing_proof(full,full_project,tmp_path):
    from test_delivery_execution_c_connection import _normal_case,_adopt_output_relations,_actual_review_adapter
    from test_e3_consumer_c_integration import _reviews
    from daikibo.assurance_stage import _relation_component,_execution_component
    from daikibo.assurance_denominators import collect_stage_context,derive_denominator
    case=_normal_case(full,full_project,tmp_path,two_repositories=True,output_relations=True,profile_format='assurance.profile.v5')
    p=case['project'];profile=full.assurance._object_by_ref(case['scope'],p,kinds={'profile'})
    assert profile['body']['format']=='assurance.profile.v5'
    def read(stage,checkpoint):
        context=collect_stage_context(full,full.owner,project=p,program=case['fixture']['program'],stage=stage,
            proposed_breakdown=case['fixture']['breakdown'],delivery=case['snapshot'])
        den=derive_denominator(context)
        relation,_,_=_relation_component(full,full.owner,p,profile['body'],stage,checkpoint,context,den,None,
            case['scope'],_reviews(full,case),REGISTRY_V2_DIGEST)
        execution,_,_=_execution_component(full,full.owner,p,stage,checkpoint,context,den,None,profile['body'])
        return relation,execution
    before=_snapshot(full)
    for stage,checkpoint in [('integration','certify'),('integration','commit_pre'),('delivery','finalize'),('delivery','finish'),('delivery','export')]:
        relation,execution=read(stage,checkpoint)
        assert relation['status']=='missing'
        assert execution['status']=='satisfied',execution
    assert _snapshot(full)==before
    adapter=_actual_review_adapter(full,tmp_path);_adopt_output_relations(full,case,adapter)
    before=_snapshot(full)
    for stage,checkpoint in [('integration','certify'),('integration','commit_pre'),('delivery','finalize'),('delivery','finish'),('delivery','export')]:
        relation,execution=read(stage,checkpoint)
        assert relation['status']=='satisfied',relation
        assert execution['status']=='satisfied',execution
        result=full.assurance.evaluate_stage(full.owner,p,case['fixture']['program'],stage,checkpoint=checkpoint,
            proposed_breakdown=case['fixture']['breakdown'],delivery=case['snapshot'])
        assert result['relations']['status']=='satisfied',result
        assert result['nodes']['status']=='satisfied',result['nodes']
        assert result['execution']['status']=='satisfied',result['execution']
        assert result['assurance_allow'] is True,result
    assert _snapshot(full)==before


def test_N1_supplemental_roles_keep_their_semantics_and_cannot_replace_domain(full):
    from daikibo.assurance_node_reviews import build_node_requests
    f=_selected(full,version=5,domain_scope=True,designs=0,implements=True)
    ref=aref(full,f['project'],f['domains'][0]['id'])
    def selected():
        return select_node_reviews(full,full.owner,node_requests=build_node_requests(full,full.owner,
            project=f['project'],contract='assurance.node-contract.v2',selectors=[{
                'selector':'domain','node_ref':ref,'roles':['consistency','design','domain_responsibility']}]))[0]['roles']
    for role in ('design','consistency'):full.rt.review(full.owner,ref['artifact'],role,'e3-assurance-fixture')
    before=_snapshot(full);roles=selected();assert _snapshot(full)==before
    assert roles['domain_responsibility']['status']!='satisfied'
    assert all(roles[x]['status']=='satisfied' for x in ('design','consistency'))
    full.rt.review(full.owner,ref['artifact'],'domain_responsibility','e3-assurance-fixture')
    before=_snapshot(full);assert all(x['status']=='satisfied' for x in selected().values());assert _snapshot(full)==before


def test_Q1_two_domains_structural_and_git_file_symbol_all_nes(full,tmp_path):
    from test_e3_selection_contract import _fixture,_register_fixture_review,_adopt
    from test_unit4p_domain_profile_v4 import _body
    from unit4p_domain_fixture import accept,git_commit,git_file_ref
    p,source,requirement,program,_=_fixture(full)
    first=accept(full,p,'domain','same label',source_refs=[source['id']],responsibilities=['first','second'],
        non_responsibilities=['network'],owned_data=['state'],interfaces=[],structural_obligations={
            'format':'daikibo.structural-obligations.v1','responsibilities':[{'id':'extra','type':'statement','statement':'boundary'}]})
    second=accept(full,p,'domain','same label',source_refs=[source['id']],responsibilities=['first','second'],
        non_responsibilities=['network'],owned_data=['other state'],interfaces=[],structural_obligations={
            'format':'daikibo.structural-obligations.v1','responsibilities':[{'id':'dependency','type':'domain_reference',
                'domain':aref(full,p,first['id']),'responsibility_index':0,'responsibility_digest':digest('first')}]})
    scope=scope_v2(full,p,[aref(full,p,x['id']) for x in (first,second)])
    body=_body(p,program,scope,5)
    body['relation_selectors']=['implements','realizes']
    body['node_review_rules']=[{'id':'domain','selector':'domain','roles':['domain_responsibility']}]
    for stage in body['stage_rules'].values():stage['node_rules']=['domain']
    _register_fixture_review(full)
    profile=full.assurance.profile_propose(full.owner,p,program,body,None);_adopt(full,p,profile,None)
    f={'project':p,'program':program,'domains':[first,second],'scope':scope,'profile':profile}
    ctx,den=_context(full,f)
    saved=scope['obligations']['body']['obligations']
    assert saved==sorted([x for x in den['obligations'] if x['category'] in {'artifact_responsibility','artifact_structural_responsibility'}],key=lambda x:x['id'])
    assert len(saved)==6
    root=tmp_path/'git';root.mkdir();(root/'impl.py').write_text('def first():\n    return 1\ndef second():\n    return 2\n')
    commit=git_commit(root);repo=full.sn.register(full.owner,p,'two source pins',str(root))['id']
    pop=full.traceability.propose(full.owner,p,kind='code',scope={'repository':repo,'commit':commit})
    revision=full.traceability.extract(full.owner,pop['id'])['revision']
    file=git_file_ref(full,p,revision,repo,'impl.py')
    rb=json.loads(full.s.one('SELECT body FROM traceability_revisions WHERE id=?',(revision,))['body'])
    item=json.loads(full.s.one("SELECT body FROM traceability_items WHERE revision=? AND item_kind='symbol' ORDER BY id LIMIT 1",(revision,))['body'])
    symbol={**file,'ref_type':'git_symbol','adapter':'python-ast-v1','adapter_digest':rb['adapter_contract']['implementation_digest'],
        'qualified_name':item['qualified_name'],'kind':item['kind'],'ordinal':item['ordinal'],'start_byte':item['byte_start'],
        'end_byte':item['byte_end'],'span_sha256':item['source_span']['span_hash'],'signature_hash':item['signature_hash']}
    for domain in (first,second):full.rt.review(full.owner,domain['id'],'domain_responsibility','e3-assurance-fixture')
    nodes=select_node_reviews(full,full.owner,node_requests=domain_nodes(full,f))
    assert all(x['roles']['domain_responsibility']['status']=='satisfied' for x in nodes)
    for domain in (first,second):
        target=aref(full,p,domain['id']);ids=[x['id'] for x in saved if x['source_ref']==target];edges=[]
        for locator,claimed in ((file,ids[:1]),(symbol,ids[1:])):
            edge=full.assurance.edge_propose(full.owner,p,{'source_ref':{'kind':'traceability_ref','project':p,'locator':locator},
                'target_ref':target,'relation':'implements','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':profile['profile_ref'],
                'claim':'Pinned source implements the declared responsibilities','obligation_ids':claimed,'required_evidence_refs':[],'authority_refs':[]})
            reviews_adopt(full,p,edge['edge'],'e3-assurance-fixture');edges.append(edge['edge'])
        aset=full.assurance.set_propose(full.owner,p,{'center_ref':target,'relation':'implements','direction':'incoming',
            'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':profile['profile_ref'],'criteria':{},'required_evidence_refs':[]})
        assert aset['missing_obligations']==[];reviews_adopt(full,p,aset['set'],'e3-assurance-fixture')
        request=_request(full,f,ctx,den,target,'implements','incoming')
        assert set(request['required_obligation_ids'])==set(ids)
        assert all(x['owners']==[target] for x in request['owner_mapping'])
        rr=build_review_assurance(full,full.owner,relation_request=request,set_ref=full.assurance._object_ref(aset['set']))
        before=_snapshot(full)
        result=evaluate_criteria(relation='implements',requirements=sorted(SET_UNIVERSAL_CRITERIA|set(registry_entry('implements',contract_digest=REGISTRY_V2_DIGEST)['set_checks'])),
            denominator=den,edges=edges,validated_reviews=nodes,relation_request=request,relation_reviews=rr)
        assert result['status']=='satisfied',result
        assert _snapshot(full)==before


def test_old_A2_set_edge_and_acceptance_missing_then_complete(tmp_path):
    f=canonical_flow(tmp_path/'missing',profile_format='assurance.profile.v5',relation_ready=False)
    c=f['control'];p=f['project']
    try:
        def denied():
            before=_snapshot(c)
            with pytest.raises(Fault):c.breakdowns.activate(c.owner,f['breakdown'])
            assert _snapshot(c)==before
        denied()
        populate_realizes(c,p,f['realize_sources'][:1],f['requirements'],f['scope'],f['profile'])
        denied()
        source=f['realize_sources'][1];req=f['requirements'][0]
        ids=[x['id'] for x in f['scope']['obligations']['body']['obligations'] if x.get('source_ref',{}).get('artifact')==req]
        edge=c.assurance.edge_propose(c.owner,p,{'source_ref':aref(c,p,source),'target_ref':aref(c,p,req),
            'relation':'realizes','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],
            'claim':'one acceptance only','obligation_ids':ids,'required_evidence_refs':[],'authority_refs':[]})
        reviews_adopt(c,p,edge['edge'],'finite')
        incomplete=c.assurance.set_propose(c.owner,p,{'center_ref':aref(c,p,source),'relation':'realizes','direction':'outgoing',
            'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],'criteria':{},'required_evidence_refs':[]})
        assert len(incomplete['missing_obligations'])==1
        denied()
        # Missing E is independently diagnosed before the complete source set is adopted.
        req=f['requirements'][1]
        ids=[x['id'] for x in f['scope']['obligations']['body']['obligations'] if x.get('source_ref',{}).get('artifact')==req]
        edge=c.assurance.edge_propose(c.owner,p,{'source_ref':aref(c,p,source),'target_ref':aref(c,p,req),
            'relation':'realizes','relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],
            'claim':'second acceptance','obligation_ids':ids,'required_evidence_refs':[],'authority_refs':[]})
        denied()
        reviews_adopt(c,p,edge['edge'],'finite')
        complete=c.assurance.set_propose(c.owner,p,{'center_ref':aref(c,p,source),'relation':'realizes','direction':'outgoing',
            'relation_contract_digest':REGISTRY_V2_DIGEST,'scope_ref':f['profile']['profile_ref'],'criteria':{},'required_evidence_refs':[]})
        assert complete['missing_obligations']==[]
        reviews_adopt(c,p,complete['set'],'finite')
        assert c.breakdowns.activate(c.owner,f['breakdown'])['status']=='active'
    finally:c.close()


def test_M2_adoption_rechecks_authority_and_predecessor_without_write(full):
    from test_unit4p_domain_profile_v4 import _replacement
    f=_selected(full,version=3);p=f['project'];body,selected=_replacement(full,f)
    modern=scope_v2(full,p,f['scope']['scope']['body']['roots'])
    reviews_adopt(full,p,modern['scope'],'e3-assurance-fixture')
    reviews_adopt(full,p,modern['obligations'],'e3-assurance-fixture')
    body.update(format='assurance.profile.v5',required_scope_contract='assurance.scope.v2',required_node_contract='assurance.node-contract.v2',
                scope_ref=modern['scope_ref'],obligations_ref=modern['obligations_ref'])
    for kind in ('empty','foreign','unresolved','digest','predecessor'):
        bad=copy.deepcopy(body)
        if kind=='empty':bad['authority_refs']=[]
        elif kind=='foreign':bad['authority_refs'][0]['project']='PRJ-other'
        elif kind=='unresolved':bad['authority_refs'][0]['source']='SRC-missing'
        elif kind=='digest':bad['authority_refs'][0]['blob_digest']='0'*64
        else:bad['previous_selection_ref']['object_digest']='0'*64
        # Mechanical storage is not authority. A malformed persisted row is
        # injected only if E1 already prevents storing its foreign reference;
        # E3 adoption must independently reject it before receipt checks.
        try:
            raw=full.assurance._store_e2_object(full.owner,p,'profile','profile:program:'+f['program'],bad)
        except Fault:
            ident='AOBJ-malformed-authority-'+kind
            revision=full.s.one("SELECT MAX(revision) AS revision FROM assurance_objects WHERE project=? AND logical_id=?",(p,'profile:program:'+f['program']))['revision']+1
            full.s.execute('INSERT INTO assurance_objects(id,project,kind,logical_id,revision,body,digest,created) VALUES(?,?,?,?,?,?,?,?)',
                (ident,p,'profile','profile:program:'+f['program'],revision,canonical(bad).decode(),digest(bad),0))
            raw={'id':ident,'digest':digest(bad)}
        before=_snapshot(full)
        with pytest.raises(Fault) as rejected:full.assurance.adopt(full.owner,p,raw['id'],raw['digest'],selected['head_event'],[])
        assert rejected.value.code in {'invalid_profile','cross_project','unresolved_reference','stale_reference','not_found','stale_head','integrity_error'},(kind,rejected.value.code)
        assert _snapshot(full)==before


def test_R4_shared_task_owner_projection_is_preserved_with_v2_scope(full,tmp_path):
    from test_e3_unit2a_denominators import _fixture,_task
    from test_e3_unit2c_extractors import _valid_breakdown
    from daikibo.assurance_denominators import collect_stage_context,derive_denominator,project_task
    from daikibo.assurance_criteria import build_relation_request
    from daikibo.task_revisions import task_definition_digest
    f=_fixture(full,tmp_path);p=f['project']
    b=_task(full,p,f['parent']['id'],'second shared owner',[{'id':'b','argv':['python','-c','print(1)'],'purpose':'b'}])
    bd_body=json.loads(full.s.one('SELECT body FROM breakdowns WHERE id=?',(f['breakdown'],))['body'])
    bd_body['units'][0]['tasks']=[f['task_a']['id'],b['id']]
    breakdown=_valid_breakdown(full,f,'BREAKDOWN-v2-shared-owners',body=bd_body)
    ctx=collect_stage_context(full,full.owner,project=p,program=f['program'],stage='plan',proposed_breakdown=breakdown)
    den=derive_denominator(ctx);scope=scope_v2(full,p,[aref(full,p,f['parent']['id'])])
    common=dict(context=ctx,denominator=den,relation='assigned_to',center_ref=aref(full,p,f['parent']['id']),direction='outgoing',scope_ref=scope['scope_ref'],registry_digest=REGISTRY_V1_DIGEST)
    global_request=build_relation_request(full,full.owner,**common)
    assert {o['task'] for x in global_request['owner_mapping'] for o in x['owners']}=={f['task_a']['id'],b['id']}
    edges=[];locals=[]
    for task in (f['task_a'],b):
        ref={'kind':'task_revision','project':p,'task':task['id'],'revision':task['revision'],'definition_digest':task_definition_digest(task['body'])}
        projection=project_task(den,ref)
        local=build_relation_request(full,full.owner,**common,projection=projection)
        assert {o['task'] for x in local['owner_mapping'] for o in x['owners']}=={task['id']}
        assert projection['global_digest']==den['digest']
        locals.append(local)
        # This is the existing Consumer-M owner proof boundary: requirement
        # identities come from sealed G, not saved acceptance aliases.
        edge=full.assurance.store_object(full.owner,p,'edge','shared-owner:'+task['id'],1,{'format':'assurance.edge.v1','project':p,'source_ref':common['center_ref'],'target_ref':ref,'relation':'assigned_to',
            'relation_contract_digest':REGISTRY_V1_DIGEST,'scope_ref':scope['scope_ref'],'claim':'Exact shared requirement assignment',
            'obligation_ids':global_request['required_obligation_ids'],
            'required_evidence_refs':[],'authority_refs':[]})
        edges.append(edge)
    from daikibo.assurance_node_reviews import build_node_requests
    nodes=select_node_reviews(full,full.owner,node_requests=build_node_requests(full,full.owner,project=p,selectors=[]))
    def coverage(request,selected_edges):
        result=evaluate_criteria(relation='assigned_to',requirements=sorted(SET_UNIVERSAL_CRITERIA|set(registry_entry('assigned_to',contract_digest=REGISTRY_V1_DIGEST)['set_checks'])),
            denominator=den,edges=selected_edges,validated_reviews=nodes,relation_request=request)
        return result['criteria']['all_requirements']
    before=_snapshot(full)
    assert coverage(global_request,edges[:1])['missing_ids']
    assert coverage(global_request,edges)['missing_ids']==[]
    assert coverage(locals[0],edges[:1])['missing_ids']==[]
    assert coverage(locals[0],edges[1:])['status']!='satisfied'
    assert _snapshot(full)==before
