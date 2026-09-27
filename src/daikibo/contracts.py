"""D03/D08: deliberately finite contract types and conservative compatibility checks.

This is a documented small type language, NOT a complete JSON Schema implementation.
Unsupported type rules fail closed; syntactic compatibility never proves semantic compatibility.
"""
from __future__ import annotations
import fnmatch
import math
from .common import Fault,canonical,digest,need,obj,parse_json,strings,text,timestamp,uid

FORMAT='daikibo.type.v1'
TYPES={'null','boolean','integer','number','string','array','object'}
KEYS={'type','enum','minimum','maximum','minLength','maxLength','items','properties','required','additionalProperties','description'}
def validate_type(schema,depth=0):
    need(depth<=32,'invalid_contract','Type schema exceeds nesting limit')
    need(isinstance(schema,dict) and set(schema)<=KEYS,'unsupported_contract_rule','Only documented daikibo.type.v1 rules are accepted')
    kind=schema.get('type');need(kind in TYPES,'invalid_contract','A supported explicit type is required')
    if 'description' in schema:text(schema['description'],'type description',10000)
    if kind=='object':
        need('properties' in schema and isinstance(schema['properties'],dict),'invalid_contract','Object properties must be explicit')
        need(type(schema.get('additionalProperties')) is bool,'invalid_contract','additionalProperties must be explicit')
        strings(schema.get('required',[]),'required properties')
        need(set(schema.get('required',[]))<=schema['properties'].keys(),'invalid_contract','Required property has no type')
        for key,child in schema['properties'].items():text(key,'property',1000);validate_type(child,depth+1)
    else:need(not {'properties','required','additionalProperties'} & schema.keys(),'invalid_contract','Object rules on a nonobject')
    if kind=='array':need('items' in schema,'invalid_contract','Array items required');validate_type(schema['items'],depth+1)
    else:need('items' not in schema,'invalid_contract','items applies only to arrays')
    for lo,hi,allowed in [('minimum','maximum',{'number','integer'}),('minLength','maxLength',{'string','array'})]:
        for name in (lo,hi):
            if name in schema:
                value=schema[name];need(kind in allowed and type(value) in (int,float) and (type(value) is int or math.isfinite(value)),'invalid_contract','Invalid type bound')
                if name.endswith('Length'):need(type(value) is int and value>=0,'invalid_contract','Length must be a nonnegative integer')
        if lo in schema and hi in schema:need(schema[lo]<=schema[hi],'invalid_contract','Contradictory bounds')
    if 'enum' in schema:
        need(isinstance(schema['enum'],list) and 0<len(schema['enum'])<=1000,'invalid_contract','enum must be finite and nonempty')
        for value in schema['enum']:
            reduced={k:v for k,v in schema.items() if k!='enum'};need(not violations(value,reduced),'invalid_contract','Enum value violates its own type')
    return schema

def violations(value,schema,path='$'):
    kind=schema['type'];valid={'null':value is None,'boolean':type(value) is bool,'integer':type(value) is int,'number':type(value) in (int,float) and (type(value) is int or math.isfinite(value)),'string':isinstance(value,str),'array':isinstance(value,list),'object':isinstance(value,dict)}[kind]
    if not valid:return [path+':wrong_type']
    errors=[]
    if 'enum' in schema and canonical(value) not in [canonical(v) for v in schema['enum']]:errors.append(path+':enum')
    if kind in {'integer','number'}:
        if 'minimum' in schema and value<schema['minimum']:errors.append(path+':minimum')
        if 'maximum' in schema and value>schema['maximum']:errors.append(path+':maximum')
    if kind in {'string','array'}:
        if len(value)<schema.get('minLength',0):errors.append(path+':minLength')
        if 'maxLength' in schema and len(value)>schema['maxLength']:errors.append(path+':maxLength')
    if kind=='array':
        for i,v in enumerate(value):errors+=violations(v,schema['items'],f'{path}[{i}]')
    if kind=='object':
        for required in schema.get('required',[]):
            if required not in value:errors.append(path+'.'+required+':missing')
        for name,v in value.items():
            if name in schema['properties']:errors+=violations(v,schema['properties'][name],path+'.'+name)
            elif not schema['additionalProperties']:errors.append(path+'.'+name+':extra')
    return errors

