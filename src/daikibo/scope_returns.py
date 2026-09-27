"""Version-bound scope return, complete fragment reviews and bounded synthesis.

This changes responsibility, never cancels Tasks or certifies a deliverable. Every
node is judged by the existing observed reviewer runtime, not by this module.
"""
from __future__ import annotations

from .common import Fault, canonical, digest, need, parse_json, text, timestamp, uid
from .packets import slices

MAX_BYTES = 128 * 1024 * 1024
MAX_PACKETS = 100000
MAX_LEVELS = 32
SCHEMA = """
CREATE TABLE IF NOT EXISTS scope_returns(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), scope TEXT NOT NULL REFERENCES workstreams(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','applied','abandoned')), result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS scope_returns_scope ON scope_returns(scope,status,created);
CREATE TABLE IF NOT EXISTS scope_return_packets(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES scope_returns(id), project TEXT NOT NULL REFERENCES projects(id),
 level INTEGER NOT NULL, ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(proposal,digest)
);
CREATE INDEX IF NOT EXISTS scope_return_packets_proposal ON scope_return_packets(proposal,level,ordinal);
CREATE TRIGGER IF NOT EXISTS scope_returns_body_immutable BEFORE UPDATE OF project,scope,body,digest,created ON scope_returns
 BEGIN SELECT RAISE(ABORT,'immutable scope return proposal'); END;
CREATE TRIGGER IF NOT EXISTS scope_returns_no_delete BEFORE DELETE ON scope_returns
 BEGIN SELECT RAISE(ABORT,'retain scope return history'); END;
CREATE TRIGGER IF NOT EXISTS scope_return_packets_immutable BEFORE UPDATE ON scope_return_packets
 BEGIN SELECT RAISE(ABORT,'immutable scope return review'); END;
CREATE TRIGGER IF NOT EXISTS scope_return_packets_no_delete BEFORE DELETE ON scope_return_packets
 BEGIN SELECT RAISE(ABORT,'retain scope return reviews'); END;
"""


def with_marker(body):
    return {**body, 'required_coverage': ['WSRETURN-' + digest(body)]}


def packet_check(packet, row):
    """Structural only: reusable by the standalone historical archive inspector."""
    b = packet['body']; raw = {k: v for k, v in b.items() if k != 'required_coverage'}
    need(digest(b) == packet['digest'] and b == with_marker(raw)
         and b.get('format') == 'daikibo.scope-return-review.v1'
         and b.get('proposal') == row['id'] and packet['proposal'] == row['id']
         and packet['project'] == row['project'] and b.get('scope') == row['scope']
         and b.get('material_digest') == row['body']['material_digest']
         and b.get('level') == packet['level'] and b.get('ordinal') == packet['ordinal']
         and type(packet['level']) is int and 0 <= packet['level'] <= MAX_LEVELS
         and len(canonical(b)) <= row['body']['byte_budget'],
         'integrity_error', 'Scope return review identity or byte budget differs')
    if packet['level'] == 0:
        need(b.get('kind') == 'fragment' and isinstance(b.get('serialized_fragment'), str),
             'integrity_error', 'Expected a scope return source fragment')
    else:
        need(b.get('kind') == 'synthesis' and isinstance(b.get('children'), list)
             and 1 <= len(b['children']) <= 16,
             'integrity_error', 'Expected bounded reviewed children')


