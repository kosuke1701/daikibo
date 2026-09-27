"""Versioned work breakdowns, lossless review packets, and scope conservation.

The model validates *declared* assignments and observed reviews. It does not infer
semantic equivalence or manufacture a good architecture from prose. Leaf units
refer to canonical domain artifacts and executable tasks; they are not another
editable copy of either. Normal task execution does not invalidate a plan.
"""
from __future__ import annotations

from collections import deque
from functools import wraps
import threading
from .common import Actor, Fault, canonical, digest, need, obj, parse_json, strings, text, timestamp, uid
from .packets import slices

FORMAT = 'daikibo.breakdown.v1'
ROLES = ('design', 'trace')
MAX_PROPOSAL_BYTES = 128 * 1024 * 1024


def consistent_view(function):
    """Bound caches to one serialized read, never across a specification revision."""
    @wraps(function)
    def invoke(self, *args, **kwargs):
        with self.s.transaction():
            nested = getattr(self.local, 'cache', None)
            if nested is None: self.local.cache = {'tasks':{}, 'artifacts':{}, 'scopes':{}}
            try: return function(self, *args, **kwargs)
            finally:
                if nested is None: self.local.cache = None
    return invoke


def topological(nodes, dependencies):
    """Iterative traversal; deep hierarchies must not depend on Python recursion."""
    waiting = {key: len(dependencies.get(key, ())) for key in nodes}
    followers = {key: [] for key in nodes}
    for key, parents in dependencies.items():
        for parent in parents:
            need(parent in waiting, 'unknown_unit', 'Unknown dependency', parent)
            followers[parent].append(key)
    ready = deque(sorted(key for key, count in waiting.items() if not count))
    result = []
    while ready:
        key = ready.popleft(); result.append(key)
        for follower in followers[key]:
            waiting[follower] -= 1
            if not waiting[follower]: ready.append(follower)
    need(len(result) == len(nodes), 'unit_cycle', 'Work hierarchy/dependencies contain a cycle')
    return result