def subset(left,right,path='$'):
    """Can every value of left be accepted by right? Conservative proven subset only."""
    failures=[]
    if left['type']!=right['type'] and not (left['type']=='integer' and right['type']=='number'):return [path+':type_changed']
    if 'enum' in right:
        if 'enum' not in left:return [path+':enum_restricted']
        if not {canonical(x) for x in left['enum']}<={canonical(x) for x in right['enum']}:failures.append(path+':enum_restricted')
    for lo,hi in [('minimum','maximum'),('minLength','maxLength')]:
        if right.get(lo,-math.inf)>left.get(lo,-math.inf):failures.append(path+':lower_bound_restricted')
        if right.get(hi,math.inf)<left.get(hi,math.inf):failures.append(path+':upper_bound_restricted')
    if left['type']=='array':failures+=subset(left['items'],right['items'],path+'[]')
    if left['type']=='object':
        if not set(right.get('required',[]))<=set(left.get('required',[])):failures.append(path+':new_required_property')
        if left['additionalProperties'] and not right['additionalProperties']:failures.append(path+':additional_properties_restricted')
        for name,s in left['properties'].items():
            if name in right['properties']:failures+=subset(s,right['properties'][name],path+'.'+name)
            elif not right['additionalProperties']:failures.append(path+'.'+name+':removed_property')
        for name in right['properties'].keys()-left['properties'].keys():
            if left['additionalProperties']:failures.append(path+'.'+name+':previously_untyped_property_now_restricted')
    return failures

def compatibility(old,new):
    findings=[];unknown=[]
    for side in ('input','output'):
        a,b=old.get(side),new.get(side)
        if not (isinstance(a,dict) and a.get('format')==FORMAT and isinstance(b,dict) and b.get('format')==FORMAT):unknown.append(side+':no_machine_type');continue
        validate_type(a['schema']);validate_type(b['schema'])
        # Old callers must still be accepted; new responses must satisfy old consumers.
        findings += [side+':'+f for f in subset(a['schema'],b['schema'])] if side=='input' else [side+':'+f for f in subset(b['schema'],a['schema'])]
    semantic=[key for key in ('statement','authentication','errors','idempotency','compatibility','consumers','verification','standard_contract','standard_contract_material') if old.get(key)!=new.get(key)]
    return {'type_compatible_proven':not findings and not unknown,'breaking_candidates':findings,'unknown':unknown,'semantic_fields_changed':semantic,'semantic_review_required':True,'old_digest':digest(old),'new_digest':digest(new)}


def interface_impact_context(knowledge, project, deltas):
    """Bind a review to the actual registered consumer/test definitions.

    This is a conservative registry snapshot, not a complete dependency proof.
    Called under the store transaction alongside the review binding. Unknown
    consumer names stay explicit; undeclared consumers still require discovery.
    """
    from .common import Actor
    store = knowledge.s
    actor = Actor('controller', 'owner')
    result = []
    for delta in sorted(deltas, key=lambda d: d['artifact']):
        art = knowledge.artifact(actor, delta['artifact'])
        if art['kind'] != 'interface':
            continue
        links = store.all("SELECT source,target,relation,confidence,basis FROM links "
                          "WHERE target=? AND relation='consumes' ORDER BY source", (art['id'],))
        controls = delta['body'].get('change_control', {})
        strings(art['body'].get('consumers', []), 'current consumer identifiers')
        strings(delta['body'].get('consumers', []), 'proposed consumer identifiers')
        declared = set(art['body'].get('consumers', [])) | set(delta['body'].get('consumers', []))
        ids = declared | {r['source'] for r in links} | set(controls.get('verification_ids', []))
        records, unresolved = [], []
        for ident in sorted(ids):
            row = store.one('SELECT project FROM artifacts WHERE id=?', (ident,))
            if row is None:
                unresolved.append(ident)
                continue
            need(row['project'] == project, 'cross_project', 'Interface dependency belongs to another project')
            record = knowledge.artifact(actor, ident)
            records.append({key: record[key] for key in ('id','revision','digest','status','kind','body')})
        # Capture declared verifier links too. Updating links need not bump an
        # artifact revision, so checking the interface's revision alone is unsafe.
        trace_ids = sorted({art['id']} | ids)
        traces = []
        for start in range(0, len(trace_ids), 400):
            part = trace_ids[start:start+400]; placeholders = ','.join('?' for _ in part)
            traces.extend(store.all('SELECT source,target,relation,confidence,basis FROM links '
                                    f'WHERE source IN ({placeholders}) OR target IN ({placeholders})', (*part,*part)))
        traces = sorted({canonical(r).decode():r for r in traces}.values(), key=lambda r: canonical(r))
        result.append({'interface': art['id'], 'revision': art['revision'], 'digest': art['digest'],
                       'registered_consumer_links': links, 'declared_consumers': sorted(declared),
                       'records': records, 'trace_links': traces, 'unresolved_names': unresolved,
                       'verification_ids': controls.get('verification_ids', []),
                       'repository_order': controls.get('repository_order', []),
                       'unknown_consumers_possible': True, 'semantic_compatibility_proven': False})
    return result

