"""D03: immutable, bottom-up partial designs; only root gates authorize execution.

A subplan may reference canonical drafts before any root plan exists. Children
are input proposals, never copied requirement authorities. Composition reuses the
ordinary complete-scope Breakdowns validator and creates a PROPOSAL, not adoption.
"""
from __future__ import annotations

from collections import defaultdict

from .common import Fault, canonical, digest, need, obj, parse_json, strings, text, timestamp, uid
from .breakdowns import MAX_PROPOSAL_BYTES, ROLES, consistent_view, topological
from .packets import slices

FORMAT = 'daikibo.subplan.v1'
MAX_TREE = 10000
MAX_DEPTH = 32
SCHEMA = """
CREATE TABLE IF NOT EXISTS subplans(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS subplans_program ON subplans(program,created,id);
CREATE TABLE IF NOT EXISTS subplan_packets(
 id TEXT PRIMARY KEY, subplan TEXT NOT NULL REFERENCES subplans(id), project TEXT NOT NULL REFERENCES projects(id),
 ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 UNIQUE(subplan,ordinal)
);
CREATE TABLE IF NOT EXISTS subplan_compositions(
 id TEXT PRIMARY KEY, subplan TEXT NOT NULL REFERENCES subplans(id), project TEXT NOT NULL REFERENCES projects(id),
 breakdown TEXT NOT NULL REFERENCES breakdowns(id), request_digest TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(subplan,request_digest)
);
CREATE TRIGGER IF NOT EXISTS subplans_immutable BEFORE UPDATE ON subplans
 BEGIN SELECT RAISE(ABORT,'immutable partial design'); END;
CREATE TRIGGER IF NOT EXISTS subplans_no_delete BEFORE DELETE ON subplans
 BEGIN SELECT RAISE(ABORT,'retain partial design history'); END;
CREATE TRIGGER IF NOT EXISTS subplan_packets_immutable BEFORE UPDATE ON subplan_packets
 BEGIN SELECT RAISE(ABORT,'immutable partial review'); END;
CREATE TRIGGER IF NOT EXISTS subplan_packets_no_delete BEFORE DELETE ON subplan_packets
 BEGIN SELECT RAISE(ABORT,'retain partial review'); END;
CREATE TRIGGER IF NOT EXISTS subplan_compositions_immutable BEFORE UPDATE ON subplan_compositions
 BEGIN SELECT RAISE(ABORT,'immutable composition history'); END;
CREATE TRIGGER IF NOT EXISTS subplan_compositions_no_delete BEFORE DELETE ON subplan_compositions
 BEGIN SELECT RAISE(ABORT,'retain composition history'); END;
"""


def pairs(values):
    need(isinstance(values,list) and len(values)<=100000,'invalid_scope','Expected a bounded acceptance list')
    result=set()
    for value in values:
        obj(value,required=('requirement','acceptance'),name='partial obligation')
        text(value['requirement'],'requirement',200);text(value['acceptance'],'acceptance',4096)
        pair=(value['requirement'],value['acceptance'])
        need(pair not in result,'duplicate_obligation','An exact obligation appears twice',value)
        result.add(pair)
    return result


def pairlist(values):
    return [{'requirement':r,'acceptance':a} for r,a in sorted(values)]


def unit_id(subplan, local):
    return 'SPU-'+digest([subplan,local])


def aggregate(ident,title,parent=None):
    return {'id':ident,'title':title,'parent':parent,'domain':None,'rationale':'Aggregate child design inputs; no scope is discharged here.',
            'obligations':[],'tasks':[],'interfaces':[],'dependencies':[]}


