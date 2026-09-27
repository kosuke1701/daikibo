"""Durable bounded submission of complete work breakdowns.

This is a staging area, never an alternate scope registry or an approval path.
Every finalized upload passes through Breakdowns.propose and its normal gates.
"""
from __future__ import annotations

from .common import canonical, digest, need, parse_json, text, timestamp, uid
from .breakdowns import MAX_PROPOSAL_BYTES

SCHEMA = """
CREATE TABLE IF NOT EXISTS breakdown_uploads(
 id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id),
 project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)),
 scope_digest TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('open','finalized','abandoned')), result TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS breakdown_upload_units(
 upload TEXT NOT NULL REFERENCES breakdown_uploads(id), unit TEXT NOT NULL,
 ordinal INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), bytes INTEGER NOT NULL,
 PRIMARY KEY(upload,unit), UNIQUE(upload,ordinal)
);
CREATE TABLE IF NOT EXISTS breakdown_upload_batches(
 upload TEXT NOT NULL REFERENCES breakdown_uploads(id), revision INTEGER NOT NULL,
 digest TEXT NOT NULL, result TEXT NOT NULL CHECK(json_valid(result)),
 PRIMARY KEY(upload,revision)
);
"""
MAX_BATCH_BYTES = 1024 * 1024


class BreakdownInputs:
    def __init__(self, control):
        self.c, self.s = control, control.s

    def _row(self, actor, upload):
        row = self.s.one('SELECT * FROM breakdown_uploads WHERE id=?', (upload,), True)
        self.c.k.project(actor, row['project'])
        row['body'] = parse_json(row['body'])
        return row

    def begin(self, actor, program, title, rationale, expected_active=None, byte_budget=24000):
        row = self.c.breakdowns._program(actor, program)
        actor.require('owner', 'agent', project=row['project'])
        text(title, 'breakdown title', 400); text(rationale, 'rationale', 20000)
        need(type(byte_budget) is int and 4096 <= byte_budget <= 100000,
             'invalid_budget', 'Packet budget must be 4096..100000 bytes')
        with self.s.transaction():
            active = self.c.breakdowns.active(actor, program)
            need((active['id'] if active else None) == expected_active,
                 'stale_breakdown', 'Begin from the current adopted plan')
            scope = self.c.breakdowns._scope(actor, row['project'])
            ident = uid('UPLOAD')
            body = {'title':title, 'rationale':rationale, 'expected_active':expected_active,
                    'byte_budget':byte_budget}
            self.s.execute('INSERT INTO breakdown_uploads VALUES(?,?,?,?,?,0,\'open\',NULL,?)',
                           (ident,program,row['project'],canonical(body).decode(),digest(scope),timestamp()))
            self.c.sec.event(row['project'], 'breakdown_upload_started', actor.id,
                             {'upload':ident,'program':program,'scope_digest':digest(scope)})
        return self.status(actor, ident)

    def status(self, actor, upload, offset=0, limit=100):
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
             'invalid_range', 'Expected a bounded page')
        row = self._row(actor, upload)
        totals = self.s.one('SELECT count(*) AS units,COALESCE(sum(bytes),0) AS bytes FROM breakdown_upload_units WHERE upload=?', (upload,))
        page = self.s.all('SELECT unit,ordinal,bytes FROM breakdown_upload_units WHERE upload=? ORDER BY ordinal LIMIT ? OFFSET ?', (upload,limit,offset))
        return {'upload':upload,'program':row['program'],'status':row['status'],'revision':row['revision'],
                'scope_digest':row['scope_digest'], **totals, 'page':page,
                'next_offset':offset+len(page) if offset+len(page)<totals['units'] else None,
                'result':parse_json(row['result']) if row['result'] else None,
                'adopted':False, 'next_operation':'put more units or finalize; reviews and activation remain mandatory'}

    def list(self, actor, project, program=None, status=None, offset=0, limit=50):
        self.c.k.project(actor,project)
        need(type(offset) is int and offset>=0 and type(limit) is int and 1<=limit<=200,
             'invalid_range','Expected a bounded upload list')
        need(status in {None,'open','finalized','abandoned'},'invalid_status','Unknown upload status')
        if program is not None:
            need(self.c.breakdowns._program(actor,program)['project']==project,'cross_project','Program belongs elsewhere')
        rows=self.s.all("""SELECT id,program,status,revision,scope_digest,created,
                json_extract(body,'$.title') AS title FROM breakdown_uploads
            WHERE project=? AND (? IS NULL OR program=?) AND (? IS NULL OR status=?)
            ORDER BY created,id LIMIT ? OFFSET ?""",(project,program,program,status,status,limit+1,offset))
        return {'project':project,'uploads':rows[:limit],'next_offset':offset+limit if len(rows)>limit else None,
                'note':'Staging records, not adopted engineering plans. Query upload_status before resuming.'}

    def put(self, actor, upload, expected_revision, units):
        need(type(expected_revision) is int and expected_revision >= 0, 'invalid_revision', 'Use the last upload revision')
        need(isinstance(units,list) and 1 <= len(units) <= 200, 'invalid_batch', 'Provide 1..200 units per batch')
        data = canonical(units)
        need(len(data) <= MAX_BATCH_BYTES, 'batch_too_large', 'Split the batch without discarding any units')
        # Validate enough to index safely; global structure is checked at finalize.
        seen = set()
        for unit in units:
            need(isinstance(unit,dict), 'invalid_unit', 'A unit must be an object')
            text(unit.get('id'), 'unit ID', 120)
            need(unit['id'] not in seen, 'duplicate_unit', 'Duplicate unit in this batch')
            seen.add(unit['id'])
        with self.s.transaction():
            row = self._row(actor,upload); actor.require('owner','agent',project=row['project'])
            previous = self.s.one('SELECT digest,result FROM breakdown_upload_batches WHERE upload=? AND revision=?', (upload,expected_revision))
            if previous:
                need(previous['digest']==digest(data), 'idempotency_conflict', 'This revision already received different units')
                need(row['status'] != 'abandoned', 'upload_closed', 'Upload was abandoned')
                return parse_json(previous['result']) | {'replayed':True}
            need(row['status']=='open','upload_closed','Upload is not open')
            need(row['revision']==expected_revision,'stale_revision','Upload changed; fetch its current status')
            old = self.s.one('SELECT count(*) AS n,COALESCE(sum(bytes),0) AS bytes FROM breakdown_upload_units WHERE upload=?', (upload,))
            need(old['n']+len(units)<=10000 and old['bytes']+len(data)<=MAX_PROPOSAL_BYTES,
                 'upload_capacity', 'Explicit complete-plan capacity exceeded; no partial plan accepted')
            for ordinal,unit in enumerate(units,old['n']):
                need(not self.s.one('SELECT unit FROM breakdown_upload_units WHERE upload=? AND unit=?',(upload,unit['id'])),
                     'duplicate_unit','A unit from an earlier batch cannot be silently overwritten',unit['id'])
                encoded = canonical(unit)
                self.s.execute('INSERT INTO breakdown_upload_units VALUES(?,?,?,?,?)', (upload,unit['id'],ordinal,encoded.decode(),len(encoded)))
            self.s.execute('UPDATE breakdown_uploads SET revision=revision+1 WHERE id=?',(upload,))
            result={'upload':upload,'revision':expected_revision+1,'units':old['n']+len(units),'status':'open','replayed':False}
            self.s.execute('INSERT INTO breakdown_upload_batches VALUES(?,?,?,?)',(upload,expected_revision,digest(data),canonical(result).decode()))
            self.c.sec.event(row['project'],'breakdown_upload_appended',actor.id,{'upload':upload,'revision':expected_revision+1,'units_added':len(units)})
            return result

    def finalize(self, actor, upload, expected_revision):
        with self.s.transaction():
            row = self._row(actor, upload); actor.require('owner','agent',project=row['project'])
            need(type(expected_revision) is int and row['revision']==expected_revision,
                 'stale_revision','Upload changed before finalization')
            if row['status']=='finalized':
                return parse_json(row['result']) | {'upload':upload,'replayed':True}
            need(row['status']=='open','upload_closed','Upload was abandoned')
            current=self.c.breakdowns._scope(actor,row['project'])
            need(digest(current)==row['scope_digest'],'stale_upload_scope',
                 'Requirements, task definitions or policy changed; make an explicit new upload')
            units=[parse_json(r['body']) for r in self.s.all('SELECT body FROM breakdown_upload_units WHERE upload=? ORDER BY ordinal',(upload,))]
            result=self.c.breakdowns.propose(actor,row['program'],units=units,**row['body'])
            self.s.execute("UPDATE breakdown_uploads SET status='finalized',result=? WHERE id=?",(canonical(result).decode(),upload))
            self.c.sec.event(row['project'],'breakdown_upload_finalized',actor.id,{'upload':upload,'proposal':result['id'],'adopted':False})
            return result | {'upload':upload,'replayed':False}

    def abandon(self, actor, upload, expected_revision, reason):
        text(reason,'abandon reason',4000)
        with self.s.transaction():
            row=self._row(actor,upload); actor.require('owner','agent',project=row['project'])
            need(row['revision']==expected_revision,'stale_revision','Upload changed')
            need(row['status']=='open','upload_closed','Only an open upload can be abandoned')
            self.s.execute("UPDATE breakdown_uploads SET status='abandoned',revision=revision+1 WHERE id=?",(upload,))
            self.c.sec.event(row['project'],'breakdown_upload_abandoned',actor.id,{'upload':upload,'reason':reason,'scope_changed':False})
        return self.status(actor,upload)