class Contracts:
    def __init__(self,c):self.c=c;self.s=c.s
    def check_type(self,actor,schema,value):
        actor.require('owner','agent','reviewer','observer');validate_type(schema)
        errors=violations(value,schema);return {'format':FORMAT,'valid':not errors,'errors':errors,'semantic_conformance':'not evaluated'}
    def compare(self,actor,interface,proposed):
        art=self.c.k.artifact(actor,interface);need(art['kind']=='interface','wrong_kind','Not an interface');self.c.k.validate_body('interface',proposed)
        report=compatibility(art['body'],proposed)
        consumers=self.s.all("SELECT source,confidence,basis FROM links WHERE target=? AND relation='consumes'",(interface,))
        report.update({'interface':interface,'revision':art['revision'],'registered_consumers':consumers,'unknown_consumers_possible':True})
        with self.s.transaction():self.c.sec.event(art['project'],'contract_compatibility_assessed',actor.id,report)
        return report
    def architecture(self,actor,project,modules,rules):
        actor.require('owner','agent',project=project);self.c.k.project(actor,project)
        need(isinstance(modules,list) and modules,'invalid_architecture','Explicit modules required')
        owners={};issues=[];unknown=[]
        for module in modules:
            obj(module,required=('id','paths','imports'));strings(module['paths'],'paths',nonempty=True);strings(module['imports'],'import prefixes')
            need(module['id'] not in owners,'duplicate_module','Module ID repeats');owners[module['id']]=module
        allowed={}
        for rule in rules:
            obj(rule,required=('from','to'));need(rule['from'] in owners and rule['to'] in owners,'invalid_architecture','Unknown module');allowed.setdefault(rule['from'],set()).add(rule['to'])
        repoids=[r['id'] for r in self.s.all('SELECT id FROM repos WHERE project=?',(project,))]
        with self.c.idx.lock:
            for repo in repoids:
                for f in self.c.idx.db.execute('SELECT path,unknown FROM files WHERE repo=?',(repo,)):
                    found=[m['id'] for m in modules if any(fnmatch.fnmatchcase(f['path'],p) for p in m['paths'])]
                    if len(found)!=1:issues.append({'path':f['path'],'reason':'ambiguous_or_missing_owner','owners':found});continue
                    origin=found[0]
                    if parse_json(f['unknown']):unknown.append({'path':f['path'],'reason':parse_json(f['unknown'])})
                    for r in self.c.idx.db.execute("SELECT name,line FROM refs WHERE repo=? AND path=? AND kind='import_candidate'",(repo,f['path'])):
                        targets=[m['id'] for m in modules if any(r['name']==p or r['name'].startswith(p+'.') or r['name'].startswith(p+'/') for p in m['imports'])]
                        for target in targets:
                            if target!=origin and target not in allowed.get(origin,set()):issues.append({'path':f['path'],'line':r['line'],'from':origin,'to':target,'reason':'forbidden_import_candidate'})
        report={'issues':issues,'unknown':unknown,'declared_graph_passed':not issues,'full_semantic_dependency_proof':False,'specification_digest':digest({'modules':modules,'rules':rules})}
        with self.s.transaction():self.c.sec.event(project,'architecture_fitness_checked',actor.id,report)
        return report
