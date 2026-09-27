"""Reviewed task-definition changes and immutable before/after history.

These are cooperative workflow records, not an authentication boundary. A change
proposal requires an observed impact review against its exact current material.
Resource counters and previous executions are never reset or rewritten.
"""
from __future__ import annotations

from .common import canonical, digest, need, parse_json, text, timestamp, uid

TASK_REVISION_FORMAT = 'daikibo.task-revision-proposal.v1'
PLAN_REVISION_FORMAT = 'daikibo.task-plan-revision-proposal.v1'
TASK_HISTORY_FORMAT = 'daikibo.task-revision.v1'
PLAN_HISTORY_FORMAT = 'daikibo.task-plan-revision.v1'


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(char in '0123456789abcdef' for char in value)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS task_revision_proposals (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
 project TEXT NOT NULL REFERENCES projects(id), from_revision INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 binding TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','applied','withdrawn')),
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_revision_proposals_task ON task_revision_proposals(task,created);
CREATE TABLE IF NOT EXISTS task_revision_history (
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
 project TEXT NOT NULL REFERENCES projects(id), from_revision INTEGER NOT NULL,
 to_revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)),
 digest TEXT NOT NULL, created REAL NOT NULL, UNIQUE(task,to_revision),
 CHECK(to_revision=from_revision+1)
);
CREATE INDEX IF NOT EXISTS task_revision_history_task ON task_revision_history(task,to_revision);
CREATE TRIGGER IF NOT EXISTS task_revision_proposals_immutable
 BEFORE UPDATE OF task,project,from_revision,body,digest,binding,created ON task_revision_proposals
 BEGIN SELECT RAISE(ABORT,'immutable task revision proposal'); END;
CREATE TRIGGER IF NOT EXISTS task_revision_history_no_update BEFORE UPDATE ON task_revision_history
 BEGIN SELECT RAISE(ABORT,'immutable task definition history'); END;
CREATE TRIGGER IF NOT EXISTS task_revision_history_no_delete BEFORE DELETE ON task_revision_history
 BEGIN SELECT RAISE(ABORT,'immutable task definition history'); END;
