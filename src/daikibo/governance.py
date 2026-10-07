"""D05 — all formal transitions depend on canonical observations, never claims."""
from __future__ import annotations
from .common import Actor, Fault, canonical, digest, finite_duration, need, obj, parse_json, text, timestamp, uid
from .execution_record import execution_record_consistency
from .observed_receipts import ordered_observed_receipts

DEFAULT_POLICY = {
    "version": 2, "max_attempts": 3, "lease_seconds": 3600, "max_parallel": 4,
    "max_run_seconds": 14_400, "default_task_timeout_seconds": 14_400,
    "max_no_progress_attempts": 3, "max_output_bytes": 8_000_000,
    "review_roles": ["spec", "quality", "test_adequacy"],
    "critical_review_roles": ["specialist"],
    "auxiliary": {}, "delegated_waivers": [],
}
CORE_CRITERIA = {"current_inputs", "actual_execution", "independent_review", "test_results", "human_authority",
                 "scope", "policy_integrity", "coverage", "no_unresolved_conflict"}


def implementation_observation_success(observed, mode):
    """Return the shared success predicate for an implementation receipt.

    Identity and row/body consistency are checked by the provenance resolver;
    this small predicate is deliberately limited to the execution outcome and
    assurance mode.  Runtime's pre-adoption handoff and Governance's ordinary
    candidate evidence therefore cannot drift into different meanings of a
    successful implementation.
    """
    if not isinstance(observed, dict) or not isinstance(observed.get('result'), dict):
        return False
    if not (observed.get('process_started') and observed.get('exit_code') == 0
            and not any(observed.get(key) for key in
                        ('timed_out', 'cancelled', 'output_overflow', 'failure'))
            and not observed['result'].get('error')
            and not observed['result'].get('collector_error')):
        return False
    if mode == 'governed' and (observed.get('assurance') != 'governed'
                               or observed.get('simulated')):
        return False
    return True

class Predicates:
    """Finite, typed policy expressions. No eval, imports, SQL, or arbitrary paths."""
    @classmethod
    def evaluate(cls, rule, facts, depth=0):
        need(depth <= 12 and isinstance(rule, dict) and len(rule)==1, "invalid_policy", "Invalid policy expression")
        op, value = next(iter(rule.items()))
        if op in {"all", "any"}:
            need(isinstance(value,list) and 0 < len(value) <= 100, "invalid_policy", "Nonempty predicate list required")
            values = [cls.evaluate(v,facts,depth+1) for v in value]
            return all(values) if op=='all' else any(values)
        if op == 'not':
            return not cls.evaluate(value,facts,depth+1)
        if op == 'present':
            need(isinstance(value,str),"invalid_policy","Fact name must be a string")
            return value in facts and facts[value] is not None
        if op in {'eq','gte','lte','in'}:
            need(isinstance(value,list) and len(value)==2 and isinstance(value[0],str),"invalid_policy","Expected [fact, value]")
            name, expected = value
            if op=='in':need(isinstance(expected,list) and len(expected)<=1000,'invalid_policy','Expected finite membership list')
            if op in {'gte','lte'}:need(type(expected) in (int,float),'invalid_policy','Comparison bound must be numeric')
            if name not in facts: return False
            actual = facts[name]
            if op=='eq': return type(actual) is type(expected) and actual==expected
            if op=='in':
                need(isinstance(expected,list) and len(expected)<=1000,"invalid_policy","Expected finite membership list")
                return actual in expected
            need(type(actual) in (int,float) and type(expected) in (int,float),"invalid_policy","Comparison operands must be numeric")
            return actual >= expected if op=='gte' else actual <= expected
        raise Fault("invalid_policy", f"Unsupported predicate: {op}")

