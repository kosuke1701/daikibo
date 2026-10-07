"""D03 — executable design phases, changes, escalation and contradiction handling."""
from __future__ import annotations
from .common import Actor, Fault, canonical, digest, need, obj, parse_json, strings, text, timestamp, uid

PHASES = ['requirements','scenarios','boundaries','contracts','feasibility','design','plan','implementation','integration','delivery']
PHASE_KINDS = {'requirements':'requirement','scenarios':'scenario','boundaries':'domain','contracts':'interface','feasibility':'finding','design':'design'}
PHASE_ACTIONS = {
    'requirements':'Extract source-grounded atomic requirements and acceptance IDs. Classify every source span; do not silently summarize away requirements. Review and adopt artifacts.',
    'scenarios':'Describe end-to-end success, failure and concurrent user journeys, including measurable nonfunctional conditions.',
    'boundaries':'Allocate cohesive responsibilities, nonresponsibilities, invariants and unique data ownership. Logical modules are not necessarily separate deployments.',
    'contracts':'Specify inputs, outputs, errors, authentication, idempotency, compatibility, consumers and verification for every boundary.',
    'feasibility':'Register hypotheses, bounded experiments, observed results and remaining unknowns. Escalate code -> module -> system before product changes.',
    'design':'For independent partial designs before root adoption, use subplan.propose with child IDs and canonical drafts, review all packets and subplan.compose into a complete root proposal after normal artifact adoption. Link reviewed design and component choices to requirements. Record rejected alternatives, rollout and failure handling.',
    'plan':'Use breakdown.propose (or upload_begin / bounded upload_put / upload_finalize) with all requirement/acceptance pairs and all noncancelled tasks; review each bounded packet using actual design and trace runs, then breakdown.activate. Create independently verifiable bounded tasks, typed dependencies, read versions, write scopes, fixed checks and acceptance mapping. Preserve complete release scope.',
    'implementation':'Claim eligible tasks, execute in managed workspaces, collect actual tests and separate spec/quality/test-adequacy reviews. Complete only through gates.',
    'integration':'Freeze integrated multi-repository snapshots, run delivery profile checks and whole-change/goal-validation reviews. Task passes alone are insufficient.',
    'delivery':'Certify exact checked snapshots and commit or create PRs. Do not deploy automatically or call unverified code deploy-ready.',
}

