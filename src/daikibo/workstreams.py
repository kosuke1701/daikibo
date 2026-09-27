"""Scoped execution under a lossless root plan; not an independent release claim."""
from __future__ import annotations

from .common import Fault, canonical, digest, need, parse_json, strings, text, timestamp, uid
from .packets import slices

SCHEMA = """
CREATE TABLE IF NOT EXISTS workstreams(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), parent TEXT REFERENCES workstreams(id),
 previous TEXT REFERENCES workstreams(id), breakdown TEXT NOT NULL REFERENCES breakdowns(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','active','superseded','withdrawn')), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workstreams_program ON workstreams(program,status,parent);
CREATE TABLE IF NOT EXISTS workstream_packets(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES workstreams(id), ordinal INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, UNIQUE(scope,ordinal)
);
CREATE TABLE IF NOT EXISTS workstream_records(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES workstreams(id), project TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('adopt','finish','withdraw')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workstream_records_scope ON workstream_records(scope,kind,created);
CREATE TRIGGER IF NOT EXISTS workstreams_immutable BEFORE UPDATE OF project,program,parent,previous,breakdown,body,digest,created ON workstreams
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope'); END;
CREATE TRIGGER IF NOT EXISTS workstream_packets_immutable BEFORE UPDATE ON workstream_packets
 BEGIN SELECT RAISE(ABORT,'immutable scope review packet'); END;
CREATE TRIGGER IF NOT EXISTS workstream_records_immutable BEFORE UPDATE ON workstream_records
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope history'); END;
CREATE TRIGGER IF NOT EXISTS workstream_records_no_delete BEFORE DELETE ON workstream_records
 BEGIN SELECT RAISE(ABORT,'immutable delegated scope history'); END;
"""
MAX_BYTES = 128 * 1024 * 1024


