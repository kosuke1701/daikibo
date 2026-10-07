"""D01: regular Claude Code conversations use the same durable workflow API.

This is a cooperative bridge, not an identity attestation service. A reported user
utterance is kept verbatim; a decision still needs the current proposal digest,
a later source, an exact quote and the existing consistency-review workflow.
"""
from __future__ import annotations
from pathlib import Path
from .common import Actor, canonical, digest, need, parse_json, text, timestamp, uid


class Native:
    def __init__(self, control):
        self.c, self.s = control, control.s

    def _session(self, session):
        row = self.s.one('SELECT * FROM native_sessions WHERE id=?', (session,), True)
        row['body'] = parse_json(row['body'])
        return row

    def _save(self, row):
        self.s.execute('UPDATE native_sessions SET body=?,updated=? WHERE id=?',
                       (canonical(row['body']).decode(), timestamp(), row['id']))

    def lookup(self, actor, cwd):
        path = str(Path(cwd).resolve())
        row = self.s.one('SELECT id,project,client FROM native_sessions WHERE cwd=? ORDER BY updated DESC LIMIT 1', (path,))
        return {'attached': bool(row), 'workspace': path, **(row or {})}

    def attach(self, actor, session, cwd, project=None, name='New project', client='claude', register_repository=True):
        text(session, 'session ID', 300); text(client, 'client', 50)
        path = Path(cwd).resolve()
        need(path.is_dir(), 'workspace_missing', 'Workspace directory does not exist')
        with self.s.transaction():
            prior = self.s.one('SELECT * FROM native_sessions WHERE id=?', (session,))
            if prior:
                need(prior['cwd'] == str(path) and (not project or project == prior['project']),
                     'session_conflict', 'Session ID is already associated with another workspace/project')
                return self.context(actor, session)
            old = self.lookup(actor, str(path))
            project = project or old.get('project') or self.c.k.create_project(actor, name)['id']
            self.c.k.project(actor, project)
            self.s.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?)',
                           (session, project, client, str(path), '{}', timestamp()))
            if register_repository and not self.s.one('SELECT id FROM repos WHERE project=? AND path=?', (project, str(path))):
                repos = self.c.repository_list(actor, project)['repositories']
                base = 'workspace'; label = base if not repos else base + '-' + str(len(repos) + 1)
                self.c.sn.register(actor, project, label, str(path))
            self.c.sec.event(project, 'native_session_attached', actor.id,
                             {'session': session, 'client': client, 'cwd': str(path), 'authenticated_identity': False})
        return self.context(actor, session)

    def input(self, actor, session, content, turn_id=None, origin='skill-relay', start_program=False):
        row = self._session(session); text(content, 'user input', 8_000_000)
        need(type(start_program) is bool,'invalid_option','start_program must be Boolean')
        need(origin in {'skill-relay', 'claude-hook', 'interactive-terminal'}, 'invalid_origin', 'Unknown input origin')
        turn_id = turn_id or uid('TURN'); text(turn_id, 'turn ID', 500)
        with self.s.transaction():
            previous = self.s.one('SELECT * FROM native_turns WHERE session=? AND turn_id=?', (session, turn_id))
            if previous:
                need(previous['digest'] == digest(content.encode()), 'idempotency_conflict', 'Turn ID reused with different text')
                recorded=self.s.one("SELECT body FROM events WHERE kind='native_user_input_recorded' AND json_extract(body,'$.source')=? ORDER BY seq DESC LIMIT 1",(previous['source'],))
                options=parse_json(recorded['body']) if recorded else {}
                need(options.get('start_program',False)==start_program,'idempotency_conflict','Turn ID reused with a different program-start request')
                return {'session': session, 'project': row['project'], 'source': previous['source'], 'replayed': True}
            human = Actor('reported-user:' + session, 'owner')
            result = self.c.i.intake(human, content, project=row['project'], bounded=True, start_program=start_program)
            source = result['source']['id']
            self.s.execute('INSERT INTO native_turns VALUES(?,?,?,?,?,?)',
                           (session, turn_id, source, digest(content.encode()), origin, timestamp()))
            row['body']['last_source'] = source; row['body']['last_turn'] = turn_id
            self._save(row)
            self.c.sec.event(row['project'], 'native_user_input_recorded', actor.id,
                             {'session': session, 'source': source, 'origin': origin, 'start_program':start_program,'identity_claim': 'reported-not-authenticated'})
        return {'session': session, 'project': row['project'], 'source': source, 'workflow': result['workflow'],
                'notifications': result['mandatory_notifications']['items'], 'notification_catalog':result['mandatory_notifications'],
                'notification_count':result['mandatory_notifications']['total'], 'notifications_are_index':True,'replayed': False}

    def context(self, actor, session):
        row = self._session(session); project = row['project']
        programs = self.c.nav.programs(actor, project, limit=10)
        counts = self.s.all('SELECT status,validity,count(*) AS count FROM tasks WHERE project=? GROUP BY status,validity', (project,))
        notices = self.c.nav.inbox(actor, project, limit=30)
        progress = self.c.execution_controls.project_progress(actor, project, limit=100)
        traceability = self.c.traceability.list(actor, project, limit=10)
        return {'session': session, 'project': project, 'workspace': row['cwd'], 'programs': programs['items'],
                'program_catalog': programs, 'task_counts': counts, 'last_source': row['body'].get('last_source'),
                'execution_progress': progress,
                'traceability': traceability['revisions'], 'traceability_catalog': traceability,
                'notifications': notices['items'], 'notification_count': notices['total'], 'notification_catalog':notices,
                'notifications_are_index': True, 'notification_read_operation': 'inbox.read',
                'more_notifications_operation': 'inbox.catalog' if notices['next_offset'] is not None else None,
                'execution_model': 'cooperative-single-user',
                'next': 'Read pending notices by digest with inbox.read and follow every catalog cursor. Displaying an index never acknowledges any notice. Discuss missing product meaning; use native.actions for proposals, job.submit for observed work, and native.present_decision before user adjudication.',
                'completion_rule': 'Chat text never completes work. task.complete, delivery.certify and program.finish must accept current observed evidence.'}

    def actions(self, actor, session, actions, source=None):
        """Execute bounded proposals from the already-running conversational agent."""
        from .supervisor import ALLOWED
        row = self._session(session); project = row['project']
        need(isinstance(actions, list) and 0 < len(actions) <= 20, 'invalid_actions', 'Supply 1..20 structured actions')
        if source:
            need(self.s.one('SELECT source FROM native_turns WHERE session=? AND source=?', (session, source)),
                 'wrong_source', 'Source was not recorded in this session')
        labels, results = {}, []
        # Configuration is permitted in the same conversation; it is not a product approval.
        configuration = {'repository.register', 'adapter.register', 'automation.configure', 'delivery.configure', 'document.import', 'execution.configure_limits', 'remote.configure', 'remote.publish'}
        allowed = ALLOWED | configuration | {'source.partition', 'source.partition_status', 'source.packet', 'task.cancel', 'change.withdraw'}
        agent = Actor('native-agent:' + session, 'agent', project)
        def resolve(value):
            if isinstance(value, dict) and set(value) == {'$ref'}:
                parts = value['$ref'].split('.'); need(parts[0] in labels, 'missing_reference', 'Earlier action label is missing')
                found = labels[parts[0]]
                for part in parts[1:]:
                    need(isinstance(found, dict) and part in found, 'missing_reference', 'Missing result field')
                    found = found[part]
                return found
            if isinstance(value, dict): return {k: resolve(v) for k,v in value.items()}
            if isinstance(value, list): return [resolve(v) for v in value]
            return value
        from .common import Fault, obj
        from .action_batches import preflight
        rejected = preflight(self.c, agent, actions, allowed)
        if rejected:
            self.c.sec.event(project,'native_proposals_rejected',agent.id,
                             {'session':session,'source':source,'failure':rejected,'executed':0})
            return {'actions': [rejected], 'all_applied': False, 'executed': 0}
        for action in actions:
            try:
                obj(action, required=('method','params'), optional=('as',))
                method = action['method']; need(method in allowed, 'workflow_boundary', 'Operation is not a proposal/configuration action')
                params = resolve(action['params'])
                need(isinstance(params,dict),'invalid_params','Resolved arguments must be a JSON object')
                need(params.get('project', project) == project, 'cross_project', 'Session proposals cannot cross projects')
                caller = Actor(agent.id, 'owner', project) if method in configuration else agent
                result = self.c.invoke(caller, method, params)
                if action.get('as'):
                    need(action['as'] not in labels, 'duplicate_label', 'Result label already exists')
                    labels[action['as']] = result
                results.append({'method': method, 'result': result})
            except Fault as exc:
                results.append({'method': action.get('method'), 'error': exc.as_dict()}); break
        with self.s.transaction():
            self.c.sec.event(project, 'native_proposals_processed', agent.id,
                             {'session': session, 'source': source, 'results': results})
        return {'actions': results, 'all_applied': len(results)==len(actions) and not any('error' in x for x in results)}

    def present_decision(self, actor, session, decision):
        with self.s.transaction():
            row = self._session(session); value = self.c.decision_get(actor, decision)
            need(value['project'] == row['project'], 'cross_project', 'Decision belongs to another project')
            expiry = self.c.p._expire_due_decision(row['project'], decision, actor=actor.id)
            if expiry and expiry.get('expired'):
                return expiry
            value = self.c.decision_get(actor, decision)
            need(value['status'] in {'pending','provisional','deferred','decision_received'},'stale_decision',
                 'Only a decision awaiting or receiving a response can be presented')
            event=self.c.sec.event(row['project'],'native_decision_presented',actor.id,
                {'session':session,'decision':decision,'digest':value['digest']})
            seq=self.s.one('SELECT seq FROM events WHERE id=?',(event,),True)['seq']
            row['body'].setdefault('presented', {})[decision] = {'digest': value['digest'], 'at': timestamp(),
                'seq':seq,'consumed':False}
            self._save(row)
        return {'decision': decision, 'expected_digest': value['digest'], 'presentation_seq':seq,'proposal': value['body'],
                'instruction': 'Ask the user now; record the subsequent answer with native.input. Do not interpret displaying this proposal as approval.'}

    def _quote(self, row, source, quote, after=0):
        text(quote, 'exact user quotation', 100000)
        turn = self.s.one('SELECT * FROM native_turns WHERE session=? AND source=?', (row['id'], source), True)
        if isinstance(after,dict) and type(after.get('seq')) is int:
            observed=self.s.one("SELECT seq FROM events WHERE project=? AND kind='native_user_input_recorded' "
                                "AND json_extract(body,'$.session')=? AND json_extract(body,'$.source')=? ORDER BY seq DESC LIMIT 1",
                                (row['project'],row['id'],source))
            need(observed and observed['seq']>after['seq'],'stale_user_input',
                 'The answer was recorded before this proposal presentation')
        elif after is not None:
            need(turn['created'] > after, 'stale_user_input', 'The answer predates the proposal shown to the user')
        original = self.s.one('SELECT blob FROM sources WHERE id=?', (source,), True)
        need(quote in self.s.blob_get(original['blob']).decode(), 'quote_mismatch', 'Quote must appear verbatim in the recorded user input')
        return turn

    def respond(self, actor, session, decision, expected_digest, source, choice, quote):
        with self.s.transaction():
            row = self._session(session); shown = row['body'].get('presented', {}).get(decision)
            need(shown and shown['digest']==expected_digest, 'proposal_not_presented', 'Present this exact proposal before recording its answer')
            decision_row=self.s.one('SELECT * FROM decisions WHERE id=?',(decision,),True)
            need(decision_row['project']==row['project'],'cross_project','Decision belongs elsewhere')
            expiry=self.c.p._expire_due_decision(row['project'],decision,actor=actor.id)
            if expiry and expiry.get('expired'):
                return expiry
            decision_row=self.s.one('SELECT * FROM decisions WHERE id=?',(decision,),True)
            self._quote(row, source, quote, shown if type(shown.get('seq')) is int else shown.get('at'))

            # A reported answer is single-use for changing the decision. It
            # can, however, be corrected when the same current answer used a
            # retained source and the user wants to bind a more precise quote
            # from that exact source. The immutable response event must prove
            # that the proposal, choice and source are still the current ones.
            # A newly displayed answer still requires a source recorded after
            # that presentation, and a different choice never reuses evidence.
            evidence=self.c.p.response_evidence(decision)
            prior=evidence['body'] if evidence else {}
            source_row=self.s.one('SELECT project,trust,blob FROM sources WHERE id=?',(source,),True)
            content=self.s.blob_get(source_row['blob']).decode()
            start=content.find(quote)
            prior_quote=prior.get('quote')
            prior_start=prior.get('start')
            prior_end=prior.get('end')
            prior_range_valid=(isinstance(prior_quote,str) and type(prior_start) is int and
                type(prior_end) is int and 0<=prior_start<=prior_end<=len(content) and
                prior_end==prior_start+len(prior_quote) and content[prior_start:prior_end]==prior_quote)
            current_answer=(decision_row['status']=='decision_received' and
                decision_row['digest']==expected_digest and prior.get('digest')==expected_digest and
                prior.get('choice')==decision_row['response'] and prior.get('source')==decision_row['source'] and
                prior.get('source_digest')==source_row['blob'] and
                source_row['project']==row['project'] and source_row['trust']=='human' and
                prior_range_valid)
            reusing_current_source=(current_answer and decision_row['source']==source)
            if shown.get('consumed',False) or reusing_current_source:
                same_answer=(reusing_current_source and decision_row['response']==choice and start>=0)
                need(same_answer,'stale_user_input',
                     'This presentation already recorded an answer; use a fresh observation to change it')
                if prior_quote==quote and prior_start==start and prior_end==start+len(quote):
                    self.c.p._validate_response_current(Actor('reported-user:' + session, 'owner'),
                                                        decision_row, parse_json(decision_row['body']))
                    shown['consumed']=True
                    row['body'].setdefault('presented',{})[decision]=shown
                    self._save(row)
                    return {'id':decision,'status':'decision_received','consistency_recheck_required':True}
            result = self.c.p.respond(Actor('reported-user:' + session, 'owner'), decision, expected_digest, choice, quote, source=source)
            if result.get('expired') or result.get('answered') is False:
                return result
            shown['consumed']=True
            row['body'].setdefault('presented',{})[decision]=shown
            self._save(row)
            self.c.sec.event(row['project'], 'native_decision_response', actor.id,
                             {'session': session, 'decision': decision, 'input_source': source, 'quote': quote,
                              'proposal_digest': expected_digest, 'identity_authenticated': False})
        return result

    def acknowledge(self, actor, session, item, source, quote, expected_digest=None):
        with self.s.transaction():
            row = self._session(session); notice = self.s.one('SELECT * FROM inbox WHERE id=?', (item,), True)
            need(notice['project']==row['project'], 'cross_project', 'Notification belongs elsewhere')
            current_digest=digest(notice['body'].encode())
            need(expected_digest is None or expected_digest==current_digest,
                 'stale_notice','Notification content changed')
            # The shared inbox layer checks durable publication/source event
            # order. Keep this session check limited to membership and quote
            # fidelity so equal timestamps and clock rollback stay valid.
            self._quote(row, source, quote, after=None)
            return self.c.i.acknowledge(Actor('reported-user:' + session, 'owner'), item, quote,
                                        source=source,expected_digest=current_digest)

    def completion(self, actor, session, subject):
        row = self._session(session); task = self.s.one('SELECT * FROM tasks WHERE id=?', (subject,))
        delivery = self.s.one('SELECT * FROM deliveries WHERE id=?', (subject,))
        program = self.s.one('SELECT * FROM programs WHERE id=?', (subject,))
        scoped = self.s.one('SELECT project FROM workstreams WHERE id=?',(subject,))
        if scoped:
            need(scoped['project']==row['project'],'cross_project','Wrong project')
            report=self.c.workstreams.completion(actor,subject)
            latest=self.s.one("SELECT body FROM workstream_records WHERE scope=? AND kind='finish' ORDER BY created DESC,id DESC LIMIT 1",(subject,))
            recorded=bool(latest and parse_json(latest['body'])['binding']==report['binding'])
            blockers=report['failures']+([] if recorded else [{'code':'workstream_finish_required'}])
            result={'subject':subject,'completed':not blockers,'state':'work_verified' if not blockers else 'incomplete',
                    'blockers':blockers,'completion_kind':'delegated_work_only','deploy_ready':False,
                    'instruction':'Do not call this a completed project. Root program integration and delivery certification remain mandatory.'}
            with self.s.transaction():
                row['body']['completion_request']=result;self._save(row)
            return result
        if program:
            need(program['project']==row['project'],'cross_project','Wrong project')
            report=self.c.lifecycle.completion(actor,subject)
            result={'subject':subject,'completed':report['completed'],'state':'completed' if report['completed'] else 'incomplete','blockers':report['failures']}
            with self.s.transaction():
                row['body']['completion_request']=result;self._save(row)
            return result
        need(task or delivery, 'unknown_subject', 'Only an actual task/delivery/program can have a completion report')
        item = task or delivery; need(item['project']==row['project'], 'cross_project', 'Wrong project')
        if task:
            blockers = self.c.g.evaluate_task(actor,subject,gate='recheck')['failures']
        else:
            blocker_details = []
            blockers = [] if delivery['status'] in {'verified','delivered'} else ['delivery_not_certified']
            if not blockers:
                from .common import Fault
                try: self.c.d.certify(actor,subject,check_only=True)
                except Fault as exc:
                    blockers.append('delivery_recheck:'+exc.code)
                    blocker_details.append({'subject': subject, 'binding': delivery['digest'], **exc.as_dict()})
        result = {'subject': subject, 'completed': not blockers, 'state': item['status'], 'blockers': blockers}
        if delivery: result['blocker_details'] = blocker_details
        with self.s.transaction():
            row['body']['completion_request'] = result; self._save(row)
        return result

    def stop_feedback(self, actor, session, stop_hook_active=False):
        row = self._session(session); pending = row['body'].get('completion_request')
        if pending:
            current = self.completion(actor, session, pending['subject'])
            if not current['completed'] and not stop_hook_active:
                return {'decision':'block','reason':'daikibo_dev has not certified this completion. '+canonical(current).decode()+'. Resolve the gates or clearly report a checkpoint/blocked state; do not claim completion.'}
        # A normal design discussion may end. Never force an infinite "continue" loop.
        return {}