class Governance:
    def __init__(self, store, security, knowledge, mode='governed'):
        self.s,self.sec,self.k,self.mode=store,security,knowledge,mode
        self.workflow=None
        self.control=None
        self.assurance=None
        # Set by the composition root after the dev18 service is constructed.
        # Governance uses this only to honor a reviewed recovery admission
        # while preserving every other currentness gate.
        self.execution_controls=None
        # Runtime owns the canonical review-material builders. Standalone
        # readers may bind that same provider, but must not synthesize a
        # weaker replacement when it is unavailable.
        self.review_materials=None
        # Set by the composition root after Breakdowns is constructed.  Keeping
        # this optional preserves the small standalone governance fixtures while
        # allowing local authorization to return to the formally adopted root
        # route after a full root currentness check.
        self.breakdowns=None
        self.local_executions=None
        knowledge.assessments=self

    def _bind_composition(self, *, assurance=None, breakdowns=None,
                          local_executions=None, traceability=None,
                          workflow=None, review_materials=None):
        """Bind existing services for a small internal reader composition.

        Standalone readers use the same retained Store and service instances as
        the composition root.  This helper only connects supplied components;
        it never constructs a fallback controller or creates an empty allow
        population when a component is absent.
        """
        for name, value in (
            ("assurance", assurance), ("breakdowns", breakdowns),
            ("local_executions", local_executions),
            ("traceability", traceability), ("workflow", workflow),
            ("review_materials", review_materials),
        ):
            if value is not None:
                setattr(self, name, value)
        # A Governance-only composition is also accepted by the readonly
        # stage reader.  Keep the back-reference on the same object even when
        # the optional Workflow binding is omitted.
        self.g = self
        if workflow is not None:
            # The stage reader accepts a Governance instance as its small
            # composition root.  Point its private ``g`` back to that same
            # reader so stage helpers keep using the supplied Task binding
            # service; no second controller or writer is constructed.
            workflow.g = self
        return self

    def policy(self, project, create=True):
        row=self.s.one("SELECT * FROM policies WHERE project=?",(project,))
        if not row:
            body=DEFAULT_POLICY.copy()
            if create:
                with self.s.transaction():
                    self.s.execute("INSERT OR IGNORE INTO policies VALUES(?,?,?,?)",(project,1,canonical(body).decode(),digest(body)))
                row=self.s.one("SELECT * FROM policies WHERE project=?",(project,),True)
            else:
                return {'project':project,'revision':1,'body':body,'digest':digest(body)}
        row['body']=parse_json(row['body'])
        return row

    def task_binding(self, task, ensure_policy=True):
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,),True)
        plan=self.s.one("SELECT digest FROM plans WHERE task=?",(task,))
        candidate=self.s.one("SELECT digest FROM candidates WHERE id=?",(row['candidate'],)) if row['candidate'] else None
        value={"task":task,"revision":row['revision'],"epoch":row['epoch'],"plan":plan['digest'] if plan else None,
               "candidate":candidate['digest'] if candidate else None,
               "reads":self.s.all("SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact",(task,)),
               "policy":self.policy(row['project'], create=ensure_policy)['digest']}
        # Only a claimed local authorization enters the ordinary task binding.
        # Root tasks retain the historical binding shape unchanged.
        if self.local_executions is not None:
            claimed=self.local_executions.claimed(task,row['epoch'])
            if claimed:
                value['execution_authorization']={'id':claimed['id'],'digest':claimed['digest']}
        return digest(value)

    def receipt(self, ident):
        row=self.s.one("SELECT * FROM receipts WHERE id=?",(ident,),True)
        body=parse_json(row['body'])
        self.sec.verify(body,row['key_id'],row['mac'])
        need(body.get('id')==ident and body.get('run')==row['run'] and body.get('subject')==row['subject'] and body.get('binding')==row['binding'],
             "invalid_evidence","Receipt record and observed content differ")
        run=self.s.one("SELECT * FROM runs WHERE id=?",(row['run'],),True)
        need(run['status']=='finished' and run['binding']==row['binding'] and run['role']==row['role'],"invalid_evidence","Run is not an observed completed execution")
        for key in ('stdout_blob','stderr_blob','input_digest'):
            if body.get(key): self.s.blob_get(body[key])
        if body.get('result',{}).get('report_blob'):self.s.blob_get(body['result']['report_blob'])
        for output in body.get('result',{}).get('build_outputs',[])+body.get('result',{}).get('build_inputs',[]):self.s.blob_get(output['blob'])
        return body

    def _require_governed_review_environment(self, body):
        need(body.get('assurance')=='governed' and not body.get('simulated'),
             "unqualified_execution", "Review lacks governed live execution")
        run=self.s.one('SELECT adapter FROM runs WHERE id=?',(body['run'],),True)
        adapter=self.s.one('SELECT qualified,receipt FROM adapters WHERE name=?',(run['adapter'],),True)
        need(adapter['qualified'] and adapter['receipt'],'adapter_unqualified',
             'Actual CLI protocol qualification is required')
        qual=parse_json(adapter['receipt']);self.sec.verify(qual['record'],qual['key_id'],qual['mac'])
        from .qualification import catalog
        from .agents import Adapters
        current_adapter=Adapters(self.s,self.sec,self.mode).get(run['adapter'])
        need(qual['record'].get('catalog_digest')==catalog()['digest'],'adapter_unqualified',
             'Qualification fixtures changed; a new real qualification run is required')
        need(qual['record'].get('configuration_digest')==digest(
                 {k:v for k,v in current_adapter.items() if k!='qualified'}),
             'adapter_unqualified','Adapter configuration changed since qualification')

    def _usable_review_judgment(self, body):
        result=body.get('result')
        if not (body.get('process_started') is True and body.get('exit_code') == 0 and
                not any(body.get(x) for x in ('timed_out','cancelled','output_overflow',
                                               'input_mutated','failure')) and
                body.get('readonly_verified') is True and body.get('judgment_valid') is True and
                isinstance(result,dict) and result.get('verdict') in {'pass','fail','blocked'} and
                not result.get('error') and not result.get('collector_error')):
            return False
        if self.mode == 'governed':
            try:
                self._require_governed_review_environment(body)
            except Fault:
                return False
        return True

    def _require_latest_review(self, ident, body):
        """Reject an older valid judgment only within its exact review family.

        Invalid executions, malformed judgments, other roles, and other
        bindings do not supersede a usable review. Valid fail/blocked judgments
        do supersede an earlier PASS for the same subject, role, and material.
        """
        role=body.get('role')
        rows=self.s.all(
            'SELECT id FROM receipts WHERE project=? AND subject=? AND role=? AND binding=?',
            (body['project'],body['subject'],role,body['binding']),
        )
        if not rows:
            return
        ordered=ordered_observed_receipts(
            self, project=body['project'], subject=body['subject'], role=role,
            binding=body['binding'], receipt_ids=[row['id'] for row in rows],
        )
        usable=[item for item in ordered if self._usable_review_judgment(item['body'])]
        if usable:
            latest=usable[-1]['row']['id']
            need(latest==ident,'stale_evidence',
                 'An older review cannot be used after a newer valid judgment for the same material',
                 {'requested':ident,'latest':latest})

    def require_review(self, ident, subject, binding, roles, *, latest=False):
        body=self.receipt(ident)
        need(body['subject']==subject and body['binding']==binding and body['role'] in roles,"stale_evidence","Review does not match required subject, role and version")
        if latest:
            self._require_latest_review(ident,body)
        need(body['result'].get('verdict')=='pass' and body['exit_code']==0 and not any(body.get(x) for x in ('timed_out','cancelled','output_overflow')),"review_failed","Review did not pass")
        need(body.get('readonly_verified') is True,"review_modified_input","Reviewer input was not unchanged")
        need(body.get('judgment_valid') is True,"invalid_review","Review output did not meet its schema")
        # Completion reviews over a Task consume the exact formal-test
        # selection that was supplied to the reviewer.  The test receipt does
        # not alter the Task binding; only this semantic material is checked,
        # so a review never becomes stale because of its own receipt.
        task_row = self.s.one('SELECT id FROM tasks WHERE id=?', (subject,))
        if task_row and body.get('role') != 'test_plan' and body.get('binding') == binding:
            material = body.get('review_test_evidence')
            need(isinstance(material, dict) and isinstance(material.get('selection_digest'), str),
                 'stale_evidence', 'Task review did not retain its test-evidence selection')
            current = self.task_test_evidence(actor=None, task=subject,
                                              binding=binding, snapshot_digest=body.get('snapshot'))
            need(material.get('selection_digest') == current.get('selection_digest'),
                 'stale_evidence', 'Task test evidence changed after this review',
                 {'expected': material.get('selection_digest'), 'actual': current.get('selection_digest')})
        if self.mode=='governed':
            self._require_governed_review_environment(body)
        return body

    def evidence_for(self, subject,binding,role):
        # The public signature deliberately has no project argument. Resolve
        # the candidate family to exactly one retained project before asking
        # the shared reader to validate receipt/run/event identity.  A family
        # spanning projects, or carrying an empty/non-string project, is
        # ambiguous evidence and must not be reduced to the first row.
        rows = self.s.all(
            "SELECT id,project FROM receipts WHERE subject=? AND binding=? AND role=? ORDER BY id",
            (subject, binding, role),
        )
        if not rows:
            return []
        projects = []
        for row in rows:
            project = row.get("project")
            if type(project) is not str or not project:
                raise Fault("observed_order_invalid", "Evidence candidate project is invalid", row.get("id"))
            projects.append(project)
        if len(set(projects)) != 1:
            raise Fault("observed_order_invalid", "Evidence candidate project is ambiguous", projects)
        ordered = ordered_observed_receipts(
            self,
            project=projects[0],
            subject=subject,
            role=role,
            binding=binding,
            receipt_ids=[row.get("id") for row in rows],
        )
        # Existing callers consume latest-first IDs and perform their own
        # success/currentness/meaning checks on the selected receipt.
        return [{"id": item["row"]["id"]} for item in reversed(ordered)]

    @staticmethod
    def _test_evidence_summary(receipt):
        """Return bounded, non-report material for one observed test receipt.

        JUnit cases and reports remain addressable through their content
        digests.  They are deliberately not copied into a review prompt.
        """
        result = receipt.get('result') if isinstance(receipt.get('result'), dict) else {}
        missing = result.get('missing')
        if not isinstance(missing, list):
            missing = [] if missing is None else [missing]
        # Required test names are bounded by plan validation, but keep the
        # review material bounded even when reading an old retained receipt.
        missing_page = missing[:100]
        summary = {
            'passed': result.get('passed'),
            'count': result.get('count'),
            'failed': result.get('failed'),
            'missing': missing_page,
            'missing_count': len(missing),
            'missing_truncated': len(missing) > len(missing_page),
            'skipped': result.get('skipped'),
            'input_mutated': receipt.get('input_mutated'),
            'exit_code': receipt.get('exit_code'),
            'timed_out': bool(receipt.get('timed_out')),
            'cancelled': bool(receipt.get('cancelled')),
            'output_overflow': bool(receipt.get('output_overflow')),
            'simulated': bool(receipt.get('simulated')),
            'assurance': receipt.get('assurance'),
        }
        refs = []
        for key, operation in (('stdout_blob', 'blob.read'), ('stderr_blob', 'blob.read'),
                               ('report_blob', 'blob.read')):
            blob = receipt.get(key) if key != 'report_blob' else result.get(key)
            if isinstance(blob, str):
                ref = {'operation': operation, 'blob': blob, 'offset': 0,
                       'limit': 65536, 'project': receipt.get('project')}
                summary[key + '_read_ref'] = ref
                refs.append(ref)
        summary['read_refs'] = refs
        return summary

    @staticmethod
    def _test_history_reason(row, task, binding):
        """Use only durable receipt-row identity before body verification.

        Run epoch/binding/body values are deliberately absent here.  A current
        row whose run was damaged must reach the consistency helper and remain
        the selected observation instead of being hidden as old history.
        """
        if row.get('subject') != task:
            return 'other_task'
        if row.get('binding') != binding:
            return 'stale_binding'
        return 'current_row'

    def task_test_evidence(self, actor, task, *, binding, snapshot_digest,
                           offset=0, limit=100, expected_selection_digest=None):
        """Select the current formal test observations for a Task.

        The selector is read-only and uses the same task, plan, candidate,
        binding and snapshot transaction for every check.  A newer matching
        receipt is selected even when it failed or is invalid; an older PASS
        is never used to hide that result.  ``judgment_valid`` is intentionally
        absent from the test gate because it is a review-only field.
        """
        need(type(offset) is int and offset >= 0, 'invalid_range', 'Invalid test-evidence offset')
        need(type(limit) is int and 1 <= limit <= 100, 'invalid_range', 'Invalid test-evidence limit')
        need(expected_selection_digest is None or isinstance(expected_selection_digest, str),
             'invalid_digest', 'Expected selection digest must be a SHA-256 string')
        with self.s.transaction():
            row = self.s.one('SELECT * FROM tasks WHERE id=?', (task,), True)
            # Internal gate rechecks already ran their caller authorization;
            # the public route supplies the authenticated actor explicitly.
            if actor is not None:
                self.k.project(actor, row['project'])
            current_binding = self.task_binding(task, ensure_policy=False)
            plan_row = self.s.one('SELECT body,digest FROM plans WHERE task=?', (task,))
            plan_body = None
            plan_status = 'missing'
            plan_digest = plan_row['digest'] if plan_row else None
            if plan_row:
                try:
                    plan_body = parse_json(plan_row['body'])
                    need(digest(plan_body) == plan_row['digest'], 'integrity_error', 'Frozen test plan changed')
                    checks = plan_body.get('checks')
                    need(isinstance(checks, list), 'invalid_test_plan', 'Frozen test plan checks are not a list')
                    plan_status = 'current'
                except (Fault, AttributeError, TypeError):
                    plan_body = None
                    checks = []
                    plan_status = 'invalid'
            else:
                checks = []

            candidate_row = self.s.one('SELECT * FROM candidates WHERE id=?', (row['candidate'],)) if row['candidate'] else None
            candidate_body = None
            candidate_status = 'absent'
            candidate_identity = None
            candidate_snapshot = None
            if candidate_row:
                try:
                    candidate_body = parse_json(candidate_row['body'])
                    candidate_snapshot = candidate_body.get('snapshot')
                    valid_snapshot = isinstance(candidate_snapshot, dict) and isinstance(candidate_snapshot.get('digest'), str) and \
                        candidate_snapshot['digest'] == digest({k: v for k, v in candidate_snapshot.items() if k != 'digest'})
                    valid_candidate = (candidate_row['task'] == task and candidate_row['epoch'] == row['epoch'] and
                                       digest(candidate_body) == candidate_row['digest'] and valid_snapshot)
                    candidate_status = 'current' if valid_candidate else 'invalid'
                    candidate_identity = {
                        'id': candidate_row['id'], 'digest': candidate_row['digest'],
                        'epoch': candidate_row['epoch'],
                        'snapshot_digest': candidate_snapshot.get('digest') if isinstance(candidate_snapshot, dict) else None,
                        'status': candidate_status,
                    }
                except (Fault, AttributeError, TypeError):
                    candidate_status = 'invalid'
                    candidate_identity = {'id': candidate_row['id'], 'digest': candidate_row['digest'],
                                          'epoch': candidate_row['epoch'], 'snapshot_digest': None,
                                          'status': candidate_status}
            elif row['candidate']:
                candidate_status = 'invalid'
                candidate_identity = {'id': row['candidate'], 'digest': None,
                                      'epoch': None, 'snapshot_digest': None,
                                      'status': candidate_status}

            # The caller normally supplies the snapshot selected for its
            # prompt.  A public read can omit it only before a candidate exists;
            # never silently replace a supplied identity with a newer one.
            selection_snapshot = snapshot_digest
            snapshot_current = (candidate_status == 'absent' and snapshot_digest is not None) or \
                (candidate_status == 'current' and selection_snapshot == candidate_snapshot.get('digest'))
            if candidate_status == 'absent' and snapshot_digest is None:
                snapshot_current = True
            identity_current = binding == current_binding and snapshot_current and candidate_status in {'absent', 'current'}

            checks_all = []
            history_counts = {}
            for check in checks:
                check_id = check.get('id') if isinstance(check, dict) else None
                check_digest = digest(check) if isinstance(check, dict) else None
                role = 'test:' + check_id if isinstance(check_id, str) else 'test:<invalid>'
                rows = self.s.all("""SELECT q.id,q.run,q.project,q.subject,q.role,q.binding,q.body,q.created,
                                          r.project AS run_project,r.task AS run_task,r.subject AS run_subject,
                                          r.role AS run_role,r.binding AS run_binding,r.epoch AS run_epoch,r.status AS run_status,
                                          r.body AS run_body,r.result AS run_result
                                   FROM receipts q LEFT JOIN runs r ON r.id=q.run
                                   WHERE q.project=? AND q.role=?
                                   ORDER BY q.id""", (row['project'], role))
                selected = None
                selected_row = None
                selected_fault = None
                current_identity_rows = []
                for candidate_receipt_row in rows:
                    reason = self._test_history_reason(candidate_receipt_row, task, binding)
                    if reason != 'current_row':
                        history_counts[reason] = history_counts.get(reason, 0) + 1
                        continue
                    current_identity_rows.append(candidate_receipt_row)
                if identity_current and current_identity_rows:
                    # Newest current subject/binding row is authoritative even
                    # if its signed body, run relation, or test failed.  The
                    # durable run_observed event sequence supplies that order;
                    # no wall-clock timestamp may let an older PASS replace it.
                    try:
                        ordered = ordered_observed_receipts(
                            self, project=row['project'], subject=task,
                            role=role, binding=binding,
                            receipt_ids=[item['id'] for item in current_identity_rows],
                        )
                        selected_id = ordered[-1]['row']['id'] if ordered else None
                        selected_row = next(
                            (item for item in current_identity_rows if item['id'] == selected_id), None,
                        )
                    except Fault as exc:
                        # Preserve a concrete current row for the diagnostic;
                        # the invalid shared-order result is never replaced by
                        # an older receipt or a created-time fallback.
                        selected_row = current_identity_rows[0]
                        selected_fault = exc
                    if selected_fault is None and selected_row is not None:
                        try:
                            selected = self.receipt(selected_row['id'])
                            run_body = parse_json(selected_row['run_body'])
                            run_result = parse_json(selected_row['run_result'])
                            execution_record_consistency(
                                {'id': selected_row['run'], 'project': selected_row['run_project'],
                                 'task': selected_row['run_task'], 'subject': selected_row['run_subject'],
                                 'role': selected_row['run_role'], 'binding': selected_row['run_binding'],
                                 'epoch': selected_row['run_epoch'], 'status': selected_row['run_status'],
                                 'body': selected_row['run_body'], 'result': selected_row['run_result']},
                                run_body, run_result,
                                {'id': selected_row['id'], 'run': selected_row['run'],
                                 'project': selected_row['project'], 'subject': selected_row['subject'],
                                 'role': selected_row['role'], 'binding': selected_row['binding'],
                                 'body': selected_row['body']}, selected)
                            need(selected.get('project') == row['project'] and selected.get('task') == task and
                                 selected.get('subject') == task and selected.get('role') == role and
                                 selected.get('binding') == binding and selected.get('epoch') == row['epoch'] and
                                 selected.get('snapshot') == selection_snapshot,
                                 'invalid_evidence', 'Test receipt does not match the current Task identity')
                            if selected.get('check_id') != check_id or selected.get('check_digest') != check_digest:
                                raise Fault('different_check_definition', 'Test receipt belongs to a different check definition')
                        except (Fault, AttributeError, KeyError, TypeError) as exc:
                            selected_fault = exc if isinstance(exc, Fault) else Fault(
                                'invalid_evidence', 'Test receipt is not a JSON object')

                if selected_row is None:
                    if identity_current:
                        status = 'unobserved'
                        reason = 'no_observation'
                    else:
                        status = 'unknown'
                        reason = 'stale_selection'
                        if binding != current_binding:
                            reason = 'stale_binding'
                        elif candidate_status == 'invalid':
                            reason = 'candidate_identity'
                        elif not snapshot_current:
                            reason = 'wrong_candidate'
                    summary = None
                    receipt_id = run_id = receipt_digest = None
                elif selected_fault is not None:
                    status = 'unknown' if selected_fault.code == 'different_check_definition' else 'invalid'
                    reason = selected_fault.code
                    summary = None
                    receipt_id = selected_row['id']
                    run_id = selected_row['run']
                    receipt_digest = None
                    history_counts[reason] = history_counts.get(reason, 0)
                elif selected is not None:
                    result = selected.get('result') if isinstance(selected.get('result'), dict) else None
                    summary = self._test_evidence_summary(selected)
                    passed = (isinstance(result, dict) and result.get('passed') is True and
                              selected.get('exit_code') == 0 and
                              not any(selected.get(key) for key in ('timed_out', 'cancelled', 'output_overflow', 'input_mutated', 'failure')) and
                              not result.get('error') and not result.get('collector_error') and
                              (self.mode != 'governed' or (selected.get('assurance') == 'governed' and not selected.get('simulated'))))
                    status = 'executed' if passed else 'failed'
                    reason = 'observed_pass' if passed else 'observed_failure'
                    receipt_id = selected['id'];run_id = selected['run'];receipt_digest = digest(selected)
                else:
                    status = 'unknown'; reason = 'invalid_selection'; summary = None
                    receipt_id = run_id = receipt_digest = None

                item = {'check_id': check_id, 'check_digest': check_digest, 'status': status,
                        'selected_receipt': receipt_id, 'run': run_id,
                        'receipt_digest': receipt_digest, 'observed_summary': summary,
                        'history_read_ref': ({'operation': 'evidence.get', 'evidence': receipt_id}
                                             if receipt_id else None),
                        'reason': reason}
                checks_all.append(item)

            selection_manifest = {
                'task': task, 'task_revision': row['revision'], 'epoch': row['epoch'],
                'task_binding': binding, 'candidate_identity': candidate_identity,
                'snapshot_digest': selection_snapshot, 'plan_digest': plan_digest,
                'plan_status': plan_status, 'candidate_status': candidate_status,
                'checks': [{key: item.get(key) for key in ('check_id', 'check_digest', 'status',
                                                            'selected_receipt', 'run', 'receipt_digest', 'reason')}
                           for item in checks_all],
            }
            selection_digest = digest(selection_manifest)
            need(expected_selection_digest is None or expected_selection_digest == selection_digest,
                 'stale_evidence', 'Test evidence selection changed',
                 {'expected': expected_selection_digest, 'actual': selection_digest})
            page = checks_all[offset:offset + limit]
            next_offset = offset + limit if offset + limit < len(checks_all) else None
            return {
                'task': task, 'task_revision': row['revision'], 'epoch': row['epoch'],
                'task_binding': binding, 'candidate_identity': candidate_identity,
                'snapshot_digest': selection_snapshot, 'plan_digest': plan_digest,
                'checks': page, 'total': len(checks_all), 'next_offset': next_offset,
                'selection_digest': selection_digest,
                'selection_current': identity_current and plan_status == 'current',
                'history': {'counts': history_counts, 'read_only': True},
            }

    def check_current(self, task, ensure_policy=True):
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,),True)
        failures=[]
        if row['validity']!='current': failures.append('inputs_require_reassessment')
        for ref in self.s.all("SELECT r.*,a.revision current_revision,a.digest current_digest,a.status FROM task_reads r JOIN artifacts a ON a.id=r.artifact WHERE r.task=?",(task,)):
            if ref['revision']!=ref['current_revision'] or ref['digest']!=ref['current_digest'] or ref['status']!='accepted':
                failures.append('stale_or_unaccepted:'+ref['artifact'])
        blocks=self.s.all("SELECT kind,ref,reason FROM blocks WHERE task=?",(task,))
        policy=self.policy(row['project'], create=ensure_policy)['body']
        # attempts is retained as diagnostic history after the v2 policy is
        # adopted.  The independently reviewed no_progress counter is the
        # only admission threshold.
        failures.extend('block:'+b['kind']+':'+b['ref'] for b in blocks
                        if not (b['kind']=='budget' and b['ref']=='attempts' and policy.get('version',1)>=2))
        return failures

    def _root_execution_current_for_program(self, actor, task, program,
                                             *, ensure_policy=False):
        """Read whether ``program`` currently owns an adopted root for ``task``.

        The program is supplied by the canonical active-membership resolver,
        rather than by the Task's optional ``workflow_id``.  This keeps the
        root route useful for the Unit4-R admission projection without letting
        a caller create a membership by naming an arbitrary program.  The
        method is read-only, including the disabled-profile compatibility
        branch for an already-adopted root.
        """
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,))
        if not row:
            return False
        body=parse_json(row['body'])
        if type(program) is not str or not program or body.get('task_kind')!='production':
            return False
        program_row=self.s.one("SELECT project,phase,body FROM programs WHERE id=?",(program,))
        if not program_row or program_row['project']!=row['project']:
            return False
        # The normal root route becomes authoritative only after the complete
        # planning prefix has been traversed and implementation has begun.
        # Planning.advance retains every prior phase in history, so integration
        # and delivery histories necessarily contain implementation (and then
        # integration) entries as well.  Requiring the old seven-item set here
        # would make a formally adopted root fall back to an expired local
        # certificate forever after the first downstream transition.
        planning_prefix=['requirements','scenarios','boundaries','contracts','feasibility','design','plan']
        phase_order=planning_prefix + ['implementation','integration','delivery']
        if program_row['phase'] not in {'implementation','integration','delivery'}:
            return False
        history=parse_json(program_row['body']).get('history',[])
        phase_index=phase_order.index(program_row['phase'])
        history_phases=[entry.get('phase') for entry in history]
        if history_phases != phase_order[:phase_index]:
            return False
        if self.breakdowns is None:
            return False
        try:
            # Consume the private adopted-root reader used by the shared
            # Unit5 boundary.  The public status wrapper remains a writer
            # compatibility surface; this projection supplies the explicit
            # disabled-root policy without filtering arbitrary error names.
            root_reader = getattr(self.breakdowns, "_program_status", None)
            if not callable(root_reader):
                return False
            disabled_root = getattr(self.breakdowns, "_disabled_old_root", None)
            enforce_plan_gate = not bool(
                callable(disabled_root) and
                disabled_root(actor, row["project"], program)
            )
            status=root_reader(
                actor, program, reviews=True, readonly=True,
                enforce_plan_gate=enforce_plan_gate,
            )
            if not status.get('current'):
                # Disabled old-root compatibility is classified by the private
                # root audit boundary.  No public failure-name filter may turn
                # a failed root report into currentness here.
                return False
            root=self.breakdowns._row(actor,status['active'])
            if not any(task in unit.get('tasks',[]) for unit in root['body'].get('units',[])):
                return False
        except Fault:
            return False
        return not self.check_current(task, ensure_policy=ensure_policy)

    def root_execution_current_for_program_readonly(self, actor, task, program):
        """Read whether an explicitly supplied active program owns this Task."""
        return self._root_execution_current_for_program(
            actor, task, program, ensure_policy=False,
        )

    def root_execution_current(self, actor, task, ensure_policy=True):
        """Resolve the Task's workflow projection through the root reader."""
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,))
        if not row:
            return False
        try:
            program_id=parse_json(row['body']).get('workflow_id')
        except Fault:
            return False
        return self._root_execution_current_for_program(
            actor, task, program_id, ensure_policy=ensure_policy,
        )

    def implementation_evidence(self, task):
        """Recheck retained observed implementation, not only the run status label."""
        row=self.s.one('SELECT * FROM tasks WHERE id=?',(task,),True)
        need(row['candidate'],'missing_candidate','No retained implementation candidate')
        candidate=self.s.one('SELECT * FROM candidates WHERE id=?',(row['candidate'],),True)
        body=parse_json(candidate['body'])
        need(candidate['task']==task and candidate['epoch']==row['epoch'] and digest(body)==candidate['digest'],
             'invalid_candidate','Candidate identity, epoch or content differs')
        observed=self.receipt(body['implementation_receipt'])
        need(observed['run']==candidate['implementation_run'] and observed['role']=='implementer'
             and observed['subject']==task and observed['task']==task and observed['project']==row['project']
             and observed['epoch']==row['epoch'], 'invalid_implementation','Observed implementation belongs to a different task or epoch')
        if self.local_executions is not None:
            claimed=self.local_executions.claimed(task,row['epoch'])
            if claimed:
                need(body.get('execution_authorization')=={'id':claimed['id'],'digest':claimed['digest']},
                    'invalid_local_execution_claim','Candidate does not retain the claimed local authorization')
        need(implementation_observation_success(observed, None),
             'implementation_failed', 'Implementation process was not successfully observed')
        if self.mode == 'governed':
            need(implementation_observation_success(observed, self.mode),
                 'unqualified_implementation', 'A fixture is not governed implementation')
        snapshot=body['snapshot']
        need(snapshot['digest']==digest({k:v for k,v in snapshot.items() if k!='digest'}),
             'invalid_candidate','Candidate snapshot binding differs')
        # Recheck changed outputs retained for this task. Whole-repository verification
        # still happens when materializing / verifying the integrated delivery.
        for change in body['changes']:
            after=change['after']
            if after is not None and after['kind']=='file':self.s.blob_get(after['blob'])
        return observed

    def evaluate_task(self, actor, task, gate='complete'):
        """Evaluate and record the public Task gate."""
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,),True)
        actor.require('owner','agent','worker',project=row['project'],task=task if actor.task else None)
        return self._evaluate_task(actor, task, gate=gate, readonly=False)

    def _evaluate_task_readonly(self, actor, task, gate='complete'):
        """Evaluate the current Task gate without durable gate/event writes."""
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,),True)
        actor.require('owner','agent','worker',project=row['project'],task=task if actor.task else None)
        return self._evaluate_task(actor, task, gate=gate, readonly=True)

    def _evaluate_task(self, actor, task, gate='complete', *, readonly=False):
        with self.s.transaction():
            row=self.s.one('SELECT * FROM tasks WHERE id=?',(task,),True)
            binding=self.task_binding(task, ensure_policy=not readonly)
            failures=self.check_current(task, ensure_policy=not readonly)
            if self.execution_controls is not None:
                recovery=self.execution_controls.recovery_authorization(
                    actor, task, readonly=readonly)
                if recovery:
                    # A recovery authorization consumes only the matching
                    # unresolved run/lease blocker.  It never clears stale
                    # inputs, no-progress limits, dependencies, or review
                    # gates.
                    failures=[value for value in failures
                              if not value.startswith('block:run_unknown:')]
            if gate in {'complete','recheck'} and self.local_executions is not None:
                claimed=self.local_executions.claimed(task,row['epoch'])
                if claimed and not self.root_execution_current(
                        actor, task, ensure_policy=not readonly):
                    claimed_body=parse_json(claimed['body'])
                    local=(self.local_executions.current_authorization_readonly(
                                actor, task, gate, claimed_body.get('certified_event'))
                           if readonly else
                           self.local_executions.current_authorization(
                                actor, task, gate, claimed_body.get('certified_event')))
                    if not local or not local.get('allowed'):
                        failures.extend('local_execution:'+value for value in (local or {}).get('failures',['not_current']))
            project=self.s.one("SELECT paused FROM projects WHERE id=?",(row['project'],),True)
            if project['paused'] or row['paused']: failures.append('paused')
            plan=self.s.one("SELECT * FROM plans WHERE task=?",(task,))
            if not plan: failures.append('no_approved_test_plan')
            body=parse_json(row['body'])
            from .obligations import from_store
            required_coverage = set(body.get('acceptance',[]))
            try: required_coverage = set(from_store(self.s,body)['required_coverage'])
            except Fault as exc: failures.append('acceptance_identity:'+exc.code)
            for dep in self.s.all("SELECT t.id,t.status,t.validity FROM task_deps d JOIN tasks t ON d.dependency=t.id WHERE d.task=?",(task,)):
                if dep['status']!='completed' or dep['validity']!='current': failures.append('dependency:'+dep['id'])
            if gate in {'complete','recheck'}:
                if gate=='complete' and row['status']!='submitted': failures.append('not_submitted')
                if gate=='recheck' and row['status']!='completed': failures.append('not_completed')
                if not row['candidate']: failures.append('no_candidate')
                else:
                    candidate=self.s.one("SELECT * FROM candidates WHERE id=?",(row['candidate'],),True)
                    if candidate['epoch']!=row['epoch']: failures.append('stale_candidate')
                    try:self.implementation_evidence(task)
                    except Fault as exc:failures.append('implementation:'+exc.code)
                policy=self.policy(row['project'], create=not readonly)['body']
                roles=list(policy['review_roles'])
                if body.get('risk')=='critical': roles += policy['critical_review_roles']
                reviewer_runs=set()
                for role in roles:
                    refs=self.evidence_for(task,binding,role)
                    if not refs: failures.append('review_missing:'+role); continue
                    # Do not cherry-pick an old PASS after a newer failure for the same input.
                    try:
                        ev=self.require_review(refs[0]['id'],task,binding,{role})
                        if ev['run'] in reviewer_runs: failures.append('review_not_independent:'+role)
                        reviewer_runs.add(ev['run'])
                        required=required_coverage
                        if not required <= set(ev['result'].get('covered',[])): failures.append('review_coverage:'+role)
                        if ev['result'].get('findings'): failures.append('unresolved_review_findings:'+role)
                        if role=='test_adequacy' and row['candidate']:
                            c=parse_json(self.s.one('SELECT body FROM candidates WHERE id=?',(row['candidate'],),True)['body'])
                            expected={f['id'] for f in c.get('findings',[])}
                            addressed={d['id'] for d in ev['result'].get('dispositions',[]) if d.get('resolution')=='acceptable' and d.get('reason')}
                            if not expected<=addressed: failures.append('unaddressed_stub_or_test_weakening')
                    except Fault as exc: failures.append(role+':'+exc.code)
                test_evidence = None
                if plan:
                    candidate_snapshot = None
                    if row['candidate']:
                        try:
                            candidate_body = parse_json(self.s.one(
                                "SELECT body FROM candidates WHERE id=?", (row['candidate'],), True)['body'])
                            candidate_snapshot = candidate_body.get('snapshot', {}).get('digest')
                        except (Fault, AttributeError):
                            # The candidate gate below reports the malformed
                            # candidate.  Keep this selector call read-only and
                            # expose its resulting unknown test observations.
                            candidate_snapshot = None
                    test_evidence = self.task_test_evidence(
                        actor, task, binding=binding, snapshot_digest=candidate_snapshot)
                    for item in test_evidence['checks']:
                        check_id = item['check_id']
                        if item['status'] == 'unobserved':
                            failures.append('test_missing:' + str(check_id))
                            continue
                        if item['status'] in {'invalid', 'unknown'}:
                            failures.append('test:' + str(item.get('reason') or 'invalid_evidence'))
                            continue
                        if item['status'] == 'failed':
                            failures.append('test_failed:' + str(check_id))
                            summary = item.get('observed_summary') or {}
                            if summary.get('input_mutated'):
                                failures.append('test_mutated_inputs:' + str(check_id))
                            if self.mode == 'governed' and (
                                    summary.get('assurance') != 'governed' or summary.get('simulated')):
                                failures.append('test_ungoverned:' + str(check_id))
                unresolved=self.s.all("SELECT id FROM inbox WHERE project=? AND status='open' AND kind IN ('product_decision','conflict')",(row['project'],))
                # Scope-specific blocks above, not a project-wide halt, govern task completion.
                facts={'risk':body.get('risk'),'review_run_count':len(reviewer_runs),'all_core_checks_passed':not failures}
                test_count=0
                if test_evidence:
                    for item in test_evidence['checks']:
                        if item['status'] == 'executed':
                            test_count += (item.get('observed_summary') or {}).get('count', 0) or 0
                facts['observed_test_count']=test_count
                for criterion in body.get('auxiliary_criteria',[]):
                    rule=policy['auxiliary'].get(criterion)
                    satisfied=bool(rule) and Predicates.evaluate(rule,facts)
                    if not satisfied and not self.valid_waiver(
                            row['project'], task, criterion,
                            ensure_policy=not readonly):
                        failures.append('auxiliary:'+criterion)
            else:
                need(gate=='ready',"invalid_gate","Supported gates: ready, complete, recheck")
                route='root'
                if row['status'] not in {'planned','ready'}: failures.append('not_planned')
                if not self.s.one("SELECT artifact FROM task_reads WHERE task=?",(task,)): failures.append('no_source_requirements')
                if self.mode=='governed' and body.get('task_kind')=='production':
                    program=self.s.one('SELECT phase,body FROM programs WHERE id=? AND project=?',(body.get('workflow_id'),row['project']))
                    if not program or program['phase']!='implementation':failures.append('engineering_workflow_not_implementation_ready')
                    elif {h['phase'] for h in parse_json(program['body']).get('history',[])}!={'requirements','scenarios','boundaries','contracts','feasibility','design','plan'}:failures.append('engineering_phase_history_incomplete')
                if failures and self.local_executions is not None and body.get('task_kind')=='production':
                    barrier={'engineering_workflow_not_implementation_ready','engineering_phase_history_incomplete'}
                    if set(failures)<=barrier:
                        local=(self.local_executions.current_authorization_readonly(
                                    actor, task, 'ready') if readonly else
                               self.local_executions.current_authorization(
                                    actor, task, 'ready'))
                        if local and local.get('allowed'):
                            failures=[];route='local'

            # Unit4-R is the same read boundary for writer and projection
            # callers.  Its reader emits no gate, claim, candidate, profile,
            # or workflow row; only the public gate below is durable.
            from .unit4_enforcement import inspect_task_admission
            admission_checkpoint = {
                'ready': 'ready', 'complete': 'complete', 'recheck': 'recheck',
            }[gate]
            control = self.control or getattr(self.workflow, 'control', None)
            # A standalone Governance instance is itself the read composition
            # root for its Store/Knowledge/Workflow services.  Let the same
            # canonical reader prove true absence; never manufacture an
            # allowed empty population merely because ``control`` is unset.
            task_admission = inspect_task_admission(
                control or self, actor, task=task, checkpoint=admission_checkpoint,
            )
            if task_admission.get('allowed') is not True:
                admission_failures = [
                    'task_admission:' + str(
                        item.get('code') or item.get('reason') or
                        item.get('kind') or 'blocked'
                    )
                    for item in task_admission.get('failures', [])
                    if isinstance(item, dict)
                ]
                failures.extend(admission_failures or ['task_admission:blocked'])
            result={"id":uid('GATE'),"task":task,"gate":gate,"binding":binding,"verdict":"fail" if failures else "pass",
                    "failures":failures,"policy":self.policy(row['project'], create=not readonly)['digest'],"assurance":self.mode,
                    "route":route if gate=='ready' else 'root',"task_admission":task_admission}
            if not readonly:
                self.s.execute("INSERT INTO gate_results VALUES(?,?,?,?,?,?,?,?,?)",(result['id'],row['project'],task,gate,binding,result['policy'],result['verdict'],canonical(result).decode(),timestamp()))
                self.sec.event(row['project'],"gate_evaluated",actor.id,result)
        return result

    def execution_readiness(self, actor, task, stage='ready', pinned=None):
        """Resolve the ordinary root route or a current local authorization.

        The ready gate records the route and its complete root diagnostics. Later
        runtime stages only recheck an already selected local authorization; they
        never turn a portable certification into a new readiness result.
        """
        need(stage in {'ready','claim','execute','candidate','complete','recheck'}, 'invalid_stage', 'Unknown execution readiness stage')
        if stage == 'ready':
            gate=self.evaluate_task(actor,task,'ready')
            if gate['verdict'] == 'pass':
                if gate.get('route') == 'local' and self.local_executions is not None:
                    local=self.local_executions.current_authorization_readonly(actor,task,stage,pinned)
                    if local and local.get('allowed'):
                        return {**local,'binding':gate['binding'],'gate':gate['id'],'failures':[]}
                    return {'allowed':False,'route':'local','binding':gate['binding'],'gate':gate['id'],
                            'failures':(local or {}).get('failures',['local_execution_not_ready'])}
                return {'allowed':True,'route':'root','binding':gate['binding'],'gate':gate['id'],'failures':[]}
            return {'allowed':False,'route':'root','binding':gate['binding'],'gate':gate['id'],'failures':gate['failures']}
        return self.execution_readiness_readonly(actor, task, stage, pinned)

    def execution_readiness_readonly(self, actor, task, stage='candidate', pinned=None):
        """Read current route authority without evaluating or recording a gate.

        The normal ``execution_readiness`` method remains the compatibility
        service wrapper: its ``ready`` route evaluates and records the existing
        root gate.  A private stage consumer such as candidate provenance must
        use this read primitive instead, so a local authorization check cannot
        call back through a stage-enforcing wrapper and form a recursive DAG.
        The primitive only reads the current Task/claim and delegates local
        qualification to ``LocalExecutions.current_authorization_readonly``;
        it never writes a gate, claim, candidate, or stage result.
        """
        need(stage in {'ready','claim','execute','candidate','complete','recheck'}, 'invalid_stage', 'Unknown execution readiness stage')
        if self.local_executions is not None:
            row=self.s.one("SELECT epoch FROM tasks WHERE id=?",(task,),True)
            claimed=self.local_executions.claimed(task,row['epoch'])
            if claimed:
                if stage in {'complete','recheck'} and self.root_execution_current(actor,task,ensure_policy=False):
                    return {'allowed':True,'route':'root','failures':[],'root_adopted':True}
                claimed_body=parse_json(claimed['body'])
                local=self.local_executions.current_authorization_readonly(
                    actor,task,stage,pinned or claimed_body.get('certified_event'))
                return local
        return {'allowed':True,'route':'root','failures':[]}

    def valid_waiver(self,project,subject,criterion, *, ensure_policy=True):
        if criterion in CORE_CRITERIA: return False
        row=self.s.one("SELECT * FROM waivers WHERE project=? AND subject=? AND criterion=? AND status='active' AND expires>? ORDER BY created DESC LIMIT 1",(project,subject,criterion,timestamp()))
        if not row: return False
        data=parse_json(row['body'])
        return data['policy_digest']==self.policy(project, create=ensure_policy)['digest']

    def waiver(self,actor,project,subject,criterion,reason,expires,remediation_task,controls,decision=None):
        actor.require('owner','agent',project=project)
        policy=self.policy(project)
        row=self.s.one("SELECT body FROM tasks WHERE id=? AND project=?",(subject,project),True)
        task=parse_json(row['body'])
        need(task['risk']!='critical' and criterion not in CORE_CRITERIA and criterion in policy['body']['auxiliary'],"nonwaivable","Required, critical and integrity controls cannot be waived")
        if actor.role!='owner':
            need(criterion in policy['body']['delegated_waivers'] and task['risk']=='lite',"human_approval_required","No authority delegated for this waiver")
        text(reason,'reason',12000); text(controls,'compensating controls',12000)
        need(timestamp()<expires<=timestamp()+86400*7,'invalid_expiry','Waiver must expire within seven days')
        self.s.one("SELECT id FROM tasks WHERE id=? AND project=?",(remediation_task,project),True)
        ident=uid('WAIVER')
        data={'owner':actor.id,'reason':reason,'controls':controls,'remediation_task':remediation_task,
              'policy_digest':policy['digest'],'authority':actor.role,'decision':decision,'risk':task['risk']}
        with self.s.transaction():
            self.s.execute("INSERT INTO waivers VALUES(?,?,?,?,?,?,?,?)",(ident,project,subject,criterion,canonical(data).decode(),'active',expires,timestamp()))
            self.s.execute("INSERT INTO timers(id,project,kind,ref,due) VALUES(?,?,?,?,?)",(uid('TIMER'),project,'waiver_expiry',ident,expires))
            self.inbox(project,'waiver',ident,data,'warning',expires)
            self.sec.event(project,'waiver_issued',actor.id,{'id':ident,**data})
        return {'id':ident,'status':'active','expires':expires,'warning':'Pending confirmation/remediation; not silently cleared.'}

    def inbox(self,project,kind,ref,body,severity='warning',due=None):
        ident=uid('INBOX')
        encoded=canonical(body).decode()
        with self.s.transaction():
            existing=self.s.one('SELECT * FROM inbox WHERE project=? AND kind=? AND ref=?',(project,kind,ref))
            if existing:
                changed=(existing['body']!=encoded or existing['severity']!=severity or existing['due']!=due)
                # ``created`` is the start of the currently visible notice
                # version. Keep it stable on an identical resend so existing
                # responses remain bound to the same material.
                version_created=timestamp() if changed else existing['created']
                self.s.execute('UPDATE inbox SET body=?,severity=?,status=\'open\',due=?,created=? WHERE id=?',
                               (encoded,severity,due,version_created,existing['id']))
                item=existing['id']
            else:
                self.s.execute('INSERT INTO inbox(id,project,kind,ref,body,severity,due,created) VALUES(?,?,?,?,?,?,?,?)',
                               (ident,project,kind,ref,encoded,severity,due,timestamp()))
                item=ident;changed=True
            if changed:
                published=self.s.one('SELECT body,severity,due FROM inbox WHERE id=?',(item,),True)
                body_digest=digest(published['body'].encode())
                self.sec.event(project,'notification_published','daikibo-governance',
                               {'item':item,'kind':kind,'ref':ref,'body_digest':body_digest,
                                'version_digest':digest({'body_digest':body_digest,'severity':published['severity'],
                                                         'due':published['due']})})

    def policy_propose(self,actor,project,body):
        actor.require('owner','agent',project=project)
        obj(body,required=tuple(DEFAULT_POLICY))
        current = self.policy(project)
        execution_fields = ('version', 'default_task_timeout_seconds', 'max_no_progress_attempts', 'max_run_seconds')
        need(all(body.get(field) == current['body'].get(field) for field in execution_fields),
             'wrong_route', 'Use execution_control.policy_propose for execution-control policy adoption')
        need(body['review_roles']==DEFAULT_POLICY['review_roles'] and body['critical_review_roles']==DEFAULT_POLICY['critical_review_roles'],
             'nonwaivable','Core review roles cannot be weakened')
        for field in ('max_attempts','lease_seconds','max_parallel','max_run_seconds','max_output_bytes'):
            need(type(body[field]) is int and body[field]>0,'invalid_policy','Positive integer limit required')
        if 'default_task_timeout_seconds' in body: finite_duration(body['default_task_timeout_seconds'],'default_task_timeout_seconds')
        if 'max_no_progress_attempts' in body: need(body['max_no_progress_attempts']==3,'invalid_policy','No-progress threshold is fixed at three')
        need(body['max_attempts']<=20 and body['max_parallel']<=32,'invalid_policy','Policy bound exceeds safe supported limits')
        need(isinstance(body['auxiliary'],dict) and not CORE_CRITERIA.intersection(body['auxiliary']),'invalid_policy','Invalid auxiliary rules')
        for rule in body['auxiliary'].values(): Predicates.evaluate(rule,{})
        need(isinstance(body['delegated_waivers'],list) and set(body['delegated_waivers'])<=body['auxiliary'].keys(),'invalid_policy','Invalid delegation')
        ident=uid('POLICY-PROPOSAL')
        old=self.policy(project)
        proposal={'type':'policy','body':body,'old_digest':old['digest'],'old_revision':old['revision']}
        with self.s.transaction():
            self.s.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",(ident,project,1,canonical(proposal).decode(),digest(proposal),'pending',None,None,None,timestamp()))
            self.inbox(project,'product_decision',ident,proposal,'critical')
        return {'id':ident,'digest':digest(proposal),'old_policy':old['digest']}
