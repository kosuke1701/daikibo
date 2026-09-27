"""Composition root and explicit role-aware workflow command surface."""
from __future__ import annotations
import base64
import dataclasses
import inspect
import threading
from pathlib import Path
from .common import Actor,Fault,canonical,digest,need,obj,parse_json,text,timestamp,uid
from .db import Store
from .security import Security
from .knowledge import Knowledge, artifact_body_contract
from .governance import Governance
from .workflow import Workflow
from .planning import Planning
from .gitops import Snapshots
from .runtime import Runtime
from .indexing import Indexer,Contexts
from .delivery import Delivery
from .interaction import Interaction
from .operations import Operations

class Control:
    def __init__(self,home,mode='governed',start_workers=True):
        need(mode in {'governed','validation'},'invalid_mode','Unknown assurance mode')
        need(not (Path(home)/'RETIRED.json').exists(),'retired_installation','This vault is retired. Restore a reviewed backup into a new vault to reactivate.')
        self.s=Store(home);self.sec=Security(self.s);self.sec.bootstrap()
        self.k=Knowledge(self.s,self.sec);self.g=Governance(self.s,self.sec,self.k,mode);self.k.assessments=self.g
        self.w=Workflow(self.s,self.sec,self.k,self.g);self.p=Planning(self.s,self.sec,self.k,self.g,self.w)
        # Unit4-R readonly admission must consume the complete composition
        # root.  Binding this identity early lets Governance/Workflow keep a
        # single evaluator, assurance, traceability, and local-execution
        # graph once the remaining services are attached below.
        self.g.control=self;self.w.control=self
        self.sn=Snapshots(self.s,self.sec,self.k);self.rt=Runtime(self.s,self.sec,self.k,self.g,self.w,self.sn,self.p,mode)
        # Keep the Workflow definition-pin path on the exact Runtime-owned
        # coordinator; no public route or alternate material store is added.
        self.w.verification_materials=self.rt.verification_materials
        # Private Runtime handoffs bind to this exact composition root.  The
        # identity is never serialized into a receipt or accepted from a
        # public report argument.
        self.rt.control=self
        from .execution_controls import ExecutionControls
        self.execution_controls=ExecutionControls(self)
        self.g.execution_controls=self.execution_controls
        self.w.execution_controls=self.execution_controls
        self.rt.execution_controls=self.execution_controls
        self.idx=Indexer(self.s,self.sec,self.k);self.ctx=Contexts(self.s,self.k,self.g,self.idx,self.sn);self.rt.context=self.ctx;self.p.indexer=self.idx
        self.d=Delivery(self.s,self.sec,self.k,self.g,self.w,self.sn,self.rt)
        self.i=Interaction(self.s,self.sec,self.k,self.p,self.w,self.g);self.ops=Operations(self)
        from .knowledge_history import KnowledgeHistory
        self.history=KnowledgeHistory(self.s,self.sec,self.k)
        from .traceability import Traceability
        self.traceability=Traceability(self)
        # Workflow/planning/delivery use this optional boundary only when a
        # program explicitly adopts a mandatory traceability binding.  All
        # legacy programs have no such binding and retain their old gates.
        self.p.traceability=self.traceability
        self.w.traceability=self.traceability
        self.d.traceability=self.traceability
        from .assurance import Assurance
        self.assurance=Assurance(self)
        # Unit 4 writer gates consume the same composition root as the
        # read-only stage evaluator; Planning must never construct a weaker
        # substitute controller for its transaction-bound plan proof.
        self.p.control=self
        # Consumer-P is a Workflow action, but its immutable provenance
        # writer is the controller-owned Assurance boundary.  Bind the two
        # after composition so standalone Workflow callers cannot construct a
        # second, weaker material store.
        self.w.assurance=self.assurance
        # Runtime execution-material adapters bind to the same E1 immutable
        # writer; no duplicate runtime assurance store is permitted.
        self.rt.assurance=self.assurance
        self.routes={};self.read_routes=set();self._register_routes();self.closed=False
        for method,function in {'artifact.history':self.history.artifact_history,'baseline.list':self.history.list,
                                'baseline.get':self.history.get,'baseline.verify':self.history.verify,
                                'baseline.export':self.history.export_archive,'baseline.rebuild_git':self.history.rebuild_git,
                                'baseline.inspect_archive':self.history.inspect_archive}.items():
            self.register(method,function,read=method not in {'baseline.export','baseline.rebuild_git'})
        from .navigation import Navigation
        self.nav = Navigation(self)
        self.i.nav = self.nav
        for name, fn in {'artifact.catalog': self.nav.artifacts, 'artifact.read': self.nav.artifact_read,
                         'inbox.catalog': self.nav.inbox, 'inbox.read': self.nav.inbox_read,
                         'program.catalog': self.nav.programs, 'program.blockers': self.nav.blockers,
                         'workflow.summary': self.nav.summary}.items():
            self.register(name, fn, read=True)
        from .jobs import Jobs
        self.jobs=Jobs(self);self.ops.reconcile_startup()
        from .supervisor import Supervisor
        self.supervisor=Supervisor(self)
        from .qualification import catalog
        self.register('adapter.qualification_catalog',lambda actor: catalog(),read=True)
        from .integrations import Integrations
        self.external=Integrations(self)
        self.register('research.fetch',self.external.fetch)
        self.register('remote.configure',self.external.configure)
        self.register('remote.status',self.external.status,read=True)
        self.register('remote.publish',lambda actor,delivery,title,body:self.jobs.submit(actor,'remote.publish',{'delivery':delivery,'title':title,'body':body}))
        from .providers import Providers
        self.providers=Providers(self);self.rt.providers=self.providers
        self.register('provider.configure',self.providers.configure)
        self.register('provider.list',self.providers.list,read=True)
        self.register('provider.remove',self.providers.remove)
        self.routes['system.backup']=lambda actor:self.jobs.submit(actor,'ops.backup',{})
        self.routes['system.audit']=lambda actor,all_blobs=False:self.jobs.submit(actor,'ops.audit',{'all_blobs':all_blobs})
        self.routes['system.gc']=lambda actor,dry_run=True,minimum_age=86400:self.jobs.submit(actor,'ops.gc',{'dry_run':dry_run,'minimum_age':minimum_age})
        self.register('job.submit',self.jobs.submit)
        self.register('job.get',self.jobs.get,read=True)
        self.register('job.list',self.jobs.list,read=True)
        self.register('job.cancel',self.jobs.cancel)
        self.register('job.retry',self.jobs.retry)
        self.register('execution.usage',self.rt.ledger.summary,read=True)
        self.register('execution.configure_limits',self.rt.ledger.configure)
        self.register('automation.configure',self.jobs.configure)
        self.register('automation.status',self.jobs.automation_status,read=True)
        from .review_scopes import ReviewScopes
        self.scopes=ReviewScopes(self);self.p.scopes=self.scopes;self.rt.scopes=self.scopes
        self.register('program.partition_review',self.scopes.partition)
        self.register('program.review_summary',self.scopes.summary,read=True)
        from .contracts import Contracts
        from .documents import Documents
        self.contracts=Contracts(self);self.documents=Documents(self)
        for name,fn in {'contract.validate':self.contracts.check_type,'contract.compare':self.contracts.compare,'architecture.check':self.contracts.architecture,'document.import':self.documents.register,'document.get':self.documents.get,'document.attach_text':self.documents.attach_text}.items():self.register(name,fn,read=name in {'document.get','contract.validate'})
        from .standard_contracts import StandardContracts
        self.standard_contracts=StandardContracts(self)
        for name,fn in {'contract.inspect_document':self.standard_contracts.inspect,
                        'contract.read_entry':self.standard_contracts.read,
                        'contract.propose_document':self.standard_contracts.propose,
                        'contract.compare_documents':self.standard_contracts.compare}.items():
            self.register(name,fn,read=name!='contract.propose_document')
        from .schema_checks import SchemaChecks
        self.schema_checks = SchemaChecks(self)
        for name, fn in {'contract.schema_capabilities': self.schema_checks.capabilities,
                         'contract.check_schema': self.schema_checks.schema,
                         'contract.check_instance': self.schema_checks.instance}.items():
            self.register(name, fn, read=True)
        from .native import Native
        self.native = Native(self)
        for name in ('lookup','attach','input','context','actions','present_decision','respond','acknowledge','completion','stop_feedback'):
            self.register('native.'+name, getattr(self.native,name), read=name=='lookup')
        from .packets import SourcePackets
        self.packets=SourcePackets(self)
        self.register('source.partition',self.packets.partition)
        self.register('source.packet',self.packets.packet,read=True)
        self.register('source.partition_status',self.packets.status,read=True)
        from .execution_history import ExecutionHistory
        self.execution_history=ExecutionHistory(self.s,self.k,self.g,self.rt.retention)
        self.register('run.work_changes',self.execution_history.changes,read=True)
        self.register('run.work_read',self.execution_history.read_file,read=True)
        self.register('run.recovery',self.execution_history.recovery,read=True)
        self.register('run.recovery_read',self.execution_history.recovery_read,read=True)
        from .breakdowns import Breakdowns
        from .program_lifecycle import ProgramLifecycle
        self.breakdowns = Breakdowns(self)
        self.g.breakdowns = self.breakdowns
        self.lifecycle = ProgramLifecycle(self)
        from .breakdown_inputs import BreakdownInputs
        self.breakdown_inputs = BreakdownInputs(self)
        for name in ('begin','put','status','list','finalize','abandon'):
            self.register('breakdown.upload_'+name,getattr(self.breakdown_inputs,name),read=name in {'status','list'})
        self.p.breakdowns = self.breakdowns
        self.rt.breakdowns = self.breakdowns
        for name, fn, read in (
            ('breakdown.propose', self.breakdowns.propose, False),
            ('breakdown.get', self.breakdowns.get, True),
            ('breakdown.units', self.breakdowns.units, True),
            ('breakdown.packet', self.breakdowns.packet, True),
            ('breakdown.audit', self.breakdowns.audit, True),
            ('breakdown.activate', self.breakdowns.activate, False),
            ('program.breakdown_status', self.breakdowns.program_status, True),
            ('program.status', self.lifecycle.status, True),
            ('program.completion', self.lifecycle.completion, True),
            ('program.finish', self.lifecycle.finish, False),
            ('program.reopen', self.lifecycle.reopen, False),
        ): self.register(name, fn, read=read)
        from .subplans import Subplans
        self.subplans = Subplans(self)
        self.rt.subplans = self.subplans
        for name in ('propose','get','list','packet','audit','coverage','compose'):
            self.register('subplan.'+name,getattr(self.subplans,name),read=name not in {'propose','compose'})
        from .local_executions import LocalExecutions
        self.local_executions = LocalExecutions(self)
        self.rt.local_executions = self.local_executions
        for name, read in (('propose',False),('inventory',True),('get',True),('list',True),('packet',True),
                           ('audit',True),('certify',False),('withdraw',False)):
            self.register('local_execution.'+name,getattr(self.local_executions,name),read=read)
        for name, read in (('inventory',True),('get',True),('propose',False),('packet',True),('apply',False),
                           ('withdraw',False),('history',True),('policy_propose',False),('policy_get',True),
                           ('policy_apply',False),('history_detail',True)):
            self.register('execution_control.'+name,getattr(self.execution_controls,name),read=read)
        self.register('task.progress',self.execution_controls.progress,read=True)
        self.register('project.progress',self.execution_controls.project_progress,read=True)
        from .task_revisions import TaskRevisions
        self.task_revisions = TaskRevisions(self.w)
        for name, fn, read in (
            ('task.propose_revision', self.task_revisions.propose, False),
            ('task.propose_plan_revision', self.task_revisions.propose_plan_revision, False),
            ('task.revision_get', self.task_revisions.get, True),
            ('task.revision_list', self.task_revisions.list, True),
            ('task.apply_revision', self.task_revisions.apply, False),
            ('task.withdraw_revision', self.task_revisions.withdraw, False),
            ('task.revision_history', self.task_revisions.history, True),
            ('task.history_record', self.task_revisions.history_record, True),
        ): self.register(name, fn, read=read)
        from .workstreams import Workstreams
        self.workstreams = Workstreams(self)
        self.rt.workstreams = self.workstreams
        from .scope_returns import ScopeReturns
        self.scope_returns = ScopeReturns(self)
        self.rt.scope_returns = self.scope_returns
        for name in ('propose','get','list','packet','advance','apply','abandon'):
            self.register('workstream.return_'+name, getattr(self.scope_returns,name),name in {'get','list','packet'})
        for name in ('propose','get','selection','list','packet','status','activate','completion','finish','withdraw','program_audit'):
            self.register('workstream.'+name, getattr(self.workstreams,name),
                          read=name in {'get','selection','list','packet','status','completion','program_audit'})
        self.shutdown_requested = False
        self.register('system.shutdown', self.request_shutdown)
        if start_workers:self.jobs.start()

    def request_shutdown(self, actor):
        self.shutdown_requested = True
        return {'shutdown_requested': True}

    def register(self,name,function,read=False):
        need(name not in self.routes,'duplicate_route','Route already registered')
        self.routes[name]=function
        if read:self.read_routes.add(name)

    def _register_routes(self):
        routes={
          'project.create':self.k.create_project,'project.get':self.k.project,
          'source.add':self.k.source,'source.read':self.k.source_read,'source.classify':self.k.classify,'source.coverage':self.k.source_coverage,
          'artifact.propose':self.k.propose,'artifact.get':self.k.artifact,'artifact.list':self.k.list_artifacts,'artifact.revise':self.k.revise,'artifact.accept':self.k.accept,
          'trace.link':self.k.link,'trace.impact':self.k.impact,'trace.audit':self.k.trace,'baseline.create':self.k.baseline,'project.export':self.k.export,
          'repository.register':self.sn.register,'repository.list':self.repository_list,
          'program.begin':self.p.begin,'program.next':self.p.next,'program.advance':self.p.advance,
          'change.propose':self.p.change,'change.attempt':self.p.attempt,'change.delta':self.p.set_delta,'change.apply':self.p.apply_technical_change,'change.withdraw':self.p.withdraw,
          'conflict.report':self.p.conflict,'decision.propose':self.p.propose_decision,'decision.respond':self.p.respond,'decision.apply':self.p.apply_decision,
          'decision.get':self.decision_get,'decision.recent':self.i.recent_decisions,
          'task.create':self.w.create,'task.get':self.w.task,'task.list':self.task_list,'task.plan_tests':self.w.plan_tests,'task.ready':self.w.ready,
          'task.claim':self.w.claim,'task.heartbeat':self.w.heartbeat,'task.complete':self.w.complete,'task.replan':self.w.replan,'task.cancel':self.w.cancel,
          'task.artifacts_collect':self.w.artifacts_collect,
          'workflow.status':self.w.status,'workflow.pause':self.w.pause,'workflow.reconcile':self.w.reconcile,
          'gate.evaluate':self.g.evaluate_task,'policy.get':self.policy_get,'policy.propose':self.g.policy_propose,'waiver.request':self.g.waiver,'waiver.close':self.i.waiver_close,
          'dialogue.input':self.i.intake,'inbox.get':self.i.inbox,'inbox.acknowledge':self.i.acknowledge,
          'adapter.register':self.rt.adapters.register,'adapter.list':self.adapter_list,'run.get':self.rt.run_status,'evidence.get':self.evidence_get,
          'task.test_evidence':self.task_test_evidence,
          'context.build':self.ctx.task_context,'context.fresh':self.ctx.fresh,'code.search':self.idx.search,'code.consumers':self.idx.consumers,'code.read':self.idx.read,'code.inventory':self.idx.inventory,
          'delivery.configure':self.d.configure,'delivery.profile_current':self.d.profile_current,'delivery.prepare':self.d.prepare,'delivery.certify':self.d.certify,'delivery.commit':self.d.commit,'delivery.export':self.d.export_bundle,'delivery.get':self.delivery_get,
          'system.doctor':self.ops.doctor,'system.audit':self.ops.audit,'system.backup':self.ops.backup,'system.gc':self.ops.garbage_collect,
          'blob.read':self.blob_read,'api.describe':self.describe,
        }
        routes.update({
          'traceability.propose':self.traceability.propose,
          # Extraction is a durable staging job on the public route.  The
          # Traceability method remains directly callable for local controls
          # and is the job worker implementation.
          'traceability.extract':lambda actor,proposal,expected_digest=None,**options:self.jobs.submit(
              actor,'traceability.extract',{'proposal':proposal,**({'expected_digest':expected_digest} if expected_digest is not None else {}),**options}),
          'traceability.get':self.traceability.get,
          'traceability.list':self.traceability.list,
          'traceability.items':self.traceability.items,
          'traceability.read':self.traceability.read,
          'traceability.diff':self.traceability.diff,
          'traceability.decide_propose':self.traceability.decide_propose,
          'traceability.scope_propose':self.traceability.scope_propose,
          'traceability.review_subject':self.traceability.review_subject,
          'traceability.adopt':self.traceability.adopt,
          'traceability.map_propose':self.traceability.map_propose,
          'traceability.map_adopt':self.traceability.map_adopt,
          'traceability.closure_propose':self.traceability.closure_propose,
          'traceability.coverage':self.traceability.coverage,
          'traceability.closure_subject':self.traceability.closure_subject,
          'traceability.history':self.traceability.history,
          'traceability.export':self.traceability.export,
          'traceability.inspect_archive':self.traceability.inspect_archive,
          'traceability.import':self.traceability.import_archive,
          'traceability.restore':self.traceability.import_archive,
          # Assurance object/event writers remain an internal storage
          # boundary; typed pinning and review/adoption use controller-owned
          # routes with the contract checks in Assurance.
          'assurance.catalog':self.assurance.catalog,
          'assurance.pin':self.assurance.pin,
          'assurance.contains':self.assurance.contains,
          'assurance.object_get':self.assurance.object_get,
          'assurance.object_list':self.assurance.object_list,
          'assurance.refs':self.assurance.refs,
          'assurance.history':self.assurance.history,
          'assurance.resolve':self.assurance.resolve,
          'assurance.resolve_pinned':self.assurance.resolve_pinned,
          'assurance.evaluate_current':self.assurance.evaluate_current,
          'assurance.scope_propose':self.assurance.scope_propose,
          'assurance.profile_propose':self.assurance.profile_propose,
          'assurance.edge_propose':self.assurance.edge_propose,
          'assurance.set_propose':self.assurance.set_propose,
          'assurance.review_subject':self.assurance.review_subject,
          'assurance.adopt':self.assurance.adopt,
          'assurance.withdraw_propose':self.assurance.withdraw_propose,
          'assurance.report':self.assurance.report,
        })
        reads={'project.get','source.read','source.coverage','artifact.get','artifact.list','trace.impact','trace.audit','project.export','repository.list','program.next','decision.get','decision.recent','task.get','task.list','workflow.status','policy.get','adapter.list','run.get','evidence.get','context.fresh','code.search','code.consumers','code.read','code.inventory','delivery.get','delivery.profile_current','blob.read','api.describe',
               'traceability.get','traceability.list','traceability.items','traceability.read','traceability.diff','traceability.review_subject','traceability.coverage','traceability.closure_subject','traceability.history','traceability.inspect_archive',
               'assurance.catalog','assurance.contains','assurance.object_get','assurance.object_list','assurance.refs','assurance.history','assurance.resolve','assurance.resolve_pinned','assurance.evaluate_current','assurance.review_subject','assurance.report'}
        reads.update({'task.test_evidence'})
        for name,fn in routes.items():self.register(name,fn,read=name in reads)

    def invoke(self,actor,method,params):
        need(method in self.routes,'unknown_method','Method is not exported',method)
        need(isinstance(params,dict),'invalid_params','Expected a JSON object')
        need(not {'actor','requester','security','store'} & params.keys(),'forbidden','Transport identity cannot be supplied in method arguments')
        fn=self.routes[method]
        try:inspect.signature(fn).bind(actor,**params)
        except TypeError as exc:raise Fault('invalid_params','Arguments do not match the method contract',str(exc)) from exc
        return fn(actor,**params)

    def _request_delivery_commit(self, actor, request, request_digest):
        """Run Delivery.commit across its durable observation boundaries.

        Ordinary mutating requests retain the single outer transaction below.
        Delivery.commit is the bounded exception because it performs a local
        Git side effect and must commit the resulting body.git/outbox fact
        before its later material and final-gate phases can fail.  Keep the
        process-wide Store lock for the complete lifecycle so two requests
        cannot race the same Git intent, while each Delivery phase commits its
        own database transaction.

        A pending request row is an internal retry marker.  It is written
        before the first phase and replaced with the normal result only after
        Delivery.commit succeeds.  Exact same-request retries resume; a
        different payload under the same id remains an idempotency conflict.
        This preserves request replay without making a failed observation
        permanently un-retryable after a process restart.
        """
        pending_format='daikibo.request-pending.v1'
        with self.s.lock:
            with self.s.transaction():
                previous=self.s.one('SELECT request_digest,result FROM requests WHERE actor=? AND id=?',(actor.id,request['id']))
                if previous:
                    need(previous['request_digest']==request_digest,
                         'idempotency_conflict','Request ID was reused for different content')
                    previous_result=parse_json(previous['result'])
                    if not (isinstance(previous_result,dict) and previous_result.get('format')==pending_format):
                        return previous_result
                else:
                    marker={'format':pending_format,'method':'delivery.commit',
                            'request_digest':request_digest,'last_error':None}
                    self.s.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                                   (actor.id,request['id'],request_digest,canonical(marker).decode(),timestamp()))
            try:
                result=self.invoke(actor,request['method'],request['params'])
            except Fault as exc:
                marker={'format':pending_format,'method':'delivery.commit',
                        'request_digest':request_digest,'last_error':exc.as_dict()}
                with self.s.transaction():
                    self.s.execute('UPDATE requests SET result=?,created=? WHERE actor=? AND id=? AND request_digest=?',
                                   (canonical(marker).decode(),timestamp(),actor.id,request['id'],request_digest))
                raise
            with self.s.transaction():
                self.s.execute('UPDATE requests SET result=?,created=? WHERE actor=? AND id=? AND request_digest=?',
                               (canonical(result).decode(),timestamp(),actor.id,request['id'],request_digest))
            return result

    def request(self,token,request):
        obj(request,required=('id','method','params'))
        text(request['id'],'request id',128);text(request['method'],'method',100)
        actor=self.sec.authenticate(token);method=request['method'];h=digest(request)
        if method in self.read_routes:return self.invoke(actor,method,request['params'])
        if method=='delivery.commit':
            return self._request_delivery_commit(actor,request,h)
        # Mutating control commands and their idempotency result commit atomically.
        # Long execution only enters via a durable job; no process is started here.
        with self.s.transaction():
            previous=self.s.one('SELECT request_digest,result FROM requests WHERE actor=? AND id=?',(actor.id,request['id']))
            if previous:
                need(previous['request_digest']==h,'idempotency_conflict','Request ID was reused for different content')
                return parse_json(previous['result'])
            result=self.invoke(actor,method,request['params'])
            self.s.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(actor.id,request['id'],h,canonical(result).decode(),timestamp()))
            return result

    def repository_list(self,actor,project):
        self.k.project(actor,project)
        rows=self.s.all('SELECT id,name,head FROM repos WHERE project=? ORDER BY name',(project,))
        return {'repositories':rows}
    def task_list(self,actor,project,limit=100,offset=0):
        self.k.project(actor,project);need(type(limit) is int and 1<=limit<=1000 and type(offset) is int and offset>=0,'invalid_range','Invalid pagination')
        rows=self.s.all('SELECT id,title FROM (SELECT id,json_extract(body,\'$.title\') AS title FROM tasks WHERE project=? ORDER BY created) LIMIT ? OFFSET ?',(project,limit+1,offset))
        return {'tasks':rows[:limit],'next_offset':offset+limit if len(rows)>limit else None}
    def decision_get(self,actor,decision):
        row=self.s.one('SELECT * FROM decisions WHERE id=?',(decision,),True);self.k.project(actor,row['project']);row['body']=parse_json(row['body']);return row
    def policy_get(self,actor,project):self.k.project(actor,project);return self.g.policy(project)
    def adapter_list(self,actor):
        actor.require('owner','agent','observer')
        return {'adapters':[{'name':r['name'],'qualified':bool(r['qualified']),**{k:v for k,v in parse_json(r['body']).items() if k in {'kind','version','simulated','model'}}} for r in self.s.all('SELECT * FROM adapters')]}
    def evidence_get(self,actor,evidence):
        result=self.g.receipt(evidence);self.k.project(actor,result['project']);return result
    def task_test_evidence(self,actor,task,offset=0,limit=100,expected_selection_digest=None):
        """Read the current formal test selection without changing state."""
        row=self.s.one('SELECT * FROM tasks WHERE id=?',(task,),True)
        self.k.project(actor,row['project'])
        binding=self.g.task_binding(task,ensure_policy=False)
        snapshot_digest=None
        if row['candidate']:
            candidate=self.s.one('SELECT body FROM candidates WHERE id=?',(row['candidate'],),True)
            try:
                snapshot=parse_json(candidate['body']).get('snapshot')
                snapshot_digest=snapshot.get('digest') if isinstance(snapshot,dict) else None
            except Fault:
                snapshot_digest=None
        else:
            body=parse_json(row['body'])
            if body.get('repos'):
                snapshot_digest=self.sn.capture(actor,row['project'],body['repos'],store_blobs=False)['digest']
        return self.g.task_test_evidence(actor,task,binding=binding,snapshot_digest=snapshot_digest,
                                         offset=offset,limit=limit,
                                         expected_selection_digest=expected_selection_digest)
    def delivery_get(self,actor,delivery):
        row=self.s.one('SELECT * FROM deliveries WHERE id=?',(delivery,),True);self.k.project(actor,row['project']);row['body']=parse_json(row['body']);return row
    def blob_read(self,actor,blob,project=None,offset=0,limit=65536):
        need(type(offset) is int and offset>=0 and type(limit) is int and 1<=limit<=1048576,'invalid_range','Invalid blob range')
        if actor.role!='owner':
            self.k.project(actor,project)
            # Do not expose backups/credentials by guessing a content hash.
            allowed=self.s.one('SELECT id FROM sources WHERE project=? AND blob=?',(project,blob)) or self.s.one("SELECT id FROM documents WHERE project=? AND json_extract(body,'$.raw_digest')=?",(project,blob))
            if not allowed:
                allowed=self.s.one("SELECT id FROM receipts WHERE project=? AND (json_extract(body,'$.stdout_blob')=? OR json_extract(body,'$.stderr_blob')=? OR json_extract(body,'$.result.report_blob')=?)",(project,blob,blob,blob))
            need(allowed,'forbidden','Blob is not a public source or an observed redacted log in this project')
        # Authorization above is intentionally complete before opening or
        # looking up an artifact session.  The one verified handle serves both
        # the total and range, so a recipe cannot pass size validation and then
        # be read through a second weaker path.
        from .backup_artifacts import open_artifact_session
        with open_artifact_session(self.s,blob) as session:
            total=session.size
            end=min(total,offset+limit)
            data=session.read_range(offset,end-offset) if offset<total else session.read_range(offset,0)
        return {'sha256':blob,'base64':base64.b64encode(data).decode(),'total_bytes':total,'next_offset':end if end<total else None}
    def describe(self,actor,method=None):
        actor.require('owner','agent','worker','reviewer','observer')
        methods={}
        for name,fn in sorted(self.routes.items()):
            if method is not None and name != method:
                continue
            descriptor={'signature':str(inspect.signature(fn)),'read_only':name in self.read_routes}
            if name in {'artifact.propose','artifact.revise'}:
                descriptor['body_contract']=artifact_body_contract()
                descriptor['description'] = (
                    'Canonical artifact body validation. For domain, design, component and interface artifacts, '
                    'the additive structural_obligations declaration is closed and exact; absent and explicit empty '
                    'declarations remain distinct, while explicit null is rejected. Domain responsibility pins are resolved against retained history.'
                )
            elif name == 'task.propose_plan_revision':
                descriptor['description'] = (
                    'Propose an immutable replacement for the complete frozen test plan while retaining the '
                    'canonical Task definition, old candidates, attempts, and failed observations. The proposal '
                    'must identify the expected Task revision and current plan digest and include exact receipt '
                    'evidence; impact review and the ordinary apply route are required before the plan is frozen.'
                )
                descriptor['body_contract'] = {
                    'required': ['task', 'expected_revision', 'expected_plan_digest', 'body', 'reason', 'evidence_refs'],
                    'body': 'Complete test plan accepted by task.plan_tests validation',
                    'evidence_refs': 'Saved receipt IDs for the exact current Task; old failures remain valid evidence',
                    'result': ['id', 'digest', 'binding', 'status', 'required_review_role', 'plan_digest'],
                    'apply_result': {'new_test_plan_required': False, 'plan_revised': True},
                }
            elif name in {'task.create','task.propose_revision','task.replan'}:
                from .assurance_additive import task_definition_contract
                descriptor['description'] = (
                    'Task definition validation preserves the existing fields and optionally accepts explicit '
                    'required_outputs and required_exercises. Declarations are semantic Task identity, must point '
                    'to actual read_artifacts, and are never inferred from write_paths or future candidates. Explicit null is rejected.'
                )
                descriptor['body_contract'] = task_definition_contract()
            elif name=='local_execution.propose':
                descriptor['body_contract']={
                    'required':['program','subplan','tasks','rationale','stage_evidence','dispositions'],
                    'optional':['byte_budget','request_id'],
                    'stage_keys':['requirements','scenarios','boundaries','contracts','feasibility','design','plan'],
                    'disposition_classes':['required_resolved','independent','unresolved'],
                    'result':['id','digest','material_digest','packet_manifest','current','next_operation'],
                }
            elif name=='local_execution.inventory':
                descriptor['body_contract']={'required':['program','subplan','tasks'],
                                             'optional':['offset','limit'],
                                             'result':['items','total','next_offset','material_digest'],
                                             'note':'Read-only exact inventory IDs/digests for disposition construction'}
            elif name in {'local_execution.certify','local_execution.withdraw'}:
                descriptor['body_contract']={'expected_digest':'Proposal digest required for optimistic concurrency',
                                             'request_id':'Optional idempotency key','result':'Append-only local execution record'}
            elif name == 'task.artifacts_collect':
                descriptor['description'] = (
                    'Collects only a controller-sealed artifact output manifest from the current candidate. '
                    'Run, receipt, epoch, producer actor, snapshot bytes, and artifact body are resolved by the '
                    'controller; newly collected Knowledge artifacts remain draft until the ordinary meaning review '
                    'and acceptance workflow. Replaying the same collection key is idempotent.'
                )
                descriptor['body_contract'] = {
                    'required': ['task', 'expected_revision', 'candidate', 'repository', 'path'],
                    'caller_selectors_only': True,
                    'manifest': {'format': 'daikibo.artifact-output.v1',
                                 'item': ['declaration_id', 'kind', 'body'],
                                 'max_packet_bytes': 1048576, 'max_outputs_per_packet': 200},
                    'producer': ['producer_actor', 'producer_epoch', 'implementation_run',
                                 'implementation_receipt'],
                    'artifact_status': 'draft',
                    'meaning_review': 'separate acceptance/review operation',
                }
            elif name == 'task.claim':
                descriptor['description'] = (
                    'Claims one eligible Task after the existing admission, readiness, dependency, and resource '
                    'checks. If no candidate can be claimed, the no_work Fault keeps a list-shaped details value '
                    'with candidate-scoped stage and failure codes from the checks actually performed. Details are '
                    'diagnostic evidence only and never grant claim, recovery, replan, or execution authority.'
                )
                descriptor['body_contract'] = {
                    'result': 'The claimed Task row, unchanged from the normal claim route',
                    'no_work': {
                        'code': 'no_work',
                        'details': [{'task': 'authorized candidate ID', 'stage': 'check stage',
                                     'failures': ['canonical failure code']}],
                        'stages': ['task_state', 'execution_admission', 'ready_gate',
                                   'local_authorization', 'dependency', 'resource_conflict'],
                        'automatic_selection': (
                            'Only the up-to-100 ready/current candidates examined by the existing scheduler are '
                            'reported; this is not a complete project inventory.'
                        ),
                    },
                }
            elif name == 'task.progress':
                descriptor['description'] = (
                    'Read-only counters, existing admission payload, and additive reporting of the current claim, '
                    'latest observed attempt, completion-review outcomes, and next-claim explanation. '
                    'The admission field describes prior epochs of the current task epoch and is not a complete claim authorization.'
                )
                descriptor['body_contract'] = {
                    'preserved': ['task', 'project', 'revision', 'epoch', 'status', 'validity', 'attempts',
                                  'no_progress_count', 'threshold', 'telemetry', 'admission', 'history_snapshot'],
                    'additive': {'reporting': 'daikibo.task-progress.v1',
                                 'snapshot': 'SHA-256 of reporting facts excluding reporting.snapshot',
                                 'writes': False},
                }
            elif name == 'project.progress':
                descriptor['description'] = (
                    'Read-only paginated task progress. Each item preserves its existing counters/admission payload '
                    'and adds reporting for observed claim and attempt evidence; the page snapshot includes each '
                    'reporting snapshot so a new review invalidates pagination even when the tasks row is unchanged.'
                )
                descriptor['body_contract'] = {
                    'preserved_item_fields': ['id', 'revision', 'epoch', 'status', 'validity', 'attempts',
                                              'no_progress_count', 'threshold', 'admission', 'telemetry'],
                    'additive_item_field': 'reporting: daikibo.task-progress.v1',
                                 'snapshot': 'SHA-256 of the compact project evidence-state stream; detailed bodies are page-only',
                                 'writes': False,
                }
            elif name == 'task.test_evidence':
                descriptor['description'] = (
                    'Read-only selection of the current Task test-plan checks and their newest suitable formal '
                    'receipts. Failed, invalid, stale and unobserved checks remain distinct; an older PASS is never '
                    'used to hide a newer observation. The selection digest is the freshness token for reviewer use.'
                )
                descriptor['body_contract'] = {
                    'required': ['task'],
                    'optional': ['offset', 'limit', 'expected_selection_digest'],
                    'result': ['task', 'task_revision', 'epoch', 'task_binding', 'candidate_identity',
                               'snapshot_digest', 'plan_digest', 'checks', 'total', 'next_offset',
                               'selection_digest', 'selection_current', 'history'],
                    'check': ['check_id', 'check_digest', 'status', 'selected_receipt', 'run',
                              'receipt_digest', 'observed_summary', 'history_read_ref', 'reason'],
                    'statuses': ['unobserved', 'executed', 'failed', 'invalid', 'unknown'],
                    'bounded_reads': ['evidence.get', 'blob.read'],
                    'judgment_valid': 'Not required for normal test receipts',
                    'writes': False,
                }
            elif name == 'execution_control.history_detail':
                descriptor['description'] = (
                    'Read-only bounded raw run/receipt identity pages for a selected task epoch. '
                    'The reader works for ambiguous legacy evidence and never reconciles or authorizes it.'
                )
                descriptor['body_contract'] = {
                    'kinds': ['implementer_runs', 'implementer_receipts'],
                    'result': ['records', 'total', 'next_offset', 'snapshot'],
                    'stale_error': 'stale_history_detail',
                    'writes': False,
                }
            elif name == 'run.work_changes':
                descriptor['description'] = (
                    'Project-scoped read-only bounded pages of the recorded working-product change list for this run. '
                    'The snapshot and receipt are scoped to the run; inspection does not adopt files, rerun tests, '
                    'approve a candidate, or complete a task.'
                )
                descriptor['body_contract'] = {
                    'required': ['run'],
                    'optional': ['offset', 'limit'],
                    'result': ['run', 'receipt', 'snapshot_blob', 'changes_blob', 'snapshot_digest',
                               'changes', 'total_changes', 'next_offset', 'candidate',
                               'accepted_completion', 'notice'],
                    'change_entry': {
                        'selector': ['repo', 'path'],
                        'file_digest': 'after.blob when after.kind is file',
                        'deletion': 'after is null and is not a work_read target',
                    },
                    'pagination': 'offset/limit with a bounded page and exact total',
                    'adoptable': False,
                    'writes': False,
                }
            elif name == 'run.work_read':
                descriptor['description'] = (
                    'Project-scoped read-only digest-bound streaming of one regular file from the run working product. '
                    'The selected run, receipt, repository, path, and expected SHA are checked before each bounded page; '
                    'the base64 field is transport only and the returned bytes are never adopted.'
                )
                descriptor['body_contract'] = {
                    'required': ['run', 'repo', 'path', 'expected_digest'],
                    'optional': ['offset', 'limit'],
                    'result': ['run', 'receipt', 'repo', 'path', 'base64', 'sha256',
                               'total_bytes', 'next_offset', 'adopted_by_read'],
                    'pagination': 'offset/limit; decode all pages and verify total size and expected SHA',
                    'regular_files_only': True,
                    'adopted_by_read': False,
                    'writes': False,
                }
            elif name == 'run.recovery':
                descriptor['description'] = (
                    'Task-scoped read-only bounded pages of a failed collector artifact manifest. The evidence is '
                    'scoped to the recorded run and its retained Task context; it remains diagnostic and cannot adopt '
                    'a candidate or alter the receipt.'
                )
                descriptor['body_contract'] = {
                    'format': 'failed-artifacts.v1',
                    'result': ['entries', 'total', 'next_offset', 'manifest_digest', 'status'],
                    'pagination': 'offset/limit with an exact total and stale manifest digest check',
                    'adoptable': False,
                    'writes': False,
                }
            elif name == 'run.recovery_read':
                descriptor['description'] = (
                    'Task-scoped read-only digest-bound streaming of one stored regular file from a failed collector manifest. '
                    'The manifest digest, entry SHA, run, repository, path, and bounded page remain fixed. '
                    'Symlinks and unsafe metadata entries cannot be dereferenced; reading does not adopt or approve the failure.'
                )
                descriptor['body_contract'] = {
                    'required': ['run', 'repo', 'path', 'expected_digest'],
                    'optional': ['offset', 'limit', 'expected_manifest'],
                    'result': ['base64', 'total_bytes', 'next_offset', 'sha256'],
                    'adopted_by_read': False,
                    'writes': False,
                }
            elif name.startswith('traceability.'):
                descriptor['description'] = (
                    'Immutable traceability populations with bounded reads plus Unit B typed decisions, mappings, '
                    'scope bindings and actual review-gated closure adoption.'
                )
                descriptor['body_contract'] = {
                    'pagination': {'default_limit': 100, 'maximum': 500, 'encoded_bytes_maximum': 1048576,
                                   'cursor': 'opaque revision/query/snapshot/last-key token',
                                   'stale': 'stale_cursor with restart=True'},
                    'next_actions': ['traceability.extract', 'traceability.review_subject', 'traceability.coverage'],
                    'extract': 'traceability.extract queues a durable job; poll job.get and retry a staging proposal after worker restart',
                    'unit_a': False,
                    'adoption': 'Requires persisted packets, independently observed readonly PASS receipts, and CAS head checks',
                }
            elif name.startswith('assurance.'):
                descriptor['description'] = (
                    'E1 immutable edge-assurance identity and history reads. The public surface resolves pinned '
                    'typed references and exposes bounded storage state; review, adoption, and completion remain E2/E3.'
                )
                descriptor['body_contract'] = {
                    'registry_digest': self.assurance.catalog(actor)['registry_digest'],
                    'pagination': {'maximum': 500, 'bounded': True},
                    'writes': name != 'assurance.catalog' and name != 'assurance.contains' and name != 'assurance.object_get' and name != 'assurance.object_list' and name != 'assurance.refs' and name != 'assurance.history' and name != 'assurance.resolve' and name != 'assurance.resolve_pinned' and name != 'assurance.evaluate_current',
                    'adoption': 'Reserved for the E2 review contract',
                }
                if name == 'assurance.profile_propose':
                    descriptor['description'] = (
                        'Propose an immutable assurance.profile.v2, assurance.profile.v3, assurance.profile.v4 or assurance.profile.v5 for one real program. '
                        'The proposal is bound to the stable profile:program:<program> head and never activates '
                        'an E3 stage gate by itself. Profile.v3 must pin the output-aware relation registry. '
                        'Legacy profile.v1 objects remain historical migration_pending records.'
                    )
                    descriptor['body_contract'] = {
                        # Historical clients read this singular v2 marker;
                        # the explicit formats field advertises v3 without
                        # changing that compatibility projection.
                        'format': 'assurance.profile.v2',
                        'formats': ['assurance.profile.v2', 'assurance.profile.v3', 'assurance.profile.v4', 'assurance.profile.v5'],
                        'v3_required_relation_contract_digest': 'REGISTRY_V2_DIGEST',
                        'v4_required_relation_contract_digest': 'REGISTRY_V2_DIGEST',
                        'v5_required_relation_contract_digest': 'REGISTRY_V2_DIGEST',
                        'v5_required_scope_contract': 'assurance.scope.v2',
                        'v5_required_node_contract': 'assurance.node-contract.v2',
                        'v5_additional_fields': ['required_scope_contract','required_node_contract'],
                        'v4_selector_constraints': {'realization_sources': {
                            'stages': ['plan', 'task'], 'relation': 'realizes',
                            'direction': 'outgoing', 'artifact_kinds': ['component', 'design', 'interface'],
                        }},
                        'required': [
                            'project', 'program', 'scope_ref', 'obligations_ref',
                            'previous_selection_ref', 'application_mode', 'stage_rules',
                            'node_review_rules', 'relation_selectors',
                            'test_definition_bindings', 'change_reason', 'authority_refs',
                        ],
                        'stages': {
                            'plan': {'denominator': 'program_plan', 'execution_results': 'none'},
                            'task': {'denominator': 'assigned_task_contributors', 'execution_results': 'assigned_checks'},
                            'integration': {'denominator': 'program_integration', 'execution_results': 'integration_checks'},
                            'delivery': {'denominator': 'actual_delivery', 'execution_results': 'certified_integration_and_actual_outputs'},
                        },
                        'application_modes': ['mandatory', 'disabled'],
                        'selection': 'expected_head CAS; previous_selection_ref is required after bootstrap',
                        'authority_families': {
                            'source': '{kind:source,project,source,blob_digest}',
                            'change': '{kind:change,project,change,revision,body_digest,pin}; retained change body/material and target/source impact are required',
                            'decision': 'artifact ref whose canonical artifacts.kind is decision; accepted source/target linkage is required',
                            'unsupported': 'scope/profile/obligations and arbitrary artifact kinds are rejected',
                        },
                        'event_predecessor': 'event.previous, event.expected_head, event.selection.previous_selection_ref, and profile.previous_selection_ref must be the same exact prior profile or all null',
                        'stage_evaluator': False,
                        'canonical_head': 'v2 and v3 share profile:program:<program>; replacements require exact previous_selection_ref and authority',
                    }
                elif name == 'assurance.report':
                    descriptor['description'] = (
                        'Read-only bounded assurance object reporting. When stage is supplied, the same Unit 3 '
                        'stage evaluator resolves the canonical profile, controller denominator, node reviews, '
                        'relation capability, execution observations and existing Unit B gate without writing '
                        'gates, receipts, packets, heads, candidates or materials.'
                    )
                    descriptor['body_contract'] = {
                        'optional': ['program', 'stage', 'checkpoint', 'task', 'delivery',
                                     'proposed_breakdown', 'local_execution', 'cursor', 'limit'],
                        'stage_checkpoints': {
                            'plan': ['plan'],
                            'task': ['ready', 'claim', 'execute', 'candidate', 'complete', 'recheck'],
                            'integration': ['certify', 'commit_pre'],
                            'delivery': ['finalize', 'finish', 'export'],
                        },
                        'result': ['selection', 'membership', 'global_denominator',
                                   'local_denominator', 'nodes', 'relations', 'execution',
                                   'unit_b', 'deferred_future', 'semantic_fingerprint',
                                   'report_snapshot', 'strong_complete'],
                        'read_only': True,
                        'system_enforcement': False,
                        'pending': ['consumer_mr', 'consumer_c', 'unit4_unit5_entrypoints'],
                    }
            methods[name]=descriptor
        return {'protocol':'daikibo.rpc.v1','methods':methods,
                'identity':'Local cooperative user; roles describe workflow responsibilities, not authenticated OS identities.'}

    def close(self):
        if self.closed:return
        self.closed=True;self.jobs.stop();self.rt.shutdown();self.providers.close();self.idx.close();self.s.close()