class Breakdowns:
    def __init__(self, control):
        self.c, self.s = control, control.s
        self.local = threading.local()

    def _program(self, actor, program):
        row = self.s.one('SELECT * FROM programs WHERE id=?', (program,), True)
        self.c.k.project(actor, row['project'])
        return row

    def _task_definition(self, actor, task):
        cache = getattr(self.local,'cache',None)
        if cache is not None and task in cache['tasks']: return cache['tasks'][task]
        row = self.c.w.task(actor, task)
        plan = self.s.one('SELECT body,digest,approved FROM plans WHERE task=?', (task,))
        result = {'id': task, 'revision': row['revision'], 'body': row['body'],
                'reads': self.s.all('SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact', (task,)),
                'dependencies': [r['dependency'] for r in self.s.all('SELECT dependency FROM task_deps WHERE task=? ORDER BY dependency', (task,))],
                'test_plan': {**plan, 'body': parse_json(plan['body'])} if plan else None}
        if cache is not None: cache['tasks'][task] = result
        return result

    @consistent_view
    def _scope(self, actor, project, ensure_policy=True):
        self.c.k.project(actor,project)
        cache = getattr(self.local,'cache',None)
        if cache is not None and project in cache['scopes']: return cache['scopes'][project]
        if cache is not None:
            # Four bounded SQL scans, rather than repeatedly asking for each task's
            # definition during validation, material generation and packet review.
            definitions = {}
            for row in self.s.all("SELECT id,revision,body FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id",(project,)):
                definitions[row['id']] = dict(id=row['id'],revision=row['revision'],body=parse_json(row['body']),reads=[],dependencies=[],test_plan=None)
            for row in self.s.all("SELECT r.* FROM task_reads r JOIN tasks t ON t.id=r.task WHERE t.project=? AND t.status!='cancelled' ORDER BY r.task,r.artifact",(project,)):
                definitions[row.pop('task')]['reads'].append(row)
            for row in self.s.all("SELECT d.* FROM task_deps d JOIN tasks t ON t.id=d.task WHERE t.project=? AND t.status!='cancelled' ORDER BY d.task,d.dependency",(project,)):
                definitions[row['task']]['dependencies'].append(row['dependency'])
            for row in self.s.all("SELECT p.task,p.body,p.digest,p.approved FROM plans p JOIN tasks t ON t.id=p.task WHERE t.project=? AND t.status!='cancelled'",(project,)):
                task = row.pop('task');row['body']=parse_json(row['body']);definitions[task]['test_plan']=row
            cache['tasks'].update(definitions)
        requirements = []
        for row in self.s.all("SELECT id,revision,digest,body FROM artifacts WHERE project=? AND kind='requirement' AND status='accepted' ORDER BY id", (project,)):
            requirements.append({k: row[k] for k in ('id','revision','digest')} | {'acceptance': parse_json(row['body'])['acceptance']})
        tasks = []
        for row in self.s.all("SELECT id FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id", (project,)):
            definition = self._task_definition(actor, row['id'])
            tasks.append({'id': row['id'], 'definition_digest': digest(definition)})
        result = {'requirements': requirements, 'tasks': tasks,
                  'policy': self.c.g.policy(project, create=ensure_policy)['digest']}
        if cache is not None: cache['scopes'][project] = result
        return result

    def _artifact(self, actor, project, ident, kind=None, allow_draft=False):
        cache = getattr(self.local,'cache',None)
        row = cache['artifacts'].get(ident) if cache is not None else None
        if row is None:
            row = self.c.k.artifact(actor, ident)
            if cache is not None: cache['artifacts'][ident] = row
        need(row['project'] == project, 'cross_project', 'Breakdown reference belongs elsewhere', ident)
        need(row['status'] == 'accepted' or (allow_draft and row['status']=='draft'), 'unaccepted_input', 'Root breakdown uses accepted artifacts only', ident)
        if kind: need(row['kind'] == kind, 'invalid_kind', 'Unexpected breakdown artifact kind', ident)
        return {k: row[k] for k in ('id','kind','revision','status','digest','owner','body')}

    def _validate(self, actor, project, units, scope, *, allow_drafts=False, allow_external=False):
        artifact = lambda ident, kind=None: self._artifact(actor, project, ident, kind, allow_draft=allow_drafts)
        external = []
        need(isinstance(units, list) and 0 < len(units) <= 10000, 'invalid_breakdown', 'Provide 1..10000 bounded units')
        ids, by_id = set(), {}
        for unit in units:
            obj(unit, required=('id','title','parent','domain','rationale','obligations','tasks','interfaces','dependencies'), name='unit')
            text(unit['id'], 'unit ID', 120); text(unit['title'], 'unit title', 400); text(unit['rationale'], 'split rationale', 12000)
            need(unit['id'] != '__structure__' and unit['id'] not in ids, 'duplicate_unit', 'Unit IDs must be unique')
            if unit['parent'] is not None: text(unit['parent'], 'parent unit', 120)
            if unit['domain'] is not None: text(unit['domain'], 'domain', 200)
            strings(unit['tasks'], 'tasks'); strings(unit['interfaces'], 'interfaces')
            need(isinstance(unit['obligations'], list) and isinstance(unit['dependencies'], list), 'invalid_breakdown', 'Obligations and dependencies must be lists')
            ids.add(unit['id']); by_id[unit['id']] = unit
        hierarchy = {key: [u['parent']] if u['parent'] else [] for key,u in by_id.items()}
        order = topological(ids, hierarchy)
        parents = {u['parent'] for u in units if u['parent']}
        leaves = ids - parents
        expected = {(r['id'], ac) for r in scope['requirements'] for ac in r['acceptance']}
        need(expected, 'empty_scope', 'No accepted requirements to allocate')
        required_tasks = {t['id'] for t in scope['tasks']}
        task_owner, obligations = {}, {}
        domains, definitions, task_obligations = {}, {}, {}
        dependencies = {}
        for key in order:
            unit = by_id[key]
            if key not in leaves:
                need(not any(unit[k] for k in ('obligations','tasks','interfaces','dependencies')), 'aggregate_assignment', 'Parent units aggregate children; only leaves own work', key)
                need(unit['domain'] is None, 'aggregate_assignment', 'Parent unit must not shadow a leaf domain')
                continue
            need(unit['domain'] and unit['tasks'], 'unallocated_unit', 'Each leaf needs a canonical domain and concrete tasks', key)
            domain = artifact(unit['domain'], 'domain'); domains[unit['domain']] = domain
            interfaces = {x: artifact(x, 'interface') for x in unit['interfaces']}
            for task in unit['tasks']:
                need(task in required_tasks and task not in task_owner, 'task_scope_mismatch', 'Every noncancelled task is allocated exactly once', task)
                definition = self._task_definition(actor, task); definitions[task] = definition
                need(unit['domain'] in definition['body']['read_artifacts'], 'missing_domain_context', 'Task must explicitly read its assigned domain', task)
                need(definition['test_plan'], 'missing_test_plan', 'Freeze concrete checks before adopting a breakdown', task)
                need({r['artifact'] for r in definition['reads']}==set(definition['body']['read_artifacts']), 'task_input_mismatch', 'Task body and input registry disagree', task)
                for read in definition['reads']:
                    current=artifact(read['artifact'])
                    need((read['revision'],read['digest'])==(current['revision'],current['digest']), 'stale_task_input', 'Replan stale task inputs before reviewing a breakdown', task)
                need(digest(definition['test_plan']['body'])==definition['test_plan']['digest'], 'integrity_error', 'Test plan digest differs', task)
                from .obligations import resolve
                reqs = {ref: artifact(ref)['body'] for ref in definition['body']['read_artifacts']
                        if artifact(ref)['kind']=='requirement'}
                task_obligations[task] = resolve(definition['body'],reqs)['pairs']
                task_owner[task] = key
            for obligation in unit['obligations']:
                obj(obligation, required=('requirement','acceptance'), name='obligation')
                text(obligation['requirement'], 'requirement ID', 200); text(obligation['acceptance'], 'acceptance ID', 4096)
                pair = (obligation['requirement'], obligation['acceptance'])
                need(pair in expected and pair not in obligations, 'acceptance_scope_mismatch', 'No invented, omitted, or multiply-owned acceptance conditions', list(pair))
                witnesses = [t for t in unit['tasks'] if pair in task_obligations[t] and definitions[t]['body'].get('task_kind') == 'production']
                need(witnesses, 'unmapped_acceptance', 'Acceptance needs a production task reading its requirement; analysis work is not implementation', list(pair))
                obligations[pair] = key
            dependencies[key] = set()
            for dependency in unit['dependencies']:
                obj(dependency, required=('unit','interface'), name='unit dependency')
                other = dependency['unit']
                need(other in leaves and other != key and other not in dependencies[key], 'invalid_dependency', 'Dependency must name another leaf exactly once')
                dependencies[key].add(other)
                if by_id[other]['domain'] != unit['domain']:
                    contract = dependency['interface']
                    need(contract in interfaces and contract in by_id[other]['interfaces'], 'missing_boundary_contract', 'Both sides of a cross-domain dependency need the same accepted contract')
                elif dependency['interface'] is not None:
                    need(dependency['interface'] in interfaces and dependency['interface'] in by_id[other]['interfaces'], 'missing_boundary_contract', 'Declared contract must be shared by both units')
        need(obligations.keys() == expected, 'acceptance_scope_mismatch', 'The full accepted requirement set, including parent acceptance conditions, must be retained', [list(x) for x in sorted(expected-obligations.keys())])
        need(task_owner.keys() == required_tasks, 'task_scope_mismatch', 'The full noncancelled task set must be retained', sorted(required_tasks-task_owner.keys()))
        ownership = {}
        for domain in domains.values():
            for data in domain['body']['owned_data']:
                need(data not in ownership, 'duplicate_data_owner', 'Distinct domains cannot claim the same data', data)
                ownership[data] = domain['id']
        # Unit order is a view of REAL task dependencies, not a second scheduler.
        actual = {key: set() for key in leaves}
        for task, definition in definitions.items():
            owner = task_owner[task]
            for predecessor in definition['dependencies']:
                if predecessor not in task_owner and allow_external:
                    predecessor_row = self.c.w.task(actor, predecessor)
                    need(predecessor_row['project']==project and predecessor_row['status']!='cancelled',
                         'missing_dependency', 'External prerequisite is missing or cancelled', predecessor)
                    external.append({'task':task,'dependency':predecessor,'definition_digest':digest(self._task_definition(actor,predecessor))})
                    continue
                need(predecessor in task_owner, 'missing_dependency', 'Cancelled or omitted task dependency', predecessor)
                other = task_owner[predecessor]
                if other != owner:
                    actual[owner].add(other)
                    dep = next((d for d in by_id[owner]['dependencies'] if d['unit'] == other), None)
                    if dep and dep['interface'] is not None:
                        need(dep['interface'] in definition['body']['read_artifacts'] and dep['interface'] in definitions[predecessor]['body']['read_artifacts'], 'missing_contract_context', 'Both dependent tasks must read their declared contract')
        need(actual == dependencies, 'dependency_mismatch', 'Unit order must exactly describe existing task dependencies', {'actual': {k: sorted(v) for k,v in actual.items()}})
        topological(leaves, dependencies)
        return {'leaf_units': sorted(leaves), 'hierarchy_order': order, 'unit_order': topological(leaves, dependencies), 'obligation_count': len(expected), 'task_count': len(required_tasks), **({'external_dependencies':sorted(external,key=lambda e:(e['task'],e['dependency']))} if allow_external else {})}

    def _material(self, actor, project, body, unit_id, unit_index=None, ensure_policy=True):
        if unit_id == '__structure__':
            return {'format': FORMAT, 'program': body['program'], 'title': body['title'], 'rationale': body['rationale'],
                    'units': body['units'],
                    'current_scope': self._scope(actor, project, ensure_policy=ensure_policy),
                    **({'origin_subplan':body['origin_subplan']} if body.get('origin_subplan') else {}),
                    'review_question': 'Check complete coverage, hierarchical responsibility, real task dependency order, cross-domain contracts, and whether the split actually serves user scenarios. Counts alone are not correctness.'}
        unit = unit_index[unit_id] if unit_index is not None else next(u for u in body['units'] if u['id'] == unit_id)
        references = {unit['domain'], *unit['interfaces'], *(o['requirement'] for o in unit['obligations'])}
        tasks = [self._task_definition(actor, t) for t in unit['tasks']]
        for t in tasks: references.update(t['body']['read_artifacts'])
        artifacts = [self._artifact(actor, project, x) for x in sorted(references) if x]
        source_ids = {s for a in artifacts for s in a['body'].get('source_refs', [])}
        sources = []
        for source in sorted(source_ids):
            row = self.s.one('SELECT id,project,blob,locator,characters FROM sources WHERE id=?', (source,), True)
            need(row['project'] == project, 'cross_project', 'Referenced source belongs elsewhere')
            sources.append({k: row[k] for k in ('id','blob','locator','characters')})
        return {'format': FORMAT, 'program': body['program'], 'unit': unit, 'artifacts': artifacts, 'tasks': tasks, 'source_index': sources,
                'policy': self.c.g.policy(project, create=ensure_policy)['digest'], 'review_question': 'Inspect responsibility, omitted parent conditions, acceptance-to-task mapping, test adequacy and boundary contracts. Original sources can be requested by ID; missing evidence is blocked, not assumed.'}

    @consistent_view
    def propose(self, actor, program, title, rationale, units, expected_active=None, byte_budget=24000, origin_subplan=None):
        row = self._program(actor, program); project = row['project']
        actor.require('owner','agent', project=project)
        text(title, 'breakdown title', 400); text(rationale, 'rationale', 20000)
        need(type(byte_budget) is int and 4096 <= byte_budget <= 100000, 'invalid_budget', 'Packet budget must be 4096..100000 UTF-8 bytes')
        # JSON copy prevents a caller changing the retained proposal object.
        units = parse_json(canonical(units), limit=MAX_PROPOSAL_BYTES)
        with self.s.transaction():
            old = self.active(actor, program)
            need((old['id'] if old else None) == expected_active, 'stale_breakdown', 'Rebase onto the current active breakdown')
            scope = self._scope(actor, project); checks = self._validate(actor, project, units, scope)
            ident = uid('BREAKDOWN')
            body = {'format': FORMAT, 'program': program, 'title': title, 'rationale': rationale, 'units': units, 'scope': scope, 'structure': checks, 'material_bindings': {}}
            if origin_subplan is not None:
                source=self.c.subplans._row(actor,origin_subplan)
                need(source['project']==project and source['program']==program and source['body']['units']==units,
                     'invalid_composition_origin','Composed root must retain the exact child-plan units')
                body['origin_subplan']={'id':source['id'],'digest':source['digest']}
            packets = []; unit_index = {u['id']:u for u in units}
            for unit_id in ['__structure__', *checks['leaf_units']]:
                material = self._material(actor, project, body, unit_id, unit_index); serialized = canonical(material).decode()
                material_digest = digest(material); body['material_bindings'][unit_id] = material_digest
                for start, end, fragment in slices(serialized, (byte_budget-1600)//2):
                    marker = 'PART-' + digest({'program':program, 'unit':unit_id, 'material':material_digest, 'start':start, 'end':end})
                    packet = {'format': 'daikibo.breakdown-review.v1', 'program': program, 'unit': unit_id,
                              'material_digest': material_digest, 'start': start, 'end': end, 'total_characters': len(serialized),
                              'serialized_fragment': fragment, 'required_coverage': [marker],
                              'instructions': 'This is a fragment, not the whole system. Review design and trace separately. Include this exact fragment marker in covered only after inspecting it; return blocked when necessary context is missing. Query breakdown.get/packet for related fragments.'}
                    encoded = canonical(packet)
                    need(len(encoded) <= byte_budget, 'context_insufficient', 'Packet metadata exceeds budget; increase byte_budget')
                    packet_id = 'BPACK-' + digest(packet)
                    self.s.execute('INSERT OR IGNORE INTO breakdown_packets VALUES(?,?,?,?,?)', (packet_id, project, encoded.decode(), digest(packet), timestamp()))
                    packets.append(packet_id)
            encoded_body = canonical(body)
            need(len(encoded_body)<=MAX_PROPOSAL_BYTES,'context_insufficient','Internal breakdown registry exceeds 128 MiB; capacity extension is required, and scope must not be silently discarded')
            h = digest(encoded_body)
            self.s.execute('INSERT INTO breakdowns VALUES(?,?,?,?,?,?,?,?)', (ident, program, project, encoded_body.decode(), h, 'proposed', expected_active, timestamp()))
            for position, packet_id in enumerate(packets):
                self.s.execute('INSERT INTO breakdown_members VALUES(?,?,?)', (ident, packet_id, position))
            self.c.sec.event(project, 'breakdown_proposed', actor.id, {'breakdown':ident,'program':program,'previous':expected_active,'digest':h,'packets':len(packets),'scope_preserved':True})
        return {'id':ident, 'digest':h, 'status':'proposed', 'packet_count':len(packets), **checks, 'semantic_reviewed':False}

    def active(self, actor, program):
        self._program(actor, program)
        return self.s.one("SELECT id,digest,status FROM breakdowns WHERE program=? AND status='active'", (program,))

    def _row(self, actor, breakdown):
        row = self.s.one('SELECT * FROM breakdowns WHERE id=?', (breakdown,), True)
        self.c.k.project(actor, row['project']); row['body'] = parse_json(row['body'],limit=MAX_PROPOSAL_BYTES)
        need(digest(row['body']) == row['digest'], 'integrity_error', 'Breakdown content changed')
        return row

    def get(self, actor, breakdown, offset=0, limit=100, include_structure=True):
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200, 'invalid_range', 'Invalid page')
        need(type(include_structure) is bool,'invalid_input','include_structure must be boolean')
        row = self._row(actor, breakdown)
        packets = self.s.all('SELECT p.id,p.digest,p.body,m.ordinal FROM breakdown_members m JOIN breakdown_packets p ON p.id=m.packet WHERE m.breakdown=? ORDER BY m.ordinal LIMIT ? OFFSET ?', (breakdown,limit,offset))
        total = self.s.one('SELECT count(*) AS n FROM breakdown_members WHERE breakdown=?',(breakdown,))['n']
        return {'id':row['id'],'program':row['program'],'digest':row['digest'],'status':row['status'],'previous':row['previous'],
                'title':row['body']['title'],'structure':row['body']['structure'] if include_structure else {k:v for k,v in row['body']['structure'].items() if k in {'obligation_count','task_count'}},
                'unit_count':len(row['body']['units']),
                'packets':[{'id':p['id'],'digest':p['digest'],'unit':parse_json(p['body'])['unit'],'bytes':len(p['body'].encode()),'ordinal':p['ordinal']} for p in packets],
                'packet_count':total,'next_offset':offset+len(packets) if offset+len(packets)<total else None}

    def units(self, actor, breakdown, offset=0, limit=50, byte_budget=1024*1024):
        need(type(offset) is int and offset>=0 and type(limit) is int and 1<=limit<=200,
             'invalid_range','Expected a bounded unit page')
        need(type(byte_budget) is int and 4096<=byte_budget<=4*1024*1024,'invalid_budget','Unit page must fit the transport')
        row=self._row(actor,breakdown); units=row['body']['units']; result=[]; size=512
        for unit in units[offset:offset+limit]:
            length=len(canonical(unit))
            if size+length>byte_budget:
                need(result,'unit_too_large','Use review packets to inspect a unit larger than this page budget',unit['id'])
                break
            result.append(unit);size+=length
        return {'breakdown':breakdown,'digest':row['digest'],'units':result,'total_units':len(units),
                'next_offset':offset+len(result) if offset+len(result)<len(units) else None}

    @consistent_view
    def packet(self, actor, packet):
        row = self.s.one('SELECT * FROM breakdown_packets WHERE id=?', (packet,), True)
        self.c.k.project(actor, row['project']); row['body'] = parse_json(row['body'],limit=MAX_PROPOSAL_BYTES)
        need(digest(row['body']) == row['digest'], 'integrity_error', 'Breakdown packet changed')
        # Same unit content may be reused across multiple revisions of the plan.
        members = self.s.all("SELECT b.id,b.body FROM breakdown_members m JOIN breakdowns b ON b.id=m.breakdown WHERE m.packet=? AND b.status IN ('active','proposed') ORDER BY b.created DESC", (packet,))
        failures = []
        for member in members:
            body = parse_json(member['body'],limit=MAX_PROPOSAL_BYTES)
            try:
                material = self._material(actor, row['project'], body, row['body']['unit'])
                need(digest(material) == row['body']['material_digest'], 'stale_breakdown_packet', 'Referenced input changed')
                need(canonical(material).decode()[row['body']['start']:row['body']['end']] == row['body']['serialized_fragment'], 'integrity_error', 'Fragment does not match its source')
                return row | {'breakdown':member['id']}
            except Fault as exc: failures.append(exc.code)
        raise Fault('stale_breakdown_packet', 'No active/proposed breakdown has this current material', failures)

    def review_subject(self, actor, packet, role):
        need(role in ROLES, 'invalid_role', 'Breakdown packets require design and trace reviews')
        row = self.packet(actor, packet)
        empty = {'format':'snapshot.v1','repos':{},'digest':digest({'repos':{}})}
        return row['project'],row['digest'],empty,{**row['body'],'breakdown':row['breakdown']},None

    def _disabled_old_root(self, actor, project, program):
        """Return whether the current source-backed disabled root is explicit."""
        assurance = getattr(self.c, 'assurance', None)
        if assurance is None or not callable(getattr(assurance, 'selected_profile', None)):
            return False
        try:
            selected = assurance.selected_profile(actor, project, program)
            if (selected.get('profile_ref') is None or
                    selected.get('application_mode') != 'disabled'):
                return False
            profile = assurance._object_by_ref(
                selected['profile_ref'], project, kinds={'profile'},
            )
            assurance._ensure_object_current(actor, project, profile, require_self=True)
            authority_refs = profile['body'].get('authority_refs')
            return isinstance(authority_refs, list) and bool(authority_refs)
        except Fault:
            return False

    def audit(self, actor, breakdown, reviews=True, readonly=False):
        """Audit a root with its mandatory plan gate unless disabled is proven."""
        row = self._row(actor, breakdown)
        return self._audit(
            actor, breakdown, reviews=reviews, readonly=readonly,
            enforce_plan_gate=not self._disabled_old_root(
                actor, row['project'], row['program']),
        )

    @consistent_view
    def _audit(self, actor, breakdown, reviews=True, readonly=False,
               enforce_plan_gate=True):
        row = self._row(actor, breakdown); body, project = row['body'], row['project']
        failures, verified = [], []
        current_scope = self._scope(actor, project, ensure_policy=not readonly)
        if current_scope != body['scope']: failures.append({'code':'scope_or_task_definition_changed'})
        try: self._validate(actor, project, body['units'], current_scope)
        except Fault as exc: failures.append(exc.as_dict())
        changed_units = []; unit_index = {u['id']:u for u in body['units']}
        for unit_id, expected in body['material_bindings'].items():
            try:
                if digest(self._material(actor, project, body, unit_id, unit_index,
                                         ensure_policy=not readonly)) != expected: changed_units.append(unit_id)
            except Fault: changed_units.append(unit_id)
        if changed_units: failures.append({'code':'stale_breakdown_inputs','units':changed_units})
        for member in self.s.all('SELECT p.id,p.digest,p.body FROM breakdown_members m JOIN breakdown_packets p ON p.id=m.packet WHERE m.breakdown=? ORDER BY m.ordinal', (breakdown,)):
            try:
                packet = parse_json(member['body'])
                need(digest(packet) == member['digest'], 'integrity_error', 'Stored packet differs')
                if reviews:
                    runs = set()
                    for role in ROLES:
                        refs = self.c.g.evidence_for(member['id'],member['digest'],role)
                        need(refs, 'review_required', 'Missing observed review', role)
                        ev = self.c.g.require_review(refs[0]['id'],member['id'],member['digest'],{role})
                        need(set(packet['required_coverage']) <= set(ev['result']['covered']), 'review_coverage', 'Reviewer did not cover this exact fragment')
                        need(ev['run'] not in runs, 'independent_review', 'Roles need separate observed executions')
                        runs.add(ev['run']); verified.append({'packet':member['id'],'role':role,'receipt':ev['id']})
            except Fault as exc: failures.append({'packet':member['id'], **exc.as_dict()})
        # Verify the membership itself, not just whichever packets remain in a table.
        observed = {}
        for member in self.s.all('SELECT p.body FROM breakdown_members m JOIN breakdown_packets p ON p.id=m.packet WHERE m.breakdown=? ORDER BY m.ordinal', (breakdown,)):
            p = parse_json(member['body']); observed.setdefault(p['unit'],[]).append(p)
        for unit_id, h in body['material_bindings'].items():
            parts = sorted(observed.get(unit_id,[]), key=lambda p:p['start'])
            cursor, contents = 0, []
            for part in parts:
                if part['start'] != cursor or part['material_digest'] != h: failures.append({'code':'packet_manifest_mismatch','unit':unit_id})
                cursor = part['end']; contents.append(part['serialized_fragment'])
            if not parts or cursor != parts[-1]['total_characters'] or digest(''.join(contents).encode()) != h:
                failures.append({'code':'missing_review_fragments','unit':unit_id})
        if set(observed) != set(body['material_bindings']): failures.append({'code':'packet_manifest_mismatch'})
        # A root composed from child designs retains that input provenance.
        # New graph relations or a newer failed child review cannot be bypassed
        # by calling root activation/delivery directly after composition.
        compositions=self.s.all('SELECT subplan,body,digest FROM subplan_compositions WHERE breakdown=?',(breakdown,))
        origin=body.get('origin_subplan')
        if origin and (len(compositions)!=1 or compositions[0]['subplan']!=origin['id']):
            failures.append({'code':'composition_history_missing'})
        for composition in compositions:
            try:
                record=parse_json(composition['body'],limit=MAX_PROPOSAL_BYTES)
                need(digest(record)==composition['digest'] and record['breakdown']==breakdown
                     and record['units_digest']==digest(body['units']) and origin=={'id':record['subplan'],'digest':record['subplan_digest']},'integrity_error','Root composition provenance differs')
                child_report=self.c.subplans.audit(actor,composition['subplan'],reviews=reviews,
                                                   readonly=readonly)
                need(child_report['current'],'subplan_inputs_changed','Composed child designs/reviews are no longer current',child_report['failures'])
            except Fault as exc:failures.append(exc.as_dict())
        # Unit 4-P keeps the existing packet/review audit intact and adds the
        # current immutable-origin/canonical-plan proof to the same report.
        # The report is still read-only; activation below reruns this proof at
        # its mutation boundary instead of treating an old audit as authority.
        if enforce_plan_gate:
            try:
                from .unit4_enforcement import inspect_plan_gate
                plan_gate=inspect_plan_gate(
                    self.c, actor, project=project, program=row['program'],
                    proposed_breakdown=breakdown,
                )
            except Fault as exc:
                plan_gate={
                    'format':'daikibo.unit4-plan-gate.v1', 'allowed':False,
                    'required':True, 'reason':exc.code, 'origin':None,
                    'selection':None, 'stage':'plan', 'checkpoint':'plan',
                    'evaluation':None, 'semantic_fingerprint':None,
                    'report_snapshot':None,
                    'failures':[{'code':exc.code,'reason':str(exc),'status':'unknown'}],
                }
        else:
            plan_gate={
                'format':'daikibo.unit4-plan-gate.v1', 'allowed':None,
                'required':False,
                'reason':'plan_gate_not_required_for_disabled_old_root',
                'origin':None, 'selection':None, 'stage':'plan',
                'checkpoint':'plan', 'evaluation':None,
                'semantic_fingerprint':None, 'report_snapshot':None,
                'failures':[],
            }
        if enforce_plan_gate and plan_gate.get('allowed') is not True:
            failures.append({'code':'stage_assurance_blocked',
                             'reason':plan_gate.get('reason','plan gate denied'),
                             'plan_gate':plan_gate})
        return {'id':breakdown,'status':row['status'],'current':not failures,'failures':failures,'reviewed':verified,
                'changed_units':changed_units,'review_checks_performed':reviews,
                'plan_gate':plan_gate,
                'semantic_correctness_guaranteed':False}

    def activate(self, actor, breakdown, expected_active=None):
        with self.s.transaction():
            row = self._row(actor, breakdown); actor.require('owner','agent', project=row['project'])
            old = self.active(actor, row['program'])
            if old and old['id'] == breakdown:
                report = self.audit(actor, breakdown)
                need(report['current'], 'breakdown_gate_denied', 'Active breakdown is no longer valid', report['failures'])
                from .unit4_enforcement import require_plan_gate
                plan_gate=require_plan_gate(
                    self.c,actor,project=row['project'],program=row['program'],
                    proposed_breakdown=breakdown,
                )
                return {'id':breakdown,'status':'active','replayed':True,
                        'plan_gate':plan_gate}
            need(row['status']=='proposed' and row['previous']==expected_active and (old['id'] if old else None)==expected_active, 'stale_breakdown', 'Concurrent activation or outdated proposal')
            report = self.audit(actor, breakdown)
            need(report['current'], 'breakdown_gate_denied', 'Scope, inputs or independent reviews are incomplete', report['failures'])
            from .unit4_enforcement import require_plan_gate
            plan_gate=require_plan_gate(
                self.c,actor,project=row['project'],program=row['program'],
                proposed_breakdown=breakdown,
            )
            if old: self.s.execute("UPDATE breakdowns SET status='superseded' WHERE id=?", (old['id'],))
            self.s.execute("UPDATE breakdowns SET status='active' WHERE id=?", (breakdown,))
            self.s.execute(
                'INSERT INTO breakdown_adoptions VALUES(?,?,?)',
                (breakdown, canonical({
                    'reviews': report['reviewed'],
                    'by': actor.id,
                    # Retain the exact origin/selection/stage proof used at
                    # the mutation boundary.  The immutable breakdown body
                    # remains the plan input; this is only the adoption
                    # decision record and is never used as a future allow.
                    'plan_gate': plan_gate,
                }).decode(), timestamp()),
            )
            self.s.execute('UPDATE programs SET revision=revision+1 WHERE id=?', (row['program'],))
            self.c.sec.event(row['project'],'breakdown_adopted',actor.id,{
                'program':row['program'], 'breakdown':breakdown,
                'previous':expected_active, 'reviews':report['reviewed'],
                'tasks_cancelled': [], 'plan_proof': plan_gate.get('proof_digest'),
            })
        return {'id':breakdown,'status':'active','previous':expected_active,'tasks_cancelled':[],
                'assurance':self.c.g.mode,'plan_gate':plan_gate,
                'semantic_correctness_guaranteed':False}

    def program_status(self, actor, program, reviews=True, readonly=False):
        row = self._program(actor, program)
        return self._program_status(
            actor, program, reviews=reviews, readonly=readonly,
            enforce_plan_gate=not self._disabled_old_root(
                actor, row['project'], program),
        )

    def _program_status(self, actor, program, reviews=True, readonly=False,
                        enforce_plan_gate=True):
        active = self.active(actor, program)
        if not active: return {'program':program,'active':None,'current':False,'failures':[{'code':'breakdown_required'}]}
        return {'program':program,'active':active['id'],
                **self._audit(actor, active['id'], reviews, readonly=readonly,
                              enforce_plan_gate=enforce_plan_gate)}
