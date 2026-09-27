import base64,copy,json,sqlite3,sys
from pathlib import Path
import pytest
from daikibo.common import Actor,Fault,canonical,digest
from daikibo.contracts import validate_type,violations,compatibility,FORMAT,subset
from conftest import make_task

@pytest.mark.parametrize('schema',[{'type':'string','pattern':'evil'}, {'type':'object','properties':{}}, {'type':'array'}, {'type':'integer','minimum':True}, {'type':'string','minLength':3,'maxLength':2}, {'type':'object','properties':{},'required':['missing'],'additionalProperties':False}])
def test_invalid_finite_schema_is_rejected(schema):
    with pytest.raises(Fault):validate_type(schema)

@pytest.mark.parametrize('value,errors',[(True,True),(1,False),(0,True),(3,True),('1',True)])
def test_finite_contract_exact_types_and_bounds(value,errors):
    schema={'type':'integer','minimum':1,'maximum':2};validate_type(schema)
    assert bool(violations(value,schema)) is errors

def typed(s):return {'format':FORMAT,'schema':s}
def contract(input,output):return {'title':'C','statement':'Contract','input':typed(input),'output':typed(output),'authentication':'none','errors':[],'idempotency':'read','compatibility':'v1','consumers':[],'verification':['TC-1']}

def test_contract_compatibility_variance():
    old=contract({'type':'integer','minimum':0},{'type':'number'})
    new=contract({'type':'number'},{'type':'integer'})
    assert compatibility(old,new)['type_compatible_proven']
    assert compatibility(new,old)['breaking_candidates']
    new['statement']='Result now means a different physical unit'
    assert compatibility(old,new)['semantic_review_required'] and 'statement' in compatibility(old,new)['semantic_fields_changed']

def test_unknown_contract_not_claimed_compatible():
    result=compatibility({'input':'plain English','output':'English'},{'input':'English','output':'English'})
    assert not result['type_compatible_proven'] and result['unknown']

def test_object_compatibility_checks_optional_and_required_fields():
    a={'type':'object','properties':{'x':{'type':'integer'}},'required':['x'],'additionalProperties':False}
    b=copy.deepcopy(a);b['properties']['y']={'type':'string'}
    assert not subset(a,b)
    b['required'].append('y');assert subset(a,b)
    assert violations({'x':True},a) and violations({'x':1,'oops':2},a)
    assert not violations({'x':1},a)

def test_document_exact_bytes_and_unknowns(full,full_project):
    c=full;p=full_project[0];raw=b'\x00\xffunknown binary\n'
    doc=c.documents.register(c.owner,p,base64.b64encode(raw).decode(),'upload.bin')
    assert c.s.blob_get(doc['raw_digest'])==raw and doc['unknown']
    assert not c.k.source_coverage(c.owner,p)['structurally_complete']
    agent=Actor('test-agent','agent',p)
    with pytest.raises(Fault):c.documents.attach_text(agent,doc['id'],doc['raw_digest'],'translation','not authorized')
    source=c.documents.attach_text(c.owner,doc['id'],doc['raw_digest'],'Verified extraction','Owner checked exact original')
    assert c.k.source_read(c.owner,source['id'])['content']=='Verified extraction'
    assert c.documents.get(c.owner,doc['id'])['body']['raw_digest']==digest(raw)

@pytest.mark.parametrize('mime,value',[('text/markdown','# original\n本文'),('application/yaml','key: value\n'),('application/json','{"requirement":"x"}'),('text/x-python','print("hello")\n')])
def test_text_document_roundtrip(full,full_project,mime,value):
    c=full;raw=value.encode();d=c.documents.register(c.owner,full_project[0],base64.b64encode(raw).decode(),'input',mime)
    assert not d['unknown'] and c.s.blob_get(d['raw_digest'])==raw
    assert c.k.source_read(c.owner,d['text_source']['id'])['content']==value