class Subplans:
    def __init__(self, control):
        self.c,self.s=control,control.s
        # consistent_view uses the SAME single-operation cache as Breakdowns.
        self.local=control.breakdowns.local

    def _row(self,actor,subplan):
        row=self.s.one('SELECT * FROM subplans WHERE id=?',(subplan,),True)
        self.c.k.project(actor,row['project'])
        row['body']=parse_json(row['body'],limit=MAX_PROPOSAL_BYTES)
        need(digest(row['body'])==row['digest'],'integrity_error','Subplan body differs')
        return row

    def _tree(self,actor,subplan):
        root=self._row(actor,subplan);stack=[(root,0)];result=[];seen=set()
        while stack:
            row,depth=stack.pop()
            need(row['id'] not in seen,'duplicate_child','A child cannot be imported twice through different branches')
            need(depth<=MAX_DEPTH and len(seen)<MAX_TREE,'subplan_capacity','Partial planning hierarchy exceeds explicit limits')
            need((row['program'],row['project'])==(root['program'],root['project']),'cross_program','A child belongs to another program')
            seen.add(row['id']);result.append(row)
            for child in reversed(row['body']['children']):
                value=self._row(actor,child['id'])
                need(value['digest']==child['digest'],'integrity_error','Referenced child differs')
                stack.append((value,depth+1))
        return result

    def _task(self,actor,task):
        row=self.c.w.task(actor,task)
        need(row['status']!='cancelled','cancelled_dependency','A partial plan cannot discharge cancelled work',task)
        return self.c.breakdowns._task_definition(actor,task)

    def _artifact(self,actor,project,ident):
        row=self.c.breakdowns._artifact(actor,project,ident,allow_draft=True)
        # Draft -> accepted of unchanged content is a separate legitimate process.
        # It is not a content revision, and never performed by this module.
        need(digest(row['body'])==row['digest'],'integrity_error','Canonical artifact content differs',ident)
        return {k:v for k,v in row.items() if k!='status'}

    def _invariants(self,actor,project):
        result=[]
        for item in self.s.all("SELECT id,body FROM artifacts WHERE project=? AND status='accepted' ORDER BY id",(project,)):
            body=parse_json(item['body'])
            if body.get('constraints') or body.get('critical'):
                result.append(self._artifact(actor,project,item['id']))
        return result

    def _material(self,actor,project,body,ensure_policy=True):
        task_ids=sorted({task for u in body['units'] for task in u['tasks']})
        tasks=[self._task(actor,t) for t in task_ids]
        external_ids=sorted({dep for t in tasks for dep in t['dependencies']}-set(task_ids))
        external=[self._task(actor,t) for t in external_ids]
        need(all(self.c.w.task(actor,t['id'])['project']==project for t in external),'cross_project','External task belongs elsewhere')
        refs=set(body['context_artifacts'])
        for unit in body['units']:
            refs.update([unit['domain']] if unit['domain'] else [])
            refs.update(unit['interfaces']);refs.update(o['requirement'] for o in unit['obligations'])
        for task in tasks+external:refs.update(task['body']['read_artifacts'])
        artifacts=[self._artifact(actor,project,x) for x in sorted(refs)]
        # Relations can change without an artifact revision. Keep those inputs
        # bound too, rather than reusing a trace review of a different graph.
        linked={};ordered=sorted(refs)
        for start in range(0,len(ordered),400):
            batch=ordered[start:start+400];marks=','.join('?' for _ in batch)
            for link in self.s.all(f'SELECT source,target,relation,confidence,basis FROM links WHERE source IN ({marks}) OR target IN ({marks})',(*batch,*batch)):
                linked[(link['source'],link['target'],link['relation'])]=link
        sources=[]
        for source in sorted({s for a in artifacts for s in a['body'].get('source_refs',[])}):
            row=self.s.one('SELECT id,project,blob,locator,characters FROM sources WHERE id=?',(source,),True)
            need(row['project']==project,'cross_project','Source belongs elsewhere')
            sources.append(row)
        return {'format':FORMAT,'program':body['program'],'title':body['title'],'rationale':body['rationale'],
                'children':body['children'],'units':body['units'],'obligations':body['obligations'],'boundary_contracts':body['boundary_contracts'],
                'artifacts':artifacts,'tasks':tasks,'external_tasks':external,'source_index':sources,
                'trace_links':[v for _,v in sorted(linked.items())],
                'global_constraints':self._invariants(actor,project),
                'policy':self.c.g.policy(project, create=ensure_policy)['digest'],
                'instructions':'Partial design only. Parent obligations remain mandatory. Inspect requirements, alternatives, actual dependencies, external prerequisites, contracts and test adequacy. Read raw sources/child packets by ID when needed. A local pass is not whole-system acceptance.'}

    def _scope(self,actor,project,obligations,units,ensure_policy=True):
        grouped=defaultdict(list)
        for requirement,ac in pairs(obligations):grouped[requirement].append(ac)
        requirements=[]
        for ident,acs in sorted(grouped.items()):
            art=self._artifact(actor,project,ident)
            need(art['kind']=='requirement' and set(acs)<=set(art['body']['acceptance']),
                 'invalid_scope','An obligation must exist in its canonical requirement',ident)
            requirements.append({k:art[k] for k in ('id','revision','digest')}|{'acceptance':sorted(acs)})
        ids=[t for u in units for t in u['tasks']]
        need(len(ids)==len(set(ids)),'duplicate_task','No task can be owned by multiple partial units')
        return {'requirements':requirements,'tasks':[{'id':t,'definition_digest':digest(self._task(actor,t))} for t in sorted(ids)],
                'policy':self.c.g.policy(project, create=ensure_policy)['digest']}

    def _dependencies(self,actor,units,boundary_contracts):
        """Derive edges from actual tasks, not a second schedule. No ambiguous guess."""
        owners={t:u for u in units for t in u['tasks']};edges={u['id']:{} for u in units}
        declared={}
        for choice in boundary_contracts:
            obj(choice,required=('task','dependency','interface'),name='boundary choice')
            for key in choice:text(choice[key],key,200)
            pair=(choice['task'],choice['dependency'])
            need(pair not in declared,'duplicate_boundary_choice','Boundary choice repeats')
            need(choice['task'] in owners and choice['dependency'] in self._task(actor,choice['task'])['dependencies'],
                 'invalid_boundary_choice','Choice must name an actual dependency from an owned Task')
            declared[pair]=choice['interface']
        for task,owner in owners.items():
            definition=self._task(actor,task)
            for dep in definition['dependencies']:
                if dep not in owners or owners[dep]['id']==owner['id']:continue
                target=owners[dep]
                shared=set(owner['interfaces']) & set(target['interfaces'])
                shared &= set(definition['body']['read_artifacts']) & set(self._task(actor,dep)['body']['read_artifacts'])
                contract=declared.get((task,dep))
                if contract:
                    need(contract in shared,'missing_boundary_contract','Chosen contract must be read and declared by both sides')
                elif owner['domain']!=target['domain']:
                    need(len(shared)==1,'ambiguous_boundary_contract' if shared else 'missing_boundary_contract',
                         'Dependent cross-domain tasks need one explicit shared contract; do not guess',{'task':task,'dependency':dep,'candidates':sorted(shared)})
                    contract=next(iter(shared))
                existing=edges[owner['id']].get(target['id'])
                need(existing is None or existing['interface']==contract,'ambiguous_boundary_contract',
                     'One unit edge cannot silently merge distinct contracts; subdivide the units')
                edges[owner['id']][target['id']]={'unit':target['id'],'interface':contract}
        return [{**u,'dependencies':[edge for _,edge in sorted(edges[u['id']].items())]} for u in units]

    @consistent_view
    def propose(self,actor,program,title,rationale,obligations,units,children=None,context_artifacts=None,byte_budget=24000,boundary_contracts=None):
        flow=self.c.breakdowns._program(actor,program);project=flow['project'];actor.require('owner','agent',project=project)
        text(title,'partial plan title',400);text(rationale,'rationale',20000)
        need(type(byte_budget)is int and 4096<=byte_budget<=100000,'invalid_budget','Packet budget must be 4096..100000 bytes')
        children=[] if children is None else children;context_artifacts=[] if context_artifacts is None else context_artifacts
        boundary_contracts=[] if boundary_contracts is None else parse_json(canonical(boundary_contracts))
        need(isinstance(boundary_contracts,list) and len(boundary_contracts)<=10000,'invalid_boundary_choice','Supply bounded explicit edge choices')
        strings(children,'children');strings(context_artifacts,'context artifacts')
        need(isinstance(units,list) and len(units)<=10000 and (units or children),'invalid_subplan','Supply local units or child plans')
        requested=pairs(obligations);need(requested,'empty_scope','A partial plan needs an explicit obligation scope')
        own=uid('SUBPLAN');combined=[aggregate(unit_id(own,'__group__'),title)];child_refs=[];seen=set();contexts=set(context_artifacts)
        for child in children:
            tree=self._tree(actor,child);childrow=tree[0]
            need(childrow['project']==project and childrow['program']==program,'cross_program','Child belongs elsewhere')
            need(not seen & {r['id'] for r in tree},'duplicate_child','The same child appears through multiple branches')
            seen.update(r['id'] for r in tree)
            self._check_current(actor,childrow)
            child_refs.append({'id':child,'digest':childrow['digest']})
            contexts.update(childrow['body']['context_artifacts'])
            for choice in childrow['body']['boundary_contracts']:
                if choice not in boundary_contracts:boundary_contracts.append(choice)
            for unit in childrow['body']['units']:
                combined.append({**unit,'parent':unit_id(own,'__group__') if unit['parent'] is None else unit['parent']})
        need(len(seen)<MAX_TREE,'subplan_capacity','Too many descendants')
        localids=set()
        for unit in units:
            obj(unit,required=('id','title','parent','domain','rationale','obligations','tasks','interfaces'),name='partial unit')
            text(unit['id'],'local unit',120)
            if unit['parent'] is not None:text(unit['parent'],'parent unit',120)
            if unit['domain'] is not None:text(unit['domain'],'domain',200)
            need(unit['id']!='__group__' and unit['id'] not in localids,'duplicate_unit','Duplicate/reserved local unit')
            localids.add(unit['id'])
            strings(unit['tasks'],'unit tasks');strings(unit['interfaces'],'unit interfaces');pairs(unit['obligations'])
        for unit in units:
            need(unit['parent'] is None or unit['parent'] in localids,'unknown_unit','Local parent must name a local unit')
            combined.append({**unit,'id':unit_id(own,unit['id']),
                             'parent':unit_id(own,unit['parent'] if unit['parent'] is not None else '__group__'),'dependencies':[]})
        # Deep-copy before validation so caller-owned lists cannot mutate retained inputs.
        combined=parse_json(canonical(combined),limit=MAX_PROPOSAL_BYTES)
        # Detect duplicate tasks before deriving a dictionary of task owners.
        tasks=[t for u in combined for t in u['tasks']]
        need(len(tasks)==len(set(tasks)),'duplicate_task','The same Task is present in multiple branches')
        combined=self._dependencies(actor,combined,boundary_contracts)
        body={'format':FORMAT,'program':program,'title':title,'rationale':rationale,'children':child_refs,
              'units':combined,'obligations':pairlist(requested),'context_artifacts':sorted(contexts),'boundary_contracts':boundary_contracts}
        scope=self._scope(actor,project,body['obligations'],combined)
        checks=self.c.breakdowns._validate(actor,project,combined,scope,allow_drafts=True,allow_external=True)
        material=self._material(actor,project,body);serialized=canonical(material).decode();h=digest(material)
        need(len(serialized.encode())<=MAX_PROPOSAL_BYTES,'subplan_capacity','Complete draft material exceeds 128MiB; never truncate it')
        body['material_digest']=h;body['structure']=checks;body['packet_manifest']=[]
        packets=[]
        for ordinal,(start,end,fragment) in enumerate(slices(serialized,(byte_budget-1800)//2)):
            packet={'format':'daikibo.subplan-review.v1','subplan':own,'program':program,'material_digest':h,
                    'start':start,'end':end,'total_characters':len(serialized),'serialized_fragment':fragment,
                    'required_coverage':['SPART-'+digest([own,h,start,end])],
                    'instructions':'Review the exact partial-plan fragment. Query subplan.packet/get and child plans for context. Design and trace are separate observed runs; report blocked for missing information. Partial PASS does not adopt artifacts, authorize execution or complete the project.'}
            encoded=canonical(packet);need(len(encoded)<=byte_budget,'context_insufficient','Packet metadata exceeds budget')
            pid=uid('SPACK');packets.append((pid,own,project,ordinal,encoded.decode(),digest(packet)))
            body['packet_manifest'].append({'id':pid,'digest':digest(packet)})
        encoded=canonical(body);need(len(encoded)<=MAX_PROPOSAL_BYTES,'subplan_capacity','Draft registry too large')
        self.s.execute('INSERT INTO subplans VALUES(?,?,?,?,?,?)',(own,project,program,encoded.decode(),digest(body),timestamp()))
        for packet in packets:self.s.execute('INSERT INTO subplan_packets VALUES(?,?,?,?,?,?)',packet)
        # Validate actual combined depth (local unit nesting and child proposal ancestry).
        self._tree(actor,own)
        self.c.sec.event(project,'subplan_proposed',actor.id,{'subplan':own,'program':program,'children':children,'root_adopted':False})
        return self.get(actor,own)

    def _check_current(self,actor,row,ensure_policy=True):
        actual=self._material(actor,row['project'],row['body'],ensure_policy=ensure_policy)
        need(digest(actual)==row['body']['material_digest'],'stale_subplan','Referenced draft, contract, task, external prerequisite or global constraint changed',row['id'])
        return actual

    def get(self,actor,subplan,offset=0,limit=50):
        need(type(offset)is int and offset>=0 and type(limit)is int and 1<=limit<=200,'invalid_range','Use a bounded packet page')
        row=self._row(actor,subplan);b=row['body'];manifest=b['packet_manifest'];page=manifest[offset:offset+limit]
        return {'id':subplan,'program':row['program'],'title':b['title'],'digest':row['digest'],
                'children':b['children'],'obligation_count':len(b['obligations']),'task_count':b['structure']['task_count'],
                'packet_count':len(manifest),'packets':page,'next_offset':offset+len(page) if offset+len(page)<len(manifest) else None,
                'root_adopted':False,'deploy_ready':False,'state':'partial_design_proposal'}

    def list(self,actor,program,offset=0,limit=50):
        flow=self.c.breakdowns._program(actor,program)
        need(type(offset)is int and offset>=0 and type(limit)is int and 1<=limit<=200,'invalid_range','Use a bounded plan page')
        rows=self.s.all("SELECT id,digest,created,json_extract(body,'$.title') AS title FROM subplans WHERE program=? ORDER BY created,id LIMIT ? OFFSET ?",(program,limit+1,offset))
        total=self.s.one('SELECT count(*) AS n FROM subplans WHERE program=?',(program,))['n']
        return {'program':program,'project':flow['project'],'items':rows[:limit],'total':total,'next_offset':offset+limit if offset+limit<total else None}

    @consistent_view
    def packet(self,actor,packet):
        row=self.s.one('SELECT * FROM subplan_packets WHERE id=?',(packet,),True);plan=self._row(actor,row['subplan'])
        material=self._check_current(actor,plan);p=parse_json(row['body']);h=digest(p)
        need(h==row['digest'] and {'id':packet,'digest':h} in plan['body']['packet_manifest'],'integrity_error','Partial review packet differs')
        serialized=canonical(material).decode()
        need(p['subplan']==plan['id'] and p['program']==plan['program'] and p['material_digest']==plan['body']['material_digest']
             and p['total_characters']==len(serialized) and serialized[p['start']:p['end']]==p['serialized_fragment'],
             'integrity_error','Partial review fragment differs from current source')
        return {**row,'body':p}

    def review_subject(self,actor,packet,role):
        need(role in ROLES,'invalid_role','Partial plans require design and trace review')
        row=self.packet(actor,packet);empty={'format':'snapshot.v1','repos':{},'digest':digest({'repos':{}})}
        return row['project'],row['digest'],empty,row['body'],None

    @consistent_view
    def _audit(self,actor,subplan,reviews=True,readonly=False):
        need(type(reviews)is bool,'invalid_option','reviews must be boolean')
        tree=self._tree(actor,subplan);failures=[];verified=[]
        for row in tree:
            try:
                self._check_current(actor,row,ensure_policy=not readonly)
                scope=self._scope(actor,row['project'],row['body']['obligations'],row['body']['units'],
                                  ensure_policy=not readonly)
                self.c.breakdowns._validate(actor,row['project'],row['body']['units'],scope,allow_drafts=True,allow_external=True)
            except Fault as exc:failures.append({'subplan':row['id'],**exc.as_dict()})
            actual=self.s.all('SELECT * FROM subplan_packets WHERE subplan=? ORDER BY ordinal',(row['id'],))
            if [{'id':p['id'],'digest':p['digest']} for p in actual]!=row['body']['packet_manifest']:
                failures.append({'subplan':row['id'],'code':'packet_manifest_mismatch'})
            cursor=0;fragments=[]
            for ordinal,p in enumerate(actual):
                try:
                    packet=parse_json(p['body'])
                    need(p['ordinal']==ordinal and digest(packet)==p['digest'] and packet['subplan']==row['id']
                         and packet['program']==row['program'] and packet['material_digest']==row['body']['material_digest']
                         and packet['start']==cursor and packet['end']==cursor+len(packet['serialized_fragment']),
                         'integrity_error','Incomplete or changed partial-plan fragment')
                    cursor=packet['end'];fragments.append(packet['serialized_fragment'])
                    need(packet['required_coverage']==['SPART-'+digest([row['id'],row['body']['material_digest'],packet['start'],packet['end']])],
                         'integrity_error','Review coverage marker differs')
                    if reviews:
                        runs=set()
                        for role in ROLES:
                            refs=self.c.g.evidence_for(p['id'],p['digest'],role)
                            need(refs,'review_required','Missing observed partial-plan review',role)
                            ev=self.c.g.require_review(refs[0]['id'],p['id'],p['digest'],{role})
                            need(set(packet['required_coverage'])<=set(ev['result']['covered']),'review_coverage','This exact fragment was not covered')
                            need(ev['run'] not in runs,'independent_review','Use distinct review executions')
                            runs.add(ev['run']);verified.append({'subplan':row['id'],'packet':p['id'],'role':role,'receipt':ev['id']})
                except Fault as exc:failures.append({'packet':p['id'],**exc.as_dict()})
            if not actual or any(parse_json(p['body'])['total_characters']!=cursor for p in actual) or digest(''.join(fragments).encode())!=row['body']['material_digest']:
                failures.append({'subplan':row['id'],'code':'missing_review_fragments'})
        return {'subplan':subplan,'current':not failures,'failures':failures,'reviewed':verified,'review_checks_performed':reviews,
                'plans_checked':len(tree),'deploy_ready':False,'semantic_correctness_guaranteed':False}

    def audit(self,actor,subplan,reviews=True,offset=0,limit=100,readonly=False):
        need(type(offset)is int and offset>=0 and type(limit)is int and 1<=limit<=500,'invalid_range','Use bounded audit pages')
        report=self._audit(actor,subplan,reviews,readonly=readonly)
        failures=report['failures'];verified=report['reviewed']
        return {**report,'failures':failures[offset:offset+limit],'reviewed':verified[offset:offset+limit],
                'failure_count':len(failures),'reviewed_count':len(verified),
                'next_failure_offset':offset+limit if offset+limit<len(failures) else None,
                'next_review_offset':offset+limit if offset+limit<len(verified) else None}

    @consistent_view
    def coverage(self,actor,subplan,offset=0,limit=100):
        need(type(offset)is int and offset>=0 and type(limit)is int and 1<=limit<=500,'invalid_range','Use a bounded residual page')
        row=self._row(actor,subplan);project=row['project'];local=pairs(row['body']['obligations'])
        current=self.c.breakdowns._scope(actor,project)
        required={(r['id'],ac) for r in current['requirements'] for ac in r['acceptance']}
        pending=sorted(required-local);local_tasks={t for u in row['body']['units'] for t in u['tasks']}
        tasks=sorted(t['id'] for t in current['tasks'] if t['id'] not in local_tasks)
        return {'subplan':subplan,'project_obligation_count':len(required),'declared_obligation_count':len(local),
                'unassigned_obligations':pairlist(pending[offset:offset+limit]),'unassigned_obligation_count':len(pending),
                'next_offset':offset+limit if offset+limit<len(pending) else None,
                'unassigned_tasks':tasks[offset:offset+limit],'unassigned_task_count':len(tasks),
                'next_task_offset':offset+limit if offset+limit<len(tasks) else None,
                'draft_or_not_current_pairs':pairlist(sorted(local-required)[offset:offset+limit]),
                'draft_or_not_current_pair_count':len(local-required),
                'next_draft_offset':offset+limit if offset+limit<len(local-required) else None,'deploy_ready':False,
                'note':'Coverage counts are not reviews; unassigned parent obligations remain required. Read the current audit before using a stale draft.'}

    @consistent_view
    def compose(self,actor,subplan,expected_active=None,byte_budget=24000):
        row=self._row(actor,subplan);actor.require('owner','agent',project=row['project'])
        need(type(byte_budget)is int and 4096<=byte_budget<=100000,'invalid_budget','Root review packet budget must be 4096..100000')
        request=digest({'expected_active':expected_active,'byte_budget':byte_budget})
        prior=self.s.one('SELECT * FROM subplan_compositions WHERE subplan=? AND request_digest=?',(subplan,request))
        audit=self._audit(actor,subplan)
        need(audit['current'],'subplan_gate_denied','All current child and parent reviews must exist',{'failures':audit['failures'][:100],'total':len(audit['failures']),'read_operation':'subplan.audit'})
        # Mandatory normal adoption of canonical drafts: no private copy and no auto-approval.
        material=self._check_current(actor,row)
        for item in material['artifacts']:
            need(self.c.k.artifact(actor,item['id'])['status']=='accepted','unaccepted_input',
                 'Adopt canonical artifacts through their normal reviews/changes before root composition',item['id'])
        if prior:
            result=parse_json(prior['body'],limit=MAX_PROPOSAL_BYTES)
            need(digest(result)==prior['digest'],'integrity_error','Composition record differs')
            b=self.c.breakdowns._row(actor,prior['breakdown'])
            current=self.c.breakdowns.audit(actor,b['id'],reviews=False)
            need(b['status'] in {'proposed','active'} and current['current'],'stale_composition','Previously composed root is stale; make an explicit new partial plan')
            return {'subplan':subplan,'breakdown':b['id'],'composition':prior['id'],'replayed':True,'root_adopted':False,'deploy_ready':False}
        result=self.c.breakdowns.propose(actor,row['program'],row['body']['title'],row['body']['rationale'],
                                        row['body']['units'],expected_active,byte_budget,origin_subplan=subplan)
        ident=uid('COMPOSE')
        body={'subplan':subplan,'subplan_digest':row['digest'],'breakdown':result['id'],'request':{'expected_active':expected_active,'byte_budget':byte_budget},
              'reviews':audit['reviewed'],'units_digest':digest(row['body']['units']),'created_by':actor.id,
              'root_adopted':False,'deploy_ready':False,'tasks_created':[],'artifacts_accepted':[]}
        self.s.execute('INSERT INTO subplan_compositions VALUES(?,?,?,?,?,?,?,?)',
                       (ident,subplan,row['project'],result['id'],request,canonical(body).decode(),digest(body),timestamp()))
        self.c.sec.event(row['project'],'subplan_composed',actor.id,{'subplan':subplan,'breakdown':result['id'],'composition':ident,'adopted':False})
        return {'subplan':subplan,'breakdown':result['id'],'composition':ident,'replayed':False,'root_adopted':False,'deploy_ready':False,
                'next_operation':'Review every root breakdown packet with separate design/trace executions, then breakdown.activate; phase and delivery gates remain mandatory.'}
