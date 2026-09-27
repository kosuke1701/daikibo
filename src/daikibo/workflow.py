"""D04 — durable task queue, fencing, bounded retries, timers and transitions."""
from __future__ import annotations
import fnmatch
from .common import Actor, Fault, canonical, digest, finite_duration, need, number, obj, parse_json, relative_path, strings, text, timestamp, uid
from .assurance_additive import validate_task_structural_obligations


def overlaps(left, right):
    # Globs are conservatively reduced to their literal prefix for contention checks.
    def prefix(x):
        return x.split('*',1)[0].split('?',1)[0].split('[',1)[0].rstrip('/')
    for a in left:
        for b in right:
            a,b=prefix(a),prefix(b)
            if not a or not b or a==b or a.startswith(b+'/') or b.startswith(a+'/'): return True
    return False

class Workflow:
    def __init__(self,store,security,knowledge,governance,execution_controls=None):
        self.s,self.sec,self.k,self.g=store,security,knowledge,governance
        self.control=None
        self.execution_controls=execution_controls
        # Control/Runtime binds the existing immutable verification-material
        # coordinator after composition.  Keeping this private avoids adding
        # a public plan_tests argument or a second material writer.
        self.verification_materials=None
        governance.workflow=self

    def task(self,actor,task):
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,),True)
        self.k.project(actor,row['project'])
        row['body']=parse_json(row['body'])
        row['blocks']=self.s.all("SELECT kind,ref,reason FROM blocks WHERE task=?",(task,))
        return row

    def validate_definition(self,actor,project,body):
        self.k.project(actor,project); actor.require('owner','agent',project=project)
        obj(body,required=('title','goal','read_artifacts','write_paths','acceptance','dependencies','repos','non_goals'),
            optional=('risk','resource_writes','resource_reads','max_attempts','timeout','auxiliary_criteria','maintenance_reason','phase','workflow_id','origin_change','acceptance_refs','structural_obligations'))
        text(body['title'],'title',500); text(body['goal'],'goal',30000)
        for field in ('read_artifacts','write_paths','acceptance','dependencies','repos','non_goals'):
            strings(body[field],field,nonempty=field in {'read_artifacts','acceptance'})
        for path in body['write_paths']: relative_path(path)
        risk=body.get('risk','standard')
        need(risk in {'lite','standard','critical'},'invalid_risk','Unknown risk profile')
        sensitive=False
        for ref in body['read_artifacts']:
            art=self.k.artifact(actor,ref)
            need(art['project']==project,'cross_project','Task reference belongs to another project')
            if art['kind']=='interface' or art['body'].get('critical') or art['body'].get('security_relevant'):
                sensitive=True
        if 'structural_obligations' in body:
            declarations = validate_task_structural_obligations(
                body['structural_obligations'], project=project, read_artifacts=body['read_artifacts'],
            )
            # Shape and Task membership are checked above.  Resolve every
            # historical artifact pin against controller material as well, so
            # a caller cannot self-assert a revision or digest.
            for collection in ('required_outputs', 'required_exercises'):
                for item in declarations[collection]:
                    for ref in item['artifact_refs']:
                        pinned = self.k.artifact(actor, ref['artifact'], ref['revision'])
                        need(pinned['project']==project, 'cross_project', 'Structural Task reference belongs elsewhere')
                        need(pinned['digest']==ref['body_digest'], 'stale_reference', 'Structural Task artifact pin digest differs')
        if any(any(word in p.lower() for word in ('auth','security','payment','migration','policy','credential')) for p in body['write_paths']): sensitive=True
        body={**body,'risk':'critical' if sensitive else risk}
        if body.get('phase') in {'requirements','scenario','boundary','contract','feasibility','plan'}:
            # Engineering workflow tasks still require actual observations; no fabricated execution.
            need(all(p.startswith('.daikibo-research/') for p in body['write_paths']),'analysis_scope_violation','Experiments may only write .daikibo-research/. Product changes require a production task and measured tests')
            body['task_kind']='analysis'
        else: body['task_kind']='production'
        for field in ('read_artifacts','write_paths','acceptance','dependencies','repos'):
            need(len(body[field])==len(set(body[field])), 'duplicate_task_reference', 'Duplicate task field: '+field)
        for field in ('resource_writes','resource_reads','auxiliary_criteria'):
            if field in body: strings(body[field],field)
        if 'max_attempts' in body:
            need(type(body['max_attempts']) is int and 1<=body['max_attempts']<=1000, 'invalid_attempts', 'Attempts must be 1..1000')
        if 'timeout' in body: finite_duration(body['timeout'],'timeout')
        for repo in body['repos']: self.s.one('SELECT id FROM repos WHERE id=? AND project=?',(repo,project),True)
        for dep in body['dependencies']:
            row=self.s.one('SELECT project,status FROM tasks WHERE id=?',(dep,),True)
            need(row['project']==project,'cross_project','Dependency belongs elsewhere')
            need(row['status']!='cancelled','cancelled_dependency','Cannot depend on cancelled work')
        from .obligations import from_store
        from_store(self.s,body)
        return body

    def create(self,actor,project,body):
        # Validation and input capture share the same local transaction.
        with self.s.transaction():
            body=self.validate_definition(actor,project,body)
            return self._create_validated(actor,project,body)

    def _create_validated(self,actor,project,body):
        with self.s.transaction():
            ident,now=uid('TASK'),timestamp()
            for repo in body['repos']: self.s.one("SELECT id FROM repos WHERE id=? AND project=?",(repo,project),True)
            self.s.execute("INSERT INTO tasks(id,project,body,status,validity,created,updated) VALUES(?,?,?,'planned','current',?,?)",(ident,project,canonical(body).decode(),now,now))
            for ref in body['read_artifacts']:
                art=self.k.artifact(actor,ref)
                self.s.execute("INSERT INTO task_reads VALUES(?,?,?,?)",(ident,ref,art['revision'],art['digest']))
            for dep in body['dependencies']:
                self.s.one("SELECT id FROM tasks WHERE id=? AND project=?",(dep,project),True)
                self.s.execute("INSERT INTO task_deps VALUES(?,?)",(ident,dep))
            self.sec.event(project,'task_planned',actor.id,{'id':ident,'risk':body['risk'],'reads':body['read_artifacts']})
        return self.task(actor,ident)

    def validate_test_plan(self,actor,task,body,current=None):
        """Validate a complete frozen test plan without writing anything.

        Plan-only revision proposals deliberately share this validator with
        the ordinary planning route so they cannot introduce a weaker plan
        admission path.
        """
        row=current or self.task(actor,task)
        actor.require('owner','agent',project=row['project'])
        obj(body,required=('checks',),optional=('test_sources','negative_cases','rationale'))
        checks=body['checks']
        need(isinstance(checks,list) and 0<len(checks)<=100,'invalid_test_plan','At least one required check is needed')
        ids=set()
        for check in checks:
            obj(check,required=('id','argv','kind'),optional=('report','required_tests','timeout','env','purpose'))
            text(check['id'],'check id',100); strings(check['argv'],'argv',maximum=200,nonempty=True)
            need(check['id'] not in ids,'invalid_test_plan','Duplicate check id'); ids.add(check['id'])
            need(check['kind'] in {'pytest','junit','command'},'invalid_test_plan','Unknown check runner')
            if check['kind'] in {'pytest','junit'}:
                relative_path(check.get('report','results.xml'))
                strings(check.get('required_tests',[]),'required_tests')
            if check['kind']=='command':
                text(check.get('purpose'),'command purpose',3000)
            finite_duration(check.get('timeout',300),'timeout')
            if 'env' in check:
                need(isinstance(check['env'],dict),'invalid_test_plan','Environment must be an object')
                need(all(isinstance(k,str) and isinstance(v,str) and k not in {'LD_PRELOAD','LD_LIBRARY_PATH','PYTHONPATH','PYTHONHOME','BASH_ENV','ENV'} and not k.startswith('DAIKIBO_') for k,v in check['env'].items()),'unsafe_environment','Protected environment override')
        if row['body']['task_kind']=='production':
            need(any(c['kind'] in {'pytest','junit'} for c in checks),'test_inventory_required','Production tasks need a measured test inventory, not only exit status')
        return body,ids

    def plan_tests(self,actor,task,body,review_receipt=None):
        row=self.task(actor,task)
        actor.require('owner','agent',project=row['project'])
        body,ids=self.validate_test_plan(actor,task,body,current=row)
        with self.s.transaction():
            # The first read above validates the caller's shape.  Re-read the
            # authoritative Task inside the writer transaction so a concurrent
            # revision/state change cannot receive a plan for an old row.  All
            # Task-dependent requirements, including the measured production
            # inventory, are enforced from this current canonical row.
            current=self.task(actor,task)
            actor.require('owner','agent',project=current['project'])
            need(current['status'] in {'planned','ready'},'invalid_state','Cannot replace test plan during/after execution')
            body,ids=self.validate_test_plan(actor,task,body,current=current)
            if actor.role!='owner':
                need(review_receipt,'review_required','Independent test plan approval required')
                self.g.require_review(review_receipt,task,digest(body),{'test_plan'})
            self.s.execute("INSERT INTO plans VALUES(?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET body=excluded.body,digest=excluded.digest,approved=excluded.approved,created=excluded.created",
                           (task,canonical(body).decode(),digest(body),actor.id if actor.role=='owner' else review_receipt,timestamp()))
            # Pin the saved Task revision and saved plan body through the
            # existing controller-owned material boundary before publishing
            # the freeze event.  Any pin fault rolls back both plan and event;
            # the CAS orphan, if one was written before the DB rollback, stays
            # subject to the existing material/blob GC rules.
            saved_task=self.task(actor,task)
            saved_plan=self.s.one("SELECT * FROM plans WHERE task=?",(task,),True)
            need(saved_plan is not None,'integrity_error','Saved test plan disappeared before definition pin')
            coordinator=self.verification_materials
            need(coordinator is not None and callable(getattr(coordinator,'pin_test_plan',None)),
                 'verification_material_unavailable','Workflow is not connected to immutable verification materials')
            coordinator.pin_test_plan(
                actor, saved_task['project'], saved_task, saved_plan,
                captured_from={'controller':'runtime','operation':'task.plan_tests'},
            )
            self.sec.event(saved_task['project'],'test_plan_frozen',actor.id,{'task':task,'digest':saved_plan['digest'],'checks':sorted(ids)})
        return {'task':task,'digest':digest(body)}

    def ready(self,actor,task):
        with self.s.transaction():
            gate=self.g.evaluate_task(actor,task,'ready')
            need(gate['verdict']=='pass','gate_denied','Task is not ready',gate)
            self.s.execute("UPDATE tasks SET status='ready',updated=? WHERE id=?",(timestamp(),task))
            self.sec.event(self.task(actor,task)['project'],'task_ready',actor.id,{'task':task,'gate':gate['id']})
        return self.task(actor,task)

    def claim(self,actor,project,task=None):
        actor.require('owner','agent','worker',project=project,task=task if actor.task else None)
        with self.s.transaction():
            project_row=self.k.project(actor,project)
            need(not project_row['paused'],'paused','Project is paused')
            if task:
                candidates=[self.task(actor,task)]
                need(candidates[0]['project']==project,'cross_project','Task belongs elsewhere')
            else:
                need(actor.task is None,'forbidden','Task capability must name its own task')
                candidates=[self.task(actor,r['id']) for r in self.s.all("SELECT id FROM tasks WHERE project=? AND status='ready' AND validity='current' AND paused=0 ORDER BY created LIMIT 100",(project,))]
            policy=self.g.policy(project)['body']
            running=self.s.all("SELECT id,body FROM tasks WHERE project=? AND status='running' AND lease_until>?",(project,timestamp()))
            need(len(running)<policy['max_parallel'],'capacity','Parallelism limit reached')
            diagnostics=[]
            for row in candidates:
                if row['paused']:
                    diagnostics.append({'task':row['id'],'stage':'task_state','failures':['task_paused']})
                    continue
                if row['status']!='ready':
                    diagnostics.append({'task':row['id'],'stage':'task_state','failures':['task_not_ready']})
                    continue
                if self.execution_controls is not None:
                    admission = self.execution_controls.admission(actor,row['id'])
                else:
                    # Preserve the existing standalone currentness check as
                    # the diagnostic source without adding a second gate.
                    current_failures=self.g.check_current(row['id'])
                    admission={'allowed': not bool(current_failures),
                               'failures': current_failures}
                if not admission['allowed']:
                    diagnostics.append({'task':row['id'],'stage':'execution_admission',
                                        'failures':admission.get('failures',[])})
                    continue
                # Unit4-R is the last readonly admission projection before
                # this transaction mutates epoch, lease, local claim, and
                # execution-control records.  A rejected candidate remains
                # diagnostic only; no claim-side mutation is attempted.
                from .unit4_enforcement import (
                    inspect_task_admission,
                    task_admission_local_claim_binding,
                )
                # The claim writer must consume the same readonly admission
                # projection whether it is attached to the full Control or
                # composed from the public Governance/Workflow services.  A
                # missing Control is a composition shape, never evidence that
                # the canonical Task population is empty.
                admission_control = self.control or self.g
                task_admission = inspect_task_admission(
                    admission_control, actor, task=row['id'], checkpoint='claim',
                )
                if task_admission.get('allowed') is not True:
                    diagnostics.append({
                        'task': row['id'], 'stage': 'task_admission',
                        'failures': [
                            item.get('code') or item.get('reason') or
                            item.get('kind') or 'blocked'
                            for item in task_admission.get('failures', [])
                            if isinstance(item, dict)
                        ] or ['task_admission_blocked'],
                    })
                    continue
                local_claim_binding = task_admission_local_claim_binding(
                    task_admission, task=row['id'],
                )
                local_auth=None
                if getattr(self.g,'local_executions',None) is not None:
                    # Resolve the same readiness route used by ready.  A stale
                    # local proposal must not shadow a Task that is already
                    # eligible through the ordinary root path.  Claim already
                    # has its own durable mutation below; this fallback is a
                    # currentness read and must not create a second ready gate
                    # or event while resolving local responsibility.
                    readonly_ready = getattr(self.g, '_evaluate_task_readonly', None)
                    readiness = (readonly_ready(actor, row['id'], 'ready')
                                 if callable(readonly_ready) else
                                 self.g.evaluate_task(actor, row['id'], 'ready'))
                    if readiness['verdict']!='pass':
                        readiness_failures=readiness.get('failures',[])
                        if any(value.startswith('dependency:') for value in readiness_failures):
                            # The readiness gate carries the dependency ID for
                            # internal diagnostics.  Claim details expose only
                            # the selected candidate and the stable cause.
                            diagnostics.append({'task':row['id'],'stage':'dependency',
                                                'failures':['dependency_not_current']})
                            readiness_failures=[value for value in readiness_failures
                                                if not value.startswith('dependency:')]
                        if readiness_failures:
                            diagnostics.append({'task':row['id'],'stage':'ready_gate',
                                                'failures':readiness_failures})
                        continue
                    if readiness.get('route')=='local':
                        local_auth=self.g.local_executions.current_authorization(actor,row['id'],'claim')
                        if not local_auth or not local_auth.get('allowed'):
                            diagnostics.append({'task':row['id'],'stage':'local_authorization',
                                                'failures':(local_auth or {}).get('failures',[])})
                            continue
                if self.s.one("SELECT d.task FROM task_deps d JOIN tasks t ON t.id=d.dependency WHERE d.task=? AND (t.status!='completed' OR t.validity!='current')",(row['id'],)):
                    diagnostics.append({'task':row['id'],'stage':'dependency',
                                        'failures':['dependency_not_current']})
                    continue
                body=row['body']
                conflict=False
                for other in running:
                    other_body=parse_json(other['body'])
                    if set(body['repos'])&set(other_body['repos']) and overlaps(body['write_paths'],other_body['write_paths']): conflict=True
                    if set(body.get('resource_writes',[])) & set(other_body.get('resource_writes',[])+other_body.get('resource_reads',[])): conflict=True
                    if set(body.get('resource_reads',[])) & set(other_body.get('resource_writes',[])): conflict=True
                if conflict:
                    diagnostics.append({'task':row['id'],'stage':'resource_conflict',
                                        'failures':['write_conflict']})
                    continue
                expiry=timestamp()+policy['lease_seconds']
                self.s.execute("UPDATE tasks SET status='running',epoch=epoch+1,lease_owner=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?",(actor.id,expiry,timestamp(),row['id']))
                if local_claim_binding is not None:
                    local_writer = getattr(self.g, 'local_executions', None)
                    need(local_writer is not None and callable(getattr(
                        local_writer, '_claim_from_task_admission', None)),
                         'admission_dependency_unavailable',
                         'Canonical local claim writer is unavailable')
                    local_writer._claim_from_task_admission(
                        actor, row['id'], row['epoch']+1, local_claim_binding,
                    )
                if self.execution_controls is not None:
                    self.execution_controls.record_claim(actor,row['id'],row['epoch']+1,row['attempts']+1)
                binding=self.g.task_binding(row['id'])
                self.sec.event(project,'task_claimed',actor.id,{'task':row['id'],'epoch':row['epoch']+1,
                    'attempt_ordinal':row['attempts']+1,'task_revision':row['revision'],'binding':binding,'expires':expiry})
                return self.task(actor,row['id'])
        # Preserve the historical list-shaped details value even when the
        # scheduler examined no candidates at all.
        raise Fault('no_work','No eligible task; blocked and conflicting tasks were not executed',
                    diagnostics)

    def heartbeat(self,actor,task,epoch):
        with self.s.transaction():
            row=self.task(actor,task)
            actor.require('owner','agent','worker',project=row['project'],task=task if actor.task else None)
            need(row['status']=='running' and row['epoch']==epoch and row['lease_until'] and row['lease_until']>timestamp(), 'stale_lease','Lease is expired or replaced')
            need(actor.id==row['lease_owner'] or actor.role=='owner','forbidden','Not the lease holder')
            expiry=timestamp()+self.g.policy(row['project'])['body']['lease_seconds']
            self.s.execute("UPDATE tasks SET lease_until=?,updated=? WHERE id=?",(expiry,timestamp(),task))
        return {'task':task,'epoch':epoch,'expires':expiry}

    def complete(self,actor,task,expected_revision):
        with self.s.transaction():
            row=self.task(actor,task)
            need(row['revision']==expected_revision,'stale_revision','Task has been replanned')
            gate=self.g.evaluate_task(actor,task)
            need(gate['verdict']=='pass','gate_denied','Completion rejected',gate)
            need(gate['binding']==self.g.task_binding(task),'stale_evidence','Binding changed during transition')
            if getattr(self,'traceability',None) is not None:
                trace_gate=self.traceability.task_closure_gate(row['project'],task,actor)
                need(trace_gate['allowed'],'traceability_incomplete','Traceability task-stage closure is not adopted',trace_gate)
            self.s.execute("UPDATE tasks SET status='completed',lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?",(timestamp(),task))
            self.emit(row['project'],'task_completed',task+':'+str(row['revision']),{'task':task,'candidate':row['candidate'],'gate':gate['id']})
            self.sec.event(row['project'],'task_completed',actor.id,{'task':task,'gate':gate['id'],'assurance':self.g.mode})
        return self.task(actor,task)

    def pause(self,actor,project,task=None,paused=True):
        actor.require('owner','agent',project=project)
        need(type(paused) is bool,'invalid_input','paused must be Boolean')
        with self.s.transaction():
            if task:
                row=self.task(actor,task); need(row['project']==project,'cross_project','Wrong project')
                self.s.execute("UPDATE tasks SET paused=?,epoch=epoch+1,lease_until=NULL,updated=? WHERE id=?",(int(paused),timestamp(),task))
            else:
                self.s.execute("UPDATE projects SET paused=? WHERE id=?",(int(paused),project))
                if paused:
                    self.s.execute("UPDATE tasks SET epoch=epoch+1,lease_until=NULL WHERE project=? AND status='running'",(project,))
            self.sec.event(project,'pause_changed',actor.id,{'task':task,'paused':paused})
        return {'project':project,'task':task,'paused':paused}

    def cancel(self,actor,task,reason):
        row=self.task(actor,task); actor.require('owner','agent',project=row['project'])
        text(reason,'cancellation reason',12000)
        with self.s.transaction():
            need(row['status']!='completed','compensation_required','Completed work needs withdrawal/compensation planning')
            self.s.execute("UPDATE tasks SET status='cancelled',epoch=epoch+1,lease_until=NULL,updated=? WHERE id=?",(timestamp(),task))
            for dep in self.s.all("SELECT task FROM task_deps WHERE dependency=?",(task,)):
                self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(dep['task'],'cancelled_dependency',task,reason))
            self.sec.event(row['project'],'task_cancelled',actor.id,{'task':task,'reason':reason})
        return self.task(actor,task)

    def replan(self,actor,task,expected_revision,reason,review_receipt=None):
        from .task_revisions import TaskRevisions
        return TaskRevisions(self).replan(actor,task,expected_revision,reason,review_receipt)

    def artifacts_collect(self, actor, task, expected_revision, candidate, repository, path):
        """Materialize declared artifact outputs from one sealed candidate.

        The caller supplies only the candidate/repository/path selector.  The
        run, receipt, epoch, producer actor, snapshot bytes, and artifact body
        are resolved from controller-owned immutable records inside one
        transaction.  Meaning review and artifact acceptance remain separate
        workflow operations.
        """
        from .artifact_provenance import (
            MATERIAL_KIND,
            collection_key,
            observed_ref,
            prepare_artifact_collection,
            resolve_produced_artifact,
            task_ref,
            validate_artifact_production_material,
        )
        text(task, "task", 300)
        text(candidate, "candidate", 300)
        # This first read only establishes the route's project authorization.
        # The row is deliberately not used as collection input: a Task can be
        # replanned, fenced, or have its adopted candidate replaced between
        # this read and the transaction below.
        initial = self.task(actor, task)
        actor.require("owner", "agent", project=initial["project"],
                      task=task if actor.task else None)
        need(type(expected_revision) is int and expected_revision >= 1,
             "invalid_revision", "Expected Task revision is invalid")
        text(repository, "repository", 300)
        relative_path(path)
        need(getattr(self, "assurance", None) is not None,
             "unavailable", "Artifact production assurance storage is not installed")

        def blob_get(ident):
            return self.s.blob_get(ident)

        with self.s.transaction():
            # The transaction is the collection linearization point.  Re-read
            # every mutable Task/candidate fence here instead of passing the
            # preliminary row into prepare_artifact_collection.  BEGIN
            # IMMEDIATE prevents a concurrent writer from changing these
            # records after this point; a write immediately before entry is
            # therefore observed and rejected as stale.
            row = self.task(actor, task)
            actor.require("owner", "agent", project=row["project"],
                          task=task if actor.task else None)
            need(row["revision"] == expected_revision, "stale_revision",
                 "Task has been replanned")
            candidate_row = self.s.one("SELECT * FROM candidates WHERE id=?", (candidate,), True)
            need(candidate_row["task"] == task and candidate_row.get("epoch") is not None,
                 "stale_reference", "Candidate is not attached to this Task")
            candidate_body = parse_json(candidate_row["body"])
            snapshot = candidate_body.get("snapshot") if isinstance(candidate_body, dict) else None
            need(isinstance(snapshot, dict) and isinstance(snapshot.get("digest"), str),
                 "integrity_error", "Candidate snapshot identity is malformed")
            candidate_ref = {
                "kind": "candidate", "project": row["project"], "candidate": candidate,
                "task": task, "task_revision": row["revision"],
                "candidate_digest": candidate_row["digest"],
                "snapshot_digest": snapshot["digest"],
            }
            # Runtime candidate adoption clears the claim atomically with the
            # submitted status.  A valid submitted/completed candidate does
            # not need a live lease, but a newly attached lease is evidence
            # that the adoption fence changed after the caller's first read.
            need(row.get("lease_owner") is None and row.get("lease_until") is None,
                 "stale_reference", "Task candidate adoption lease is not sealed")

            def resolve_artifact(ref):
                # Production history can point at a draft row; ordinary
                # assurance artifact endpoints retain their stricter
                # accepted-state rules.
                return resolve_produced_artifact(
                    self.s, ref, project=row["project"], code="integrity_error",
                )

            prepared = prepare_artifact_collection(
                candidate_ref, self.assurance._candidate_context, repository, path,
                blob_get, project=row["project"], task=task,
                revision=expected_revision, current_candidate=row.get("candidate"),
                current_task_status=row["status"], current_task_epoch=row.get("epoch"),
                code="integrity_error",
            )
            state = prepared["state"]
            task_pin = task_ref(state)
            candidate_pin = prepared["candidate_ref"]
            execution_pin = observed_ref(state)
            results = []
            for output in prepared["outputs"]:
                declaration = output["declaration_id"]
                key = collection_key(candidate_pin, repository, path,
                                     prepared["manifest_blob"], declaration)
                existing = []
                for material_row in self.s.all(
                        "SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
                        (row["project"],)):
                    envelope = parse_json(material_row["body"])
                    if envelope.get("material_kind") != MATERIAL_KIND:
                        continue
                    try:
                        material_payload = parse_json(self.s.blob_get(envelope["payload_blob"]))
                    except Fault:
                        raise
                    if not isinstance(material_payload, dict):
                        continue
                    if (material_payload.get("candidate_ref") == candidate_pin and
                            material_payload.get("declaration_id") == declaration):
                        existing.append((material_row, envelope, material_payload))
                if existing:
                    # A declaration is collected once for a sealed candidate.
                    # A replay with the same packet is idempotent; a changed
                    # packet is an integrity failure rather than a new result.
                    need(len(existing) == 1, "integrity_error",
                         "Artifact declaration has ambiguous production history", declaration)
                    material_row, envelope, material_payload = existing[0]
                    need(material_payload.get("collection_key") == key,
                         "integrity_error", "Artifact declaration was collected from different output bytes", declaration)
                    checked = validate_artifact_production_material(
                        material_payload, context=self.assurance._candidate_context,
                        resolve_artifact=resolve_artifact, blob_get=blob_get,
                        project=row["project"], code="integrity_error")
                    results.append({"declaration_id": declaration,
                                    "artifact": checked["artifact"],
                                    "material": {"id": material_row["id"], "digest": material_row["digest"]},
                                    "created": False})
                    continue

                artifact = self.k.propose(actor, row["project"], output["kind"], output["body"])
                artifact_pin = {
                    "kind": "artifact", "project": row["project"],
                    "artifact": artifact["id"], "revision": artifact["revision"],
                    "body_digest": artifact["digest"],
                }
                payload = {
                    "format": "daikibo.artifact-production.v1",
                    "project": row["project"], "task_ref": task_pin,
                    "candidate_ref": candidate_pin,
                    "implementation_run": state["run_id"],
                    "implementation_receipt": state["receipt_id"],
                    "producer_actor": prepared["producer"]["producer_actor"],
                    "producer_epoch": prepared["producer"]["epoch"],
                    "manifest_repository": repository, "manifest_path": path,
                    "manifest_blob": prepared["manifest_blob"],
                    "declaration_id": declaration, "artifact_ref": artifact_pin,
                    "collection_key": key,
                }
                checked = validate_artifact_production_material(
                    payload, context=self.assurance._candidate_context,
                    resolve_artifact=resolve_artifact, blob_get=blob_get,
                    project=row["project"], code="integrity_error")
                material, created = self.assurance.store_material(
                    actor, row["project"], MATERIAL_KIND, payload,
                    [task_pin, candidate_pin, execution_pin, artifact_pin],
                    {"kind": "task.artifacts_collect", "actor": actor.id,
                     "task": task, "candidate": candidate},
                    {"controller": "workflow", "operation": "task.artifacts_collect",
                     "producer_actor": prepared["producer"]["producer_actor"],
                     "run": state["run_id"], "receipt": state["receipt_id"]},
                )
                results.append({"declaration_id": declaration,
                                "artifact": checked["artifact"],
                                "material": {"id": material["id"], "digest": material["digest"]},
                                "created": created})
        return {"task": task, "revision": expected_revision, "candidate": candidate,
                "repository": repository, "path": path,
                "producer_actor": prepared["producer"]["producer_actor"],
                "producer_epoch": prepared["producer"]["epoch"],
                "artifacts": results}

    def emit(self,project,kind,dedup,body):
        ident=uid('OUT')
        self.s.execute("INSERT INTO outbox(id,project,kind,dedup,body,status,due,created) VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(dedup) DO NOTHING",(ident,project,kind,dedup,canonical(body).decode(),timestamp(),timestamp()))
        return ident

    def reconcile(self,actor,project=None):
        actor.require('owner','agent',project=project)
        now=timestamp(); repaired=[]
        with self.s.transaction():
            for row in self.s.all("SELECT * FROM tasks WHERE status='running' AND (lease_until IS NULL OR lease_until<=?) AND (? IS NULL OR project=?)",(now,project,project)):
                if self.execution_controls is not None:
                    self.execution_controls.finalize_attempt(row['id'],row['epoch'],None)
                self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1,lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?",(now,row['id']))
                self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(row['id'],'run_unknown',row['id'],'Lease lost; reconcile observed side effects before retry'))
                self.g.inbox(row['project'],'run_unknown',row['id'],{'task':row['id'],'reason':'lease expired or cancelled'},'warning')
                self.sec.event(row['project'],'lease_expired','system',{'task':row['id']})
                # Leave history, move to planned so the next sweep does not repeatedly refence it.
                self.s.execute("UPDATE tasks SET status='planned' WHERE id=?",(row['id'],)); repaired.append(row['id'])
            for timer in self.s.all("SELECT * FROM timers WHERE fired IS NULL AND due<=? AND (? IS NULL OR project=?) ORDER BY due",(now,project,project)):
                if timer['kind']=='waiver_expiry':
                    self.s.execute("UPDATE waivers SET status='expired' WHERE id=? AND status='active'",(timer['ref'],))
                    self.g.inbox(timer['project'],'waiver',timer['ref'],{'expired':True,'required_action':'remediation'},'critical',timer['due'])
                elif timer['kind']=='decision_expiry':
                    decision=self.s.one('SELECT body,status FROM decisions WHERE id=?',(timer['ref'],))
                    if decision and decision['status']=='provisional':
                        body=parse_json(decision['body'])
                        self.s.execute("UPDATE decisions SET status='expired' WHERE id=?",(timer['ref'],))
                        if body.get('refs'):self.k.invalidate(timer['project'],body['refs'],'Provisional decision expired')
                        self.g.inbox(timer['project'],'decision',timer['ref'],{'expired':True,'must_reconcile':True},'critical',timer['due'])
                elif timer['kind']=='inbox_reminder':
                    self.s.execute("UPDATE inbox SET status='open' WHERE id=?",(timer['ref'],))
                self.s.execute("UPDATE timers SET fired=? WHERE id=?",(now,timer['id']))
                self.sec.event(timer['project'],'timer_fired','system',{'timer':timer['id'],'lag_seconds':max(0,now-timer['due'])})
            # Local notifications are transactionally delivered to inbox; external effects use their own reconciler.
            for event in self.s.all("SELECT * FROM outbox WHERE status='pending' AND kind='task_completed' AND (? IS NULL OR project=?)",(project,project)):
                self.s.execute("UPDATE outbox SET status='delivered',attempts=attempts+1,result=? WHERE id=?",(canonical({'observed':True}).decode(),event['id']))
        return {'reconciled_tasks':repaired,'at':now}

    def status(self,actor,project):
        self.k.project(actor,project)
        return {'project':project,'counts':self.s.all("SELECT status,validity,count(*) AS count FROM tasks WHERE project=? GROUP BY status,validity",(project,)),
                'blocked':self.s.all("SELECT b.* FROM blocks b JOIN tasks t ON t.id=b.task WHERE t.project=? LIMIT 200",(project,)),
                'warnings':self.s.all("SELECT id,kind,ref,severity,due,body FROM inbox WHERE project=? AND status='open' ORDER BY created LIMIT 100",(project,)),
                'active_exceptions':self.s.all("SELECT id,subject,criterion,status,expires FROM waivers WHERE project=? AND status IN ('active','expired')",(project,)),
                'assurance':self.g.mode,'now':timestamp()}