def test_malformed_json_preserved_not_discarded(full,full_project):
    c=full;raw=b'{"duplicate":1,"duplicate":2}'
    d=c.documents.register(c.owner,full_project[0],base64.b64encode(raw).decode(),'bad.json','application/json')
    assert d['unknown'] and c.s.blob_get(d['raw_digest'])==raw

def test_context_budget_is_strict_not_plus_hidden_metadata(full,full_project):
    c=full;t=make_task(c,full_project)
    for budget in (1000,2000,4000,16000):
        try:package=c.ctx.task_context(c.owner,t,byte_budget=budget)
        except Fault as exc:assert exc.code=='context_insufficient'
        else:assert len(canonical(package['package']))<=budget

def test_architecture_forbidden_import_candidate(full,full_project):
    c=full;p,r,q,root=full_project
    (root/'a.py').write_text('from payment import charge\ncharge()\n');c.idx.index(c.owner,r)
    modules=[{'id':'ui','paths':['a.py','calc.py','test_calc.py'],'imports':['ui']},{'id':'payment','paths':['payment/*.py'],'imports':['payment']}]
    check=c.contracts.architecture(c.owner,p,modules,[])
    assert any(x.get('reason')=='forbidden_import_candidate' for x in check['issues'])
    check=c.contracts.architecture(c.owner,p,modules,[{'from':'ui','to':'payment'}])
    assert check['declared_graph_passed'] and not check['full_semantic_dependency_proof']

def test_bounded_phase_review_requires_all_real_packet_invocations(full,full_project):
    c=full;p,r,q,root=full_project;src=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    program=c.p.begin(c.owner,p,src)['program']
    for i in range(10):c.k.propose(c.owner,p,'design',{'title':f'Design {i}','statement':'Detail '*350})
    scopes=c.scopes.partition(c.owner,program,byte_budget=10000)
    assert len(scopes['packets'])>=3
    assert not c.scopes.summary(c.owner,program)['complete']
    for packet in scopes['packets']:
        assert packet['bytes']<=10000
        c.rt.review(c.owner,packet['id'],'phase','fixture')
    assert c.scopes.summary(c.owner,program)['complete']
    c.k.propose(c.owner,p,'design',{'title':'new','statement':'A new mandatory input'})
    assert not c.scopes.summary(c.owner,program)['complete']

def test_analysis_label_cannot_bypass_production_test_gate(full,full_project):
    c=full;p,r,q,root=full_project
    with pytest.raises(Fault) as exc:
        c.w.create(c.owner,p,{'title':'Pretend analysis','goal':'Edit production without measured tests','read_artifacts':[q],'write_paths':['calc.py'],'acceptance':['AC-ADD'],'dependencies':[],'repos':[r],'non_goals':[],'phase':'feasibility'})
    assert exc.value.code=='analysis_scope_violation'

def test_rejected_decision_releases_block_but_requires_reassessment(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project)
    d=c.p.propose_decision(c.owner,p,{'title':'Change product','reason':'Candidate','options':['approve','reject'],'recommendation':'reject','refs':[q],'requirement_affecting':True})
    c.p.respond(c.owner,d['id'],d['digest'],'reject','Keep existing semantics')
    assert not c.s.one("SELECT task FROM blocks WHERE kind='decision' AND ref=?",(d['id'],))
    assert c.w.task(c.owner,t)['validity']=='needs_review'

def test_checkpoint_v1_additive_migration_keeps_backup(tmp_path):
    from daikibo.db import Store,SCHEMA_VERSION
    h=tmp_path/'old';s=Store(h);s.execute('DROP TABLE program_origins');s.execute('DROP TABLE review_scopes');s.execute('DROP TABLE documents');s.execute('PRAGMA user_version=1');s.close()
    s=Store(h)
    try:
        assert s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
        assert (h/'pre-migration-v1.sqlite3').exists()
        assert s.one("SELECT name FROM sqlite_master WHERE name='documents'")
    finally:s.close()