class ScopeReturns:
    def __init__(self, control):
        self.c, self.s = control, control.s

    def material(self, actor, scope, reason):
        """Capture current impact even when the original adopted plan became stale."""
        text(reason, 'withdrawal reason', 20000)
        row = self.c.workstreams._row(actor, scope)
        need(row['status'] == 'active', 'scope_closed', 'Only an active assignment can be returned')
        children = self.s.all("SELECT id,digest FROM workstreams WHERE parent=? AND status='active' ORDER BY id", (scope,))
        current_root = self.c.breakdowns.active(actor, row['program'])
        parent = self.c.workstreams._row(actor, row['parent']) if row['parent'] else None
        selection = row['body']['selection']
        tasks = sorted(set(selection['tasks']) | {d['dependency'] for d in selection['external_dependencies']})
        states, inputs = [], set()
        for task in tasks:
            t = self.s.one('SELECT id,project,body,revision,status,validity,epoch,candidate FROM tasks WHERE id=?', (task,), True)
            need(t['project'] == row['project'], 'integrity_error', 'Cross-project task in scope return')
            body = parse_json(t.pop('body')); t['definition_digest'] = digest(body)
            t['read_artifacts'] = body['read_artifacts']; inputs.update(body['read_artifacts'])
            t['blocks'] = self.s.all('SELECT kind,ref,reason FROM blocks WHERE task=? ORDER BY kind,ref', (task,))
            t['dependencies'] = self.s.all('SELECT dependency FROM task_deps WHERE task=? ORDER BY dependency', (task,))
            states.append(t)
        artifacts = []
        for ident in sorted(inputs):
            art = self.c.k.artifact(actor, ident)
            artifacts.append({k: art[k] for k in ('id','kind','revision','digest','status','body')})
        return {'format':'daikibo.scope-return-material.v1', 'scope':scope, 'scope_digest':row['digest'],
                'reason':reason, 'program':row['program'], 'retained_selection':selection,
                'current_root': {k:current_root[k] for k in ('id','digest')} if current_root else None,
                'parent':{k:parent[k] for k in ('id','status','digest')} if parent else None,
                'children':children, 'tasks':states, 'artifacts':artifacts,
                'global':self.c.workstreams._global(row['project']),
                'effect':'Return responsibility to the parent/root, without deleting requirements, cancelling tasks, skipping gates or certifying delivery.'}

    def _row(self, actor, proposal):
        row = self.s.one('SELECT * FROM scope_returns WHERE id=?', (proposal,), True)
        self.c.k.project(actor, row['project']); row['body'] = parse_json(row['body'], limit=MAX_BYTES)
        row['result'] = parse_json(row['result'], limit=MAX_BYTES) if row['result'] else None
        need(digest(row['body']) == row['digest'], 'integrity_error', 'Return proposal body differs')
        return row

    def _current(self, actor, row):
        need(row['status'] == 'proposed', 'proposal_closed', 'Return proposal is no longer open')
        m = self.material(actor, row['scope'], row['body']['material']['reason'])
        need(not m['children'], 'active_child_scopes', 'Return active child assignments first')
        need(digest(m) == row['body']['material_digest'], 'stale_return_input', 'Current responsibility/inputs changed; propose a reviewed new return')

    def _node(self, actor, packet, proposal=None):
        p = self.s.one('SELECT * FROM scope_return_packets WHERE id=?', (packet,), True)
        row = proposal or self._row(actor, p['proposal'])
        p['body'] = parse_json(p['body']); packet_check(p, row)
        return p

    def _leaves(self, actor, row):
        result, cursor, fragments = [], 0, []
        manifest = row['body']['leaf_manifest']
        need(manifest and len(manifest) <= MAX_PACKETS and len({m['id'] for m in manifest}) == len(manifest),
             'missing_review_fragments', 'Complete unique source manifest required')
        for ordinal, ref in enumerate(manifest):
            p = self._node(actor, ref['id'], row); b = p['body']; fragment = b['serialized_fragment']
            need(p['digest'] == ref['digest'] and p['level'] == 0 and p['ordinal'] == ordinal
                 and b['start'] == cursor and b['end'] == cursor + len(fragment),
                 'missing_review_fragments', 'Return fragment ordering or identity differs')
            cursor = b['end']; fragments.append(fragment); result.append(p)
        raw = ''.join(fragments).encode()
        need(all(p['body']['total_characters'] == cursor for p in result)
             and digest(raw) == row['body']['material_digest']
             and raw == canonical(row['body']['material']), 'missing_review_fragments', 'Return material was truncated or substituted')
        return result

    def propose(self, actor, scope, reason, byte_budget=24000):
        need(type(byte_budget) is int and 4096 <= byte_budget <= 100000, 'invalid_budget', 'Review budget must be 4096..100000 UTF-8 bytes')
        with self.s.transaction():
            material = self.material(actor, scope, reason)
            owner = self.c.workstreams._row(actor, scope); actor.require('owner','agent',project=owner['project'])
            need(not material['children'], 'active_child_scopes', 'Return active children first; no implicit cancellation')
            raw = canonical(material); need(len(raw) < MAX_BYTES - 1024*1024, 'context_insufficient', 'Complete return exceeds supported proposal capacity; nothing was omitted')
            ident = uid('WSRETURN'); binding = digest(raw); manifest = []; packets = []; encoded = raw.decode()
            for ordinal, (start, end, fragment) in enumerate(slices(encoded, (byte_budget - 2000)//2)):
                need(ordinal < MAX_PACKETS, 'context_insufficient', 'Too many complete review fragments')
                b = with_marker({'format':'daikibo.scope-return-review.v1','kind':'fragment','proposal':ident,'scope':scope,
                     'level':0,'ordinal':ordinal,'material_digest':binding,'start':start,'end':end,'total_characters':len(encoded),
                     'serialized_fragment':fragment,
                     'instruction':'Judge this fragment of a responsibility return. Missing context means blocked, not pass. State impact and caveats in rationale/observations; all results receive a separate synthesis review.'})
                need(len(canonical(b)) <= byte_budget, 'context_insufficient', 'Complete source fragment exceeds budget')
                pid = uid('RPACK'); h = digest(b); manifest.append({'id':pid,'digest':h}); packets.append((pid,ident,owner['project'],0,ordinal,canonical(b).decode(),h,timestamp()))
            body = {'format':'daikibo.scope-return.v1','material':material,'material_digest':binding,'byte_budget':byte_budget,'leaf_manifest':manifest}
            need(len(canonical(body)) <= MAX_BYTES, 'context_insufficient', 'Return manifest exceeds supported capacity')
            self.s.execute('INSERT INTO scope_returns VALUES(?,?,?,?,?,?,?,?)', (ident,owner['project'],scope,canonical(body).decode(),digest(body),'proposed',None,timestamp()))
            for p in packets:self.s.execute('INSERT INTO scope_return_packets VALUES(?,?,?,?,?,?,?,?)',p)
            self.c.sec.event(owner['project'],'scope_return_proposed',actor.id,{'proposal':ident,'scope':scope,'fragments':len(manifest),'root_scope_reduced':False})
        return self.get(actor,ident)

    def get(self, actor, proposal, offset=0, limit=100):
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200, 'invalid_range', 'Bounded page required')
        row = self._row(actor,proposal); manifest = row['body']['leaf_manifest']
        return {k:row[k] for k in ('id','scope','project','status','digest')} | {
            'material_digest':row['body']['material_digest'],'byte_budget':row['body']['byte_budget'],
            'leaf_count':len(manifest),'leaves':manifest[offset:offset+limit],
            'next_offset':offset+limit if offset+limit < len(manifest) else None,
            'result':row['result'],'deploy_ready':False}

    def list(self, actor, scope, offset=0, limit=50):
        self.c.workstreams._row(actor,scope)
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200, 'invalid_range', 'Bounded page required')
        rows = self.s.all('SELECT id,digest,status,created FROM scope_returns WHERE scope=? ORDER BY created,id LIMIT ? OFFSET ?', (scope,limit+1,offset))
        return {'scope':scope,'items':rows[:limit],'next_offset':offset+limit if len(rows)>limit else None,'deploy_ready':False}

    def _latest(self, p):
        refs = self.c.g.evidence_for(p['id'],p['digest'],'impact')
        need(refs,'review_required','An actual impact review of this node is required',p['id'])
        ev = self.c.g.require_review(refs[0]['id'],p['id'],p['digest'],{'impact'})
        need(set(p['body']['required_coverage']) <= set(ev['result']['covered']), 'review_coverage', 'Review must address the exact return node')
        need(not ev['result']['findings'], 'unresolved_findings', 'Return review has unresolved findings')
        return {'packet':p['id'],'packet_digest':p['digest'],'receipt':ev['id'],'run':ev['run'],'result':ev['result']}

    def _tree(self, actor, row, node, reviewed=False):
        """Recheck descendants and latest evidence; no persistent validity cache."""
        stack=[(node,False)]; visiting=set(); completed={}; leaves=[]
        while stack:
            p,after=stack.pop(); ident=p['id']; b=p['body']
            if not after:
                need(ident not in visiting and ident not in completed,'review_tree_overlap','Repeated/cyclic return subtree')
                visiting.add(ident); stack.append((p,True))
                if p['level']==0:leaves.append(ident)
                else:
                    for ref in reversed(b['children']):
                        child=self._node(actor,ref['packet'],row)
                        need(child['level']==p['level']-1 and child['digest']==ref['packet_digest'], 'stale_return_review','Synthesis child identity differs')
                        stack.append((child,False))
            else:
                if p['level']>0:
                    for ref in b['children']:
                        need(completed[ref['packet']] == ref,'stale_return_review','Synthesis must use latest exact complete child result',ref['packet'])
                completed[ident]=self._latest(p) if ident!=node['id'] or reviewed else None
                visiting.remove(ident)
        return leaves, completed[node['id']]

    def packet(self, actor, packet, historical=False):
        need(type(historical) is bool,'invalid_input','historical must be boolean')
        with self.s.transaction():
            p = self._node(actor,packet); row=self._row(actor,p['proposal']); self._leaves(actor,row)
            if not historical:self._current(actor,row); self._tree(actor,row,p)
            return {'id':p['id'],'proposal':p['proposal'],'digest':p['digest'],'body':p['body'],'historical':historical,'new_review_evidence':False}

    def review_subject(self, actor, packet, role):
        need(role=='impact','invalid_role','Return nodes require impact review')
        p=self.packet(actor,packet); row=self._row(actor,p['proposal'])
        empty={'format':'snapshot.v1','repos':{},'digest':digest({'repos':{}})}
        return row['project'],p['digest'],empty,p['body'],None

    def _synthesis(self,row,level,ordinal,children):
        return with_marker({'format':'daikibo.scope-return-review.v1','kind':'synthesis','proposal':row['id'],'scope':row['scope'],
            'material_digest':row['body']['material_digest'],'level':level,'ordinal':ordinal,'children':children,
            'instruction':'Independently judge these complete child impact judgments together. Check boundary conflicts and caveats, not merely that children passed. If context is insufficient return blocked. Responsibility returns to parent/root; requirements, tasks and gates must not be removed. Root synthesis is a distinct execution even for one leaf.'})

    def advance(self, actor, proposal, offset=0, limit=100):
        need(type(offset) is int and offset>=0 and type(limit) is int and 1<=limit<=200,'invalid_range','Bounded page required')
        with self.s.transaction():
            row=self._row(actor,proposal); actor.require('owner','agent',project=row['project']); self._current(actor,row)
            nodes=self._leaves(actor,row); level=0
            while True:
                judgments=[]; waiting=[]
                for p in nodes:
                    try:judgments.append(self._latest(p))
                    except Fault as exc:waiting.append({'packet':p['id'],'digest':p['digest'],'role':'impact','failure':exc.as_dict()})
                root=nodes[0]['id'] if level>0 and len(nodes)==1 else None
                if waiting or root:
                    return {'proposal':proposal,'proposal_digest':row['digest'],'level':level,'root_packet':root,
                            'ready':not waiting and root is not None,'pending_total':len(waiting),'pending':waiting[offset:offset+limit],
                            'next_offset':offset+limit if offset+limit<len(waiting) else None,
                            'root_review_receipt':judgments[0]['receipt'] if not waiting and root else None,'deploy_ready':False}
                level+=1;need(level<=MAX_LEVELS,'context_insufficient','Synthesis exceeds supported depth')
                groups=[];current=[]
                for result in judgments:
                    trial=current+[result]
                    if current and (len(trial)>16 or len(canonical(self._synthesis(row,level,len(groups),trial)))>row['body']['byte_budget']):
                        groups.append(current);current=[]
                    current.append(result)
                    need(len(canonical(self._synthesis(row,level,len(groups),current)))<=row['body']['byte_budget'],
                         'review_result_too_large','Complete review result does not fit synthesis budget. Re-review concisely or repropose a larger budget; no result was truncated.')
                if current:groups.append(current)
                need(len(nodes)==1 or len(groups)<len(nodes),'synthesis_cannot_reduce','Cannot combine whole results within budget; no result was truncated')
                nodes=[]
                for ordinal,children in enumerate(groups):
                    body=self._synthesis(row,level,ordinal,children);h=digest(body)
                    p=self.s.one('SELECT * FROM scope_return_packets WHERE proposal=? AND digest=?',(proposal,h))
                    if not p:
                        ident=uid('RPACK')
                        self.s.execute('INSERT INTO scope_return_packets VALUES(?,?,?,?,?,?,?,?)',(ident,proposal,row['project'],level,ordinal,canonical(body).decode(),h,timestamp()))
                        self.c.sec.event(row['project'],'scope_return_synthesis_created',actor.id,{'proposal':proposal,'packet':ident,'level':level})
                        p=self.s.one('SELECT * FROM scope_return_packets WHERE id=?',(ident,),True)
                    p['body']=parse_json(p['body']);packet_check(p,row);nodes.append(p)

    def apply(self, actor, proposal, expected_digest, root_packet, review_receipt):
        with self.s.transaction():
            row=self._row(actor,proposal);actor.require('owner','agent',project=row['project'])
            need(row['digest']==expected_digest,'stale_revision','Return proposal digest differs')
            if row['status']=='applied':
                need(row['result']['root_packet']==root_packet and row['result']['review_receipt']==review_receipt,
                     'idempotency_conflict','Replay must match the original applied return')
                return {**row['result'],'replayed':True}
            self._current(actor,row);leaves=self._leaves(actor,row);root=self._node(actor,root_packet,row)
            need(root['level']>0,'synthesis_required','Source fragments alone cannot authorize the whole return')
            covered,judgment=self._tree(actor,row,root,reviewed=True)
            need(covered==[p['id'] for p in leaves],'incomplete_return_review','Root synthesis must cover the complete original scope, in order')
            need(judgment['receipt']==review_receipt,'stale_evidence','Use latest root synthesis review')
            scope=self.c.workstreams._row(actor,row['scope'])
            self.s.execute("UPDATE workstreams SET status='withdrawn' WHERE id=?",(row['scope'],))
            event={'proposal':proposal,'proposal_digest':row['digest'],'root_packet':root_packet,'root_digest':root['digest'],
                   'review_receipt':review_receipt,'root_judgment':judgment,'material_digest':row['body']['material_digest'],
                   'reason':row['body']['material']['reason'],'tasks_cancelled':[],'root_scope_reduced':False,'deploy_ready':False}
            record=self.c.workstreams._record(scope,'withdraw',event)
            result={'proposal':proposal,'scope':row['scope'],'record':record,**event,'status':'withdrawn','replayed':False}
            self.s.execute("UPDATE scope_returns SET status='applied',result=? WHERE id=?",(canonical(result).decode(),proposal))
            self.c.sec.event(row['project'],'scope_return_applied',actor.id,{'proposal':proposal,'scope':row['scope'],'record':record,'root_scope_reduced':False})
            return result

    def abandon(self, actor, proposal, reason):
        text(reason,'abandon reason',20000)
        with self.s.transaction():
            row=self._row(actor,proposal);actor.require('owner','agent',project=row['project'])
            need(row['status']=='proposed','proposal_closed','Only open return proposals can be abandoned')
            result={'reason':reason,'actor':actor.id,'scope_unchanged':True}
            self.s.execute("UPDATE scope_returns SET status='abandoned',result=? WHERE id=?",(canonical(result).decode(),proposal))
            self.c.sec.event(row['project'],'scope_return_abandoned',actor.id,{'proposal':proposal,**result})
            return {'proposal':proposal,'status':'abandoned','scope_unchanged':True}