class Planning:
    def __init__(self,store,security,knowledge,governance,workflow):
        self.s,self.sec,self.k,self.g,self.w=store,security,knowledge,governance,workflow;self.scopes=None;self.indexer=None;self.breakdowns=None

    def begin(self,actor,project,source,mode='auto',compact=False):
        actor.require('owner','agent',project=project); self.k.project(actor,project)
        need(type(compact) is bool,'invalid_option','compact must be boolean')
        src=self.s.one("SELECT project FROM sources WHERE id=?",(source,),True)
        need(src['project']==project,'cross_project','Source belongs elsewhere')
        need(mode in {'auto','greenfield','brownfield'},'invalid_mode','Unknown development mode')
        repos=self.s.all("SELECT id,path FROM repos WHERE project=?",(project,))
        if mode=='auto': mode='brownfield' if repos else 'greenfield'
        ident=uid('FLOW')
        body={'source':source,'mode':mode,'scope_rule':'all accepted requirements remain in release scope','history':[]}
        with self.s.transaction():
            self.s.execute("INSERT INTO programs VALUES(?,?,?,1,?,?)",(ident,project,PHASES[0],canonical(body).decode(),timestamp()))
            from .program_origins import begin_body, insert_origin
            insert_origin(self.s, begin_body(ident, project))
            self.sec.event(project,'program_started',actor.id,{'program':ident,'mode':mode})
        if compact:
            return {'program':ident,'project':project,'phase':PHASES[0],'revision':1,'details_operation':'program.blockers'}
        return self.next(actor,ident)

    def program_binding(self,program):
        row=self.s.one("SELECT * FROM programs WHERE id=?",(program,),True)
        from .program_origins import resolve_program_origin
        origin=resolve_program_origin(self.s, project=row['project'], program=program)
        assurance=getattr(self,'assurance',None)
        if assurance is None:
            control=getattr(self,'control',None)
            assurance=getattr(control,'assurance',None)
        selection=None
        if assurance is not None and hasattr(assurance,'selected_profile'):
            selection=assurance.selected_profile(Actor('system','owner'),row['project'],program)
        return digest({'id':program,'phase':row['phase'],'revision':row['revision'],
                       'artifacts':self.s.all("SELECT id,revision,digest,status FROM artifacts WHERE project=? ORDER BY id",(row['project'],)),
                       'tasks':self.s.all("SELECT id,revision,status,validity,candidate FROM tasks WHERE project=? ORDER BY id",(row['project'],)),
                       'knowledge_epoch':self.k.progress_epoch(row['project']),
                       'workstreams':self.s.all('SELECT id,digest,status FROM workstreams WHERE program=? ORDER BY id',(program,)),
                       'scope_records':self.s.all('SELECT r.id,r.digest FROM workstream_records r JOIN workstreams w ON w.id=r.scope WHERE w.program=? ORDER BY r.id',(program,)),
                       'profile':self.s.one('SELECT digest FROM profiles WHERE project=?',(row['project'],)),
                       'deliveries':self.s.all('SELECT id,digest,status FROM deliveries WHERE project=? ORDER BY id',(row['project'],)),
                       'breakdown':self.s.one("SELECT id,digest FROM breakdowns WHERE program=? AND status='active'",(program,)),
                       'policy':self.g.policy(row['project'])['digest'],
                       'program_origin':{'program':origin['program'],'project':origin['project'],
                                         'policy':origin['policy'],'origin':origin['origin'],
                                         'digest':origin['digest']},
                       'assurance_selection':selection})

    def next(self,actor,program):
        row=self.s.one("SELECT * FROM programs WHERE id=?",(program,),True)
        self.k.project(actor,row['project'])
        return {'program':program,'project':row['project'],'phase':row['phase'],'revision':row['revision'],
                'instructions':PHASE_ACTIONS[row['phase']], 'binding':self.program_binding(program),
                'required_output_kind':PHASE_KINDS.get(row['phase']), 'blockers':self.phase_blockers(row),
                'mandatory_notifications':self.w.status(actor,row['project'])['warnings'],
                'next_operation':'Produce and independently review the missing artifacts, then program.advance with observed phase review receipt.'}

    def phase_blockers(self,row):
        project,phase=row['project'],row['phase']; failures=[]
        owner=Actor('system','owner')
        kind=PHASE_KINDS.get(phase)
        if kind and not self.s.one("SELECT id FROM artifacts WHERE project=? AND kind=? AND status='accepted'",(project,kind)):
            failures.append('no_accepted_'+kind)
        if phase=='requirements':
            coverage=self.k.source_coverage(owner,project)
            if not coverage['structurally_complete']: failures.append('unclassified_source_spans')
        if phase=='boundaries':
            if self.indexer:
                inventory=self.indexer.inventory(owner,project)
                for repo in inventory['repos']:
                    if not repo.get('index') or repo['index']['status']!='ready':failures.append('discovery_index_missing:'+repo['repo'])
                if any(r['files'] for r in inventory['repos']):
                    observed=self.s.one("SELECT id FROM events WHERE project=? AND kind='discovery_observed' LIMIT 1",(project,))
                    if not observed:failures.append('discovery_not_observed')
            elif self.g.mode=='governed' and self.s.one('SELECT id FROM repos WHERE project=?',(project,)):
                failures.append('discovery_service_unavailable')
            owned={}
            for r in self.s.all("SELECT id,body FROM artifacts WHERE project=? AND kind='domain' AND status='accepted'",(project,)):
                for data in parse_json(r['body'])['owned_data']:
                    if data in owned: failures.append('duplicate_data_owner:'+data)
                    owned[data]=r['id']
        if phase=='design':
            for r in self.s.all("SELECT id FROM artifacts WHERE project=? AND kind='requirement' AND status='accepted'",(project,)):
                if not self.s.one("SELECT l.source FROM links l JOIN artifacts a ON a.id=l.source WHERE l.target=? AND l.relation='realizes' AND l.confidence='asserted' AND a.status='accepted' AND a.kind IN ('design','component')",(r['id'],)): failures.append('unallocated_requirement:'+r['id'])
        if phase=='plan':
            if self.breakdowns and row.get('id'):
                status=self.breakdowns.program_status(owner,row['id'])
                if not status['current']:failures.append('breakdown_incomplete')
            trace=self.k.trace(owner,project)
            if not trace['structural_complete']: failures.append('trace_incomplete')
            if getattr(self,'traceability',None) is not None and row.get('id'):
                traceability_gate=self.traceability.planning_gate(project,row['id'],owner)
                failures.extend(traceability_gate['failures'])
            for t in self.s.all("SELECT id FROM tasks WHERE project=? AND status!='cancelled'",(project,)):
                if not self.s.one("SELECT task FROM plans WHERE task=?",(t['id'],)): failures.append('no_frozen_checks:'+t['id'])
            if not self.s.one("SELECT project FROM profiles WHERE project=?",(project,)): failures.append('delivery_profile_missing')
            # Unit 4-P consumes the immutable origin and the current
            # canonical mandatory selection at the plan boundary.  Keep this
            # read-only blocker diagnostic complete; advance() reruns the same
            # proof inside its mutation transaction.
            try:
                from .unit4_enforcement import inspect_plan_gate
                active=self.breakdowns.active(owner,row['id']) if self.breakdowns and row.get('id') else None
                control=getattr(self,'control',None)
                if control is None:
                    raise Fault('assurance_unavailable','Unit 4 plan enforcement is not attached to the composition root')
                gate=inspect_plan_gate(control,owner,project=project,program=row.get('id'),
                                       proposed_breakdown=active['id'] if active else None)
                if gate['allowed'] is not True:
                    failures.extend('assurance_plan_blocked:'+str(item.get('code','unknown'))
                                    for item in gate.get('failures',[]))
                    if not gate.get('failures'):
                        failures.append('assurance_plan_blocked:'+gate.get('reason','unknown'))
            except Fault as exc:
                failures.append('assurance_plan_blocked:'+exc.code)
        if phase=='implementation':
            rows=self.s.all("SELECT id,status,validity FROM tasks WHERE project=? AND status!='cancelled'",(project,))
            if not rows or any(r['status']!='completed' or r['validity']!='current' for r in rows): failures.append('work_incomplete')
        if phase in {'integration','delivery'}:
            if not self.s.one("SELECT id FROM deliveries WHERE project=? AND status IN ('verified','delivered')",(project,)): failures.append('integrated_delivery_unverified')
        return failures

    def advance(self,actor,program,expected_revision,review_receipt):
        row=self.s.one("SELECT * FROM programs WHERE id=?",(program,),True)
        actor.require('owner','agent',project=row['project'])
        with self.s.transaction():
            row=self.s.one('SELECT * FROM programs WHERE id=?',(program,),True)
            need(row['revision']==expected_revision,'stale_revision','Program has changed')
            blockers=self.phase_blockers(row)
            need(not blockers,'phase_blocked','Required phase outputs are incomplete',blockers)
            assurance_gate=None
            if row['phase']=='plan':
                from .unit4_enforcement import require_plan_gate
                active=self.breakdowns.active(actor,program) if self.breakdowns else None
                control=getattr(self,'control',None)
                need(control is not None,'assurance_unavailable','Unit 4 plan enforcement is not attached to the composition root')
                assurance_gate=require_plan_gate(
                    control,actor,project=row['project'],program=program,
                    proposed_breakdown=active['id'] if active else None,
                )
            self.g.require_review(review_receipt,program,self.program_binding(program),{'phase'})
            if self.scopes and self.s.one("SELECT id FROM review_scopes WHERE program=? AND phase=? AND status='active'",(program,row['phase'])):
                need(self.scopes.summary(actor,program)['complete'],'incomplete_review_coverage','Bounded reviews do not cover the complete current phase')
            i=PHASES.index(row['phase'])
            need(i<len(PHASES)-1,'end_of_workflow','Already at final delivery phase')
            body=parse_json(row['body']);history={'phase':row['phase'],'review':review_receipt,'at':timestamp()}
            if assurance_gate is not None:
                history['assurance']=assurance_gate
            body['history'].append(history)
            self.s.execute("UPDATE programs SET phase=?,revision=revision+1,body=? WHERE id=?",(PHASES[i+1],canonical(body).decode(),program))
            event_body={'program':program,'from':row['phase'],'to':PHASES[i+1]}
            if assurance_gate is not None:
                event_body['plan_proof']=assurance_gate.get('proof_digest')
            self.sec.event(row['project'],'phase_advanced',actor.id,event_body)
        return self.next(actor,program)

    def change(self,actor,project,body):
        actor.require('owner','agent','worker',project=project,task=actor.task)
        obj(body,required=('title','origin','reason','affected','evidence'),optional=('deltas','source','unknown_neighbors','withdrawal','compensation','program'))
        text(body['title'],'title',500);text(body['reason'],'reason',20000)
        need(body['origin'] in {'user','requirement','design','implementation','test','operations','dependency','withdrawal'},'invalid_origin','Unknown change origin')
        strings(body['affected'],'affected',nonempty=True);strings(body['evidence'],'evidence',nonempty=True)
        if body['origin']=='user':
            need(body.get('source'),'source_required','Incoming user changes must reference original input')
            source=self.s.one("SELECT project,trust FROM sources WHERE id=?",(body['source'],),True)
            need(source['project']==project and source['trust']=='human','human_input_required','Untrusted text cannot impersonate a new user instruction')
        for evidence in body['evidence']:
            need(self.s.one("SELECT id FROM sources WHERE id=? AND project=?",(evidence,project)) or self.s.one("SELECT id FROM receipts WHERE id=? AND project=?",(evidence,project)) or self.s.one("SELECT id FROM artifacts WHERE id=? AND project=? AND kind IN ('finding','risk','unknown')",(evidence,project)), 'missing_evidence','Change evidence does not exist')
        impact=self.k.impact(actor,project,body['affected'])
        unknown=strings(body.get('unknown_neighbors',[]),'unknown_neighbors')
        for t in unknown: self.s.one("SELECT id FROM tasks WHERE id=? AND project=?",(t,project),True)
        body={**body,'impact':impact,'baseline_refs':[{k:a[k] for k in ('id','revision','digest')} for a in [self.k.artifact(actor,x) for x in body['affected']]]}
        self.validate_deltas(actor,project,body.get('deltas',[]))
        ident=uid('CHG');stage='awaiting_product_decision' if body['origin']=='user' else 'local_repair'
        with self.s.transaction():
            self.s.execute("INSERT INTO changes VALUES(?,?,?,?,?,?)",(ident,project,stage,canonical(body).decode(),1,timestamp()))
            for t in set(impact['tasks']) | set(unknown):
                self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(t,'change',ident,body['reason']))
                self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1,lease_until=NULL,updated=? WHERE id=?",(timestamp(),t))
            self.sec.event(project,'change_registered',actor.id,{'id':ident,'stage':stage,'impact':impact})
            if stage=='awaiting_product_decision': self.g.inbox(project,'product_decision',ident,body,'warning')
        return {'id':ident,'stage':stage,'impact':impact,'binding':self.change_binding(ident)}

    def validate_deltas(self,actor,project,deltas):
        need(isinstance(deltas,list) and len(deltas)<=1000,'invalid_delta','Invalid delta list')
        ids=set()
        for d in deltas:
            obj(d,required=('artifact','expected_revision','body'),optional=('withdraw',))
            need(d['artifact'] not in ids,'invalid_delta','Duplicate artifact delta');ids.add(d['artifact'])
            art=self.k.artifact(actor,d['artifact'])
            need(art['project']==project and art['revision']==d['expected_revision'],'stale_revision','Delta base is not current')
            self.k.validate_body(art['kind'],d['body'])
            if art['kind']=='interface' and art['body']!=d['body']:
                from .contracts import compatibility
                comparison=compatibility(art['body'],d['body'])
                transition=d['body'].get('change_control')
                obj(transition,required=('consumer_impact','verification_ids','repository_order','migration'))
                need(isinstance(transition['consumer_impact'],dict),'consumer_impact_required','Declare an impact disposition for every registered consumer')
                strings(art['body'].get('consumers', []), 'current consumer identifiers')
                strings(d['body'].get('consumers', []), 'proposed consumer identifiers')
                registered={r['source'] for r in self.s.all("SELECT source FROM links WHERE target=? AND relation='consumes'",(art['id'],))}
                registered.update(art['body'].get('consumers', []))
                registered.update(d['body'].get('consumers', []))
                need(registered<=transition['consumer_impact'].keys(),'consumer_impact_required','Consumer omitted from interface change analysis')
                for consumer, disposition in transition['consumer_impact'].items():
                    text(consumer, 'consumer identity', 1000)
                    text(disposition, 'consumer impact rationale', 20000)
                strings(transition['verification_ids'],'contract tests',nonempty=True);strings(transition['repository_order'],'migration repository order')
                text(transition['migration'],'migration or explicit compatibility rationale',20000)
                for test in transition['verification_ids']:
                    testing=self.k.artifact(actor,test);need(testing['project']==project and testing['kind']=='test' and testing['status']!='withdrawn','contract_test_required','Verification must reference a registered test')
                for repo in transition['repository_order']:self.s.one('SELECT id FROM repos WHERE id=? AND project=?',(repo,project),True)

        return ids

    def change_review_material(self, change):
        """Return one consistent view used both by the prompt and by its binding."""
        from .contracts import interface_impact_context
        from .review_dependencies import accepted_invariants
        with self.s.transaction():
            row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
            body=parse_json(row['body'])
            material={'change':change,'revision':row['revision'],'stage':row['stage'],'body':body,
                      'current':[{k:a[k] for k in ('id','revision','digest','status')}
                                 for a in [self.k.artifact(Actor('system','owner'),x) for x in body['affected']]],
                      'policy':self.g.policy(row['project'])['digest'],
                      'invariants':accepted_invariants(self.s,row['project'])}
            impacts=interface_impact_context(self.k,row['project'],body.get('deltas',[]))
            if impacts:
                material['interface_impact']=impacts
            return row, material

    def change_binding(self,change):
        _, material=self.change_review_material(change)
        return digest(material)

    def attempt(self,actor,change,level,body):
        row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
        actor.require('owner','agent',project=row['project'])
        need(level in {'local_repair','module_replan','system_replan'} and row['stage']==level,'wrong_escalation_level','Resolve the current design level before escalating')
        obj(body,required=('hypothesis','alternatives','evidence','outcome','remaining_unknown'),optional=('resource_limit','solution','review_receipt'))
        text(body['hypothesis'],'hypothesis',12000);strings(body['alternatives'],'alternatives',nonempty=True)
        strings(body['evidence'],'evidence',nonempty=True);text(body['remaining_unknown'],'unknowns',12000,empty=True)
        need(body['outcome'] in {'solution','no_solution_found','resource_exhausted'},'invalid_outcome','Invalid feasibility result')
        for ev in body['evidence']: self.g.receipt(ev)
        if body['outcome']=='no_solution_found':
            need(body.get('review_receipt'),'review_required','Independent assessment is required before escalation')
            self.g.require_review(body['review_receipt'],change,self.change_binding(change),{'feasibility'})
        with self.s.transaction():
            ident=uid('ATTEMPT')
            self.s.execute("INSERT INTO attempts VALUES(?,?,?,?,?)",(ident,change,level,canonical(body).decode(),timestamp()))
            if body['outcome']=='no_solution_found':
                next_stage={'local_repair':'module_replan','module_replan':'system_replan','system_replan':'awaiting_product_decision'}[level]
            elif body['outcome']=='solution': next_stage='reconciling'
            else: next_stage=level
            self.s.execute("UPDATE changes SET stage=?,revision=revision+1 WHERE id=?",(next_stage,change))
            if next_stage=='awaiting_product_decision' or body['outcome']=='resource_exhausted':
                self.g.inbox(row['project'],'product_decision' if next_stage=='awaiting_product_decision' else 'search_incomplete',change,body,'warning')
            self.sec.event(row['project'],'feasibility_attempt',actor.id,{'change':change,'level':level,'outcome':body['outcome'],'stage':next_stage})
        return {'id':ident,'stage':next_stage,'note':'Resource exhaustion is not proof of infeasibility.'}

    def set_delta(self,actor,change,expected_revision,deltas,reason,force_revision=False):
        row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
        actor.require('owner','agent',project=row['project']);text(reason,'reason',12000)
        need(type(force_revision) is bool,'invalid_option','force_revision must be Boolean')
        with self.s.transaction():
            row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
            need(row['revision']==expected_revision,'stale_revision','Change has been revised')
            self.validate_deltas(actor,row['project'],deltas)
            body=parse_json(row['body'])
            history=body.get('delta_history',[])
            if (not force_revision and history and history[-1].get('actor')==actor.id
                and history[-1]['revision']==expected_revision-1 and history[-1].get('stage')==row['stage']
                and history[-1]['reason']==reason and canonical(body.get('deltas',[]))==canonical(deltas)):
                for ref in body['baseline_refs']:
                    art=self.k.artifact(actor,ref['id'])
                    need(art['revision']==ref['revision'] and art['digest']==ref['digest'],
                         'stale_revision','Change baseline is no longer current')
                self.sec.event(row['project'],'change_delta_replayed',actor.id,{'change':change,'revision':expected_revision})
                return {'id':change,'revision':expected_revision,'binding':self.change_binding(change),'unchanged':True}
            body['deltas']=deltas;body.setdefault('delta_history',[]).append({'reason':reason,'revision':expected_revision,'actor':actor.id,'stage':row['stage']})
            self.s.execute("UPDATE changes SET body=?,revision=revision+1 WHERE id=?",(canonical(body).decode(),change))
            self.s.execute("UPDATE decisions SET status='superseded' WHERE project=? AND status IN ('pending','decision_received') AND json_extract(body,'$.change')=?",(row['project'],change))
            self.sec.event(row['project'],'change_delta_revised',actor.id,{'change':change,'revision':expected_revision+1})
        return {'id':change,'revision':expected_revision+1,'binding':self.change_binding(change)}

    def conflict(self,actor,project,refs,explanation,options):
        actor.require('owner','agent',project=project)
        strings(refs,'conflicting refs',nonempty=True);strings(options,'options',nonempty=True);text(explanation,'explanation',20000)
        artifacts=[self.k.artifact(actor,x) for x in refs]
        need(all(a['project']==project for a in artifacts),'cross_project','Conflict reference mismatch')
        ident=uid('CONFLICT');body={'refs':[{k:a[k] for k in ('id','revision','digest')} for a in artifacts],'explanation':explanation,'options':options}
        impact=self.k.impact(actor,project,refs)
        with self.s.transaction():
            self.s.execute("INSERT INTO conflicts VALUES(?,?,?,?,?,?)",(ident,project,canonical(body).decode(),'awaiting_user',None,timestamp()))
            for task in impact['tasks']:
                self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(task,'conflict',ident,explanation))
                self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1,lease_until=NULL WHERE id=?",(task,))
            self.g.inbox(project,'conflict',ident,body,'warning')
            self.sec.event(project,'conflict_recorded',actor.id,{'id':ident,'impact':impact})
        return {'id':ident,'body':body,'impact':impact}

    def provisional_review_material(self,project,body):
        from .review_dependencies import accepted_invariants
        with self.s.transaction():
            clean={k:v for k,v in body.items() if k not in {'consistency_receipt','bindings'}}
            artifacts=[self.k.artifact(Actor('control','owner'),x) for x in clean.get('refs',[])]
            need(all(a['project']==project for a in artifacts),'cross_project','Decision reference belongs elsewhere')
            return {'format':'provisional-decision-review.v1','project':project,'proposal':clean,
                    'refs':[{k:a[k] for k in ('id','revision','digest')} for a in artifacts],
                    'invariants':accepted_invariants(self.s,project),
                    'policy':self.g.policy(project)['digest']}

    def provisional_binding(self,project,body):
        return digest(self.provisional_review_material(project,body))

    def propose_decision(self,actor,project,body):
        with self.s.transaction():
            return self._propose_decision(actor,project,body)

    def _propose_decision(self,actor,project,body):
        actor.require('owner','agent',project=project)
        obj(body,required=('title','reason','options','recommendation','refs','requirement_affecting'),
            optional=('change','conflict','supersedes','provisional','expires','reversible','consistency_receipt'))
        for f in ('title','reason','recommendation'):text(body[f],f,20000)
        strings(body['options'],'options',nonempty=True);strings(body['refs'],'refs')
        need(type(body['requirement_affecting']) is bool,'invalid_decision','Requirement-affecting flag must be boolean')
        body={**body,'bindings':[{k:a[k] for k in ('id','revision','digest')} for a in [self.k.artifact(actor,x) for x in body['refs']]]}
        if body.get('change'):
            change=self.s.one("SELECT * FROM changes WHERE id=? AND project=?",(body['change'],project),True)
            need(change['stage']=='awaiting_product_decision','premature_escalation','Try local, module and system remedies before product escalation')
            body['change_binding']=self.change_binding(body['change'])
        if body.get('conflict'):self.s.one("SELECT id FROM conflicts WHERE id=? AND project=?",(body['conflict'],project),True)
        if body.get('supersedes'): self.s.one("SELECT id FROM decisions WHERE id=? AND project=?",(body['supersedes'],project),True)
        provisional=body.get('provisional',False)
        if provisional:
            need(not body['requirement_affecting'] and body.get('reversible') is True and not body.get('change') and not body.get('conflict'), 'product_decision_required','Only reversible, specification-preserving assumptions may be provisional')
            need(timestamp()<body.get('expires',0)<=timestamp()+86400,'invalid_expiry','Provisional assumptions require a short expiry')
            need(body.get('consistency_receipt'),'review_required','Provisional decision requires consistency check')
            subject=body['refs'][0] if body['refs'] else None
            need(subject,'invalid_decision','Provisional decisions need a scope reference')
            self.g.require_review(body['consistency_receipt'],project,self.provisional_binding(project,body),{'decision_proposal'},latest=True)
        ident=uid('DEC');h=digest(body)
        with self.s.transaction():
            self.s.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",(ident,project,1,canonical(body).decode(),h,'provisional' if provisional else 'pending',None,None,None,timestamp()))
            self.g.inbox(project,'provisional_decision' if provisional else 'product_decision',ident,body,'warning',body.get('expires'))
            if provisional:self.s.execute("INSERT INTO timers(id,project,kind,ref,due) VALUES(?,?,?,?,?)",(uid('TIMER'),project,'decision_expiry',ident,body['expires']))
            if body['requirement_affecting'] and body['refs']:
                impact=self.k.impact(actor,project,body['refs'])
                for task in impact['tasks']:
                    self.s.execute('INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)',(task,'decision',ident,'Product decision pending'))
                    self.s.execute("UPDATE tasks SET epoch=epoch+1,validity='needs_review',lease_until=NULL WHERE id=?",(task,))
            self.sec.event(project,'decision_proposed',actor.id,{'id':ident,'digest':h,'provisional':provisional})
        return {'id':ident,'revision':1,'digest':h,'body':body}

    def respond(self,actor,decision,expected_digest,choice,utterance,source=None):
        actor.require('owner')
        row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
        body=parse_json(row['body']);text(utterance,'human utterance',100000)
        with self.s.transaction():
            need(row['digest']==expected_digest and row['status'] in {'pending','provisional','decision_received'},'stale_decision','Respond to the exact pending proposal')
            allowed=body.get('options',['approve','reject','defer'])
            need(choice in allowed or choice in {'reject','defer'},'invalid_choice','Choose one of the displayed options')
            for ref in body.get('bindings',[]):
                current=self.k.artifact(actor,ref['id'])
                need(current['revision']==ref['revision'] and current['digest']==ref['digest'],'stale_decision','A bound requirement changed')
            if body.get('change'): need(body['change_binding']==self.change_binding(body['change']),'stale_decision','Change proposal changed')
            if source is None:
                src=self.k.source(actor,row['project'],utterance,'trusted-dialogue:'+decision)
                self.k.classify(actor,src['id'],0,len(utterance),'reference',[],'Authenticated response to exact versioned decision '+decision)
                quote={'source':src['id'],'start':0,'end':len(utterance),'quote':utterance}
            else:
                quote=self.k.human_quote(actor,row['project'],source,utterance,after=row['created'])
            source_id=quote['source']
            status='rejected' if choice=='reject' else 'deferred' if choice=='defer' else 'decision_received'
            self.s.execute("UPDATE decisions SET status=?,response=?,source=? WHERE id=?",(status,choice,source_id,decision))
            if status=='rejected':
                self.s.execute("UPDATE inbox SET status='acknowledged' WHERE project=? AND ref=?",(row['project'],decision))
                self.s.execute("DELETE FROM blocks WHERE kind='decision' AND ref=?",(decision,))
                # Rejection doesn't make old code current: tasks still need explicit reassessment.
            self.sec.event(row['project'],'human_response_observed',actor.id,{'decision':decision,'digest':expected_digest,'choice':choice,**quote})
        return {'id':decision,'status':status,'consistency_recheck_required':status=='decision_received'}

    def decision_binding(self,decision):
        row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
        response_evidence=self.response_evidence(decision)
        return digest({'decision':decision,'digest':row['digest'],'response':row['response'],'source':row['source'],
                       **({'response_evidence':response_evidence} if response_evidence else {}),
                       'current_artifacts':self.s.all("SELECT id,revision,digest FROM artifacts WHERE project=? AND status='accepted' ORDER BY id",(row['project'],)),
                       'other_decisions':self.s.all("SELECT id,digest,status,response FROM decisions WHERE project=? AND id!=? AND status IN ('applied','provisional','decision_received') ORDER BY id",(row['project'],decision)),
                       'policy':self.g.policy(row['project'])['digest']})

    def response_evidence(self,decision):
        event=self.s.one("SELECT id,actor,body FROM events WHERE kind='human_response_observed' AND json_extract(body,'$.decision')=? ORDER BY seq DESC LIMIT 1",(decision,))
        if event and 'quote' in parse_json(event['body']):
            return {**event,'body':parse_json(event['body'])}
        return None

    def apply_decision(self,actor,decision,review_receipt):
        row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
        actor.require('owner','agent',project=row['project'])
        if parse_json(row['body']).get('type') == 'execution_control_policy':
            raise Fault('wrong_route','Use execution_control.policy_apply for source-backed policy adoption')
        with self.s.transaction():
            need(row['status']=='decision_received' and row['source'],'human_approval_required','A displayed proposal or agent approval is insufficient')
            src=self.s.one("SELECT trust FROM sources WHERE id=?",(row['source'],),True)
            need(src['trust']=='human','human_approval_required','No authenticated human response')
            self.g.require_review(review_receipt,decision,self.decision_binding(decision),{'consistency'})
            body=parse_json(row['body'])
            if body.get('type')=='policy':
                old=self.g.policy(row['project'])
                execution_fields=('version','default_task_timeout_seconds','max_no_progress_attempts','max_run_seconds')
                need(all(body['body'].get(field)==old['body'].get(field) for field in execution_fields),
                     'wrong_route','Use execution_control.policy_apply for execution-control policy adoption')
                need(old['digest']==body['old_digest'],'stale_policy','Policy changed since proposal')
                self.s.execute("UPDATE policies SET revision=revision+1,body=?,digest=? WHERE project=?",(canonical(body['body']).decode(),digest(body['body']),row['project']))
            elif body.get('change'):
                need(body['change_binding']==self.change_binding(body['change']),'stale_decision','Change was revised after approval')
                self._apply_change(actor,body['change'],review_receipt,decision)
            if body.get('conflict'):
                c=self.s.one("SELECT body FROM conflicts WHERE id=? AND project=?",(body['conflict'],row['project']),True)
                # Only a reviewed, explicit change can override conflicting accepted requirements.
                need(body.get('change') or row['response']=='keep_existing','change_required','Conflict resolution must say how specifications converge')
                self.s.execute("UPDATE conflicts SET status='resolved',decision=? WHERE id=?",(decision,body['conflict']))
                self.s.execute("DELETE FROM blocks WHERE kind='conflict' AND ref=?",(body['conflict'],))
                self.s.execute("UPDATE inbox SET status='acknowledged' WHERE ref=?",(body['conflict'],))
            if body.get('supersedes'):
                old=self.s.one("SELECT body FROM decisions WHERE id=?",(body['supersedes'],),True)
                self.s.execute("UPDATE decisions SET status='superseded' WHERE id=?",(body['supersedes'],))
                roots=parse_json(old['body']).get('refs',[])
                if roots:self.k.invalidate(row['project'],roots,'Human corrected a previous decision')
            self.s.execute("UPDATE decisions SET status='applied',consistency_receipt=? WHERE id=?",(review_receipt,decision))
            self.s.execute("DELETE FROM blocks WHERE kind='decision' AND ref=?",(decision,))
            self.s.execute("UPDATE inbox SET status='acknowledged' WHERE project=? AND ref=?",(row['project'],decision))
            self.sec.event(row['project'],'decision_applied',actor.id,{'decision':decision,'receipt':review_receipt})
        return {'id':decision,'status':'applied'}

    def apply_technical_change(self,actor,change,review_receipt):
        with self.s.transaction():
            row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
            actor.require('owner','agent',project=row['project'])
            need(row['stage']=='reconciling','invalid_stage','Record a specification-preserving solution first')
            body=parse_json(row['body'])
            for delta in body.get('deltas',[]):
                art=self.k.artifact(actor,delta['artifact'])
                need(art['kind'] not in {'requirement','acceptance','outcome','decision'},'product_decision_required','Technical change cannot silently modify product semantics')
            self.g.require_review(review_receipt,change,self.change_binding(change),{'consistency'},latest=True)
            return self._apply_change(actor,change,review_receipt,None)

    def _apply_change(self,actor,change,receipt,decision):
        row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
        body=parse_json(row['body']);deltas=body.get('deltas',[])
        need(deltas,'empty_change','No implementation delta was defined')
        changed=self.validate_deltas(actor,row['project'],deltas)
        for delta in deltas:
            conflicts=self.k.explicit_conflicts(row['project'],delta['body'],exclude=changed)
            need(not conflicts,'secondary_conflict','Chosen resolution conflicts with other accepted requirements',conflicts)
        constraints={}
        for delta in deltas:
            for k,v in delta['body'].get('constraints',{}).items():
                need(k not in constraints or constraints[k]==v,'secondary_conflict','Proposed deltas contradict one another')
                constraints[k]=v
        for delta in deltas:
            art=self.k.artifact(actor,delta['artifact'])
            if delta.get('withdraw'):
                need(body.get('compensation') or not self.s.one("SELECT id FROM deliveries WHERE project=? AND status='delivered'",(row['project'],)), 'compensation_required','Published changes need compensation or migration')
            self.k._revise(actor,art,delta['expected_revision'],delta['body'],'Change '+change,'withdrawn' if delta.get('withdraw') else 'accepted')
        self.s.execute("UPDATE changes SET stage='ready_for_reimplementation',revision=revision+1 WHERE id=?",(change,))
        self.s.execute("DELETE FROM blocks WHERE kind='change' AND ref=?",(change,))
        self.s.execute("UPDATE inbox SET status='acknowledged' WHERE ref=?",(change,))
        self.sec.event(row['project'],'change_applied',actor.id,{'change':change,'receipt':receipt,'decision':decision,'changed':sorted(changed)})
        return {'id':change,'stage':'ready_for_reimplementation','reassessment_required':True}

    def withdraw(self,actor,change,reason,compensation):
        row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
        actor.require('owner',project=row['project']);text(reason,'reason',12000)
        body=parse_json(row['body'])
        need(row['stage']!='ready_for_reimplementation' or bool(compensation),'compensation_required','Applied change cannot be silently discarded')
        with self.s.transaction():
            body['withdrawal']={'reason':reason,'compensation':compensation,'by':actor.id}
            self.s.execute("UPDATE changes SET stage='withdrawn',body=?,revision=revision+1 WHERE id=?",(canonical(body).decode(),change))
            self.s.execute("DELETE FROM blocks WHERE kind='change' AND ref=?",(change,))
            self.sec.event(row['project'],'change_withdrawn',actor.id,{'change':change,'reason':reason,'compensation':compensation})
        return {'id':change,'status':'withdrawn','history_preserved':True}
