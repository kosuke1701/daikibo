"""D08 — integrated candidate verification, protected scope and exact Git delivery."""
from __future__ import annotations
import copy
import os
import sys
from pathlib import Path
from .common import Actor, Fault, canonical, digest, finite_duration, need, obj, parse_json, strings, text, timestamp, uid
from .gitops import git

class Delivery:
    def __init__(self,store,security,knowledge,governance,workflow,snapshots,runtime):
        self.s,self.sec,self.k,self.g,self.w,self.sn,self.rt=store,security,knowledge,governance,workflow,snapshots,runtime
        runtime.delivery=self

    def profile_current(self,actor,project):
        """Return the frozen Delivery profile through a read-only public route.

        ``configure`` stores the immutable snapshot and its digest together in
        the profile row.  Readers must receive both that stored representation
        and the exact configure input projection so a caller cannot recompute
        a digest from a body that silently omitted the snapshot.  This method
        performs authorization and integrity/scope checks only; it emits no
        event and does not update any row.
        """
        actor.require('owner','agent','worker','reviewer','observer',project=project)
        row=self.s.one('SELECT body,digest,scope FROM profiles WHERE project=?',(project,))
        need(row,'profile_not_found','No current Delivery profile exists for this project')
        stored=parse_json(row['body']);scope=parse_json(row['scope'])
        need(isinstance(stored,dict),'profile_corrupt','Stored Delivery profile body is not an object')
        need(row['digest']==digest(stored),'profile_corrupt','Stored Delivery profile digest does not match its body')
        baseline=stored.get('baseline_snapshot')
        need(isinstance(baseline,dict),'profile_corrupt','Stored Delivery profile has no baseline snapshot')
        configure_body=copy.deepcopy(stored)
        configure_body.pop('baseline_snapshot',None)
        obj(configure_body,required=('target_environment','checks','required_requirements','required_tasks','applicability','repo_order','rollback'),
            optional=('reason','remote','build_outputs','program'))
        need(isinstance(scope,dict) and set(scope)=={'requirements','tasks','policy'},
             'profile_corrupt','Stored Delivery profile scope is malformed')
        need(scope['requirements']==sorted(configure_body['required_requirements']) and
             scope['tasks']==sorted(configure_body['required_tasks']) and
             isinstance(scope['policy'],str) and len(scope['policy'])==64 and
             all(char in '0123456789abcdef' for char in scope['policy']),
             'profile_corrupt','Stored Delivery profile scope does not bind its configure body')
        # A profile keeps the policy identity that was frozen with it.  A
        # later policy revision makes that identity non-current, but remains
        # readable as historical profile context.  Only malformed policy
        # identifiers are corruption; currentness is returned separately so
        # callers can apply the appropriate stale/current gate themselves.
        current_policy=self.g.policy(project,create=False)
        current_policy_digest=current_policy.get('digest') if isinstance(current_policy,dict) else None
        need(isinstance(current_policy_digest,str) and len(current_policy_digest)==64 and
             all(char in '0123456789abcdef' for char in current_policy_digest),
             'profile_corrupt','Current policy identity is malformed')
        policy_current=scope['policy']==current_policy_digest
        for task in configure_body['required_tasks']:
            self.s.one('SELECT id FROM tasks WHERE id=? AND project=?',(task,project),True)
        for repo in configure_body['repo_order']:
            self.s.one('SELECT id FROM repos WHERE id=? AND project=?',(repo,project),True)
        return {
            'project':project,
            'digest':row['digest'],
            'stored_digest':row['digest'],
            'body':copy.deepcopy(configure_body),
            'configure_body':copy.deepcopy(configure_body),
            'stored_body':copy.deepcopy(stored),
            'baseline_snapshot':copy.deepcopy(baseline),
            'scope':copy.deepcopy(scope),
            'policy_current':policy_current,
            'current_policy_digest':current_policy_digest,
            'scope_currentness':{'policy_current':policy_current,
                                 'stored_policy_digest':scope['policy'],
                                 'current_policy_digest':current_policy_digest},
            'read_only':True,
        }

    def configure(self,actor,project,body,expected_digest=None,review_receipt=None):
        actor.require('owner',project=project)
        obj(body,required=('target_environment','checks','required_requirements','required_tasks','applicability','repo_order','rollback'),optional=('reason','remote','build_outputs','program'))
        text(body['target_environment'],'target environment',10000);text(body['rollback'],'rollback procedure',10000)
        strings(body['required_requirements'],'requirements',nonempty=True);strings(body['required_tasks'],'tasks',nonempty=True);strings(body['repo_order'],'repo order',nonempty=True)
        checks=body['checks'];need(isinstance(checks,list) and checks,'invalid_profile','Profile needs concrete commands')
        from .build_outputs import validate_definition
        definitions=body.get('build_outputs',[])
        need(isinstance(definitions,list),'invalid_profile','build_outputs must be a list')
        defined={};produced=set();consumed=set()
        for output in definitions:
            validate_definition(output)
            need(output['id'] not in defined and output['repo'] in body['repo_order'],'invalid_profile','Duplicate or unknown build output')
            defined[output['id']]=output
        need(len({(o['repo'],o['path']) for o in definitions})==len(definitions),'invalid_profile','Output paths must be unique')
        categories=set();check_ids=set()
        for check in checks:
            obj(check,required=('id','category','repo','kind','argv'),optional=('timeout','report','required_tests','purpose','env','produces','uses'))
            strings(check['argv'],'command argv',nonempty=True);text(check['id'],'check ID',100)
            need(check['id'] not in check_ids,'invalid_profile','Duplicate delivery check');check_ids.add(check['id'])
            need(check['kind'] in {'pytest','junit','command'},'invalid_profile','Unknown check kind')
            need(check['repo'] in body['repo_order'],'invalid_profile','Check repo missing from delivery order')
            uses=strings(check.get('uses',[]),'used build outputs');makes=strings(check.get('produces',[]),'produced build outputs')
            need(set(uses)<=produced,'invalid_build_order','Consume only outputs from earlier checks')
            need(set(makes)<=defined.keys() and not set(makes)&produced,'invalid_build_order','Every output has one registered producer')
            need(not makes or check['category']=='build','invalid_build_order','Only build checks may produce deployable artifacts')
            produced.update(makes);consumed.update(uses)
            categories.add(check['category'])
            if check['kind']=='command':text(check.get('purpose'),'command purpose',5000)
        need(produced==defined.keys() and produced<=consumed,'unverified_build_output','Every declared build output must be produced and consumed by subsequent checks')
        need({'build','start','smoke','integration','scenario'}<=categories,'incomplete_profile','Build, start, smoke, integration and scenario checks are all required')
        need(any(c['kind'] in {'pytest','junit'} for c in checks),'test_inventory_required','Profile needs a measured test suite')
        need(isinstance(body['applicability'],dict),'invalid_profile','Explicit applicability decisions required')
        for category in ('migration','security','performance','contract'):
            applicability=body['applicability'].get(category)
            obj(applicability,required=('applicable','reason'))
            need(type(applicability['applicable']) is bool,'invalid_profile','Applicability must be Boolean');text(applicability['reason'])
            if applicability['applicable']:need(category in categories,'missing_required_check',f'{category} is applicable but has no check')
        for ref in body['required_requirements']:
            art=self.k.artifact(actor,ref);need(art['project']==project and art['kind']=='requirement' and art['status']=='accepted','invalid_scope','Required requirement must be current and accepted')
        for task in body['required_tasks']:self.s.one('SELECT id FROM tasks WHERE id=? AND project=?',(task,project),True)
        for repo in body['repo_order']:self.s.one('SELECT id FROM repos WHERE id=? AND project=?',(repo,project),True)
        if body.get('program'):
            self.s.one('SELECT id FROM programs WHERE id=? AND project=?',(body['program'],project),True)
        old=self.s.one('SELECT * FROM profiles WHERE project=?',(project,))
        if old:
            need(expected_digest==old['digest'] and review_receipt,'scope_change_required','Profile is frozen; authenticate a reviewed amendment')
            self.g.require_review(review_receipt,project,digest(body),{'delivery_profile'})
        scope={'requirements':sorted(body['required_requirements']),'tasks':sorted(body['required_tasks']),'policy':self.g.policy(project)['digest']}
        base=self.sn.capture(actor,project,body['repo_order'])
        stored={**body,'baseline_snapshot':base}
        with self.s.transaction():
            self.s.execute('INSERT INTO profiles VALUES(?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET body=excluded.body,digest=excluded.digest,scope=excluded.scope,created=excluded.created',
                           (project,canonical(stored).decode(),digest(stored),canonical(scope).decode(),timestamp()))
            self.sec.event(project,'delivery_profile_frozen',actor.id,{'digest':digest(stored),'scope':scope,'previous':old['digest'] if old else None,'review':review_receipt})
        return {'project':project,'digest':digest(stored),'scope':scope}

    def _order(self,tasks):
        ids=set(tasks);visited=set();visiting=set();result=[]
        def visit(task):
            need(task not in visiting,'cycle','Cyclic task dependencies')
            if task in visited:return
            visiting.add(task)
            for r in self.s.all('SELECT dependency FROM task_deps WHERE task=?',(task,)):
                need(r['dependency'] in ids,'incomplete_scope','Required dependency omitted from scope')
                visit(r['dependency'])
            visiting.remove(task);visited.add(task);result.append(task)
        for task in sorted(ids):visit(task)
        return result

    def assemble(self,actor,project,tasks,baseline=None,readonly=False):
        original=copy.deepcopy(baseline or self.sn.capture(actor,project,store_blobs=not readonly))
        for task in self._order(tasks):
            row=self.w.task(actor,task)
            need(row['status']=='completed' and row['validity']=='current','incomplete_work','Required task is not currently complete',task)
            need(not self.g.check_current(task,ensure_policy=not readonly),'stale_context','Task inputs are stale',task)
            if row['body'].get('task_kind')=='analysis':continue  # Experiment outputs are never promoted into a delivery snapshot.
            c=parse_json(self.s.one('SELECT body FROM candidates WHERE id=?',(row['candidate'],),True)['body'])
            for delta in c['changes']:
                need(delta['repo'] in original['repos'],'missing_repository','Candidate refers to an omitted repository')
                files=original['repos'][delta['repo']]['files'];current=files.get(delta['path'])
                if current==delta['after']:continue
                need(current==delta['before'],'integration_conflict','Parallel candidates changed the same code incompatibly',{'task':task,'repo':delta['repo'],'path':delta['path']})
                if delta['after'] is None:files.pop(delta['path'],None)
                else:files[delta['path']]=delta['after']
        for repo in original['repos'].values():repo['bytes']=sum(e.get('size',0) for e in repo['files'].values())
        original.pop('digest',None);original['digest']=digest(original)
        return original

    def prepare(self,actor,project):
        actor.require('owner','agent',project=project)
        profile=self.s.one('SELECT * FROM profiles WHERE project=?',(project,),True)
        scope=parse_json(profile['scope']);body=parse_json(profile['body'])
        current_req={r['id'] for r in self.s.all("SELECT id FROM artifacts WHERE project=? AND kind='requirement' AND status='accepted'",(project,))}
        need(current_req==set(scope['requirements']),'scope_mismatch','The protected complete requirement set no longer matches; explicit reviewed amendment required')
        current_tasks={r['id'] for r in self.s.all("SELECT id FROM tasks WHERE project=? AND status!='cancelled'",(project,))}
        need(current_tasks==set(scope['tasks']),'scope_mismatch','The protected complete task set does not match')
        program=body.get('program')
        if program is None:
            latest=self.s.one('SELECT id FROM programs WHERE project=? ORDER BY created DESC,id DESC LIMIT 1',(project,))
            program=latest['id'] if latest else None
        breakdown=None
        if program:
            status=self.rt.breakdowns.program_status(actor,program)
            need(status['current'],'breakdown_gate_denied','Delivery must name the currently reviewed execution plan',status['failures'])
            active=self.rt.breakdowns.active(actor,program)
            breakdown={'id':active['id'],'digest':active['digest']}
        snapshot=self.assemble(actor,project,scope['tasks'],body['baseline_snapshot'])
        binding={'program':program,'breakdown':breakdown,'profile':profile['digest'],'scope':scope,'snapshot':snapshot['digest'],
                 'tasks':[{"id":t,"binding":self.g.task_binding(t)} for t in sorted(scope['tasks'])],
                 'requirements':[{k:r[k] for k in ('id','revision','digest')} for r in [self.k.artifact(actor,x) for x in scope['requirements']]],
                 'policy':self.g.policy(project)['digest']}
        record={'binding':binding,'snapshot':snapshot,'build_definitions':body.get('build_outputs',[]),'build_outputs':{},'checks':body['checks'],'target_environment':body['target_environment'],
                'applicability':body['applicability'],'rollback':body['rollback'],'results':[],'git':{},'limitations':[]}
        ident=uid('DELIVERY');h=digest(binding)
        with self.s.transaction():
            self.s.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',(ident,project,canonical(record).decode(),h,'prepared',timestamp()))
            self.sec.event(project,'integration_snapshot_frozen',actor.id,{'delivery':ident,'digest':h,'snapshot':snapshot['digest']})
        return {'id':ident,'digest':h,'snapshot':snapshot['digest']}

    def current(self,delivery):
        row=self.s.one('SELECT * FROM deliveries WHERE id=?',(delivery,),True);body=parse_json(row['body']);binding=body['binding']
        profile=self.s.one('SELECT digest FROM profiles WHERE project=?',(row['project'],),True)
        failures=[]
        if profile['digest']!=binding['profile']:failures.append('profile_changed')
        if binding.get('program') and binding.get('breakdown'):
            active=self.rt.breakdowns.active(Actor('controller','owner'),binding['program'])
            if not active or (active['id'],active['digest'])!=(binding['breakdown']['id'],binding['breakdown']['digest']):
                failures.append('breakdown_replaced')
        if self.g.policy(row['project'])['digest']!=binding['policy']:failures.append('policy_changed')
        current_req={r['id'] for r in self.s.all("SELECT id FROM artifacts WHERE project=? AND kind='requirement' AND status='accepted'",(row['project'],))}
        current_tasks={r['id'] for r in self.s.all("SELECT id FROM tasks WHERE project=? AND status!='cancelled'",(row['project'],))}
        if current_req!=set(binding['scope']['requirements']) or current_tasks!=set(binding['scope']['tasks']):failures.append('protected_scope_changed')
        for t in binding['tasks']:
            if self.g.task_binding(t['id'])!=t['binding'] or self.g.check_current(t['id']):failures.append('task_changed:'+t['id'])
        for r in binding['requirements']:
            a=self.k.artifact(Actor('system','owner'),r['id'])
            if a['revision']!=r['revision'] or a['digest']!=r['digest'] or a['status']!='accepted':failures.append('requirement_changed:'+r['id'])
        need(not failures,'stale_delivery','Delivery input changed',failures)
        return row,body

    def verify(self,actor,delivery):
        row,body=self.current(delivery);actor.require('owner','agent',project=row['project'])
        results=[];available={};definitions={o['id']:o for o in body.get('build_definitions',[])}
        snapshot_ref,_=self.rt.verification_materials.pin_delivery_snapshot(
            actor,row['project'],row,body,
            captured_from={'controller':'delivery','operation':'delivery.verify','capture_id':uid('VMAT')})
        expected_binding=row['digest'];expected_snapshot=body['snapshot']['digest']
        expected_checks={item['id']:digest(item) for item in body['checks']}
        for check in body['checks']:
            if not set(check.get('uses',[]))<=available.keys():
                results.append({'check':check['id'],'category':check['category'],'passed':False,'blocked':'Required build output was not successfully produced'});continue
            adjusted=dict(check);adjusted['build_inputs']=[available[k] for k in check.get('uses',[])];adjusted['build_outputs']=[definitions[k] for k in check.get('produces',[])]
            target_name=body['snapshot']['repos'][check['repo']]['name']
            report=check.get('report','results.xml')
            original_report=check.get('original_report',report)
            if len(body['snapshot']['repos'])>1:
                adjusted['report']=target_name+'/'+report
                adjusted['original_report']=target_name+'/'+original_report
            else:
                adjusted['original_report']=original_report
            def command(work,home,cwd,selected=adjusted):
                args=list(selected['argv'])
                if args[0] in {'python','python3'}:args[0]=sys.executable
                if selected['kind']=='pytest':
                    report_path=selected.get('report','results.xml')
                    # Multi-repository checks execute from their repository
                    # subdirectory, while the collector validates the report
                    # from the aggregate snapshot root.  The selected report
                    # remains the root-relative identity (for ignored-file
                    # comparison); only the subprocess argument walks back to
                    # that root before entering the repository-qualified path.
                    if len(check_snapshot['repos'])>1:
                        report_path='../'+report_path
                    args+=['--junitxml',report_path,'-p','no:cacheprovider']
                return args,None
            # Every check sees an independent clean reconstruction of the complete snapshot.
            # One repo is the working directory; multi-repo consumers remain available as siblings.
            check_snapshot=body['snapshot']
            def factory(work,home,cwd,selected=adjusted):
                target=work/check_snapshot['repos'][check['repo']]['name']
                argv,_=command(work,home,target,selected)
                # Trusted Python wrapper changes cwd; caller cannot pick a host directory.
                wrapper='import os,sys; os.chdir(sys.argv[1]); os.execvp(sys.argv[2],sys.argv[2:])'
                return [sys.executable,'-c',wrapper,str(target),*argv],None
            # Runtime.observe sees the aggregate work root for multi-repository
            # checks, while the trusted command runs inside check['repo'].
            # Keep both report coordinates in that same aggregate space; an
            # original_report is a second path in the checked repository, not
            # a repository-qualified path supplied by the caller.
            check_ref=self.rt.verification_materials.delivery_check_ref(row['project'],snapshot_ref,check)
            expected_check_digest=expected_checks[check['id']]
            def revalidate(expected_binding=expected_binding,expected_snapshot=expected_snapshot,
                           expected_check_digest=expected_check_digest,check_id=check['id']):
                fresh_row,fresh_body=self.current(delivery)
                if (fresh_row['digest']!=expected_binding or
                        digest(fresh_body.get('binding',{}))!=expected_binding or
                        fresh_body.get('snapshot',{}).get('digest')!=expected_snapshot):
                    return {'current':False,'reason':'delivery_binding_changed'}
                fresh_checks=[item for item in fresh_body.get('checks',[]) if item.get('id')==check_id]
                if len(fresh_checks)!=1 or digest(fresh_checks[0])!=expected_check_digest:
                    return {'current':False,'reason':'delivery_check_changed'}
                return {'current':True}
            check_timeout=check.get('timeout',300)
            if check_timeout is None: check_timeout=300
            check_timeout=finite_duration(check_timeout,'delivery check timeout')
            execution_context=self.rt.verification_materials.context(
                actor=actor,definition_ref=check_ref,
                execution_subject={'kind':'delivery','id':delivery,'binding':row['digest']},
                task_revision=None,candidate_ref=None,binding=row['digest'],
                timeout_authorization_refs=[],
                test_artifact_refs=self.rt.execution_test_artifact_refs(actor, row['project'], check_ref),
                revalidate=revalidate,
                captured_from={'controller':'delivery','operation':'delivery.verify','capture_id':uid('VMAT')})
            run_id=uid('RUN')
            observed,_,_=self.rt.observe(row['project'],None,delivery,'delivery:'+check['id'],None,row['digest'],check_snapshot,factory,
                                         timeout=check_timeout,check=adjusted,extra_env=check.get('env'),
                                         run_id=run_id,verification_context=execution_context)
            if observed['result'].get('passed'):
                for output in observed['result'].get('build_outputs',[]):available[output['id']]={**output,'producer_receipt':observed['id']}
            results.append({'check':check['id'],'category':check['category'],'receipt':observed['id'],'passed':observed['result'].get('passed',False),
                            'verification_material':observed.get('verification_material')})
        with self.s.transaction():
            row,body=self.current(delivery);body['results']=results;body['build_outputs']=available
            self.s.execute('UPDATE deliveries SET body=? WHERE id=?',(canonical(body).decode(),delivery))
            self.sec.event(row['project'],'integration_checks_observed',actor.id,{'delivery':delivery,'results':results})
        return {'delivery':delivery,'results':results,'needs_independent_whole_change_reviews':True}

    def review_subject(self,actor,subject):
        row,body=self.current(subject);self.k.project(actor,row['project'])
        context={k:body[k] for k in ('binding','target_environment','applicability','rollback','results','build_outputs')}
        profile=parse_json(self.s.one('SELECT body FROM profiles WHERE project=?',(row['project'],),True)['body'])
        baseline=profile['baseline_snapshot']
        context['baseline']={'snapshot_blob':self.s.blob_put(canonical(baseline)),
            'snapshot_digest':baseline['digest'],
            'instructions':'Read this frozen baseline manifest with blob.read (base64, paginated), then read required original files by each entry blob. Compare them with the supplied integrated workspace to assess test preservation and whole-change effects. Never infer preservation from a PASS label.'}
        context['required_scenarios']=[self.k.artifact(actor,r['id']) for r in body['binding']['requirements']]
        from .obligations import delivery_coverage
        context.update(delivery_coverage(context['required_scenarios']))
        return row['project'],row['digest'],body['snapshot'],context,None

    def certify(self,actor,delivery,check_only=False):
        row,body=self.current(delivery);actor.require('owner','agent',project=row['project'])
        failures=[]
        with self.s.transaction():
            if self.g.mode!='governed':failures.append('validation_mode_cannot_certify_deploy_ready')
            program=body['binding'].get('program')
            if not program or not body['binding'].get('breakdown'):
                failures.append('engineering_workflow_required')
            else:
                state=self.s.one('SELECT phase FROM programs WHERE id=?',(program,),True)
                if state['phase'] not in {'integration','delivery'}:failures.append('engineering_phases_incomplete')
                report=self.rt.breakdowns.program_status(actor,program)
                if not report['current']:failures.append('breakdown_not_current:'+program)
                if getattr(self.rt,'workstreams',None):
                    delegated=self.rt.workstreams.program_audit(actor,program)
                    if not delegated['current']:failures.append('delegated_work_incomplete:'+program)
            for change in self.s.all("SELECT id FROM changes WHERE project=? AND stage NOT IN ('withdrawn','ready_for_reimplementation','closed')",(row['project'],)):
                failures.append('unresolved_change:'+change['id'])
            for t in body['binding']['tasks']:
                task=self.w.task(actor,t['id'])
                if task['status']!='completed' or task['validity']!='current':failures.append('task_not_complete:'+t['id'])
                # Task-level receipts are revalidated, not trusted solely from status labels.
                roles=list(self.g.policy(row['project'])['body']['review_roles'])
                if task['body'].get('risk')=='critical':roles+=self.g.policy(row['project'])['body']['critical_review_roles']
                for role in roles:
                    refs=self.g.evidence_for(t['id'],t['binding'],role)
                    try:
                        need(refs,'missing_evidence','Missing task review');self.g.require_review(refs[0]['id'],t['id'],t['binding'],{role})
                    except Fault as exc:failures.append(t['id']+':'+role+':'+exc.code)
                plan=parse_json(self.s.one('SELECT body FROM plans WHERE task=?',(t['id'],),True)['body'])
                candidate=self.s.one('SELECT body FROM candidates WHERE id=?',(task['candidate'],)) if task['candidate'] else None
                snapshot_digest=None
                if candidate:
                    candidate_body=parse_json(candidate['body'])
                    snapshot=candidate_body.get('snapshot')
                    snapshot_digest=snapshot.get('digest') if isinstance(snapshot,dict) else None
                selection=self.g.task_test_evidence(actor,t['id'],binding=t['binding'],snapshot_digest=snapshot_digest)
                observed={item['check_id']:item for item in selection['checks']}
                for check in plan['checks']:
                    try:
                        item=observed.get(check['id'])
                        need(item is not None,'missing_evidence','Required task test is absent from the frozen plan')
                        need(item['status']=='executed','test_failed','Task test is no longer valid',item)
                    except Fault as exc:failures.append(t['id']+':'+check['id']+':'+exc.code)
            if not self.k.trace(actor,row['project'])['structural_complete']:failures.append('traceability_incomplete')
            if not self.k.source_coverage(actor,row['project'])['structurally_complete']:failures.append('source_coverage_incomplete')
            by_id={r['check']:r for r in body['results']}
            for check in body['checks']:
                result=by_id.get(check['id'])
                if not result or not result.get('receipt'):failures.append('integration_missing:'+check['id']);continue
                try:
                    ev=self.g.receipt(result['receipt'])
                    consumed={o['id']:o for o in ev['result'].get('build_inputs',[])}
                    for output_id in check.get('uses',[]):
                        expected=body.get('build_outputs',{}).get(output_id)
                        if not expected or consumed.get(output_id)!=expected:failures.append('build_input_mismatch:'+check['id']+':'+output_id)
                    for output in ev['result'].get('build_outputs',[]):
                        expected=body.get('build_outputs',{}).get(output['id'])
                        if expected!={**output,'producer_receipt':ev['id']}:failures.append('build_output_mismatch:'+output['id'])
                    if ev['binding']!=row['digest'] or ev['subject']!=delivery or not ev['result'].get('passed') or ev['assurance']!='governed' or ev['exit_code']!=0 or any(ev.get(x) for x in ('timed_out','cancelled','output_overflow','input_mutated')):failures.append('integration_failed:'+check['id'])
                except Fault as exc:failures.append('integration:'+exc.code)
            for role in ('integration','goal_validation'):
                refs=self.g.evidence_for(delivery,row['digest'],role)
                try:
                    need(refs,'missing_evidence','Missing whole-change review');ev=self.g.require_review(refs[0]['id'],delivery,row['digest'],{role})
                    from .obligations import delivery_coverage
                    required=set(delivery_coverage([self.k.artifact(actor,req['id']) for req in body['binding']['requirements']])['required_coverage'])
                    need(required<=set(ev['result'].get('covered',[])),'review_coverage','Whole-change review omitted acceptance criteria')
                except Fault as exc:failures.append(role+':'+exc.code)
            if getattr(self,'traceability',None) is not None:
                trace_gate=self.traceability.integrated_closure_gate(row['project'],delivery,actor)
                failures.extend(trace_gate['failures'])
                if row['status']=='delivered':
                    delivered_gate=self.traceability.delivered_closure_gate(row['project'],delivery,actor)
                    failures.extend(delivered_gate['failures'])
            if self.s.one("SELECT id FROM decisions WHERE project=? AND status NOT IN ('applied','rejected','superseded')",(row['project'],)):failures.append('unconfirmed_decisions')
            if self.s.one("SELECT id FROM conflicts WHERE project=? AND status NOT IN ('resolved','rejected')",(row['project'],)):failures.append('unresolved_conflicts')
            if self.s.one("SELECT id FROM waivers WHERE project=? AND status IN ('active','expired')",(row['project'],)):failures.append('unresolved_waivers')
            need(not failures,'release_gate_denied','Integrated delivery is not ready',failures)
            if check_only:
                return {'id':delivery,'status':row['status'],'currently_valid':True,
                        'deploy_ready_for':body['target_environment'],'automatic_deployment':False}
            self.s.execute("UPDATE deliveries SET status='verified' WHERE id=?",(delivery,))
            self.sec.event(row['project'],'deploy_ready_certified',actor.id,{'delivery':delivery,'binding':row['digest'],'target_environment':body['target_environment']})
        return {'id':delivery,'status':'verified','deploy_ready_for':body['target_environment'],'automatic_deployment':False}

    def commit(self,actor,delivery,message):
        actor.require('owner');text(message,'commit message',20000)
        row,body=self.current(delivery)
        need(row['status'] in {'verified','delivered'},'release_gate_denied','Only a certified snapshot can become formal delivery')
        self.certify(actor,delivery,check_only=True)
        ref='refs/heads/daikibo/'+delivery
        profile=parse_json(self.s.one('SELECT body FROM profiles WHERE project=?',(row['project'],),True)['body'])

        # Keep the external Git observation separate from the later material
        # and release proof.  A capture or final-gate failure must never erase
        # a body.git/outbox result that is needed for reconciliation.
        def _saved_state():
            saved_row, saved_body = self.current(delivery)
            need(isinstance(saved_body.get('git'), dict),
                 'integrity_error','Delivery Git observations are malformed')
            return saved_row, saved_body

        def _failure_details(existing=None):
            try:
                # Read the authoritative stored body directly here.  A later
                # currentness fault must not hide observations that were
                # already committed and need reconciliation.
                stored = self.s.one('SELECT body FROM deliveries WHERE id=?',(delivery,),True)
                stored_body = parse_json(stored['body'])
                saved_repositories = sorted(stored_body.get('git', {})) if isinstance(stored_body,dict) and isinstance(stored_body.get('git'),dict) else []
            except Fault:
                saved_repositories = []
            return {
                'delivery': delivery,
                'saved_repositories': saved_repositories,
                'actual_delivery_commit_refs': list(existing or []),
                'pending_repositories': [repo for repo in profile['repo_order']
                                         if repo not in saved_repositories],
            }

        # Phase 1: commit/reconcile every repository, then commit the exact
        # observation and outbox result.  No verification material is written
        # in this phase.
        for rid in profile['repo_order']:
            row,body=_saved_state()
            if rid in body['git']:
                # A restart may find body.git durable while the outbox result
                # was still pending.  Repair only that local projection; do
                # not repeat a Git side effect.
                saved_result=body['git'][rid]
                with self.s.transaction():
                    row,body=_saved_state()
                    observed=body['git'].get(rid)
                    need(observed is not None and canonical(observed)==canonical(saved_result),
                         'git_conflict','Saved Delivery Git observation changed during reconciliation',rid)
                    outbox=self.s.one('SELECT * FROM outbox WHERE dedup=?',(delivery+':'+rid,))
                    if outbox is None:
                        self.w.emit(row['project'],'git_commit',delivery+':'+rid,
                                   {'delivery':delivery,'repo':rid,'ref':ref,
                                    'snapshot':body['snapshot']['digest']})
                        outbox=self.s.one('SELECT * FROM outbox WHERE dedup=?',(delivery+':'+rid,),True)
                    if outbox['status']!='delivered' or outbox.get('result')!=canonical(saved_result).decode():
                        self.s.execute("UPDATE outbox SET status='delivered',result=?,attempts=attempts+1 WHERE dedup=?",
                                       (canonical(saved_result).decode(),delivery+':'+rid))
                continue
            self.certify(actor,delivery,check_only=True)
            intent={'delivery':delivery,'repo':rid,'ref':ref,'snapshot':body['snapshot']['digest']}
            with self.s.transaction():self.w.emit(row['project'],'git_commit',delivery+':'+rid,intent)
            bare=self.s.home/'git'/rid
            existing=git(bare,'rev-parse','--verify',ref,check=False) if bare.exists() else None
            if existing and existing.returncode==0:
                # Reconcile exact objects, not merely the existence of a branch name.
                expected=existing.stdout.decode().strip()
                verified=self.sn.commit_snapshot(body['snapshot'],rid,message,ref=ref,expected=expected,reconcile_only=True)
                need(verified['commit']==expected,'git_conflict','Existing delivery branch did not represent the sealed snapshot')
                result=verified
            else:result=self.sn.commit_snapshot(body['snapshot'],rid,message,ref=ref)
            with self.s.transaction():
                row,body=_saved_state()
                if rid in body['git']:
                    need(canonical(body['git'][rid])==canonical(result),
                         'git_conflict','Delivery was observed with another Git result',rid)
                    result=body['git'][rid]
                else:
                    body['git'][rid]=result
                self.s.execute('UPDATE deliveries SET body=? WHERE id=?',(canonical(body).decode(),delivery))
                self.s.execute("UPDATE outbox SET status='delivered',result=?,attempts=attempts+1 WHERE dedup=?",(canonical(result).decode(),delivery+':'+rid))
                self.sec.event(row['project'],'repository_commit_observed','git-broker',result)

        row,body=_saved_state()
        need(set(body['git'])==set(profile['repo_order']),'partial_delivery','Not all repositories were delivered')

        actual_commit_refs=[]
        try:
            # Snapshot capture has its own durable transaction and is the
            # immutable dependency for all actual commit pins.
            with self.s.transaction():
                row,body=_saved_state()
                snapshot_ref, _ = self.rt.verification_materials.pin_delivery_snapshot(
                    actor, row['project'], row, body,
                    captured_from={'controller':'delivery','operation':'delivery.commit.snapshot'},
                )

            # Enumerate saved material read-only.  Existing actual refs are
            # reused; only repositories with a saved Git observation but no
            # valid material are sent through the existing producer.
            from .delivery_material_reader import read_delivery_material
            material = read_delivery_material(self.rt, actor, project=row['project'], delivery=snapshot_ref)
            existing_refs = {
                item['repository']: item['actual_ref']
                for item in material['repositories']
                if item.get('actual_ref') is not None
            }
            for rid in profile['repo_order']:
                row,body=_saved_state()
                saved_ref=existing_refs.get(rid)
                if saved_ref is not None:
                    actual_commit_refs.append(saved_ref)
                    continue
                # A producer failure here leaves all prior pins and the
                # complete observed Git map committed for a later retry.
                with self.s.transaction():
                    row,body=_saved_state()
                    actual_ref, _ = self.rt.verification_materials.pin_actual_delivery_commit(
                        actor, row['project'], row, body, rid, body['git'][rid],
                        snapshot_ref=snapshot_ref,
                        captured_from={'controller':'delivery','operation':'delivery.commit.actual_commit'},
                    )
                actual_commit_refs.append(actual_ref)
                existing_refs[rid]=actual_ref
        except Fault as exc:
            details=_failure_details(actual_commit_refs)
            if isinstance(exc.details, dict): exc.details={**exc.details, **details}
            elif exc.details is None: exc.details=details
            raise

        actual_commit_refs=sorted(actual_commit_refs,key=lambda value:value['repository'])

        # Mapping observations are durable independently of the final status.
        try:
            with self.s.transaction():
                row,body=_saved_state()
                if getattr(self,'traceability',None) is not None:
                    self.traceability.record_delivered_mappings(actor,row['project'],delivery,row,body)
        except Fault as exc:
            details=_failure_details(actual_commit_refs)
            if isinstance(exc.details, dict): exc.details={**exc.details, **details}
            elif exc.details is None: exc.details=details
            raise

        # Final gate and status transition are last.  A rejection preserves
        # body.git, outbox, actual material, and mapping records above.
        try:
            with self.s.transaction():
                row,body=_saved_state()
                if getattr(self,'traceability',None) is not None:
                    delivered_gate=self.traceability.delivered_closure_gate(row['project'],delivery,actor)
                    need(delivered_gate['allowed'],'traceability_incomplete',
                         'Actual delivery mapping is incomplete',delivered_gate)
                self.certify(actor,delivery,check_only=True)
                self.s.execute("UPDATE deliveries SET status='delivered' WHERE id=?",(delivery,))
                self.sec.event(row['project'],'delivery_finalized',actor.id,
                               {'delivery':delivery,'repositories':sorted(body['git'])})
        except Fault as exc:
            details=_failure_details(actual_commit_refs)
            if isinstance(exc.details, dict): exc.details={**exc.details, **details}
            elif exc.details is None: exc.details=details
            raise
        row,body=_saved_state()
        return {'id':delivery,'status':'delivered','manifest':body,'digest':digest(body),
                'actual_delivery_commit_refs':actual_commit_refs}

    def export_bundle(self,actor,delivery,repo):
        actor.require('owner');row,body=self.current(delivery)
        need(row['status']=='delivered' and repo in body['git'],'not_delivered','No verified commit to export')
        self.certify(actor,delivery,check_only=True)
        g=body['git'][repo];path=self.s.home/'exports'/f'{delivery}-{repo}.bundle';path.parent.mkdir(exist_ok=True,mode=0o700)
        git(Path(g['git_dir']),'bundle','create',str(path),g['ref'])
        data=path.read_bytes();h=self.s.blob_put(data)
        return {'blob':h,'bytes':len(data),'commit':g['commit'],'ref':g['ref']}
