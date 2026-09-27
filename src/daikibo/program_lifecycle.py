"""Composite workflow completion and explicit, history-preserving backtracking."""
from __future__ import annotations
from .common import Fault, canonical, digest, need, parse_json, text, timestamp, uid
from .planning import PHASES


class ProgramLifecycle:
    def __init__(self, control):
        self.c, self.s = control, control.s

    def _row(self, actor, program):
        row = self.s.one('SELECT * FROM programs WHERE id=?', (program,), True)
        self.c.k.project(actor, row['project'])
        return row

    def engineering_digest(self, project):
        """A closure is a historical fact, not authority over future user input."""
        return self.c.supervisor.state_digest(project)

    def status(self, actor, program):
        row = self._row(actor, program)
        closure = self.s.one('SELECT * FROM program_closures WHERE program=? ORDER BY created DESC,id DESC LIMIT 1', (program,))
        recorded = parse_json(closure['body']) if closure else None
        changed = bool(recorded and recorded['engineering_digest'] != self.engineering_digest(row['project']))
        return {'program':program,'project':row['project'],'phase':row['phase'],'revision':row['revision'],
                'recorded_closure':closure['id'] if closure else None,
                'state':'closed_recorded' if recorded and not changed else 'reassessment_required' if recorded else 'in_progress',
                'current_completion_verified':False, 'state_changed_after_close':changed,
                'next':'program.completion to recheck current evidence; a historical close or finished Tasks alone is not deploy readiness'}

    def completion(self, actor, program, delivery=None):
        row = self._row(actor, program); project = row['project']; failures = []
        # Capture one consistent local read, including rechecks of referenced evidence.
        with self.s.transaction():
            row = self._row(actor, program)
            if row['phase'] != 'delivery': failures.append({'code':'phase_incomplete','phase':row['phase']})
            if self.s.one('SELECT paused FROM projects WHERE id=?',(project,))['paused']:
                failures.append({'code':'project_paused'})
            breakdown = self.c.breakdowns.program_status(actor, program)
            if not breakdown['current']: failures.append({'code':'breakdown_incomplete','details':breakdown['failures']})
            if getattr(self.c, 'workstreams', None):
                delegated = self.c.workstreams.program_audit(actor, program)
                if not delegated['current']:
                    failures.append({'code':'delegated_work_incomplete','details':delegated['failures']})
            scope = self.c.k.trace(actor, project)
            if not scope['structural_complete']: failures.append({'code':'trace_incomplete','details':scope['missing']})
            if not self.c.k.source_coverage(actor,project)['structurally_complete']:
                failures.append({'code':'source_coverage_incomplete'})
            for task in self.s.all("SELECT id,status,validity FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id", (project,)):
                if task['status'] != 'completed' or task['validity'] != 'current':
                    failures.append({'code':'task_incomplete','task':task['id']}); continue
                result = self.c.g.evaluate_task(actor,task['id'],gate='recheck')
                if result['verdict'] != 'pass': failures.append({'code':'task_evidence_invalid','task':task['id'],'details':result['failures']})
            if self.s.one("SELECT id FROM jobs WHERE project=? AND status IN ('queued','running','retry_wait') AND kind NOT IN ('supervisor.turn')", (project,)):
                failures.append({'code':'work_still_pending'})
            for change in self.s.all("SELECT id,stage FROM changes WHERE project=? AND stage NOT IN ('withdrawn','ready_for_reimplementation','closed')", (project,)):
                failures.append({'code':'change_unresolved','change':change['id'],'stage':change['stage']})
            # Delivery.certify repeats decisions, exceptions, exact snapshot, tests and
            # whole-change reviews. It must not be replaced by a status-label check.
            if delivery is None:
                found = self.s.one("SELECT id FROM deliveries WHERE project=? AND status IN ('verified','delivered') AND json_extract(body,'$.binding.program')=? ORDER BY created DESC LIMIT 1", (project,program))
                delivery = found['id'] if found else None
            if delivery is None: failures.append({'code':'integrated_delivery_required'})
            else:
                d = self.s.one('SELECT project,status,digest,body FROM deliveries WHERE id=?',(delivery,),True)
                need(d['project']==project, 'cross_project', 'Delivery belongs elsewhere')
                if parse_json(d['body']).get('binding',{}).get('program')!=program: failures.append({'code':'delivery_program_mismatch'})
                if d['status'] not in {'verified','delivered'}: failures.append({'code':'delivery_not_certified'})
                try: self.c.d.certify(actor, delivery, check_only=True)
                except Fault as exc: failures.append({'code':'delivery_recheck_failed','details':exc.as_dict()})
                if getattr(self.c, 'traceability', None) is not None and d['status'] == 'delivered':
                    trace_gate = self.c.traceability.delivered_closure_gate(project, delivery, actor)
                    failures.extend({'code': code} for code in trace_gate['failures'])
            if self.c.g.mode != 'governed': failures.append({'code':'validation_not_final'})
        return {'program':program,'revision':row['revision'],'delivery':delivery,'completed':not failures,
                'failures':failures,'assurance':self.c.g.mode,'scope_reduced':False,
                'task_counts':self.s.all('SELECT status,validity,count(*) AS count FROM tasks WHERE project=? GROUP BY status,validity',(project,)),
                'meaning_correctness_guaranteed':False}

    def finish(self, actor, program, expected_revision, delivery, review_receipt):
        with self.s.transaction():
            row = self._row(actor, program); actor.require('owner','agent', project=row['project'])
            need(row['revision']==expected_revision, 'stale_revision', 'Workflow changed before closure')
            report = self.completion(actor, program, delivery)
            need(report['completed'], 'program_gate_denied', 'Composite workflow is not complete', report['failures'])
            binding = self.c.p.program_binding(program)
            self.c.g.require_review(review_receipt,program,binding,{'phase'})
            if self.s.one("SELECT id FROM review_scopes WHERE program=? AND phase='delivery' AND status='active'", (program,)):
                need(self.c.scopes.summary(actor, program)['complete'], 'incomplete_review_coverage', 'Final phase packets are not fully reviewed')
            stamp = self.engineering_digest(row['project'])
            old = self.s.one('SELECT id,body FROM program_closures WHERE program=? AND binding=? AND delivery=?', (program,binding,delivery))
            if old and parse_json(old['body'])['engineering_digest']==stamp:
                return {'id':old['id'],'program':program,'state':'completed','replayed':True}
            ident = uid('CLOSURE')
            record = {'program_revision':row['revision'],'delivery':delivery,'review_receipt':review_receipt,
                      'engineering_digest':stamp,'breakdown':self.c.breakdowns.active(actor,program),'report':report}
            self.s.execute('INSERT INTO program_closures VALUES(?,?,?,?,?,?,?)', (ident,program,row['project'],delivery,binding,canonical(record).decode(),timestamp()))
            self.c.sec.event(row['project'],'program_completed',actor.id,{'closure':ident,'program':program,'delivery':delivery,'review_receipt':review_receipt})
        return {'id':ident,'program':program,'state':'completed','delivery':delivery,'scope_reduced':False}

    def reopen(self, actor, program, expected_revision, target_phase, reason, cause, review_receipt=None):
        text(reason, 'reason for returning to an earlier engineering layer', 20000)
        text(cause, 'source, change or finding ID', 200)
        with self.s.transaction():
            row = self._row(actor,program); actor.require('owner','agent', project=row['project'])
            need(row['revision']==expected_revision, 'stale_revision', 'Program has changed')
            need(target_phase in PHASES and PHASES.index(target_phase)<=PHASES.index(row['phase']), 'invalid_backtrack', 'Backtracking cannot skip forward phases')
            found = None
            for table in ('sources','changes','artifacts'):
                candidate = self.s.one(f'SELECT project FROM {table} WHERE id=?',(cause,))
                if candidate: found=candidate; break
            need(found and found['project']==row['project'], 'invalid_cause', 'Backtracking needs a recorded cause in the same project')
            if actor.role != 'owner':
                need(review_receipt, 'review_required', 'Agent backtracking requires observed impact review')
                self.c.g.require_review(review_receipt,program,self.c.p.program_binding(program),{'impact'})
            body = parse_json(row['body']); body.setdefault('history',[]).append({'event':'backtrack','from':row['phase'],'to':target_phase,'reason':reason,'cause':cause,'review':review_receipt,'at':timestamp()})
            self.s.execute('UPDATE programs SET phase=?,revision=revision+1,body=? WHERE id=?', (target_phase,canonical(body).decode(),program))
            self.c.sec.event(row['project'],'program_reopened',actor.id,{'program':program,'from':row['phase'],'to':target_phase,'cause':cause,'tasks_changed':[],'old_closures_preserved':True})
        return self.status(actor,program)
