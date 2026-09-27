"""Bounded discovery views, not substitutes for engineering completion gates.

Catalogs retain exact counts and content/revision cursors. Reading any page does
not approve a decision, classify source text, or certify a requirement.
"""
from __future__ import annotations
import hashlib
from .common import canonical, digest, need
from .planning import PHASE_ACTIONS, PHASE_KINDS


class Navigation:
    def __init__(self, control):
        self.c, self.s = control, control.s

    @staticmethod
    def _range(offset, limit, maximum=200):
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= maximum,
             'invalid_range', 'Use a nonnegative offset and a bounded positive limit')

    def _catalog(self, actor, project, sql, params, offset, limit, expected_snapshot, namespace, *, body_hash=False):
        self.c.k.project(actor, project); self._range(offset, limit)
        need(offset == 0 or expected_snapshot is not None, 'cursor_required', 'Continue with the preceding catalog snapshot')
        with self.s.transaction():
            h = hashlib.sha256(canonical({'catalog': namespace, 'project': project, 'params': params}))
            items, total = [], 0
            for raw in self.s.conn.execute(sql, params):
                row = dict(raw)
                if body_hash:
                    body = row.pop('body').encode('utf-8')
                    row['digest'] = hashlib.sha256(body).hexdigest(); row['body_bytes'] = len(body)
                h.update(canonical(row)); h.update(b'\n')
                if offset <= total < offset + limit: items.append(row)
                total += 1
            snapshot = h.hexdigest()
            need(expected_snapshot is None or expected_snapshot == snapshot, 'stale_catalog',
                 'The collection changed; restart its catalog before assembling pages')
            need(offset <= total, 'invalid_range', 'Catalog offset is past the end')
            return {'project': project, 'items': items, 'total': total, 'offset': offset,
                    'next_offset': offset + len(items) if offset + len(items) < total else None,
                    'snapshot': snapshot, 'bodies_included': False, 'acknowledgement': False}

    def artifacts(self, actor, project, kind=None, status=None, offset=0, limit=50, expected_snapshot=None):
        return self._catalog(actor, project,
            "SELECT id,kind,revision,status,digest,substr(json_extract(body,'$.title'),1,200) AS title_label,length(CAST(body AS BLOB)) AS body_bytes FROM artifacts WHERE project=? AND (? IS NULL OR kind=?) AND (? IS NULL OR status=?) ORDER BY id",
            (project,kind,kind,status,status), offset,limit,expected_snapshot,'artifacts')

    def programs(self, actor, project, offset=0, limit=50, expected_snapshot=None):
        return self._catalog(actor, project,
            "SELECT id,phase,revision,json_extract(body,'$.source') AS source,json_extract(body,'$.mode') AS mode FROM programs WHERE project=? ORDER BY created,id",
            (project,), offset,limit,expected_snapshot,'programs')

    def inbox(self, actor, project, offset=0, limit=50, expected_snapshot=None):
        result = self._catalog(actor, project,
            "SELECT id,kind,ref,severity,status,due,body FROM inbox WHERE project=? AND status='open' ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,created,id",
            (project,), offset,limit,expected_snapshot,'inbox',body_hash=True)
        result['instruction'] = 'All open notices remain pending. Read their bodies with inbox.read; listing or reading is NOT user acknowledgement.'
        return result

    @staticmethod
    def _fragment(raw, offset, byte_budget, expected_digest, identity):
        Navigation._range(offset, byte_budget, 65536)
        need(byte_budget >= 64, 'invalid_range', 'Use a byte budget of at least 64')
        h = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        need(expected_digest == h, 'stale_fragment', 'Read the catalog again; exact content digest is required')
        need(offset <= len(raw), 'invalid_range', 'Character offset is past the body')
        # Offsets are Unicode characters; the budget is UTF-8 bytes. Never split
        # a code point. The caller reconstructs canonical JSON from exact ranges.
        end = min(len(raw), offset + byte_budget)
        part = raw[offset:end]
        if len(part.encode('utf-8')) > byte_budget:
            lo, hi = offset, end
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(raw[offset:mid].encode('utf-8')) <= byte_budget: lo = mid
                else: hi = mid - 1
            end, part = lo, raw[offset:lo]
        return {**identity, 'content_digest': h, 'encoding': 'canonical-json-utf8',
                'start': offset, 'end': end, 'total_characters': len(raw), 'content': part,
                'next_offset': end if end < len(raw) else None, 'complete_body': offset == 0 and end == len(raw),
                'semantic_review': False, 'acknowledgement': False}

    def artifact_read(self, actor, artifact, expected_digest, offset=0, byte_budget=12000, revision=None):
        with self.s.transaction():
            row = self.s.one('SELECT * FROM artifacts WHERE id=?', (artifact,), True)
            self.c.k.project(actor, row['project'])
            if revision is not None:
                need(type(revision) is int and revision > 0, 'invalid_revision', 'Revision must be a positive integer')
                previous = self.s.one('SELECT body,digest,revision FROM revisions WHERE artifact=? AND revision=?', (artifact,revision), True)
                row.update(previous)
            return self._fragment(row['body'], offset,byte_budget,expected_digest,
                                  {'artifact': artifact, 'project': row['project'], 'revision': row['revision']})

    def inbox_read(self, actor, item, expected_digest, offset=0, byte_budget=12000):
        with self.s.transaction():
            row = self.s.one('SELECT * FROM inbox WHERE id=?', (item,), True)
            self.c.k.project(actor, row['project'])
            return self._fragment(row['body'], offset,byte_budget,expected_digest,
                                  {'item': item, 'project': row['project'], 'status': row['status']})

    def blockers(self, actor, program, offset=0, limit=50, expected_snapshot=None, byte_budget=24000):
        self._range(offset,limit)
        need(type(byte_budget) is int and 1024 <= byte_budget <= 65536, 'invalid_range', 'Invalid blocker page budget')
        need(offset == 0 or expected_snapshot is not None, 'cursor_required', 'Continue with the preceding blocker snapshot')
        with self.s.transaction():
            row = self.s.one('SELECT * FROM programs WHERE id=?', (program,), True)
            self.c.k.project(actor, row['project'])
            failures = self.c.p.phase_blockers(row)  # Deliberately complete, never page the gate itself.
            binding = self.c.p.program_binding(program)
            snapshot = digest({'binding': binding, 'blockers': failures})
            need(expected_snapshot is None or expected_snapshot == snapshot, 'stale_catalog', 'Phase conditions changed')
            need(offset <= len(failures), 'invalid_range', 'Offset is past the end')
            items, used = [], 0
            for failure in failures[offset:offset+limit]:
                cost = len(canonical(failure)) + 1
                if cost + used > byte_budget:
                    need(bool(items), 'context_insufficient', 'One blocker exceeds the page budget')
                    break
                items.append(failure); used += cost
            return {'program': program, 'project': row['project'], 'phase': row['phase'], 'revision': row['revision'],
                    'binding': binding, 'snapshot': snapshot, 'instructions': PHASE_ACTIONS[row['phase']],
                    'required_output_kind': PHASE_KINDS.get(row['phase']), 'items': items, 'total': len(failures),
                    'offset': offset, 'next_offset': offset+len(items) if offset+len(items)<len(failures) else None,
                    'phase_complete': False, 'all_structural_blockers_clear': not failures,
                    'note': 'An empty page is not phase approval. program.advance still requires full checks and observed reviews.'}

    def summary(self, actor, project):
        self.c.k.project(actor,project)
        with self.s.transaction():
            return {'project': project,
                'counts': self.s.all('SELECT status,validity,count(*) AS count FROM tasks WHERE project=? GROUP BY status,validity',(project,)),
                'block_counts': self.s.all('SELECT b.kind,count(*) AS count FROM blocks b JOIN tasks t ON t.id=b.task WHERE t.project=? GROUP BY b.kind',(project,)),
                'notice_counts': self.s.all("SELECT severity,kind,count(*) AS count FROM inbox WHERE project=? AND status='open' GROUP BY severity,kind",(project,)),
                'exception_counts': self.s.all("SELECT status,count(*) AS count FROM waivers WHERE project=? AND status!='closed' GROUP BY status",(project,)),
                'program_count': self.s.one('SELECT count(*) AS n FROM programs WHERE project=?',(project,))['n'],
                'assurance': self.c.g.mode, 'completion_evaluated': False,
                'detail_operations': ['program.catalog','program.blockers','artifact.catalog','artifact.read','inbox.catalog','inbox.read','workflow.status']}