'''


def task_definition_digest(body):
    """Return the identity of a Task's immutable definition body.

    Task rows and revision-history snapshots carry additional mutable or
    relational fields around the nested ``task.body``.  Typed task-revision
    references always identify that nested definition body, so all live and
    historical readers use this one projection rule.
    """
    need(type(body) is dict, 'integrity_error', 'Task definition body is malformed')
    return digest(body)


class TaskRevisions:
    def __init__(self, workflow):
        self.w = workflow
        self.s, self.k, self.g, self.sec = workflow.s, workflow.k, workflow.g, workflow.sec

    def snapshot(self, actor, task):
        row = self.w.task(actor, task)
        plan = self.s.one('SELECT * FROM plans WHERE task=?', (task,))
        if plan:
            plan['body'] = parse_json(plan['body'])
            need(digest(plan['body']) == plan['digest'], 'integrity_error', 'Frozen test plan changed')
        return {'task': row, 'reads': self.s.all(
            'SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact', (task,)),
            'dependencies': [r['dependency'] for r in self.s.all(
                'SELECT dependency FROM task_deps WHERE task=? ORDER BY dependency', (task,))],
            'test_plan': plan}

    def descendants(self, task):
        return [r['id'] for r in self.s.all('''
            WITH RECURSIVE affected(id) AS (
                SELECT task FROM task_deps WHERE dependency=?
                UNION SELECT d.task FROM task_deps d JOIN affected a ON d.dependency=a.id
            ) SELECT id FROM affected ORDER BY id''', (task,))]

    def validate(self, actor, before, body):
        normalized = self.w.validate_definition(actor, before['project'], body)
        need(before['status'] != 'cancelled', 'invalid_state', 'Cancelled work requires a new task')
        descendants = set(self.descendants(before['id']))
        need(before['id'] not in normalized['dependencies']
             and not descendants.intersection(normalized['dependencies']),
             'dependency_cycle', 'Task revision would create a dependency cycle')
        # A plan correction is not an implicit policy exception.
        order = {'lite': 0, 'standard': 1, 'critical': 2}
        need(order[normalized['risk']] >= order[before['body'].get('risk', 'standard')],
             'risk_downgrade', 'Do not lower task risk through a definition correction')
        for ref in normalized['read_artifacts']:
            need(self.k.artifact(actor, ref)['status'] == 'accepted',
                 'unaccepted_input', 'Revision inputs must be adopted specifications')
        return normalized

    def material(self, actor, task, proposed):
        before = self.snapshot(actor, task)
        inputs = [self.k.artifact(actor, ref) for ref in sorted(proposed['read_artifacts'])]
        dependencies = [self.snapshot(actor, dep) for dep in sorted(proposed['dependencies'])]
        downstream = [self.s.one('SELECT id,project,revision,status,validity FROM tasks WHERE id=?',
                                (ident,), True) for ident in self.descendants(task)]
        need(all(row['project'] == before['task']['project'] for row in downstream),
             'cross_project', 'Dependency graph crosses projects')
        from .planning import PHASES, PHASE_ACTIONS
        programs = []
        for ident in sorted({body['workflow_id'] for body in (before['task']['body'], proposed)
                             if body.get('workflow_id')}):
            row = self.s.one('SELECT * FROM programs WHERE id=?', (ident,), True)
            need(row['project'] == before['task']['project'], 'cross_project',
                 'Task workflow belongs to another project')
            row['body'] = parse_json(row['body'])
            programs.append(row)
        return {'before': before, 'proposed': proposed, 'inputs': inputs,
                'dependencies': dependencies, 'downstream': downstream,
                'policy': self.g.policy(before['task']['project'])['digest'],
                'policy_definition': self.g.policy(before['task']['project']),
                'workflows': programs,
                'workflow_phases': PHASES, 'workflow_instructions': PHASE_ACTIONS}

    def _candidate_material(self, task_row):
        """Resolve the retained candidate identity from the controller rows."""
        ident = task_row.get('candidate')
        if not ident:
            return None
        row = self.s.one('SELECT * FROM candidates WHERE id=?', (ident,), True)
        need(row and row['task'] == task_row['id'] and row['epoch'] == task_row['epoch'],
             'stale_task_proposal', 'Current candidate identity is no longer valid')
        candidate = parse_json(row['body'])
        need(digest(candidate) == row['digest'], 'integrity_error', 'Candidate material changed')
        snapshot = candidate.get('snapshot')
        need(isinstance(snapshot, dict) and isinstance(snapshot.get('digest'), str)
             and snapshot['digest'] == digest({k: v for k, v in snapshot.items() if k != 'digest'}),
             'integrity_error', 'Candidate snapshot material changed')
        return {'id': row['id'], 'task': row['task'], 'epoch': row['epoch'],
                'digest': row['digest'], 'snapshot_digest': snapshot['digest']}

    def _evidence_material(self, task_row, evidence_refs):
        """Capture finite, exact receipt observations for a plan proposal.

        Receipts are retained by reference.  Only their signed identity and a
        bounded result summary enter the immutable proposal body; raw output
        remains in the normal blob store and is checked by ``receipt``.
        """
        need(isinstance(evidence_refs, list) and len(evidence_refs) <= 100,
             'invalid_evidence', 'Evidence references must be a bounded list')
        project, task = task_row['project'], task_row['id']
        binding = self.g.task_binding(task)
        candidate = self._candidate_material(task_row)
        plan_row = self.s.one('SELECT body,digest FROM plans WHERE task=?', (task,), True)
        old_plan = parse_json(plan_row['body']) if plan_row else None
        if plan_row:
            need(old_plan is not None and digest(old_plan) == plan_row['digest'],
                 'integrity_error', 'Frozen test plan changed')
        seen = set(); material = []
        for ident in evidence_refs:
            need(isinstance(ident, str) and ident and ident not in seen,
                 'invalid_evidence', 'Evidence references must be unique receipt IDs')
            seen.add(ident)
            receipt = self.g.receipt(ident)
            need(receipt.get('project') == project and receipt.get('subject') == task
                 and receipt.get('task') == task and receipt.get('binding') == binding,
                 'invalid_evidence', 'Evidence receipt is foreign or stale for this Task')
            need(isinstance(receipt.get('role'), str) and receipt['role'].startswith('test:'),
                 'invalid_evidence', 'Plan revision evidence must be a saved test observation')
            need(receipt.get('epoch') == task_row['epoch'], 'invalid_evidence',
                 'Evidence receipt belongs to another Task epoch')
            if candidate is not None:
                need(receipt.get('snapshot') == candidate['snapshot_digest'],
                     'invalid_evidence', 'Evidence receipt belongs to another candidate snapshot')
            check_id = receipt['role'][5:]
            if old_plan is not None:
                checks = [check for check in old_plan.get('checks', [])
                          if isinstance(check, dict) and check.get('id') == check_id]
                need(len(checks) == 1 and receipt.get('check_digest') == digest(checks[0]),
                     'invalid_evidence', 'Evidence receipt belongs to another frozen test check')
            result = receipt.get('result') if isinstance(receipt.get('result'), dict) else {}
            missing = result.get('missing', [])
            if not isinstance(missing, list):
                missing = [missing]
            summary = {
                'passed': result.get('passed'), 'count': result.get('count'),
                'failed': result.get('failed'), 'missing': missing[:100],
                'missing_count': len(missing), 'skipped': result.get('skipped'),
                'exit_code': receipt.get('exit_code'),
                'timed_out': bool(receipt.get('timed_out')),
                'cancelled': bool(receipt.get('cancelled')),
                'output_overflow': bool(receipt.get('output_overflow')),
                'input_mutated': receipt.get('input_mutated'),
                'assurance': receipt.get('assurance'),
                'simulated': bool(receipt.get('simulated')),
            }
            refs = []
            for key, operation in (('stdout_blob', 'blob.read'), ('stderr_blob', 'blob.read')):
                blob = receipt.get(key)
                if isinstance(blob, str):
                    refs.append({'operation': operation, 'blob': blob, 'offset': 0,
                                 'limit': 65536, 'project': project})
            report = result.get('report_blob')
            if isinstance(report, str):
                refs.append({'operation': 'blob.read', 'blob': report, 'offset': 0,
                             'limit': 65536, 'project': project})
            summary['read_refs'] = refs
            material.append({'id': ident, 'project': project, 'subject': task,
                             'role': receipt.get('role'), 'binding': binding,
                             'run': receipt.get('run'), 'receipt_digest': digest(receipt),
                             'observation_digest': digest({'result': result, 'receipt': ident}),
                             'observation': summary})
        return material

    def plan_material(self, actor, task, proposed_plan, expected_plan_digest, evidence_refs,
                      before=None):
        """Build complete currentness material for an immutable plan proposal."""
        before = before or self.snapshot(actor, task)
        row = before['task']
        need(before['test_plan'] is not None, 'missing_test_plan', 'A frozen plan is required before revision')
        need(before['test_plan']['digest'] == expected_plan_digest,
             'stale_plan', 'Expected frozen plan digest differs')
        validated, _ = self.w.validate_test_plan(actor, task, proposed_plan, current=row)
        need(digest(validated) != expected_plan_digest, 'empty_revision',
             'Use task replan to refresh an unchanged test plan')
        common = self.material(actor, task, row['body'])
        # The proposed Task definition is deliberately the canonical existing
        # body.  It is a context identity, never a caller supplied replacement.
        common.pop('proposed', None)
        common.update({
            'before': before,
            'proposed_task': row['body'],
            'proposed_plan': validated,
            'expected_plan_digest': expected_plan_digest,
            'candidate': self._candidate_material(row),
            'evidence_refs': self._evidence_material(row, evidence_refs),
        })
        return common

    def propose_plan_revision(self, actor, task, expected_revision, expected_plan_digest,
                              body, reason, evidence_refs):
        text(reason, 'task plan revision reason', 20000)
        need(type(expected_revision) is int and expected_revision > 0,
             'invalid_revision', 'Expected task revision must be an integer')
        need(_sha(expected_plan_digest),
             'invalid_digest', 'Expected frozen plan digest must be a SHA-256 string')
        with self.s.transaction():
            row = self.w.task(actor, task)
            actor.require('owner', 'agent', project=row['project'])
            need(row['revision'] == expected_revision, 'stale_revision', 'Task was already revised')
            need(row['status'] != 'cancelled', 'invalid_state',
                 'Cancelled work requires a new Task')
            before = self.snapshot(actor, task)
            material = self.plan_material(actor, task, body, expected_plan_digest, evidence_refs, before)
            data = {'format': PLAN_REVISION_FORMAT, 'reason': reason, 'material': material}
            need(len(canonical(data)) <= 700000, 'revision_context_too_large',
                 'Plan change must fit a bounded impact review')
            ident = uid('TPROP')
            binding = digest({'proposal': ident, 'body': data})
            self.s.execute('INSERT INTO task_revision_proposals VALUES(?,?,?,?,?,?,?,\'proposed\',NULL,?)',
                           (ident, task, row['project'], expected_revision, canonical(data).decode(),
                            digest(data), binding, timestamp()))
            self.sec.event(row['project'], 'task_plan_revision_proposed', actor.id,
                           {'id': ident, 'task': task, 'binding': binding,
                            'reason': reason, 'plan_digest': digest(body)})
        return {'id': ident, 'task': task, 'digest': digest(data), 'binding': binding,
                'status': 'proposed', 'required_review_role': 'impact',
                'would_reassess': [r['id'] for r in material['downstream']],
                'plan_digest': digest(body)}

    def propose(self, actor, task, expected_revision, body, reason):
        text(reason, 'task revision reason', 20000)
        need(type(expected_revision) is int and expected_revision > 0,
             'invalid_revision', 'Expected task revision must be an integer')
        with self.s.transaction():
            row = self.w.task(actor, task)
            actor.require('owner', 'agent', project=row['project'])
            need(row['revision'] == expected_revision, 'stale_revision', 'Task was already revised')
            proposed = self.validate(actor, row, body)
            need(proposed != row['body'], 'empty_revision', 'Use task.replan to refresh unchanged definitions')
            material = self.material(actor, task, proposed)
            data = {'format': TASK_REVISION_FORMAT, 'reason': reason, 'material': material}
            need(len(canonical(data))<=700000,'revision_context_too_large',
                 'Task change must fit a bounded impact review; split work rather than omit inputs')
            ident = uid('TPROP')
            binding = digest({'proposal': ident, 'body': data})
            self.s.execute('INSERT INTO task_revision_proposals VALUES(?,?,?,?,?,?,?,\'proposed\',NULL,?)',
                           (ident, task, row['project'], expected_revision, canonical(data).decode(),
                            digest(data), binding, timestamp()))
            self.sec.event(row['project'], 'task_revision_proposed', actor.id,
                           {'id': ident, 'task': task, 'binding': binding, 'reason': reason})
        return {'id': ident, 'task': task, 'digest': digest(data), 'binding': binding,
                'status': 'proposed', 'required_review_role': 'impact',
                'would_reassess': [r['id'] for r in material['downstream']]}

    def get(self, actor, proposal):
        row = self.s.one('SELECT * FROM task_revision_proposals WHERE id=?', (proposal,), True)
        self.k.project(actor, row['project'])
        row['body'] = parse_json(row['body'])
        need(digest(row['body']) == row['digest']
             and digest({'proposal': proposal, 'body': row['body']}) == row['binding'],
             'integrity_error', 'Task change proposal is inconsistent')
        if row['result']: row['result'] = parse_json(row['result'])
        validate_proposal_record(row,row['project'])
        return row

    def current(self, actor, proposal):
        row = self.get(actor, proposal)
        need(row['status'] == 'proposed', 'invalid_state', 'Proposal is not pending')
        stored = row['body']['material']
        if row['body'].get('format') == PLAN_REVISION_FORMAT:
            before = self.snapshot(actor, row['task'])
            need(before['task']['status'] != 'cancelled', 'stale_task_proposal',
                 'Task was cancelled; submit a new proposal for a new Task')
            current = self.plan_material(
                actor, row['task'], stored['proposed_plan'], stored['expected_plan_digest'],
                [ref['id'] for ref in stored['evidence_refs']], before,
            )
            need(digest(current) == digest(stored), 'stale_task_proposal',
                 'Task, plan, candidate, evidence, dependency graph or policy changed; submit a new proposal')
            need(current['proposed_task'] == before['task']['body'], 'stale_task_proposal',
                 'Plan proposal Task definition identity changed')
            return row
        current = self.material(actor, row['task'], stored['proposed'])
        need(digest(current) == digest(stored), 'stale_task_proposal',
             'Task, inputs, dependency graph or policy changed; submit a new proposal')
        raw = {k: v for k, v in stored['proposed'].items() if k != 'task_kind'}
        need(self.validate(actor, current['before']['task'], raw) == stored['proposed'],
             'stale_task_proposal', 'Current validation no longer agrees with the proposal')
        return row

    def review_subject(self, actor, proposal, role):
        need(role == 'impact', 'invalid_role', 'Task changes need an impact review')
        row = self.current(actor, proposal)
        plan_only = row['body'].get('format') == PLAN_REVISION_FORMAT
        instructions = ('Compare the exact Task identity and complete before/after frozen plan. '
                        'The Task definition body must remain byte-equivalent; preserve old '
                        'failed observations and candidate history. A revised plan is unexecuted '
                        'and cannot certify or waive missing tests. Report blocked when the '
                        'plan change is not justified.') if plan_only else (
                        'Compare the exact before/after task, preserved requirements, '
                        'write boundaries, dependencies and downstream consequences. Do not change '
                        'product requirements or treat missing tests as waived; new checks are required '
                        'after apply. Report blocked when the specification does not justify the change.')
        context = {**row['body'], 'required_coverage': ['task-revision:' + row['digest']],
                   'instructions': instructions}
        if plan_only:
            material = row['body']['material']
            # Keep the proposal's exact before/after plan identities visible at
            # the review boundary instead of requiring a reviewer to infer a
            # replacement from an arbitrary proposal-shaped dictionary.
            context.update({
                'before_plan': material['before']['test_plan'],
                'after_plan': material['proposed_plan'],
                'proposed_task': material['proposed_task'],
                'candidate': material['candidate'],
                'evidence_refs': material['evidence_refs'],
                'downstream': material['downstream'],
            })
        empty = {'format': 'snapshot.v1', 'repos': {}, 'digest': digest({'repos': {}})}
        return row['project'], row['binding'], empty, context, None

    def _record(self, actor, before, after, reason, review, proposal, affected):
        data = {'format': TASK_HISTORY_FORMAT, 'before': before, 'after': after,
                'reason': reason, 'review_receipt': review, 'proposal': proposal,
                'affected_tasks': affected, 'counter_reset': False}
        ident = uid('THIST')
        task = before['task']
        self.s.execute('INSERT INTO task_revision_history VALUES(?,?,?,?,?,?,?,?)',
                       (ident, task['id'], task['project'], task['revision'], after['task']['revision'],
                        canonical(data).decode(), digest(data), timestamp()))
        return ident

    def _record_plan(self, before, after, reason, review, proposal, affected,
                     proposed_task, proposed_plan, evidence_refs):
        data = {
            'format': PLAN_HISTORY_FORMAT, 'before': before, 'after': after,
            'reason': reason, 'review_receipt': review, 'proposal': proposal,
            'affected_tasks': affected, 'counter_reset': False,
            'proposed_task': proposed_task, 'proposed_plan': proposed_plan,
            'evidence_refs': evidence_refs,
        }
        ident = uid('THIST')
        task = before['task']
        self.s.execute('INSERT INTO task_revision_history VALUES(?,?,?,?,?,?,?,?)',
                       (ident, task['id'], task['project'], task['revision'], after['task']['revision'],
                        canonical(data).decode(), digest(data), timestamp()))
        return ident

    def _apply_plan(self, actor, before, material, reason, review, proposal):
        """Apply a reviewed plan-only change in the caller's Store transaction."""
        row = before['task']; task = row['id']; affected = []
        for ident in self.descendants(task):
            current = self.w.task(actor, ident)
            if current['status'] == 'cancelled':
                continue
            self.s.execute('UPDATE tasks SET validity=\'needs_review\',epoch=epoch+1,lease_owner=NULL,'
                           'lease_until=NULL,updated=? WHERE id=?', (timestamp(), ident))
            self.s.execute('INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)',
                           (ident, 'changed_task_definition', task, reason))
            affected.append(ident)

        # Re-acquire the same authoritative input and dependency pins used by
        # ordinary definition revision.  The proposal's currentness check has
        # already proved these identities unchanged, but writing them through
        # the existing boundary keeps the two apply paths equivalent.
        self.s.execute('DELETE FROM task_reads WHERE task=?', (task,))
        for ref in row['body']['read_artifacts']:
            art = self.k.artifact(actor, ref)
            self.s.execute('INSERT INTO task_reads VALUES(?,?,?,?)',
                           (task, ref, art['revision'], art['digest']))
        self.s.execute('DELETE FROM task_deps WHERE task=?', (task,))
        for dep in row['body']['dependencies']:
            self.s.execute('INSERT INTO task_deps VALUES(?,?)', (task, dep))

        # Keep the Task definition body and all attempt/candidate history
        # intact.  Revision/epoch still advance because the frozen plan is a
        # currentness identity consumed by ordinary execution gates.
        self.s.execute("UPDATE tasks SET revision=revision+1,epoch=epoch+1,status='planned',"
                       "validity='current',candidate=NULL,lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?",
                       (timestamp(), task))
        self.s.execute('INSERT INTO plans VALUES(?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET '
                       'body=excluded.body,digest=excluded.digest,approved=excluded.approved,created=excluded.created',
                       (task, canonical(material['proposed_plan']).decode(), digest(material['proposed_plan']),
                        review, timestamp()))
        saved_task = self.w.task(actor, task)
        saved_plan = self.s.one('SELECT * FROM plans WHERE task=?', (task,), True)
        need(saved_plan is not None, 'integrity_error', 'Saved revised plan disappeared')
        saved_plan['body'] = parse_json(saved_plan['body'])
        need(digest(saved_plan['body']) == saved_plan['digest'], 'integrity_error',
             'Saved revised plan digest differs')
        coordinator = self.w.verification_materials
        need(coordinator is not None and callable(getattr(coordinator, 'pin_test_plan', None)),
             'verification_material_unavailable', 'Workflow is not connected to immutable verification materials')
        coordinator.pin_test_plan(
            actor, saved_task['project'], saved_task, saved_plan,
            captured_from={'controller': 'runtime', 'operation': 'task.apply_plan_revision',
                           'proposal': proposal},
        )
        self.s.execute("DELETE FROM blocks WHERE task=? AND kind IN "
                       "('changed_input','review_failed','changed_task_definition')", (task,))
        after = self.snapshot(actor, task)
        history = self._record_plan(before, after, reason, review, proposal, affected,
                                    material['proposed_task'], material['proposed_plan'],
                                    material['evidence_refs'])
        self.sec.event(row['project'], 'task_plan_revised', actor.id,
                       {'task': task, 'old_revision': row['revision'], 'history': history,
                        'proposal': proposal, 'reason': reason, 'affected_tasks': affected,
                        'plan_digest': after['test_plan']['digest']})
        return {'task': task, 'revision': row['revision'] + 1, 'history': history,
                'affected_tasks': affected, 'new_test_plan_required': False,
                'plan_digest': after['test_plan']['digest'], 'plan_revised': True}

    def _apply(self, actor, before, body, reason, review=None, proposal=None):
        row = before['task']; task = row['id']
        affected = []
        for ident in self.descendants(task):
            current = self.w.task(actor, ident)
            if current['status'] == 'cancelled': continue
            self.s.execute('UPDATE tasks SET validity=\'needs_review\',epoch=epoch+1,lease_owner=NULL,'
                           'lease_until=NULL,updated=? WHERE id=?', (timestamp(), ident))
            self.s.execute('INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)',
                           (ident, 'changed_task_definition', task, reason))
            affected.append(ident)
        self.s.execute('DELETE FROM task_reads WHERE task=?', (task,))
        for ref in body['read_artifacts']:
            art = self.k.artifact(actor, ref)
            self.s.execute('INSERT INTO task_reads VALUES(?,?,?,?)', (task, ref, art['revision'], art['digest']))
        self.s.execute('DELETE FROM task_deps WHERE task=?', (task,))
        for dep in body['dependencies']:
            self.s.execute('INSERT INTO task_deps VALUES(?,?)', (task, dep))
        self.s.execute('UPDATE tasks SET body=?,revision=revision+1,epoch=epoch+1,status=\'planned\','
                       'validity=\'current\',candidate=NULL,lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?',
                       (canonical(body).decode(), timestamp(), task))
        self.s.execute('DELETE FROM plans WHERE task=?', (task,))
        # A changed parent is reassessed by this explicit replan. Independent gates
        # still refuse to run until predecessors are actually current and complete.
        # A replan clears definition/review blockers it explicitly replaces,
        # while preserving an unresolved run/lease blocker.  The latter needs
        # an independently reviewed execution-control recovery decision and
        # cannot be erased by changing the task body.
        self.s.execute("DELETE FROM blocks WHERE task=? AND kind IN "
                       "('changed_input','review_failed','changed_task_definition')", (task,))
        history = self._record(actor, before, self.snapshot(actor, task), reason, review, proposal, affected)
        self.sec.event(row['project'], 'task_replanned' if proposal is None else 'task_definition_revised', actor.id,
                       {'task': task, 'old_revision': row['revision'], 'history': history,
                        'proposal': proposal, 'reason': reason, 'affected_tasks': affected})
        return {'task': task, 'revision': row['revision'] + 1, 'history': history,
                'affected_tasks': affected, 'new_test_plan_required': True}

    def apply(self, actor, proposal, expected_digest, review_receipt):
        with self.s.transaction():
            row = self.get(actor, proposal)
            actor.require('owner', 'agent', project=row['project'])
            need(row['digest'] == expected_digest, 'stale_revision', 'Proposal digest differs')
            if row['status'] == 'applied':
                need(row['result']['review_receipt'] == review_receipt, 'idempotency_conflict',
                     'Applied proposal is replayable only with its recorded review')
                return {**row['result'], 'replayed': True}
            row = self.current(actor, proposal)
            refs = self.g.evidence_for(proposal, row['binding'], 'impact')
            need(refs and refs[0]['id'] == review_receipt, 'review_required',
                 'Use the latest impact review, not an older pass after a failure')
            ev = self.g.require_review(review_receipt, proposal, row['binding'], {'impact'})
            need(not ev['result'].get('findings')
                 and 'task-revision:' + row['digest'] in ev['result'].get('covered', []),
                 'review_coverage', 'Review must address this complete task revision without unresolved findings')
            material = row['body']['material']
            if row['body'].get('format') == PLAN_REVISION_FORMAT:
                result = self._apply_plan(actor, material['before'], material,
                                          row['body']['reason'], review_receipt, proposal)
            else:
                result = self._apply(actor, material['before'], material['proposed'],
                                     row['body']['reason'], review_receipt, proposal)
            result.update(proposal=proposal, review_receipt=review_receipt)
            self.s.execute('UPDATE task_revision_proposals SET status=\'applied\',result=? WHERE id=?',
                           (canonical(result).decode(), proposal))
            return result

    def withdraw(self, actor, proposal, expected_digest, reason):
        text(reason, 'withdrawal reason', 20000)
        with self.s.transaction():
            row = self.get(actor, proposal)
            actor.require('owner', 'agent', project=row['project'])
            need(row['digest'] == expected_digest, 'stale_revision', 'Proposal changed')
            need(row['status'] == 'proposed', 'invalid_state', 'Applied changes need a new revision, not deletion')
            self.s.execute('UPDATE task_revision_proposals SET status=\'withdrawn\' WHERE id=?', (proposal,))
            self.sec.event(row['project'], 'task_revision_withdrawn', actor.id,
                           {'proposal': proposal, 'reason': reason})
        return {'proposal': proposal, 'status': 'withdrawn', 'task_unchanged': True}

    def replan(self, actor, task, expected_revision, reason, review_receipt=None):
        text(reason, 'replan reason', 12000)
        with self.s.transaction():
            before = self.snapshot(actor, task); row = before['task']
            actor.require('owner', 'agent', project=row['project'])
            need(type(expected_revision) is int and row['revision'] == expected_revision,
                 'stale_revision', 'Task changed')
            need(row['status'] != 'cancelled', 'invalid_state', 'Cancelled work requires a new task')
            if actor.role != 'owner':
                need(review_receipt, 'review_required', 'Independent impact reassessment required')
                self.g.require_review(review_receipt, task, self.g.task_binding(task), {'impact'})
            body = self.validate(actor, row, {k: v for k, v in row['body'].items() if k != 'task_kind'})
            self._apply(actor, before, body, reason, review_receipt)
        return self.w.task(actor, task)

    def list(self, actor, project, task=None, status='proposed', offset=0, limit=50, expected_snapshot=None):
        self.k.project(actor, project)
        need(status in {'proposed','applied','withdrawn','all'},'invalid_state','Unknown proposal status')
        need(type(offset) is int and offset>=0 and type(limit) is int and 1<=limit<=200,
             'invalid_range','Use a bounded proposal page')
        with self.s.transaction():
            if task: need(self.w.task(actor,task)['project']==project,'cross_project','Task belongs elsewhere')
            rows=self.s.all('SELECT id,task,from_revision,digest,binding,status,created FROM task_revision_proposals '
                           "WHERE project=? AND (? IS NULL OR task=?) AND (?='all' OR status=?) ORDER BY created,id",
                           (project,task,task,status,status))
            stamp=digest(rows)
            need((offset==0 and expected_snapshot is None) or expected_snapshot==stamp,
                 'stale_proposal_list','Proposal list changed between pages')
            return {'project':project,'proposals':rows[offset:offset+limit],'total':len(rows),'snapshot':stamp,
                    'next_offset':offset+limit if offset+limit<len(rows) else None}

    def history(self, actor, task, offset=0, limit=50, expected_snapshot=None):
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
             'invalid_range', 'Use a bounded history page')
        with self.s.transaction():
            current = self.w.task(actor, task)
            rows = self.s.all('SELECT id,from_revision,to_revision,digest,created FROM task_revision_history '
                              'WHERE task=? ORDER BY to_revision', (task,))
            stamp = digest(rows)
            need((offset == 0 and expected_snapshot is None) or expected_snapshot == stamp,
                 'stale_history', 'Task history changed between pages')
            start = rows[0]['from_revision'] if rows else current['revision']
            return {'task': task, 'current_revision': current['revision'], 'snapshot': stamp,
                    'records': rows[offset:offset+limit], 'total': len(rows),
                    'next_offset': offset+limit if offset+limit < len(rows) else None,
                    'history_start_revision': start, 'complete_since_creation': start == 1,
                    'legacy_history_synthesized': False}

    def history_record(self, actor, history, expected_digest=None):
        row = self.s.one('SELECT * FROM task_revision_history WHERE id=?', (history,), True)
        self.k.project(actor, row['project']); row['body'] = parse_json(row['body'])
        need(digest(row['body']) == row['digest'], 'integrity_error', 'Task history was changed')
        validate_history_record(row,row['project'])
        need(expected_digest is None or expected_digest == row['digest'], 'stale_history', 'History digest differs')
        return row


def validate_snapshot(record, task, project, revision):
    row = record['task']
    need(row['id'] == task and row['project'] == project and row['revision'] == revision,
         'invalid_archive', 'Task snapshot identity differs')
    need({r['artifact'] for r in record['reads']} == set(row['body']['read_artifacts'])
         and set(record['dependencies']) == set(row['body']['dependencies']),
         'invalid_archive', 'Historical task body and registered inputs differ')
    plan = record['test_plan']
    if plan:
        need(plan['task'] == task and digest(plan['body']) == plan['digest'],
             'invalid_archive', 'Historical frozen test plan is inconsistent')


def validate_history_record(row, project):
    need(row['project'] == project and digest(row['body']) == row['digest']
         and type(row['from_revision']) is int and row['from_revision'] >= 1
         and type(row['to_revision']) is int and row['to_revision'] == row['from_revision'] + 1,
         'invalid_archive', 'Task history revision/hash differs')
    body = row['body']
    if body.get('format') == PLAN_HISTORY_FORMAT:
        need(body.get('counter_reset') is False, 'invalid_archive', 'Plan history counter changed')
        validate_snapshot(body['before'], row['task'], project, row['from_revision'])
        validate_snapshot(body['after'], row['task'], project, row['to_revision'])
        before_task = body['before']['task']; after_task = body['after']['task']
        need(before_task['body'] == after_task['body'], 'invalid_archive',
             'Plan-only history changed the Task definition')
        need(after_task['revision'] == before_task['revision'] + 1
             and after_task['epoch'] == before_task['epoch'] + 1,
             'invalid_archive', 'Plan-only history did not advance revision and epoch exactly once')
        need(before_task['attempts'] == after_task['attempts'], 'invalid_archive',
             'Plan-only revision reset spent attempts')
        need(after_task['candidate'] is None and after_task['status'] == 'planned'
             and after_task['validity'] == 'current', 'invalid_archive',
             'Plan-only history does not leave a planned current Task')
        need(after_task['body'] == body.get('proposed_task'), 'invalid_archive',
             'Plan-only history proposed Task identity differs')
        after_plan = body['after'].get('test_plan')
        need(isinstance(after_plan, dict) and isinstance(body.get('proposed_plan'), dict)
             and body['proposed_plan'] == after_plan.get('body')
             and digest(body['proposed_plan']) == after_plan.get('digest'),
             'invalid_archive', 'Plan-only history frozen plan differs')
        need(isinstance(body.get('reason'), str) and body['reason'],
             'invalid_archive', 'Plan-only history reason is missing')
        need(isinstance(body.get('proposal'), str) and isinstance(body.get('review_receipt'), str),
             'invalid_archive', 'Plan-only history review linkage is missing')
        _validate_evidence_material(body.get('evidence_refs'), row['task'], project)
        return
    need(body.get('format') == TASK_HISTORY_FORMAT and body['counter_reset'] is False,
         'invalid_archive', 'Invalid task definition history format')
    validate_snapshot(body['before'], row['task'], project, row['from_revision'])
    validate_snapshot(body['after'], row['task'], project, row['to_revision'])
    need(body['after']['task']['attempts'] == body['before']['task']['attempts'],
         'invalid_archive', 'Revision must not reset spent attempts')
    need(body['after']['task']['candidate'] is None and body['after']['test_plan'] is None,
         'invalid_archive', 'A definition change cannot certify previous outputs or tests')


def _validate_evidence_material(refs, task, project):
    need(isinstance(refs, list) and len(refs) <= 100,
         'invalid_archive', 'Evidence material is not a bounded list')
    seen = set()
    for ref in refs:
        need(isinstance(ref, dict), 'invalid_archive', 'Evidence material entry is not an object')
        need(set(ref) == {'id', 'project', 'subject', 'role', 'binding', 'run',
                          'receipt_digest', 'observation_digest', 'observation'},
             'invalid_archive', 'Evidence material entry shape differs')
        ident = ref['id']
        need(isinstance(ident, str) and ident and ident not in seen,
             'invalid_archive', 'Evidence material has duplicate or invalid receipt')
        seen.add(ident)
        need(ref['project'] == project and ref['subject'] == task and
             isinstance(ref['role'], str) and ref['role'].startswith('test:') and isinstance(ref['binding'], str)
             and _sha(ref['binding']) and isinstance(ref['run'], str) and ref['run'],
             'invalid_archive', 'Evidence material identity differs')
        need(_sha(ref['receipt_digest']) and _sha(ref['observation_digest']),
             'invalid_archive', 'Evidence material digest differs')
        obs = ref['observation']
        need(isinstance(obs, dict) and isinstance(obs.get('read_refs'), list),
             'invalid_archive', 'Evidence observation summary is malformed')


def _validate_plan_material_shape(material, task, project, revision):
    need(isinstance(material, dict), 'invalid_archive', 'Plan proposal material is not an object')
    required = {'before', 'inputs', 'dependencies', 'downstream', 'policy',
                'policy_definition', 'workflows', 'workflow_phases', 'workflow_instructions',
                'proposed_task', 'proposed_plan', 'expected_plan_digest', 'candidate', 'evidence_refs'}
    need(set(material) == required, 'invalid_archive', 'Plan proposal material shape differs')
    before = material['before']
    validate_snapshot(before, task, project, revision)
    need(_sha(material['expected_plan_digest']) and before['test_plan'] is not None
         and _sha(before['test_plan']['digest'])
         and before['test_plan']['digest'] == material['expected_plan_digest'],
         'invalid_archive', 'Plan proposal expected frozen plan differs')
    need(material['proposed_task'] == before['task']['body'],
         'invalid_archive', 'Plan proposal Task identity differs')
    need(isinstance(material['proposed_plan'], dict)
         and digest(material['proposed_plan']) != material['expected_plan_digest'],
         'invalid_archive', 'Plan proposal does not contain a changed plan')
    candidate = material['candidate']
    if candidate is not None:
        need(isinstance(candidate, dict) and set(candidate) == {'id', 'task', 'epoch', 'digest', 'snapshot_digest'},
             'invalid_archive', 'Plan proposal candidate identity differs')
        need(candidate['task'] == task and candidate['epoch'] == before['task']['epoch']
             and all(_sha(candidate[key])
                     for key in ('digest', 'snapshot_digest')),
             'invalid_archive', 'Plan proposal candidate is stale')
    _validate_evidence_material(material['evidence_refs'], task, project)


def validate_proposal_record(row, project):
    need(row['project'] == project and digest(row['body']) == row['digest']
         and digest({'proposal': row['id'], 'body': row['body']}) == row['binding'],
         'invalid_archive', 'Task revision proposal binding differs')
    body = row['body']
    if body.get('format') == PLAN_REVISION_FORMAT:
        _validate_plan_material_shape(body.get('material'), row['task'], project, row['from_revision'])
        need(isinstance(body.get('reason'), str) and body['reason'],
             'invalid_archive', 'Plan proposal reason is missing')
        if row['status'] == 'applied':
            need(row['result'] and row['result']['proposal'] == row['id']
                 and row['result']['task'] == row['task']
                 and row['result']['revision'] == row['from_revision'] + 1
                 and row['result'].get('new_test_plan_required') is False
                 and row['result'].get('plan_revised') is True,
                 'invalid_archive', 'Applied plan proposal result differs')
        else:
            need(row['status'] in {'proposed', 'withdrawn'} and row['result'] is None,
                 'invalid_archive', 'Unexpected result for unapplied plan proposal')
        return
    need(body.get('format') == TASK_REVISION_FORMAT, 'invalid_archive', 'Unknown task proposal format')
    validate_snapshot(body['material']['before'], row['task'], project, row['from_revision'])
    if row['status'] == 'applied':
        need(row['result'] and row['result']['proposal'] == row['id'] and row['result']['task'] == row['task']
             and row['result']['revision'] == row['from_revision'] + 1,
             'invalid_archive', 'Applied proposal result differs')
    else:
        need(row['status'] in {'proposed', 'withdrawn'} and row['result'] is None,
             'invalid_archive', 'Unexpected result for unapplied proposal')