class Workstreams:
    def __init__(self, control):
        self.c, self.s = control, control.s

    def _row(self, actor, scope):
        row = self.s.one('SELECT * FROM workstreams WHERE id=?', (scope,), True)
        self.c.k.project(actor, row['project'])
        row['body'] = parse_json(row['body'], limit=MAX_BYTES)
        need(digest(row['body']) == row['digest'], 'integrity_error', 'Delegated scope differs from its recorded body')
        return row

    def _global(self, project, *, ensure_policy=True):
        # Global constraints remain mandatory even if the leaf Task does not read them.
        return {'policy': self.c.g.policy(project, create=ensure_policy)['digest'], 'constraints': self.s.all(
            "SELECT id,revision,digest FROM artifacts WHERE project=? AND status='accepted' "
            "AND (json_extract(body,'$.critical')=1 OR json_type(body,'$.constraints') IS NOT NULL) ORDER BY id", (project,))}

    def _selection(self, actor, program, unit_ids, *, ensure_policy=True):
        active = self.c.breakdowns.active(actor, program)
        need(active, 'breakdown_required', 'Adopt the complete root breakdown before delegating')
        root = self.c.breakdowns._row(actor, active['id'])
        body = root['body']; by_id = {u['id']: u for u in body['units']}
        need(set(unit_ids) <= set(body['structure']['leaf_units']), 'invalid_scope_units', 'Only existing leaf units may be delegated')
        tasks, obligations, bindings, material = [], [], {}, []
        for ident in sorted(unit_ids):
            value = self.c.breakdowns._material(
                actor, root['project'], body, ident, by_id,
                ensure_policy=ensure_policy,
            )
            need(digest(value) == body['material_bindings'][ident], 'stale_scope_input', 'Selected unit must match its adopted root plan', ident)
            material.append(value); bindings[ident] = digest(value)
            tasks.extend(by_id[ident]['tasks']); obligations.extend(by_id[ident]['obligations'])
        external = []
        owned_tasks = set(tasks)
        for task in sorted(tasks):
            for dep in self.s.all('SELECT dependency FROM task_deps WHERE task=? ORDER BY dependency', (task,)):
                if dep['dependency'] not in owned_tasks:
                    external.append({'task': task, 'dependency': dep['dependency'],
                                     'definition_digest': digest(self.c.breakdowns._task_definition(actor, dep['dependency']))})
        return root, {'units': sorted(unit_ids), 'tasks': sorted(tasks),
                      'obligations': sorted(obligations, key=lambda o: (o['requirement'], o['acceptance'])),
                      'unit_bindings': bindings, 'external_dependencies': external,
                      'global': self._global(root['project'], ensure_policy=ensure_policy)}, material

    def _lineage(self, actor, row):
        seen = {row['id']}; current = row
        while current['parent']:
            need(current['parent'] not in seen, 'scope_cycle', 'Cycle in delegated scopes')
            parent = self._row(actor, current['parent']); seen.add(parent['id'])
            need(parent['status'] == 'active' and parent['program'] == row['program']
                 and parent['breakdown'] == row['breakdown'], 'stale_parent_scope', 'Parent is no longer the active scope for this plan')
            need(set(current['body']['selection']['units']) <= set(parent['body']['selection']['units']),
                 'outside_parent_scope', 'Child cannot add obligations outside its parent')
            current = parent

    def _check(self, actor, row, *, readonly=False):
        need(row['status'] in {'proposed', 'active'}, 'scope_closed', 'Scope is no longer current')
        self._lineage(actor, row)
        if row['status']=='active':
            records=self.s.all("SELECT body,digest FROM workstream_records WHERE scope=? AND kind='adopt'",(row['id'],))
            need(len(records)==1 and digest(parse_json(records[0]['body']))==records[0]['digest'],
                 'missing_adoption_record','Active scope must retain its adoption record')
        active = self.c.breakdowns.active(actor, row['program'])
        need(active and active['id'] == row['breakdown'], 'stale_root_plan', 'Root plan changed; propose a reviewed replacement')
        _, selection, _ = self._selection(
            actor, row['program'], row['body']['selection']['units'],
            ensure_policy=not readonly,
        )
        need(selection == row['body']['selection'], 'stale_scope_input', 'Scope inputs, boundary prerequisites, or global constraints changed')

    def _no_overlap(self, row):
        for other in self.s.all("SELECT id,body FROM workstreams WHERE program=? AND status='active' AND parent IS ? AND id!=?", (row['program'], row['parent'], row['previous'] or '')):
            if other['id'] == row['id']: continue
            common = set(row['body']['selection']['units']) & set(parse_json(other['body'], limit=MAX_BYTES)['selection']['units'])
            need(not common, 'overlapping_scope', 'Active sibling scopes cannot own the same leaf unit', {'scope': other['id'], 'units': sorted(common)})

    def propose(self, actor, program, title, rationale, unit_ids, parent=None, previous=None, byte_budget=24000):
        strings(unit_ids, 'leaf units', nonempty=True)
        need(len(unit_ids) == len(set(unit_ids)) and len(unit_ids) <= 10000, 'invalid_scope_units', 'Unique bounded leaf unit IDs are required')
        text(title, 'scope title', 400); text(rationale, 'delegation rationale', 20000)
        need(type(byte_budget) is int and 4096 <= byte_budget <= 100000, 'invalid_budget', 'Packet budget must be 4096..100000 UTF-8 bytes')
        with self.s.transaction():
            root, selection, materials = self._selection(actor, program, unit_ids)
            actor.require('owner', 'agent', project=root['project'])
            report = self.c.breakdowns.audit(actor, root['id'])
            need(report['current'], 'breakdown_gate_denied', 'Root plan must be fully reviewed and current before delegation', report['failures'])
            if previous:
                old = self._row(actor, previous)
                need(old['status'] == 'active' and old['program'] == program and old['parent'] == parent,
                     'invalid_scope_replacement', 'Replace an active scope under the same parent')
                need(not self.s.one("SELECT id FROM workstreams WHERE parent=? AND status='active'", (previous,)),
                     'active_child_scopes', 'Withdraw or explicitly replace children before their parent')
            ident = uid('WORKSTREAM')
            body = {'format': 'daikibo.workstream.v1', 'title': title, 'rationale': rationale,
                    'selection': selection, 'packet_manifest': [],
                    'scope_reduced': False, 'deploy_ready': False}
            row = {'id': ident, 'program': program, 'project': root['project'], 'parent': parent,
                   'previous': previous, 'breakdown': root['id'], 'body': body, 'status': 'proposed'}
            self._lineage(actor, row); self._no_overlap(row)
            # Retained packet manifest is authoritative; missing packets never count as a full review.
            material = {'program': program, 'parent': parent, 'previous': previous, 'breakdown': root['id'],
                        'title': title, 'rationale': rationale, 'selection': selection, 'units': materials,
                        'instruction': 'Delegated work, not a release. Check scope/ownership, exact obligations, shared contracts and external prerequisites. Root requirements remain mandatory.'}
            encoded = canonical(material).decode()
            need(len(encoded.encode()) <= MAX_BYTES, 'context_insufficient', 'Complete scope material exceeds explicit capacity; delegate smaller units without deleting root scope')
            packets = []
            for start, end, fragment in slices(encoded, (byte_budget - 1600)//2):
                marker = 'WSPART-' + digest([ident, digest(material), start, end])
                packet = {'format': 'daikibo.workstream-review.v1', 'scope': ident, 'program': program,
                          'start': start, 'end': end, 'total_characters': len(encoded), 'material_digest': digest(material),
                          'serialized_fragment': fragment, 'required_coverage': [marker],
                          'instructions': 'A fragment, not the entire program. Inspect related packets; if insufficient return blocked. Separate design and trace runs are required.'}
                need(len(canonical(packet)) <= byte_budget, 'context_insufficient', 'Scope packet exceeds input budget')
                packet_id = uid('WPACK'); h = digest(packet)
                packets.append((packet_id, ident, len(packets), canonical(packet).decode(), h))
                body['packet_manifest'].append({'id': packet_id, 'digest': h})
            body['material_digest'] = digest(material)
            self.s.execute('INSERT INTO workstreams VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (ident, root['project'], program, parent, previous, root['id'], canonical(body).decode(), digest(body), 'proposed', timestamp()))
            for packet in packets: self.s.execute('INSERT INTO workstream_packets VALUES(?,?,?,?,?)', packet)
            self.c.sec.event(root['project'], 'workstream_proposed', actor.id, {'scope': ident, 'program': program, 'parent': parent, 'units': selection['units'], 'requirements_deleted': []})
        return {'scope': ident, 'status': 'proposed', 'packet_count': len(packets), 'tasks': len(selection['tasks']), 'obligations': len(selection['obligations']), 'deploy_ready': False}

    def _packets(self, row):
        packets = self.s.all('SELECT * FROM workstream_packets WHERE scope=? ORDER BY ordinal', (row['id'],))
        need([{'id': p['id'], 'digest': p['digest']} for p in packets] == row['body']['packet_manifest'],
             'missing_review_fragments', 'Complete scope packet manifest is required')
        cursor = 0; fragments = []
        for ordinal, p in enumerate(packets):
            body = parse_json(p['body'])
            need(p['ordinal'] == ordinal and digest(body) == p['digest'] and body['scope'] == row['id']
                 and body['program'] == row['program'] and body['material_digest'] == row['body']['material_digest']
                 and body['start'] == cursor and body['end'] == cursor + len(body['serialized_fragment']),
                 'integrity_error', 'Scope fragment identity, digest or range differs')
            cursor = body['end']; fragments.append(body['serialized_fragment']); p['body'] = body
        need(packets and cursor == packets[-1]['body']['total_characters']
             and all(p['body']['total_characters'] == cursor for p in packets)
             and digest(''.join(fragments).encode()) == row['body']['material_digest'],
             'missing_review_fragments', 'Scope review material is incomplete')
        return packets

    def _reviews(self, row):
        verified = []
        for p in self._packets(row):
            runs = set()
            for role in ('design', 'trace'):
                refs = self.c.g.evidence_for(p['id'], p['digest'], role)
                need(refs, 'review_required', 'Observed design and trace reviews are required', role)
                ev = self.c.g.require_review(refs[0]['id'], p['id'], p['digest'], {role})
                need(set(p['body']['required_coverage']) <= set(ev['result']['covered']), 'review_coverage', 'Reviewer must inspect the exact scope fragment')
                need(ev['run'] not in runs, 'independent_review', 'Scope roles require separate actual runs')
                need(not ev['result']['findings'], 'unresolved_findings', 'Resolve scope findings before adoption')
                runs.add(ev['run']); verified.append({'packet': p['id'], 'role': role, 'receipt': ev['id']})
        return verified

    def packet(self, actor, packet):
        value = self.s.one('SELECT * FROM workstream_packets WHERE id=?', (packet,), True)
        row = self._row(actor, value['scope'])
        with self.s.transaction():
            self._check(actor, row)
            for candidate in self._packets(row):
                if candidate['id'] == packet: return candidate
        raise Fault('not_found', 'Scope packet not found')

    def review_subject(self, actor, packet, role):
        need(role in {'design', 'trace'}, 'invalid_role', 'Scope packets need design and trace roles')
        value = self.packet(actor, packet); row = self._row(actor, value['scope'])
        empty = {'format': 'snapshot.v1', 'repos': {}, 'digest': digest({'repos': {}})}
        return row['project'], value['digest'], empty, value['body'], None

    def get(self, actor, scope, offset=0, limit=100):
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200, 'invalid_range', 'Bounded page required')
        row = self._row(actor, scope); b = row['body']
        return {k: row[k] for k in ('id', 'program', 'project', 'parent', 'previous', 'breakdown', 'status', 'digest')} | {
            'title': b['title'], 'unit_count': len(b['selection']['units']), 'task_count': len(b['selection']['tasks']),
            'obligation_count': len(b['selection']['obligations']), 'packet_count': len(b['packet_manifest']),
            'packets': b['packet_manifest'][offset:offset+limit],
            'next_offset': offset+limit if offset+limit < len(b['packet_manifest']) else None,
            'history_only': row['status'] in {'withdrawn', 'superseded'}, 'deploy_ready': False}

    def selection(self, actor, scope, kind='tasks', offset=0, limit=100):
        need(kind in {'units', 'tasks', 'obligations', 'external_dependencies'}, 'invalid_kind', 'Unknown scope catalog')
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200, 'invalid_range', 'Bounded page required')
        row = self._row(actor, scope); values = row['body']['selection'][kind]
        return {'scope': scope, 'scope_digest': row['digest'], 'kind': kind, 'total': len(values), 'items': values[offset:offset+limit],
                'next_offset': offset+limit if offset+limit < len(values) else None, 'root_scope_reduced': False}

    def list(self, actor, program, offset=0, limit=50):
        self.c.breakdowns._program(actor, program)
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200, 'invalid_range', 'Bounded page required')
        rows = self.s.all('SELECT id,parent,previous,breakdown,status,digest,created,json_extract(body,\'$.title\') AS title FROM workstreams WHERE program=? ORDER BY created,id LIMIT ? OFFSET ?', (program, limit+1, offset))
        return {'program': program, 'items': rows[:limit], 'next_offset': offset+limit if len(rows)>limit else None, 'deploy_ready': False}

    def status(self, actor, scope):
        return self._status(actor, scope, readonly=False)

    def _status(self, actor, scope, *, readonly=False):
        with self.s.transaction():
            row = self._row(actor, scope); failures = []
            try: self._check(actor, row, readonly=readonly); self._reviews(row)
            except Fault as exc: failures.append(exc.as_dict())
            return {'scope': scope, 'status': row['status'], 'current': not failures, 'failures': failures, 'deploy_ready': False}

    def _record(self, row, kind, value):
        ident = uid('WSREC')
        self.s.execute('INSERT INTO workstream_records VALUES(?,?,?,?,?,?,?)',
                       (ident, row['id'], row['project'], kind, canonical(value).decode(), digest(value), timestamp()))
        return ident

    def activate(self, actor, scope):
        with self.s.transaction():
            row = self._row(actor, scope); actor.require('owner', 'agent', project=row['project'])
            self._check(actor, row); self._no_overlap(row); reviews = self._reviews(row)
            report = self.c.breakdowns.audit(actor, row['breakdown'])
            need(report['current'], 'breakdown_gate_denied', 'Root plan is not current at adoption', report['failures'])
            if row['status'] == 'active': return {'scope': scope, 'status': 'active', 'replayed': True}
            if row['previous']:
                old = self._row(actor, row['previous'])
                need(old['status'] == 'active', 'stale_scope_replacement', 'Previous assignment was replaced or withdrawn')
                need(not self.s.one("SELECT id FROM workstreams WHERE parent=? AND status='active'", (old['id'],)),
                     'active_child_scopes', 'Resolve delegated children before replacing the parent')
                self.s.execute("UPDATE workstreams SET status='superseded' WHERE id=?", (old['id'],))
            self.s.execute("UPDATE workstreams SET status='active' WHERE id=?", (scope,))
            record = self._record(row, 'adopt', {'reviews': reviews, 'actor': actor.id, 'root_scope_reduced': False})
            self.c.sec.event(row['project'], 'workstream_adopted', actor.id, {'scope': scope, 'record': record, 'program': row['program']})
            return {'scope': scope, 'status': 'active', 'replayed': False, 'record': record, 'deploy_ready': False}

    def _completion(self, actor, scope, child_results, task_cache, *, readonly=False):
        row = self._row(actor, scope); failures = []; stamp = []
        if row['status'] != 'active': failures.append({'code': 'scope_not_active'})
        status = self._status(actor, scope, readonly=readonly); failures.extend(status['failures'])
        if self.s.one('SELECT paused FROM projects WHERE id=?', (row['project'],))['paused']:
            failures.append({'code': 'project_paused'})
        owned = set(row['body']['selection']['tasks'])
        external = {d['dependency'] for d in row['body']['selection']['external_dependencies']}
        for task in sorted(owned | external):
            if task not in task_cache:
                t = self.c.w.task(actor, task)
                value = {'task': task,
                         'binding': self.c.g.task_binding(
                             task, ensure_policy=not readonly),
                         'status': t['status'], 'validity': t['validity']}
                if t['status'] == 'completed' and t['validity'] == 'current':
                    gate = (self.c.g._evaluate_task_readonly(
                                actor, task, gate='recheck') if readonly else
                            self.c.g.evaluate_task(actor, task, gate='recheck'))
                else:
                    gate = None
                task_cache[task] = value, gate
            value, gate = task_cache[task]; stamp.append(value)
            if gate is None:
                failures.append({'code': 'external_prerequisite_incomplete' if task in external else 'task_incomplete', 'task': task})
            elif gate['verdict'] != 'pass': failures.append({'code': 'task_evidence_invalid', 'task': task, 'details': gate['failures']})
        # Direct children have already been checked bottom-up by completion().
        children = self.s.all("SELECT id FROM workstreams WHERE parent=? AND status='active' ORDER BY id", (scope,))
        for child in children:
            result = child_results[child['id']]
            record = self.s.one("SELECT body FROM workstream_records WHERE scope=? AND kind='finish' ORDER BY created DESC,id DESC LIMIT 1", (child['id'],))
            if not result['ready'] or not record or parse_json(record['body'])['binding'] != result['binding']:
                failures.append({'code': 'child_scope_incomplete', 'scope': child['id']})
        stamp.extend({'child': child['id'], 'closure': self.s.one("SELECT id,digest FROM workstream_records WHERE scope=? AND kind='finish' ORDER BY created DESC,id DESC LIMIT 1", (child['id'],))} for child in children)
        binding = digest({
            'scope': row['digest'], 'tasks': stamp,
            'global': self._global(row['project'], ensure_policy=not readonly),
        })
        return {'scope': scope, 'ready': not failures, 'failures': failures, 'binding': binding,
                'owned_task_count': len(owned), 'external_task_count': len(external),
                'meaning': 'assigned work checked; root integration/delivery remains mandatory', 'deploy_ready': False}

    def _tree_results(self, actor, roots, *, readonly=False):
        # One transaction-local Task cache; never reused after input/evidence changes.
        seen, order, stack = set(), [], list(roots)
        while stack:
            item = stack.pop()
            need(item not in seen, 'scope_cycle', 'Cycle or duplicate root in workstream hierarchy')
            seen.add(item); self._row(actor, item); order.append(item)
            stack.extend(r['id'] for r in self.s.all("SELECT id FROM workstreams WHERE parent=? AND status='active'", (item,)))
        results, tasks = {}, {}
        for item in reversed(order):
            results[item] = self._completion(
                actor, item, results, tasks, readonly=readonly,
            )
        return results

    def completion(self, actor, scope):
        with self.s.transaction():
            return self._tree_results(actor, [scope])[scope]

    def finish(self, actor, scope):
        with self.s.transaction():
            row = self._row(actor, scope); actor.require('owner', 'agent', project=row['project'])
            report = self.completion(actor, scope)
            need(report['ready'], 'workstream_gate_denied', 'Assigned work has unresolved inputs, prerequisites or evidence', report['failures'])
            prior = self.s.one("SELECT id,body FROM workstream_records WHERE scope=? AND kind='finish' ORDER BY created DESC,id DESC LIMIT 1", (scope,))
            if prior and parse_json(prior['body'])['binding'] == report['binding']:
                return {'scope': scope, 'record': prior['id'], 'state': 'work_verified', 'replayed': True, 'deploy_ready': False}
            record = self._record(row, 'finish', {'binding': report['binding'], 'report': report, 'actor': actor.id})
            self.c.sec.event(row['project'], 'workstream_finished', actor.id, {'scope': scope, 'record': record, 'deploy_ready': False})
            return {'scope': scope, 'record': record, 'state': 'work_verified', 'replayed': False, 'deploy_ready': False}

    def program_audit(self, actor, program):
        return self._program_audit(actor, program, readonly=False)

    def _program_audit(self, actor, program, *, readonly=False):
        self.c.breakdowns._program(actor, program); failures = []
        with self.s.transaction():
            rows = self.s.all("SELECT id FROM workstreams WHERE program=? AND status='active' ORDER BY id", (program,))
            ids={r['id'] for r in rows}
            roots=[r['id'] for r in self.s.all("SELECT id,parent FROM workstreams WHERE program=? AND status='active'",(program,)) if r['parent'] not in ids]
            reports=self._tree_results(actor, roots, readonly=readonly)
            need(set(reports)==ids,'scope_cycle','Active scope forest is incomplete')
            for row in rows:
                report = reports[row['id']]
                record = self.s.one("SELECT body FROM workstream_records WHERE scope=? AND kind='finish' ORDER BY created DESC,id DESC LIMIT 1", (row['id'],))
                if not report['ready'] or not record or parse_json(record['body'])['binding'] != report['binding']:
                    failures.append({'scope': row['id'], 'code': 'delegated_work_incomplete', 'details': report['failures']})
        return {'program': program, 'current': not failures, 'failures': failures, 'active_scopes': len(rows), 'root_scope_reduced': False}

    def withdrawal_subject(self, actor, scope, proposal):
        from .common import obj
        obj(proposal, required=('reason',), name='withdrawal proposal')
        text(proposal['reason'], 'withdrawal reason', 20000)
        row = self._row(actor, scope)
        need(row['status'] == 'active', 'scope_closed', 'Withdraw an active assignment')
        children = self.s.all("SELECT id FROM workstreams WHERE parent=? AND status='active' ORDER BY id", (scope,))
        context = {'scope': scope, 'scope_digest': row['digest'], 'selection': row['body']['selection'],
                   'current_impact': self.c.scope_returns.material(actor, scope, proposal['reason']),
                   'reason': proposal['reason'], 'children': children, 'policy': self.c.g.policy(row['project'])['digest'],
                   'effect': 'Return responsibility to parent/root; do not cancel Tasks, waive obligations, or certify a release.'}
        need(len(canonical(context)) <= 700000, 'context_insufficient', 'Withdrawal material exceeds one review; use workstream.return_propose/advance/apply for bounded complete synthesis')
        binding = digest(context)
        context['required_coverage'] = ['WSWITHDRAW-' + binding]
        empty = {'format': 'snapshot.v1', 'repos': {}, 'digest': digest({'repos': {}})}
        return row['project'], binding, empty, context, None

    def withdraw(self, actor, scope, reason, review_receipt):
        with self.s.transaction():
            row = self._row(actor, scope); actor.require('owner', 'agent', project=row['project'])
            project, binding, _, context, _ = self.withdrawal_subject(actor, scope, {'reason': reason})
            need(not context['children'], 'active_child_scopes', 'Return active child scopes first; no implicit cancellation')
            refs = self.c.g.evidence_for(scope, binding, 'impact')
            need(refs and refs[0]['id'] == review_receipt, 'stale_evidence', 'Use the latest exact withdrawal review')
            ev = self.c.g.require_review(review_receipt, scope, binding, {'impact'})
            need(set(context['required_coverage']) <= set(ev['result']['covered']), 'review_coverage', 'Review the exact withdrawal effect')
            need(not ev['result']['findings'], 'unresolved_findings', 'Resolve withdrawal findings before applying')
            self.s.execute("UPDATE workstreams SET status='withdrawn' WHERE id=?", (scope,))
            record = self._record(row, 'withdraw', {'reason': reason, 'review_receipt': review_receipt, 'binding': binding, 'context': context, 'tasks_cancelled': []})
            self.c.sec.event(project, 'workstream_withdrawn', actor.id, {'scope': scope, 'record': record, 'scope_reduced': False})
            return {'scope': scope, 'status': 'withdrawn', 'record': record, 'tasks_cancelled': [], 'root_scope_reduced': False}
