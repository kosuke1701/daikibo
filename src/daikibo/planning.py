"""D03 — executable design phases, changes, escalation and contradiction handling."""
from __future__ import annotations
import copy
from .common import MAX_JSON_BYTES, Actor, Fault, canonical, digest, need, obj, parse_json, strings, text, timestamp, uid

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
        self.validate_deltas(actor,project,body.get('deltas',[]))
        roots=self._change_roots(body)
        impact=self.k.impact(actor,project,roots)
        unknown=strings(body.get('unknown_neighbors',[]),'unknown_neighbors')
        for t in unknown: self.s.one("SELECT id FROM tasks WHERE id=? AND project=?",(t,project),True)
        refs=[]
        for artifact_id in roots:
            artifact=self.k.artifact(actor,artifact_id)
            refs.append({k:artifact[k] for k in ('id','revision','digest')})
        body={**body,'impact':impact,'impact_roots':roots,'baseline_refs':refs,
              'scope_layer':'local_repair'}
        no_effect=self._change_has_no_effect(actor,project,body,body.get('deltas',[]))
        body['no_effect']=no_effect
        ident=uid('CHG')
        # Origin records provenance and may provide a trusted source. It is not
        # a substitute for the independent layer disposition.
        stage='local_repair'
        with self.s.transaction():
            task_fence=({'format':'keep-existing-task-fence.v1','change':ident,
                         'candidates':{},'scope':'no_effect'} if no_effect else
                self._capture_keep_existing_task_fence(project,ident,impact,set(unknown)))
            self.s.execute("INSERT INTO changes VALUES(?,?,?,?,?,?)",(ident,project,stage,canonical(body).decode(),1,timestamp()))
            if not no_effect:
                for t in set(impact['tasks']) | set(unknown):
                    self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(t,'change',ident,body['reason']))
                    self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1,lease_until=NULL,updated=? WHERE id=?",(timestamp(),t))
            self.sec.event(project,'change_registered',actor.id,{'id':ident,'stage':stage,'impact':impact,
                'task_fence':task_fence,'no_effect':no_effect})
            if no_effect:
                self.sec.event(project,'change_no_effect_observed',actor.id,{'change':ident,
                    'origin':body['origin'],'source':body.get('source'),'request_fulfilled':False,
                    'delta_targets':sorted(item['artifact'] for item in body.get('deltas',[]))})
            binding=self.change_binding(ident)
        return {'id':ident,'stage':stage,'impact':impact,'binding':binding,'no_effect':no_effect}

    @staticmethod
    def _change_roots(body):
        return sorted(set(body.get('affected',[])) | {
            delta['artifact'] for delta in body.get('deltas',[])
            if isinstance(delta,dict) and isinstance(delta.get('artifact'),str)
        })

    def _change_fence_candidates(self,project,change,impact):
        """Rebuild the controller-observed set of untouched planned tasks."""
        events=self.s.all("SELECT body FROM events WHERE project=? AND kind IN "
            "('change_registered','change_impact_recalculated') "
            "AND json_extract(body,'$.id')=? ORDER BY seq",(project,change))
        candidates={}
        for event in events:
            fence=parse_json(event['body']).get('task_fence',{})
            if fence.get('format')!='keep-existing-task-fence.v1' or fence.get('scope')!='planned_unstarted_only':
                continue
            candidates.update(fence.get('candidates',{}))
        current=set(impact.get('tasks',[]))
        return {task:value for task,value in candidates.items() if task in current}

    def _capture_keep_existing_task_fence(self,project,change,impact,unknown):
        """Retain controller-observed pre-change state for a narrow no-work fence."""
        candidates={}
        if unknown or len(impact.get('tasks',[]))>100:
            return {'format':'keep-existing-task-fence.v1','change':change,
                    'candidates':{},'scope':'unknown_or_unbounded'}
        for task in sorted(set(impact.get('tasks',[]))):
            row=self.s.one('SELECT * FROM tasks WHERE id=? AND project=?',(task,project))
            if row is None:
                continue
            reads=self.s.all('SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact',(task,))
            blocks=self.s.all('SELECT kind,ref,reason FROM blocks WHERE task=? ORDER BY kind,ref',(task,))
            runs=self.s.one('SELECT count(*) AS n FROM runs WHERE task=?',(task,))['n']
            candidates_count=self.s.one('SELECT count(*) AS n FROM candidates WHERE task=?',(task,))['n']
            attempts=self.s.one('SELECT count(*) AS n FROM execution_attempts WHERE task=?',(task,))['n']
            local_records=self.s.one('SELECT count(*) AS n FROM local_execution_records WHERE task=?',(task,))['n']
            if (row['status']!='planned' or row['validity']!='current' or row['candidate'] is not None or
                    row['lease_owner'] is not None or row['lease_until'] is not None or row['paused'] or
                    blocks or runs or candidates_count or attempts or local_records or
                    self.s.one('SELECT task FROM plans WHERE task=?',(task,)) is not None or len(reads)>64 or
                    len(row['body'].encode())>200000):
                continue
            candidates[task]={'revision':row['revision'],'epoch':row['epoch'],
                'task_body_digest':digest(row['body'].encode()),'status':row['status'],
                'validity':row['validity'],'paused':bool(row['paused']),
                'reads':reads,'blocks':blocks}
        # Keep event payloads bounded. If it is too large, this change follows
        # the ordinary reassessment path instead of carrying partial proof.
        material={'format':'keep-existing-task-fence.v1','change':change,'candidates':candidates,
                  'scope':'planned_unstarted_only'}
        if len(canonical(material))>200000:
            material['candidates']={}
            material['scope']='proof_exceeds_bound'
        return material

    def _restore_no_effect_task_fences(self,actor,project,change,body,impact):
        """Revalidate only pre-fenced, still-unstarted tasks after a delta becomes empty."""
        candidates=self._change_fence_candidates(project,change,impact)
        restored=[]
        for task,before in candidates.items():
            row=self.s.one('SELECT * FROM tasks WHERE id=? AND project=?',(task,project))
            if row is None:continue
            blocks=self.s.all('SELECT kind,ref,reason FROM blocks WHERE task=? ORDER BY kind,ref',(task,))
            expected_block=[{'kind':'change','ref':change,'reason':body.get('reason')}]
            if (row['status']!='planned' or row['validity']!='needs_review' or
                    row['revision']!=before['revision'] or row['epoch']!=before['epoch']+1 or
                    row['candidate'] is not None or row['lease_owner'] is not None or
                    row['lease_until'] is not None or row['paused'] or
                    digest(row['body'].encode())!=before['task_body_digest'] or
                    canonical(blocks)!=canonical(expected_block) or
                    canonical(self.s.all('SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact',(task,)))!=canonical(before['reads']) or
                    self.s.one('SELECT task FROM plans WHERE task=?',(task,)) is not None or
                    self.s.one('SELECT 1 FROM runs WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM candidates WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM execution_attempts WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM local_execution_records WHERE task=? LIMIT 1',(task,)) is not None):
                continue
            pins_current=True
            for pin in before['reads']:
                current=self.s.one('SELECT project,revision,digest,status FROM artifacts WHERE id=?',(pin['artifact'],))
                if (current is None or current['project']!=project or current['status']!='accepted' or
                        current['revision']!=pin['revision'] or current['digest']!=pin['digest']):
                    pins_current=False;break
            if not pins_current:continue
            cursor=self.s.execute("UPDATE tasks SET validity='current',updated=? WHERE id=? AND project=? "
                "AND status='planned' AND validity='needs_review' AND revision=? AND epoch=?",
                (timestamp(),task,project,before['revision'],before['epoch']+1))
            need(cursor.rowcount==1,'stale_task','Task changed during no-effect revalidation',task)
            proof={'task':task,'change':change,'before_epoch':before['epoch'],
                   'current_epoch':before['epoch']+1,'task_body_digest':before['task_body_digest'],
                   'read_pins_digest':digest(before['reads']),'reason':'exact_delta_has_no_effect'}
            self.sec.event(project,'task_revalidated_after_no_effect_change',actor.id,proof)
            restored.append(proof)
        return sorted(restored,key=lambda item:item['task'])

    def _keep_existing_task_revalidations(self,project,change,decision,*,apply=False,expected=None,actor='system'):
        """Prove the same narrow task-fence conditions before and after decline."""
        registration=self.s.one("SELECT body FROM events WHERE project=? AND kind='change_registered' "
            "AND json_extract(body,'$.id')=? ORDER BY seq LIMIT 1",(project,change))
        if registration is None:
            return []
        evidence=parse_json(registration['body']).get('task_fence',{})
        if (evidence.get('format')!='keep-existing-task-fence.v1' or
                evidence.get('scope')!='planned_unstarted_only'):
            return []
        decision_row=self.s.one('SELECT * FROM decisions WHERE id=? AND project=?',(decision,project))
        change_row=self.s.one('SELECT * FROM changes WHERE id=? AND project=?',(change,project))
        expected_status='applied' if apply else 'decision_received'
        expected_change_stage='withdrawn' if apply else 'awaiting_product_decision'
        attempts=self.s.one('SELECT count(*) AS n FROM attempts WHERE change_id=?',(change,))['n']
        expected_change_revision=1+attempts+(1 if apply else 0)
        if (not decision_row or not change_row or change_row['stage']!=expected_change_stage or
                change_row['revision']!=expected_change_revision):
            return []
        decision_body=parse_json(decision_row['body']);change_body=parse_json(change_row['body'])
        if (decision_row['status']!=expected_status or
                self._choice_effect(decision_body,decision_row['response'])!='keep_existing' or
                decision_body.get('change')!=change or
                set(decision_body.get('refs',[]))!=set(change_body.get('affected',[])) or
                change_body.get('unknown_neighbors') or
                change_body.get('delta_history') or
                (apply and change_body.get('withdrawal',{}).get('decision')!=decision)):
            return []
        registered=parse_json(registration['body'])
        if change_body.get('impact')!=registered.get('impact'):
            return []
        siblings=self.s.all("SELECT id,status FROM decisions WHERE project=? AND "
            "json_extract(body,'$.change')=? AND id!=?",(project,change,decision))
        if siblings:
            return []
        allowed={item['task'] for item in expected or []} if apply else None
        valid=[]
        for task,before in evidence.get('candidates',{}).items():
            if allowed is not None and task not in allowed:
                continue
            row=self.s.one('SELECT * FROM tasks WHERE id=? AND project=?',(task,project))
            if row is None:
                continue
            blocks=self.s.all('SELECT kind,ref,reason FROM blocks WHERE task=? ORDER BY kind,ref',(task,))
            expected_epoch=before['epoch']+2
            expected_blocks=([] if apply else
                [{'kind':'change','ref':change,'reason':change_body.get('reason')},
                 {'kind':'decision','ref':decision,'reason':'Product decision pending'}])
            if (row['status']!='planned' or row['validity']!='needs_review' or
                    row['revision']!=before['revision'] or row['epoch']!=expected_epoch or
                    row['candidate'] is not None or row['lease_owner'] is not None or
                    row['lease_until'] is not None or row['paused'] or
                    digest(row['body'].encode())!=before['task_body_digest'] or
                    canonical(self.s.all('SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact',(task,)))!=canonical(before['reads']) or
                    canonical(blocks)!=canonical(expected_blocks) or
                    self.s.one('SELECT task FROM plans WHERE task=?',(task,)) is not None or
                    self.s.one('SELECT 1 FROM runs WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM candidates WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM execution_attempts WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM local_execution_records WHERE task=? LIMIT 1',(task,)) is not None):
                continue
            pins_unchanged=True
            for pin in before['reads']:
                artifact=self.s.one('SELECT project,revision,digest,status FROM artifacts WHERE id=?',(pin['artifact'],))
                if (artifact is None or artifact['project']!=project or artifact['status']!='accepted' or
                        artifact['revision']!=pin['revision'] or artifact['digest']!=pin['digest']):
                    pins_unchanged=False;break
            if not pins_unchanged:
                continue
            proof={'task':task,'change':change,'decision':decision,'before_epoch':before['epoch'],
                   'current_epoch':expected_epoch,'task_body_digest':before['task_body_digest'],
                   'read_pins_digest':digest(before['reads']),'blocks_cleared':[change,decision]}
            if apply:
                cursor=self.s.execute("UPDATE tasks SET validity='current',updated=? WHERE id=? AND project=? "
                    "AND status='planned' AND validity='needs_review' AND epoch=?",
                    (timestamp(),task,project,expected_epoch))
                if cursor.rowcount!=1:
                    continue
                actor_id=actor.id if hasattr(actor,'id') else actor
                self.sec.event(project,'task_revalidated_after_keep_existing',actor_id,proof)
            valid.append(proof)
        return sorted(valid,key=lambda item:(item['task'],item['change'],item['decision']))

    def _restore_keep_existing_task_fences(self,actor,project,change,decision,expected):
        """Apply a reviewed lightweight revalidation without rolling back epoch."""
        return self._keep_existing_task_revalidations(project,change,decision,
            apply=True,expected=expected,actor=actor)

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

    def _change_has_no_effect(self,actor,project,body,deltas=None):
        deltas=body.get('deltas',[]) if deltas is None else deltas
        if (not isinstance(deltas,list) or not deltas or body.get('unknown_neighbors') or
                not isinstance(body.get('affected'),list) or
                len(set(body['affected']))!=len(body['affected'])):
            return False
        targets=[]
        for delta in deltas:
            if not isinstance(delta,dict) or type(delta.get('withdraw',False)) is not bool or delta.get('withdraw'):
                return False
            target=delta.get('artifact')
            if not isinstance(target,str):return False
            current=self.k.artifact(actor,target)
            if (current['project']!=project or current['status']!='accepted' or
                    current['revision']!=delta.get('expected_revision') or
                    canonical(current['body'])!=canonical(delta.get('body'))):
                return False
            targets.append(target)
        return len(set(targets))==len(targets) and set(targets)==set(body['affected'])

    @staticmethod
    def _is_display_metadata_repair(artifact,delta):
        """Recognize a candidate title-only edit; this is not a semantic verdict."""
        if artifact['status']!='accepted' or not isinstance(delta,dict) or delta.get('withdraw') or not isinstance(delta.get('body'),dict):
            return False
        before=artifact['body'];after=delta['body']
        if set(before)!=set(after) or 'title' not in before:
            return False
        if before.get('title')==after.get('title') or not isinstance(after.get('title'),str):
            return False
        return canonical({k:v for k,v in before.items() if k!='title'})==canonical(
            {k:v for k,v in after.items() if k!='title'})

    @staticmethod
    def _is_editorial_repair_candidate(artifact,delta):
        """Recognize text-edit candidates; independent review decides equivalence."""
        if (artifact['status']!='accepted' or not isinstance(delta,dict) or delta.get('withdraw') or
                not isinstance(delta.get('body'),dict)):
            return False
        before=artifact['body'];after=delta['body']
        if set(before)!=set(after):
            return False
        changed=[key for key in before if canonical(before[key])!=canonical(after[key])]
        return bool(changed) and all(key in {'title','statement','description','summary','label','name'} and
                                     isinstance(before[key],str) and isinstance(after[key],str)
                                     for key in changed)

    def _is_user_editorial_repair(self,actor,project,body):
        if body.get('origin')!='user' or not body.get('source') or not body.get('deltas'):
            return False
        source=self.s.one('SELECT project,trust FROM sources WHERE id=?',(body['source'],))
        if source is None or source['project']!=project or source['trust']!='human':
            return False
        return all(self._is_editorial_repair_candidate(self.k.artifact(actor,item['artifact']),item)
                   for item in body['deltas'])

    def _validate_display_metadata_repair(self,actor,change,artifact,delta):
        need(self._is_display_metadata_repair(artifact,delta),'product_decision_required',
             'The proposed edit changes fields beyond display title metadata')
        need(artifact['project']==change['project'] and artifact['status']=='accepted',
             'stale_reference','Display repair requires a current accepted artifact')
        body=parse_json(change['body']) if isinstance(change.get('body'),str) else change['body']
        if body.get('origin')=='user':
            source=body.get('source')
            source_row=self.s.one('SELECT project,trust,blob FROM sources WHERE id=?',(source,)) if source else None
            need(source_row and source_row['project']==change['project'] and source_row['trust']=='human',
                 'human_input_required','A user-requested display repair needs its original trusted source')
        marker='display-metadata-equivalence:'+artifact['id']
        material=self.change_review_material(change['id'])[1]
        need(marker in material.get('required_coverage',[]),'repair_review_required',
             'The current change material does not require an explicit metadata-equivalence assessment')

    def _change_upper_contract_context(self,project,roots):
        """Bind a bounded bidirectional artifact neighborhood around the change."""
        seen=set(roots);frontier=set(roots);links={};artifacts={};upper_paths=[]
        bounded=True;max_hops=8;max_nodes=1000;max_links=5000
        for _ in range(max_hops):
            if not frontier:break
            ids=sorted(frontier);placeholders=','.join('?' for _ in ids)
            remaining=max_links-len(links)
            if remaining<=0:
                bounded=False;break
            rows=self.s.all(f"SELECT source,target,relation,confidence,basis FROM links "
                f"WHERE source IN ({placeholders}) OR target IN ({placeholders}) "
                "ORDER BY source,target,relation LIMIT ?",(*ids,*ids,remaining+1))
            if len(rows)>remaining:
                rows=rows[:remaining];bounded=False
            next_nodes=set()
            for link in rows:
                links[canonical(link).decode()]=link
                next_nodes.update((link['source'],link['target']))
            frontier=set()
            for ident in next_nodes-seen:
                row=self.s.one('SELECT id,project FROM artifacts WHERE id=?',(ident,))
                if row is None or row['project']!=project:
                    continue
                seen.add(ident);frontier.add(ident)
            if len(seen)>max_nodes or len(links)>max_links:
                bounded=False;break
        if frontier:
            bounded=False
        for ident in sorted(seen):
            row=self.s.one('SELECT id,project,revision,digest,status,kind,body FROM artifacts WHERE id=?',(ident,))
            if row is None or row['project']!=project:
                continue
            artifacts[ident]={**row,'body':parse_json(row['body'])}
        # A nearby requirement is not a parent contract unless an asserted
        # relation records that exact direction and relationship.
        path_frontier=[(root,root,[]) for root in sorted(roots)];path_seen=set()
        for _ in range(max_hops):
            next_frontier=[]
            for current,origin,path in path_frontier:
                current_row=artifacts.get(current)
                if current_row is None:
                    continue
                remaining_paths=max_links-len(upper_paths)
                if remaining_paths<=0:
                    bounded=False;break
                if current_row['kind']=='requirement':
                    parents=self.s.all("SELECT source AS parent,source,target,relation,confidence,basis FROM links "
                        "WHERE target=? AND relation='decomposes' AND confidence='asserted' ORDER BY source LIMIT ?",
                        (current,remaining_paths+1))
                else:
                    parents=self.s.all("SELECT target AS parent,source,target,relation,confidence,basis FROM links "
                        "WHERE source=? AND relation IN ('realizes','derived_from','supersedes') "
                        "AND confidence='asserted' ORDER BY target,relation LIMIT ?",(current,remaining_paths+1))
                if len(parents)>remaining_paths:
                    parents=parents[:remaining_paths];bounded=False
                for edge in parents:
                    parent=edge['parent']
                    key=(origin,parent,edge['relation'])
                    if parent==current or key in path_seen:
                        continue
                    walk_nodes=[origin]+[step.get('walk_to') for step in path]
                    if parent in walk_nodes:
                        bounded=False
                        continue
                    path_seen.add(key)
                    item={'from':edge['source'],'relation':edge['relation'],'to':edge['target'],
                          'confidence':edge['confidence'],'basis':edge['basis'],
                          'walk_from':current,'walk_to':parent}
                    next_path=path+[item]
                    upper_paths.append({'root':origin,'path':next_path,'upper':parent})
                    next_frontier.append((parent,origin,next_path))
            path_frontier=next_frontier
            if not path_frontier:
                break
        if path_frontier:
            bounded=False
        return {'format':'change-upper-contract-pins.v1','roots':sorted(roots),
            'max_hops':max_hops,'bounded':bounded,
            'artifacts':[artifacts[key] for key in sorted(artifacts)],
            'links':[links[key] for key in sorted(links)],'upper_paths':upper_paths}

    def _change_scope_review(self,change,stage,body,project,impact,upper_contracts,interface_impacts):
        candidates=self._change_fence_candidates(project,change,impact)
        dispositions=[{'id':'scope:'+change,'kind':'layer_scope','subject':change,
                       'allowed':['within_scope','upper_scope_required','unresolved']},
                      {'id':'target:'+change,'kind':'layer_target','subject':change,
                       'allowed':['local_repair','module_replan','system_replan','awaiting_product_decision']}]
        for delta in body.get('deltas',[]):
            dispositions.append({'id':'effect:'+delta['artifact'],'kind':'delta_effect',
                'subject':delta['artifact'],'allowed':['preserves_meaning','within_current_contract',
                                                       'changes_upper_contract','unknown']})
        for task in sorted(candidates):
            dispositions.append({'id':'carry:'+task,'kind':'review_carry_and_task_fence',
                'subject':task,'allowed':['unaffected','affected','unknown'],
                'note':'unaffected is only a candidate; controller pin/fence checks are still required'})
        for interface in interface_impacts:
            names=set(interface.get('declared_consumers',[]))
            names.update(item.get('source') for item in interface.get('registered_consumer_links',[])
                         if isinstance(item.get('source'),str))
            for name in sorted(names):
                marker='consumer:'+interface['interface']+':'+digest(name)[:16]
                dispositions.append({'id':marker,'kind':'interface_consumer','subject':interface['interface'],
                                     'consumer':name,'allowed':['addressed','unknown']})
        for task in sorted(set(body.get('unknown_neighbors',[]))):
            dispositions.append({'id':'unknown-neighbor:'+task,'kind':'declared_unknown_consumer',
                                 'subject':task,'allowed':['addressed','unresolved']})
        return {'format':'change-scope-review.v1','layer':body.get('scope_layer',stage),
            'target_layers':['local_repair','module_replan','system_replan','awaiting_product_decision'],
            'required_dispositions':dispositions,'impact':impact,
            'upper_contracts':upper_contracts,
            'known_interface_consumers':interface_impacts,
            'unknown_neighbors':body.get('unknown_neighbors',[])}

    def change_review_material(self, change):
        """Return one consistent view used both by the prompt and by its binding."""
        from .contracts import interface_impact_context
        from .review_dependencies import accepted_invariants
        with self.s.transaction():
            row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
            body=parse_json(row['body'])
            before_after=[]
            required_coverage=[]
            roots=self._change_roots(body)
            current_impact=self.k.impact(Actor('system','owner'),row['project'],roots)
            current_refs=[{k:a[k] for k in ('id','revision','digest')}
                          for a in [self.k.artifact(Actor('system','owner'),x) for x in roots]]
            for delta in body.get('deltas',[]):
                current=self.k.artifact(Actor('system','owner'),delta['artifact'])
                historical=self.k.artifact(Actor('system','owner'),delta['artifact'],delta['expected_revision'])
                before_after.append({'artifact':delta['artifact'],'kind':current['kind'],
                    'expected_revision':delta['expected_revision'],'expected_digest':historical['digest'],
                    'current':{'revision':current['revision'],'digest':current['digest'],'status':current['status'],
                               'body':current['body']},
                    'before':{'revision':historical['revision'],'digest':historical['digest'],
                              'status':historical['status'],'body':historical['body']},
                    'after':{'body':delta['body'],'status':'withdrawn' if delta.get('withdraw') else 'accepted'}})
                required_coverage.append('change-effect:'+delta['artifact'])
                if current['revision']==delta['expected_revision'] and self._is_display_metadata_repair(current,delta):
                    required_coverage.append('display-metadata-equivalence:'+delta['artifact'])
            evidence=[]
            evidence_ids=list(dict.fromkeys(body.get('evidence',[])+([body['source']] if body.get('source') else [])))
            for ident in evidence_ids:
                source=self.s.one('SELECT id,project,blob,locator,characters,trust FROM sources WHERE id=? AND project=?',(ident,row['project']))
                if source:
                    item={'kind':'source','id':source['id'],'digest':source['blob'],'locator':source['locator'],
                          'characters':source['characters'],'trust':source['trust']}
                    content=self.s.blob_get(source['blob']).decode('utf-8')
                    if len(content)<=100000:
                        item['content']=content
                    else:
                        item.update({'read_operation':'source.read','required_coverage':'source-content:'+ident})
                        required_coverage.append('source-content:'+ident)
                    evidence.append(item)
                    continue
                receipt=self.g.receipt(ident) if self.s.one('SELECT id FROM receipts WHERE id=? AND project=?',(ident,row['project'])) else None
                if receipt:
                    evidence.append({'kind':'receipt','id':ident,'receipt':receipt})
                    continue
                artifact=self.k.artifact(Actor('system','owner'),ident)
                evidence.append({'kind':'artifact','id':ident,'revision':artifact['revision'],'digest':artifact['digest'],
                                 'status':artifact['status'],'body':artifact['body']})
            upper_contracts=self._change_upper_contract_context(row['project'],roots)
            interface_impacts=interface_impact_context(self.k,row['project'],body.get('deltas',[]))
            scope_review=self._change_scope_review(change,row['stage'],body,row['project'],current_impact,
                                                   upper_contracts,interface_impacts)
            required_coverage.extend(item['id'] for item in scope_review['required_dispositions'])
            material={'format':'change-review-material.v2','change':change,'revision':row['revision'],'stage':row['stage'],'body':body,
                      'current':current_refs,'registered_impact':body.get('impact'),
                      'current_impact':current_impact,
                      'impact_matches_registration':canonical(current_impact)==canonical(body.get('impact')),
                      'task_fence_candidates':self._change_fence_candidates(row['project'],change,current_impact),
                      'policy':self.g.policy(row['project'])['digest'],
                      'invariants':accepted_invariants(self.s,row['project']),
                      'before_after':before_after,'evidence':evidence,
                      'upper_contracts':upper_contracts,'scope_review':scope_review,
                      'required_coverage':sorted(set(required_coverage))}
            if interface_impacts:
                material['interface_impact']=interface_impacts
            if len(canonical(material))>700000:
                material['required_coverage']=sorted(set(material['required_coverage']+['change-material:'+change]))
            return row, material

    def _validate_change_scope_result(self,material,result,*,allow_upper=False,require_within=False):
        protocol=material.get('scope_review')
        need(isinstance(protocol,dict) and protocol.get('format')=='change-scope-review.v1',
             'scope_review_required','Change review does not contain the typed layer-scope contract')
        need(material.get('impact_matches_registration') is True,
             'stale_change_impact','Recalculate and stop newly affected tasks before reviewing this change')
        required=protocol.get('required_dispositions',[])
        expected={item['id']:item for item in required}
        need(len(expected)==len(required),'invalid_change_review','Controller scope disposition IDs are not unique')
        covered=set(result.get('covered',[])) if isinstance(result.get('covered'),list) else set()
        missing=set(expected)-covered
        need(not missing,'incomplete_review_coverage','Independent review omitted change scope dispositions',
             {'missing':sorted(missing)})
        observed={}
        for item in result.get('dispositions',[]):
            if not isinstance(item,dict) or item.get('id') not in expected:
                continue
            marker=item['id']
            need(marker not in observed,'invalid_change_review','Change scope disposition is duplicated',marker)
            need(item.get('resolution') in expected[marker]['allowed'],
                 'invalid_change_review','Change scope disposition used an unsupported resolution',
                 {'id':marker,'resolution':item.get('resolution')})
            need(isinstance(item.get('reason'),str) and item['reason'].strip(),
                 'invalid_change_review','Change scope disposition needs a reason',marker)
            observed[marker]=item
        need(set(observed)==set(expected),'invalid_change_review',
             'Independent review did not provide each typed change scope disposition',
             {'missing':sorted(set(expected)-set(observed))})
        scope=observed['scope:'+material['change']]['resolution']
        target=observed['target:'+material['change']]['resolution']
        layer=protocol.get('layer')
        order=['local_repair','module_replan','system_replan','awaiting_product_decision']
        need(layer in order and target in order,'invalid_change_review',
             'Current change layer or reviewed target layer is unsupported')
        if scope=='within_scope':
            need(target==layer,'invalid_change_review',
                 'A within-layer disposition must target the current layer',
                 {'layer':layer,'target':target})
        elif scope=='upper_scope_required':
            need(order.index(target)>order.index(layer),'invalid_change_review',
                 'An upper-scope disposition must identify a strictly higher layer',
                 {'layer':layer,'target':target})
        else:
            need(target==layer,'invalid_change_review',
                 'An unresolved disposition cannot authorize layer advancement',
                 {'layer':layer,'target':target})
        effects={item['subject']:observed[item['id']]['resolution']
                 for item in required if item['kind']=='delta_effect'}
        carry={item['subject']:observed[item['id']]['resolution']
               for item in required if item['kind']=='review_carry_and_task_fence'}
        consumers={item['id']:observed[item['id']]['resolution']
                   for item in required if item['kind']=='interface_consumer'}
        unknown={item['subject']:observed[item['id']]['resolution']
                 for item in required if item['kind']=='declared_unknown_consumer'}
        need(set(effects)=={item['artifact'] for item in material['body'].get('deltas',[])},
             'invalid_change_review','Typed effect decisions do not cover the exact current delta set')
        after_by={item['artifact']:item for item in material.get('before_after',[])}
        observations=result.get('observations',[])
        for artifact,resolution in effects.items():
            detail=after_by[artifact]
            if resolution=='preserves_meaning':
                need(any(isinstance(obs,dict) and obs.get('ref')==artifact and
                         isinstance(obs.get('detail'),str) and obs['detail'].strip()
                         for obs in observations),
                     'invalid_change_review','A meaning-preserving judgment must cite the exact artifact',artifact)
            if resolution=='within_current_contract':
                pins=material['upper_contracts']
                upper_by={item['id']:item for item in pins.get('artifacts',[])}
                def asserted_upper_path(path):
                    if (path.get('root')!=artifact or path.get('upper')==artifact or
                            upper_by.get(path.get('upper'),{}).get('status')!='accepted' or
                            upper_by.get(path.get('upper'),{}).get('kind') not in {'requirement','interface'} or
                            not path.get('path')):
                        return False
                    current=artifact;visited={artifact}
                    for edge in path['path']:
                        target=edge.get('walk_to');source=edge.get('walk_from')
                        current_kind=upper_by.get(current,{}).get('kind')
                        expected_relation={'decomposes'} if current_kind=='requirement' else {
                            'realizes','derived_from','supersedes'}
                        direction=(edge.get('relation') in expected_relation and
                            edge.get('confidence')=='asserted' and source==current and
                            isinstance(target,str) and target not in visited and
                            ((edge.get('relation')=='decomposes' and
                              edge.get('to')==source and edge.get('from')==target) or
                             (edge.get('relation')!='decomposes' and
                              edge.get('from')==source and edge.get('to')==target)))
                        if not direction:return False
                        current=target;visited.add(target)
                    return current==path.get('upper')
                linked_parent=any(asserted_upper_path(path) for path in pins.get('upper_paths',[]))
                need(pins.get('bounded') is True and linked_parent,
                     'upper_contract_required','A within-contract change needs an exact asserted parent contract pin',artifact)
            if detail['kind']=='interface':
                before=detail['before']['body'];after=detail['after']['body']
                changed_contract_fields={key for key in set(before)|set(after)
                    if key not in {'title','description'} and canonical(before.get(key))!=canonical(after.get(key))}
                if changed_contract_fields and resolution!='preserves_meaning':
                    need(scope!='within_scope','unknown_consumer',
                         'Interface contract changes cannot close while external consumer completeness is unknown',
                         {'artifact':artifact,'fields':sorted(changed_contract_fields)})
            if resolution in {'changes_upper_contract','unknown'}:
                need(scope!='within_scope','upper_scope_required',
                     'A changed or unknown upper contract cannot be closed in this layer',artifact)
        for marker,resolution in consumers.items():
            need(resolution!='unknown' or scope!='within_scope','unknown_consumer',
                 'Unknown interface consumers prevent local closure',marker)
        for task,resolution in unknown.items():
            need(scope!='within_scope','unknown_impact',
                 'Caller-declared unknown consumers require controller discovery or upper review',task)
        if not material['upper_contracts'].get('bounded'):
            need(scope!='within_scope','upper_contract_unbounded',
                 'The linked upper-contract neighborhood exceeds the bounded review scope')
        if require_within:
            need(scope=='within_scope','scope_escalation_required',
                 'The independent review did not close this change within the current layer',scope)
        if not allow_upper:
            need(scope=='within_scope','scope_escalation_required',
                 'The independent review requires an upper layer or left scope unresolved',scope)
        interface_unknown=[]
        for detail in after_by.values():
            if detail['kind']!='interface':
                continue
            before=detail['before']['body'];after=detail['after']['body']
            changed_fields={key for key in set(before)|set(after)
                if key not in {'title','description'} and canonical(before.get(key))!=canonical(after.get(key))}
            if changed_fields and effects[detail['artifact']]!='preserves_meaning':
                interface_unknown.append(detail['artifact'])
        return {'scope':scope,'target_layer':target,'layer':layer,
                'effects':effects,'carry':carry,'consumers':consumers,
                'unknown_neighbors':unknown,'dispositions':observed,
                'unknown_interface_contracts':sorted(interface_unknown),
                'all_meaning_preserved':bool(effects) and all(value=='preserves_meaning' for value in effects.values())}

    def _revalidate_unaffected_change_tasks(self,actor,change,material,scope_result,receipt):
        """Carry only a fenced, unstarted task whose exact pins can be advanced."""
        if (not scope_result['all_meaning_preserved'] or material['body'].get('unknown_neighbors') or
                material['upper_contracts'].get('bounded') is not True):
            return []
        candidates=material.get('task_fence_candidates',{})
        approved={task for task,disposition in scope_result['carry'].items() if disposition=='unaffected'}
        deltas={item['artifact']:item for item in material['body'].get('deltas',[])}
        row=self.s.one('SELECT project,body,stage FROM changes WHERE id=?',(change,),True)
        project=row['project'];change_body=parse_json(row['body'])
        roots=sorted(deltas)
        per_root={root:set(self.k.impact(Actor('system','owner'),project,[root])['tasks']) for root in roots}
        valid=[]
        for task in sorted(approved & set(candidates)):
            before=candidates[task]
            task_row=self.s.one('SELECT * FROM tasks WHERE id=? AND project=?',(task,project))
            if task_row is None:
                continue
            reads_before=before.get('reads',[])
            if not any(pin['artifact'] in deltas for pin in reads_before):
                continue
            current_reads=self.s.all('SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact',(task,))
            if canonical(current_reads)!=canonical(reads_before):
                continue
            updated_pins=[];pin_updates=[];eligible=True
            for pin in reads_before:
                artifact=self.s.one('SELECT project,revision,digest,status FROM artifacts WHERE id=?',(pin['artifact'],))
                delta=deltas.get(pin['artifact'])
                if delta:
                    if (artifact is None or artifact['project']!=project or artifact['status']!='accepted' or
                            artifact['revision']!=delta['expected_revision']+1 or
                            artifact['digest']!=digest(delta['body']) or
                            pin['revision']!=delta['expected_revision']):
                        eligible=False;break
                    updated={'artifact':pin['artifact'],'revision':artifact['revision'],'digest':artifact['digest']}
                    pin_updates.append((updated,pin))
                else:
                    if (artifact is None or artifact['project']!=project or artifact['status']!='accepted' or
                            artifact['revision']!=pin['revision'] or artifact['digest']!=pin['digest']):
                        eligible=False;break
                    updated=pin
                updated_pins.append(updated)
            expected_roots=[root for root in roots if task in per_root[root]]
            expected_blocks=[{'kind':'change','ref':change,'reason':change_body['reason']}]
            expected_blocks.extend({'kind':'changed_input','ref':root,'reason':'Change '+change}
                                   for root in expected_roots)
            current_blocks=self.s.all('SELECT kind,ref,reason FROM blocks WHERE task=? ORDER BY kind,ref',(task,))
            expected_epoch=before['epoch']+1+len(expected_roots)
            if (not eligible or not expected_roots or task_row['status']!='planned' or
                    task_row['validity']!='needs_review' or task_row['revision']!=before['revision'] or
                    task_row['epoch']!=expected_epoch or task_row['candidate'] is not None or
                    task_row['lease_owner'] is not None or task_row['lease_until'] is not None or
                    task_row['paused'] or digest(task_row['body'].encode())!=before['task_body_digest'] or
                    canonical(current_blocks)!=canonical(expected_blocks) or
                    self.s.one('SELECT task FROM plans WHERE task=?',(task,)) is not None or
                    self.s.one('SELECT 1 FROM runs WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM candidates WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM execution_attempts WHERE task=? LIMIT 1',(task,)) is not None or
                    self.s.one('SELECT 1 FROM local_execution_records WHERE task=? LIMIT 1',(task,)) is not None):
                continue
            old_pins=[]
            for updated,pin in pin_updates:
                cursor=self.s.execute('UPDATE task_reads SET revision=?,digest=? WHERE task=? AND artifact=? AND revision=? AND digest=?',
                    (updated['revision'],updated['digest'],task,pin['artifact'],pin['revision'],pin['digest']))
                need(cursor.rowcount==1,'stale_task_fence',
                     'A task input changed while its unaffected status was being revalidated',
                     {'task':task,'artifact':pin['artifact']})
                old_pins.append(pin)
            cursor=self.s.execute("UPDATE tasks SET validity='current',updated=? WHERE id=? AND project=? "
                "AND status='planned' AND validity='needs_review' AND revision=? AND epoch=? "
                "AND candidate IS NULL AND lease_owner IS NULL AND lease_until IS NULL AND paused=0",
                (timestamp(),task,project,before['revision'],expected_epoch))
            need(cursor.rowcount==1,'stale_task_fence',
                 'A task changed while its unaffected status was being revalidated',task)
            for root in expected_roots:
                self.s.execute("DELETE FROM blocks WHERE task=? AND kind='changed_input' AND ref=?",(task,root))
            proof={'task':task,'change':change,'receipt':receipt,'before_epoch':before['epoch'],
                'current_epoch':expected_epoch,'task_body_digest':before['task_body_digest'],
                'old_pins':old_pins,'new_pins':[item for item,_ in pin_updates],
                'cleared_input_blocks':expected_roots,'scope_disposition':'unaffected'}
            self.sec.event(project,'task_revalidated_after_unaffected_change',actor.id,proof)
            valid.append(proof)
        return valid

    def _has_current_system_no_solution_path(self,change,scope_result):
        """Accept a user decision after a reviewed, final system-layer infeasibility attempt."""
        if (scope_result.get('scope')!='within_scope' or
                scope_result.get('layer')!='system_replan' or
                scope_result.get('target_layer')!='system_replan'):
            return False
        latest=self.s.one("SELECT a.*,e.body AS attempt_event FROM attempts a JOIN events e "
            "ON e.project=(SELECT project FROM changes WHERE id=a.change_id) "
            "AND e.kind='feasibility_attempt' AND json_extract(e.body,'$.attempt')=a.id "
            "WHERE a.change_id=? ORDER BY e.seq DESC,e.id DESC LIMIT 1",(change,))
        if latest is None or latest['level']!='system_replan':
            return False
        attempt={'id':latest['id'],'body':latest['body']}
        body=parse_json(attempt['body']);receipt_id=body.get('review_receipt')
        if body.get('outcome')!='no_solution_found' or not isinstance(receipt_id,str):
            return False
        event_body=parse_json(latest['attempt_event'])
        if (event_body.get('stage')!='awaiting_product_decision' or
                event_body.get('outcome')!='no_solution_found' or
                event_body.get('scope_disposition')!='within_scope' or
                event_body.get('scope_target')!='system_replan' or
                event_body.get('review_receipt')!=receipt_id):
            return False
        review=self.g.receipt(receipt_id)
        if review.get('role')!='feasibility' or review.get('subject')!=change or \
                review.get('result',{}).get('verdict')!='pass':
            return False
        self.g.require_review(receipt_id,change,review['binding'],{'feasibility'},latest=True)
        return True

    def change_binding(self,change):
        _, material=self.change_review_material(change)
        return digest(material)

    def change_get(self,actor,change):
        identity=self.s.one('SELECT project FROM changes WHERE id=?',(change,),True)
        self.k.project(actor,identity['project'])
        row,material=self.change_review_material(change)
        body=parse_json(row['body'])
        return {'id':change,'project':row['project'],'revision':row['revision'],'stage':row['stage'],
                'title':body['title'],'origin':body['origin'],'affected':body['affected'],
                'no_effect':bool(body.get('no_effect')),
                'binding':digest(material),'material_format':material['format'] if 'format' in material else 'change-review-material.v1',
                'read_operation':'change.read','read_digest':digest(canonical(material))}

    def change_read(self,actor,change,expected_digest,offset=0,byte_budget=12000):
        identity=self.s.one('SELECT project FROM changes WHERE id=?',(change,),True)
        self.k.project(actor,identity['project'])
        row,material=self.change_review_material(change)
        raw=canonical(material).decode()
        from .navigation import Navigation
        return Navigation._fragment(raw,offset,byte_budget,expected_digest,
            {'change':change,'project':row['project'],'revision':row['revision'],'stage':row['stage'],
             'binding':digest(material)})

    def attempt(self,actor,change,level,body):
        row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
        actor.require('owner','agent',project=row['project'])
        need(level in {'local_repair','module_replan','system_replan'} and row['stage']==level,'wrong_escalation_level','Resolve the current design level before escalating')
        obj(body,required=('hypothesis','alternatives','evidence','outcome','remaining_unknown'),optional=('resource_limit','solution','review_receipt'))
        text(body['hypothesis'],'hypothesis',12000);strings(body['alternatives'],'alternatives',nonempty=True)
        strings(body['evidence'],'evidence',nonempty=True);text(body['remaining_unknown'],'unknowns',12000,empty=True)
        need(body['outcome'] in {'solution','no_solution_found','resource_exhausted','scope_exceeded'},
             'invalid_outcome','Invalid feasibility or scope result')
        for ev in body['evidence']: self.g.receipt(ev)
        scope_result=None
        if body['outcome'] in {'no_solution_found','scope_exceeded'}:
            need(body.get('review_receipt'),'review_required','Independent assessment is required before escalation')
            review=self.g.receipt(body['review_receipt'])
            _,material=self.change_review_material(change)
            if review.get('role')=='consistency':
                self.g.require_review(body['review_receipt'],change,digest(material),{'consistency'},latest=True)
            else:
                self.g.require_review(body['review_receipt'],change,self.change_binding(change),{'feasibility'},latest=True)
            scope_result=self._validate_change_scope_result(material,review.get('result',{}),
                allow_upper=True,require_within=False)
            need(scope_result['scope']!='unresolved','scope_unresolved',
                 'An unresolved scope review cannot advance this change')
            if body['outcome']=='scope_exceeded':
                need(scope_result['scope']=='upper_scope_required','scope_escalation_required',
                     'Direct layer routing requires an independently reviewed upper-scope disposition')
        with self.s.transaction():
            ident=uid('ATTEMPT')
            self.s.execute("INSERT INTO attempts VALUES(?,?,?,?,?)",(ident,change,level,canonical(body).decode(),timestamp()))
            if body['outcome']=='no_solution_found':
                next_stage=(scope_result['target_layer'] if scope_result['scope']=='upper_scope_required' else
                    {'local_repair':'module_replan','module_replan':'system_replan',
                     'system_replan':'awaiting_product_decision'}[level])
            elif body['outcome']=='scope_exceeded': next_stage=scope_result['target_layer']
            elif body['outcome']=='solution': next_stage='reconciling'
            else: next_stage=level
            change_body=parse_json(row['body'])
            change_body['scope_layer']=(level if next_stage=='awaiting_product_decision' else next_stage) \
                if body['outcome'] in {'no_solution_found','scope_exceeded'} else level
            self.s.execute("UPDATE changes SET stage=?,body=?,revision=revision+1 WHERE id=?",
                           (next_stage,canonical(change_body).decode(),change))
            if next_stage=='awaiting_product_decision' or body['outcome']=='resource_exhausted':
                kind='product_decision' if next_stage=='awaiting_product_decision' else 'search_incomplete'
                self._publish_change_notice(row['project'],change,parse_json(self.s.one('SELECT body FROM changes WHERE id=?',(change,),True)['body']),body,kind=kind)
            self.sec.event(row['project'],'feasibility_attempt',actor.id,{'change':change,'level':level,
                'attempt':ident,
                'outcome':body['outcome'],'stage':next_stage,
                'scope_disposition':scope_result['scope'] if scope_result else None,
                'scope_target':scope_result['target_layer'] if scope_result else None,
                'review_receipt':body.get('review_receipt')})
        note=('The reviewed scope requires a higher layer; no infeasibility is claimed.'
              if body['outcome']=='scope_exceeded' else 'Resource exhaustion is not proof of infeasibility.')
        return {'id':ident,'stage':next_stage,'note':note}

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
            if (not force_revision and body.get('no_effect') and
                    canonical(body.get('deltas',[]))==canonical(deltas)):
                for ref in body['baseline_refs']:
                    art=self.k.artifact(actor,ref['id'])
                    need(art['revision']==ref['revision'] and art['digest']==ref['digest'],
                         'stale_revision','Change baseline is no longer current')
                replay_roots=self._change_roots(body)
                replay_impact=self.k.impact(actor,row['project'],replay_roots)
                if replay_roots==body.get('impact_roots') and canonical(replay_impact)==canonical(body.get('impact')):
                    self.sec.event(row['project'],'change_delta_replayed',actor.id,
                                   {'change':change,'revision':expected_revision})
                    return {'id':change,'revision':expected_revision,'binding':self.change_binding(change),
                            'unchanged':True,'no_effect':True}
            if (not force_revision and history and history[-1].get('actor')==actor.id
                and history[-1]['reason']==reason and canonical(body.get('deltas',[]))==canonical(deltas)):
                for ref in body['baseline_refs']:
                    art=self.k.artifact(actor,ref['id'])
                    need(art['revision']==ref['revision'] and art['digest']==ref['digest'],
                         'stale_revision','Change baseline is no longer current')
                replay_roots=self._change_roots(body)
                replay_impact=self.k.impact(actor,row['project'],replay_roots)
                if (replay_roots==body.get('impact_roots') and
                        canonical(replay_impact)==canonical(body.get('impact'))):
                    self.sec.event(row['project'],'change_delta_replayed',actor.id,{'change':change,'revision':expected_revision})
                    return {'id':change,'revision':expected_revision,'binding':self.change_binding(change),'unchanged':True}
            old_impact=body.get('impact',{})
            was_no_effect=bool(body.get('no_effect'))
            body['deltas']=deltas
            no_effect=self._change_has_no_effect(actor,row['project'],body,deltas)
            body['no_effect']=no_effect
            roots=self._change_roots(body)
            current_refs={}
            for artifact_id in roots:
                artifact=self.k.artifact(actor,artifact_id)
                current_refs[artifact_id]={k:artifact[k] for k in ('id','revision','digest')}
                previous=next((item for item in body.get('baseline_refs',[]) if item.get('id')==artifact_id),None)
                changed_target=any(item['artifact']==artifact_id for item in deltas)
                if previous is not None and not changed_target:
                    need(previous['revision']==artifact['revision'] and previous['digest']==artifact['digest'],
                         'stale_revision','An affected artifact changed while the change was open',artifact_id)
            new_impact=self.k.impact(actor,row['project'],roots)
            unknown=set(body.get('unknown_neighbors',[]))
            new_tasks=(set(new_impact.get('tasks',[])) if was_no_effect else
                       set(new_impact.get('tasks',[]))-set(old_impact.get('tasks',[])))
            task_fence=({'format':'keep-existing-task-fence.v1','change':change,
                         'candidates':{},'scope':'no_effect'} if no_effect else
                self._capture_keep_existing_task_fence(row['project'],change,
                    {**new_impact,'tasks':sorted(new_tasks)},unknown))
            body['impact_roots']=roots;body['impact']=new_impact
            body['baseline_refs']=[current_refs[item] for item in roots]
            body.setdefault('delta_history',[]).append({'reason':reason,'revision':expected_revision,'actor':actor.id,'stage':row['stage']})
            self.s.execute("UPDATE changes SET body=?,revision=revision+1 WHERE id=?",(canonical(body).decode(),change))
            self._terminalize_change_decisions(row['project'],change,actor.id,'change_delta_revised')
            task_revalidations=[]
            if no_effect:
                task_revalidations=self._restore_no_effect_task_fences(actor,row['project'],change,
                    body,new_impact)
                self.s.execute("DELETE FROM blocks WHERE kind='change' AND ref=?",(change,))
            else:
                for task in sorted(new_tasks|unknown):
                    self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(task,'change',change,body['reason']))
                    self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1,lease_until=NULL,updated=? WHERE id=?",
                                   (timestamp(),task))
            if no_effect:
                self._close_notices(row['project'],change,actor.id,'change_has_no_effect')
                if row['stage']=='awaiting_product_decision':
                    self.g.inbox(row['project'],'search_incomplete',change,{
                        'change':change,'revision':expected_revision+1,'no_effect':True,
                        'request_fulfilled':False,
                        'message':'The submitted artifact delta leaves accepted artifacts unchanged; the source request remains unresolved.'},'warning')
                self.sec.event(row['project'],'change_no_effect_observed',actor.id,{'change':change,
                    'origin':body['origin'],'source':body.get('source'),'request_fulfilled':False,
                    'delta_targets':sorted(item['artifact'] for item in deltas),
                    'task_revalidations':task_revalidations})
            else:
                self._refresh_change_notice(row['project'],change,body,row['stage'],actor.id)
            self.sec.event(row['project'],'change_impact_recalculated',actor.id,{'id':change,
                'impact':new_impact,'added_tasks':sorted(new_tasks),'task_fence':task_fence,
                'no_effect':no_effect})
            self.sec.event(row['project'],'change_delta_revised',actor.id,{'change':change,'revision':expected_revision+1})
            binding=self.change_binding(change)
        return {'id':change,'revision':expected_revision+1,'binding':binding,
                'no_effect':no_effect,'task_revalidations':task_revalidations}

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
            if clean.get('supersedes'):
                clean['supersession_closure']=self._supersession_closure(project,clean['supersedes'])
            artifacts=[self.k.artifact(Actor('control','owner'),x) for x in clean.get('refs',[])]
            need(all(a['project']==project for a in artifacts),'cross_project','Decision reference belongs elsewhere')
            return {'format':'provisional-decision-review.v1','project':project,'proposal':clean,
                    'refs':[{k:a[k] for k in ('id','revision','digest')} for a in artifacts],
                    'invariants':accepted_invariants(self.s,project),
                    'policy':self.g.policy(project)['digest']}

    def provisional_binding(self,project,body):
        return digest(self.provisional_review_material(project,body))

    @staticmethod
    def _decision_has_effects(body):
        return bool(body.get('change') or body.get('conflict') or body.get('supersedes') or
                    body.get('type')=='policy')

    def _choice_effect(self,body,choice):
        """Resolve a human choice to the fixed effect sealed by the proposal."""
        if choice=='reject':return 'reject'
        if choice=='defer':return 'defer'
        declared=body.get('choice_effects',{})
        if choice in declared:return declared[choice]
        if choice=='approve':return 'accept'
        if choice=='keep_existing':return 'keep_existing'
        if self._decision_has_effects(body):return None
        return 'record_only'

    def _decision_effect_actions(self,body,effect):
        """The shared selection rule used by single apply and batch projection."""
        need(effect in {'accept','keep_existing','record_only'},'choice_effect_required',
             'This choice has no declared effect for a decision with linked changes')
        if self._decision_has_effects(body):
            need(effect!='record_only','invalid_choice_effect',
                 'A side-effecting decision cannot use a record-only choice')
        if body.get('conflict'):
            need(effect in {'accept','keep_existing'},'change_required',
                 'Conflict resolution must choose a converging change or keep the existing requirement')
            need(effect!='accept' or bool(body.get('change')),'change_required',
                 'Conflict resolution must specify a converging change or keep the existing requirement')
        return {'apply_change':effect=='accept' and bool(body.get('change')),
                'decline_change':effect=='keep_existing' and bool(body.get('change')),
                'apply_policy':effect=='accept' and body.get('type')=='policy',
                'resolve_conflict':bool(body.get('conflict')),
                'supersede':effect=='accept' and bool(body.get('supersedes'))}

    def _validate_choice_effects(self,body):
        effects=body.get('choice_effects',{})
        need(isinstance(effects,dict),'invalid_choice_effect','choice_effects must map choices to effects')
        reserved={'approve','keep_existing','reject','defer'}
        allowed={'accept','keep_existing','record_only'}
        for choice,effect in effects.items():
            need(isinstance(choice,str) and choice in body['options'] and choice not in reserved,
                 'invalid_choice_effect','choice_effects may name only displayed nonreserved choices')
            need(effect in allowed,'invalid_choice_effect','Unknown choice effect')
        has_effects=self._decision_has_effects(body)
        for choice in body['options']:
            effect=self._choice_effect(body,choice)
            need(effect is not None,'choice_effect_required',
                 'Declare the effect for every displayed choice on a decision with linked effects',
                 {'choice':choice})
            need(not (has_effects and effect=='record_only'),'invalid_choice_effect',
                 'A side-effecting decision cannot include a record-only choice',{'choice':choice})
            if body.get('conflict') and effect not in {'reject','defer'}:
                self._decision_effect_actions(body,effect)

    def propose_decision(self,actor,project,body):
        with self.s.transaction():
            return self._propose_decision(actor,project,body)

    def _propose_decision(self,actor,project,body):
        actor.require('owner','agent',project=project)
        obj(body,required=('title','reason','options','recommendation','refs','requirement_affecting'),
            optional=('change','conflict','supersedes','provisional','expires','reversible','consistency_receipt',
                      'choice_effects'))
        for f in ('title','reason','recommendation'):text(body[f],f,20000)
        strings(body['options'],'options',nonempty=True);strings(body['refs'],'refs')
        need(type(body['requirement_affecting']) is bool,'invalid_decision','Requirement-affecting flag must be boolean')
        body={**body,'bindings':[{k:a[k] for k in ('id','revision','digest')} for a in [self.k.artifact(actor,x) for x in body['refs']]]}
        if body.get('change'):
            change=self.s.one("SELECT * FROM changes WHERE id=? AND project=?",(body['change'],project),True)
            need(change['stage']=='awaiting_product_decision','premature_escalation',
                 'An independent scope review must route this change to the product-decision layer before a human decision is proposed')
            body['change_binding']=self.change_binding(body['change'])
        if body.get('conflict'):self.s.one("SELECT id FROM conflicts WHERE id=? AND project=?",(body['conflict'],project),True)
        if body.get('supersedes'):
            self.s.one("SELECT id FROM decisions WHERE id=? AND project=?",(body['supersedes'],project),True)
            body={**body,'supersession_closure':self._supersession_closure(project,body['supersedes'])}
        self._validate_choice_effects(body)
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

    def _decision_expiry_due(self, row):
        body = parse_json(row['body'])
        if body.get('provisional') is not True:
            return None
        timer = self.s.one("SELECT * FROM timers WHERE kind='decision_expiry' AND ref=?", (row['id'],))
        return timer['due'] if timer else body.get('expires')

    def _timely_final_response(self, row, due):
        if due is None or row.get('response') in (None, 'defer') or row.get('source') is None:
            return False
        # The response event, rather than registration of its source, proves
        # that the human made a final choice before the provisional window.
        # A same-choice/same-source quote correction can follow later. An
        # intervening defer or a changed answer/source starts a new answer
        # chain, so an older unrelated final answer cannot keep it alive.
        events = self.s.all("SELECT seq,body,created FROM events WHERE project=? AND kind='human_response_observed' "
                            "AND json_extract(body,'$.decision')=? ORDER BY seq ASC",
                            (row['project'], row['id']))
        if not events:
            return False
        latest = parse_json(events[-1]['body'])
        if (latest.get('digest') != row['digest'] or latest.get('choice') != row['response'] or
                latest.get('source') != row['source']):
            return False
        chain = []
        for event in reversed(events):
            answer = parse_json(event['body'])
            if (answer.get('digest') != row['digest'] or answer.get('choice') != row['response'] or
                    answer.get('source') != row['source']):
                break
            chain.append(event)
        answer_time=lambda event: parse_json(event['body']).get('response_observed_at',event['created'])
        if not chain or answer_time(chain[-1]) >= due:
            return False
        # Durable ordering prevents a late answer from becoming timely if a
        # clock rollback gives its event a pre-deadline wall-clock value.
        timer=self.s.one("SELECT id,fired FROM timers WHERE kind='decision_expiry' AND ref=?",(row['id'],))
        deadline_events = self.s.all("SELECT seq FROM events WHERE project=? AND "
            "((kind='decision_expired' AND json_extract(body,'$.decision')=?) OR "
            "(kind='timer_fired' AND (json_extract(body,'$.decision')=? OR json_extract(body,'$.timer')=?))) ORDER BY seq LIMIT 1",
            (row['project'], row['id'], row['id'], timer['id'] if timer else None))
        if timer and timer['fired'] is not None and not deadline_events:
            # Old stores can contain a fired timer without its corresponding
            # sequence event. The timestamp alone cannot prove that a later
            # answer preceded the deadline after clock rollback.
            return False
        return not deadline_events or chain[-1]['seq'] < deadline_events[0]['seq']

    def _expire_due_decision(self, project, decision, *, actor='system', now=None):
        """Expire one overdue unresolved provisional decision inside the caller's writer tx.

        This helper never raises after mutating state. Callers can return its
        result so an enclosing RPC transaction commits the expiry instead of
        rolling it back with a stale-decision Fault.
        """
        now = timestamp() if now is None else now
        row = self.s.one('SELECT * FROM decisions WHERE id=? AND project=?', (decision, project))
        if row is None:
            return None
        timer = self.s.one("SELECT * FROM timers WHERE kind='decision_expiry' AND ref=?", (decision,))
        timer_was_fired = bool(timer and timer['fired'] is not None)
        due = self._decision_expiry_due(row)
        if due is None:
            return None
        # A fired timer is durable proof that the deadline was reached. Keep
        # that fact if the wall clock later moves backwards.
        effective_now = max(now, timer['fired']) if timer_was_fired else now
        if due > effective_now:
            if row['status'] == 'expired':
                return {'id': decision, 'status': 'expired', 'expired': True,
                        'answered': False, 'must_reconcile': True,
                        'timer_fired': timer_was_fired}
            return None
        body = parse_json(row['body'])
        if row['status'] == 'expired':
            if timer and not timer_was_fired:
                cursor=self.s.execute('UPDATE timers SET fired=? WHERE id=? AND fired IS NULL',(now,timer['id']))
                if cursor.rowcount:
                    self.sec.event(project,'timer_fired',actor,{'timer':timer['id'],'decision':decision,
                        'reason':'provisional_expired','lag_seconds':max(0,now-due)})
            return {'id': decision, 'status': 'expired', 'expired': True,
                    'answered': False, 'must_reconcile': True,
                    'timer_fired': timer_was_fired}
        timely_final=self._timely_final_response(row,due)
        if row['status'] in {'applied', 'withdrawn', 'superseded'} or timely_final:
            if timer and not timer_was_fired:
                cursor = self.s.execute('UPDATE timers SET fired=? WHERE id=? AND fired IS NULL', (now, timer['id']))
                if cursor.rowcount:
                    self.sec.event(project, 'timer_fired', actor, {'timer': timer['id'],
                        'decision': decision, 'reason': 'final_answer_recorded_before_expiry',
                        'lag_seconds': max(0, now - due)})
            return {'id': decision, 'expired': row['status'] == 'expired', 'timer_fired': bool(timer)}
        if row['status'] not in {'provisional', 'deferred', 'decision_received', 'rejected'}:
            if timer and not timer_was_fired:
                cursor = self.s.execute('UPDATE timers SET fired=? WHERE id=? AND fired IS NULL', (now, timer['id']))
                if cursor.rowcount:
                    self.sec.event(project, 'timer_fired', actor,
                                   {'timer': timer['id'], 'decision': decision,
                                    'reason': 'decision_not_expirable', 'lag_seconds': max(0, now - due)})
            return {'id': decision, 'expired': False, 'timer_fired': bool(timer)}
        self._terminalize_decision(project, decision, actor, 'decision_expired', status='expired')
        newly_expired = row['status'] != 'expired'
        if newly_expired and body.get('refs'):
            self.k.invalidate(project, body['refs'], 'Provisional decision expired')
        self.g.inbox(project, 'decision', decision,
                     {'expired': True, 'must_reconcile': True}, 'critical', due)
        if timer and not timer_was_fired:
            cursor = self.s.execute('UPDATE timers SET fired=? WHERE id=? AND fired IS NULL', (now, timer['id']))
            if cursor.rowcount:
                self.sec.event(project, 'timer_fired', actor, {'timer': timer['id'],
                    'decision': decision, 'reason': 'provisional_expired',
                    'lag_seconds': max(0, now - due)})
        if newly_expired:
            self.sec.event(project, 'decision_expired', actor,
                           {'decision': decision, 'due': due, 'from_status': row['status'],
                            'critical_notice_required': True, 'timer_already_fired': timer_was_fired})
        return {'id': decision, 'status': 'expired', 'expired': True,
                'answered': False, 'must_reconcile': True}

    def _require_timely_decision_answer(self, row):
        due = self._decision_expiry_due(row)
        if due is not None:
            need(self._timely_final_response(row, due), 'stale_decision',
                 'A provisional decision needs a final human response recorded before its expiry; propose it again')

    def respond(self,actor,decision,expected_digest,choice,utterance,source=None):
        actor.require('owner')
        text(utterance,'human utterance',100000)
        with self.s.transaction():
            # Re-read under the writer transaction so direct invoke callers
            # receive the same compare-and-swap protection as RPC callers.
            row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
            expiry=self._expire_due_decision(row['project'],decision,actor=actor.id)
            if expiry and expiry.get('expired'):
                return expiry
            row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
            body=parse_json(row['body'])
            need(row['digest']==expected_digest and row['status'] in {'pending','provisional','decision_received','deferred'},'stale_decision','Respond to the exact pending proposal')
            allowed=body.get('options',['approve','reject','defer'])
            need(choice in allowed or choice in {'reject','defer'},'invalid_choice','Choose one of the displayed options')
            effect=self._choice_effect(body,choice)
            need(effect is not None,'choice_effect_required',
                 'This proposal does not bind an effect for the selected choice; create a proposal with explicit choice_effects')
            if self._decision_has_effects(body) and effect=='record_only':
                raise Fault('invalid_choice_effect','A side-effecting decision cannot use a record-only choice')
            if effect not in {'reject','defer'}:
                self._decision_effect_actions(body,effect)
            self._validate_response_current(actor,row,body)
            prior=self.response_evidence(decision) if row['status'] in {'decision_received','deferred'} else None
            if source is None:
                src=self.k.source(actor,row['project'],utterance,'trusted-dialogue:'+decision)
                self.k.classify(actor,src['id'],0,len(utterance),'reference',[],'Authenticated response to exact versioned decision '+decision)
                quote={'source':src['id'],'source_digest':src['digest'],'start':0,
                       'end':len(utterance),'quote':utterance}
            else:
                quote=self._human_quote_after_proposal(actor,row,decision,source,utterance)
            due=self._decision_expiry_due(row)
            if due is not None and timestamp() >= due and self._timely_final_response(row,due):
                # The timely final choice resolves the provisional deadline.
                # Afterward only an exact retry or quote-only correction from
                # the same source can refine its evidence; a changed answer
                # needs a new versioned proposal.
                same_answer=(row['status']=='decision_received' and prior is not None and
                             choice==row['response'] and quote['source']==row['source'])
                need(same_answer,'stale_decision',
                     'A final provisional choice cannot be revised after expiry; create a new proposal')
            if prior:
                if choice!=row['response'] and quote['source']==row['source']:
                    raise Fault('stale_user_input','Changing a decision choice requires a new human source after the previous answer')
                if quote['source']!=row['source']:
                    registered=self.s.one("SELECT seq FROM events WHERE project=? AND kind='source_registered' "
                        "AND json_extract(body,'$.source')=? ORDER BY seq DESC LIMIT 1",(row['project'],quote['source']))
                    need(registered is not None and registered['seq']>prior['seq'],'stale_user_input',
                         'A revised answer needs a human source recorded after the previous answer')
                old=prior['body']
                if (choice==row['response'] and quote['source']==row['source'] and
                    quote.get('source_digest')==old.get('source_digest') and
                    quote.get('start')==old.get('start') and quote.get('end')==old.get('end') and
                    quote.get('quote')==old.get('quote')):
                    expiry=self._expire_due_decision(row['project'],decision,actor=actor.id)
                    if expiry and expiry.get('expired'):
                        return expiry
                    return {'id':decision,'status':row['status'],
                            'consistency_recheck_required':row['status']=='decision_received'}
            # Source creation and quote validation can take long enough to
            # cross the deadline after the entry check. Reconcile immediately
            # before the answer write so a late first answer is never recorded.
            expiry=self._expire_due_decision(row['project'],decision,actor=actor.id)
            if expiry and expiry.get('expired'):
                return expiry
            row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
            response_observed_at=timestamp()
            due=self._decision_expiry_due(row)
            if due is not None and response_observed_at>=due and not self._timely_final_response(row,due):
                expiry=self._expire_due_decision(row['project'],decision,actor=actor.id,
                                                  now=response_observed_at)
                if expiry and expiry.get('expired'):
                    return expiry
            source_id=quote['source']
            status='rejected' if choice=='reject' else 'deferred' if choice=='defer' else 'decision_received'
            cursor=self.s.execute("UPDATE decisions SET status=?,response=?,source=? WHERE id=? AND digest=? AND status=?",
                                  (status,choice,source_id,decision,expected_digest,row['status']))
            need(cursor.rowcount==1,'stale_decision','Decision changed while recording the answer')
            if status=='rejected':
                self._close_notices(row['project'],decision,actor.id,'human_rejected_decision',kinds=('product_decision','provisional_decision'))
                self.s.execute("DELETE FROM blocks WHERE kind='decision' AND ref=?",(decision,))
                # Rejection doesn't make old code current: tasks still need explicit reassessment.
            self.sec.event(row['project'],'human_response_observed',actor.id,{'decision':decision,'digest':expected_digest,
                'choice':choice,'selected_effect':effect,'response_observed_at':response_observed_at,**quote})
            expiry=self._expire_due_decision(row['project'],decision,actor=actor.id)
            if expiry and expiry.get('expired'):
                return expiry
        return {'id':decision,'status':status,'consistency_recheck_required':status=='decision_received'}

    def decision_review_material(self,decision,*,allow_stale_linked_change=False):
        """Canonical material shared by the consistency prompt and its binding."""
        from .review_dependencies import accepted_invariants
        row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
        self._require_timely_decision_answer(row)
        body=parse_json(row['body'])
        closure=self._supersession_closure(row['project'],body['supersedes']) if body.get('supersedes') else []
        if body.get('supersedes'):
            need(canonical(closure)==canonical(body.get('supersession_closure',[])),
                 'stale_decision','The proposal did not bind the current supersession chain; repropose it')
        change_material=None;full_change_material=None
        if body.get('change'):
            change_row,full_change_material=self.change_review_material(body['change'])
            current_change_binding=digest(full_change_material)
            need(change_row['project']==row['project'] and
                 (allow_stale_linked_change or body.get('change_binding')==current_change_binding),
                 'stale_decision','The linked change was revised after this decision was proposed')
            change_material={'id':body['change'],'revision':change_row['revision'],
                             'binding':current_change_binding,'read_operation':'change.read',
                             'read_digest':digest(canonical(full_change_material)),
                             'read_access_operation':'change.get'}
        conflict=None
        if body.get('conflict'):
            conflict_row=self.s.one('SELECT body,status FROM conflicts WHERE id=? AND project=?',(body['conflict'],row['project']),True)
            conflict={'id':body['conflict'],'status':conflict_row['status'],'body':parse_json(conflict_row['body'])}
        policy=self.g.policy(row['project'])
        response_evidence=self.response_evidence(decision)
        selected_effect=self._choice_effect(body,row['response']) if row['response'] is not None else None
        task_revalidations=(self._keep_existing_task_revalidations(row['project'],body['change'],decision)
            if body.get('change') and selected_effect=='keep_existing' else [])
        if task_revalidations and body.get('supersedes') and self._decision_effect_actions(body,selected_effect)['supersede']:
            roots=sorted({ref for ancestor in closure for ref in ancestor['body'].get('refs',[])})
            if roots:
                invalidated=set(self.k.impact(Actor('system','owner'),row['project'],roots)['tasks'])
                task_revalidations=[proof for proof in task_revalidations if proof['task'] not in invalidated]
        other_decisions=[]
        for other in self.s.all("SELECT id,body,digest,response,status FROM decisions WHERE project=? AND id!=? AND status IN ('applied','provisional','decision_received') ORDER BY id",(row['project'],decision)):
            other['body']=parse_json(other['body']);other_decisions.append(other)
        material={'format':'decision-review-material.v1','decision':decision,'digest':row['digest'],
                  'proposal':body,'status':row['status'],'response':row['response'],'source':row['source'],
                  'selected_effect':selected_effect,'response_evidence':response_evidence,'linked_change':change_material,
                  'conflict':conflict,'supersession_closure':closure,
                  'task_revalidations':task_revalidations,
                  'current_artifacts':self.s.all("SELECT id,revision,digest,status FROM artifacts WHERE project=? AND status='accepted' ORDER BY id",(row['project'],)),
                  'other_decisions':other_decisions,
                  'required_coverage':[],
                  'policy':policy,'invariants':accepted_invariants(self.s,row['project'])}
        # Keep nearby decisions fully visible by default. If their complete
        # bodies make the prompt large, retain an exact digest reference and
        # require the reviewer to read the immutable view to completion.
        while len(canonical(material))>700000:
            candidates=[(len(canonical(item)),index,item) for index,item in enumerate(material['other_decisions'])
                        if isinstance(item.get('body'),dict)]
            if not candidates:break
            _,index,item=max(candidates)
            external={'id':item['id'],'digest':item['digest'],'status':item['status'],
                      'response':item['response']}
            read_material={'id':item['id'],'digest':item['digest'],'status':item['status'],
                           'response':item['response'],'body':item['body']}
            external.update({'read_operation':'decision.read','read_digest':digest(read_material)})
            material['other_decisions'][index]=external
            material['required_coverage'].append('other-decision:'+item['id'])
        if full_change_material is not None:
            material['required_coverage'].extend(full_change_material.get('required_coverage',[]))
            # Keep small linked changes visible in the standard prompt. Large
            # ones remain exact, digest-bound packets read through change.read.
            if len(canonical(material))+len(canonical(full_change_material))<=700000:
                material['linked_change']['material']=full_change_material
            else:
                marker='linked-change-material:'+body['change']
                material['required_coverage'].append(marker)
        material['required_coverage']=sorted(set(material['required_coverage']))
        return row['project'],material

    def decision_binding(self,decision):
        _,material=self.decision_review_material(decision)
        return digest(material)

    def decision_review_subject(self,actor,decision,incremental_from=None):
        batch=self.s.one('SELECT * FROM decision_batches WHERE id=?',(decision,))
        if batch:
            self.k.project(actor,batch['project'])
            packet=parse_json(batch['body'])
            need(digest(packet)==batch['digest'],'integrity_error','Stored decision batch packet changed')
            current=self._decision_batch_material(actor,batch['project'],
                [member['decision'] for member in packet['members']],batch=decision)
            value={'decision_batch':decision,'project':batch['project'],'binding':digest(current),
                   'format':current['format'],'material':current}
            if incremental_from:
                try:
                    context=self.decision_incremental_context(actor,decision,incremental_from,current_material=current)
                    incremental={'available':True,'binding':digest(current),
                        'context_digest':digest(context),'required_coverage':context['required_coverage']}
                    if context['format']=='decision-apply-incremental-review.v1':
                        incremental['applied_decisions']=[item['decision'] for item in context['applied_effects']]
                    else:
                        incremental['added_artifact']=context['proof']['added_artifact']['id']
                    value['incremental_review']=incremental
                except Fault as exc:
                    if exc.code!='incremental_review_unavailable':raise
                    value['incremental_review']={'available':False,'reason':exc.message}
            return value
        identity=self.s.one('SELECT project FROM decisions WHERE id=?',(decision,),True)
        self.k.project(actor,identity['project'])
        project,material=self.decision_review_material(decision,
            allow_stale_linked_change=bool(incremental_from))
        value={'decision':decision,'project':project,'binding':digest(material),
                'format':material['format'],'material':material,
                'linked_change':material.get('linked_change')}
        if incremental_from:
            try:
                context=self.decision_incremental_context(actor,decision,incremental_from,current_material=material)
                incremental={'available':True,'binding':digest(material),
                    'context_digest':digest(context),'required_coverage':context['required_coverage']}
                if context['format']=='decision-apply-incremental-review.v1':
                    incremental['applied_decisions']=[item['decision'] for item in context['applied_effects']]
                else:
                    incremental['added_artifact']=context['proof']['added_artifact']['id']
                value['incremental_review']=incremental
            except Fault as exc:
                if exc.code!='incremental_review_unavailable':raise
                value['incremental_review']={'available':False,'reason':exc.message}
        return value

    def _receipt_prompt_context(self,receipt_id,subject,binding):
        receipt=self.g.receipt(receipt_id)
        need(receipt.get('subject')==subject and receipt.get('binding')==binding and
             receipt.get('role')=='consistency','stale_evidence',
             'Incremental review receipt does not match the exact consistency review family')
        run=self.s.one('SELECT * FROM runs WHERE id=?',(receipt['run'],),True)
        run_body=parse_json(run['body'])
        need(run['status']=='finished' and run['subject']==subject and run['role']=='consistency' and
             run['binding']==binding and run_body.get('input_digest')==receipt.get('input_digest') and
             run_body.get('input_blob')==receipt.get('input_blob'),
             'stale_evidence','Review run and receipt do not share the exact stored prompt')
        raw=self.s.blob_get(receipt['input_blob'])
        need(digest(raw)==receipt['input_digest'],'stale_evidence','Review prompt digest differs from its receipt')
        prompt=parse_json(raw)
        need(prompt.get('subject')==subject and prompt.get('binding')==binding and
             prompt.get('role')=='consistency' and isinstance(prompt.get('context'),dict),
             'stale_evidence','Review prompt identity differs from its receipt')
        context={key:value for key,value in prompt['context'].items()
                 if key not in {'managed_execution','read_access'}}
        return receipt,run,context

    def _base_incremental_review(self,actor,subject,base_receipt,is_batch):
        receipt=self.g.receipt(base_receipt)
        base_binding=receipt.get('binding')
        need(isinstance(base_binding,str) and base_binding,'incremental_review_unavailable',
             'Base review has no exact material binding')
        self.g.require_review(base_receipt,subject,base_binding,{'consistency'},latest=True)
        receipt,run,context=self._receipt_prompt_context(base_receipt,subject,base_binding)
        need(receipt['result'].get('verdict')=='pass','incremental_review_unavailable',
             'Only a current full consistency PASS can be carried into an incremental review')
        if is_batch:
            batch=self.s.one('SELECT * FROM decision_batches WHERE id=?',(subject,),True)
            packet=parse_json(batch['body'])
            need(batch['status']=='prepared' and digest(packet)==batch['digest']==base_binding,
                 'incremental_review_unavailable','Base batch review is not the current immutable prepared packet')
            required=packet['required_coverage'];external='decision-batch-material:'+subject
            if external in required:
                expected={'decision_batch_ref':{'id':subject,'digest':batch['digest'],
                         'read_operation':'decision.batch_read'},
                          'members':[{'decision':member['decision'],'coverage':member['coverage']}
                                     for member in packet['members']],
                          'required_coverage':required}
            else:
                expected={'decision_batch':packet,'required_coverage':required}
            need(canonical(context)==canonical(expected),'incremental_review_unavailable',
                 'Base receipt did not review the frozen full batch packet')
            base_material=packet
            old_required=required
        else:
            need(context.get('format')=='decision-review-material.v1' and
                 context.get('decision')==subject and digest(context)==base_binding,
                 'incremental_review_unavailable','Base receipt did not review a full decision material packet')
            base_material=context
            old_required=context.get('required_coverage',[])
        covered=set(receipt['result'].get('covered',[]))
        missing=set(old_required)-covered
        need(not missing,'incremental_review_unavailable',
             'Base PASS did not cover all of its frozen required material',{'missing':sorted(missing)})
        observed=self.s.one("SELECT seq FROM events WHERE project=? AND kind='run_observed' "
            "AND json_extract(body,'$.run')=? ORDER BY seq DESC LIMIT 1",(receipt['project'],receipt['run']))
        need(observed is not None,'incremental_review_unavailable',
             'Base review has no durable run-observed ordering proof')
        return receipt,run,context,base_material,old_required,observed['seq']

    def _incremental_added_requirement(self,project,base_material,current_material,is_batch,base_seq):
        if is_batch:
            old_artifacts=base_material.get('baseline',{}).get('artifacts',[])
            new_artifacts=current_material.get('baseline',{}).get('artifacts',[])
        else:
            old_artifacts=base_material.get('current_artifacts',[])
            new_artifacts=current_material.get('current_artifacts',[])
        old_by={item.get('id'):item for item in old_artifacts if isinstance(item,dict)}
        new_by={item.get('id'):item for item in new_artifacts if isinstance(item,dict)}
        added=set(new_by)-set(old_by)
        need(not(set(old_by)-set(new_by)) and len(added)==1 and len(new_by)==len(old_by)+1,
             'incremental_review_unavailable','Incremental review accepts exactly one append-only accepted artifact')
        added_id=next(iter(added))
        need(all(new_by[key]==old_by[key] for key in old_by),
             'incremental_review_unavailable','An existing accepted artifact changed since the base PASS')
        row=self.s.one('SELECT * FROM artifacts WHERE id=? AND project=?',(added_id,project),True)
        body=parse_json(row['body'])
        need(row['kind']=='requirement' and row['status']=='accepted' and
             isinstance(body.get('source_refs'),list) and bool(body['source_refs']) and
             set(body)<= {'title','statement','acceptance','source_refs','constraints','critical'} and
             not body.get('constraints') and not body.get('critical'),
             'incremental_review_unavailable',
             'Incremental review is limited to one source-backed unconstrained noncritical requirement')
        if is_batch:
            descriptor=new_by[added_id]
            need(descriptor.get('body')==body and descriptor.get('revision')==row['revision'] and
                 descriptor.get('digest')==row['digest'] and descriptor.get('status')=='accepted',
                 'incremental_review_unavailable','Added requirement differs from current batch projection')
            final_rows=current_material.get('final',{}).get('artifacts',[])
            final_by={item.get('id'):item for item in final_rows if isinstance(item,dict)}
            need(final_by.get(added_id)==descriptor,
                 'incremental_review_unavailable','Added requirement is not unchanged in the final batch projection')
        else:
            descriptor=new_by[added_id]
            need(descriptor=={'id':row['id'],'revision':row['revision'],'digest':row['digest'],
                              'status':'accepted'},
                 'incremental_review_unavailable','Added requirement differs from current decision material')
        accepted=self.s.one("SELECT seq FROM events WHERE project=? AND kind='artifact_accepted' "
            "AND json_extract(body,'$.id')=? AND json_extract(body,'$.revision')=? "
            "AND json_extract(body,'$.digest')=? ORDER BY seq DESC LIMIT 1",
            (project,added_id,row['revision'],row['digest']))
        need(accepted is not None and accepted['seq']>base_seq,
             'incremental_review_unavailable','Added requirement was not accepted after the base full PASS')
        link_events=self.s.one("SELECT seq FROM events WHERE project=? AND kind='link_recorded' AND seq>? LIMIT 1",
                               (project,base_seq))
        need(link_events is None,'incremental_review_unavailable',
             'Trace links changed after the base PASS; run a full consistency review')
        links=self.s.all('SELECT l.source,l.target,l.relation,l.confidence,l.basis FROM links l '
            'JOIN artifacts a ON a.id=l.source WHERE a.project=? ORDER BY l.source,l.target,l.relation',(project,))
        need(len(canonical(links))<=120000 and not any(link['source']==added_id or link['target']==added_id
             for link in links),'incremental_review_unavailable',
             'Added requirement has trace links or the current link snapshot exceeds its safe bound')
        sources=[];total_source_bytes=0
        for source_id in body['source_refs']:
            source=self.s.one('SELECT id,project,trust,blob,locator,characters FROM sources WHERE id=?',(source_id,))
            need(source is not None and source['project']==project and source['trust']=='human',
                 'incremental_review_unavailable','Added requirement does not retain exact trusted human sources')
            content=self.s.blob_get(source['blob']).decode('utf-8')
            total_source_bytes+=len(content.encode())
            sources.append({**source,'content':content})
        need(total_source_bytes<=200000 and len(canonical({'body':body,'sources':sources}))<=300000,
             'incremental_review_unavailable','Added requirement evidence exceeds the bounded delta review size')
        return {'id':added_id,'kind':row['kind'],'revision':row['revision'],'digest':row['digest'],
                'status':row['status'],'body':body},sources,links

    @staticmethod
    def _artifact_projection(artifact):
        return {key:artifact[key] for key in ('id','project','revision','digest','status','kind','body')}

    def _decision_apply_incremental_context(self,actor,subject,base_receipt,current_material,
                                            base_review_data=None):
        """Prove bounded prior single-decision applies are the only material drift."""
        project_row=self.s.one('SELECT project FROM decisions WHERE id=?',(subject,))
        if project_row is None:
            return None
        project=project_row['project']
        if base_review_data is None:
            base_review_data=self._base_incremental_review(actor,subject,base_receipt,False)
        base_review,base_run,_,base_material,old_required,base_seq=base_review_data
        timeline=self.s.all('SELECT id,seq,kind,body FROM events WHERE project=? AND seq>? ORDER BY seq,id LIMIT 1001',
                            (project,base_seq))
        applied=[row for row in timeline if row['kind']=='decision_applied']
        if not applied:
            return None
        need(len(applied)<=20,'incremental_review_unavailable',
             'Sequential apply proof is bounded to at most twenty earlier decisions')
        allowed={'run_registered','run_observed','decision_applied','change_applied','artifact_revised',
                 'decision_apply_incremental_review_applied','notification_closed','source_registered',
                 'adapter_registered','provider_configured','autonomy_configured','native_session_attached',
                 'native_decision_presented','native_user_input_recorded','inbox_displayed',
                 'inbox_acknowledged','notification_published','job_queued','job_attempt_finished',
                 'job_cancel_requested','lease_expired','timer_fired','index_completed',
                 'external_read_failed','run_incomplete','scheduler_fault','rpc_internal_error',
                 'artifact_save_replayed','change_delta_replayed'}
        need(len(timeline)<=1000 and all(row['kind'] in allowed for row in timeline),
             'incremental_review_unavailable',
             'The current material changed through an unsupported event; run a full consistency review')
        base_other={item['id']:item for item in base_material.get('other_decisions',[])
                    if isinstance(item,dict) and isinstance(item.get('id'),str)}
        current_other={item['id']:item for item in current_material.get('other_decisions',[])
                       if isinstance(item,dict) and isinstance(item.get('id'),str)}
        base_artifacts={item['id']:item for item in base_material.get('current_artifacts',[])
                        if isinstance(item,dict) and isinstance(item.get('id'),str)}
        current_artifacts={item['id']:item for item in current_material.get('current_artifacts',[])
                           if isinstance(item,dict) and isinstance(item.get('id'),str)}
        need(len(base_artifacts)==len(base_material.get('current_artifacts',[])) and
             len(current_artifacts)==len(current_material.get('current_artifacts',[])),
             'incremental_review_unavailable','Accepted artifact projection is malformed')
        current_decision=self.s.one('SELECT * FROM decisions WHERE id=? AND project=?',(subject,project),True)
        base_proposal=base_material.get('proposal')
        need(base_material.get('decision')==subject and isinstance(base_proposal,dict) and
             base_material.get('digest')==current_decision['digest'] and
             canonical(base_proposal)==canonical(parse_json(current_decision['body'])) and
             base_material.get('response')==current_decision['response'] and
             base_material.get('source')==current_decision['source'] and
             current_decision['status']=='decision_received',
             'incremental_review_unavailable',
             'The current proposal, human answer, or decision state changed since its full review')
        actions=[];changed_targets=set();processed_change_events=set();previous_decisions=set()
        for event_row in applied:
            event=parse_json(event_row['body']);prior_id=event.get('decision')
            need(isinstance(prior_id,str) and prior_id!=subject and not event.get('batch') and
                 prior_id in base_other,
                 'incremental_review_unavailable',
                 'Only individually reviewed prior decisions visible in the full packet can be carried')
            old_decision=base_other[prior_id]
            decision_row=self.s.one('SELECT * FROM decisions WHERE id=? AND project=?',(prior_id,project),True)
            prior_body=parse_json(decision_row['body'])
            need(old_decision.get('status') in {'decision_received','provisional'} and
                 decision_row['status']=='applied' and decision_row['digest']==old_decision.get('digest') and
                 canonical(prior_body)==canonical(old_decision.get('body')) and
                 decision_row['response']==old_decision.get('response') and
                 event.get('selected_effect') in {'accept','record_only'} and event.get('receipt'),
                 'incremental_review_unavailable',
                 'A prior decision changed beyond a recorded single apply')
            source=self.s.one('SELECT project,trust,blob FROM sources WHERE id=?',(decision_row['source'],))
            answer=self.response_evidence(prior_id)
            need(source is not None and source['project']==project and source['trust']=='human' and
                 answer is not None and answer['body'].get('digest')==decision_row['digest'] and
                 answer['body'].get('choice')==decision_row['response'] and
                 answer['body'].get('source')==decision_row['source'],
                 'incremental_review_unavailable','A prior apply no longer has its exact trusted human answer')
            effect=self._choice_effect(prior_body,decision_row['response'])
            prior_actions=self._decision_effect_actions(prior_body,effect)
            if effect=='record_only':
                need(not any(prior_actions.values()) and not self._decision_has_effects(prior_body),
                     'incremental_review_unavailable',
                     'A record-only decision cannot also carry a side effect')
                previous_decisions.add(prior_id)
                actions.append({'decision':prior_id,
                    'decision_event':{'id':event_row['id'],'seq':event_row['seq'],'body':event},
                    'change_event':None,'artifact_event':None,'change':None,'change_revision':None,
                    'change_body_digest':None,'target':None,'before':None,'after':None,
                    'selected_effect':'record_only','review_receipt':event['receipt']})
                continue
            need(effect=='accept' and prior_actions['apply_change'] and not any(prior_actions[key] for key in
                 ('decline_change','apply_policy','resolve_conflict','supersede')) and
                 not prior_body.get('conflict') and not prior_body.get('supersedes') and
                 prior_body.get('type')!='policy',
                 'incremental_review_unavailable',
                 'Only exact accepted single-change effects or no-effect record-only decisions can be carried')
            change=prior_body.get('change')
            change_row=self.s.one('SELECT * FROM changes WHERE id=? AND project=?',(change,project),True)
            change_body=parse_json(change_row['body'])
            change_events=[]
            for candidate in timeline:
                if candidate['kind']!='change_applied':continue
                details=parse_json(candidate['body'])
                if details.get('decision')==prior_id and details.get('change')==change and \
                        details.get('receipt')==event.get('receipt') and candidate['seq']<event_row['seq']:
                    change_events.append((candidate,details))
            need(len(change_events)==1 and change_row['stage']=='ready_for_reimplementation' and
                 change_row['revision']>1 and change_events[0][0]['id'] not in processed_change_events,
                 'incremental_review_unavailable','A prior change apply has no unique current event proof')
            change_event,change_event_body=change_events[0]
            processed_change_events.add(change_event['id'])
            deltas=change_body.get('deltas',[])
            delta_ids=[delta.get('artifact') for delta in deltas if isinstance(delta,dict)]
            need(len(deltas)==1 and len(delta_ids)==1 and isinstance(delta_ids[0],str) and
                 change_event_body.get('changed')==delta_ids and not deltas[0].get('withdraw'),
                 'incremental_review_unavailable',
                 'Sequential supplemental review currently accepts one non-withdrawn target per prior change')
            artifact_id=delta_ids[0]
            need(artifact_id not in changed_targets and artifact_id in base_artifacts and
                 artifact_id in current_artifacts,
                 'incremental_review_unavailable','Sequential changes overlap or leave the reviewed artifact set')
            delta=deltas[0];before_row=base_artifacts[artifact_id]
            before=self.k.artifact(Actor('system','owner'),artifact_id,delta['expected_revision'])
            after=self.k.artifact(Actor('system','owner'),artifact_id)
            need(before_row.get('status')=='accepted' and
                 before_row.get('revision')==delta.get('expected_revision') and
                 before_row.get('digest')==before.get('digest') and
                 after['status']=='accepted' and after['revision']==before['revision']+1 and
                 canonical(after['body'])==canonical(delta['body']) and
                 after['digest']==digest(delta['body']) and
                 current_artifacts[artifact_id]=={'id':artifact_id,'revision':after['revision'],
                      'digest':after['digest'],'status':'accepted'},
                 'incremental_review_unavailable',
                 'A prior artifact is not exactly the recorded one-revision decision result')
            artifact_events=[]
            for candidate in timeline:
                if candidate['kind']!='artifact_revised':continue
                details=parse_json(candidate['body'])
                if details.get('id')==artifact_id and details.get('revision')==after['revision'] and \
                        details.get('digest')==after['digest'] and \
                        candidate['seq']<change_event['seq'] and candidate['seq']<event_row['seq']:
                    artifact_events.append((candidate,details))
            need(len(artifact_events)==1,
                 'incremental_review_unavailable',
                 'A prior change target has no unique exact artifact-revision event')
            prior_change_targets={d['artifact'] for d in parse_json(self.s.one(
                'SELECT body FROM changes WHERE id=?',(parse_json(current_decision['body']).get('change'),),True)['body']).get('deltas',[])} \
                if parse_json(current_decision['body']).get('change') else set()
            need(artifact_id not in prior_change_targets,'incremental_review_unavailable',
                 'A prior decision modified this decision\'s target')
            historical=self.k.artifact(Actor('system','owner'),artifact_id,before['revision'])
            before_projection=self._artifact_projection(historical)
            # Acceptance may be a status transition without a new artifact
            # revision, so the historical revision row can say draft while the
            # immutable full-review packet correctly records it as accepted.
            before_projection['status']=before_row['status']
            after_projection=self._artifact_projection(after)
            changed_targets.add(artifact_id);previous_decisions.add(prior_id)
            actions.append({'decision':prior_id,
                'decision_event':{'id':event_row['id'],'seq':event_row['seq'],'body':event},
                'change_event':{'id':change_event['id'],'seq':change_event['seq'],'body':change_event_body},
                'artifact_event':{'id':artifact_events[0][0]['id'],'seq':artifact_events[0][0]['seq'],
                                  'body':artifact_events[0][1]},
                'change':change,'change_revision':change_row['revision'],
                'change_body_digest':digest(change_body),'target':artifact_id,
                'before':before_projection,'after':after_projection,
                'selected_effect':event['selected_effect'],'review_receipt':event['receipt']})
        change_effect_count=sum(item['change_event'] is not None for item in actions)
        target_effect_count=sum(item['target'] is not None for item in actions)
        need(len(processed_change_events)==change_effect_count and
             change_effect_count==len([row for row in timeline if row['kind']=='change_applied']),
             'incremental_review_unavailable','An unrelated change was applied after the base review')
        need(len([row for row in timeline if row['kind']=='artifact_revised'])==target_effect_count,
             'incremental_review_unavailable','An unrelated artifact revision occurred after the base review')
        supplemental_events=[parse_json(row['body']) for row in timeline
                             if row['kind']=='decision_apply_incremental_review_applied']
        need(all(event.get('decision') in previous_decisions and
                 isinstance(event.get('proof'),dict) and
                 event['proof'].get('format')=='decision-apply-incremental-proof.v1'
                 for event in supplemental_events),
             'incremental_review_unavailable','A prior supplemental apply event has no matching proof')
        known_refs=previous_decisions|{item['change'] for item in actions}
        for row in timeline:
            if row['kind']=='notification_closed':
                need(parse_json(row['body']).get('ref') in known_refs,
                     'incremental_review_unavailable','An unrelated user-facing notice changed after the base review')
        need(canonical(base_material.get('invariants',[]))==
             canonical(current_material.get('invariants',[])),
             'incremental_review_unavailable','Accepted invariants changed; run a full consistency review')
        normalized=copy.deepcopy(current_material)
        # Prior decision status is the only allowed change in the other-decision list.
        normalized_other={item['id']:item for item in normalized.get('other_decisions',[])
                          if isinstance(item,dict) and isinstance(item.get('id'),str)}
        for effect in actions:
            prior_id=effect['decision'];old=base_other[prior_id];now=normalized_other.get(prior_id)
            need(now is not None and now.get('status')=='applied' and
                 canonical({key:value for key,value in now.items() if key!='status'})==
                 canonical({key:value for key,value in old.items() if key!='status'}),
                 'incremental_review_unavailable','A prior decision changed beyond its applied status')
            normalized_other[prior_id]=old
        normalized['other_decisions']=sorted(normalized_other.values(),key=lambda item:item['id'])
        # Match only the exact old/new revision pair proven by each apply event.
        normalized_rows={item['id']:item for item in normalized.get('current_artifacts',[])}
        base_rows={item['id']:item for item in base_material.get('current_artifacts',[])}
        for effect in actions:
            ident=effect['target']
            if ident is None:
                continue
            old=base_rows[ident];now=normalized_rows.get(ident)
            need(now=={'id':ident,'revision':effect['after']['revision'],
                       'digest':effect['after']['digest'],'status':'accepted'} and
                 old=={'id':ident,'revision':effect['before']['revision'],
                       'digest':effect['before']['digest'],'status':'accepted'},
                 'incremental_review_unavailable','Current accepted artifact differs from an exact prior apply')
            normalized_rows[ident]=old
        normalized['current_artifacts']=sorted(normalized_rows.values(),key=lambda item:item['id'])
        prior_by_id={item['target']:item for item in actions if item['target'] is not None}
        def normalize_change_material(current_change,base_change):
            if not isinstance(current_change,dict) or not isinstance(base_change,dict):
                raise Fault('incremental_review_unavailable','Linked change material is unavailable for bounded comparison')
            need(current_change.get('format')==base_change.get('format')=='change-review-material.v2',
                 'incremental_review_unavailable','Linked change material format changed')
            value=copy.deepcopy(current_change)
            def normalize_refs(rows,old_rows):
                if not isinstance(rows,list) or not isinstance(old_rows,list): return
                old_by={item.get('id'):item for item in old_rows if isinstance(item,dict)}
                for index,item in enumerate(rows):
                    if not isinstance(item,dict) or item.get('id') not in prior_by_id:continue
                    effect=prior_by_id[item['id']];old=old_by.get(item['id'])
                    expected_new={'id':item['id'],'revision':effect['after']['revision'],
                                  'digest':effect['after']['digest']}
                    expected_old={'id':item['id'],'revision':effect['before']['revision'],
                                  'digest':effect['before']['digest']}
                    need(item==expected_new and old==expected_old,
                         'incremental_review_unavailable','Linked change artifact pins changed beyond a proven apply')
                    rows[index]=old
            normalize_refs(value.get('current'),base_change.get('current'))
            for key in ('upper_contracts',):
                current_contracts=value.get(key);base_contracts=base_change.get(key)
                if isinstance(current_contracts,dict) and isinstance(base_contracts,dict):
                    now_rows=current_contracts.get('artifacts',[]);old_rows=base_contracts.get('artifacts',[])
                    if isinstance(now_rows,list) and isinstance(old_rows,list):
                        old_by={item.get('id'):item for item in old_rows if isinstance(item,dict)}
                        for index,item in enumerate(now_rows):
                            if not isinstance(item,dict) or item.get('id') not in prior_by_id:continue
                            effect=prior_by_id[item['id']];old=old_by.get(item['id'])
                            need(item==effect['after'] and old==effect['before'],
                                 'incremental_review_unavailable',
                                 'Upper-contract artifact changed beyond a proven apply')
                            now_rows[index]=old
            current_scope=value.get('scope_review');base_scope=base_change.get('scope_review')
            if isinstance(current_scope,dict) and isinstance(base_scope,dict):
                current_contracts=current_scope.get('upper_contracts');base_contracts=base_scope.get('upper_contracts')
                if isinstance(current_contracts,dict) and isinstance(base_contracts,dict):
                    now_rows=current_contracts.get('artifacts',[]);old_rows=base_contracts.get('artifacts',[])
                    if isinstance(now_rows,list) and isinstance(old_rows,list):
                        old_by={item.get('id'):item for item in old_rows if isinstance(item,dict)}
                        for index,item in enumerate(now_rows):
                            if not isinstance(item,dict) or item.get('id') not in prior_by_id:continue
                            effect=prior_by_id[item['id']];old=old_by.get(item['id'])
                            need(item==effect['after'] and old==effect['before'],
                                 'incremental_review_unavailable',
                                 'Scope upper-contract artifact changed beyond a proven apply')
                            now_rows[index]=old
            # Nested material must otherwise be byte-for-byte the old linked-change view.
            return value
        base_link=base_material.get('linked_change');current_link=normalized.get('linked_change')
        if base_link or current_link:
            need(isinstance(base_link,dict) and isinstance(current_link,dict) and
                 base_link.get('id')==current_link.get('id') and
                 base_link.get('revision')==current_link.get('revision') and
                 isinstance(base_link.get('material'),dict) and isinstance(current_link.get('material'),dict) and
                 base_link.get('binding')==base_proposal.get('change_binding'),
                 'incremental_review_unavailable',
                 'An externalized or changed linked change requires a full consistency review')
            current_material_normalized=normalize_change_material(current_link['material'],base_link['material'])
            need(canonical(current_material_normalized)==canonical(base_link['material']),
                 'incremental_review_unavailable',
                 'Linked change changed beyond the exact prior artifact effects')
            normalized['linked_change']=copy.deepcopy(base_link)
        need(canonical(normalized)==canonical(base_material),
             'incremental_review_unavailable',
             'Decision material changed beyond bounded prior independent applies')
        base_markers=set(base_material.get('required_coverage',[]))
        current_markers=set(current_material.get('required_coverage',[]))
        required=sorted(current_markers|{
            'decision-apply-prior:'+item['decision'] for item in actions}|
            {'decision-apply-artifact:'+item['target'] for item in actions if item['target'] is not None})
        proof={'format':'decision-apply-incremental-proof.v1','subject':subject,
            'base_review_receipt':base_receipt,'base_binding':base_review['binding'],
            'base_run':base_review['run'],'base_run_observed_seq':base_seq,
            'base_input_digest':base_review['input_digest'],
            'base_material_digest':digest(base_material),'current_material_digest':digest(current_material),
            'base_material':base_material,'current_material':current_material,
            'applied_effects':actions,'base_required_coverage':old_required,
            'current_required_coverage':sorted(current_markers),
            'required_coverage':required}
        context={'format':'decision-apply-incremental-review.v1','subject':subject,
            'base_review':{'receipt':base_receipt,'binding':base_review['binding'],'run':base_review['run'],
                'input_digest':base_review['input_digest'],'verdict':base_review['result']['verdict'],
                'rationale':base_review['result'].get('rationale'),'covered':base_review['result'].get('covered',[]),
                'required_coverage':old_required},
            'base_material':base_material,'current_material':current_material,
            'applied_effects':actions,
            'review_instructions':'Review only the recorded effects of the earlier individually applied decisions on this unchanged proposal and human answer. Confirm the exact current linked change scope dispositions as shown. Do not treat a prior apply as changing this proposal, choice, source, or answer.',
            'required_coverage':required,'proof':proof}
        need(len(canonical(context))<=700000,'incremental_review_unavailable',
             'Sequential apply proof exceeds the bounded supplemental-review size')
        return context

    def decision_incremental_context(self,actor,subject,base_receipt,*,current_material=None):
        """Build a narrowly scoped delta review while retaining the full-material binding."""
        batch=self.s.one('SELECT * FROM decision_batches WHERE id=?',(subject,))
        is_batch=batch is not None
        if is_batch:
            actor.require('owner','agent',project=batch['project']);project=batch['project']
            frozen=parse_json(batch['body'])
            need(batch['status']=='prepared' and digest(frozen)==batch['digest'],
                 'incremental_review_unavailable','Only a current prepared immutable batch can use a delta review')
            if current_material is None:
                current_material=self._decision_batch_material(actor,project,
                    [member['decision'] for member in frozen['members']],batch=subject)
        else:
            row=self.s.one('SELECT * FROM decisions WHERE id=?',(subject,))
            need(row is not None,'not_found','Unknown decision review subject')
            actor.require('owner','agent',project=row['project']);project=row['project']
            if current_material is None:
                project,current_material=self.decision_review_material(subject)
        current_binding=digest(current_material)
        base_review,base_run,base_context,base_material,old_required,base_seq=\
            self._base_incremental_review(actor,subject,base_receipt,is_batch)
        if not is_batch:
            apply_context=self._decision_apply_incremental_context(actor,subject,base_receipt,
                current_material,(base_review,base_run,base_context,base_material,old_required,base_seq))
            if apply_context is not None:
                return apply_context
        addition,sources,links=self._incremental_added_requirement(project,base_material,
            current_material,is_batch,base_seq)
        need(canonical(base_material.get('invariants',[]))==canonical(current_material.get('invariants',[])),
             'incremental_review_unavailable','Accepted invariants changed; run a full consistency review')
        current_member_bindings={}
        if is_batch:
            old_members={item['decision']:item for item in base_material['members']}
            for member in current_material['members']:
                old=old_members.get(member['decision'])
                need(old is not None,'incremental_review_unavailable','Batch membership changed')
                single=self.decision_review_material(member['decision'])[1]
                normalized=copy.deepcopy(single)
                normalized['current_artifacts']=[item for item in normalized['current_artifacts']
                                                  if item['id']!=addition['id']]
                need(digest(normalized)==old.get('decision_binding'),
                     'incremental_review_unavailable',
                     'A batch member decision binding changed beyond the added requirement')
                current_member_bindings[member['decision']]=member['decision_binding']
            normalized_packet=copy.deepcopy(current_material)
            for field in ('baseline','final'):
                normalized_packet[field]['artifacts']=[item for item in normalized_packet[field]['artifacts']
                                                       if item['id']!=addition['id']]
            for member in normalized_packet['members']:
                member['decision_binding']=old_members[member['decision']]['decision_binding']
            need(canonical(normalized_packet)==canonical(base_material),
                 'incremental_review_unavailable',
                 'Batch material changed beyond one independent accepted requirement')
            need(current_material.get('required_coverage')==base_material.get('required_coverage') and
                 len(canonical(current_material))<=600000,
                 'incremental_review_unavailable',
                 'Batch coverage or packet-size thresholds changed; run a full consistency review')
        else:
            normalized=copy.deepcopy(current_material)
            normalized['current_artifacts']=[item for item in normalized['current_artifacts']
                                              if item['id']!=addition['id']]
            need(canonical(normalized)==canonical(base_material),
                 'incremental_review_unavailable',
                 'Decision material changed beyond one independent accepted requirement')
        source_markers=['source-content:'+source['id']+':'+source['blob'] for source in sources]
        required=sorted(['decision-incremental-requirement:'+addition['id'],*source_markers])
        proof={'format':'decision-incremental-proof.v1','subject':subject,
            'subject_kind':'decision_batch' if is_batch else 'decision',
            'base_review_receipt':base_receipt,'base_binding':base_review['binding'],
            'base_run':base_review['run'],'base_run_observed_seq':base_seq,
            'base_input_digest':base_review['input_digest'],
            'base_material_digest':digest(base_material),
            'base_packet_digest':base_material.get('id') and batch['digest'] if is_batch else None,
            'current_material_digest':current_binding,'added_artifact':addition,
            'sources':sources,'trace_links':links,'trace_links_digest':digest(links),
            'current_member_bindings':current_member_bindings}
        context={'format':'decision-incremental-review.v1','subject':subject,
            'subject_kind':proof['subject_kind'],'base_review':{
                'receipt':base_receipt,'binding':base_review['binding'],'run':base_review['run'],
                'input_digest':base_review['input_digest'],'verdict':base_review['result']['verdict'],
                'rationale':base_review['result'].get('rationale'),
                'covered':base_review['result'].get('covered',[]),
                'required_coverage':old_required},
            'base_material':base_material,'base_material_digest':proof['base_material_digest'],
            'current_material_digest':current_binding,'added_requirement':addition,
            'added_requirement_sources':sources,'current_trace_links':links,
            'current_trace_links_digest':proof['trace_links_digest'],
            'required_coverage':required,'proof':proof}
        need(len(canonical(context))<=700000,'incremental_review_unavailable',
             'The bounded delta review packet is too large; submit a full consistency review')
        return context

    def validate_decision_incremental_receipt(self,actor,subject,review_receipt,current_material):
        """Rebuild the delta context from current state before accepting its PASS."""
        binding=digest(current_material)
        receipt,run,context=self._receipt_prompt_context(review_receipt,subject,binding)
        if context.get('format')!='decision-incremental-review.v1':
            if context.get('format')!='decision-apply-incremental-review.v1':
                return None
            need(receipt.get('result',{}).get('verdict')=='pass',
                 'review_failed','Sequential apply supplemental review did not pass')
            proof=context.get('proof')
            need(isinstance(proof,dict) and isinstance(proof.get('base_review_receipt'),str),
                 'stale_evidence','Sequential apply review omits its carried full PASS')
            expected=self.decision_incremental_context(actor,subject,proof['base_review_receipt'],
                                                       current_material=current_material)
            need(expected.get('format')=='decision-apply-incremental-review.v1' and
                 canonical(context)==canonical(expected),'stale_evidence',
                 'Sequential apply review no longer matches its exact controller proof')
            missing=set(expected['required_coverage'])-set(receipt['result'].get('covered',[]))
            need(not missing,'incomplete_review_coverage',
                 'Sequential apply review omitted a required effect or current scope marker',
                 {'missing':sorted(missing)})
            return expected
        if context.get('format')!='decision-incremental-review.v1':
            return None
        need(receipt.get('result',{}).get('verdict')=='pass',
             'review_failed','Incremental consistency review did not pass')
        base_review=context.get('base_review')
        need(isinstance(base_review,dict) and isinstance(base_review.get('receipt'),str),
             'stale_evidence','Incremental review omits its carried full PASS')
        expected=self.decision_incremental_context(actor,subject,base_review['receipt'],
                                                   current_material=current_material)
        need(canonical(context)==canonical(expected),'stale_evidence',
             'Incremental review context no longer matches its exact controller proof')
        missing=set(expected['required_coverage'])-set(receipt['result'].get('covered',[]))
        need(not missing,'incomplete_review_coverage',
             'Incremental review did not cover every added requirement and source marker',
             {'missing':sorted(missing)})
        return expected

    def decision_read(self,actor,decision,expected_digest,offset=0,byte_budget=12000):
        row=self.s.one('SELECT * FROM decisions WHERE id=?',(decision,),True)
        self.k.project(actor,row['project'])
        view={'id':row['id'],'digest':row['digest'],'status':row['status'],
              'response':row['response'],'body':parse_json(row['body'])}
        raw=canonical(view).decode()
        from .navigation import Navigation
        return Navigation._fragment(raw,offset,byte_budget,expected_digest,
            {'decision':decision,'project':row['project'],'binding':digest(view)})

    def _decision_batch_material(self,actor,project,decisions,batch=None):
        actor.require('owner','agent',project=project)
        strings(decisions,'decision batch members',nonempty=True)
        need(2<=len(decisions)<=20 and len(set(decisions))==len(decisions),
             'invalid_batch','A decision batch needs 2..20 unique decisions')
        decision_rows={r['id']:r for r in self.s.all('SELECT * FROM decisions WHERE project=?',(project,))}
        members=[];changes={};deltas=[];policies=[];conflict_ids=set();closures={}
        for decision in decisions:
            row=decision_rows.get(decision)
            need(row is not None,'not_found','Decision belongs to another project or does not exist')
            need(row['status']=='decision_received' and row['source'],'human_approval_required',
                 'Every batch member must have a current human response')
            self._require_timely_decision_answer(row)
            source=self.s.one('SELECT project,trust,blob,locator,characters FROM sources WHERE id=?',(row['source'],),True)
            need(source['project']==project and source['trust']=='human','human_approval_required',
                 'Every batch answer must have a trusted human source')
            body=parse_json(row['body'])
            self._validate_response_current(actor,row,body)
            effect=self._choice_effect(body,row['response'])
            need(effect is not None,'choice_effect_required',
                 'This stored choice has no declared effect; create a new proposal with explicit choice_effects')
            actions=self._decision_effect_actions(body,effect)
            answer=self.response_evidence(decision)
            need(answer is not None,'answer_evidence_missing','Every batch member needs a retained exact answer quote')
            quoted=dict(answer['body'])
            need(quoted.get('digest')==row['digest'] and quoted.get('choice')==row['response'] and
                 quoted.get('source')==row['source'] and type(quoted.get('start')) is int and
                 type(quoted.get('end')) is int and isinstance(quoted.get('quote'),str),
                 'answer_evidence_invalid','A batch answer does not bind its exact proposal, source, choice and quote')
            need(quoted.get('selected_effect',effect)==effect,'answer_evidence_invalid',
                 'A batch answer does not bind the selected effect')
            if quoted.get('source_digest') is None:
                # Older response events did not persist this field. Reconstruct
                # it only from the immutable source-registration event that
                # anchored the retained source, without rewriting history.
                registered=self.s.one("SELECT seq FROM events WHERE project=? AND kind='source_registered' "
                    "AND json_extract(body,'$.source')=? AND json_extract(body,'$.digest')=? ORDER BY seq LIMIT 1",
                    (project,row['source'],source['blob']))
                need(registered is not None and registered['seq']<answer['seq'],
                     'answer_evidence_invalid','The retained answer has no exact source-registration proof')
                quoted['source_digest']=source['blob']
                answer={**answer,'body':quoted,'legacy_source_digest_derived':True}
            need(quoted.get('source_digest')==source['blob'],'answer_evidence_invalid',
                 'A batch answer source digest differs from its registered source')
            content=self.s.blob_get(source['blob']).decode('utf-8')
            need(0<=quoted['start']<quoted['end']<=len(content) and
                 content[quoted['start']:quoted['end']]==quoted['quote'],
                 'answer_evidence_invalid','A batch answer quote differs from its exact source range')
            review_project,review_material=self.decision_review_material(decision)
            need(review_project==project,'cross_project','Decision review material belongs elsewhere')
            member={'decision':decision,'digest':row['digest'],'status':row['status'],'proposal':body,
                    'response':row['response'],'selected_effect':effect,
                    'source':{'id':row['source'],'digest':source['blob'],
                    'trust':source['trust'],'locator':source['locator'],'characters':source['characters']},
                    'answer_evidence':answer,'decision_binding':digest(review_material),
                    'required_coverage':[marker for marker in review_material.get('required_coverage',[])
                                         if not marker.startswith(('other-decision:','linked-change-material:'))],
                    'task_revalidations':review_material.get('task_revalidations',[]),
                    'coverage':'decision-member:'+decision}
            if body.get('change'):
                change_row,change_material=self.change_review_material(body['change'])
                need(change_row['project']==project and change_row['stage']=='awaiting_product_decision',
                     'stale_decision','A batch change is no longer awaiting its exact product decision')
                need(body.get('change_binding')==digest(change_material),'stale_decision',
                     'A batch change differs from its approved exact material')
                need(body['change'] not in changes,'overlapping_batch_change',
                     'A batch cannot contain multiple decisions for one mutable change')
                changes[body['change']]={'revision':change_row['revision'],'stage':change_row['stage'],
                                         'binding':digest(change_material),'material':change_material,
                                         'decision':decision,
                                         'actions':actions,
                                         'answer_actor':answer['actor'],
                                         'body_digest':digest(parse_json(change_row['body']))}
                if actions['apply_change']:
                    for delta in parse_json(change_row['body']).get('deltas',[]):
                        deltas.append({'decision':decision,'change':body['change'],**delta})
                member['change_material']=change_material
            if body.get('conflict'):
                if actions['resolve_conflict']:
                    need(body['conflict'] not in conflict_ids,'overlapping_batch_conflict',
                         'A batch cannot resolve the same conflict more than once')
                    conflict_ids.add(body['conflict'])
            if body.get('type')=='execution_control_policy':
                raise Fault('wrong_route','Execution-control policy decisions need their dedicated review and apply route')
            if actions['apply_policy']:
                policies.append((decision,body))
            if actions['supersede']:
                for ancestor in self._supersession_closure(project,body['supersedes']):
                    need(ancestor['id'] not in closures,'overlapping_batch_supersession',
                         'Selected decisions have overlapping supersession closures')
                    closures[ancestor['id']]=ancestor
            members.append(member)
        member_ids=set(decisions)
        need(not(member_ids & closures.keys()),'overlapping_batch_supersession',
             'A batch member cannot also be an ancestor selected for this batch')

        current_policy=self.g.policy(project)
        need(len(policies)<=1,'overlapping_batch_policy','A batch may contain at most one policy update')
        final_policy={'revision':current_policy['revision'],'digest':current_policy['digest'],'body':current_policy['body']}
        if policies:
            _,policy_body=policies[0]
            execution_fields=('version','default_task_timeout_seconds','max_no_progress_attempts','max_run_seconds')
            need(all(policy_body['body'].get(field)==current_policy['body'].get(field) for field in execution_fields),
                 'wrong_route','Use execution_control.policy_apply for execution-control policy adoption')
            need(policy_body.get('old_digest')==current_policy['digest'],'stale_policy','Policy changed since proposal')
            final_policy={'revision':current_policy['revision']+1,'digest':digest(policy_body['body']),
                          'body':policy_body['body']}

        artifact_rows=self.s.all("SELECT id,revision,digest,status,body FROM artifacts WHERE project=? AND status='accepted' ORDER BY id",(project,))
        baseline_artifacts=[{**r,'body':parse_json(r['body'])} for r in artifact_rows]
        final_by_id={r['id']:dict(r) for r in baseline_artifacts}
        affected=set()
        for item in deltas:
            artifact=item['artifact'];need(artifact not in affected,'overlapping_batch_delta',
                'A batch cannot apply multiple deltas to the same artifact')
            affected.add(artifact)
            current=self.k.artifact(actor,artifact)
            need(current['status']=='accepted' and current['revision']==item['expected_revision'],
                 'stale_revision','A batch delta must target its exact current accepted artifact')
            self.k.validate_body(current['kind'],item['body'])
            final_by_id[artifact]={'id':artifact,'revision':current['revision']+1,
                'digest':digest(item['body']),'status':'withdrawn' if item.get('withdraw') else 'accepted',
                'body':item['body']}

        def constraint_conflicts(rows):
            constraints={}
            for artifact in rows:
                if artifact['status']!='accepted':continue
                for key,value in artifact['body'].get('constraints',{}).items():
                    constraints.setdefault(key,[]).append((artifact['id'],value))
            result=set()
            for key,values in constraints.items():
                for i,(left,left_value) in enumerate(values):
                    for right,right_value in values[i+1:]:
                        if left_value!=right_value:
                            result.add((key,*sorted((left,right))))
            return result
        before_conflicts=constraint_conflicts(baseline_artifacts)
        final_artifacts=[final_by_id[k] for k in sorted(final_by_id)]
        after_conflicts=constraint_conflicts(final_artifacts)
        introduced=after_conflicts-before_conflicts
        need(not introduced,'secondary_conflict','Combined final state creates contradictory accepted constraints',
             [list(x) for x in sorted(introduced)])
        remaining=[item for item in after_conflicts if item[1] in affected or item[2] in affected]
        need(not remaining,'secondary_conflict',
             'Combined final state leaves a contradiction involving an affected artifact',
             [list(x) for x in sorted(remaining)])

        active_statuses={'applied','provisional','decision_received'}
        relevant_decisions=set(decisions)|set(closures)
        relevant_decisions.update(r['id'] for r in decision_rows.values()
            if r['status'] in active_statuses or any(parse_json(r['body']).get('change')==change for change in changes))
        decision_state=[{'id':r['id'],'digest':r['digest'],'status':r['status'],'response':r['response'],
                         'source':r['source'],'body':parse_json(r['body'])}
                        for r in sorted(decision_rows.values(),key=lambda v:v['id'])
                        if r['id'] in relevant_decisions]
        final_decisions={r['id']:dict(r) for r in decision_state}
        for decision in decisions:
            final_decisions[decision]['status']='applied'
        for changed in changes:
            for decision_row in final_decisions.values():
                if decision_row['id'] not in member_ids and decision_row['body'].get('change')==changed and \
                        decision_row['status'] in {'pending','decision_received','deferred','provisional'}:
                    decision_row['status']='superseded'
        for ancestor in closures.values():
            if ancestor['id'] in final_decisions:
                final_decisions[ancestor['id']]['status']='superseded'
        conflict_state=[{'id':r['id'],'body':parse_json(r['body']),'status':r['status'],'decision':r['decision']}
                        for r in self.s.all('SELECT id,body,status,decision FROM conflicts WHERE project=? ORDER BY id',(project,))]
        conflict_decisions={member['proposal']['conflict']:member['decision'] for member in members
                            if member['proposal'].get('conflict') and
                            self._decision_effect_actions(member['proposal'],member['selected_effect'])['resolve_conflict']}
        final_conflicts=[{**r,'status':'resolved' if r['id'] in conflict_ids else r['status'],
                          'decision':conflict_decisions.get(r['id'],r['decision'])}
                         for r in conflict_state]
        final_changes={}
        for ident,item in changes.items():
            if item['actions']['apply_change']:
                stage='ready_for_reimplementation';body_digest=item['body_digest']
            else:
                stage='withdrawn'
                declined=self._declined_change_body(item['material']['body'],item['decision'],item['answer_actor'])
                body_digest=digest(declined)
            final_changes[ident]={'id':ident,'revision':item['revision']+1,'stage':stage,
                                  'body_digest':body_digest}
        cleared_block_refs={('decision',member['decision']) for member in members}
        closed_notice_refs={}
        for member in members:
            proposal=member['proposal'];decision=member['decision']
            closed_notice_refs[decision]={'kinds':['product_decision','provisional_decision'],
                                           'reason':'decision_batch_applied'}
            actions=self._decision_effect_actions(proposal,member['selected_effect'])
            if proposal.get('change'):
                change=proposal['change'];cleared_block_refs.add(('change',change))
                change_reason='change_applied' if actions['apply_change'] else 'change_declined'
                closed_notice_refs[change]={'kinds':None,'reason':change_reason}
                for decision_row in decision_state:
                    if decision_row['body'].get('change')==change:
                        sibling=decision_row['id'];cleared_block_refs.add(('decision',sibling))
                        if sibling!=decision:
                            closed_notice_refs[sibling]={'kinds':['product_decision','provisional_decision'],
                                                         'reason':change_reason}
            if proposal.get('conflict') and actions['resolve_conflict']:
                conflict=proposal['conflict'];cleared_block_refs.add(('conflict',conflict))
                closed_notice_refs[conflict]={'kinds':None,'reason':'decision_batch_resolved_conflict'}
            if actions['supersede']:
                for ancestor in self._supersession_closure(project,proposal['supersedes']):
                    cleared_block_refs.add(('decision',ancestor['id']))
                    closed_notice_refs[ancestor['id']]={'kinds':['product_decision','provisional_decision'],
                                                        'reason':'superseded_by:'+decision}
        baseline_blocks=[]
        for kind,ref in sorted(cleared_block_refs):
            baseline_blocks.extend(dict(r) for r in self.s.all(
                'SELECT b.task,b.kind,b.ref,b.reason FROM blocks b JOIN tasks t ON t.id=b.task '
                'WHERE t.project=? AND b.kind=? AND b.ref=? ORDER BY b.task,b.kind,b.ref',
                (project,kind,ref)))
        final_blocks=[r for r in baseline_blocks if (r['kind'],r['ref']) not in cleared_block_refs]
        block_effects=[r for r in baseline_blocks if (r['kind'],r['ref']) in cleared_block_refs]
        relevant_notice_refs=set(closed_notice_refs)
        baseline_notices=[]
        for notice in self.s.all('SELECT id,kind,ref,status,body FROM inbox WHERE project=? ORDER BY id',(project,)):
            if notice['ref'] in relevant_notice_refs:
                baseline_notices.append({k:notice[k] for k in ('id','kind','ref','status')} |
                                        {'body_digest':digest(notice['body'].encode())})
        def notice_closes(notice):
            rule=closed_notice_refs.get(notice['ref'])
            return bool(rule and notice['status']=='open' and
                        (rule['kinds'] is None or notice['kind'] in rule['kinds']))
        final_notices=[{**r,'status':'acknowledged' if notice_closes(r) else r['status']}
                       for r in baseline_notices]
        invalidated_roots=sorted({ref for member in members if
                                  self._decision_effect_actions(member['proposal'],member['selected_effect'])['supersede']
                                  for ancestor in self._supersession_closure(project,member['proposal']['supersedes'])
                                  for ref in ancestor['body'].get('refs',[])})
        # A per-member keep-existing proof may be safe in isolation but become
        # unsafe when another member changes one of its inputs, applies policy,
        # or invalidates one of its roots.  The final atomic projection records
        # only proofs that survive every projected member effect.
        unsafe_revalidation_tasks=set()
        if policies:
            unsafe_revalidation_tasks.update(
                proof['task'] for member in members
                for proof in member.get('task_revalidations',[]))
        if affected:
            affected_impact=self.k.impact(actor,project,sorted(affected))
            unsafe_revalidation_tasks.update(affected_impact['tasks'])
        if invalidated_roots:
            root_impact=self.k.impact(actor,project,invalidated_roots)
            unsafe_revalidation_tasks.update(root_impact['tasks'])
        for member in members:
            member['task_revalidations']=[proof for proof in member.get('task_revalidations',[])
                                          if proof['task'] not in unsafe_revalidation_tasks]
        coverage=set(member['coverage'] for member in members)
        for member in members:
            coverage.update(member.get('required_coverage',[]))
            change_id=member['proposal'].get('change')
            if change_id:
                coverage.update(changes[change_id]['material'].get('required_coverage',[]))
        coverage=sorted(coverage)
        material={'format':'decision-batch-review-material.v1','project':project,
                **({'id':batch} if batch else {}),
                'members':members,'supersession_closure':[closures[k] for k in sorted(closures)],
                'changes':{k:v for k,v in sorted(changes.items())},
                'baseline':{'artifacts':baseline_artifacts,'policy':current_policy,
                            'decisions':decision_state,'conflicts':conflict_state,
                            'changes':{k:{'id':k,'revision':v['revision'],'stage':v['stage'],
                                          'binding':v['binding'],'body_digest':v['body_digest']}
                                       for k,v in sorted(changes.items())},
                            'existing_blocks':baseline_blocks,'notices':baseline_notices},
                'final':{'artifacts':final_artifacts,'policy':final_policy,
                         'decisions':[final_decisions[k] for k in sorted(final_decisions)],
                         'conflicts':final_conflicts,'changes':final_changes,
                         'retained_existing_blocks':final_blocks,'notices':final_notices},
                'projection_scope':'Specifications, decisions, changes, and explicitly scoped lifecycle effects. '
                    'This packet does not model complete task readiness; invalidation and work reassessment remain mandatory.',
                'effects':{'deleted_scoped_blocks':block_effects,
                           'closed_notices':[{'ref':ref,**rule} for ref,rule in sorted(closed_notice_refs.items())],
                           'invalidated_roots':invalidated_roots,
                           'changed_artifacts':sorted(affected),
                           'task_revalidations':sorted(
                               [proof for member in members for proof in member.get('task_revalidations',[])],
                               key=lambda item:(item['task'],item['change'],item['decision'])),
                           'reassessment_required':bool(affected or invalidated_roots)},
                'required_coverage':coverage,
                'invariants':__import__('daikibo.review_dependencies',fromlist=['accepted_invariants']).accepted_invariants(self.s,project)}
        if batch and len(canonical(material))>700000:
            material['required_coverage']=sorted([*coverage,'decision-batch-material:'+batch])
        return material

    def decision_batch_prepare(self,actor,project,decisions):
        with self.s.transaction():
            actor.require('owner','agent',project=project)
            strings(decisions,'decision batch members',nonempty=True)
            need(2<=len(decisions)<=20 and len(set(decisions))==len(decisions),
                 'invalid_batch','A decision batch needs 2..20 unique decisions')
            expired=[]
            for decision in decisions:
                member=self.s.one('SELECT project FROM decisions WHERE id=?',(decision,))
                if member and member['project']==project:
                    outcome=self._expire_due_decision(project,decision,actor=actor.id)
                    if outcome and outcome.get('expired'):
                        expired.append(decision)
            if expired:
                return {'status':'expired','expired_decisions':expired,
                        'answered':False,'must_reconcile':True}
            ident=uid('DBATCH')
            body=self._decision_batch_material(actor,project,decisions,batch=ident)
            encoded=canonical(body)
            need(len(encoded)<=MAX_JSON_BYTES,'batch_too_large',
                 'The immutable decision batch exceeds the JSON read limit; split it into smaller disjoint batches',
                 {'bytes':len(encoded),'limit':MAX_JSON_BYTES})
            h=digest(body)
            self.s.execute('INSERT INTO decision_batches(id,project,body,digest,status,result,created,applied) VALUES(?,?,?,?,?,?,?,?)',
                (ident,project,encoded.decode(),h,'prepared',None,timestamp(),None))
            self.sec.event(project,'decision_batch_prepared',actor.id,{'id':ident,'digest':h,'members':decisions,
                'required_coverage':body['required_coverage']})
            return {'id':ident,'project':project,'digest':h,'status':'prepared',
                    'members':decisions,'required_coverage':body['required_coverage'],
                    'read_operation':'decision.batch_read'}

    def decision_batch_get(self,actor,batch,incremental_from=None):
        row=self.s.one('SELECT * FROM decision_batches WHERE id=?',(batch,),True)
        self.k.project(actor,row['project']);body=parse_json(row['body'])
        need(digest(body)==row['digest'],'integrity_error','Stored decision batch packet changed')
        result={'id':batch,'project':row['project'],'digest':row['digest'],'status':row['status'],
                'created':row['created'],'applied':row['applied'],'members':[m['decision'] for m in body['members']],
                'required_coverage':body['required_coverage'],'read_operation':'decision.batch_read',
                'read_digest':digest(canonical(body)),'result':parse_json(row['result']) if row['result'] else None}
        if incremental_from:
            try:
                current=self._decision_batch_material(actor,row['project'],
                    [member['decision'] for member in body['members']],batch=batch)
                context=self.decision_incremental_context(actor,batch,incremental_from,current_material=current)
                result['incremental_review']={'available':True,'binding':digest(current),
                    'context_digest':digest(context),'required_coverage':context['required_coverage'],
                    'added_artifact':context['proof']['added_artifact']['id']}
            except Fault as exc:
                if exc.code!='incremental_review_unavailable':raise
                result['incremental_review']={'available':False,'reason':exc.message}
        return result

    def decision_batch_read(self,actor,batch,expected_digest,offset=0,byte_budget=12000):
        row=self.s.one('SELECT * FROM decision_batches WHERE id=?',(batch,),True)
        self.k.project(actor,row['project']);body=parse_json(row['body'])
        need(digest(body)==row['digest'],'integrity_error','Stored decision batch packet changed')
        raw=canonical(body).decode()
        from .navigation import Navigation
        return Navigation._fragment(raw,offset,byte_budget,expected_digest,
            {'batch':batch,'project':row['project'],'binding':row['digest']})

    def decision_batch_apply(self,actor,batch,review_receipt):
        with self.s.transaction():
            row=self.s.one('SELECT * FROM decision_batches WHERE id=?',(batch,),True)
            actor.require('owner','agent',project=row['project'])
            if row['status']=='applied':
                result=parse_json(row['result'])
                need(result.get('review_receipt')==review_receipt,'stale_evidence',
                     'An applied batch can only be replayed with its recorded review')
                return {**result,'replayed':True}
            body=parse_json(row['body'])
            encoded=canonical(body)
            need(len(encoded)<=MAX_JSON_BYTES,'integrity_error','Stored decision batch exceeds the JSON read limit')
            need(digest(body)==row['digest'],'integrity_error','Stored decision batch packet changed')
            expired=[]
            for member in body['members']:
                outcome=self._expire_due_decision(row['project'],member['decision'],actor=actor.id)
                if outcome and outcome.get('expired'):
                    expired.append(member['decision'])
            if expired:
                return {'id':batch,'status':'prepared','applied':False,
                        'expired_decisions':expired,'must_reconcile':True}
            try:
                current=self._decision_batch_material(actor,row['project'],
                    [m['decision'] for m in body['members']],batch=batch)
            except Fault as exc:
                raise Fault('stale_decision_batch',
                    'A decision member or its evidence no longer matches the frozen batch',
                    {'cause':exc.code,'message':exc.message,'details':exc.details}) from exc
            current_binding=digest(current)
            self.g.require_review(review_receipt,batch,current_binding,{'consistency'},latest=True)
            incremental=self.validate_decision_incremental_receipt(actor,batch,review_receipt,current)
            if current_binding!=row['digest']:
                need(incremental is not None and
                     incremental['proof'].get('base_packet_digest')==row['digest'] and
                     incremental['base_material_digest']==row['digest'],
                     'stale_decision_batch',
                     'The current batch differs from its immutable packet without a validated append-only delta review')
            review=self.g.receipt(review_receipt)
            if incremental is None:
                missing=set(body['required_coverage'])-set(review['result'].get('covered',[]))
                need(not missing,'incomplete_review_coverage','Consistency review must cover every batch member',
                     {'missing':sorted(missing)})
            changed=[];roots=[];task_revalidations=[];keep_existing_members=[]
            for member in body['members']:
                decision=member['decision'];drow=self.s.one('SELECT * FROM decisions WHERE id=? AND project=?',(decision,row['project']),True)
                decision_body=parse_json(drow['body'])
                actions=self._decision_effect_actions(decision_body,member['selected_effect'])
                if actions['apply_policy']:
                    old=self.g.policy(row['project'])
                    policy_body=decision_body['body']
                    cursor=self.s.execute('UPDATE policies SET revision=revision+1,body=?,digest=? WHERE project=? AND revision=? AND digest=?',
                        (canonical(policy_body).decode(),digest(policy_body),row['project'],old['revision'],old['digest']))
                    need(cursor.rowcount==1,'stale_policy','Policy changed while applying the batch')
                if actions['apply_change']:
                    result=self._apply_change(actor,decision_body['change'],review_receipt,decision,skip_conflict_check=True)
                    changed.extend([d['artifact'] for d in parse_json(self.s.one('SELECT body FROM changes WHERE id=?',(decision_body['change'],),True)['body']).get('deltas',[])])
                elif actions['decline_change']:
                    self._decline_change(actor,row['project'],decision_body['change'],decision)
                if actions['resolve_conflict']:
                    conflict=decision_body['conflict']
                    self.s.execute("UPDATE conflicts SET status='resolved',decision=? WHERE id=? AND project=? AND status='awaiting_user'",
                                   (decision,conflict,row['project']))
                    need(self.s.conn.execute('SELECT changes()').fetchone()[0]==1,'stale_conflict','Conflict changed while applying the batch')
                    self.s.execute('DELETE FROM blocks WHERE kind=\'conflict\' AND ref=?',(conflict,))
                    self._close_notices(row['project'],conflict,actor.id,'decision_batch_resolved_conflict')
                if actions['supersede']:
                    for ancestor in self._decision_lineage(row['project'],decision):
                        self._terminalize_decision(row['project'],ancestor['id'],actor.id,'superseded_by:'+decision,allow_applied=True)
                        roots.extend(ancestor['body'].get('refs',[]))
                cursor=self.s.execute("UPDATE decisions SET status='applied',consistency_receipt=? WHERE id=? AND digest=? AND status='decision_received' AND source=?",
                    (review_receipt,decision,drow['digest'],drow['source']))
                need(cursor.rowcount==1,'stale_decision','Decision changed while applying batch member')
                self.s.execute("DELETE FROM blocks WHERE kind='decision' AND ref=?",(decision,))
                self._close_notices(row['project'],decision,actor.id,'decision_batch_applied',kinds=('product_decision','provisional_decision'))
                if actions['decline_change']:
                    expected_tasks=member.get('task_revalidations',[])
                    keep_existing_members.append((decision_body['change'],decision,expected_tasks))
                self.sec.event(row['project'],'decision_applied',actor.id,{'decision':decision,'receipt':review_receipt,
                    'batch':batch,'batch_digest':row['digest'],'selected_effect':member['selected_effect']})
            if roots:self.k.invalidate(row['project'],sorted(set(roots)),'Human corrected previous decisions in an atomic batch')
            # Restore only after every member's effects and root invalidations
            # have landed.  The frozen projection already omits tasks affected
            # by those other effects, so any proof which no longer holds is a
            # stale packet rather than a partial restoration.
            for change,decision,expected_tasks in keep_existing_members:
                actual_tasks=self._restore_keep_existing_task_fences(actor,row['project'],
                    change,decision,expected_tasks)
                need(canonical(actual_tasks)==canonical(expected_tasks),'stale_decision_batch',
                     'Task revalidation proof changed while applying the batch member')
                task_revalidations.extend(actual_tasks)
            expected_task_revalidations=body.get('effects',{}).get('task_revalidations',[])
            task_revalidations=sorted(task_revalidations,key=lambda item:(item['task'],item['change'],item['decision']))
            need(canonical(task_revalidations)==canonical(expected_task_revalidations),'stale_decision_batch',
                 'Task revalidation projection differs from the atomic batch result')
            result={'id':batch,'status':'applied','members':[m['decision'] for m in body['members']],
                    'changed_artifacts':sorted(set(changed)),'review_receipt':review_receipt,
                    'batch_digest':row['digest'],'atomic':True,
                    'task_revalidations':task_revalidations}
            if incremental is not None:
                result.update({'review_mode':'incremental',
                    'base_review_receipt':incremental['proof']['base_review_receipt'],
                    'supplemental_review_receipt':review_receipt,
                    'current_material_digest':incremental['current_material_digest'],
                    'incremental_proof':incremental['proof']})
            self.s.execute("UPDATE decision_batches SET status='applied',result=?,applied=? WHERE id=? AND status='prepared'",
                           (canonical(result).decode(),timestamp(),batch))
            need(self.s.conn.execute('SELECT changes()').fetchone()[0]==1,'stale_decision_batch','Batch state changed concurrently')
            self.sec.event(row['project'],'decision_batch_applied',actor.id,{'id':batch,'digest':row['digest'],
                'members':result['members'],'changed_artifacts':result['changed_artifacts'],'review':review_receipt})
            return result

    def response_evidence(self,decision):
        event=self.s.one("SELECT id,seq,actor,body,created FROM events WHERE kind='human_response_observed' AND json_extract(body,'$.decision')=? ORDER BY seq DESC LIMIT 1",(decision,))
        if event and 'quote' in parse_json(event['body']):
            return {**event,'body':parse_json(event['body'])}
        return None

    def _close_notices(self,project,ref,actor,reason,kinds=None):
        params=[project,ref]
        sql="SELECT id,kind FROM inbox WHERE project=? AND ref=? AND status='open'"
        if kinds:
            sql += " AND kind IN ("+','.join('?' for _ in kinds)+")"
            params.extend(kinds)
        for notice in self.s.all(sql,params):
            self.s.execute("UPDATE inbox SET status='acknowledged' WHERE id=? AND status='open'",(notice['id'],))
            self.sec.event(project,'notification_closed',actor,{'item':notice['id'],'kind':notice['kind'],
                'ref':ref,'closed_reason':reason,'user_acknowledgement':False})

    def _terminalize_decision(self,project,decision,actor,reason,*,status='superseded',allow_applied=False):
        row=self.s.one('SELECT status FROM decisions WHERE id=? AND project=?',(decision,project))
        if not row:
            return False
        changed=False
        eligible={'pending','decision_received','deferred','provisional'} | ({'applied'} if allow_applied else set())
        if status=='expired':
            eligible.add('rejected')
        if row['status'] in eligible and row['status']!=status:
            cursor=self.s.execute('UPDATE decisions SET status=? WHERE id=? AND project=? AND status=?',
                                  (status,decision,project,row['status']))
            changed=cursor.rowcount==1
        open_notices=self.s.one("SELECT count(*) AS n FROM inbox WHERE project=? AND ref=? AND status='open' AND kind IN ('product_decision','provisional_decision')",(project,decision))['n']
        blocks=self.s.one("SELECT count(*) AS n FROM blocks WHERE kind='decision' AND ref=?",(decision,))['n']
        self._close_notices(project,decision,actor,reason,kinds=('product_decision','provisional_decision'))
        # Only this decision's blocker is cleared. Other input/conflict gates
        # on the same task remain visible and actionable.
        self.s.execute("DELETE FROM blocks WHERE kind='decision' AND ref=?",(decision,))
        changed=changed or bool(open_notices or blocks)
        if changed:
            self.sec.event(project,'decision_terminalized',actor,{'decision':decision,'from_status':row['status'],
                'status':status if row['status'] in eligible else row['status'],
                'closed_reason':reason})
        return changed

    @staticmethod
    def _declined_change_body(body,decision,answer_actor):
        return {**body,'withdrawal':{'reason':'Linked decision selected keep_existing',
            'compensation':None,'by':answer_actor,'decision':decision}}

    def _decline_change(self,actor,project,change,decision):
        row=self.s.one('SELECT revision,stage,body FROM changes WHERE id=? AND project=?',(change,project),True)
        need(row['stage']=='awaiting_product_decision','stale_decision',
             'The linked change is no longer awaiting its exact product decision')
        evidence=self.response_evidence(decision)
        need(evidence is not None,'answer_evidence_missing','Declining a linked change needs retained human response evidence')
        body=self._declined_change_body(parse_json(row['body']),decision,evidence['actor'])
        cursor=self.s.execute("UPDATE changes SET stage='withdrawn',body=?,revision=revision+1 "
            "WHERE id=? AND project=? AND stage='awaiting_product_decision' AND revision=?",
            (canonical(body).decode(),change,project,row['revision']))
        need(cursor.rowcount==1,'stale_decision','The linked change changed while retaining the existing specification')
        self.s.execute("DELETE FROM blocks WHERE kind='change' AND ref=?",(change,))
        self._close_notices(project,change,actor.id,'change_declined')
        self._terminalize_change_decisions(project,change,actor.id,'change_declined',exclude=(decision,))
        decision_row=self.s.one('SELECT source FROM decisions WHERE id=? AND project=?',(decision,project),True)
        self.sec.event(project,'change_declined',actor.id,{'change':change,'decision':decision,
            'response_source':decision_row['source'],'selected_effect':'keep_existing'})
        return {'id':change,'stage':'withdrawn','specifications_retained':True}

    def _terminalize_change_decisions(self,project,change,actor,reason,exclude=()):
        rows=self.s.all("SELECT id FROM decisions WHERE project=? AND json_extract(body,'$.change')=? ORDER BY created,id",(project,change))
        for row in rows:
            if row['id'] not in exclude:
                self._terminalize_decision(project,row['id'],actor,reason)

    def _refresh_change_notice(self,project,change,body,stage,actor):
        self._close_notices(project,change,actor,'change_material_revised',kinds=('product_decision','search_incomplete'))
        if stage=='awaiting_product_decision':
            self._publish_change_notice(project,change,body)

    def _publish_change_notice(self,project,change,body,attempt=None,kind='product_decision'):
        row=self.s.one('SELECT revision FROM changes WHERE id=? AND project=?',(change,project),True)
        binding=self.change_binding(change)
        published={'change':change,'revision':row['revision'],'binding':binding,'body':body}
        if attempt is not None:
            published['latest_attempt']=attempt
        self.g.inbox(project,kind,change,published,'warning')

    def _decision_lineage(self,project,decision):
        """Return supersedes ancestors nearest-first, failing closed on corrupt chains."""
        chain=[];seen=set();current=decision
        while current:
            need(current not in seen,'decision_lineage_invalid','Decision supersedes chain contains a cycle')
            seen.add(current)
            row=self.s.one('SELECT id,digest,status,body FROM decisions WHERE id=? AND project=?',(current,project))
            need(row is not None,'decision_lineage_invalid','Decision supersedes chain leaves its project')
            body=parse_json(row['body'])
            if current!=decision:
                # The outer closure enumerates every ancestor. Each ancestor
                # body still stores its full immutable digest, but embedding
                # its own closure recursively makes a linear A→B→C history
                # grow exponentially when copied into the next proposal.
                body={k:v for k,v in body.items() if k!='supersession_closure'}
                row['body']=body;chain.append(row)
            current=body.get('supersedes')
            need(len(seen)<=100,'decision_lineage_invalid','Decision supersedes chain exceeds its safety bound')
        return chain

    def _supersession_closure(self,project,first):
        """Return a direct supersedes target and every ancestor, nearest first."""
        chain=[];seen=set();current=first
        while current:
            need(current not in seen,'decision_lineage_invalid','Decision supersedes chain contains a cycle')
            seen.add(current)
            row=self.s.one('SELECT id,digest,status,body FROM decisions WHERE id=? AND project=?',(current,project))
            need(row is not None,'decision_lineage_invalid','Decision supersedes chain leaves its project')
            body=parse_json(row['body'])
            # Preserve the immutable full-row digest and all original proposal
            # fields; the generated nested closure is represented once by this
            # outer flat closure and is not repeated recursively.
            body={k:v for k,v in body.items() if k!='supersession_closure'}
            chain.append({'id':row['id'],'digest':row['digest'],'status':row['status'],'body':body})
            current=body.get('supersedes')
            need(len(seen)<=100,'decision_lineage_invalid','Decision supersedes chain exceeds its safety bound')
        return chain

    def _validate_response_current(self,actor,row,body,*,allow_reviewed_apply_drift=False):
        for ref in body.get('bindings',[]):
            current=self.k.artifact(actor,ref['id'])
            need(current['revision']==ref['revision'] and current['digest']==ref['digest'],
                 'stale_decision','A bound requirement changed')
        if body.get('change') and not allow_reviewed_apply_drift:
            need(body['change_binding']==self.change_binding(body['change']),
                 'stale_decision','Change proposal changed')
        if body.get('conflict'):
            conflict=self.s.one('SELECT status FROM conflicts WHERE id=? AND project=?',(body['conflict'],row['project']),True)
            need(conflict['status']=='awaiting_user','stale_decision','Conflict has already been resolved')
        if body.get('supersedes'):
            need('supersession_closure' in body,'supersession_review_required',
                 'This legacy proposal did not bind its full supersession chain; repropose it for a fresh review')
            closure=self._supersession_closure(row['project'],body['supersedes'])
            need(canonical(closure)==canonical(body.get('supersession_closure',[])),
                 'stale_decision','A decision in the reviewed supersession chain changed')
        if row['status'] in {'decision_received','deferred'}:
            event=self.response_evidence(row['id'])
            need(event is not None,'answer_evidence_missing',
                 'The latest decision response lacks retained exact human answer evidence')
            answer=event['body'];effect=self._choice_effect(body,row['response'])
            need(effect is not None,'choice_effect_required',
                 'This stored choice has no declared effect; create a new proposal with explicit choice_effects')
            expected_status='deferred' if row['response']=='defer' else 'decision_received'
            need(answer.get('decision')==row['id'] and answer.get('digest')==row['digest'] and
                 answer.get('choice')==row['response'] and answer.get('source')==row['source'] and
                 row['status']==expected_status and
                 answer.get('selected_effect',effect)==effect,
                 'answer_evidence_invalid','The retained human response does not bind the current choice and effect')
            source=self.s.one('SELECT project,trust,blob FROM sources WHERE id=?',(row['source'],),True)
            need(source['project']==row['project'] and source['trust']=='human' and
                 answer.get('source_digest',source['blob'])==source['blob'],
                 'answer_evidence_invalid','The retained response source is no longer the exact trusted source')
            registered=self.s.one("SELECT seq FROM events WHERE project=? AND kind='source_registered' "
                "AND json_extract(body,'$.source')=? AND json_extract(body,'$.digest')=? ORDER BY seq DESC LIMIT 1",
                (row['project'],row['source'],source['blob']))
            proposed=self.s.one("SELECT seq FROM events WHERE project=? AND kind='decision_proposed' "
                "AND json_extract(body,'$.id')=? AND json_extract(body,'$.digest')=? ORDER BY seq DESC LIMIT 1",
                (row['project'],row['id'],row['digest']))
            need(registered is not None and registered['seq']<event['seq'] and
                 (proposed is None or registered['seq']>proposed['seq']),
                 'answer_evidence_invalid','The retained response source is not causally ordered after its proposal')
            previous=self.s.one("SELECT seq,body FROM events WHERE project=? AND kind='human_response_observed' "
                "AND json_extract(body,'$.decision')=? AND seq<? ORDER BY seq DESC LIMIT 1",
                (row['project'],row['id'],event['seq']))
            if previous:
                prior_answer=parse_json(previous['body'])
                if prior_answer.get('source')==row['source']:
                    need(prior_answer.get('choice')==row['response'],'answer_evidence_invalid',
                         'A changed choice reused the previous human source')
                else:
                    need(registered['seq']>previous['seq'],'answer_evidence_invalid',
                         'A changed answer source was registered before the prior response')
            content=self.s.blob_get(source['blob']).decode('utf-8')
            start,end,quoted=answer.get('start'),answer.get('end'),answer.get('quote')
            need(type(start) is int and type(end) is int and isinstance(quoted,str) and
                 0<=start<end<=len(content) and content[start:end]==quoted,
                 'answer_evidence_invalid','The retained human quotation differs from its source range')

    def _decision_apply_event_references(self,project,decision,receipt,body):
        decision_event=self.s.one("SELECT id,seq,body FROM events WHERE project=? AND kind='decision_applied' "
            "AND json_extract(body,'$.decision')=? AND json_extract(body,'$.receipt')=? ORDER BY seq DESC LIMIT 1",
            (project,decision,receipt),True)
        decision_details=parse_json(decision_event['body'])
        need(not decision_details.get('batch'),'stale_evidence',
             'Sequential individual apply cannot reference a batch event')
        result={'decision_event':{'id':decision_event['id'],'seq':decision_event['seq'],
                                  'body':decision_details},'change_event':None,'artifact_events':[]}
        if not body.get('change'):
            return result
        change_events=self.s.all("SELECT id,seq,body FROM events WHERE project=? AND kind='change_applied' "
            "AND json_extract(body,'$.decision')=? AND json_extract(body,'$.change')=? "
            "AND json_extract(body,'$.receipt')=? AND seq<? ORDER BY seq DESC LIMIT 2",
            (project,decision,body['change'],receipt,decision_event['seq']))
        need(len(change_events)==1,'stale_evidence',
             'Applied linked change has no unique journal reference')
        change_event=change_events[0];change_details=parse_json(change_event['body'])
        change_row=self.s.one('SELECT body FROM changes WHERE id=? AND project=?',
                              (body['change'],project),True)
        target_ids={delta['artifact'] for delta in parse_json(change_row['body']).get('deltas',[])}
        need(set(change_details.get('changed',[]))==target_ids,
             'stale_evidence','Applied change targets differ from the linked proposal')
        artifact_events=[]
        for artifact in sorted(target_ids):
            current=self.k.artifact(Actor('system','owner'),artifact)
            rows=self.s.all("SELECT id,seq,body FROM events WHERE project=? AND kind='artifact_revised' "
                "AND json_extract(body,'$.id')=? AND json_extract(body,'$.revision')=? "
                "AND json_extract(body,'$.digest')=? AND seq<? ORDER BY seq DESC LIMIT 2",
                (project,artifact,current['revision'],current['digest'],change_event['seq']))
            need(len(rows)==1,'stale_evidence','Applied target has no unique exact revision event',artifact)
            artifact_events.append({'id':rows[0]['id'],'seq':rows[0]['seq'],
                                    'body':parse_json(rows[0]['body'])})
        result.update({'change_event':{'id':change_event['id'],'seq':change_event['seq'],
                                       'body':change_details},
                       'artifact_events':artifact_events})
        return result

    def _human_quote_after_proposal(self,actor,row,decision,source,utterance):
        proposed=self.s.one("SELECT seq FROM events WHERE project=? AND kind='decision_proposed' "
                            "AND json_extract(body,'$.id')=? AND json_extract(body,'$.digest')=? ORDER BY seq DESC LIMIT 1",
                            (row['project'],decision,row['digest']))
        if not proposed:
            proposal_body=digest(row['body'].encode())
            proposed=self.s.one("SELECT seq FROM events WHERE project=? AND kind='notification_published' "
                "AND json_extract(body,'$.ref')=? AND json_extract(body,'$.kind') IN ('product_decision','provisional_decision') "
                "AND json_extract(body,'$.body_digest')=? ORDER BY seq LIMIT 1",
                (row['project'],decision,proposal_body))
        registered=self.s.one("SELECT seq FROM events WHERE project=? AND kind='source_registered' "
                              "AND json_extract(body,'$.source')=? ORDER BY seq DESC LIMIT 1",
                              (row['project'],source))
        if proposed and registered:
            need(registered['seq']>proposed['seq'],'stale_user_input',
                 'The answer source predates this decision proposal')
            source_row=self.s.one('SELECT created FROM sources WHERE id=? AND project=?',(source,row['project']),True)
            # The source-registration event is the causal freshness proof.
            # Use an inclusive source-local timestamp only to preserve the
            # human_quote API's trust/project/exact-text validation; comparing
            # it to the decision timestamp would reintroduce clock rollback.
            return self.k.human_quote(actor,row['project'],source,utterance,after=source_row['created'])
        # Existing stores without both causal events retain the prior wall-clock guard.
        return self.k.human_quote(actor,row['project'],source,utterance,after=row['created'])

    def apply_decision(self,actor,decision,review_receipt):
        with self.s.transaction():
            row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
            actor.require('owner','agent',project=row['project'])
            expiry=self._expire_due_decision(row['project'],decision,actor=actor.id)
            if expiry and expiry.get('expired'):
                return expiry
            row=self.s.one("SELECT * FROM decisions WHERE id=?",(decision,),True)
            self._require_timely_decision_answer(row)
            if parse_json(row['body']).get('type') == 'execution_control_policy':
                raise Fault('wrong_route','Use execution_control.policy_apply for source-backed policy adoption')
            need(row['status']=='decision_received' and row['source'],'human_approval_required','A displayed proposal or agent approval is insufficient')
            src=self.s.one("SELECT trust FROM sources WHERE id=?",(row['source'],),True)
            need(src['trust']=='human','human_approval_required','No authenticated human response')
            _,review_material=self.decision_review_material(decision,allow_stale_linked_change=True)
            current_binding=digest(review_material)
            self.g.require_review(review_receipt,decision,current_binding,{'consistency'},latest=True)
            incremental=self.validate_decision_incremental_receipt(actor,decision,review_receipt,review_material)
            required=set(review_material.get('required_coverage',[]))
            if required and incremental is None:
                receipt=self.g.receipt(review_receipt)
                need(required<=set(receipt['result'].get('covered',[])),'incomplete_review_coverage',
                     'Consistency review did not cover every externally read decision material packet',
                     {'missing':sorted(required-set(receipt['result'].get('covered',[])))})
            body=parse_json(row['body'])
            allow_apply_drift=bool(incremental and
                incremental.get('format')=='decision-apply-incremental-review.v1')
            current_link=(review_material.get('linked_change') or {}).get('binding')
            if body.get('change') and body.get('change_binding')!=current_link:
                need(allow_apply_drift,'stale_decision',
                     'The linked change changed after approval without a validated sequential-apply review')
            self._validate_response_current(actor,row,body,
                allow_reviewed_apply_drift=allow_apply_drift)
            effect=self._choice_effect(body,row['response'])
            actions=self._decision_effect_actions(body,effect)
            if actions['apply_policy']:
                old=self.g.policy(row['project'])
                execution_fields=('version','default_task_timeout_seconds','max_no_progress_attempts','max_run_seconds')
                need(all(body['body'].get(field)==old['body'].get(field) for field in execution_fields),
                     'wrong_route','Use execution_control.policy_apply for execution-control policy adoption')
                need(old['digest']==body['old_digest'],'stale_policy','Policy changed since proposal')
                self.s.execute("UPDATE policies SET revision=revision+1,body=?,digest=? WHERE project=?",(canonical(body['body']).decode(),digest(body['body']),row['project']))
            if actions['apply_change']:
                need(allow_apply_drift or body['change_binding']==self.change_binding(body['change']),
                     'stale_decision','Change was revised after approval')
                self._apply_change(actor,body['change'],review_receipt,decision)
            elif actions['decline_change']:
                self._decline_change(actor,row['project'],body['change'],decision)
            if actions['resolve_conflict']:
                self.s.execute("UPDATE conflicts SET status='resolved',decision=? WHERE id=?",(decision,body['conflict']))
                self.s.execute("DELETE FROM blocks WHERE kind='conflict' AND ref=?",(body['conflict'],))
                self.s.execute("UPDATE inbox SET status='acknowledged' WHERE ref=?",(body['conflict'],))
            if actions['supersede']:
                lineage=self._decision_lineage(row['project'],decision)
                roots=[]
                for old in lineage:
                    self._terminalize_decision(row['project'],old['id'],actor.id,'superseded_by:'+decision,allow_applied=True)
                    roots.extend(old['body'].get('refs',[]))
                if roots:self.k.invalidate(row['project'],sorted(set(roots)),'Human corrected a previous decision')
            cursor=self.s.execute("UPDATE decisions SET status='applied',consistency_receipt=? WHERE id=? AND digest=? AND status='decision_received' AND source=?",
                                  (review_receipt,decision,row['digest'],row['source']))
            need(cursor.rowcount==1,'stale_decision','Decision changed while applying')
            self.s.execute("DELETE FROM blocks WHERE kind='decision' AND ref=?",(decision,))
            self._close_notices(row['project'],decision,actor.id,'decision_applied',kinds=('product_decision','provisional_decision'))
            task_revalidations=[]
            if actions['decline_change']:
                expected_tasks=review_material.get('task_revalidations',[])
                task_revalidations=self._restore_keep_existing_task_fences(actor,row['project'],
                    body['change'],decision,expected_tasks)
                need(canonical(task_revalidations)==canonical(expected_tasks),'stale_decision',
                     'Task revalidation proof changed while applying the decision')
            self.sec.event(row['project'],'decision_applied',actor.id,{'decision':decision,'receipt':review_receipt,
                'selected_effect':effect})
            if incremental is not None:
                if incremental.get('format')=='decision-apply-incremental-review.v1':
                    apply_events=self._decision_apply_event_references(row['project'],decision,
                                                                       review_receipt,body)
                    self.sec.event(row['project'],'decision_apply_incremental_review_applied',actor.id,
                        {'decision':decision,'base_review_receipt':incremental['proof']['base_review_receipt'],
                         'supplemental_review_receipt':review_receipt,
                         'current_material_digest':incremental['proof']['current_material_digest'],
                         'proof':incremental['proof'],'application_events':apply_events})
                else:
                    archive_proof={**incremental['proof'],'base_material':incremental['base_material']}
                    self.sec.event(row['project'],'decision_incremental_review_applied',actor.id,
                        {'decision':decision,'base_review_receipt':incremental['proof']['base_review_receipt'],
                         'supplemental_review_receipt':review_receipt,
                         'current_material_digest':incremental['current_material_digest'],
                         'proof':archive_proof})
        result={'id':decision,'status':'applied','task_revalidations':task_revalidations}
        if incremental is not None:
            result.update({'review_mode':'incremental',
                'base_review_receipt':incremental['proof']['base_review_receipt'],
                'supplemental_review_receipt':review_receipt,
                'current_material_digest':incremental['proof']['current_material_digest']})
            if incremental.get('format')=='decision-apply-incremental-review.v1':
                result['applied_decisions']= [item['decision'] for item in incremental['applied_effects']]
                result['incremental_review_format']=incremental['format']
        return result

    def apply_technical_change(self,actor,change,review_receipt):
        with self.s.transaction():
            row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
            actor.require('owner','agent',project=row['project'])
            need(row['stage'] in {'local_repair','module_replan','system_replan','reconciling'},
                 'invalid_stage','A product-level decision cannot be bypassed through technical apply')
            change_row,change_material=self.change_review_material(change)
            need(change_row['project']==row['project'],'cross_project','Change belongs elsewhere')
            binding=digest(change_material)
            self.g.require_review(review_receipt,change,binding,{'consistency'},latest=True)
            all_markers=set(change_material.get('required_coverage',[]))
            review=self.g.receipt(review_receipt)
            covered=set(review['result'].get('covered',[]))
            need(all_markers<=covered,'incomplete_review_coverage',
                 'Consistency review must cover every required change-material packet',
                 {'missing':sorted(all_markers-covered)})
            self._validate_change_scope_result(change_material,review['result'],require_within=True)
            body=parse_json(row['body'])
            if self._is_user_editorial_repair(actor,row['project'],body):
                need(all(value=='preserves_meaning' for value in
                         self._validate_change_scope_result(change_material,review['result'],
                                                            require_within=True)['effects'].values()),
                     'product_decision_required','An editorial user repair needs a meaning-preserving review')
            repair_markers=['display-metadata-equivalence:'+d['artifact'] for d in body.get('deltas',[])
                            if self._is_display_metadata_repair(self.k.artifact(actor,d['artifact']),d)]
            if repair_markers:
                observations=review['result'].get('observations',[])
                for delta,marker in zip([d for d in body.get('deltas',[])
                                         if self._is_display_metadata_repair(self.k.artifact(actor,d['artifact']),d)],repair_markers):
                    need(marker in covered and any(isinstance(o,dict) and o.get('ref')==delta['artifact'] and
                         isinstance(o.get('detail'),str) and o['detail'].strip() for o in observations),
                         'repair_review_required','Consistency review must explicitly assess this title change and cite its artifact')
            return self._apply_change(actor,change,review_receipt,None)

    def _apply_change(self,actor,change,receipt,decision,*,skip_conflict_check=False):
        row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
        body=parse_json(row['body']);deltas=body.get('deltas',[])
        need(deltas,'empty_change','No implementation delta was defined')
        # Registration can leave a no-effect request open for a substantive
        # follow-up, but no technical, human or batch apply path may turn that
        # exact no-op into an artifact revision or an acknowledgement.
        need(not self._change_has_no_effect(Actor('system','owner'),row['project'],body,deltas),
             'change_has_no_effect',
             'The proposed delta leaves accepted artifacts unchanged; the source request remains unresolved')
        change_row,material=self.change_review_material(change)
        review=self.g.receipt(receipt)
        scope_result=self._validate_change_scope_result(material,review.get('result',{}),
            allow_upper=decision is not None,require_within=decision is None)
        if decision is not None:
            direct_upper=(scope_result['scope']=='upper_scope_required' and
                          scope_result['target_layer']=='awaiting_product_decision')
            exhausted_system=self._has_current_system_no_solution_path(change,scope_result)
            need(row['stage']=='awaiting_product_decision' and (direct_upper or exhausted_system),
                 'human_approval_required','Upper-scope changes require a current human decision and resolved impact evidence',
                 {'stage':row['stage'],'scope':scope_result['scope'],
                  'layer':scope_result['layer'],'target_layer':scope_result['target_layer'],
                  'direct_upper':direct_upper,'system_no_solution_path':exhausted_system})
            need(not any(value=='unknown' for value in scope_result['effects'].values()) and
                 not any(value=='unknown' for value in scope_result['consumers'].values()) and
                 not scope_result['unknown_neighbors'] and
                 not scope_result['unknown_interface_contracts'],
                 'unknown_change_impact','Unresolved consumer or semantic impact prevents application')
        roots=self._change_roots(body)
        actual_impact=self.k.impact(Actor('system','owner'),row['project'],roots)
        need(canonical(actual_impact)==canonical(body.get('impact')),
             'stale_change_impact','Current graph impact differs; recalculate and review the change again')
        for task in actual_impact.get('tasks',[]):
            if self.s.one("SELECT id FROM tasks WHERE id=? AND status!='cancelled'",(task,)):
                need(self.s.one("SELECT 1 FROM blocks WHERE task=? AND kind='change' AND ref=?",(task,change)),
                     'stale_change_impact','A newly affected task is not stopped for this change',task)
        changed=self.validate_deltas(actor,row['project'],deltas)
        if not skip_conflict_check:
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
        task_revalidations=[]
        if decision is None:
            task_revalidations=self._revalidate_unaffected_change_tasks(actor,change,material,scope_result,receipt)
        self.s.execute("UPDATE changes SET stage='ready_for_reimplementation',revision=revision+1 WHERE id=?",(change,))
        self.s.execute("DELETE FROM blocks WHERE kind='change' AND ref=?",(change,))
        self._close_notices(row['project'],change,actor.id,'change_applied')
        self._terminalize_change_decisions(row['project'],change,actor.id,'change_applied',exclude=(decision,) if decision else ())
        self.sec.event(row['project'],'change_applied',actor.id,{'change':change,'receipt':receipt,'decision':decision,
            'changed':sorted(changed),'task_revalidations':task_revalidations})
        return {'id':change,'stage':'ready_for_reimplementation','reassessment_required':True,
                'task_revalidations':task_revalidations}

    def withdraw(self,actor,change,reason,compensation):
        row=self.s.one("SELECT * FROM changes WHERE id=?",(change,),True)
        actor.require('owner',project=row['project']);text(reason,'reason',12000)
        body=parse_json(row['body'])
        need(row['stage']!='ready_for_reimplementation' or bool(compensation),'compensation_required','Applied change cannot be silently discarded')
        with self.s.transaction():
            body['withdrawal']={'reason':reason,'compensation':compensation,'by':actor.id}
            self.s.execute("UPDATE changes SET stage='withdrawn',body=?,revision=revision+1 WHERE id=?",(canonical(body).decode(),change))
            self.s.execute("DELETE FROM blocks WHERE kind='change' AND ref=?",(change,))
            self._close_notices(row['project'],change,actor.id,'change_withdrawn')
            self._terminalize_change_decisions(row['project'],change,actor.id,'change_withdrawn')
            self.sec.event(row['project'],'change_withdrawn',actor.id,{'change':change,'reason':reason,'compensation':compensation})
        return {'id':change,'status':'withdrawn','history_preserved':True}
